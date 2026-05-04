from __future__ import annotations

import argparse
import ast
import csv
from dataclasses import asdict, dataclass
import json
import logging
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any, Iterable, Sequence


PWD = Path(__file__).resolve().parent
if str(PWD) not in sys.path:
    sys.path.insert(0, str(PWD))

from configProject import Config


# =============================================================================
# User configuration
# =============================================================================

CONFIG_SOURCE = "folder"
MASTER_PROJECT_PATH = r"C:\Users\Ripple\Downloads\Turkey Flood\Group-4-Model"
SHEET_NAME = None

# None means "run every project listed in the active structure source".
# PROJECTS_TO_RUN: list[str] | None = None
PROJECTS_TO_RUN = ["ARDICLI", "CIGRI", "CUKUROREN", "CUKUROREN-T"]

RASMAPPER_V01_ROOT = PWD / "rasmapper_v01"
PROJECT_NAME_TEMPLATE = "{project}_2D"

# IDE/GUI run-button defaults. With no command-line arguments, pressing Run on
# this file uses these settings and processes the selected projects one by one.
RUN_ALL_PROJECTS = False
RUN_DRY_RUN = False
RUN_JSON_ONLY = False
RUN_STOP_ON_ERROR = False
JSON_OUTPUT_DIR: str | None = None

# rasmapper_v01.py imports the GIS stack. On this machine Python 3.12 has those
# packages, while the default IDE interpreter may not.
RASMAPPER_PYTHON_EXE: str | None = (
    r"C:\Users\Ripple\AppData\Local\Programs\Python\Python312\python.exe"
)

# Connected rivers are grouped into one project using network.csv/networks.csv.
# Disconnected components are written as separate 2D projects.
WORKFLOW_STEPS = (
    "prepare-skip-terrain",
    "prepare",
    "install-geometry",
    "create-unsteady-plan",
    "compute-plan",
    "check-mannings",
)

SKIP_TERRAIN = False
MESH_CELL_SIZE = 10.0
BREAKLINE_NEAR_SPACING = 0.5
BREAKLINE_NEAR_REPEATS = 5
BREAKLINE_FAR_SPACING = 3.0
LANDCOVER_CELL_SIZE = 2.0
PREFERRED_DSS_F_PART = "Q100"
DOWNSTREAM_BC_METHOD = "Normal Depth"
PLAN_COMPUTATION_INTERVAL = "6SEC"
SIMULATION_DURATION_HOURS = 24.0
INSTALL_TIMEOUT_SECONDS = 120
COMPUTE_TIMEOUT_SECONDS = 1800
SKIP_GEOMETRY_REGENERATION = False
COMPUTE_OVERWRITE = True
MERGE_PREPARED_TERRAIN_WITH_ORIGINAL = True
MERGED_TERRAIN_DIR: str | None = None
MERGED_TERRAIN_RESAMPLING = "bilinear"

PROJECTION_FILE: str | None = None
DSS_FILE: str | None = None
LANDCOVER_SHP: str | None = None
REFERENCE_GEOM_PATH: str | None = None
REFERENCE_GEOM_HDF_PATH: str | None = None
TEMPLATE_UNSTEADY_PATH: str | None = None
TEMPLATE_UNSTEADY_HDF_PATH: str | None = None
TEMPLATE_PLAN_PATH: str | None = None
EXISTING_LANDCOVER_TIF_PATH: str | None = None
EXISTING_LANDCOVER_HDF_PATH: str | None = None


logger = logging.getLogger("implementation2djson")


@dataclass(frozen=True)
class ChannelInput:
    name: str
    cross_section_csv: str
    bank_shp_path: str
    dtm_path: str


@dataclass(frozen=True)
class RunSpec:
    source_project_name: str
    model_project_name: str
    sub_project_names: list[str]
    dtm_result: dict[str, Any]


@dataclass
class JsonRunResult:
    source_project_name: str
    model_project_name: str
    sub_project_names: list[str]
    json_path: str
    output_folder: str
    success: bool
    message: str
    commands: list[list[str]]
    command_results: list[dict[str, Any]]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate rasmapper_v01 JSON inputs from implementationDTM.py outputs "
            "and call rasmapper_v01.py for each project/component."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--dry-run", action="store_true", default=RUN_DRY_RUN, help="Generate JSON and validate paths without running rasmapper_v01.py.")
    parser.add_argument("--json-only", action="store_true", default=RUN_JSON_ONLY, help="Generate JSON files without validating or running rasmapper_v01.py.")
    parser.add_argument("--project", action="append", help="Project or sub-project name to run. Repeat or comma-separate for multiple values.")
    parser.add_argument("--all-projects", action="store_true", default=RUN_ALL_PROJECTS, help="Run every project discovered from the active structure source.")
    parser.add_argument("--step", action="append", help="rasmapper_v01 command to run. Repeat or comma-separate for multiple commands.")
    parser.add_argument("--full", action="store_true", help="Run the default full v01 sequence.")
    parser.add_argument("--prepare-only", action="store_true", help="Only run rasmapper_v01 prepare.")
    parser.add_argument("--compute", action="store_true", help="Append compute-plan to the selected workflow.")
    parser.add_argument("--no-compute", action="store_true", help="Remove compute-plan from the selected workflow.")
    parser.add_argument("--skip-terrain", action="store_true", default=SKIP_TERRAIN, help="Pass --skip-terrain to rasmapper_v01 prepare.")
    error_group = parser.add_mutually_exclusive_group()
    error_group.add_argument("--continue-on-error", dest="stop_on_error", action="store_false", default=RUN_STOP_ON_ERROR, help="Continue running remaining components after an error.")
    error_group.add_argument("--stop-on-error", dest="stop_on_error", action="store_true", help="Stop immediately if one component fails.")
    parser.add_argument(
        "--skip-geometry-regeneration",
        action="store_true",
        default=SKIP_GEOMETRY_REGENERATION,
        help="Pass --skip-regeneration to rasmapper_v01 install-geometry.",
    )
    parser.add_argument(
        "--regenerate-geometry",
        dest="skip_geometry_regeneration",
        action="store_false",
        help="Run the RAS Mapper GUI mesh-regeneration part of install-geometry.",
    )
    parser.add_argument("--install-timeout", type=int, default=INSTALL_TIMEOUT_SECONDS, help="Timeout in seconds for GUI geometry commands.")
    parser.add_argument("--compute-timeout", type=int, default=COMPUTE_TIMEOUT_SECONDS, help="Timeout in seconds for compute-plan.")
    parser.add_argument("--no-overwrite", dest="compute_overwrite", action="store_false", default=COMPUTE_OVERWRITE, help="Do not pass --overwrite to compute-plan.")
    parser.add_argument("--geom", default="g01", help="Geometry number/path for apply-mannings and check-mannings.")
    parser.add_argument("--json-dir", type=Path, default=Path(JSON_OUTPUT_DIR) if JSON_OUTPUT_DIR else None, help="Directory where generated rasmapper_v01 JSON files are written.")
    parser.add_argument("--rasmapper-root", type=Path, default=RASMAPPER_V01_ROOT, help="Folder containing rasmapper_v01.py.")
    parser.add_argument("--python-exe", default=default_rasmapper_python_exe(), help="Python executable used to run rasmapper_v01.py.")
    parser.add_argument(
        "--source",
        choices=["sheet", "folder", "auto"],
        help="Load project structure from the Google Sheet, a master folder path, or auto-fallback.",
    )
    parser.add_argument(
        "--master-project-path",
        help="Master project folder to use when --source folder is selected or auto falls back to the filesystem.",
    )
    parser.add_argument("--sheet-name", help="Optional Google Sheet tab name. Defaults to 'Structure'.")
    return parser.parse_args()


