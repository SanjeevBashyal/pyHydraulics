#!/usr/bin/env python
"""
Prepare a HEC-RAS 2D RAS Mapper workspace from project source files.

This script follows a hybrid workflow:
1. Automate the parts that ras_commander can safely do from files.
2. Generate helper artifacts for the parts that still require RAS Mapper.
3. Keep all outputs reviewable inside a normal HEC-RAS project folder.

The script does not pretend to have a stable file-only API for every
RAS Mapper dialog. New 2D geometry creation, perimeter authoring,
breakline enforcement, mesh repair, and boundary-condition placement are
left as explicit GUI checkpoints with supporting files generated here.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import textwrap
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import h5py
import numpy as np
import pandas as pd
import rasterio
import shapefile

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ras_commander import (
    GeomLandCover,
    HecRasElements,
    RasMap,
    RasMapperElements,
    Win32Primitives,
    get_logger,
    init_ras_project,
    log_call,
)
from ras_commander import RasTerrain
from ras_commander.gui.workflows.mesh_regeneration import MeshRegenerationWorkflow


DEFAULT_FILES_ROOT = Path(r"F:\codex_rascommander\files")
DEFAULT_RAS_EXE = Path(
    r"C:\Program Files (x86)\HEC\HEC-RAS\6.6\Ras.exe"
)
DEFAULT_GDAL_GRID = Path(
    r"C:\Program Files (x86)\HEC\HEC-RAS\6.6\GDAL\bin64\gdal_grid.exe"
)
DEFAULT_PROJECT_NAME = "Files_4_RASMAPPER"
DEFAULT_WORKING_ROOT = REPO_ROOT / "working"
DEFAULT_EXISTING_LANDCOVER_TIF = Path(
    r"F:\TURFM_files\Files_4_RASMAPPER\LC files\LC_At_clip_V1.tif"
)
DEFAULT_EXISTING_LANDCOVER_HDF = Path(
    r"F:\TURFM_files\Files_4_RASMAPPER\LC files\LC_At_clip_V1.hdf"
)

LOGGER = get_logger(__name__)

HELPER_MAP_LAYER_NAMES = (
    "Perimeter Source",
    "Breakline Source",
    "Cross Sections",
    "Boundary Candidates",
    "Centerline Guide",
    "Landcover Source",
)


@dataclass(frozen=True)
class CrossSectionInfo:
    station_label: str
    station_sort: float
    points: List[Tuple[float, float]]
    mean_point: Tuple[float, float]
    mean_z: float


@dataclass(frozen=True)
class RasMapperConfig:
    files_root: Path
    working_root: Path
    project_name: str
    ras_exe: Path
    gdal_grid_exe: Path
    projection_name: str = "TUREF_CM30_projection.prj"
    dtm_name: str = "SET4_27_DTM_070226_R1.tif"
    perimeter_name: str = "Ataturk.shp"
    breakline_name: str = "BUR-BUR-MER-ATATURK-T_SEV_USTU_V1.shp"
    dss_name: str = "Burdur_Debiler.dss"
    cross_section_name: str = "BUR-BUR-MER-ATATURK-T_KESIT_TESLIM_V1.csv"
    landcover_name: str = "Ataturk_LC.shp"
    existing_landcover_tif_name: Optional[str] = str(
        DEFAULT_EXISTING_LANDCOVER_TIF
    )
    existing_landcover_hdf_name: Optional[str] = str(
        DEFAULT_EXISTING_LANDCOVER_HDF
    )
    reference_geom_name: str = "Bur-AtaturkT.g01"
    reference_geom_hdf_name: str = "Bur-AtaturkT.g01.hdf"
    terrain_layer_name: str = "Terrain"
    flow_area_name: str = "Ataturk_2D"
    geometry_title: str = "ATATURK_2D_CLEAN"
    storage_area_name: Optional[str] = None
    hdf_2d_area_name: Optional[str] = None
    mesh_cell_size: float = 10.0
    breakline_near_spacing: float = 0.5
    breakline_near_repeats: int = 5
    breakline_far_spacing: float = 3.0
    landcover_cell_size: float = 2.0
    landcover_nodata_manning: float = 0.025
    boundary_offset_distance: float = 1.0
    preferred_dss_a_part: str = "BUR-BUR-MER-ATATURK-T"
    preferred_dss_f_part: str = "Q100"
    downstream_bc_method: str = "Normal Depth"

    @property
    def project_root(self) -> Path:
        return (self.working_root / self.project_name).resolve()

    @property
    def project_file(self) -> Path:
        return self.project_root / f"{self.project_name}.prj"

    @property
    def rasmap_path(self) -> Path:
        return self.project_root / f"{self.project_name}.rasmap"

    @property
    def terrain_dir(self) -> Path:
        return self.project_root / "Terrain"

    @property
    def inputs_dir(self) -> Path:
        return self.project_root / "Inputs"

    @property
    def helper_dir(self) -> Path:
        return self.project_root / "Helpers"

    @property
    def reports_dir(self) -> Path:
        return self.project_root / "Reports"

    @property
    def boundary_dir(self) -> Path:
        return self.project_root / "Boundary"

    @property
    def landcover_dir(self) -> Path:
        return self.project_root / "LandCover"

    @property
    def land_classification_dir(self) -> Path:
        return self.project_root / "Land Classification"

    @property
    def projection_src(self) -> Path:
        return self.files_root / self.projection_name

    @property
    def projection_copy(self) -> Path:
        return self.terrain_dir / self.projection_name

    @property
    def dtm_src(self) -> Path:
        return self.files_root / self.dtm_name

    @property
    def perimeter_src(self) -> Path:
        return self.files_root / self.perimeter_name

    @property
    def breakline_src(self) -> Path:
        return self.files_root / self.breakline_name

    @property
    def dss_src(self) -> Path:
        return self.files_root / self.dss_name

    @property
    def dss_catalog_src(self) -> Path:
        return self.files_root / (self.dss_src.stem + ".dsc.h5")

    @property
    def cross_section_src(self) -> Path:
        return self.files_root / self.cross_section_name

    @property
    def landcover_src(self) -> Path:
        return self.files_root / self.landcover_name

    @property
    def reference_geom_src(self) -> Path:
        return self.files_root / self.reference_geom_name

    @property
    def reference_geom_hdf_src(self) -> Path:
        return self.files_root / self.reference_geom_hdf_name

    @property
    def perimeter_copy(self) -> Path:
        return self.inputs_dir / "Perimeter" / self.perimeter_name

    @property
    def breakline_copy(self) -> Path:
        return self.inputs_dir / "Breaklines" / self.breakline_name

    @property
    def landcover_copy(self) -> Path:
        return self.inputs_dir / "LandCover" / self.landcover_name

    @property
    def dss_copy(self) -> Path:
        return self.boundary_dir / self.dss_name

    @property
    def dss_catalog_copy(self) -> Path:
        return self.boundary_dir / self.dss_catalog_src.name

    @property
    def cross_section_copy(self) -> Path:
        return self.boundary_dir / self.cross_section_name

    @property
    def terrain_hdf(self) -> Path:
        return self.terrain_dir / (Path(self.dtm_name).stem + ".hdf")

    @property
    def geom_path(self) -> Path:
        return self.project_root / f"{self.project_name}.g01"

    @property
    def geom_hdf_path(self) -> Path:
        return self.project_root / f"{self.project_name}.g01.hdf"

    @property
    def landcover_raster(self) -> Path:
        return self.land_classification_dir / "LandCover.tif"

    @property
    def landcover_hdf(self) -> Path:
        return self.land_classification_dir / "LandCoverTable.hdf"

    @property
    def existing_landcover_tif_src(self) -> Optional[Path]:
        if not self.existing_landcover_tif_name:
            return None
        return Path(self.existing_landcover_tif_name)

    @property
    def existing_landcover_hdf_src(self) -> Optional[Path]:
        if not self.existing_landcover_hdf_name:
            return None
        return Path(self.existing_landcover_hdf_name)

    @property
    def existing_landcover_prj_src(self) -> Optional[Path]:
        if self.existing_landcover_tif_src is None:
            return None
        return self.existing_landcover_tif_src.with_suffix(".prj")

    @property
    def existing_landcover_tif_copy(self) -> Path:
        return self.land_classification_dir / self.existing_landcover_tif_src.name

    @property
    def existing_landcover_hdf_copy(self) -> Path:
        return self.land_classification_dir / self.existing_landcover_hdf_src.name

    @property
    def existing_landcover_prj_copy(self) -> Path:
        return self.existing_landcover_tif_copy.with_suffix(".prj")

    @property
    def has_existing_landcover_layer(self) -> bool:
        return (
            self.existing_landcover_tif_src is not None
            and self.existing_landcover_hdf_src is not None
            and self.existing_landcover_tif_src.exists()
            and self.existing_landcover_hdf_src.exists()
        )

    @property
    def active_landcover_map_tif(self) -> Path:
        if self.has_existing_landcover_layer:
            return self.existing_landcover_tif_copy
        return self.landcover_raster

    @property
    def active_landcover_map_hdf(self) -> Optional[Path]:
        if self.has_existing_landcover_layer:
            return self.existing_landcover_hdf_copy
        return None

    @property
    def landcover_mannings_raster(self) -> Path:
        cell_size = (
            str(int(self.landcover_cell_size))
            if float(self.landcover_cell_size).is_integer()
            else str(self.landcover_cell_size).replace(".", "p")
        )
        return (
            self.landcover_dir
            / f"{self.landcover_copy.stem}_ManningN_{cell_size}m.tif"
        )

    @property
    def landcover_lookup_csv(self) -> Path:
        return self.landcover_dir / "landcover_lookup.csv"

    @property
    def landcover_status_json(self) -> Path:
        return self.reports_dir / "landcover_layer_status.json"

    @property
    def mesh_qc_json(self) -> Path:
        return self.reports_dir / "mesh_enforcement_summary.json"

    @property
    def dss_catalog_csv(self) -> Path:
        return self.boundary_dir / "dss_catalog.csv"

    @property
    def boundary_json(self) -> Path:
        return self.boundary_dir / "boundary_candidates.json"

    @property
    def boundary_csv(self) -> Path:
        return self.boundary_dir / "boundary_candidates.csv"

    @property
    def cross_sections_shp(self) -> Path:
        return self.helper_dir / "cross_sections.shp"

    @property
    def centerline_shp(self) -> Path:
        return self.helper_dir / "centerline.shp"

    @property
    def boundary_shp(self) -> Path:
        return self.helper_dir / "boundary_candidates.shp"

    @property
    def spatial_overview_png(self) -> Path:
        return self.reports_dir / "spatial_overview.png"

    @property
    def checklist_md(self) -> Path:
        return self.reports_dir / "rasmapper_manual_checklist.md"

    @property
    def prepare_summary_json(self) -> Path:
        return self.reports_dir / "prepare_summary.json"

    @property
    def hecras_version(self) -> str:
        return self.ras_exe.parent.name

    @property
    def gdal_rasterize_exe(self) -> Path:
        return self.gdal_grid_exe.with_name("gdal_rasterize.exe")


def _normalize_code(value: Any) -> str:
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    return text


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    return value


def _resolve_config_path(config_json: Path, raw_value: str) -> Path:
    path = Path(raw_value)
    if path.is_absolute():
        return path.resolve()
    return (config_json.parent / path).resolve()


def load_study_area_overrides(config_json: Path) -> Dict[str, Any]:
    raw = json.loads(config_json.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Study-area config must be a JSON object: {config_json}")

    valid_fields = {field.name for field in fields(RasMapperConfig)}
    unknown = sorted(set(raw) - valid_fields)
    if unknown:
        joined = ", ".join(unknown)
        raise ValueError(
            f"Unknown study-area config keys in {config_json}: {joined}"
        )

    resolved: Dict[str, Any] = {}
    path_fields = {
        "files_root",
        "working_root",
        "ras_exe",
        "gdal_grid_exe",
        "existing_landcover_tif_name",
        "existing_landcover_hdf_name",
    }
    for key, value in raw.items():
        if key in path_fields:
            if value in (None, ""):
                resolved[key] = None
            else:
                resolved[key] = _resolve_config_path(config_json, str(value))
            continue
        resolved[key] = value
    return resolved


def _parse_storage_area_name(lines: Sequence[str]) -> Optional[str]:
    for line in lines:
        if not line.startswith("Storage Area="):
            continue
        raw = line.split("=", 1)[1].strip()
        return raw.split(",", 1)[0].strip()
    return None


def resolve_storage_area_name(
    config: RasMapperConfig,
    geom_path: Optional[Path] = None,
    lines: Optional[Sequence[str]] = None,
) -> str:
    if config.storage_area_name:
        return config.storage_area_name

    if lines is not None:
        parsed = _parse_storage_area_name(lines)
        if parsed:
            return parsed

    candidates = [geom_path, config.geom_path, config.reference_geom_src]
    for candidate in candidates:
        if candidate is None or not candidate.exists():
            continue
        parsed = _parse_storage_area_name(
            candidate.read_text(encoding="utf-8", errors="replace").splitlines(True)
        )
        if parsed:
            return parsed

    return f"{config.perimeter_src.stem}Perimeter"


def resolve_hdf_2d_area_group(
    config: RasMapperConfig,
    handle: h5py.File,
    geom_path: Optional[Path] = None,
    lines: Optional[Sequence[str]] = None,
) -> str:
    base_root = "Geometry/2D Flow Areas"
    if base_root not in handle:
        raise RuntimeError("Geometry HDF does not contain any 2D Flow Areas")

    area_group = handle[base_root]
    candidates: List[str] = []
    if config.hdf_2d_area_name:
        candidates.append(config.hdf_2d_area_name)
    storage_area = resolve_storage_area_name(
        config,
        geom_path=geom_path,
        lines=lines,
    )
    if storage_area not in candidates:
        candidates.append(storage_area)

    for candidate in candidates:
        if candidate in area_group:
            return f"{base_root}/{candidate}"

    available = list(area_group.keys())
    if len(available) == 1:
        return f"{base_root}/{available[0]}"

    raise RuntimeError(
        "Could not determine the 2D flow area group in the geometry HDF. "
        f"Candidates={candidates}, available={available}"
    )


@log_call
def validate_inputs(
    config: RasMapperConfig,
    require_reference_geometry: bool = False,
) -> None:
    required = [
        config.files_root,
        config.ras_exe,
        config.gdal_grid_exe,
        config.gdal_rasterize_exe,
        config.projection_src,
        config.dtm_src,
        config.perimeter_src,
        config.breakline_src,
        config.dss_src,
        config.dss_catalog_src,
        config.cross_section_src,
        config.landcover_src,
    ]
    if require_reference_geometry:
        required.extend(
            [
                config.reference_geom_src,
                config.reference_geom_hdf_src,
            ]
        )
    missing = [path for path in required if not path.exists()]
    if missing:
        details = "\n".join(f"- {path}" for path in missing)
        raise FileNotFoundError(f"Required inputs missing:\n{details}")

    existing_pair = [
        config.existing_landcover_tif_src,
        config.existing_landcover_hdf_src,
    ]
    if any(path is not None for path in existing_pair) and not all(
        path is not None for path in existing_pair
    ):
        details = "\n".join(
            f"- {name}"
            for name, path in (
                ("existing_landcover_tif_name", config.existing_landcover_tif_src),
                ("existing_landcover_hdf_name", config.existing_landcover_hdf_src),
            )
            if path is None
        )
        raise FileNotFoundError(
            "Existing native landcover layer is incomplete:\n"
            f"{details}"
        )

    present_existing = [path for path in existing_pair if path is not None]
    if any(path.exists() for path in present_existing) and not all(
        path.exists() for path in present_existing
    ):
        details = "\n".join(
            f"- {path}"
            for path in present_existing
            if not path.exists()
        )
        raise FileNotFoundError(
            "Existing native landcover layer is incomplete:\n"
            f"{details}"
        )


def ensure_dirs(paths: Iterable[Path]) -> None:
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)


def copy_file(src: Path, dst: Path) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return dst


@log_call
def copy_shapefile_family(src_shp: Path, dst_dir: Path) -> Path:
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst_shp = dst_dir / src_shp.name
    for family_file in src_shp.parent.glob(f"{src_shp.stem}.*"):
        shutil.copy2(family_file, dst_dir / family_file.name)
    return dst_shp


def remove_shapefile_family(shp_path: Path) -> None:
    base_name = shp_path.with_suffix("").name
    for existing in shp_path.parent.glob(f"{base_name}.*"):
        existing.unlink()


def write_minimal_project_file(config: RasMapperConfig) -> None:
    if config.project_file.exists():
        LOGGER.info("Project file already exists: %s", config.project_file)
        return

    content = textwrap.dedent(
        f"""\
        Proj Title={config.project_name}
        Default Exp/Contr=0.3,0.1
        SI Units=1
        English Units=0
        """
    )
    config.project_file.write_text(content, encoding="utf-8")
    LOGGER.info("Created project file: %s", config.project_file)


def _ensure_projection_element(rasmap_path: Path, projection_path: Path) -> None:
    tree = ET.parse(rasmap_path)
    root = tree.getroot()
    rel_projection = ".\\" + str(
        projection_path.relative_to(rasmap_path.parent)
    ).replace("/", "\\")

    projection = root.find("RASProjectionFilename")
    if projection is None:
        projection = ET.Element("RASProjectionFilename")
        version_elem = root.find("Version")
        insert_index = list(root).index(version_elem) + 1 if version_elem is not None else 0
        root.insert(insert_index, projection)
    projection.set("Filename", rel_projection)

    tree.write(rasmap_path, encoding="utf-8", xml_declaration=False)


def write_minimal_rasmap(config: RasMapperConfig) -> None:
    if config.rasmap_path.exists():
        LOGGER.info("RASMapper file already exists: %s", config.rasmap_path)
        return

    xml_content = textwrap.dedent(
        """\
        <RASMapper>
          <Version>2.0</Version>
          <Geometries Checked="True" Expanded="True" />
          <Results Checked="True" Expanded="True" />
          <MapLayers Checked="True" Expanded="True" />
          <CurrentSettings>
            <ProjectSettings />
            <Folders />
          </CurrentSettings>
        </RASMapper>
        """
    )
    config.rasmap_path.write_text(xml_content, encoding="utf-8")
    _ensure_projection_element(config.rasmap_path, config.projection_copy)
    LOGGER.info("Created RASMapper file: %s", config.rasmap_path)


def load_polygon_rings(shp_path: Path) -> List[List[Tuple[float, float]]]:
    with shapefile.Reader(str(shp_path)) as reader:
        if reader.numRecords < 1:
            raise ValueError(f"No polygons found in {shp_path}")
        shape = reader.shape(0)
        parts = list(shape.parts) + [len(shape.points)]
        rings = []
        for idx in range(len(parts) - 1):
            start = parts[idx]
            end = parts[idx + 1]
            ring = [(float(x), float(y)) for x, y in shape.points[start:end]]
            if ring:
                rings.append(ring)
        if not rings:
            raise ValueError(f"No polygon ring coordinates found in {shp_path}")
        return rings


def load_polyline_features(
    shp_path: Path,
) -> List[Tuple[Dict[str, Any], List[Tuple[float, float]]]]:
    with shapefile.Reader(str(shp_path)) as reader:
        field_names = [field[0] for field in reader.fields[1:]]
        features = []
        for shape_record in reader.iterShapeRecords():
            record = dict(zip(field_names, shape_record.record))
            parts = list(shape_record.shape.parts) + [len(shape_record.shape.points)]
            for idx in range(len(parts) - 1):
                start = parts[idx]
                end = parts[idx + 1]
                coords = [
                    (float(x), float(y))
                    for x, y in shape_record.shape.points[start:end]
                ]
                if len(coords) >= 2:
                    features.append((record, coords))
        return features


def _polygon_centroid(ring: Sequence[Tuple[float, float]]) -> Tuple[float, float]:
    if len(ring) < 3:
        xs = [pt[0] for pt in ring]
        ys = [pt[1] for pt in ring]
        return sum(xs) / len(xs), sum(ys) / len(ys)

    coords = list(ring)
    if coords[0] != coords[-1]:
        coords.append(coords[0])

    area2 = 0.0
    cx = 0.0
    cy = 0.0
    for idx in range(len(coords) - 1):
        x0, y0 = coords[idx]
        x1, y1 = coords[idx + 1]
        cross = x0 * y1 - x1 * y0
        area2 += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross

    if abs(area2) < 1e-12:
        xs = [pt[0] for pt in ring]
        ys = [pt[1] for pt in ring]
        return sum(xs) / len(xs), sum(ys) / len(ys)

    area = area2 / 2.0
    return cx / (6.0 * area), cy / (6.0 * area)


def _point_side_of_line(
    point: Tuple[float, float],
    line: Sequence[Tuple[float, float]],
) -> str:
    start_x, start_y = line[0]
    end_x, end_y = line[-1]
    px, py = point
    cross = (end_x - start_x) * (py - start_y) - (end_y - start_y) * (px - start_x)
    if cross > 0:
        return "left"
    if cross < 0:
        return "right"
    return "on"


def _station_sort_value(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("inf")


def _dedupe_consecutive_points(
    points: Sequence[Tuple[float, float]],
    tol: float = 1e-9,
) -> List[Tuple[float, float]]:
    deduped: List[Tuple[float, float]] = []
    for point in points:
        if not deduped:
            deduped.append(point)
            continue
        prev = deduped[-1]
        if (
            abs(prev[0] - point[0]) <= tol
            and abs(prev[1] - point[1]) <= tol
        ):
            continue
        deduped.append(point)
    return deduped


@log_call
def load_cross_sections(csv_path: Path) -> List[CrossSectionInfo]:
    data = pd.read_csv(csv_path)
    required = {"Station", "X", "Y", "Z"}
    missing = required - set(data.columns)
    if missing:
        raise ValueError(
            f"Cross-section CSV missing columns: {sorted(missing)}"
        )

    sections: List[CrossSectionInfo] = []
    for station, group in data.groupby("Station", sort=False):
        points = _dedupe_consecutive_points(
            list(zip(group["X"].astype(float), group["Y"].astype(float)))
        )
        xs = [pt[0] for pt in points]
        ys = [pt[1] for pt in points]
        sections.append(
            CrossSectionInfo(
                station_label=str(station),
                station_sort=_station_sort_value(station),
                points=points,
                mean_point=(sum(xs) / len(xs), sum(ys) / len(ys)),
                mean_z=float(group["Z"].astype(float).mean()),
            )
        )

    sections.sort(key=lambda item: item.station_sort)
    if len(sections) >= 2 and sections[0].mean_z < sections[-1].mean_z:
        sections.reverse()

    return sections


def estimate_normal_depth_slope(
    sections: Sequence[CrossSectionInfo],
) -> float:
    if len(sections) < 2:
        return 0.0005

    downstream = sections[-1]
    upstream_neighbor = sections[-2]
    dx = downstream.mean_point[0] - upstream_neighbor.mean_point[0]
    dy = downstream.mean_point[1] - upstream_neighbor.mean_point[1]
    distance = math.hypot(dx, dy)
    if distance <= 0.0:
        return 0.0005

    dz = abs(upstream_neighbor.mean_z - downstream.mean_z)
    slope = dz / distance if distance else 0.0005
    return max(slope, 0.0001)


def _write_prj_for_shapefile(config: RasMapperConfig, shp_path: Path) -> None:
    prj_path = shp_path.with_suffix(".prj")
    shutil.copy2(config.projection_copy, prj_path)


@log_call
def write_cross_section_shapefile(
    sections: Sequence[CrossSectionInfo],
    shp_path: Path,
    config: RasMapperConfig,
) -> Path:
    remove_shapefile_family(shp_path)
    writer = shapefile.Writer(str(shp_path.with_suffix("")), shapeType=shapefile.POLYLINE)
    writer.field("station", "C", size=24)
    writer.field("mean_z", "F", size=12, decimal=3)
    writer.field("seq", "N", size=8, decimal=0)
    writer.field("role", "C", size=16)

    for index, section in enumerate(sections, start=1):
        if index == 1:
            role = "upstream"
        elif index == len(sections):
            role = "downstream"
        else:
            role = "interior"
        writer.line([section.points])
        writer.record(section.station_label, section.mean_z, index, role)

    writer.close()
    _write_prj_for_shapefile(config, shp_path)
    return shp_path


@log_call
def write_centerline_shapefile(
    sections: Sequence[CrossSectionInfo],
    shp_path: Path,
    config: RasMapperConfig,
) -> Path:
    remove_shapefile_family(shp_path)
    writer = shapefile.Writer(str(shp_path.with_suffix("")), shapeType=shapefile.POLYLINE)
    writer.field("name", "C", size=40)
    writer.field("count", "N", size=8, decimal=0)
    writer.line([[section.mean_point for section in sections]])
    writer.record("cross_section_centerline", len(sections))
    writer.close()
    _write_prj_for_shapefile(config, shp_path)
    return shp_path


def _decode_hdf_bytes(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip()
    return str(value).strip()


@log_call
def parse_dss_catalog(dsc_h5_path: Path) -> pd.DataFrame:
    part_map = {
        "A": ("UniqueAs", 1),
        "B": ("UniqueBs", 0),
        "C": ("UniqueCs", 2),
        "D": ("UniqueDs", 4),
        "E": ("UniqueEs", 6),
        "F": ("UniqueFs", 7),
    }

    with h5py.File(dsc_h5_path, "r") as handle:
        unique_parts = {
            part: [_decode_hdf_bytes(v) for v in handle[dataset][()]]
            for part, (dataset, _) in part_map.items()
        }
        path_indices = handle["PathIndices"][()]

    rows: List[Dict[str, Any]] = []
    for row_index, index_row in enumerate(path_indices):
        row: Dict[str, Any] = {"catalog_row": int(row_index)}
        for part, (_, column_index) in part_map.items():
            value_index = int(index_row[column_index])
            row[part] = unique_parts[part][value_index] if value_index >= 0 else ""
        row["pathname"] = (
            f"//{row['A']}/{row['B']}/{row['C']}/"
            f"{row['D']}/{row['E']}/{row['F']}/"
        )
        rows.append(row)

    return pd.DataFrame(rows)


def choose_preferred_dss_path(
    catalog_df: pd.DataFrame,
    preferred_a_part: str,
    preferred_f_part: str,
) -> Tuple[Optional[str], pd.DataFrame]:
    catalog = catalog_df.copy()
    catalog["selected"] = False

    filtered = catalog[catalog["A"] == preferred_a_part]
    if preferred_f_part:
        preferred = filtered[filtered["F"] == preferred_f_part]
        if not preferred.empty:
            selected_index = preferred.index[0]
            catalog.loc[selected_index, "selected"] = True
            return str(catalog.loc[selected_index, "pathname"]), catalog

    if not filtered.empty:
        selected_index = filtered.index[0]
        catalog.loc[selected_index, "selected"] = True
        return str(catalog.loc[selected_index, "pathname"]), catalog

    if not catalog.empty:
        catalog.loc[catalog.index[0], "selected"] = True
        return str(catalog.loc[catalog.index[0], "pathname"]), catalog

    return None, catalog


def _register_map_layer(
    layer_name: str,
    layer_file: Path,
    layer_type: str,
    rasmap_path: Path,
    label_field: Optional[str] = None,
    symbology: Optional[Dict[str, Any]] = None,
) -> None:
    tree = ET.parse(rasmap_path)
    root = tree.getroot()
    map_layers = root.find("MapLayers")
    if map_layers is None:
        map_layers = ET.SubElement(root, "MapLayers")
        map_layers.set("Checked", "True")
        map_layers.set("Expanded", "True")

    for layer in list(map_layers.findall("Layer")):
        if layer.get("Name") == layer_name:
            map_layers.remove(layer)

    try:
        relative_path = layer_file.relative_to(rasmap_path.parent)
        filename = ".\\" + str(relative_path).replace("/", "\\")
    except ValueError:
        filename = str(layer_file)

    layer_elem = ET.SubElement(map_layers, "Layer")
    layer_elem.set("Name", layer_name)
    layer_elem.set("Type", layer_type)
    layer_elem.set("Checked", "True")
    layer_elem.set("Filename", filename)

    if layer_type in ("RasterLayer", "InterpolatedLayer", "FinalNValueLayer"):
        resample_elem = ET.SubElement(layer_elem, "ResampleMethod")
        resample_elem.text = "near"
        surface_elem = ET.SubElement(layer_elem, "Surface")
        surface_elem.set("On", "True")

    if label_field:
        label_elem = ET.SubElement(layer_elem, "LabelFeatures")
        label_elem.set("Checked", "True")
        label_elem.set("PercentPosition", "0")
        label_elem.set("rows", "1")
        label_elem.set("cols", "1")
        label_elem.set("r0c0", label_field)
        label_elem.set("Position", "0")
        label_elem.set("Color", "-16777216")
        label_elem.set("FontSize", "8.25")

    if symbology:
        sym_elem = ET.SubElement(layer_elem, "Symbology")
        if "line_color" in symbology:
            r, g, b, a = symbology["line_color"]
            pen_elem = ET.SubElement(sym_elem, "Pen")
            pen_elem.set("R", str(r))
            pen_elem.set("G", str(g))
            pen_elem.set("B", str(b))
            pen_elem.set("A", str(a))
            pen_elem.set("Dash", "0")
            pen_elem.set("Width", str(symbology.get("line_width", 2)))
        if "fill_color" in symbology:
            r, g, b, a = symbology["fill_color"]
            brush_elem = ET.SubElement(sym_elem, "Brush")
            brush_elem.set("Type", "SolidBrush")
            brush_elem.set("R", str(r))
            brush_elem.set("G", str(g))
            brush_elem.set("B", str(b))
            brush_elem.set("A", str(a))
            brush_elem.set("Name", "PolygonFill")

    tree.write(rasmap_path, encoding="utf-8", xml_declaration=False)


def _relative_rasmap_filename(rasmap_path: Path, layer_file: Path) -> str:
    try:
        relative_path = layer_file.relative_to(rasmap_path.parent)
        return ".\\" + str(relative_path).replace("/", "\\")
    except ValueError:
        return str(layer_file)


def _remove_map_layer(rasmap_path: Path, layer_name: str) -> bool:
    tree = ET.parse(rasmap_path)
    root = tree.getroot()
    map_layers = root.find("MapLayers")
    if map_layers is None:
        return False

    removed = False
    for layer in list(map_layers.findall("Layer")):
        if layer.get("Name") == layer_name:
            map_layers.remove(layer)
            removed = True

    if removed:
        tree.write(rasmap_path, encoding="utf-8", xml_declaration=False)
    return removed


def _cleanup_stale_landcover_layers(rasmap_path: Path) -> List[Dict[str, str]]:
    tree = ET.parse(rasmap_path)
    root = tree.getroot()
    map_layers = root.find("MapLayers")
    if map_layers is None:
        return []

    removed: List[Dict[str, str]] = []
    for layer in list(map_layers.findall("Layer")):
        name = layer.get("Name", "")
        filename = layer.get("Filename", "")
        if name == "LC_corine" or "LC_corine.hdf" in filename:
            removed.append({"name": name, "filename": filename})
            map_layers.remove(layer)

    if removed:
        tree.write(rasmap_path, encoding="utf-8", xml_declaration=False)
    return removed


def _remove_helper_map_layers(rasmap_path: Path) -> List[str]:
    removed: List[str] = []
    for layer_name in HELPER_MAP_LAYER_NAMES:
        if _remove_map_layer(rasmap_path, layer_name):
            removed.append(layer_name)
    return removed


def _write_prj_from_raster_or_projection(
    raster_path: Path,
    prj_path: Path,
    fallback_prj: Path,
) -> None:
    if prj_path.exists():
        return

    wkt = ""
    try:
        with rasterio.open(raster_path) as src:
            if src.crs:
                wkt = src.crs.to_wkt()
    except Exception:
        wkt = ""

    if not wkt and fallback_prj.exists():
        wkt = fallback_prj.read_text(encoding="utf-8", errors="ignore").strip()

    if wkt:
        prj_path.write_text(wkt, encoding="utf-8")


@log_call
def install_existing_landcover_layer(config: RasMapperConfig) -> Optional[Dict[str, Any]]:
    if not config.has_existing_landcover_layer:
        return None

    copy_file(
        config.existing_landcover_tif_src,
        config.existing_landcover_tif_copy,
    )
    copy_file(
        config.existing_landcover_hdf_src,
        config.existing_landcover_hdf_copy,
    )
    if (
        config.existing_landcover_prj_src is not None
        and config.existing_landcover_prj_src.exists()
    ):
        copy_file(
            config.existing_landcover_prj_src,
            config.existing_landcover_prj_copy,
        )
    else:
        _write_prj_from_raster_or_projection(
            config.existing_landcover_tif_copy,
            config.existing_landcover_prj_copy,
            config.projection_copy,
        )

    with rasterio.open(config.existing_landcover_tif_copy) as src:
        transform = src.transform
        crs_text = src.crs.to_string() if src.crs else None
        cell_size_x = abs(float(transform.a))
        cell_size_y = abs(float(transform.e))

    return {
        "type": "existing_ras_landcover",
        "landcover_tif": config.existing_landcover_tif_copy,
        "landcover_hdf": config.existing_landcover_hdf_copy,
        "cell_size_x": cell_size_x,
        "cell_size_y": cell_size_y,
        "crs": crs_text,
    }


def _sync_landcover_map_layers(config: RasMapperConfig) -> None:
    _remove_helper_map_layers(config.rasmap_path)

    for layer_name in ("LandCover", "LandCover Raster"):
        _remove_map_layer(config.rasmap_path, layer_name)

    if config.has_existing_landcover_layer:
        _register_map_layer(
            "LandCover",
            config.active_landcover_map_hdf,
            "LandCoverLayer",
            rasmap_path=config.rasmap_path,
        )
    else:
        _register_map_layer(
            "LandCover",
            config.landcover_raster,
            "InterpolatedLayer",
            rasmap_path=config.rasmap_path,
        )


def _ensure_geometry_landcover_association(config: RasMapperConfig) -> bool:
    if not config.rasmap_path.exists():
        return False

    tree = ET.parse(config.rasmap_path)
    root = tree.getroot()
    geometries = root.find("Geometries")
    if geometries is None:
        return False

    target_filename = _relative_rasmap_filename(
        config.rasmap_path,
        config.geom_hdf_path,
    )
    target_layer = None
    for layer in geometries.findall("Layer"):
        if layer.get("Type") != "RASGeometry":
            continue
        filename = layer.get("Filename", "")
        name = layer.get("Name", "")
        if filename == target_filename or name == config.geometry_title:
            target_layer = layer
            break

    if target_layer is None:
        return False

    changed = False

    def _find_child(
        *, layer_type: Optional[str] = None, layer_name: Optional[str] = None
    ) -> Optional[ET.Element]:
        for child in target_layer.findall("Layer"):
            if layer_type is not None and child.get("Type") != layer_type:
                continue
            if layer_name is not None and child.get("Name") != layer_name:
                continue
            return child
        return None

    landcover_regions = _find_child(layer_type="RasLandCoverRegions")
    if landcover_regions is None:
        landcover_regions = ET.SubElement(target_layer, "Layer")
        landcover_regions.set("Type", "RasLandCoverRegions")
        landcover_regions.set("Checked", "True")
        changed = True
    else:
        if landcover_regions.get("Checked") != "True":
            landcover_regions.set("Checked", "True")
            changed = True

    final_n = _find_child(layer_type="FinalNValueLayer")
    if final_n is None:
        final_n = ET.SubElement(target_layer, "Layer")
        final_n.set("Type", "FinalNValueLayer")
        final_n.set("Checked", "True")
        ET.SubElement(final_n, "ResampleMethod").text = "near"
        surface = ET.SubElement(final_n, "Surface")
        surface.set("On", "True")
        changed = True
    else:
        if final_n.get("Checked") != "True":
            final_n.set("Checked", "True")
            changed = True
        if final_n.find("ResampleMethod") is None:
            ET.SubElement(final_n, "ResampleMethod").text = "near"
            changed = True
        if final_n.find("Surface") is None:
            surface = ET.SubElement(final_n, "Surface")
            surface.set("On", "True")
            changed = True

    mannings_group = _find_child(
        layer_type="InterpretationOverrideGroupLayer",
        layer_name="Manning's n",
    )
    if mannings_group is None:
        mannings_group = ET.SubElement(target_layer, "Layer")
        mannings_group.set("Name", "Manning's n")
        mannings_group.set("Type", "InterpretationOverrideGroupLayer")
        mannings_group.set("Checked", "True")
        mannings_group.set("Expanded", "True")
        changed = True
    else:
        if mannings_group.get("Checked") != "True":
            mannings_group.set("Checked", "True")
            changed = True
        if mannings_group.get("Expanded") != "True":
            mannings_group.set("Expanded", "True")
            changed = True

    if changed:
        tree.write(config.rasmap_path, encoding="utf-8", xml_declaration=False)
    return changed


def _load_landcover_table(shp_path: Path) -> pd.DataFrame:
    try:
        reader = shapefile.Reader(str(shp_path), encoding="utf-8")
    except LookupError:
        reader = shapefile.Reader(str(shp_path))

    with reader:
        field_names = [field[0] for field in reader.fields[1:]]
        rows: List[Dict[str, Any]] = []
        for record in reader.records():
            rows.append(dict(zip(field_names, record)))

    frame = pd.DataFrame(rows)
    required = {"KodText", "Adi", "Manningn"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(
            f"Landcover shapefile missing attributes: {sorted(missing)}"
        )

    frame = frame[["KodText", "Adi", "Manningn"]].copy()
    frame["KodText"] = frame["KodText"].map(_normalize_code)
    frame["Adi"] = frame["Adi"].astype(str)
    frame["Manningn"] = frame["Manningn"].astype(float)
    frame = frame.drop_duplicates(subset=["KodText"]).sort_values("KodText")
    return frame.reset_index(drop=True)


@log_call
def create_landcover_raster(config: RasMapperConfig) -> Path:
    config.landcover_raster.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(config.dtm_src) as src:
        bounds = src.bounds

    if config.landcover_raster.exists():
        try:
            config.landcover_raster.unlink()
        except PermissionError:
            shutil.copy2(
                config.projection_copy,
                config.landcover_raster.with_suffix(".prj"),
            )
            LOGGER.warning(
                "Landcover raster is locked; reusing existing raster: %s",
                config.landcover_raster,
            )
            return config.landcover_raster

    command = [
        str(config.gdal_rasterize_exe),
        "-a",
        "KodText",
        "-ot",
        "UInt16",
        "-a_nodata",
        "0",
        "-tr",
        str(config.landcover_cell_size),
        str(config.landcover_cell_size),
        "-tap",
        "-te",
        str(bounds.left),
        str(bounds.bottom),
        str(bounds.right),
        str(bounds.top),
        "-of",
        "GTiff",
        "-l",
        config.landcover_copy.stem,
        str(config.landcover_copy),
        str(config.landcover_raster),
    ]

    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "gdal_rasterize failed:\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )

    shutil.copy2(
        config.projection_copy,
        config.landcover_raster.with_suffix(".prj"),
    )

    LOGGER.info("Created landcover raster: %s", config.landcover_raster)
    return config.landcover_raster


@log_call
def create_mannings_raster(
    config: RasMapperConfig,
    lookup_df: pd.DataFrame,
) -> Path:
    config.landcover_mannings_raster.parent.mkdir(parents=True, exist_ok=True)

    with rasterio.open(config.landcover_raster) as src:
        data = src.read(1)
        profile = src.profile.copy()

    value_by_code = {
        int(_normalize_code(row["KodText"])): float(row["Manningn"])
        for _, row in lookup_df.iterrows()
    }
    nodata_value = -9999.0
    mannings = np.full(data.shape, nodata_value, dtype=np.float32)
    for code, value in value_by_code.items():
        mannings[data == code] = value
    mannings[data == 0] = config.landcover_nodata_manning

    profile.update(dtype="float32", count=1, nodata=nodata_value)
    if config.landcover_mannings_raster.exists():
        try:
            config.landcover_mannings_raster.unlink()
        except PermissionError:
            LOGGER.warning(
                "Manning raster is locked; reusing existing raster: %s",
                config.landcover_mannings_raster,
            )
            return config.landcover_mannings_raster
    with rasterio.open(config.landcover_mannings_raster, "w", **profile) as dst:
        dst.write(mannings, 1)

    LOGGER.info(
        "Created Manning raster from landcover classes: %s",
        config.landcover_mannings_raster,
    )
    return config.landcover_mannings_raster


def _bytes_dtype(length: int) -> np.dtype:
    return np.dtype(f"S{length}")


def _read_projection_wkt(prj_path: Path) -> str:
    if not prj_path.exists():
        return ""
    return prj_path.read_text(encoding="utf-8", errors="ignore").strip()


@log_call
def create_landcover_layer_hdf(
    config: RasMapperConfig,
    lookup_df: pd.DataFrame,
) -> Path:
    config.landcover_hdf.parent.mkdir(parents=True, exist_ok=True)
    legacy_hdf = config.land_classification_dir / "LandCover.hdf"
    if legacy_hdf != config.landcover_hdf and legacy_hdf.exists():
        legacy_backup = config.land_classification_dir / "LandCover.hdf.legacy.bak"
        if legacy_backup.exists():
            legacy_backup.unlink()
        legacy_hdf.replace(legacy_backup)
    if config.landcover_hdf.exists():
        config.landcover_hdf.unlink()

    all_rows = [("NoData", 0)]
    for _, row in lookup_df.iterrows():
        all_rows.append((str(row["KodText"]).strip(), int(_normalize_code(row["KodText"]))))

    max_name_len = max(6, min(64, max(len(name) for name, _ in all_rows)))

    raster_map_dtype = np.dtype(
        [("ID", "<i4"), ("Name", _bytes_dtype(max_name_len))]
    )
    variables_dtype = np.dtype(
        [
            ("Name", _bytes_dtype(max_name_len)),
            ("ManningsN", "<f4"),
            ("Percent Impervious", "<f4"),
        ]
    )

    raster_map = np.zeros(len(all_rows), dtype=raster_map_dtype)
    variables = np.zeros(len(all_rows), dtype=variables_dtype)
    mannings_lookup = {
        str(row["KodText"]).strip(): float(row["Manningn"])
        for _, row in lookup_df.iterrows()
    }

    for idx, (name, code) in enumerate(all_rows):
        name_bytes = name.encode("utf-8", errors="ignore")[:max_name_len]
        raster_map[idx]["ID"] = int(code)
        raster_map[idx]["Name"] = name_bytes
        variables[idx]["Name"] = name_bytes
        if int(code) == 0:
            variables[idx]["ManningsN"] = -9999.0
            variables[idx]["Percent Impervious"] = -9999.0
        else:
            variables[idx]["ManningsN"] = float(mannings_lookup.get(name, 0.03))
            variables[idx]["Percent Impervious"] = -9999.0

    with h5py.File(config.landcover_hdf, "w") as handle:
        handle.create_dataset("Raster Map", data=raster_map)
        handle.create_dataset("Variables", data=variables)

        def _set_fixed_bytes_attr(key: str, value: str) -> None:
            raw = (value or "").encode("utf-8", errors="ignore")
            dtype = f"S{max(1, len(raw))}"
            handle.attrs.create(key, np.array(raw, dtype=dtype))

        _set_fixed_bytes_attr("File Type", "HEC Land Cover")
        _set_fixed_bytes_attr("LC Type", "LandCover")
        _set_fixed_bytes_attr("Version", "2.0")
        _set_fixed_bytes_attr(
            "Projection",
            _read_projection_wkt(config.projection_copy),
        )
        _set_fixed_bytes_attr("GUID", str(uuid.uuid4()))

    LOGGER.info("Created landcover layer HDF: %s", config.landcover_hdf)
    return config.landcover_hdf


@log_call
def create_boundary_artifacts(
    config: RasMapperConfig,
    sections: Sequence[CrossSectionInfo],
    perimeter_ring: Sequence[Tuple[float, float]],
    preferred_dss_path: Optional[str],
) -> pd.DataFrame:
    centroid = _polygon_centroid(perimeter_ring)
    downstream_slope = estimate_normal_depth_slope(sections)
    storage_area_name = resolve_storage_area_name(config)
    boundary_lines = build_boundary_lines_from_sources(
        sections,
        perimeter_ring,
        boundary_offset_distance=config.boundary_offset_distance,
        storage_area_name=storage_area_name,
    )
    line_by_role = {
        "upstream": boundary_lines[0],
        "downstream": boundary_lines[1],
    }

    boundary_rows: List[Dict[str, Any]] = []
    for role, section in (
        ("upstream", sections[0]),
        ("downstream", sections[-1]),
    ):
        interior_side = _point_side_of_line(centroid, section.points)
        boundary_rows.append(
            {
                "bc_name": f"{config.flow_area_name}_{role}",
                "role": role,
                "station": section.station_label,
                "mean_z": round(section.mean_z, 3),
                "interior_side": interior_side,
                "reverse_if_interior_should_be_right": interior_side == "left",
                "reverse_if_interior_should_be_left": interior_side == "right",
                "method": "Flow Hydrograph"
                if role == "upstream"
                else config.downstream_bc_method,
                "normal_depth_slope": downstream_slope
                if role == "downstream"
                else None,
                "dss_path": preferred_dss_path if role == "upstream" else None,
                "offset_distance": config.boundary_offset_distance,
                "storage_area_name": storage_area_name,
            }
        )

    remove_shapefile_family(config.boundary_shp)
    writer = shapefile.Writer(
        str(config.boundary_shp.with_suffix("")),
        shapeType=shapefile.POLYLINE,
    )
    writer.field("bc_name", "C", size=40)
    writer.field("role", "C", size=16)
    writer.field("station", "C", size=24)
    writer.field("mean_z", "F", size=12, decimal=3)
    writer.field("inside", "C", size=8)
    writer.field("rev_r", "N", size=1, decimal=0)
    writer.field("rev_l", "N", size=1, decimal=0)
    writer.field("method", "C", size=24)
    writer.field("slope", "F", size=12, decimal=6)
    writer.field("dss_path", "C", size=200)

    for row in boundary_rows:
        coords = line_by_role[row["role"]]["coords"]
        writer.line([coords])
        writer.record(
            row["bc_name"],
            row["role"],
            row["station"],
            row["mean_z"],
            row["interior_side"],
            1 if row["reverse_if_interior_should_be_right"] else 0,
            1 if row["reverse_if_interior_should_be_left"] else 0,
            row["method"],
            row["normal_depth_slope"] or 0.0,
            (row["dss_path"] or "")[:200],
        )
    writer.close()
    _write_prj_for_shapefile(config, config.boundary_shp)

    boundary_df = pd.DataFrame(boundary_rows)
    boundary_df.to_csv(config.boundary_csv, index=False)
    config.boundary_json.write_text(
        json.dumps(_to_jsonable(boundary_rows), indent=2),
        encoding="utf-8",
    )
    return boundary_df


def generate_overview_plot(
    config: RasMapperConfig,
    perimeter_ring: Sequence[Tuple[float, float]],
    breaklines: Sequence[Sequence[Tuple[float, float]]],
    sections: Sequence[CrossSectionInfo],
) -> Optional[Path]:
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        LOGGER.warning("matplotlib not available; skipping overview plot")
        return None

    fig, ax = plt.subplots(figsize=(10, 8))

    px = [point[0] for point in perimeter_ring]
    py = [point[1] for point in perimeter_ring]
    ax.plot(px, py, color="black", linewidth=1.8, label="Perimeter Source")

    for index, breakline in enumerate(breaklines):
        bx = [point[0] for point in breakline]
        by = [point[1] for point in breakline]
        ax.plot(
            bx,
            by,
            color="#d95f02",
            linewidth=1.5,
            label="Breakline Source" if index == 0 else None,
        )

    for index, section in enumerate(sections):
        xs = [point[0] for point in section.points]
        ys = [point[1] for point in section.points]
        ax.plot(
            xs,
            ys,
            color="#1b9e77",
            alpha=0.8,
            linewidth=1.0,
            label="Cross Sections" if index == 0 else None,
        )
        ax.text(
            section.mean_point[0],
            section.mean_point[1],
            section.station_label,
            fontsize=8,
            color="#0b3d2e",
        )

    centerline = [section.mean_point for section in sections]
    ax.plot(
        [pt[0] for pt in centerline],
        [pt[1] for pt in centerline],
        color="#7570b3",
        linewidth=2.0,
        label="Derived Centerline",
    )

    ax.set_title("RAS Mapper Inputs and Derived Boundary Guides")
    ax.set_xlabel("X")
    ax.set_ylabel("Y")
    ax.axis("equal")
    ax.legend(loc="best")
    ax.grid(True, alpha=0.25)

    config.spatial_overview_png.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(config.spatial_overview_png, dpi=200)
    plt.close(fig)
    return config.spatial_overview_png


def write_manual_checklist(
    config: RasMapperConfig,
    preferred_dss_path: Optional[str],
    downstream_slope: float,
) -> None:
    if config.active_landcover_map_hdf is not None:
        active_landcover_lines = (
            f"- Existing native RAS landcover TIFF: "
            f"`{config.active_landcover_map_tif}`\n"
            f"- Existing native RAS landcover HDF: "
            f"`{config.active_landcover_map_hdf}`"
        )
        landcover_step = (
            f"15. Confirm the `LandCover` map layer points to\n"
            f"    `{config.active_landcover_map_hdf}` and uses\n"
            f"    `{config.active_landcover_map_tif}`."
        )
    else:
        active_landcover_lines = (
            "- Existing native RAS landcover TIFF: `None`\n"
            "- Existing native RAS landcover HDF: `None`"
        )
        landcover_step = (
            f"15. Confirm the `LandCover` map layer points to the generated\n"
            f"    raster `{config.landcover_raster}`."
        )

    checklist = textwrap.dedent(
        f"""\
        # RAS Mapper Manual Checklist

        Project: `{config.project_name}`

        This script prepared the project shell, terrain, helper GIS layers,
        cross-section analysis, DSS catalog report, landcover products, and
        Manning lookup tables. The remaining items below still require the
        RAS Mapper GUI because ras_commander does not expose a stable file
        API for those dialogs.

        ## Inputs prepared by script

        - Projection: `{config.projection_copy}`
        - Terrain source DTM: `{config.dtm_src}`
        - Terrain HDF target: `{config.terrain_hdf}`
        - Perimeter source: `{config.perimeter_copy}`
        - Breakline source: `{config.breakline_copy}`
        - Landcover source polygon: `{config.landcover_copy}`
        - Landcover 2 m raster from `KodText`: `{config.landcover_raster}`
        - Generated Manning-table HDF: `{config.landcover_hdf}`
        {active_landcover_lines}
        - Final Manning raster at 2 m: `{config.landcover_mannings_raster}`
        - Manning lookup from `Manningn`: `{config.landcover_lookup_csv}`
        - Cross-section helper lines: `{config.cross_sections_shp}`
        - Boundary helper lines: `{config.boundary_shp}`
        - DSS catalog report: `{config.dss_catalog_csv}`
        - Boundary recommendations: `{config.boundary_csv}`

        ## RAS Mapper steps

        1. Open project `{config.project_file}` and open RAS Mapper.
        2. Create a new geometry for 2D and name the 2D flow area
           `{config.flow_area_name}`.
        3. Verify the project projection is `{config.projection_copy}`.
        4. Confirm the terrain layer `{config.terrain_layer_name}` is present.
        5. Create or import the 2D perimeter from `{config.perimeter_copy}`.
        6. Generate computation points with cell size
           `{config.mesh_cell_size:.1f} m x {config.mesh_cell_size:.1f} m`.
        7. Import the breaklines from `{config.breakline_copy}`.
        8. Edit breakline properties:
           near spacing = `{config.breakline_near_spacing:.1f} m`,
           near repeats = `{config.breakline_near_repeats}`.
        9. Enforce all breaklines.
        10. Run the mesh error repair / fix-all workflow in the perimeter editor.
        11. Add upstream and downstream boundary conditions using the helper
            lines in `{config.boundary_shp}`.
            The helper lines are already offset `{config.boundary_offset_distance:.1f} m`
            outside the perimeter.
        12. Upstream recommended DSS pathname:
            `{preferred_dss_path or "No preferred path found - inspect dss_catalog.csv"}`
        13. Downstream recommended method:
            `{config.downstream_bc_method}` with slope `{downstream_slope:.6f}`.
        14. Review boundary line orientation. The helper CSV reports whether the
            2D area falls on the line's left or right side and includes reverse
            flags for either interior-right or interior-left conventions.
        {landcover_step}
        16. Confirm the geometry `Manning's n` / `Final n Value` layers render.
        17. If you edit Manning values in the GUI, re-run:
            `python working/rasmapper.py apply-mannings`
        18. Then run:
            `python working/rasmapper.py check-mannings`

        ## Notes

        - The helper files are review artifacts, not hidden state.
        - Re-running `prepare` updates helper files without deleting your
          existing project geometry or plan files.
        """
    )
    config.checklist_md.write_text(checklist, encoding="utf-8")


def _upsert_project_line(
    lines: List[str],
    prefix: str,
    value: str,
    insert_after_prefixes: Optional[Sequence[str]] = None,
) -> List[str]:
    replacement = f"{prefix}{value}\n"
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            lines[index] = replacement
            return lines

    insert_at = len(lines)
    if insert_after_prefixes:
        for index, line in enumerate(lines):
            if any(line.startswith(candidate) for candidate in insert_after_prefixes):
                insert_at = index + 1
    lines.insert(insert_at, replacement)
    return lines


def ensure_project_has_geom_reference(config: RasMapperConfig) -> None:
    lines = config.project_file.read_text(encoding="utf-8").splitlines(True)
    lines = _upsert_project_line(
        lines,
        "Geom File=",
        "g01",
        insert_after_prefixes=(
            "English Units=",
            "SI Units=",
            "Default Exp/Contr=",
        ),
    )
    config.project_file.write_text("".join(lines), encoding="utf-8")


def set_geometry_title(geom_path: Path, title: str) -> None:
    lines = geom_path.read_text(encoding="utf-8", errors="replace").splitlines(True)
    lines = _upsert_project_line(lines, "Geom Title=", title)
    geom_path.write_text("".join(lines), encoding="utf-8")


def seed_geometry_from_reference(config: RasMapperConfig) -> None:
    copy_file(config.reference_geom_src, config.geom_path)
    set_geometry_title(config.geom_path, config.geometry_title)
    ensure_project_has_geom_reference(config)


def build_expected_region_mannings(
    region_df: pd.DataFrame,
    lookup_df: pd.DataFrame,
) -> pd.DataFrame:
    lookup_by_code = {
        str(row["KodText"]).upper(): float(row["Manningn"])
        for _, row in lookup_df.iterrows()
    }

    updated = region_df.copy()
    updated["MainChannel"] = updated["MainChannel"].astype(float)

    for index, row in updated.iterrows():
        key = _normalize_code(row["Land Cover Name"]).upper()
        if key in lookup_by_code:
            updated.at[index, "MainChannel"] = lookup_by_code[key]

    return updated


def update_hdf_region_mannings(
    geom_hdf_path: Path,
    region_df: pd.DataFrame,
) -> None:
    calibration_path = "Geometry/Land Cover (Manning's n)/Calibration Table"
    if not geom_hdf_path.exists():
        return

    value_by_name = {
        str(row["Land Cover Name"]).strip(): float(row["MainChannel"])
        for _, row in region_df.iterrows()
    }

    with h5py.File(geom_hdf_path, "r+") as handle:
        if calibration_path not in handle:
            return

        dataset = handle[calibration_path]
        values = dataset[:]

        region_field = None
        for candidate in ("Manning's Region 1", "MainChannel"):
            if candidate in values.dtype.names:
                region_field = candidate
                break
        if region_field is None:
            raise ValueError(
                f"Could not find regional Manning field in {calibration_path}"
            )

        for index in range(len(values)):
            raw_name = values[index]["Land Cover Name"]
            name = (
                raw_name.decode("utf-8", errors="replace").strip()
                if isinstance(raw_name, bytes)
                else str(raw_name).strip()
            )
            if name in value_by_name:
                values[index][region_field] = value_by_name[name]

        dataset[...] = values


def build_exact_region_mannings_from_lookup(
    geom_path: Path,
    lookup_df: pd.DataFrame,
    nodata_value: float,
) -> pd.DataFrame:
    existing = GeomLandCover.get_region_mannings_n(geom_path)
    if not existing.empty:
        region_name = str(existing["Region Name"].iloc[0])
    else:
        region_name = "Manning's Region 1"

    rows = [
        {
            "Table Number": str(len(lookup_df) + 1),
            "Land Cover Name": "NoData",
            "MainChannel": nodata_value,
            "Region Name": region_name,
        }
    ]
    for _, row in lookup_df.sort_values("KodText").iterrows():
        rows.append(
            {
                "Table Number": str(len(lookup_df) + 1),
                "Land Cover Name": str(row["KodText"]).strip(),
                "MainChannel": float(row["Manningn"]),
                "Region Name": region_name,
            }
        )
    return pd.DataFrame(rows)


def set_region_mannings_exact(
    geom_path: Path,
    region_df: pd.DataFrame,
) -> None:
    lines = geom_path.read_text(encoding="utf-8", errors="replace").splitlines(True)

    region_name = str(region_df["Region Name"].iloc[0])
    table_value = str(region_df["Table Number"].iloc[0])
    region_start = None
    polygon_idx = None

    for idx, line in enumerate(lines):
        if line.strip() == f"LCMann Region Name={region_name}":
            region_start = idx
            continue
        if region_start is not None and line.strip().startswith("LCMann Region Polygon="):
            polygon_idx = idx
            break

    if region_start is None or polygon_idx is None:
        raise ValueError(
            f"Could not locate Manning region block in geometry: {geom_path}"
        )

    new_region_lines = [
        f"LCMann Region Name={region_name}\n",
        f"LCMann Region Table={table_value}\n",
    ]
    for _, row in region_df.iterrows():
        new_region_lines.append(
            f"{row['Land Cover Name']},{float(row['MainChannel'])}\n"
        )

    updated = lines[:region_start] + new_region_lines + lines[polygon_idx:]
    current_time = time.strftime("%b/%d/%Y %H:%M:%S")
    for idx, line in enumerate(updated):
        if line.strip().startswith("LCMann Region Time="):
            updated[idx] = f"LCMann Region Time={current_time}\n"
            break
    geom_path.write_text("".join(updated), encoding="utf-8")


def update_hdf_region_mannings_exact(
    geom_hdf_path: Path,
    region_df: pd.DataFrame,
) -> None:
    calibration_path = "Geometry/Land Cover (Manning's n)/Calibration Table"
    if not geom_hdf_path.exists():
        return

    with h5py.File(geom_hdf_path, "r+") as handle:
        if calibration_path not in handle:
            return

        dataset = handle[calibration_path]
        dtype = dataset.dtype
        attrs = dict(dataset.attrs)

        name_field = "Land Cover Name"
        base_field = "Base Manning's n Value"
        region_field = next(
            field
            for field in dtype.names
            if field not in (name_field, base_field)
        )

        rows = np.zeros(len(region_df) + 1, dtype=dtype)
        rows[0][name_field] = b""
        rows[0][base_field] = np.nan
        rows[0][region_field] = np.nan

        for idx, (_, row) in enumerate(region_df.iterrows(), start=1):
            rows[idx][name_field] = str(row["Land Cover Name"]).encode("utf-8")
            rows[idx][base_field] = np.nan
            rows[idx][region_field] = float(row["MainChannel"])

        parent = handle["Geometry/Land Cover (Manning's n)"]
        del parent["Calibration Table"]
        new_ds = parent.create_dataset("Calibration Table", data=rows)
        for key, value in attrs.items():
            new_ds.attrs[key] = value


def sync_landcover_geometry(
    config: RasMapperConfig,
    lookup_df: pd.DataFrame,
) -> pd.DataFrame:
    region_df = build_exact_region_mannings_from_lookup(
        config.geom_path,
        lookup_df,
        nodata_value=config.landcover_nodata_manning,
    )
    set_region_mannings_exact(config.geom_path, region_df)
    update_hdf_region_mannings_exact(config.geom_hdf_path, region_df)
    return region_df


def _fixed_width_xy_line(points: Sequence[Tuple[float, float]]) -> str:
    return "".join(f"{x:16.8f}{y:16.8f}" for x, y in points).rstrip() + "\n"


def _chunk_points(
    points: Sequence[Tuple[float, float]],
    chunk_size: int,
) -> List[List[Tuple[float, float]]]:
    return [
        list(points[index:index + chunk_size])
        for index in range(0, len(points), chunk_size)
    ]


def _point_on_segment(
    point: Tuple[float, float],
    start: Tuple[float, float],
    end: Tuple[float, float],
    tol: float = 1e-6,
) -> bool:
    px, py = point
    ax, ay = start
    bx, by = end
    cross = (px - ax) * (by - ay) - (py - ay) * (bx - ax)
    if abs(cross) > tol:
        return False
    dot = (px - ax) * (bx - ax) + (py - ay) * (by - ay)
    if dot < -tol:
        return False
    seg_len_sq = (bx - ax) ** 2 + (by - ay) ** 2
    if dot - seg_len_sq > tol:
        return False
    return True


def _point_in_ring(
    point: Tuple[float, float],
    ring: Sequence[Tuple[float, float]],
) -> bool:
    px, py = point
    inside = False
    for index in range(len(ring) - 1):
        start = ring[index]
        end = ring[index + 1]
        if _point_on_segment(point, start, end):
            return True
        x1, y1 = start
        x2, y2 = end
        intersects = ((y1 > py) != (y2 > py))
        if not intersects:
            continue
        x_at_y = x1 + (py - y1) * (x2 - x1) / (y2 - y1)
        if px < x_at_y:
            inside = not inside
    return inside


def _aligned_grid_values(
    start: float,
    stop: float,
    spacing: float,
    descending: bool = False,
) -> List[float]:
    if spacing <= 0:
        raise ValueError("Grid spacing must be positive")
    first = math.floor(start / spacing) * spacing + spacing / 2.0
    while first < start - 1e-9:
        first += spacing
    values: List[float] = []
    current = first
    while current <= stop + 1e-9:
        values.append(current)
        current += spacing
    if descending:
        values.reverse()
    return values


def _append_unique_point(
    points: List[Tuple[float, float]],
    seen: set[Tuple[int, int]],
    point: Tuple[float, float],
    precision: float = 1000.0,
) -> None:
    key = (
        int(round(point[0] * precision)),
        int(round(point[1] * precision)),
    )
    if key in seen:
        return
    seen.add(key)
    points.append(point)


def _sample_polyline_points(
    coords: Sequence[Tuple[float, float]],
    spacing: float,
) -> List[Tuple[float, float]]:
    if spacing <= 0:
        raise ValueError("Polyline spacing must be positive")
    if len(coords) < 2:
        return list(coords)

    sampled = [coords[0]]
    for start, end in zip(coords[:-1], coords[1:]):
        ax, ay = start
        bx, by = end
        seg_len = math.hypot(bx - ax, by - ay)
        if seg_len == 0:
            continue
        n_steps = max(1, int(math.ceil(seg_len / spacing)))
        for step in range(1, n_steps + 1):
            t = min(1.0, step * spacing / seg_len)
            sampled.append((ax + (bx - ax) * t, ay + (by - ay) * t))
    return sampled


def _generate_breakline_seed_points(
    ring: Sequence[Tuple[float, float]],
    breaklines: Sequence[Sequence[Tuple[float, float]]],
    near_spacing: float,
    near_repeats: int,
) -> List[Tuple[float, float]]:
    seed_points: List[Tuple[float, float]] = []
    seen: set[Tuple[int, int]] = set()

    for breakline in breaklines:
        for start, end in zip(breakline[:-1], breakline[1:]):
            ax, ay = start
            bx, by = end
            dx = bx - ax
            dy = by - ay
            seg_len = math.hypot(dx, dy)
            if seg_len == 0:
                continue

            nx = -dy / seg_len
            ny = dx / seg_len
            base_points = _sample_polyline_points([start, end], near_spacing)

            for point in base_points:
                px, py = point
                for repeat in range(0, near_repeats + 1):
                    offset = repeat * near_spacing
                    candidates = [
                        (px, py) if repeat == 0 else None,
                        (px + nx * offset, py + ny * offset),
                        (px - nx * offset, py - ny * offset),
                    ]
                    for candidate in candidates:
                        if candidate is None:
                            continue
                        if _point_in_ring(candidate, ring):
                            _append_unique_point(seed_points, seen, candidate)
    return seed_points


def _generate_computation_points(
    ring: Sequence[Tuple[float, float]],
    spacing: float,
    breaklines: Optional[Sequence[Sequence[Tuple[float, float]]]] = None,
    breakline_near_spacing: Optional[float] = None,
    breakline_near_repeats: Optional[int] = None,
) -> List[Tuple[float, float]]:
    xmin, xmax, ymin, ymax = _ring_bounds(ring)
    xs = _aligned_grid_values(xmin, xmax, spacing)
    ys = _aligned_grid_values(ymin, ymax, spacing, descending=True)
    points: List[Tuple[float, float]] = []
    seen: set[Tuple[int, int]] = set()
    for y in ys:
        for x in xs:
            point = (x, y)
            if _point_in_ring(point, ring):
                _append_unique_point(points, seen, point)

    if breaklines and breakline_near_spacing and breakline_near_repeats:
        for point in _generate_breakline_seed_points(
            ring,
            breaklines,
            breakline_near_spacing,
            breakline_near_repeats,
        ):
            _append_unique_point(points, seen, point)

    if not points:
        raise ValueError(
            "Generated zero computation points from the perimeter ring"
        )
    return points


def _replace_line_block_by_count(
    lines: List[str],
    prefix: str,
    replacement_lines: List[str],
) -> List[str]:
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            try:
                count = int(line.split("=")[1].strip().split()[0])
            except ValueError:
                count = 0
            start = index + 1
            end = start + count
            return lines[:index] + replacement_lines + lines[end:]
    raise ValueError(f"Could not find block starting with '{prefix}'")


def _replace_block_between_prefixes(
    lines: List[str],
    start_prefix: str,
    end_prefix: str,
    replacement_lines: List[str],
) -> List[str]:
    start = None
    end = None
    for index, line in enumerate(lines):
        if start is None and line.startswith(start_prefix):
            start = index
        elif start is not None and line.startswith(end_prefix):
            end = index
            break
    if start is None or end is None:
        raise ValueError(
            f"Could not replace block '{start_prefix}' -> '{end_prefix}'"
        )
    return lines[:start] + replacement_lines + lines[end:]


def _current_timestamp_compact() -> str:
    return time.strftime("%d%b%Y %H:%M:%S")


def _ring_bounds(ring: Sequence[Tuple[float, float]]) -> Tuple[float, float, float, float]:
    xs = [point[0] for point in ring]
    ys = [point[1] for point in ring]
    return min(xs), max(xs), min(ys), max(ys)


def _update_viewing_rectangle(
    lines: List[str],
    ring: Sequence[Tuple[float, float]],
    pad_fraction: float = 0.05,
) -> List[str]:
    xmin, xmax, ymin, ymax = _ring_bounds(ring)
    dx = max(xmax - xmin, 1.0)
    dy = max(ymax - ymin, 1.0)
    px = dx * pad_fraction
    py = dy * pad_fraction
    value = f" {xmin - px} , {xmax + px} , {ymax + py} , {ymin - py} "
    return _upsert_project_line(lines, "Viewing Rectangle=", value)


def _ensure_closed_ring(
    ring: Sequence[Tuple[float, float]],
) -> List[Tuple[float, float]]:
    pts = list(ring)
    if not pts:
        raise ValueError("Perimeter ring is empty")
    if pts[0] != pts[-1]:
        pts.append(pts[0])
    return pts


def _load_boundary_lines_from_shapefile(
    shp_path: Path,
) -> List[Dict[str, Any]]:
    with shapefile.Reader(str(shp_path)) as reader:
        field_names = [field[0] for field in reader.fields[1:]]
        rows = []
        for shape_record in reader.iterShapeRecords():
            record = dict(zip(field_names, shape_record.record))
            coords = [(float(x), float(y)) for x, y in shape_record.shape.points]
            rows.append({"record": record, "coords": coords})
    return rows


def _nearest_edge_segment(
    point: Tuple[float, float],
    ring: Sequence[Tuple[float, float]],
) -> Tuple[int, float, Tuple[float, float], Tuple[float, float], Tuple[float, float]]:
    px, py = point
    best = None
    for index in range(len(ring) - 1):
        ax, ay = ring[index]
        bx, by = ring[index + 1]
        vx = bx - ax
        vy = by - ay
        denom = vx * vx + vy * vy
        if denom == 0:
            continue
        t = max(0.0, min(1.0, ((px - ax) * vx + (py - ay) * vy) / denom))
        qx = ax + t * vx
        qy = ay + t * vy
        dist = math.hypot(px - qx, py - qy)
        candidate = (dist, index, t, (qx, qy), (ax, ay), (bx, by))
        if best is None or candidate[0] < best[0]:
            best = candidate
    if best is None:
        raise ValueError("Could not find nearest perimeter edge")
    return best[1], best[2], best[3], best[4], best[5]


def _segment_centered_on_edge(
    projection: Tuple[float, float],
    edge_start: Tuple[float, float],
    edge_end: Tuple[float, float],
    preferred_length: float = 10.0,
) -> List[Tuple[float, float]]:
    ax, ay = edge_start
    bx, by = edge_end
    px, py = projection
    dx = bx - ax
    dy = by - ay
    length = math.hypot(dx, dy)
    if length == 0:
        return [edge_start, edge_end]

    ux = dx / length
    uy = dy / length
    half = min(preferred_length / 2.0, length / 2.0)

    proj_dist = math.hypot(px - ax, py - ay)
    start_dist = max(0.0, min(length, proj_dist - half))
    end_dist = max(0.0, min(length, proj_dist + half))

    start = (ax + ux * start_dist, ay + uy * start_dist)
    end = (ax + ux * end_dist, ay + uy * end_dist)
    return [start, end]


def _offset_segment_outside_ring(
    segment: Sequence[Tuple[float, float]],
    ring: Sequence[Tuple[float, float]],
    offset_distance: float,
) -> List[Tuple[float, float]]:
    if len(segment) < 2 or offset_distance <= 0:
        return list(segment)

    start = segment[0]
    end = segment[-1]
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    seg_len = math.hypot(dx, dy)
    if seg_len == 0:
        return list(segment)

    nx = -dy / seg_len
    ny = dx / seg_len
    midpoint = ((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0)

    candidates = []
    for sign in (1.0, -1.0):
        ox = nx * offset_distance * sign
        oy = ny * offset_distance * sign
        shifted = [(point[0] + ox, point[1] + oy) for point in segment]
        shifted_mid = (midpoint[0] + ox, midpoint[1] + oy)
        candidates.append(
            (
                _point_in_ring(shifted_mid, ring),
                math.hypot(shifted_mid[0] - midpoint[0], shifted_mid[1] - midpoint[1]),
                shifted,
            )
        )

    outside_candidates = [item for item in candidates if not item[0]]
    if outside_candidates:
        return outside_candidates[0][2]
    return max(candidates, key=lambda item: item[1])[2]


def _offset_polyline_outside_ring(
    polyline: Sequence[Tuple[float, float]],
    ring: Sequence[Tuple[float, float]],
    offset_distance: float,
) -> List[Tuple[float, float]]:
    if len(polyline) < 2 or offset_distance <= 0:
        return list(polyline)

    start = polyline[0]
    end = polyline[-1]
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    seg_len = math.hypot(dx, dy)
    if seg_len == 0:
        return list(polyline)

    nx = -dy / seg_len
    ny = dx / seg_len
    centroid = _polygon_centroid(ring)
    interior_side = _point_side_of_line(centroid, polyline)
    preferred_sign = -1.0 if interior_side == "left" else 1.0

    middle_index = len(polyline) // 2
    middle_point = polyline[middle_index]
    candidates = []
    for sign in (preferred_sign, -preferred_sign):
        ox = nx * offset_distance * sign
        oy = ny * offset_distance * sign
        shifted = [(point[0] + ox, point[1] + oy) for point in polyline]
        shifted_mid = (middle_point[0] + ox, middle_point[1] + oy)
        candidates.append((not _point_in_ring(shifted_mid, ring), shifted))

    for outside, shifted in candidates:
        if outside:
            return shifted
    return candidates[0][1]


def build_boundary_lines_from_sources(
    sections: Sequence[CrossSectionInfo],
    perimeter_ring: Sequence[Tuple[float, float]],
    boundary_offset_distance: float,
    storage_area_name: str,
) -> List[Dict[str, Any]]:
    boundary_lines = []
    for role, section in (
        ("upstream BC", sections[0]),
        ("downstream BC", sections[-1]),
    ):
        coords = _offset_polyline_outside_ring(
            section.points,
            perimeter_ring,
            offset_distance=boundary_offset_distance,
        )
        boundary_lines.append(
            {
                "name": role,
                "storage_area": storage_area_name,
                "coords": coords,
            }
        )
    return boundary_lines


def _distance_point_to_segment(
    point: Tuple[float, float],
    start: Tuple[float, float],
    end: Tuple[float, float],
) -> float:
    px, py = point
    ax, ay = start
    bx, by = end
    dx = bx - ax
    dy = by - ay
    denom = dx * dx + dy * dy
    if denom == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / denom))
    qx = ax + t * dx
    qy = ay + t * dy
    return math.hypot(px - qx, py - qy)


def _distance_point_to_polyline(
    point: Tuple[float, float],
    coords: Sequence[Tuple[float, float]],
) -> float:
    return min(
        _distance_point_to_segment(point, start, end)
        for start, end in zip(coords[:-1], coords[1:])
    )


def analyze_mesh_enforcement(config: RasMapperConfig) -> Dict[str, Any]:
    if not config.geom_hdf_path.exists():
        return {"available": False, "reason": "geometry_hdf_missing"}

    breaklines = [coords for _, coords in load_polyline_features(config.breakline_src)]
    with h5py.File(config.geom_hdf_path, "r") as handle:
        base = resolve_hdf_2d_area_group(
            config,
            handle,
            geom_path=config.geom_path,
        )
        face_points = handle[f"{base}/FacePoints Coordinate"][:]
        attrs = handle["Geometry/2D Flow Area Break Lines/Attributes"][:]

    rows = []
    for index, attr in enumerate(attrs):
        coords = breaklines[index]
        near_spacing = float(attr["Cell Spacing Near"])
        near_repeats = int(attr["Near Repeats"])
        influence_distance = near_spacing * near_repeats
        distances = [
            _distance_point_to_polyline((float(x), float(y)), coords)
            for x, y in face_points
        ]
        within = [value for value in distances if value <= influence_distance + 1e-6]
        bins = {}
        for repeat in range(1, near_repeats + 1):
            cutoff = near_spacing * repeat
            bins[f"within_{repeat}x"] = sum(value <= cutoff + 1e-6 for value in distances)

        rows.append(
            {
                "name": _decode_hdf_bytes(attr["Name"]),
                "near_spacing": near_spacing,
                "far_spacing": float(attr["Cell Spacing Far"]),
                "near_repeats": near_repeats,
                "influence_distance": influence_distance,
                "facepoint_count_within_influence": len(within),
                "closest_facepoint_distance": min(distances) if distances else None,
                "median_facepoint_distance_within_influence": (
                    float(np.median(within)) if within else None
                ),
                **bins,
            }
        )

    summary = {
        "available": True,
        "mesh_hdf": str(config.geom_hdf_path),
        "breaklines": rows,
    }
    config.mesh_qc_json.write_text(
        json.dumps(_to_jsonable(summary), indent=2),
        encoding="utf-8",
    )
    return summary


def sync_geometry_to_source_shapes(config: RasMapperConfig) -> Dict[str, Any]:
    geom_text = config.geom_path.read_text(encoding="utf-8", errors="replace")
    lines = geom_text.splitlines(True)

    perimeter_ring = _ensure_closed_ring(load_polygon_rings(config.perimeter_src)[0])
    breaklines = [coords for _, coords in load_polyline_features(config.breakline_src)]
    sections = load_cross_sections(config.cross_section_src)
    boundary_lines = build_boundary_lines_from_sources(
        sections,
        perimeter_ring,
        boundary_offset_distance=config.boundary_offset_distance,
        storage_area_name=resolve_storage_area_name(
            config,
            geom_path=config.geom_path,
            lines=lines,
        ),
    )

    lines = _update_viewing_rectangle(lines, perimeter_ring)
    point_generation = (
        f",,{config.mesh_cell_size:g},{config.mesh_cell_size:g}"
    )
    lines = _upsert_project_line(
        lines,
        "Storage Area Point Generation Data=",
        point_generation,
    )

    computation_points = _generate_computation_points(
        perimeter_ring,
        config.mesh_cell_size,
        breaklines=breaklines,
        breakline_near_spacing=config.breakline_near_spacing,
        breakline_near_repeats=config.breakline_near_repeats,
    )

    surface_lines = [f"Storage Area Surface Line= {len(perimeter_ring)} \n"]
    surface_lines.extend(_fixed_width_xy_line([point]) for point in perimeter_ring)
    lines = _replace_line_block_by_count(
        lines,
        "Storage Area Surface Line=",
        surface_lines,
    )

    lines = _replace_block_between_prefixes(
        lines,
        "Storage Area 2D Points=",
        "Storage Area 2D PointsPerimeterTime=",
        [
            f"Storage Area 2D Points= {len(computation_points)} \n",
            *[
                _fixed_width_xy_line(chunk)
                for chunk in _chunk_points(computation_points, 2)
            ],
        ],
    )
    lines = _upsert_project_line(
        lines,
        "Storage Area 2D PointsPerimeterTime=",
        _current_timestamp_compact(),
    )

    breakline_lines: List[str] = []
    for index, coords in enumerate(breaklines, start=1):
        breakline_lines.append(f"BreakLine Name=Breakline {index}\n")
        breakline_lines.append(
            f"BreakLine CellSize Min={config.breakline_near_spacing}\n"
        )
        breakline_lines.append(
            f"BreakLine CellSize Max={config.breakline_far_spacing}\n"
        )
        breakline_lines.append(
            f"BreakLine Near Repeats={config.breakline_near_repeats}\n"
        )
        breakline_lines.append("BreakLine Protection Radius=0\n")
        breakline_lines.append(f"BreakLine Polyline= {len(coords)} \n")
        breakline_lines.extend(
            _fixed_width_xy_line(chunk)
            for chunk in _chunk_points(coords, 2)
        )
    lines = _replace_block_between_prefixes(
        lines,
        "BreakLine Name=",
        "BC Line Name=",
        breakline_lines,
    )

    bc_lines: List[str] = []
    for boundary in boundary_lines:
        start = boundary["coords"][0]
        end = boundary["coords"][-1]
        middle = boundary["coords"][len(boundary["coords"]) // 2]
        bc_lines.append(f"BC Line Name={boundary['name']:<32}\n")
        bc_lines.append(
            f"BC Line Storage Area={boundary['storage_area']}\n"
        )
        bc_lines.append(
            f"BC Line Start Position= {start[0]} , {start[1]} \n"
        )
        bc_lines.append(
            f"BC Line Middle Position= {middle[0]} , {middle[1]} \n"
        )
        bc_lines.append(
            f"BC Line End Position= {end[0]} , {end[1]} \n"
        )
        bc_lines.append(f"BC Line Arc= {len(boundary['coords'])} \n")
        bc_lines.extend(
            _fixed_width_xy_line(chunk)
            for chunk in _chunk_points(boundary["coords"], 2)
        )
        bc_lines.append(
            "BC Line Text Position= 1.79769313486232E+308 , "
            "1.79769313486232E+308 \n"
        )
    lines = _replace_block_between_prefixes(
        lines,
        "BC Line Name=",
        "LCMann Time=",
        bc_lines,
    )

    config.geom_path.write_text("".join(lines), encoding="utf-8")

    return {
        "perimeter_points": len(perimeter_ring),
        "computation_points": len(computation_points),
        "mesh_cell_size": config.mesh_cell_size,
        "breakline_counts": [len(coords) for coords in breaklines],
        "boundary_lines": [
            {
                "name": item["name"],
                "coords": item["coords"],
            }
            for item in boundary_lines
        ],
    }


def regenerate_geometry_hdf(
    config: RasMapperConfig,
    timeout: int = 300,
) -> Dict[str, Any]:
    if config.geom_hdf_path.exists():
        config.geom_hdf_path.unlink()

    ras_obj = init_ras_project(
        config.project_file,
        str(config.ras_exe),
        ras_object="new",
        load_results_summary=False,
    )

    result = MeshRegenerationWorkflow.regenerate_mesh(
        ras_object=ras_obj,
        timeout=timeout,
        close_after=True,
        mannings_layer_name="LandCover",
    )

    if not config.geom_hdf_path.exists():
        raise RuntimeError("Geometry HDF was not recreated by HEC-RAS")

    with h5py.File(config.geom_hdf_path, "r") as handle:
        base = resolve_hdf_2d_area_group(
            config,
            handle,
            geom_path=config.geom_path,
        )
        if base not in handle:
            raise RuntimeError(
                "Geometry HDF was recreated, but no 2D flow area mesh was found"
            )
        n_cells = int(handle[f"{base}/Cells Center Coordinate"].shape[0])
        perimeter = handle[f"{base}/Perimeter"][:]
        breakline_attrs = handle["Geometry/2D Flow Area Break Lines/Attributes"][:]

    region_records = None
    landcover_status = None
    if config.landcover_lookup_csv.exists():
        lookup_df = pd.read_csv(config.landcover_lookup_csv, dtype={"KodText": str})
        create_landcover_raster(config)
        create_mannings_raster(config, lookup_df)
        create_landcover_layer_hdf(config, lookup_df)
        existing_landcover_status = install_existing_landcover_layer(config)
        _sync_landcover_map_layers(config)
        geometry_assoc_updated = _ensure_geometry_landcover_association(config)
        region_df = sync_landcover_geometry(config, lookup_df)
        region_records = region_df.to_dict(orient="records")
        landcover_status = {
            "generated_landcover_raster": config.landcover_raster,
            "generated_landcover_hdf": config.landcover_hdf,
            "active_landcover_map_tif": config.active_landcover_map_tif,
            "active_landcover_map_hdf": config.active_landcover_map_hdf,
            "landcover_mannings_raster": config.landcover_mannings_raster,
            "lookup_records": lookup_df.to_dict(orient="records"),
            "region_records": region_records,
            "existing_ras_layer": existing_landcover_status,
            "geometry_landcover_association_updated": geometry_assoc_updated,
        }
        config.landcover_status_json.write_text(
            json.dumps(_to_jsonable(landcover_status), indent=2),
            encoding="utf-8",
        )

    removed_layers = _cleanup_stale_landcover_layers(config.rasmap_path)
    mesh_qc = analyze_mesh_enforcement(config)

    return {
        "workflow_success": bool(result.success),
        "hdf_path": config.geom_hdf_path,
        "hdf_2d_area_group": base,
        "mesh_cell_count": n_cells,
        "hdf_perimeter_points": len(perimeter),
        "hdf_breakline_count": len(breakline_attrs),
        "region_mannings": region_records,
        "landcover_status": landcover_status,
        "mesh_qc": mesh_qc,
        "removed_stale_landcover_layers": removed_layers,
        "timeout": timeout,
        "geometry_association_selected": result.step_results.get(
            "Associate geometry Manning's n layer"
        ),
    }


def install_reference_geometry(
    config: RasMapperConfig,
    timeout: int = 300,
    skip_regeneration: bool = False,
) -> Dict[str, Any]:
    if not config.project_file.exists():
        raise FileNotFoundError(
            f"Project file not found: {config.project_file}. Run prepare first."
        )

    seed_geometry_from_reference(config)

    source_sync = sync_geometry_to_source_shapes(config)
    regen_summary = None
    if config.geom_hdf_path.exists():
        config.geom_hdf_path.unlink()
    if not skip_regeneration:
        regen_summary = regenerate_geometry_hdf(config, timeout=timeout)

    lookup_df = pd.read_csv(config.landcover_lookup_csv, dtype={"KodText": str})
    create_landcover_raster(config)
    create_mannings_raster(config, lookup_df)
    create_landcover_layer_hdf(config, lookup_df)
    existing_landcover_status = install_existing_landcover_layer(config)
    _sync_landcover_map_layers(config)
    geometry_assoc_updated = _ensure_geometry_landcover_association(config)
    expected_region_df = sync_landcover_geometry(config, lookup_df)

    summary = {
        "geom_path": config.geom_path,
        "geom_hdf_path": config.geom_hdf_path,
        "geometry_title": config.geometry_title,
        "resolved_storage_area_name": resolve_storage_area_name(
            config,
            geom_path=config.geom_path,
        ),
        "mesh_cell_size": config.mesh_cell_size,
        "breakline_near_spacing": config.breakline_near_spacing,
        "breakline_near_repeats": config.breakline_near_repeats,
        "source_sync": source_sync,
        "regeneration": regen_summary,
        "skip_regeneration": skip_regeneration,
        "generated_landcover_raster": config.landcover_raster,
        "generated_landcover_hdf": config.landcover_hdf,
        "active_landcover_map_tif": config.active_landcover_map_tif,
        "active_landcover_map_hdf": config.active_landcover_map_hdf,
        "landcover_mannings_raster": config.landcover_mannings_raster,
        "existing_ras_layer": existing_landcover_status,
        "geometry_landcover_association_updated": geometry_assoc_updated,
        "region_mannings": expected_region_df.to_dict(orient="records"),
    }
    (config.reports_dir / "geometry_install_summary.json").write_text(
        json.dumps(_to_jsonable(summary), indent=2),
        encoding="utf-8",
    )
    return summary


def sync_reference_geometry(config: RasMapperConfig) -> Dict[str, Any]:
    if not config.project_file.exists():
        raise FileNotFoundError(
            f"Project file not found: {config.project_file}. Run prepare first."
        )

    seed_geometry_from_reference(config)
    source_sync = sync_geometry_to_source_shapes(config)
    if config.geom_hdf_path.exists():
        config.geom_hdf_path.unlink()

    region_records = None
    existing_landcover_status = install_existing_landcover_layer(config)
    if config.landcover_lookup_csv.exists():
        lookup_df = pd.read_csv(config.landcover_lookup_csv, dtype={"KodText": str})
        create_landcover_raster(config)
        create_mannings_raster(config, lookup_df)
        create_landcover_layer_hdf(config, lookup_df)
        existing_landcover_status = install_existing_landcover_layer(config)
        _sync_landcover_map_layers(config)
        geometry_assoc_updated = _ensure_geometry_landcover_association(config)
        expected_region_df = sync_landcover_geometry(config, lookup_df)
        region_records = expected_region_df.to_dict(orient="records")
    else:
        geometry_assoc_updated = _ensure_geometry_landcover_association(config)

    summary = {
        "geom_path": config.geom_path,
        "geometry_title": config.geometry_title,
        "resolved_storage_area_name": resolve_storage_area_name(
            config,
            geom_path=config.geom_path,
        ),
        "mesh_cell_size": config.mesh_cell_size,
        "breakline_near_spacing": config.breakline_near_spacing,
        "breakline_near_repeats": config.breakline_near_repeats,
        "source_sync": source_sync,
        "generated_landcover_raster": config.landcover_raster,
        "generated_landcover_hdf": config.landcover_hdf,
        "active_landcover_map_tif": config.active_landcover_map_tif,
        "active_landcover_map_hdf": config.active_landcover_map_hdf,
        "landcover_mannings_raster": config.landcover_mannings_raster,
        "existing_ras_layer": existing_landcover_status,
        "geometry_landcover_association_updated": geometry_assoc_updated,
        "region_mannings": region_records,
        "geom_hdf_deleted": True,
    }
    (config.reports_dir / "geometry_sync_summary.json").write_text(
        json.dumps(_to_jsonable(summary), indent=2),
        encoding="utf-8",
    )
    return summary


def _candidate_keys(name: str) -> List[str]:
    text = str(name).strip()
    keys = [text.upper(), _normalize_code(text).upper()]
    if " - " in text:
        keys.append(text.split(" - ", 1)[0].strip().upper())
    numeric = []
    for char in text:
        if char.isdigit() or char == ".":
            numeric.append(char)
        else:
            break
    if numeric:
        keys.append(_normalize_code("".join(numeric)).upper())
    return [key for key in keys if key]


def build_expected_mannings(
    geometry_df: pd.DataFrame,
    lookup_df: pd.DataFrame,
) -> pd.DataFrame:
    code_index = {
        str(code).upper(): row
        for _, row in lookup_df.iterrows()
        for code in [row["KodText"]]
    }
    name_index = {
        str(row["Adi"]).strip().upper(): row
        for _, row in lookup_df.iterrows()
    }

    mapped_rows: List[Dict[str, Any]] = []
    missing_names: List[str] = []

    for _, geom_row in geometry_df.iterrows():
        geom_name = str(geom_row["Land Cover Name"]).strip()
        match = None
        for key in _candidate_keys(geom_name):
            match = code_index.get(key) or name_index.get(key)
            if match is not None:
                break

        if match is None:
            missing_names.append(geom_name)
            continue

        mapped_rows.append(
            {
                "Table Number": geom_row["Table Number"],
                "Land Cover Name": geom_name,
                "Base Mannings n Value": float(match["Manningn"]),
                "Matched KodText": str(match["KodText"]),
                "Matched Adi": str(match["Adi"]),
            }
        )

    if missing_names:
        missing_text = ", ".join(sorted(set(missing_names)))
        raise ValueError(
            "Could not map geometry landcover names to KodText/Adi values: "
            f"{missing_text}"
        )

    return pd.DataFrame(mapped_rows)


def resolve_geometry_path(config: RasMapperConfig, geom: Optional[str]) -> Path:
    if geom:
        candidate = Path(geom)
        if candidate.exists():
            return candidate.resolve()
        if len(geom) <= 3 and geom.lower().lstrip("g").isdigit():
            geom_number = geom.lower().lstrip("g").zfill(2)
            inferred = config.project_root / f"{config.project_name}.g{geom_number}"
            if inferred.exists():
                return inferred
        raise FileNotFoundError(f"Geometry file not found for '{geom}'")

    geometry_files = sorted(config.project_root.glob("*.g[0-9][0-9]"))
    if not geometry_files:
        raise FileNotFoundError(
            "No geometry files found. Create geometry in RAS Mapper first."
        )
    if len(geometry_files) == 1:
        return geometry_files[0]

    latest = max(geometry_files, key=lambda path: path.stat().st_mtime)
    LOGGER.warning(
        "Multiple geometry files found. Using most recently modified: %s",
        latest,
    )
    return latest


@log_call
def apply_mannings(config: RasMapperConfig, geom: Optional[str]) -> Path:
    geom_path = resolve_geometry_path(config, geom)
    lookup_df = pd.read_csv(config.landcover_lookup_csv, dtype={"KodText": str})
    geometry_df = GeomLandCover.get_base_mannings_n(geom_path)
    if not geometry_df.empty:
        expected_df = build_expected_mannings(geometry_df, lookup_df)
        GeomLandCover.set_base_mannings_n(
            geom_path,
            expected_df[
                ["Table Number", "Land Cover Name", "Base Mannings n Value"]
            ],
        )
        audit_path = config.reports_dir / "final_mannings_from_geom.csv"
        expected_df.to_csv(audit_path, index=False)
        LOGGER.info("Applied base Manning values to geometry: %s", geom_path)
        return audit_path

    region_df = GeomLandCover.get_region_mannings_n(geom_path)
    if region_df.empty:
        raise ValueError(
            f"No base or region Manning table found in geometry: {geom_path}"
        )

    expected_region_df = build_expected_region_mannings(region_df, lookup_df)
    GeomLandCover.set_region_mannings_n(geom_path, expected_region_df)
    if geom_path.with_suffix(geom_path.suffix + ".hdf").exists():
        update_hdf_region_mannings(
            geom_path.with_suffix(geom_path.suffix + ".hdf"),
            expected_region_df,
        )

    audit_path = config.reports_dir / "final_region_mannings_from_geom.csv"
    expected_region_df.to_csv(audit_path, index=False)
    LOGGER.info("Applied regional Manning values to geometry: %s", geom_path)
    return audit_path


@log_call
def check_mannings(config: RasMapperConfig, geom: Optional[str]) -> Path:
    geom_path = resolve_geometry_path(config, geom)
    lookup_df = pd.read_csv(config.landcover_lookup_csv, dtype={"KodText": str})
    geometry_df = GeomLandCover.get_base_mannings_n(geom_path)
    if not geometry_df.empty:
        expected_df = build_expected_mannings(geometry_df, lookup_df)
        merged = geometry_df.merge(
            expected_df[
                [
                    "Land Cover Name",
                    "Base Mannings n Value",
                    "Matched KodText",
                    "Matched Adi",
                ]
            ],
            on="Land Cover Name",
            how="left",
            suffixes=("_geom", "_expected"),
        )
        merged["matches"] = (
            merged["Base Mannings n Value_geom"].round(6)
            == merged["Base Mannings n Value_expected"].round(6)
        )
        report_path = config.reports_dir / "mannings_check_report.csv"
        merged.to_csv(report_path, index=False)
        return report_path

    region_df = GeomLandCover.get_region_mannings_n(geom_path)
    if region_df.empty:
        raise ValueError(
            f"No base or region Manning table found in geometry: {geom_path}"
        )

    expected_region_df = build_expected_region_mannings(region_df, lookup_df)
    merged = region_df.merge(
        expected_region_df[
            ["Land Cover Name", "MainChannel", "Region Name"]
        ],
        on=["Land Cover Name", "Region Name"],
        how="left",
        suffixes=("_geom", "_expected"),
    )
    merged["matches"] = (
        merged["MainChannel_geom"].round(6)
        == merged["MainChannel_expected"].round(6)
    )
    report_path = config.reports_dir / "region_mannings_check_report.csv"
    merged.to_csv(report_path, index=False)
    return report_path


def open_project_in_rasmapper(
    config: RasMapperConfig,
    wait_for_user: bool,
    timeout: int,
) -> bool:
    command = f'"{config.ras_exe}" "{config.project_file}"'
    process = subprocess.Popen(command)
    LOGGER.info("Launched HEC-RAS: pid=%s", process.pid)
    time.sleep(1.0)
    HecRasElements.handle_already_running_dialog(timeout=3)

    def find_window() -> Optional[int]:
        windows = Win32Primitives.get_windows_by_pid(process.pid)
        hwnd, _ = HecRasElements.find_main_hecras_window(windows)
        return hwnd

    hwnd = Win32Primitives.wait_for_window(find_window, timeout=30)
    if not hwnd:
        raise RuntimeError("Could not find HEC-RAS main window")

    if not HecRasElements.click_menu_by_path(
        hwnd,
        ["&GIS Tools", "RAS &Mapper"],
    ):
        raise RuntimeError(
            "HEC-RAS opened, but GIS Tools > RAS Mapper could not be clicked."
        )

    if not RasMapperElements.wait_for_rasmapper(
        timeout=timeout,
        check_interval=3,
    ):
        raise RuntimeError("RAS Mapper window did not appear before timeout")

    if wait_for_user:
        LOGGER.info("Waiting for user to close RAS Mapper...")
        while RasMapperElements.find_rasmapper_window():
            time.sleep(2.0)
    return True


@log_call
def prepare_project(config: RasMapperConfig, skip_terrain: bool) -> Dict[str, Any]:
    validate_inputs(config)

    ensure_dirs(
        [
            config.working_root,
            config.project_root,
            config.terrain_dir,
            config.inputs_dir,
            config.helper_dir,
            config.reports_dir,
            config.boundary_dir,
            config.landcover_dir,
            config.land_classification_dir,
        ]
    )

    copy_file(config.projection_src, config.projection_copy)
    copy_shapefile_family(config.perimeter_src, config.perimeter_copy.parent)
    copy_shapefile_family(config.breakline_src, config.breakline_copy.parent)
    copy_shapefile_family(config.landcover_src, config.landcover_copy.parent)
    copy_file(config.dss_src, config.dss_copy)
    copy_file(config.dss_catalog_src, config.dss_catalog_copy)
    copy_file(config.cross_section_src, config.cross_section_copy)

    write_minimal_project_file(config)
    write_minimal_rasmap(config)

    perimeter_ring = load_polygon_rings(config.perimeter_copy)[0]
    breakline_features = load_polyline_features(config.breakline_copy)
    breaklines = [coords for _, coords in breakline_features]
    sections = load_cross_sections(config.cross_section_copy)

    write_cross_section_shapefile(sections, config.cross_sections_shp, config)
    write_centerline_shapefile(sections, config.centerline_shp, config)

    landcover_lookup = _load_landcover_table(config.landcover_copy)
    landcover_lookup.to_csv(config.landcover_lookup_csv, index=False, encoding="utf-8")
    create_landcover_raster(config)
    create_mannings_raster(config, landcover_lookup)
    create_landcover_layer_hdf(config, landcover_lookup)
    existing_landcover_status = install_existing_landcover_layer(config)

    dss_catalog = parse_dss_catalog(config.dss_catalog_copy)
    preferred_dss_path, dss_catalog = choose_preferred_dss_path(
        dss_catalog,
        preferred_a_part=config.preferred_dss_a_part,
        preferred_f_part=config.preferred_dss_f_part,
    )
    dss_catalog.to_csv(config.dss_catalog_csv, index=False)

    boundary_df = create_boundary_artifacts(
        config,
        sections=sections,
        perimeter_ring=perimeter_ring,
        preferred_dss_path=preferred_dss_path,
    )

    generate_overview_plot(
        config,
        perimeter_ring=perimeter_ring,
        breaklines=breaklines,
        sections=sections,
    )

    downstream_slope = estimate_normal_depth_slope(sections)
    write_manual_checklist(
        config,
        preferred_dss_path=preferred_dss_path,
        downstream_slope=downstream_slope,
    )

    if not skip_terrain:
        RasTerrain.create_terrain_hdf(
            input_rasters=[config.dtm_src],
            output_hdf=config.terrain_hdf,
            projection_prj=config.projection_copy,
            units="Meters",
            stitch=True,
            hecras_version=config.hecras_version,
        )
        RasMap.add_terrain_layer(
            terrain_hdf=config.terrain_hdf,
            rasmap_path=config.rasmap_path,
            layer_name=config.terrain_layer_name,
            projection_prj=config.projection_copy,
        )
    else:
        _ensure_projection_element(config.rasmap_path, config.projection_copy)

    _sync_landcover_map_layers(config)
    geometry_assoc_updated = _ensure_geometry_landcover_association(config)
    removed_layers = _cleanup_stale_landcover_layers(config.rasmap_path)

    landcover_status = {
        "generated_landcover_raster": config.landcover_raster,
        "generated_landcover_hdf": config.landcover_hdf,
        "active_landcover_map_tif": config.active_landcover_map_tif,
        "active_landcover_map_hdf": config.active_landcover_map_hdf,
        "landcover_mannings_raster": config.landcover_mannings_raster,
        "lookup_records": landcover_lookup.to_dict(orient="records"),
        "existing_ras_layer": existing_landcover_status,
        "geometry_landcover_association_updated": geometry_assoc_updated,
    }
    config.landcover_status_json.write_text(
        json.dumps(_to_jsonable(landcover_status), indent=2),
        encoding="utf-8",
    )

    summary = {
        "project_root": config.project_root,
        "project_file": config.project_file,
        "rasmap_path": config.rasmap_path,
        "resolved_storage_area_name": resolve_storage_area_name(config),
        "terrain_hdf": config.terrain_hdf if config.terrain_hdf.exists() else None,
        "terrain_created": config.terrain_hdf.exists(),
        "generated_landcover_raster": config.landcover_raster,
        "generated_landcover_hdf": config.landcover_hdf,
        "active_landcover_map_tif": config.active_landcover_map_tif,
        "active_landcover_map_hdf": config.active_landcover_map_hdf,
        "landcover_mannings_raster": config.landcover_mannings_raster,
        "landcover_lookup_csv": config.landcover_lookup_csv,
        "existing_ras_layer": existing_landcover_status,
        "geometry_landcover_association_updated": geometry_assoc_updated,
        "cross_sections_shp": config.cross_sections_shp,
        "boundary_shp": config.boundary_shp,
        "dss_catalog_csv": config.dss_catalog_csv,
        "preferred_dss_path": preferred_dss_path,
        "downstream_slope": downstream_slope,
        "boundary_candidates": boundary_df.to_dict(orient="records"),
        "removed_stale_landcover_layers": removed_layers,
        "checklist_md": config.checklist_md,
        "spatial_overview_png": (
            config.spatial_overview_png
            if config.spatial_overview_png.exists()
            else None
        ),
    }
    config.prepare_summary_json.write_text(
        json.dumps(_to_jsonable(summary), indent=2),
        encoding="utf-8",
    )
    return summary


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare and audit a RAS Mapper 2D workspace.",
    )
    parser.add_argument(
        "--config-json",
        type=Path,
        help=(
            "Optional study-area JSON file. Values in the file override "
            "built-in defaults, and explicit CLI flags override the JSON."
        ),
    )
    parser.add_argument(
        "--files-root",
        type=Path,
        default=DEFAULT_FILES_ROOT,
        help="Root folder containing the source GIS, DSS, and DTM files.",
    )
    parser.add_argument(
        "--working-root",
        type=Path,
        default=DEFAULT_WORKING_ROOT,
        help="Root folder where the HEC-RAS project will be created.",
    )
    parser.add_argument(
        "--project-name",
        default=DEFAULT_PROJECT_NAME,
        help="HEC-RAS project folder and .prj base name.",
    )
    parser.add_argument(
        "--ras-exe",
        type=Path,
        default=DEFAULT_RAS_EXE,
        help="Path to Ras.exe.",
    )
    parser.add_argument(
        "--gdal-grid-exe",
        type=Path,
        default=DEFAULT_GDAL_GRID,
        help="Path to gdal_grid.exe. gdal_rasterize.exe is assumed beside it.",
    )
    parser.add_argument(
        "--mesh-cell-size",
        type=float,
        default=10.0,
        help=(
            "Target 2D mesh cell size in meters. Larger values create a "
            "coarser, faster mesh."
        ),
    )
    parser.add_argument(
        "--existing-landcover-tif",
        type=Path,
        default=DEFAULT_EXISTING_LANDCOVER_TIF,
        help=(
            "Optional existing HEC-RAS landcover TIFF to register as the "
            "native LandCover map layer."
        ),
    )
    parser.add_argument(
        "--existing-landcover-hdf",
        type=Path,
        default=DEFAULT_EXISTING_LANDCOVER_HDF,
        help=(
            "Optional existing HEC-RAS landcover HDF to register as the "
            "native LandCover map layer."
        ),
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_parser = subparsers.add_parser(
        "prepare",
        help="Create the project shell and helper artifacts.",
    )
    prepare_parser.add_argument(
        "--skip-terrain",
        action="store_true",
        help="Skip terrain HDF creation for a faster dry run.",
    )

    prepare_open_parser = subparsers.add_parser(
        "prepare-open",
        help="Run prepare, then open the project in RAS Mapper.",
    )
    prepare_open_parser.add_argument(
        "--skip-terrain",
        action="store_true",
        help="Skip terrain HDF creation for a faster dry run.",
    )
    prepare_open_parser.add_argument(
        "--wait-for-user",
        action="store_true",
        help="Wait until the user closes RAS Mapper.",
    )
    prepare_open_parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Seconds to wait for the RAS Mapper window.",
    )

    open_parser = subparsers.add_parser(
        "open",
        help="Open the prepared project in HEC-RAS and RAS Mapper.",
    )
    open_parser.add_argument(
        "--wait-for-user",
        action="store_true",
        help="Wait until the user closes RAS Mapper.",
    )
    open_parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Seconds to wait for the RAS Mapper window.",
    )

    subparsers.add_parser(
        "sync-geometry",
        help=(
            "Rewrite the working geometry from the source perimeter, "
            "breakline, and boundary inputs without opening HEC-RAS."
        ),
    )

    regen_parser = subparsers.add_parser(
        "regenerate-geometry",
        help="Open HEC-RAS and RAS Mapper to rebuild the geometry HDF.",
    )
    regen_parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Seconds to wait for RAS Mapper mesh regeneration.",
    )

    subparsers.add_parser(
        "install-geometry",
        help="Clone the supplied reference geometry into the working project.",
    )
    install_parser = subparsers.choices["install-geometry"]
    install_parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Seconds to wait for RAS Mapper mesh regeneration.",
    )
    install_parser.add_argument(
        "--skip-regeneration",
        action="store_true",
        help="Only rewrite the geometry text and skip the GUI regeneration step.",
    )

    for command_name in ("apply-mannings", "check-mannings"):
        command_parser = subparsers.add_parser(
            command_name,
            help=f"{command_name.replace('-', ' ').title()} for the geometry.",
        )
        command_parser.add_argument(
            "--geom",
            help="Geometry path or number (for example 01 or g01).",
        )

    return parser.parse_args(argv)


def build_config(args: argparse.Namespace) -> RasMapperConfig:
    values: Dict[str, Any] = {
        "files_root": DEFAULT_FILES_ROOT.resolve(),
        "working_root": DEFAULT_WORKING_ROOT.resolve(),
        "project_name": DEFAULT_PROJECT_NAME,
        "ras_exe": DEFAULT_RAS_EXE.resolve(),
        "gdal_grid_exe": DEFAULT_GDAL_GRID.resolve(),
        "mesh_cell_size": 10.0,
        "existing_landcover_tif_name": str(DEFAULT_EXISTING_LANDCOVER_TIF),
        "existing_landcover_hdf_name": str(DEFAULT_EXISTING_LANDCOVER_HDF),
    }

    if args.config_json:
        config_json = args.config_json.resolve()
        values.update(load_study_area_overrides(config_json))

    cli_overrides = {
        "files_root": args.files_root.resolve(),
        "working_root": args.working_root.resolve(),
        "project_name": args.project_name,
        "ras_exe": args.ras_exe.resolve(),
        "gdal_grid_exe": args.gdal_grid_exe.resolve(),
        "mesh_cell_size": args.mesh_cell_size,
        "existing_landcover_tif_name": str(args.existing_landcover_tif.resolve()),
        "existing_landcover_hdf_name": str(args.existing_landcover_hdf.resolve()),
    }
    cli_defaults = {
        "files_root": DEFAULT_FILES_ROOT.resolve(),
        "working_root": DEFAULT_WORKING_ROOT.resolve(),
        "project_name": DEFAULT_PROJECT_NAME,
        "ras_exe": DEFAULT_RAS_EXE.resolve(),
        "gdal_grid_exe": DEFAULT_GDAL_GRID.resolve(),
        "mesh_cell_size": 10.0,
        "existing_landcover_tif_name": str(DEFAULT_EXISTING_LANDCOVER_TIF.resolve()),
        "existing_landcover_hdf_name": str(DEFAULT_EXISTING_LANDCOVER_HDF.resolve()),
    }
    for key, value in cli_overrides.items():
        if value != cli_defaults[key]:
            values[key] = value

    return RasMapperConfig(**values)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    config = build_config(args)

    try:
        if args.command == "prepare":
            summary = prepare_project(config, skip_terrain=args.skip_terrain)
            print(json.dumps(_to_jsonable(summary), indent=2))
            return 0

        if args.command == "prepare-open":
            summary = prepare_project(config, skip_terrain=args.skip_terrain)
            print(json.dumps(_to_jsonable(summary), indent=2))
            open_project_in_rasmapper(
                config,
                wait_for_user=args.wait_for_user,
                timeout=args.timeout,
            )
            return 0

        if args.command == "open":
            validate_inputs(config)
            if not config.project_file.exists():
                raise FileNotFoundError(
                    f"Project file not found: {config.project_file}. "
                    "Run 'prepare' first."
                )
            open_project_in_rasmapper(
                config,
                wait_for_user=args.wait_for_user,
                timeout=args.timeout,
            )
            return 0

        if args.command == "sync-geometry":
            validate_inputs(config, require_reference_geometry=True)
            summary = sync_reference_geometry(config)
            print(json.dumps(_to_jsonable(summary), indent=2))
            return 0

        if args.command == "regenerate-geometry":
            validate_inputs(config)
            if not config.project_file.exists():
                raise FileNotFoundError(
                    f"Project file not found: {config.project_file}. "
                    "Run 'prepare' first."
                )
            if not config.geom_path.exists():
                raise FileNotFoundError(
                    f"Geometry file not found: {config.geom_path}. "
                    "Run 'sync-geometry' or 'install-geometry' first."
                )
            summary = regenerate_geometry_hdf(config, timeout=args.timeout)
            print(json.dumps(_to_jsonable(summary), indent=2))
            return 0

        if args.command == "install-geometry":
            validate_inputs(config, require_reference_geometry=True)
            summary = install_reference_geometry(
                config,
                timeout=args.timeout,
                skip_regeneration=args.skip_regeneration,
            )
            print(json.dumps(_to_jsonable(summary), indent=2))
            return 0

        if args.command == "apply-mannings":
            report = apply_mannings(config, geom=args.geom)
            print(report)
            return 0

        if args.command == "check-mannings":
            report = check_mannings(config, geom=args.geom)
            print(report)
            return 0

        raise ValueError(f"Unsupported command: {args.command}")
    except Exception as exc:
        LOGGER.exception("rasmapper.py failed")
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    exit_code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(exit_code)
