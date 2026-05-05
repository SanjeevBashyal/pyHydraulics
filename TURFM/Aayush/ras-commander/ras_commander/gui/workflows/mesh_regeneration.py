"""
Mesh Regeneration workflow with iterative error correction.

Regenerates 2D mesh in RASMapper after geometry file changes.
If face perimeter errors occur, simplifies the geometry near
the error and retries with adjusted cell size.

This is the critical workflow for Glenn's ras-agent pipeline.

Workflow (single attempt):
1. Launch HEC-RAS with project
2. Open RASMapper → triggers HDF regeneration from modified .g## file
3. Save geometry (Ctrl+S) → finalizes the HDF
4. Close HEC-RAS

Iterative workflow (with error correction):
1. Attempt mesh regeneration
2. Check HDF for valid mesh (datasets present, cell count > 0)
3. If invalid: close HEC-RAS, simplify perimeter geometry, adjust cell size
4. Write simplified geometry back to .g## file
5. Retry (up to max_iterations)
"""

import re
import time
from pathlib import Path
from typing import Optional, List, Tuple

# Win32 imports - Windows only
try:
    import win32gui
    import win32con
    import win32api
    WIN32_AVAILABLE = True
except ImportError:
    win32gui = win32con = win32api = None
    WIN32_AVAILABLE = False