def default_rasmapper_python_exe() -> str:
    if RASMAPPER_PYTHON_EXE:
        configured = Path(RASMAPPER_PYTHON_EXE)
        if configured.exists():
            return str(configured)
    return sys.executable


def build_config(args: argparse.Namespace) -> Config:
    source = args.source or CONFIG_SOURCE
    master_project_path = args.master_project_path or MASTER_PROJECT_PATH
    sheet_name = args.sheet_name or SHEET_NAME
    config = Config(
        structure_source=source,
        master_project_path=master_project_path,
        project_folder=master_project_path,
        sheet_name=sheet_name,
    )
    config.setup_essential_directories()
    return config


def selected_projects(args: argparse.Namespace) -> list[str] | None:
    if args.all_projects:
        return None
    if not args.project:
        return PROJECTS_TO_RUN
    projects: list[str] = []
    for value in args.project:
        projects.extend(item.strip() for item in str(value).split(",") if item.strip())
    return projects or PROJECTS_TO_RUN


def selected_steps(args: argparse.Namespace) -> tuple[str, ...]:
    if args.prepare_only:
        steps = ["prepare"]
    elif args.full:
        steps = list(WORKFLOW_STEPS)
    elif args.step:
        steps = []
        for value in args.step:
            steps.extend(item.strip() for item in str(value).split(",") if item.strip())
    else:
        steps = list(WORKFLOW_STEPS)

    steps = normalize_v01_steps(steps)
    if args.compute and "compute-plan" not in steps:
        steps.append("compute-plan")
    if args.no_compute:
        steps = [step for step in steps if step != "compute-plan"]
    return tuple(steps)


def normalize_v01_steps(steps: Sequence[str]) -> list[str]:
    aliases = {
        "compute": "compute-plan",
        "run": "compute-plan",
        "plan": "create-unsteady-plan",
        "create-plan": "create-unsteady-plan",
        "install": "install-geometry",
        "geometry": "install-geometry",
        "dry-prepare": "prepare-skip-terrain",
        "prepare-dry-run": "prepare-skip-terrain",
        "prepare-skip-terrain": "prepare-skip-terrain",
        "skip-terrain-prepare": "prepare-skip-terrain",
        "regen": "regenerate-geometry",
        "regenerate": "regenerate-geometry",
        "sync": "sync-geometry",
        "mannings": "apply-mannings",
        "manning": "apply-mannings",
        "check": "check-mannings",
    }
    normalized: list[str] = []
    for step in steps:
        key = str(step).strip().lower().replace("_", "-")
        if key == "full":
            for full_step in WORKFLOW_STEPS:
                if full_step not in normalized:
                    normalized.append(full_step)
            continue
        step_name = aliases.get(key, key)
        if step_name and step_name not in normalized:
            normalized.append(step_name)
    return normalized or ["prepare"]


def workflow_has_gui_step(steps: Sequence[str]) -> bool:
    return any(
        step in {"install-geometry", "regenerate-geometry", "open", "prepare-open"}
        for step in steps
    )


def build_run_specs(config: Config, projects: Iterable[str] | None) -> list[RunSpec]:
    project_subprojects = filtered_project_subprojects(
        config.discover_project_subprojects(),
        projects,
    )
    if not project_subprojects:
        raise ValueError(
            "No projects were selected from the active structure source "
            f"({config.structure_source})."
        )

    specs: list[RunSpec] = []
    for project_name, sub_project_names in project_subprojects.items():
        specs.extend(project_run_specs(config, project_name, sub_project_names))
    return specs


def filtered_project_subprojects(
    project_subprojects: dict[str, list[str]],
    selected: Iterable[str] | None,
) -> dict[str, list[str]]:
    if selected is None:
        return project_subprojects

    selected_names = {_normalize_name(name) for name in selected}
    filtered: dict[str, list[str]] = {}
    for project_name, sub_project_names in project_subprojects.items():
        if _normalize_name(project_name) in selected_names:
            filtered[project_name] = sub_project_names
            continue
        selected_subprojects = [
            name for name in sub_project_names if _normalize_name(name) in selected_names
        ]
        if selected_subprojects:
            filtered[project_name] = selected_subprojects
    return filtered


def project_run_specs(
    config: Config,
    project_name: str,
    sub_project_names: Sequence[str],
) -> list[RunSpec]:
    channels = project_channel_inputs(config, project_name, sub_project_names)
    connected_groups = group_connected_channels(channels, read_network_connections(config))
    if not connected_groups:
        connected_groups = [channels]

    channel_groups: dict[tuple[str, str], list[ChannelInput]] = {}
    for connected_group in connected_groups:
        component_key = component_output_key(project_name, connected_group, len(connected_groups))
        for channel in connected_group:
            channel_groups.setdefault((channel.dtm_path, component_key), []).append(channel)

    has_multiple_groups = len(channel_groups) > 1
    specs: list[RunSpec] = []
    for (_, component_key), grouped_channels in channel_groups.items():
        component_name = component_name_from_key(
            project_name,
            grouped_channels,
            component_key,
            has_multiple_groups,
        )
        if component_key.startswith("single:") and len(grouped_channels) == 1:
            sub_project_name = grouped_channels[0].name
            isolated_dtm_dir = Path(config.get_gis_sub_project_path(project_name, sub_project_name)) / "DTM"
            if isolated_dtm_dir.exists():
                component_name = sub_project_name

        grouped_subprojects = [channel.name for channel in grouped_channels]
        specs.append(
            RunSpec(
                source_project_name=project_name,
                model_project_name=component_name,
                sub_project_names=grouped_subprojects,
                dtm_result=resolve_dtm_result(
                    config,
                    project_name,
                    grouped_subprojects,
                    component_name,
                ),
            )
        )
    return specs


def project_channel_inputs(
    config: Config,
    project_name: str,
    sub_project_names: Sequence[str],
) -> list[ChannelInput]:
    channels: list[ChannelInput] = []
    for sub_project_name in sub_project_names:
        paths = config.get_sub_project_paths(
            project_name,
            sub_project_name,
            resolve_dtm=True,
        )
        channels.append(
            ChannelInput(
                name=paths.sub_project_name,
                cross_section_csv=paths.cross_section_file_path,
                bank_shp_path=paths.bank_line_file_path,
                dtm_path=paths.dtm_path,
            )
        )
    return channels


def read_network_connections(config: Config) -> list[dict[str, str]]:
    network_path = network_csv_path(config)
    if network_path is None:
        return []

    with network_path.open("r", encoding="utf-8-sig", newline="") as handle:
        sample = handle.read(2048)
        handle.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample)
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(handle, dialect=dialect)
        if not reader.fieldnames or len(reader.fieldnames) < 2:
            return []
        lookup = {field.strip().casefold(): field for field in reader.fieldnames}
        from_field = lookup.get("from") or reader.fieldnames[0]
        to_field = lookup.get("to") or reader.fieldnames[1]

        connections: list[dict[str, str]] = []
        for row in reader:
            from_name = str(row.get(from_field, "")).strip()
            to_name = str(row.get(to_field, "")).strip()
            if (
                from_name
                and to_name
                and from_name.casefold() != "nan"
                and to_name.casefold() != "nan"
            ):
                connections.append({"from": from_name, "to": to_name})
        return connections


def network_csv_path(config: Config) -> Path | None:
    for filename in ("networks.csv", "network.csv"):
        preferred = Path(config.ESSENTIALS_PATH) / filename
        if preferred.exists():
            return preferred
    for filename in ("networks.csv", "network.csv"):
        matches = sorted(Path(config.PROJECT_FOLDER).glob(f"0*Essentials*/{filename}"))
        if matches:
            return matches[0]
    return None