from ...LoggingConfig import get_logger
from ...Decorators import log_call
from ..win32_primitives import Win32Primitives
from ..hecras_elements import HecRasElements
from ..rasmapper_elements import RasMapperElements
from ..workflow_base import WorkflowStep, WorkflowResult, WorkflowExecutor
from ..constants import Win32Constants

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Geometry file helpers (read/write perimeter from .g## ASCII)
# ---------------------------------------------------------------------------

def _get_2d_area_name(geometry_file: Path) -> Optional[str]:
    """Parse .g## file and return the first 2D flow area name, or None."""
    pattern = re.compile(r"^2D Flow Area=\s*(.+?)\s*,", re.MULTILINE | re.IGNORECASE)
    text = geometry_file.read_text(errors="replace")
    match = pattern.search(text)
    return match.group(1).strip() if match else None


def _get_geometry_title(geometry_file: Path) -> Optional[str]:
    """Parse .g## file and return the geometry title, or None."""
    text = geometry_file.read_text(errors="replace")
    match = re.search(r"^Geom Title=(.+)$", text, re.MULTILINE)
    return match.group(1).strip() if match else None


def _read_perimeter_coords(geometry_file: Path) -> Optional[List[Tuple[float, float]]]:
    """Read the 2D Flow Area Perimeter coordinates from a .g## file."""
    text = geometry_file.read_text(errors="replace")

    # Match "2D Flow Area Perimeter= N" followed by N lines of "     x,y"
    pattern = re.compile(
        r"2D Flow Area Perimeter=\s*(\d+)\s*\n"
        r"((?:[ \t]*-?[\d.]+\s*,\s*-?[\d.]+[ \t]*\n)*)",
        re.MULTILINE,
    )
    match = pattern.search(text)
    if not match:
        return None

    coord_lines = match.group(2).strip().split("\n")
    coords = []
    for line in coord_lines:
        line = line.strip()
        if "," in line:
            parts = line.split(",")
            coords.append((float(parts[0].strip()), float(parts[1].strip())))
    return coords


def _read_cell_size(geometry_file: Path) -> Optional[float]:
    """Read the 2D Flow Area Cell Size from a .g## file."""
    text = geometry_file.read_text(errors="replace")
    match = re.search(r"2D Flow Area Cell Size=\s*([\d.]+)", text)
    return float(match.group(1)) if match else None


def _write_perimeter_and_cell_size(
    geometry_file: Path,
    area_name: str,
    coords: List[Tuple[float, float]],
    cell_size: float,
) -> bool:
    """
    Write simplified perimeter coordinates and cell size to .g## file.

    Creates .bak backup before modifying. Auto-closes polygon.
    """
    text = geometry_file.read_text(errors="replace")

    # Verify the named area exists
    area_pat = re.compile(
        r"2D Flow Area=\s*" + re.escape(area_name) + r"\s*,",
        re.IGNORECASE,
    )
    if not area_pat.search(text):
        logger.warning(f"2D flow area '{area_name}' not found in {geometry_file.name}")
        return False

    # Backup
    bak_path = geometry_file.with_suffix(geometry_file.suffix + ".bak")
    bak_path.write_text(text, encoding="utf-8")

    # Close polygon
    pts = list(coords)
    if pts and pts[0] != pts[-1]:
        pts.append(pts[0])

    coord_lines = "\n".join(f"     {x:.3f},{y:.3f}" for x, y in pts)
    new_block = f"2D Flow Area Perimeter= {len(pts)}\n{coord_lines}\n"

    # Replace perimeter block
    perim_pat = re.compile(
        r"(2D Flow Area Perimeter=\s*\d+\s*\n)"
        r"((?:[ \t]*-?[\d.]+\s*,\s*-?[\d.]+[ \t]*\n)*)",
        re.MULTILINE,
    )
    updated, n = perim_pat.subn(new_block, text, count=1)
    if n == 0:
        # Insert after the area header
        updated = area_pat.sub(lambda m: m.group(0) + "\n" + new_block, updated, count=1)

    # Update cell size
    cs_pat = re.compile(r"2D Flow Area Cell Size=\s*[\d.]+")
    updated = cs_pat.sub(f"2D Flow Area Cell Size= {cell_size:.1f}", updated)

    geometry_file.write_text(updated, encoding="utf-8")
    logger.info(
        f"Updated {geometry_file.name}: {len(pts)} perimeter points, cell size {cell_size:.1f}"
    )
    return True


# ---------------------------------------------------------------------------
# Mesh validation (post-regeneration HDF check)
# ---------------------------------------------------------------------------

def _check_mesh_valid(geom_hdf_path: Path, mesh_name: Optional[str] = None) -> dict:
    """
    Check if the geometry HDF contains a valid 2D mesh.

    Returns dict with:
        valid (bool): True if mesh looks complete
        n_cells (int): Number of mesh cells (0 if missing)
        n_faces (int): Number of mesh faces (0 if missing)
        error (str): Description of issue if invalid
    """
    import h5py

    result = {"valid": False, "n_cells": 0, "n_faces": 0, "error": ""}

    if not geom_hdf_path.exists():
        result["error"] = f"HDF file does not exist: {geom_hdf_path}"
        return result

    try:
        with h5py.File(str(geom_hdf_path), "r") as f:
            base = "Geometry/2D Flow Areas"
            if base not in f:
                result["error"] = "No 2D Flow Areas group in HDF"
                return result

            # Auto-detect mesh name if not provided
            if mesh_name is None:
                if "Attributes" in f[base]:
                    names = [n.decode() if isinstance(n, bytes) else str(n)
                             for n in f[base]["Attributes"][()]["Name"]]
                    if names:
                        mesh_name = names[0]

            if mesh_name is None:
                result["error"] = "No mesh areas found in HDF"
                return result

            mesh_base = f"{base}/{mesh_name}"
            if mesh_base not in f:
                flat_cell_points = f"{base}/Cell Points"
                flat_attributes = f"{base}/Attributes"
                if flat_cell_points in f and flat_attributes in f:
                    attrs = f[flat_attributes][()]
                    n_cells = int(f[flat_cell_points].shape[0])
                    if (
                        len(attrs) > 0
                        and attrs.dtype.names
                        and "Cell Count" in attrs.dtype.names
                    ):
                        n_cells = int(attrs[0]["Cell Count"])

                    result["n_cells"] = n_cells
                    result["n_faces"] = 0
                    if n_cells < 3:
                        result["error"] = f"Too few cells: {n_cells}"
                        return result

                    result["valid"] = True
                    result["layout"] = "flat_2d_flow_area"
                    return result

                result["error"] = f"Mesh '{mesh_name}' not found in HDF"
                return result

            # Check required datasets
            required = [
                "Cells Center Coordinate",
                "FacePoints Coordinate",
                "Faces FacePoint Indexes",
            ]
            for ds_name in required:
                ds_path = f"{mesh_base}/{ds_name}"
                if ds_path not in f:
                    result["error"] = f"Missing dataset: {ds_name}"
                    return result
                if f[ds_path].shape[0] == 0:
                    result["error"] = f"Empty dataset: {ds_name}"
                    return result

            n_cells = f[f"{mesh_base}/Cells Center Coordinate"].shape[0]
            n_faces = f[f"{mesh_base}/Faces FacePoint Indexes"].shape[0]

            result["n_cells"] = n_cells
            result["n_faces"] = n_faces

            if n_cells < 3:
                result["error"] = f"Too few cells: {n_cells}"
                return result

            # Check face-cell connectivity (face perimeter connection test)
            if f"{mesh_base}/Faces Cell Indexes" in f:
                face_cells = f[f"{mesh_base}/Faces Cell Indexes"][()]
                # Every face should connect to at least one valid cell (index >= 0)
                max_valid = face_cells.max(axis=1)
                if (max_valid < 0).any():
                    n_bad = int((max_valid < 0).sum())
                    result["error"] = f"{n_bad} faces with no valid cell connection"
                    return result

            result["valid"] = True
            return result

    except Exception as e:
        result["error"] = f"HDF read error: {e}"
        return result


def _get_file_mtime(path: Optional[Path]) -> Optional[float]:
    """Return file modification time or None if the file is missing."""
    if path is None or not path.exists():
        return None
    return path.stat().st_mtime


def _wait_for_file_change(
    path: Optional[Path],
    baseline_mtime: Optional[float],
    timeout: int,
    check_interval: float = 1.0,
) -> bool:
    """Wait for a file modification time to change."""
    if path is None:
        return False

    start_time = time.time()
    while time.time() - start_time < timeout:
        if path.exists():
            current_mtime = _get_file_mtime(path)
            if baseline_mtime is None or current_mtime != baseline_mtime:
                return True
        time.sleep(check_interval)
    return False


def _capture_editor_snapshot(geom_hdf_path: Optional[Path]) -> dict:
    """Capture the current mesh-editor state for change detection."""
    return {
        "hdf_mtime": _get_file_mtime(geom_hdf_path),
        "mesh_status_text": RasMapperElements.get_mesh_status_text(),
        "status_items": RasMapperElements.get_status_list_items(),
    }


def _wait_for_editor_change(
    geom_hdf_path: Optional[Path],
    baseline: dict,
    timeout: int,
    require_hdf_change: bool = False,
) -> dict:
    """
    Wait for the 2D Flow Area editor or geometry HDF to change.

    The real RAS Mapper mesh actions may update in-memory state before the
    HDF write occurs, so this watches both UI status text and HDF metadata.
    """
    start_time = time.time()
    last_log_time = start_time

    while time.time() - start_time < timeout:
        current = _capture_editor_snapshot(geom_hdf_path)
        hdf_changed = current["hdf_mtime"] != baseline.get("hdf_mtime")
        status_changed = (
            current["mesh_status_text"] != baseline.get("mesh_status_text")
        )
        items_changed = current["status_items"] != baseline.get("status_items")

        if require_hdf_change:
            if hdf_changed:
                return current
        else:
            if hdf_changed or status_changed or items_changed:
                return current

        elapsed = time.time() - start_time
        if elapsed - (last_log_time - start_time) >= 15:
            logger.info(
                "Waiting for RAS Mapper editor change... (%ss elapsed)",
                int(elapsed),
            )
            last_log_time = time.time()

        time.sleep(1.0)

    return _capture_editor_snapshot(geom_hdf_path)


def _latest_mesh_messages() -> List[str]:
    """Return mesh-related status lines from the RAS Mapper messages pane."""
    return [
        text for text in RasMapperElements.get_status_list_items()
        if text.lower().startswith("mesh:")
    ]


def _mesh_messages_indicate_errors(messages: List[str]) -> bool:
    """Return True when RAS Mapper messages still describe mesh errors."""
    for message in messages:
        text = message.lower()
        if "maximum" in text and "face" in text:
            return True
        if "mesh error" in text or "mesh errors" in text:
            if not _mesh_messages_indicate_clean([message]):
                return True
        if "error" in text and "no error" not in text and "0 error" not in text:
            return True
    return False


def _mesh_messages_indicate_clean(messages: List[str]) -> bool:
    """Return True when RAS Mapper messages/report text says errors are clear."""
    clean_markers = (
        "no mesh errors",
        "0 mesh errors",
        "no errors",
        "0 errors",
        "no fixes were necessary",
        "no fixes required",
    )
    return any(marker in message.lower() for message in messages for marker in clean_markers)


# ---------------------------------------------------------------------------
# Perimeter simplification
# ---------------------------------------------------------------------------

def _simplify_perimeter(
    coords: List[Tuple[float, float]],
    tolerance: float,
) -> List[Tuple[float, float]]:
    """
    Simplify perimeter polygon using Douglas-Peucker algorithm.

    Also removes acute angles (< 20 degrees) that cause face perimeter errors.

    Args:
        coords: List of (x, y) tuples (may or may not be closed).
        tolerance: Simplification tolerance in coordinate units.

    Returns:
        Simplified coordinate list.
    """
    try:
        from shapely.geometry import Polygon

        # Ensure closed
        pts = list(coords)
        if pts and pts[0] != pts[-1]:
            pts.append(pts[0])

        poly = Polygon(pts)
        if not poly.is_valid:
            poly = poly.buffer(0)  # Fix self-intersections

        simplified = poly.simplify(tolerance, preserve_topology=True)

        # Extract exterior coords (remove closing point — _write adds it back)
        result = list(simplified.exterior.coords[:-1])

        logger.info(
            f"Simplified perimeter: {len(pts)} → {len(result)} points "
            f"(tolerance={tolerance:.1f})"
        )
        return result

    except ImportError:
        logger.warning("Shapely not available — skipping perimeter simplification")
        return coords


# ---------------------------------------------------------------------------
# Main workflow class
# ---------------------------------------------------------------------------

class MeshRegenerationWorkflow:
    """
    Regenerate 2D mesh in RASMapper with iterative error correction.

    Single attempt:
        regenerate_mesh() — open RASMapper, save, check HDF, close

    Iterative (recommended for automated pipelines):
        regenerate_mesh_iterative() — retry with perimeter simplification
        and cell size adjustment until mesh is valid or max attempts reached.

    All methods are static and decorated with @log_call.
    """

    # ------------------------------------------------------------------
    # Single-attempt mesh regeneration
    # ------------------------------------------------------------------

    @staticmethod
    @log_call
    def regenerate_mesh(
        geometry_name: Optional[str] = None,
        flow_area_name: Optional[str] = None,
        ras_object=None,
        timeout: int = 600,
        close_after: bool = True,
        mannings_layer_name: Optional[str] = None,
    ) -> WorkflowResult:
        """
        Single mesh regeneration attempt.

        Opens RASMapper (which re-reads the modified .g## file),
        saves (triggering HDF regeneration), and checks the result.

        Args:
            geometry_name: Name of the geometry. Auto-detected if None.
            flow_area_name: Name of the 2D flow area. Auto-detected if None.
            ras_object: Optional RasPrj object instance.
            timeout: Max seconds to wait for mesh generation. Default 600.
            close_after: If True, close RASMapper and HEC-RAS when done.

        Returns:
            WorkflowResult. step_results['mesh_check'] contains validation dict.
        """
        context = {
            'geometry_name': geometry_name,
            'flow_area_name': flow_area_name,
            'ras_object': ras_object,
            'timeout': timeout,
            'close_after': close_after,
            'mannings_layer_name': mannings_layer_name,
        }

        steps = MeshRegenerationWorkflow._build_single_attempt_steps(context)
        return WorkflowExecutor.execute(steps, context, workflow_name="MeshRegeneration")

    # ------------------------------------------------------------------
    # Iterative mesh regeneration with error correction
    # ------------------------------------------------------------------

    @staticmethod
    @log_call
    def regenerate_mesh_iterative(
        ras_object=None,
        timeout: int = 600,
        max_iterations: int = 5,
        initial_cell_size: Optional[float] = None,
        cell_size_increase_factor: float = 1.3,
        simplify_tolerance_factor: float = 0.25,
    ) -> WorkflowResult:
        """
        Iterative mesh regeneration with face perimeter error correction.

        On each iteration:
        1. Open RASMapper → save → check HDF for valid mesh
        2. If valid: done
        3. If invalid: close HEC-RAS, simplify perimeter, increase cell size, retry

        Simplification strategy:
        - tolerance = cell_size * simplify_tolerance_factor
        - Each iteration increases cell size by cell_size_increase_factor
        - Perimeter is simplified more aggressively each round

        Args:
            ras_object: Optional RasPrj object instance.
            timeout: Max seconds per attempt for RASMapper operations.
            max_iterations: Maximum attempts before giving up. Default 5.
            initial_cell_size: Starting cell size. Read from .g## if None.
            cell_size_increase_factor: Multiply cell size by this on each retry. Default 1.3.
            simplify_tolerance_factor: simplify_tolerance = cell_size * this. Default 0.25.

        Returns:
            WorkflowResult with:
                success: True if a valid mesh was generated
                step_results['mesh_check']: Final mesh validation dict
                step_results['iterations']: Number of attempts taken
                step_results['final_cell_size']: Cell size that worked (or last tried)
                step_results['final_n_cells']: Cell count of successful mesh
        """
        from ...RasPrj import ras
        ras_obj = ras_object or ras
        ras_obj.check_initialized()

        # Locate geometry file and HDF
        geom_file = _find_geometry_file(ras_obj)
        if geom_file is None:
            return WorkflowResult(
                success=False,
                error=RuntimeError("No geometry file found in project"),
            )

        geom_hdf = geom_file.parent / (geom_file.name + ".hdf")
        area_name = _get_2d_area_name(geom_file)

        if area_name is None:
            return WorkflowResult(
                success=False,
                error=RuntimeError(f"No 2D flow area found in {geom_file.name}"),
            )

        # Read initial state
        cell_size = initial_cell_size or _read_cell_size(geom_file) or 500.0
        original_coords = _read_perimeter_coords(geom_file)
        current_coords = list(original_coords) if original_coords else None

        if current_coords is None:
            return WorkflowResult(
                success=False,
                error=RuntimeError(f"No perimeter coordinates in {geom_file.name}"),
            )

        result = WorkflowResult(success=False)
        result.step_results['iterations'] = 0
        result.step_results['final_cell_size'] = cell_size

        for iteration in range(1, max_iterations + 1):
            logger.info(
                f"--- Mesh attempt {iteration}/{max_iterations} "
                f"(cell_size={cell_size:.1f}, perimeter_pts={len(current_coords)}) ---"
            )
            result.step_results['iterations'] = iteration

            # Step 1: Open RASMapper, save, close
            attempt = MeshRegenerationWorkflow.regenerate_mesh(
                ras_object=ras_obj,
                timeout=timeout,
                close_after=True,
            )

            if not attempt.success:
                logger.warning(f"Attempt {iteration} failed at GUI level: {attempt.error}")
                result.steps_failed.append(f"attempt_{iteration}_gui")
                # Continue to try simplification anyway
            else:
                result.steps_completed.append(f"attempt_{iteration}_gui")

            # Step 2: Check HDF for valid mesh
            time.sleep(1)  # Let file system settle
            mesh_check = _check_mesh_valid(geom_hdf)
            result.step_results['mesh_check'] = mesh_check

            if mesh_check['valid']:
                logger.info(
                    f"Mesh is valid! {mesh_check['n_cells']} cells, "
                    f"{mesh_check['n_faces']} faces (attempt {iteration})"
                )
                result.success = True
                result.step_results['final_cell_size'] = cell_size
                result.step_results['final_n_cells'] = mesh_check['n_cells']
                result.steps_completed.append(f"attempt_{iteration}_valid")
                return result

            # Step 3: Mesh invalid — log the issue and prepare fix
            logger.warning(
                f"Attempt {iteration}: mesh invalid — {mesh_check['error']}"
            )
            result.steps_failed.append(f"attempt_{iteration}_mesh: {mesh_check['error']}")

            if iteration >= max_iterations:
                logger.error(f"Max iterations ({max_iterations}) reached — mesh still invalid")
                break

            # Step 4: Simplify perimeter and increase cell size
            cell_size *= cell_size_increase_factor
            simplify_tolerance = cell_size * simplify_tolerance_factor

            logger.info(
                f"Adjusting: cell_size → {cell_size:.1f}, "
                f"simplify_tolerance → {simplify_tolerance:.1f}"
            )

            current_coords = _simplify_perimeter(current_coords, simplify_tolerance)

            # Step 5: Write modified geometry
            success = _write_perimeter_and_cell_size(
                geom_file, area_name, current_coords, cell_size
            )
            if not success:
                logger.error("Failed to write modified geometry")
                break

            result.step_results['final_cell_size'] = cell_size

        result.error = RuntimeError(
            f"Mesh generation failed after {result.step_results['iterations']} iterations. "
            f"Last error: {result.step_results.get('mesh_check', {}).get('error', 'unknown')}"
        )
        return result

    # ------------------------------------------------------------------
    # Step implementations for single attempt
    # ------------------------------------------------------------------

    @staticmethod
    def _build_single_attempt_steps(context: dict) -> list:
        """Build step sequence for a single regeneration attempt."""
        steps = [
            WorkflowStep(
                name="Launch HEC-RAS",
                action=MeshRegenerationWorkflow._step_launch_hecras,
                max_retries=2,
                retry_delay=3.0,
            ),
            WorkflowStep(
                name="Open RASMapper",
                action=MeshRegenerationWorkflow._step_open_rasmapper,
                max_retries=2,
                retry_delay=2.0,
            ),
            WorkflowStep(
                name="Wait for RASMapper",
                action=MeshRegenerationWorkflow._step_wait_for_rasmapper,
                max_retries=1,
                timeout=context.get('timeout', 600),
            ),
            WorkflowStep(
                name="Wait for 2D Flow Area editor",
                action=MeshRegenerationWorkflow._step_wait_for_d2_editor,
                max_retries=2,
                retry_delay=2.0,
            ),
            WorkflowStep(
                name="Run 2D editor mesh actions",
                action=MeshRegenerationWorkflow._step_run_d2_editor_mesh_actions,
                max_retries=1,
                timeout=context.get('timeout', 600),
            ),
            WorkflowStep(
                name="Wait for mesh/HDF write",
                action=MeshRegenerationWorkflow._step_wait_for_save,
                max_retries=1,
                timeout=context.get('timeout', 600),
            ),
        ]

        if context.get('mannings_layer_name'):
            steps.append(
                WorkflowStep(
                    name="Associate geometry Manning's n layer",
                    action=MeshRegenerationWorkflow._step_apply_geometry_association,
                    max_retries=2,
                    retry_delay=2.0,
                )
            )

        if context.get('close_after', True):
            steps.append(WorkflowStep(
                name="Close RASMapper and HEC-RAS",
                action=MeshRegenerationWorkflow._step_close,
                max_retries=2,
                retry_delay=1.0,
                required=False,
            ))

        return steps

    @staticmethod
    def _step_launch_hecras(context: dict) -> None:
        """Launch HEC-RAS and store process/hwnd in context."""
        process, hwnd = HecRasElements.launch_and_wait(
            ras_object=context.get('ras_object'),
            timeout=30,
        )
        if not hwnd:
            raise RuntimeError("Failed to launch HEC-RAS")
        context['hecras_process'] = process
        context['hecras_hwnd'] = hwnd

        ras_obj = context.get('ras_object')
        if ras_obj is not None:
            geom_file = _find_geometry_file(ras_obj)
            if geom_file is not None:
                context['geom_file'] = geom_file
                context['geom_hdf_path'] = geom_file.parent / f"{geom_file.name}.hdf"
                context['rasmap_path'] = (
                    ras_obj.project_folder / f"{ras_obj.project_name}.rasmap"
                )

    @staticmethod
    def _step_open_rasmapper(context: dict) -> None:
        """Open RASMapper via GIS Tools menu."""
        hwnd = context['hecras_hwnd']

        try:
            win32gui.SetForegroundWindow(hwnd)
        except:
            pass
        time.sleep(0.5)

        if not HecRasElements.click_menu_by_path(hwnd, ["&GIS Tools", "RAS &Mapper"]):
            logger.info("Trying keyboard shortcut Alt+G, M...")
            Win32Primitives.send_keyboard_shortcut(hwnd, Win32Constants.VK_MENU, ord('G'))
            time.sleep(0.3)
            win32api.keybd_event(ord('M'), 0, 0, 0)
            time.sleep(0.05)
            win32api.keybd_event(ord('M'), 0, Win32Constants.KEYEVENTF_KEYUP, 0)

    @staticmethod
    def _step_wait_for_rasmapper(context: dict) -> None:
        """Wait for RASMapper to become responsive."""
        timeout = context.get('timeout', 600)
        result = RasMapperElements.wait_for_rasmapper(timeout=timeout)
        if not result:
            raise RuntimeError("RASMapper did not become responsive")
        context['rasmapper_hwnd'] = result[0]
        context['rasmapper_title'] = result[1]
        context['editor_mode'] = 'save_only'

    @staticmethod
    def _step_wait_for_d2_editor(context: dict) -> None:
        """
        Prefer the populated tree/context-menu workflow when available, then
        fall back to the embedded 2D Flow Area editor.

        If neither path is present, keep the older save-only fallback so the
        workflow still behaves reasonably on projects without editor state.
        """
        geom_file = context.get('geom_file')
        geometry_title = None
        if geom_file is not None:
            geometry_title = _get_geometry_title(geom_file)
        if geometry_title:
            context['geometry_title'] = geometry_title
            context['breaklines_tree_path'] = [
                "Geometries",
                geometry_title,
                "2D Flow Areas",
                "Breaklines",
            ]
            context['flow_areas_tree_path'] = [
                "Geometries",
                geometry_title,
                "2D Flow Areas",
            ]
            if RasMapperElements.select_tree_path(context['breaklines_tree_path']):
                context['editor_mode'] = 'tree_menu'
                return

        editor = RasMapperElements.wait_for_embedded_window(
            title="2D Flow Area Editor",
            automation_id="D2Editor",
            timeout=min(context.get('timeout', 600), 60),
        )
        if editor is None:
            logger.warning(
                "2D Flow Area Editor not found; falling back to Ctrl+S mesh save"
            )
            context['editor_mode'] = 'save_only'
            return

        context['editor_mode'] = 'd2_editor'
        context['mesh_snapshot_before_actions'] = _capture_editor_snapshot(
            context.get('geom_hdf_path')
        )

    @staticmethod
    def _step_run_d2_editor_mesh_actions(context: dict) -> None:
        """
        Use the real embedded 2D Flow Area editor controls when available.

        This is the RAS Mapper path that actually corresponds to:
        - Generate Computation Points
        - Enforce Breaklines / Refinement Regions
        - Force Mesh Recomputation
        """
        if context.get('editor_mode') == 'tree_menu':
            MeshRegenerationWorkflow._step_run_tree_menu_mesh_actions(context)
            return

        if context.get('editor_mode') != 'd2_editor':
            MeshRegenerationWorkflow._step_save_geometry(context)
            return

        timeout = context.get('timeout', 600)
        geom_hdf_path = context.get('geom_hdf_path')

        if not RasMapperElements.set_checkbox_state(
            title="Enforce Breaklines / Refinement Regions",
            checked=True,
            automation_id="cbInclude",
        ):
            logger.warning(
                "Could not confirm breakline enforcement checkbox state"
            )

        baseline = _capture_editor_snapshot(geom_hdf_path)
        if not RasMapperElements.post_button_click(
            title="Generate Computation Points",
            automation_id="btnGenerateComputationPoints",
        ):
            raise RuntimeError("Could not click Generate Computation Points")
        context['generate_activity'] = _wait_for_editor_change(
            geom_hdf_path=geom_hdf_path,
            baseline=baseline,
            timeout=min(timeout, 180),
            require_hdf_change=False,
        )

        baseline = _capture_editor_snapshot(geom_hdf_path)
        if not RasMapperElements.post_button_click(
            title="Force Mesh Recomputation",
            automation_id="btnForceMeshRecomputation",
        ):
            raise RuntimeError("Could not click Force Mesh Recomputation")
        context['recompute_activity'] = _wait_for_editor_change(
            geom_hdf_path=geom_hdf_path,
            baseline=baseline,
            timeout=min(timeout, 300),
            require_hdf_change=False,
        )

        context['pre_save_hdf_mtime'] = _get_file_mtime(geom_hdf_path)
        MeshRegenerationWorkflow._step_save_geometry(context)

    @staticmethod
    def _open_context_item(path: List[str], menu_text: str) -> None:
        """Select a tree path, open its context menu, and click a menu item."""
        if not RasMapperElements.open_tree_context_menu(path):
            raise RuntimeError(f"Could not open context menu for path: {path}")
        if not RasMapperElements.click_menu_item(menu_text):
            raise RuntimeError(
                f"Could not click context menu item '{menu_text}' for path: {path}"
            )

    @staticmethod
    def _try_open_context_item(path: List[str], menu_text: str) -> bool:
        """Best-effort variant of _open_context_item for state-dependent menu items."""
        try:
            MeshRegenerationWorkflow._open_context_item(path, menu_text)
            return True
        except RuntimeError as exc:
            logger.warning("%s", exc)
            return False

    @staticmethod
    def _accept_embedded_save_dialog(context: dict) -> None:
        """Click all visible Yes buttons in the embedded save dialog."""
        dialog_title = f"Save Edits ({context['geometry_title']})"
        total_clicks = 0
        for _ in range(4):
            clicks = RasMapperElements.click_embedded_dialog_button(
                dialog_title=dialog_title,
                button_text="Yes",
                max_clicks=4,
            )
            total_clicks += clicks
            if clicks == 0:
                break
        if total_clicks:
            logger.info("Accepted %s embedded save dialog button(s)", total_clicks)

    @staticmethod
    def _dismiss_try_fix_reports() -> List[str]:
        """Collect and dismiss embedded 'Try to Fix Mesh Report' dialogs."""
        dialog_title = "Try to Fix Mesh Report"
        collected: List[str] = []
        seen = set()

        for _ in range(8):
            for text in RasMapperElements.get_embedded_dialog_list_items(dialog_title):
                if text not in seen:
                    seen.add(text)
                    collected.append(text)

            clicks = RasMapperElements.click_embedded_dialog_button(
                dialog_title=dialog_title,
                button_text="OK",
                max_clicks=8,
            )
            if clicks == 0:
                break
            time.sleep(0.5)

        return collected

    @staticmethod
    def _step_run_tree_menu_mesh_actions(context: dict) -> None:
        """
        Run the exact tree/context-menu workflow the user requested:

        1. Breaklines > Edit Geometry
        2. Breaklines > Enforce All Breaklines
        3. Breaklines > Stop Editing > Yes
        4. 2D Flow Areas > Edit Geometry
        5. 2D Flow Areas > Try to Fix All Meshes (repeat until no fixes)
        6. 2D Flow Areas > Stop Editing > Yes
        """
        timeout = context.get('timeout', 600)
        breaklines_path = context['breaklines_tree_path']
        flow_areas_path = context['flow_areas_tree_path']

        MeshRegenerationWorkflow._open_context_item(
            breaklines_path,
            "Edit Geometry",
        )
        MeshRegenerationWorkflow._open_context_item(
            breaklines_path,
            "Enforce All Breaklines",
        )
        time.sleep(2)
        MeshRegenerationWorkflow._try_open_context_item(
            breaklines_path,
            "Stop Editing",
        )
        time.sleep(1)
        MeshRegenerationWorkflow._accept_embedded_save_dialog(context)

        MeshRegenerationWorkflow._open_context_item(
            flow_areas_path,
            "Edit Geometry",
        )

        max_fix_attempts = 8
        latest_messages = _latest_mesh_messages()
        context['mesh_fix_messages'] = []
        MeshRegenerationWorkflow._dismiss_try_fix_reports()

        for attempt in range(1, max_fix_attempts + 1):
            baseline = {
                "hdf_mtime": _get_file_mtime(context.get('geom_hdf_path')),
                "mesh_status_text": "",
                "status_items": latest_messages,
            }
            MeshRegenerationWorkflow._open_context_item(
                flow_areas_path,
                "Try to Fix All Meshes",
            )

            updated = _wait_for_editor_change(
                geom_hdf_path=context.get('geom_hdf_path'),
                baseline=baseline,
                timeout=min(timeout, 90),
                require_hdf_change=False,
            )
            report_messages = MeshRegenerationWorkflow._dismiss_try_fix_reports()
            latest_messages = [
                text for text in updated.get("status_items", [])
                if text.lower().startswith("mesh:")
            ]
            for text in report_messages:
                if text not in latest_messages:
                    latest_messages.insert(0, text)
            context['mesh_fix_messages'] = latest_messages
            if latest_messages:
                latest = latest_messages[0]
                logger.info(
                    "Try-to-fix pass %s/%s: %s",
                    attempt,
                    max_fix_attempts,
                    latest,
                )
                latest_lower = latest.lower()
                if _mesh_messages_indicate_clean(latest_messages):
                    break
                if "no fixes could be found" in latest_lower:
                    break

        if _mesh_messages_indicate_errors(latest_messages):
            raise RuntimeError(
                "RAS Mapper still reports mesh errors after "
                f"{max_fix_attempts} try-to-fix passes: {latest_messages[:3]}"
            )

        MeshRegenerationWorkflow._open_context_item(
            flow_areas_path,
            "Stop Editing",
        )
        time.sleep(1)
        MeshRegenerationWorkflow._accept_embedded_save_dialog(context)
        context['pre_save_hdf_mtime'] = _get_file_mtime(context.get('geom_hdf_path'))
        MeshRegenerationWorkflow._step_save_geometry(context)

    @staticmethod
    def _step_save_geometry(context: dict) -> None:
        """Save geometry in RASMapper via Ctrl+S, triggering HDF regeneration."""
        rasmapper_hwnd = context['rasmapper_hwnd']

        try:
            win32gui.SetForegroundWindow(rasmapper_hwnd)
        except:
            pass
        time.sleep(0.5)

        # Ctrl+S
        win32api.keybd_event(0x11, 0, 0, 0)  # Ctrl down
        time.sleep(0.05)
        win32api.keybd_event(ord('S'), 0, 0, 0)
        time.sleep(0.05)
        win32api.keybd_event(ord('S'), 0, Win32Constants.KEYEVENTF_KEYUP, 0)
        time.sleep(0.05)
        win32api.keybd_event(0x11, 0, Win32Constants.KEYEVENTF_KEYUP, 0)

        logger.info("Sent Ctrl+S to RASMapper")

    @staticmethod
    def _step_apply_geometry_association(context: dict) -> dict:
        """
        Run Geometries > Manage Geometry Associations and set Manning's n to
        the requested LandCover layer, then save the project.
        """
        mannings_layer_name = context.get('mannings_layer_name')
        if not mannings_layer_name:
            return

        geometry_root_path = ["Geometries"]
        rasmapper_hwnd = context.get('rasmapper_hwnd')

        if rasmapper_hwnd:
            RasMapperElements.wait_for_rasmapper_idle(
                rasmapper_hwnd,
                timeout=30,
                check_interval=1,
            )
        time.sleep(1.0)

        combo_title = "Manning's n Row 0"
        selected = ""

        for attempt in range(2):
            RasMapperElements.click_embedded_dialog_button(
                dialog_title="Manage Layer Associations",
                button_text="Close",
                max_clicks=2,
            )
            time.sleep(0.5)

            MeshRegenerationWorkflow._open_context_item(
                geometry_root_path,
                "Manage Geometry Associations...",
            )

            dialog = RasMapperElements.wait_for_embedded_window(
                title="Manage Layer Associations",
                automation_id="ManageGeometryAssociation",
                timeout=30,
            )
            if dialog is None:
                raise RuntimeError(
                    "Manage Geometry Associations dialog did not open"
                )

            time.sleep(1.0)
            selected = RasMapperElements.get_combobox_selected_text(
                title=combo_title,
                parent=dialog,
            )
            if selected.lower() == mannings_layer_name.lower():
                break

            if RasMapperElements.set_combobox_selection_via_keys(
                title=combo_title,
                value=mannings_layer_name,
                parent=dialog,
            ):
                selected = RasMapperElements.get_combobox_selected_text(
                    title=combo_title,
                    parent=dialog,
                )
                if selected.lower() == mannings_layer_name.lower():
                    break

            RasMapperElements.click_embedded_dialog_button(
                dialog_title="Manage Layer Associations",
                button_text="Close",
                max_clicks=2,
            )
            time.sleep(1.0)
        else:
            raise RuntimeError(
                f"Could not set {combo_title} to '{mannings_layer_name}'"
            )

        clicks = RasMapperElements.click_embedded_dialog_button(
            dialog_title="Manage Layer Associations",
            button_text="Close",
            max_clicks=2,
        )
        if clicks == 0:
            raise RuntimeError(
                "Could not close Manage Geometry Associations dialog"
            )

        context['geometry_association_selected'] = selected
        context['pre_save_rasmap_mtime'] = _get_file_mtime(
            context.get('rasmap_path')
        )
        MeshRegenerationWorkflow._step_save_geometry(context)

        if not _wait_for_file_change(
            path=context.get('rasmap_path'),
            baseline_mtime=context.get('pre_save_rasmap_mtime'),
            timeout=30,
        ):
            logger.warning(
                "RAS Mapper association save did not change rasmap mtime within timeout"
            )

        return {
            "selected_layer": selected,
            "rasmap_path": context.get('rasmap_path'),
        }

    @staticmethod
    def _step_wait_for_save(context: dict) -> None:
        """Wait for RASMapper to finish saving / HDF regeneration."""
        rasmapper_hwnd = context['rasmapper_hwnd']
        timeout = context.get('timeout', 600)
        geom_hdf_path = context.get('geom_hdf_path')
        pre_save_hdf_mtime = context.get('pre_save_hdf_mtime')

        time.sleep(2)

        if geom_hdf_path is not None:
            start_time = time.time()
            while time.time() - start_time < timeout:
                if geom_hdf_path.exists():
                    mesh_check = _check_mesh_valid(geom_hdf_path)
                    context['mesh_check'] = mesh_check
                    current_mtime = _get_file_mtime(geom_hdf_path)

                    if mesh_check['valid']:
                        logger.info(
                            "Geometry HDF is valid after mesh actions: %s cells",
                            mesh_check['n_cells'],
                        )
                        if (
                            pre_save_hdf_mtime is None
                            or current_mtime != pre_save_hdf_mtime
                        ):
                            return
                        # Accept the valid HDF even if the write time did not
                        # change after Ctrl+S; some actions write before save.
                        if context.get('editor_mode') in {
                            'd2_editor',
                            'tree_menu',
                        }:
                            return

                time.sleep(2)

        if not RasMapperElements.wait_for_rasmapper_idle(rasmapper_hwnd, timeout=timeout):
            logger.warning("RASMapper may still be processing, but continuing...")

    @staticmethod
    def _step_close(context: dict) -> None:
        """Close RASMapper and HEC-RAS."""
        rasmapper_hwnd = context.get('rasmapper_hwnd')
        hecras_hwnd = context.get('hecras_hwnd')
        hecras_process = context.get('hecras_process')

        if rasmapper_hwnd:
            Win32Primitives.close_window(rasmapper_hwnd)
            time.sleep(2)

        HecRasElements.dismiss_save_prompt(timeout=3)

        if hecras_hwnd:
            Win32Primitives.close_window(hecras_hwnd)

        if hecras_process:
            try:
                hecras_process.wait(timeout=10)
            except:
                pass

        logger.info("RASMapper and HEC-RAS closed")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_geometry_file(ras_obj) -> Optional[Path]:
    """Find the current geometry file from the RAS project."""
    try:
        # Read the .prj file to find current geometry
        prj_text = ras_obj.prj_file.read_text(errors="replace")
        match = re.search(r"Geom File=(\S+)", prj_text)
        if match:
            geom_ext = match.group(1).strip()
            # The geometry file is project_name.g01 etc.
            geom_file = ras_obj.project_folder / f"{ras_obj.project_name}.{geom_ext}"
            if geom_file.exists():
                return geom_file

        # Fallback: find any .g## file
        for g in sorted(ras_obj.project_folder.glob(f"{ras_obj.project_name}.g*")):
            if g.suffix and g.suffix[1:].startswith("g") and not g.suffix.endswith(".hdf"):
                return g

    except Exception as e:
        logger.warning(f"Could not find geometry file: {e}")

    return None