def group_connected_channels(
    channels: Sequence[ChannelInput],
    connections: Sequence[dict[str, str]],
) -> list[list[ChannelInput]]:
    if not channels:
        return []

    parent = list(range(len(channels)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for connection in connections:
        from_index = find_channel_index(channels, connection.get("from"))
        to_index = find_channel_index(channels, connection.get("to"))
        if from_index is not None and to_index is not None and from_index != to_index:
            union(from_index, to_index)

    grouped: dict[int, list[ChannelInput]] = {}
    for index, channel in enumerate(channels):
        grouped.setdefault(find(index), []).append(channel)

    return sorted(
        grouped.values(),
        key=lambda group: (
            0 if len(group) > 1 else 1,
            _normalize_name(group[0].name),
        ),
    )


def find_channel_index(channels: Sequence[ChannelInput], network_name: Any) -> int | None:
    if not network_name:
        return None
    for index, channel in enumerate(channels):
        aliases = [
            channel.name,
            Path(channel.cross_section_csv).stem,
            Path(channel.bank_shp_path).parent.name,
        ]
        if any(names_match(network_name, alias) for alias in aliases):
            return index
    return None


def component_output_key(
    project_name: str,
    connected_group: Sequence[ChannelInput],
    connected_group_count: int,
) -> str:
    if len(connected_group) == 1 and connected_group_count > 1:
        return f"subproject:{connected_group[0].name}"
    if len(connected_group) == 1:
        return f"single:{connected_group[0].name}"
    return f"project:{project_name}"


def component_name_from_key(
    project_name: str,
    grouped_channels: Sequence[ChannelInput],
    component_key: str,
    has_multiple_groups: bool,
) -> str:
    if component_key.startswith("subproject:"):
        return component_key.split(":", 1)[1]
    if component_key.startswith("single:"):
        sub_project_name = component_key.split(":", 1)[1]
        return sub_project_name if has_multiple_groups else project_name
    if grouped_channels:
        return project_name
    return project_name


def resolve_dtm_result(
    config: Config,
    project_name: str,
    sub_project_names: Sequence[str],
    component_name: str,
) -> dict[str, Any]:
    summary_results = load_dtm_summary(config)
    matches = [
        result for result in summary_results if result_matches_component(result, component_name)
    ]
    channel_matches = filter_results_by_channels(matches, sub_project_names)
    if channel_matches:
        matches = channel_matches
    elif has_channel_scoped_results(matches):
        matches = []
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(f"Multiple implementationDTM outputs matched {component_name}.")

    if _normalize_name(component_name) == _normalize_name(project_name):
        matches = [
            result for result in summary_results if result_matches_project(result, project_name)
        ]
        channel_matches = filter_results_by_channels(matches, sub_project_names)
        if channel_matches:
            matches = channel_matches
        elif has_channel_scoped_results(matches):
            matches = []
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError(f"Multiple implementationDTM outputs matched {project_name}.")

    return fallback_dtm_result(config, project_name, sub_project_names, component_name)


def load_dtm_summary(config: Config) -> list[dict[str, Any]]:
    summary_path = Path(config.TEMP_PATH) / "implementationDTM_summary.json"
    if not summary_path.exists():
        return []

    raw = json.loads(summary_path.read_text(encoding="utf-8"))
    items = raw if isinstance(raw, list) else [raw]
    flattened: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict) and item.get("dtm_group_results"):
            for group_result in item["dtm_group_results"]:
                if isinstance(group_result, dict):
                    merged = dict(group_result)
                    merged.setdefault("project", item.get("project"))
                    flattened.append(merged)
        elif isinstance(item, dict):
            flattened.append(item)
    return flattened


def fallback_dtm_result(
    config: Config,
    project_name: str,
    sub_project_names: Sequence[str],
    component_name: str,
) -> dict[str, Any]:
    dtm_dirs = component_dtm_dirs(config, project_name, component_name, sub_project_names)
    selected_dtm_dir: Path | None = None
    output_tifs: list[Path] = []
    perimeter_shps: list[Path] = []
    merged_bank_shps: list[Path] = []

    for dtm_dir in dtm_dirs:
        output_tifs = sorted_dtm_files(
            dtm_dir.glob("*channel_terrain*.tif"),
            preferred_stem=component_name,
        )
        perimeter_shps = sorted_dtm_files(
            dtm_dir.glob("*Study_Perimeter*.shp"),
            preferred_stem=component_name,
        )
        merged_bank_shps = sorted_dtm_files(
            dtm_dir.glob("*Merged_Banks*.shp"),
            preferred_stem=component_name,
        )
        if output_tifs and perimeter_shps:
            selected_dtm_dir = dtm_dir
            break

    if not output_tifs or not perimeter_shps or selected_dtm_dir is None:
        raise FileNotFoundError(
            f"Prepared DTM outputs were not found for 2D component {component_name}. "
            "Run implementationDTM.py first. Searched: "
            + ", ".join(str(path) for path in dtm_dirs)
        )

    channels = [
        asdict(channel)
        for channel in project_channel_inputs(config, project_name, sub_project_names)
    ]
    return {
        "project": project_name,
        "component": component_name,
        "output_tif": str(output_tifs[0]),
        "perimeter_shp": str(perimeter_shps[0]),
        "merged_banks_shp": str(merged_bank_shps[0]) if merged_bank_shps else None,
        "connected_bank_products": fallback_connected_bank_products(selected_dtm_dir),
        "channels": channels,
        "junctions": [],
        "gis_output_dir": str(selected_dtm_dir),
    }


def component_dtm_dirs(
    config: Config,
    project_name: str,
    component_name: str,
    sub_project_names: Sequence[str],
) -> list[Path]:
    gis_root = Path(config.GIS_PATH)
    project_dir = Path(config.get_gis_project_path(project_name))
    dirs: list[Path] = []

    def add(path: Path) -> None:
        if path.exists() and path.is_dir() and path not in dirs:
            dirs.append(path)

    if component_name and _normalize_name(component_name) != _normalize_name(project_name):
        add(project_dir / component_name / "DTM")
        add(gis_root / component_name / "DTM")

    for sub_project_name in sub_project_names:
        add(Path(config.get_gis_sub_project_path(project_name, sub_project_name)) / "DTM")

    add(project_dir / "DTM")
    if component_name:
        add(gis_root / component_name / "DTM")
    return dirs


def sorted_dtm_files(paths: Iterable[Path], *, preferred_stem: str) -> list[Path]:
    preferred = _normalize_name(preferred_stem)
    return sorted(
        [Path(path) for path in paths],
        key=lambda path: (
            0 if _normalize_name(path.stem).startswith(preferred) else 1,
            0 if "junction_channel_terrain" in path.stem else 1,
            -path.stat().st_mtime,
            path.name,
        ),
    )


def fallback_connected_bank_products(dtm_dir: Path) -> list[dict[str, str]]:
    products: list[dict[str, str]] = []
    for path in sorted(dtm_dir.parent.glob("*.shp")):
        stem = path.stem.lower()
        if "junction_clipped" in stem:
            products.append({"junction_clipped_banks_shp": str(path)})
        elif "combined" in stem and "sev_ustu" in stem:
            products.append({"merged_banks_shp": str(path)})
    return products


def resolve_original_terrain_path(config: Config, spec: RunSpec) -> Path:
    candidates: list[Path] = []

    def add(value: Any) -> None:
        if not value:
            return
        path = Path(str(value))
        if path.exists() and path not in candidates:
            candidates.append(path)

    add(spec.dtm_result.get("dtm_path"))
    for channel in spec.dtm_result.get("channels", []) or []:
        add(channel.get("dtm_path"))

    for sub_project_name in spec.sub_project_names:
        try:
            add(
                config.resolve_dtm_path(
                    spec.source_project_name,
                    sub_project_name,
                    required=True,
                )
            )
        except FileNotFoundError:
            continue

    if not candidates:
        raise FileNotFoundError(
            f"Original terrain from dtm.csv could not be resolved for "
            f"{spec.source_project_name}: {', '.join(spec.sub_project_names)}."
        )
    return candidates[0]


def merge_prepared_terrain_with_original(
    config: Config,
    model_project_name: str,
    *,
    original_dtm: Path,
    prepared_dtm: Path,
) -> Path:
    if Path(original_dtm).resolve() == Path(prepared_dtm).resolve():
        return prepared_dtm

    output_dir = (
        Path(MERGED_TERRAIN_DIR)
        if MERGED_TERRAIN_DIR
        else project_temp_2d_dir(config, model_project_name) / "merged_terrain"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{safe_name(model_project_name)}_original_plus_channel.tif"

    newest_source_mtime = max(original_dtm.stat().st_mtime, prepared_dtm.stat().st_mtime)
    if output_path.exists() and output_path.stat().st_mtime >= newest_source_mtime:
        return output_path

    try:
        import numpy as np
        import rasterio
        from rasterio.enums import Resampling
        from rasterio.warp import reproject, transform_bounds
        from rasterio.windows import Window, from_bounds
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "Merging prepared terrain with the original DTM requires rasterio "
            "and numpy. Run implementation2djson.py with the Python environment "
            "used for implementationDTM.py."
        ) from exc

    shutil.copy2(original_dtm, output_path)
    resampling = getattr(Resampling, MERGED_TERRAIN_RESAMPLING, Resampling.bilinear)

    with rasterio.open(prepared_dtm) as src, rasterio.open(output_path, "r+") as dst:
        src_bounds = src.bounds
        if src.crs and dst.crs and src.crs != dst.crs:
            src_bounds = transform_bounds(src.crs, dst.crs, *src.bounds, densify_pts=21)

        raw_window = from_bounds(*src_bounds, transform=dst.transform)
        col_start = max(0, int(np.floor(raw_window.col_off)))
        row_start = max(0, int(np.floor(raw_window.row_off)))
        col_stop = min(dst.width, int(np.ceil(raw_window.col_off + raw_window.width)))
        row_stop = min(dst.height, int(np.ceil(raw_window.row_off + raw_window.height)))
        width = col_stop - col_start
        height = row_stop - row_start
        if width <= 0 or height <= 0:
            raise ValueError(
                f"Prepared terrain does not overlap original terrain: "
                f"{prepared_dtm} vs {original_dtm}"
            )

        dst_window = Window(col_start, row_start, width, height)
        dst_data = dst.read(1, window=dst_window)
        overlay = np.full((height, width), np.nan, dtype="float32")
        src_crs = src.crs or dst.crs
        dst_crs = dst.crs or src.crs
        reproject(
            source=rasterio.band(src, 1),
            destination=overlay,
            src_transform=src.transform,
            src_crs=src_crs,
            src_nodata=src.nodata,
            dst_transform=dst.window_transform(dst_window),
            dst_crs=dst_crs,
            dst_nodata=np.nan,
            resampling=resampling,
        )

        valid = np.isfinite(overlay)
        if src.nodata is not None:
            valid &= ~np.isclose(overlay, float(src.nodata))
        if not np.any(valid):
            raise ValueError(f"Prepared terrain has no valid cells to merge: {prepared_dtm}")

        dst_data[valid] = overlay[valid].astype(dst_data.dtype, copy=False)
        dst.write(dst_data, 1, window=dst_window)

    copy_projection_sidecar(original_dtm, output_path)
    print(
        f"  Merged terrain: {prepared_dtm} over {original_dtm} -> {output_path}",
        flush=True,
    )
    return output_path


def build_v01_json_config(
    config: Config,
    spec: RunSpec,
    *,
    rasmapper_script: Path,
) -> tuple[dict[str, Any], Path]:
    model_project_name = spec.model_project_name
    dtm_result = spec.dtm_result
    prepared_dtm = required_result_path(dtm_result, "output_tif")
    rasmapper_dtm = prepared_dtm
    if MERGE_PREPARED_TERRAIN_WITH_ORIGINAL:
        original_dtm = resolve_original_terrain_path(config, spec)
        rasmapper_dtm = merge_prepared_terrain_with_original(
            config,
            model_project_name,
            original_dtm=original_dtm,
            prepared_dtm=prepared_dtm,
        )
    perimeter_shp = required_result_path(dtm_result, "perimeter_shp")
    breakline_shp = resolve_breakline_source(config, model_project_name, dtm_result)
    landcover_shp = resolve_landcover_source(config, spec.source_project_name, perimeter_shp)
    projection_file = resolve_projection_file(config)
    dss_file = resolve_dss_file(config)
    reference_geom = resolve_reference_geometry_file(rasmapper_script, ".g01")
    reference_geom_hdf = resolve_reference_geometry_file(rasmapper_script, ".g01.hdf")
    template_unsteady = resolve_template_file(rasmapper_script, "UnsteadyTemplate", ".u01")
    template_unsteady_hdf = resolve_template_file(rasmapper_script, "UnsteadyTemplate", ".u01.hdf")
    template_plan = resolve_template_file(rasmapper_script, "PlanTemplate", ".p01")
    junction_csv = write_junction_coordinate_csv(config, model_project_name, dtm_result)
    cross_section_paths = [
        str(Path(channel["cross_section_csv"]))
        for channel in dtm_result.get("channels", []) or []
        if channel.get("cross_section_csv")
    ]
    if not cross_section_paths:
        cross_section_paths = [
            config.get_sub_project_paths(
                spec.source_project_name,
                sub_project_name,
            ).cross_section_file_path
            for sub_project_name in spec.sub_project_names
        ]

    ras_project_name = PROJECT_NAME_TEMPLATE.format(project=model_project_name)
    storage_area_name = storage_area_name_for_component(model_project_name)
    output_folder = Path(config.HEC_PATH) / ras_project_name
    raw_config = {
        "files_root": str(Path(config.PROJECT_FOLDER)),
        "working_root": str(Path(config.HEC_PATH)),
        "results_root": str(Path(config.get_output_project_path(spec.source_project_name)) / "2D"),
        "project_name": ras_project_name,
        "ras_exe": str(Path(config.RAS_EXE_PATH)),
        "gdal_grid_exe": str(Path(config.RAS_EXE_PATH).parent / "GDAL" / "bin64" / "gdal_grid.exe"),
        "projection_name": str(projection_file),
        "dtm_name": str(rasmapper_dtm),
        "perimeter_name": str(perimeter_shp),
        "breakline_name": str(breakline_shp),
        "dss_name": str(dss_file),
        "cross_section_name": cross_section_paths,
        "junction_bc_csv_name": str(junction_csv) if junction_csv else None,
        "landcover_name": str(landcover_shp),
        "existing_landcover_tif_name": existing_path_string(EXISTING_LANDCOVER_TIF_PATH),
        "existing_landcover_hdf_name": existing_path_string(EXISTING_LANDCOVER_HDF_PATH),
        "reference_geom_name": str(reference_geom) if reference_geom else "reference_geometry.g01",
        "reference_geom_hdf_name": str(reference_geom_hdf) if reference_geom_hdf else "reference_geometry.g01.hdf",
        "terrain_layer_name": "Prepared Interpolated DTM",
        "flow_area_name": f"{model_project_name}_2D",
        "geometry_title": f"{model_project_name}_2D_CLEAN",
        "storage_area_name": storage_area_name,
        "hdf_2d_area_name": storage_area_name,
        "mesh_cell_size": MESH_CELL_SIZE,
        "breakline_near_spacing": BREAKLINE_NEAR_SPACING,
        "breakline_near_repeats": BREAKLINE_NEAR_REPEATS,
        "breakline_far_spacing": BREAKLINE_FAR_SPACING,
        "landcover_cell_size": LANDCOVER_CELL_SIZE,
        "landcover_nodata_manning": 0.025,
        "region_default_manning": 0.025,
        "boundary_offset_distance": 1.0,
        "downstream_bc_length_multiplier": 10.0,
        "preferred_dss_a_part": preferred_dss_a_part(model_project_name, dtm_result),
        "preferred_dss_f_part": PREFERRED_DSS_F_PART,
        "downstream_bc_method": DOWNSTREAM_BC_METHOD,
        "junction_bc_name": "junction BC",
        "junction_snap_tolerance": 100.0,
        "branch_connectivity_threshold": 0.25,
        "unsteady_number": "01",
        "plan_number": "01",
        "template_unsteady_name": str(template_unsteady) if template_unsteady else None,
        "template_unsteady_hdf_name": str(template_unsteady_hdf) if template_unsteady_hdf else None,
        "template_plan_name": str(template_plan) if template_plan else None,
        "unsteady_title": f"{ras_project_name}_Unsteady",
        "plan_title": f"{ras_project_name}_Unsteady_Plan",
        "plan_short_identifier": plan_short_identifier(ras_project_name),
        "plan_flow_regime": "Mixed Flow",
        "plan_simulation_date": ",,,",
        "auto_plan_simulation_date": True,
        "simulation_start_time": "0000",
        "simulation_duration_hours": SIMULATION_DURATION_HOURS,
        "simulation_start_offset_hours": 0.0,
        "simulation_end_offset_hours": 0.0,
        "plan_computation_interval": PLAN_COMPUTATION_INTERVAL,
        "plan_hydrograph_output_interval": "5MIN",
        "plan_output_interval": "5MIN",
        "plan_detailed_output_interval": "5MIN",
        "plan_instantaneous_interval": "5MIN",
        "plan_mapping_interval": "5MIN",
        "plan_use_courant_timestep": True,
        "plan_use_time_series_timestep": False,
        "plan_max_courant": 1.0,
        "plan_min_courant": 0.45,
        "plan_steps_below_min_before_doubling": 4,
        "plan_max_doubling_base_timestep": 2,
        "plan_max_halving_base_timestep": 2,
        "plan_residence_courant": 0.0,
        "plan_run_htab": True,
        "plan_run_unet": True,
        "plan_run_postprocess": False,
        "plan_run_rasmapper": True,
        "plan_num_cores": 0,
        "upstream_bc_name": "upstream BC",
        "downstream_bc_name": "downstream BC",
        "upstream_flow_interval": "1HOUR",
        "upstream_flow_hydrograph_slope": 0.05,
        "downstream_friction_slope": None,
        "unsteady_dss_file_relative": None,
        "compute_timeout_seconds": COMPUTE_TIMEOUT_SECONDS,
        "auto_confirm_geometry_preprocessor": True,
        "auto_adjust_simulation_window_from_dss": True,
        "copy_compute_results_to_project": True,
    }

    allowed_keys = v01_config_keys(rasmapper_script)
    return {key: jsonable(value) for key, value in raw_config.items() if key in allowed_keys}, output_folder


def write_component_json(
    config: Config,
    spec: RunSpec,
    json_dir: Path,
    rasmapper_script: Path,
) -> tuple[Path, dict[str, Any], Path]:
    v01_config, output_folder = build_v01_json_config(
        config,
        spec,
        rasmapper_script=rasmapper_script,
    )
    json_dir.mkdir(parents=True, exist_ok=True)
    json_path = json_dir / f"{safe_name(spec.model_project_name)}_rasmapper_v01.json"
    json_path.write_text(json.dumps(v01_config, indent=2), encoding="utf-8")
    return json_path, v01_config, output_folder


def resolve_breakline_source(
    config: Config,
    model_project_name: str,
    dtm_result: dict[str, Any],
) -> Path:
    merged_banks = optional_result_path(dtm_result, "merged_banks_shp")
    if merged_banks is not None:
        return merged_banks

    sources: list[Path] = []
    for product in dtm_result.get("connected_bank_products", []) or []:
        for key in ("merged_banks_shp", "junction_clipped_banks_shp"):
            path = optional_result_path(product, key)
            if path is not None:
                sources.append(path)
    for channel in dtm_result.get("channels", []) or []:
        bank_path = channel.get("bank_shp_path")
        if bank_path and Path(bank_path).exists():
            sources.append(Path(bank_path))

    if not sources:
        raise FileNotFoundError(f"No bank/breakline shapefile was found for {model_project_name}.")
    return write_combined_breakline_shapefile(config, model_project_name, sources)


def resolve_landcover_source(
    config: Config,
    project_name: str,
    perimeter_shp: Path,
) -> Path:
    configured = optional_existing_path(LANDCOVER_SHP)
    if configured is not None:
        return configured

    preferred = Path(config.ESSENTIALS_PATH) / "LandCover" / "Burdur_Corine_Turef30.shp"
    if preferred.exists():
        return preferred

    search_roots = [Path(config.ESSENTIALS_PATH)]
    for resolver in (config.get_gis_project_path, config.get_project_path):
        try:
            search_roots.append(Path(resolver(project_name)))
        except (FileNotFoundError, ValueError):
            continue

    patterns = ("*Burdur_Corine_Turef30.shp", "*LandCover*.shp", "*Land_Cover*.shp", "*LC*.shp")
    for root in search_roots:
        if not root.exists():
            continue
        for pattern in patterns:
            matches = sorted(root.rglob(pattern))
            if matches:
                return matches[0]

    return write_uniform_landcover_shapefile(config, project_name, perimeter_shp)


def resolve_projection_file(config: Config) -> Path:
    configured = optional_existing_path(PROJECTION_FILE)
    if configured is not None:
        return configured
    return find_essential_file(config, "TUREF_CM30_projection.prj", "*.prj")


def resolve_dss_file(config: Config) -> Path:
    configured = optional_existing_path(DSS_FILE)
    if configured is not None:
        return configured
    return find_essential_file(config, "Burdur_Debiler.dss", "*.dss")


def resolve_reference_geometry_file(rasmapper_script: Path, suffix: str) -> Path | None:
    configured = (
        optional_existing_path(REFERENCE_GEOM_HDF_PATH)
        if suffix.endswith(".hdf")
        else optional_existing_path(REFERENCE_GEOM_PATH)
    )
    if configured is not None:
        return configured

    fallback_root = rasmapper_script.parent / "inputs" / "ReferenceGeometry"
    if fallback_root.exists():
        matches = sorted(fallback_root.glob(f"*{suffix}"))
        if matches:
            return matches[0]
    return None


def resolve_template_file(rasmapper_script: Path, folder_name: str, suffix: str) -> Path | None:
    configured = {
        ("UnsteadyTemplate", ".u01"): optional_existing_path(TEMPLATE_UNSTEADY_PATH),
        ("UnsteadyTemplate", ".u01.hdf"): optional_existing_path(TEMPLATE_UNSTEADY_HDF_PATH),
        ("PlanTemplate", ".p01"): optional_existing_path(TEMPLATE_PLAN_PATH),
    }.get((folder_name, suffix))
    if configured is not None:
        return configured

    fallback_root = rasmapper_script.parent / "inputs" / folder_name
    if fallback_root.exists():
        matches = sorted(fallback_root.glob(f"*{suffix}"))
        if matches:
            return matches[0]
    return None


def find_essential_file(config: Config, preferred_name: str, pattern: str) -> Path:
    for root in config.get_essential_directories():
        preferred = root / preferred_name
        if preferred.exists():
            return preferred
    for root in config.get_essential_directories():
        matches = sorted(root.rglob(pattern)) if root.exists() else []
        if matches:
            return matches[0]
    raise FileNotFoundError(f"Could not find {preferred_name!r} or pattern {pattern!r}.")


def write_junction_coordinate_csv(
    config: Config,
    model_project_name: str,
    dtm_result: dict[str, Any],
) -> Path | None:
    junctions = list(dtm_result.get("junctions", []) or [])
    if not junctions:
        return None

    channel_by_name = {
        _normalize_name(channel.get("name")): channel
        for channel in dtm_result.get("channels", []) or []
    }
    rows: list[dict[str, Any]] = []
    fieldnames: set[str] = set()
    for junction in junctions:
        row = {
            key: value
            for key, value in dict(junction).items()
            if key != "extended_centerline"
        }
        main = channel_by_name.get(_normalize_name(row.get("main")))
        tributary = channel_by_name.get(_normalize_name(row.get("tributary")))
        if main:
            row["main_cross_section_csv"] = main.get("cross_section_csv", "")
        if tributary:
            row["tributary_cross_section_csv"] = tributary.get("cross_section_csv", "")
        row.setdefault("source", "implementationDTM")
        rows.append(row)
        fieldnames.update(row.keys())

    csv_path = project_temp_2d_dir(config, model_project_name) / f"{model_project_name}_2d_junctions.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    ordered = [
        field
        for field in (
            "main",
            "tributary",
            "main_cross_section_csv",
            "tributary_cross_section_csv",
            "tributary_endpoint",
            "x",
            "y",
            "easting",
            "northing",
            "elevation",
            "source",
        )
        if field in fieldnames
    ]
    ordered.extend(sorted(fieldnames - set(ordered)))
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ordered)
        writer.writeheader()
        writer.writerows(rows)
    return csv_path


def write_uniform_landcover_shapefile(
    config: Config,
    project_name: str,
    perimeter_shp: Path,
) -> Path:
    import shapefile

    output_path = project_temp_2d_dir(config, project_name) / f"{project_name}_Default_LandCover.shp"
    remove_shapefile_family(output_path)
    reader = shapefile.Reader(str(perimeter_shp))
    writer = shapefile.Writer(str(output_path.with_suffix("")), shapeType=shapefile.POLYGON)
    writer.field("KodText", "C", size=16)
    writer.field("Adi", "C", size=64)
    writer.field("Manningn", "F", size=10, decimal=4)
    for shape in reader.shapes():
        for part in shape_parts(shape):
            if len(part) >= 3:
                writer.poly([part])
                writer.record("1", "Default", 0.035)
    writer.close()
    copy_projection_sidecar(perimeter_shp, output_path)
    return output_path


def write_combined_breakline_shapefile(
    config: Config,
    project_name: str,
    sources: Sequence[Path],
) -> Path:
    import shapefile

    output_path = project_temp_2d_dir(config, project_name) / f"{project_name}_2D_Breaklines.shp"
    remove_shapefile_family(output_path)
    writer = shapefile.Writer(str(output_path.with_suffix("")), shapeType=shapefile.POLYLINE)
    writer.field("Name", "C", size=80)
    feature_count = 0
    first_source: Path | None = None
    seen: set[tuple[tuple[float, float], ...]] = set()
    for source in sources:
        source = Path(source)
        if not source.exists():
            continue
        first_source = first_source or source
        reader = shapefile.Reader(str(source))
        for shape_index, shape in enumerate(reader.shapes(), start=1):
            for part in shape_parts(shape):
                if len(part) < 2:
                    continue
                key = tuple((round(x, 3), round(y, 3)) for x, y in part)
                if key in seen:
                    continue
                seen.add(key)
                writer.line([part])
                writer.record(f"{source.stem}_{shape_index}")
                feature_count += 1
    writer.close()
    if feature_count == 0:
        remove_shapefile_family(output_path)
        raise FileNotFoundError(f"No usable breakline features were found for {project_name}.")
    if first_source:
        copy_projection_sidecar(first_source, output_path)
    return output_path


def validate_v01_config(
    config: dict[str, Any],
    *,
    require_reference_geometry: bool,
) -> None:
    files_root = Path(config["files_root"])
    dss = source_path(files_root, config["dss_name"])
    required = [
        files_root,
        Path(config["ras_exe"]),
        Path(config["gdal_grid_exe"]),
        Path(config["gdal_grid_exe"]).with_name("gdal_rasterize.exe"),
        source_path(files_root, config["projection_name"]),
        source_path(files_root, config["dtm_name"]),
        source_path(files_root, config["perimeter_name"]),
        source_path(files_root, config["breakline_name"]),
        dss,
        dss.parent / f"{dss.stem}.dsc.h5",
        source_path(files_root, config["landcover_name"]),
    ]
    for cross_section in as_list(config.get("cross_section_name")):
        required.append(source_path(files_root, cross_section))
    if config.get("junction_bc_csv_name"):
        required.append(source_path(files_root, config["junction_bc_csv_name"]))
    if require_reference_geometry:
        required.extend(
            [
                source_path(files_root, config["reference_geom_name"]),
                source_path(files_root, config["reference_geom_hdf_name"]),
            ]
        )

    missing = [path for path in required if not Path(path).exists()]
    if missing:
        details = "\n".join(f"- {path}" for path in missing)
        raise FileNotFoundError(f"Required rasmapper_v01 inputs missing:\n{details}")

    existing_pair = [
        optional_path_value(config.get("existing_landcover_tif_name")),
        optional_path_value(config.get("existing_landcover_hdf_name")),
    ]
    if any(path is not None for path in existing_pair) and not all(
        path is not None for path in existing_pair
    ):
        raise FileNotFoundError(
            "Existing native landcover layer is incomplete: both "
            "existing_landcover_tif_name and existing_landcover_hdf_name are required."
        )
    present_existing = [path for path in existing_pair if path is not None]
    if any(path.exists() for path in present_existing) and not all(
        path.exists() for path in present_existing
    ):
        details = "\n".join(f"- {path}" for path in present_existing if not path.exists())
        raise FileNotFoundError(f"Existing native landcover layer is incomplete:\n{details}")


def run_v01_commands(
    *,
    python_exe: str,
    rasmapper_script: Path,
    rasmapper_root: Path,
    json_path: Path,
    steps: Sequence[str],
    skip_terrain: bool,
    install_timeout: int,
    compute_timeout: int,
    skip_geometry_regeneration: bool,
    compute_overwrite: bool,
    geom: str,
) -> tuple[bool, str, list[list[str]], list[dict[str, Any]]]:
    commands: list[list[str]] = []
    command_results: list[dict[str, Any]] = []

    for step in steps:
        command_step = "prepare" if step == "prepare-skip-terrain" else step
        command = [
            python_exe,
            str(rasmapper_script),
            "--config-json",
            str(json_path),
            command_step,
        ]
        if step == "prepare-skip-terrain":
            command.append("--skip-terrain")
        elif command_step in {"prepare", "prepare-open"} and skip_terrain:
            command.append("--skip-terrain")
        if command_step in {"install-geometry", "regenerate-geometry", "open", "prepare-open"}:
            command.extend(["--timeout", str(install_timeout)])
        if command_step == "install-geometry" and skip_geometry_regeneration:
            command.append("--skip-regeneration")
        if command_step == "compute-plan":
            if compute_overwrite:
                command.append("--overwrite")
            command.extend(["--timeout", str(compute_timeout)])
        if command_step in {"apply-mannings", "check-mannings"} and geom:
            command.extend(["--geom", geom])

        commands.append(command)
        logger.info("Running %s", " ".join(command))
        print(f"    rasmapper_v01 step: {step}", flush=True)
        print(f"    command: {' '.join(command)}", flush=True)
        outer_timeout = subprocess_timeout_for_step(
            command_step,
            install_timeout=install_timeout,
            compute_timeout=compute_timeout,
        )
        try:
            completed = subprocess.run(
                command,
                cwd=rasmapper_root,
                timeout=outer_timeout,
            )
        except subprocess.TimeoutExpired:
            command_results.append(
                {
                    "step": step,
                    "command_step": command_step,
                    "returncode": None,
                    "command": command,
                    "timeout_seconds": outer_timeout,
                }
            )
            return (
                False,
                (
                    f"rasmapper_v01.py {step} exceeded the wrapper timeout "
                    f"({outer_timeout} seconds). Close HEC-RAS/RAS Mapper/"
                    "PipeServer.exe and rerun this component."
                ),
                commands,
                command_results,
            )
        command_results.append(
            {
                "step": step,
                "command_step": command_step,
                "returncode": completed.returncode,
                "command": command,
            }
        )
        if completed.returncode != 0:
            return (
                False,
                f"rasmapper_v01.py {step} failed with exit code {completed.returncode}.",
                commands,
                command_results,
            )

    return True, "rasmapper_v01.py workflow completed.", commands, command_results


def subprocess_timeout_for_step(
    step: str,
    *,
    install_timeout: int,
    compute_timeout: int,
) -> int | None:
    if step in {"install-geometry", "regenerate-geometry", "open", "prepare-open"}:
        return int(install_timeout) + 180
    if step == "compute-plan":
        return int(compute_timeout) + 300
    return None


def write_summary(config: Config, results: list[JsonRunResult]) -> Path:
    summary_path = Path(config.TEMP_PATH) / "implementation2djson_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps([asdict(result) for result in results], indent=2),
        encoding="utf-8",
    )
    return summary_path


def v01_config_keys(rasmapper_script: Path) -> set[str]:
    keys: set[str] = set()
    try:
        tree = ast.parse(rasmapper_script.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == "RasMapperConfig":
                for item in node.body:
                    if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                        keys.add(item.target.id)
                break
    except (OSError, SyntaxError):
        logger.warning("Could not parse RasMapperConfig fields from %s.", rasmapper_script)

    template_path = rasmapper_script.with_name("model_template_v01.json")
    if template_path.exists():
        try:
            raw = json.loads(template_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                keys.update(raw.keys())
        except json.JSONDecodeError:
            logger.warning("Could not parse %s.", template_path)
    return keys


def filter_results_by_channels(
    results: Sequence[dict[str, Any]],
    sub_project_names: Sequence[str],
) -> list[dict[str, Any]]:
    target = {_normalize_name(name) for name in sub_project_names}
    target.discard("")
    if not target:
        return []
    return [
        result
        for result in results
        if result_channel_names(result) and result_channel_names(result) == target
    ]


def has_channel_scoped_results(results: Sequence[dict[str, Any]]) -> bool:
    return any(result_channel_names(result) for result in results)


def result_channel_names(result: dict[str, Any]) -> set[str]:
    names = {
        _normalize_name(channel.get("name"))
        for channel in result.get("channels", []) or []
        if channel.get("name")
    }
    names.discard("")
    return names


def result_matches_component(result: dict[str, Any], component_name: str) -> bool:
    target = _normalize_name(component_name)
    component_value = result.get("component")
    if component_value:
        return _normalize_name(component_value) == target
    for value in (result.get("project"), result.get("project_name")):
        if _normalize_name(value) == target:
            return True
    output_tif = result.get("output_tif")
    if output_tif:
        return _normalize_name(Path(output_tif).stem).startswith(target)
    return False


def result_matches_project(result: dict[str, Any], project_name: str) -> bool:
    target = _normalize_name(project_name)
    for value in (result.get("project"), result.get("project_name")):
        if _normalize_name(value) == target:
            return True
    component_value = result.get("component")
    if component_value and _normalize_name(component_value) != target:
        return False
    output_tif = result.get("output_tif")
    if output_tif:
        path = Path(output_tif)
        return _normalize_name(path.stem).startswith(target) or any(
            _normalize_name(part) == target for part in path.parts
        )
    return False


def required_result_path(result: dict[str, Any], key: str) -> Path:
    path = optional_result_path(result, key)
    if path is None:
        raise FileNotFoundError(f"DTM result is missing required path: {key}")
    return path


def optional_result_path(result: dict[str, Any], key: str) -> Path | None:
    value = result.get(key)
    if not value:
        return None
    path = Path(value)
    return path if path.exists() else None


def project_temp_2d_dir(config: Config, project_name: str) -> Path:
    return Path(config.get_temp_project_path(project_name)) / "2D"


def shape_parts(shape: Any) -> list[list[tuple[float, float]]]:
    points = [(float(point[0]), float(point[1])) for point in shape.points]
    starts = list(shape.parts) + [len(points)]
    return [points[starts[index]:starts[index + 1]] for index in range(len(starts) - 1)]


def remove_shapefile_family(shp_path: Path) -> None:
    base = shp_path.with_suffix("").name
    shp_path.parent.mkdir(parents=True, exist_ok=True)
    for existing in shp_path.parent.glob(f"{base}.*"):
        existing.unlink()


def copy_projection_sidecar(source_shp: Path, destination_shp: Path) -> None:
    source_prj = Path(source_shp).with_suffix(".prj")
    if source_prj.exists():
        shutil.copy2(source_prj, Path(destination_shp).with_suffix(".prj"))


def source_path(root: Path, value: Any) -> Path:
    path = Path(str(value))
    if path.is_absolute():
        return path
    return root / path


def optional_path_value(value: Any) -> Path | None:
    if value in (None, ""):
        return None
    return Path(str(value))


def optional_existing_path(value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    return path if path.exists() else None


def existing_path_string(value: str | None) -> str | None:
    path = optional_existing_path(value)
    return str(path) if path else None


def as_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item).strip()]
    if value in (None, ""):
        return []
    return [str(value)]


def jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value


def safe_name(value: Any) -> str:
    safe = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(value)).strip("_")
    return safe or "rasmapper_project"


def plan_short_identifier(project_name: str) -> str:
    value = re.sub(r"[^0-9A-Za-z_]+", "_", project_name).strip("_")
    return value if len(value) <= 24 else value[:24].rstrip("_")


def storage_area_name_for_component(component_name: str) -> str:
    safe = re.sub(r"[^0-9A-Za-z]+", "", str(component_name))
    if not safe:
        safe = "StudyArea"
    return f"{safe}Perimeter"


def preferred_dss_a_part(project_name: str, dtm_result: dict[str, Any]) -> str:
    channels = dtm_result.get("channels", []) or []
    if channels and channels[0].get("name"):
        return str(channels[0]["name"])
    return project_name


def _normalize_name(value: Any) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "", str(value or "")).upper()


def names_match(left: Any, right: Any) -> bool:
    left_norm = _normalize_name(left)
    right_norm = _normalize_name(right)
    if not left_norm or not right_norm:
        return False
    return (
        left_norm == right_norm
        or left_norm.endswith(right_norm)
        or right_norm.endswith(left_norm)
    )


def main() -> list[JsonRunResult]:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    args = parse_args()
    config = build_config(args)
    rasmapper_root = args.rasmapper_root.resolve()
    rasmapper_script = rasmapper_root / "rasmapper_v01.py"
    if not rasmapper_script.exists():
        raise FileNotFoundError(f"rasmapper_v01.py was not found: {rasmapper_script}")

    steps = selected_steps(args)
    require_reference_geometry = any(
        step in {"install-geometry", "sync-geometry", "regenerate-geometry"}
        for step in steps
    )
    json_dir = args.json_dir or Path(config.TEMP_PATH) / "implementation2djson"
    run_specs = build_run_specs(config, selected_projects(args))

    print("\n2D JSON GUI/direct run configuration", flush=True)
    print(f"  Source: {config.structure_source}", flush=True)
    print(f"  Master project path: {config.PROJECT_FOLDER}", flush=True)
    print(f"  rasmapper_v01.py: {rasmapper_script}", flush=True)
    print(f"  rasmapper Python: {args.python_exe}", flush=True)
    print(f"  JSON output folder: {json_dir}", flush=True)
    print(f"  Steps: {', '.join(steps)}", flush=True)
    print(f"  Geometry GUI timeout: {args.install_timeout} seconds", flush=True)
    print(f"  Compute timeout: {args.compute_timeout} seconds", flush=True)
    if workflow_has_gui_step(steps):
        print(
            "  GUI note: close existing HEC-RAS/RAS Mapper/PipeServer.exe "
            "sessions before the geometry step.",
            flush=True,
        )
    print(f"  Components to run: {len(run_specs)}", flush=True)

    results: list[JsonRunResult] = []
    for index, spec in enumerate(run_specs, start=1):
        command_results: list[dict[str, Any]] = []
        commands: list[list[str]] = []
        success = True
        message = "JSON generated."
        output_folder = Path(config.HEC_PATH) / PROJECT_NAME_TEMPLATE.format(project=spec.model_project_name)

        try:
            print(
                f"\n[{index}/{len(run_specs)}] {spec.model_project_name} "
                f"from {spec.source_project_name}: {', '.join(spec.sub_project_names)}",
                flush=True,
            )
            json_path, v01_config, output_folder = write_component_json(
                config,
                spec,
                json_dir,
                rasmapper_script,
            )
            print(f"  JSON written: {json_path}", flush=True)
            if not args.json_only:
                validate_v01_config(
                    v01_config,
                    require_reference_geometry=require_reference_geometry,
                )
                message = "JSON generated and inputs validated."
                print("  Inputs validated.", flush=True)

            if not args.dry_run and not args.json_only:
                success, message, commands, command_results = run_v01_commands(
                    python_exe=args.python_exe,
                    rasmapper_script=rasmapper_script,
                    rasmapper_root=rasmapper_root,
                    json_path=json_path,
                    steps=steps,
                    skip_terrain=args.skip_terrain,
                    install_timeout=args.install_timeout,
                    compute_timeout=args.compute_timeout,
                    skip_geometry_regeneration=args.skip_geometry_regeneration,
                    compute_overwrite=args.compute_overwrite,
                    geom=args.geom,
                )
        except Exception as exc:
            logger.exception("2D JSON workflow failed for %s", spec.model_project_name)
            success = False
            message = str(exc)
            json_path = json_dir / f"{safe_name(spec.model_project_name)}_rasmapper_v01.json"

        result = JsonRunResult(
            source_project_name=spec.source_project_name,
            model_project_name=spec.model_project_name,
            sub_project_names=list(spec.sub_project_names),
            json_path=str(json_path),
            output_folder=str(output_folder),
            success=success,
            message=message,
            commands=commands,
            command_results=command_results,
        )
        results.append(result)
        if not success and args.stop_on_error:
            break

    summary_path = write_summary(config, results)
    print(f"\n2D JSON summary written to: {summary_path}")
    for result in results:
        status = "OK" if result.success else "FAILED"
        sub_projects = ", ".join(result.sub_project_names)
        print(f"[{status}] {result.model_project_name} ({sub_projects})")
        print(f"  JSON: {result.json_path}")
        print(f"  Output: {result.output_folder}")
        print(f"  {result.message}")

    return results


if __name__ == "__main__":
    main()
