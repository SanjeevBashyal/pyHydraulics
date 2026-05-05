#!/usr/bin/env python
"""
Prepare a generalized HEC-RAS 2D RAS Mapper workspace.

This is the reusable study-area version of the workflow. It keeps the
same automation behavior as rasmapper.py, but its defaults are generic
so a new model can start from a clean JSON config instead of inheriting
site-specific file names and paths.
"""

from __future__ import annotations

import argparse
import inspect
import json
import math
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import h5py
import numpy as np
import pandas as pd
import rasterio
import shapefile

WORKSPACE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = WORKSPACE_ROOT
for candidate in (
    WORKSPACE_ROOT,
    WORKSPACE_ROOT.parent / "ras-commander",
    WORKSPACE_ROOT.parent,
):
    if (candidate / "ras_commander").is_dir():
        REPO_ROOT = candidate.resolve()
        break
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


DEFAULT_WORKING_ROOT = WORKSPACE_ROOT / "projects"
DEFAULT_RESULTS_ROOT = WORKSPACE_ROOT / "results"
DEFAULT_FILES_ROOT = WORKSPACE_ROOT / "inputs"
DEFAULT_RAS_EXE = Path(
    r"C:\Program Files (x86)\HEC\HEC-RAS\6.6\Ras.exe"
)
DEFAULT_GDAL_GRID = Path(
    r"C:\Program Files (x86)\HEC\HEC-RAS\6.6\GDAL\bin64\gdal_grid.exe"
)
DEFAULT_PROJECT_NAME = "study_area_project"
DEFAULT_EXISTING_LANDCOVER_TIF: Optional[Path] = None
DEFAULT_EXISTING_LANDCOVER_HDF: Optional[Path] = None

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
class CrossSectionGroup:
    name: str
    source_path: Path
    copy_path: Path
    sections: List[CrossSectionInfo]


@dataclass(frozen=True)
class StructureConnection:
    name: str
    structure_type: str
    line: List[Tuple[float, float]]
    culvert_shape_code: int
    culvert_shape_name: str
    culvert_rise: float
    culvert_span: float
    culvert_length: float
    upstream_invert: float
    downstream_invert: float
    deck_distance: float
    deck_width: float
    deck_weir_coefficient: float
    deck_skew: float
    deck_max_submerge: float
    culvert_mannings_n: float
    culvert_bottom_n: float
    entrance_loss: float
    exit_loss: float
    inlet_type: int
    outlet_type: int
    culvert_chart_number: int
    num_barrels: int
    barrel_center_spacing: float
    htab_hwmax: float


@dataclass(frozen=True)
class RasMapperConfig:
    files_root: Path
    working_root: Path
    project_name: str
    ras_exe: Path
    gdal_grid_exe: Path
    results_root: Path = DEFAULT_RESULTS_ROOT
    projection_name: str = "projection.prj"
    dtm_name: str = "terrain.tif"
    perimeter_name: str = "perimeter.shp"
    breakline_name: str = "breaklines.shp"
    dss_name: str = "boundary.dss"
    cross_section_name: Any = "cross_sections.csv"
    junction_bc_csv_name: Optional[str] = None
    structure_csv_name: Optional[str] = None
    landcover_name: str = "landcover.shp"
    existing_landcover_tif_name: Optional[str] = None
    existing_landcover_hdf_name: Optional[str] = None
    reference_geom_name: str = "reference_geometry.g01"
    reference_geom_hdf_name: str = "reference_geometry.g01.hdf"
    terrain_layer_name: str = "Terrain"
    flow_area_name: str = "StudyArea_2D"
    geometry_title: str = "STUDYAREA_2D_CLEAN"
    storage_area_name: Optional[str] = None
    hdf_2d_area_name: Optional[str] = None
    mesh_cell_size: float = 10.0
    breakline_near_spacing: float = 0.5
    breakline_near_repeats: int = 5
    breakline_far_spacing: float = 3.0
    landcover_cell_size: float = 2.0
    landcover_nodata_manning: float = 0.025
    region_default_manning: float = 0.025
    boundary_offset_distance: float = 1.0
    downstream_bc_length_multiplier: float = 10.0
    preferred_dss_a_part: str = "A_PART"
    preferred_dss_f_part: str = "Q100"
    downstream_bc_method: str = "Normal Depth"
    junction_bc_name: str = "junction BC"
    junction_snap_tolerance: float = 100.0
    branch_connectivity_threshold: float = 0.25
    unsteady_number: str = "01"
    plan_number: str = "01"
    template_unsteady_name: Optional[str] = None
    template_unsteady_hdf_name: Optional[str] = None
    template_plan_name: Optional[str] = None
    unsteady_title: str = "Unsteady Flow"
    plan_title: str = "2D Unsteady Plan"
    plan_short_identifier: str = "2D_Unsteady"
    plan_flow_regime: str = "Mixed Flow"
    plan_simulation_date: str = ",,,"
    auto_plan_simulation_date: bool = True
    simulation_start_time: str = "0000"
    simulation_duration_hours: float = 24.0
    simulation_start_offset_hours: float = 0.0
    simulation_end_offset_hours: float = 0.0
    plan_computation_interval: str = "6SEC"
    plan_hydrograph_output_interval: Optional[str] = "5MIN"
    plan_output_interval: str = "5MIN"
    plan_detailed_output_interval: Optional[str] = "5MIN"
    plan_instantaneous_interval: str = "5MIN"
    plan_mapping_interval: str = "5MIN"
    plan_use_courant_timestep: bool = True
    plan_use_time_series_timestep: bool = False
    plan_max_courant: float = 1.0
    plan_min_courant: float = 0.45
    plan_steps_below_min_before_doubling: int = 4
    plan_max_doubling_base_timestep: int = 2
    plan_max_halving_base_timestep: int = 2
    plan_residence_courant: float = 0.0
    plan_run_htab: bool = True
    plan_run_unet: bool = True
    plan_run_postprocess: bool = False
    plan_run_rasmapper: bool = True
    plan_num_cores: int = 0
    upstream_bc_name: str = "upstream BC"
    downstream_bc_name: str = "downstream BC"
    upstream_flow_interval: str = "1HOUR"
    upstream_flow_hydrograph_slope: float = 0.05
    downstream_friction_slope: Optional[float] = None
    unsteady_dss_file_relative: Optional[str] = None
    compute_timeout_seconds: int = 7200
    auto_confirm_geometry_preprocessor: bool = True
    auto_adjust_simulation_window_from_dss: bool = True
    copy_compute_results_to_project: bool = True

    @property
    def project_root(self) -> Path:
        return (self.working_root / self.project_name).resolve()

    @property
    def project_file(self) -> Path:
        return self.project_root / f"{self.project_name}.prj"

    @property
    def default_compute_output_dir(self) -> Path:
        return (
            self.results_root
            / f"{self.project_name}_plan{self.plan_id}_run"
        ).resolve()

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
        return self.terrain_dir / Path(self.projection_name).name

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
        return self.dss_src.parent / (self.dss_src.stem + ".dsc.h5")

    @property
    def cross_section_src(self) -> Path:
        return self.cross_section_srcs[0]

    @property
    def cross_section_srcs(self) -> List[Path]:
        return [
            _source_path(self.files_root, name)
            for name in _as_config_list(self.cross_section_name)
        ]

    @property
    def junction_bc_csv_src(self) -> Optional[Path]:
        if not self.junction_bc_csv_name:
            return None
        return _source_path(self.files_root, self.junction_bc_csv_name)

    @property
    def structure_csv_src(self) -> Optional[Path]:
        if not self.structure_csv_name:
            return None
        return _source_path(self.files_root, self.structure_csv_name)

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
    def template_unsteady_src(self) -> Optional[Path]:
        if not self.template_unsteady_name:
            return None
        return _source_path(self.files_root, self.template_unsteady_name)

    @property
    def template_unsteady_hdf_src(self) -> Optional[Path]:
        if not self.template_unsteady_hdf_name:
            return None
        return _source_path(self.files_root, self.template_unsteady_hdf_name)

    @property
    def template_plan_src(self) -> Optional[Path]:
        if not self.template_plan_name:
            return None
        return _source_path(self.files_root, self.template_plan_name)

    @property
    def perimeter_copy(self) -> Path:
        return self.inputs_dir / "Perimeter" / Path(self.perimeter_name).name

    @property
    def breakline_copy(self) -> Path:
        return self.inputs_dir / "Breaklines" / Path(self.breakline_name).name

    @property
    def landcover_copy(self) -> Path:
        return self.inputs_dir / "LandCover" / Path(self.landcover_name).name

    @property
    def dss_copy(self) -> Path:
        return self.boundary_dir / Path(self.dss_name).name

    @property
    def dss_catalog_copy(self) -> Path:
        return self.boundary_dir / self.dss_catalog_src.name

    @property
    def cross_section_copy(self) -> Path:
        return self.cross_section_copies[0]

    @property
    def cross_section_copies(self) -> List[Path]:
        return [
            self.boundary_dir / "CrossSections" / Path(name).name
            for name in _as_config_list(self.cross_section_name)
        ]

    @property
    def junction_bc_csv_copy(self) -> Optional[Path]:
        if not self.junction_bc_csv_src:
            return None
        return self.boundary_dir / Path(self.junction_bc_csv_src).name

    @property
    def structure_csv_copy(self) -> Optional[Path]:
        if not self.structure_csv_src:
            return None
        return self.boundary_dir / "Structures" / Path(self.structure_csv_src).name

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
    def unsteady_id(self) -> str:
        value = str(self.unsteady_number).lower()
        if value.startswith("u"):
            value = value[1:]
        return value.zfill(2)

    @property
    def plan_id(self) -> str:
        value = str(self.plan_number).lower()
        if value.startswith("p"):
            value = value[1:]
        return value.zfill(2)

    @property
    def unsteady_path(self) -> Path:
        return self.project_root / f"{self.project_name}.u{self.unsteady_id}"

    @property
    def unsteady_hdf_path(self) -> Path:
        return self.project_root / f"{self.project_name}.u{self.unsteady_id}.hdf"

    @property
    def plan_path(self) -> Path:
        return self.project_root / f"{self.project_name}.p{self.plan_id}"

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
    def unsteady_plan_summary_json(self) -> Path:
        return self.reports_dir / "unsteady_plan_summary.json"

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
    def compute_summary_json(self) -> Path:
        return self.reports_dir / "compute_summary.json"

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
    if isinstance(value, datetime):
        return value.isoformat()
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


def _source_path(root: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path.resolve()
    return (root / path).resolve()


def _as_config_list(value: Any) -> List[str]:
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item).strip()]
    if value in (None, ""):
        return []
    return [str(value)]


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
        "results_root",
        "ras_exe",
        "gdal_grid_exe",
        "structure_csv_name",
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


def _resolved_optional_path_string(path: Optional[Path]) -> Optional[str]:
    if path is None:
        return None
    return str(path.resolve())


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

    if "Attributes" in area_group and (
        "Cell Info" in area_group or "Cell Points" in area_group
    ):
        attrs = area_group["Attributes"][:]
        names = [
            _decode_hdf_bytes(row["Name"])
            for row in attrs
            if "Name" in attrs.dtype.names
        ]
        if not names or any(name in candidates for name in names) or len(attrs) == 1:
            return base_root

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
        config.landcover_src,
    ]
    required.extend(config.cross_section_srcs)
    if config.junction_bc_csv_src is not None:
        required.append(config.junction_bc_csv_src)
    if config.structure_csv_src is not None:
        required.append(config.structure_csv_src)
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
    ensure_project_has_geom_reference(config)
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


def load_cross_section_groups(config: RasMapperConfig) -> List[CrossSectionGroup]:
    groups: List[CrossSectionGroup] = []
    for source_path, copy_path in zip(
        config.cross_section_srcs,
        config.cross_section_copies,
    ):
        load_path = copy_path if copy_path.exists() else source_path
        groups.append(
            CrossSectionGroup(
                name=source_path.stem,
                source_path=source_path,
                copy_path=copy_path,
                sections=load_cross_sections(load_path),
            )
        )
    return groups


def _vector_between(
    start: Tuple[float, float],
    end: Tuple[float, float],
) -> Tuple[float, float]:
    return end[0] - start[0], end[1] - start[1]


def _unit_dot(
    a: Tuple[float, float],
    b: Tuple[float, float],
) -> float:
    norm_a = math.hypot(a[0], a[1])
    norm_b = math.hypot(b[0], b[1])
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return (a[0] * b[0] + a[1] * b[1]) / (norm_a * norm_b)


def classify_case_a_topology(
    groups: Sequence[CrossSectionGroup],
    config: RasMapperConfig,
) -> Dict[str, Any]:
    if len(groups) != 2:
        return {"model_type": "single", "selection_method": "not_applicable"}

    for group in groups:
        if len(group.sections) < 2:
            raise ValueError(
                "Case A junction detection requires at least two cross "
                f"sections in each CSV. Group '{group.name}' has "
                f"{len(group.sections)}."
            )

    rows = []
    for group in groups:
        previous_section = group.sections[-2]
        last_section = group.sections[-1]
        rows.append(
            {
                "group": group,
                "name": group.name,
                "previous": previous_section,
                "last": last_section,
                "flow_vector": _vector_between(
                    previous_section.mean_point,
                    last_section.mean_point,
                ),
            }
        )

    end_0 = rows[0]["last"].mean_point
    end_1 = rows[1]["last"].mean_point
    rows[0]["away_score"] = _unit_dot(
        rows[0]["flow_vector"],
        _vector_between(end_1, end_0),
    )
    rows[1]["away_score"] = _unit_dot(
        rows[1]["flow_vector"],
        _vector_between(end_0, end_1),
    )

    candidates = [
        row
        for row in rows
        if row["away_score"] >= config.branch_connectivity_threshold
    ]
    if len(candidates) != 1:
        report = {
            "model_type": "junction_case_a",
            "selection_method": "connectivity",
            "status": "ambiguous",
            "threshold": config.branch_connectivity_threshold,
            "branches": [
                {
                    "name": row["name"],
                    "away_score": row["away_score"],
                    "last_station": row["last"].station_label,
                    "last_mean_point": row["last"].mean_point,
                    "previous_station": row["previous"].station_label,
                }
                for row in rows
            ],
        }
        raise ValueError(
            "Could not identify a single downstream-continuing branch for "
            "Case A junction model. Provide a downstream BC CSV or adjust "
            "cross-section ordering. Details: "
            f"{json.dumps(_to_jsonable(report), indent=2)}"
        )

    downstream = candidates[0]
    tributary = rows[1] if downstream is rows[0] else rows[0]
    topology = {
        "model_type": "junction_case_a",
        "selection_method": "connectivity",
        "status": "selected",
        "threshold": config.branch_connectivity_threshold,
        "downstream_group": downstream["name"],
        "tributary_group": tributary["name"],
        "junction_point_estimate": (
            (
                tributary["last"].mean_point[0]
                + downstream["previous"].mean_point[0]
            )
            / 2.0,
            (
                tributary["last"].mean_point[1]
                + downstream["previous"].mean_point[1]
            )
            / 2.0,
        ),
        "branches": [
            {
                "name": row["name"],
                "away_score": row["away_score"],
                "first_station": row["group"].sections[0].station_label,
                "last_station": row["last"].station_label,
                "last_mean_point": row["last"].mean_point,
                "flow_vector": row["flow_vector"],
                "role": (
                    "downstream_continuing"
                    if row is downstream
                    else "tributary"
                ),
            }
            for row in rows
        ],
    }
    return topology


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


def normalize_dss_pathname(pathname: str) -> str:
    clean = str(pathname).strip().strip("/")
    if not clean:
        return ""
    parts = [
        part.strip()
        for part in clean.split("/")
    ]
    if len(parts) < 6:
        parts.extend([""] * (6 - len(parts)))
    parts = parts[:6]
    if not parts:
        return ""
    return "/" + "/".join(parts) + "/"


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
        row["pathname"] = normalize_dss_pathname(
            f"/{row['A']}/{row['B']}/{row['C']}/"
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


def choose_dss_paths_for_groups(
    catalog_df: pd.DataFrame,
    groups: Sequence[CrossSectionGroup],
    preferred_f_part: str,
    fallback_path: Optional[str],
) -> Dict[str, Optional[str]]:
    if catalog_df.empty:
        return {group.name: fallback_path for group in groups}

    a_parts = sorted(
        {str(value) for value in catalog_df["A"].dropna().unique()},
        key=len,
        reverse=True,
    )
    paths: Dict[str, Optional[str]] = {}
    for group in groups:
        search_text = " ".join(
            [
                group.name,
                group.source_path.stem,
                group.source_path.name,
            ]
        ).upper()
        selected_a = next(
            (
                a_part
                for a_part in a_parts
                if a_part.upper() in search_text
            ),
            None,
        )
        if not selected_a:
            paths[group.name] = fallback_path
            continue

        matches = catalog_df[catalog_df["A"].astype(str) == selected_a]
        if preferred_f_part:
            f_matches = matches[
                matches["F"].astype(str) == preferred_f_part
            ]
            if not f_matches.empty:
                paths[group.name] = str(f_matches.iloc[0]["pathname"])
                continue

        paths[group.name] = (
            str(matches.iloc[0]["pathname"]) if not matches.empty else fallback_path
        )
    return paths


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


def _result_profile_name(result_hdf: Path) -> str:
    """Return a stable result profile name for RAS Mapper map entries."""
    dataset = (
        "Results/Unsteady/Output/Output Blocks/Base Output/"
        "Unsteady Time Series/Time Date Stamp"
    )
    if not result_hdf.exists():
        return ""

    try:
        with h5py.File(result_hdf, "r") as hdf:
            if dataset not in hdf or len(hdf[dataset]) == 0:
                return ""
            raw_value = hdf[dataset][min(1, len(hdf[dataset]) - 1)]
    except OSError:
        return ""

    if isinstance(raw_value, bytes):
        return raw_value.decode("utf-8", errors="ignore").strip()
    return str(raw_value).strip()


def _ensure_plan_result_layers(config: RasMapperConfig) -> bool:
    """Register the computed plan/result HDF in the project .rasmap file."""
    result_hdf = config.project_root / (
        f"{config.project_name}.p{config.plan_id}.hdf"
    )
    if not config.rasmap_path.exists() or not result_hdf.exists():
        return False

    tree = ET.parse(config.rasmap_path)
    root = tree.getroot()
    results = root.find("Results")
    if results is None:
        results = ET.Element("Results")
        map_layers = root.find("MapLayers")
        if map_layers is not None:
            root.insert(list(root).index(map_layers), results)
        else:
            root.append(results)

    results.set("Checked", "True")
    results.set("Expanded", "True")

    result_filename = _relative_rasmap_filename(config.rasmap_path, result_hdf)

    for layer in list(results.findall("Layer")):
        filename = layer.get("Filename", "")
        layer_name = layer.get("Name", "")
        if (
            filename == result_filename
            or layer.get("Type") == "RASPlan"
            or layer_name == config.plan_short_identifier
        ):
            results.remove(layer)

    result_layer = ET.SubElement(results, "Layer")
    result_layer.set("Name", config.plan_short_identifier)
    result_layer.set("Type", "RASResults")
    result_layer.set("Checked", "True")
    result_layer.set("Expanded", "True")
    result_layer.set("Filename", result_filename)

    profile_name = _result_profile_name(result_hdf)
    for display_name, map_type, checked in (
        ("Depth", "depth", "True"),
        ("Velocity", "velocity", "False"),
        ("WSE", "elevation", "False"),
    ):
        map_layer = ET.SubElement(result_layer, "Layer")
        map_layer.set("Name", display_name)
        map_layer.set("Type", "RASResultsMap")
        map_layer.set("Checked", checked)
        params = ET.SubElement(map_layer, "MapParameters")
        params.set("MapType", map_type)
        params.set("ProfileIndex", "1")
        if profile_name:
            params.set("ProfileName", profile_name)

    tree.write(config.rasmap_path, encoding="utf-8", xml_declaration=False)
    return True


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
            variables[idx]["ManningsN"] = float(config.region_default_manning)
            variables[idx]["Percent Impervious"] = -9999.0
        else:
            variables[idx]["ManningsN"] = float(
                mannings_lookup.get(name, config.region_default_manning)
            )
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
    breaklines: Sequence[Sequence[Tuple[float, float]]],
    preferred_dss_path: Optional[str],
    cross_section_groups: Optional[Sequence[CrossSectionGroup]] = None,
    dss_paths_by_group: Optional[Dict[str, Optional[str]]] = None,
) -> pd.DataFrame:
    centroid = _polygon_centroid(perimeter_ring)
    storage_area_name = resolve_storage_area_name(config)
    boundary_rows: List[Dict[str, Any]] = []

    def upstream_coords(section: CrossSectionInfo) -> List[Tuple[float, float]]:
        limited_section = _limit_boundary_section_to_breaklines(
            section.points,
            section.mean_point,
            perimeter_ring,
            breaklines,
        )
        return _offset_polyline_outside_ring(
            limited_section,
            perimeter_ring,
            offset_distance=config.boundary_offset_distance,
        )

    def append_row(
        *,
        line_name: str,
        role: str,
        section: CrossSectionInfo,
        coords: List[Tuple[float, float]],
        method: str,
        branch_name: str,
        normal_depth_slope: Optional[float] = None,
        dss_path: Optional[str] = None,
    ) -> None:
        interior_side = _point_side_of_line(centroid, section.points)
        boundary_rows.append(
            {
                "bc_name": line_name,
                "bc_line_name": line_name,
                "role": role,
                "branch": branch_name,
                "station": section.station_label,
                "mean_z": round(section.mean_z, 3),
                "interior_side": interior_side,
                "reverse_if_interior_should_be_right": interior_side == "left",
                "reverse_if_interior_should_be_left": interior_side == "right",
                "method": method,
                "normal_depth_slope": normal_depth_slope,
                "dss_path": dss_path,
                "offset_distance": config.boundary_offset_distance,
                "storage_area_name": storage_area_name,
                "coords": coords,
            }
        )

    groups = list(cross_section_groups or [])
    topology: Dict[str, Any] = {"model_type": "single"}
    if len(groups) == 2:
        topology = classify_case_a_topology(groups, config)
        downstream_group_name = str(topology["downstream_group"])
        downstream_group = next(
            group for group in groups if group.name == downstream_group_name
        )
        downstream_section = downstream_group.sections[-1]
        downstream_slope = estimate_normal_depth_slope(downstream_group.sections)

        for index, group in enumerate(groups, start=1):
            section = group.sections[0]
            append_row(
                line_name=f"{config.upstream_bc_name} {index}",
                role="upstream",
                section=section,
                coords=upstream_coords(section),
                method="Flow Hydrograph",
                branch_name=group.name,
                dss_path=(
                    (dss_paths_by_group or {}).get(group.name)
                    or preferred_dss_path
                ),
            )

        append_row(
            line_name=config.downstream_bc_name,
            role="downstream",
            section=downstream_section,
            coords=_build_downstream_boundary_from_cross_section(
                downstream_section,
                perimeter_ring,
                boundary_offset_distance=config.boundary_offset_distance,
                length_multiplier=config.downstream_bc_length_multiplier,
            ),
            method=config.downstream_bc_method,
            branch_name=downstream_group.name,
            normal_depth_slope=downstream_slope,
        )
    else:
        downstream_slope = estimate_normal_depth_slope(sections)
        append_row(
            line_name=config.upstream_bc_name,
            role="upstream",
            section=sections[0],
            coords=upstream_coords(sections[0]),
            method="Flow Hydrograph",
            branch_name="main",
            dss_path=preferred_dss_path,
        )
        append_row(
            line_name=config.downstream_bc_name,
            role="downstream",
            section=sections[-1],
            coords=_build_downstream_boundary_from_cross_section(
                sections[-1],
                perimeter_ring,
                boundary_offset_distance=config.boundary_offset_distance,
                length_multiplier=config.downstream_bc_length_multiplier,
            ),
            method=config.downstream_bc_method,
            branch_name="main",
            normal_depth_slope=downstream_slope,
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
    writer.field("branch", "C", size=64)
    writer.field("storage", "C", size=32)

    for row in boundary_rows:
        coords = row["coords"]
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
            row["branch"][:64],
            row["storage_area_name"][:32],
        )
    writer.close()
    _write_prj_for_shapefile(config, config.boundary_shp)

    report_rows = [
        {key: value for key, value in row.items() if key != "coords"}
        for row in boundary_rows
    ]
    boundary_df = pd.DataFrame(report_rows)
    boundary_df.to_csv(config.boundary_csv, index=False)
    config.boundary_json.write_text(
        json.dumps(
            _to_jsonable(
                {
                    "topology": topology,
                    "boundary_rows": boundary_rows,
                }
            ),
            indent=2,
        ),
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
            `python working/rasmapper_v01.py apply-mannings`
        18. Then run:
            `python working/rasmapper_v01.py check-mannings`

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
    geom_token = config.geom_path.suffix.lstrip(".")
    lines = _upsert_project_line(
        lines,
        "Current Geom=",
        geom_token,
        insert_after_prefixes=(
            "Proj Title=",
        ),
    )
    lines = _upsert_project_line(
        lines,
        "Geom File=",
        geom_token,
        insert_after_prefixes=(
            "Current Geom=",
            "English Units=",
            "SI Units=",
            "Default Exp/Contr=",
        ),
    )
    config.project_file.write_text("".join(lines), encoding="utf-8")


def _upsert_line_after_prefix(
    lines: List[str],
    prefix: str,
    replacement: str,
    insert_after_prefixes: Sequence[str],
) -> List[str]:
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            lines[index] = replacement
            return lines

    insert_at = len(lines)
    for index, line in enumerate(lines):
        if any(line.startswith(candidate) for candidate in insert_after_prefixes):
            insert_at = index + 1
    lines.insert(insert_at, replacement)
    return lines


def _remove_lines_with_prefixes(
    lines: Sequence[str],
    prefixes: Sequence[str],
) -> List[str]:
    return [
        line
        for line in lines
        if not any(line.startswith(prefix) for prefix in prefixes)
    ]


def _bool_to_ras_flag(value: bool) -> int:
    return -1 if bool(value) else 0


def _format_2d_boundary_location(storage_area: str, bc_name: str) -> str:
    fields = [
        "",
        "",
        "",
        "",
        "",
        storage_area,
        "",
        bc_name,
        "",
    ]
    widths = [16, 16, 8, 8, 16, 16, 16, 32, 32]
    joined = ",".join(
        f"{str(value):<{widths[index]}}"
        for index, value in enumerate(fields)
    )
    return f"Boundary Location={joined}\n"


def _get_template_program_version(
    lines: Sequence[str],
    default: str = "6.60",
) -> str:
    for line in lines:
        if line.startswith("Program Version="):
            return line.split("=", 1)[1].strip() or default
    return default


def _parse_dss_path_parts(pathname: str) -> Dict[str, str]:
    parts = normalize_dss_pathname(pathname).strip("/").split("/")
    keys = ("A", "B", "C", "D", "E", "F")
    return {
        key: parts[index] if index < len(parts) else ""
        for index, key in enumerate(keys)
    }


def _read_dss_catalog_for_config(config: RasMapperConfig) -> pd.DataFrame:
    for catalog_path in (config.dss_catalog_csv, config.dss_catalog_copy):
        if catalog_path.exists():
            if catalog_path.suffix.lower() == ".csv":
                return pd.read_csv(catalog_path, dtype=str).fillna("")
            return parse_dss_catalog(catalog_path)
    return pd.DataFrame()


def resolve_dss_path_from_catalog(
    config: RasMapperConfig,
    pathname: str,
) -> str:
    normalized = normalize_dss_pathname(pathname)
    parts = _parse_dss_path_parts(normalized)
    if parts["D"]:
        return normalized

    catalog = _read_dss_catalog_for_config(config)
    required_columns = {"A", "B", "C", "D", "E", "F", "pathname"}
    if catalog.empty or not required_columns.issubset(catalog.columns):
        return normalized

    matches = catalog[
        (catalog["A"].astype(str) == parts["A"])
        & (catalog["B"].astype(str) == parts["B"])
        & (catalog["C"].astype(str) == parts["C"])
        & (catalog["E"].astype(str).str.upper() == parts["E"].upper())
        & (catalog["F"].astype(str).str.upper() == parts["F"].upper())
        & (catalog["D"].astype(str).str.strip() != "")
    ]
    if matches.empty:
        return normalized

    return normalize_dss_pathname(str(matches.iloc[0]["pathname"]))


def _parse_dss_d_part_date(d_part: str) -> datetime:
    clean = d_part.strip()
    for date_format in ("%d%b%Y", "%d%B%Y", "%d%b%y", "%d%B%y"):
        try:
            return datetime.strptime(clean.title(), date_format)
        except ValueError:
            continue
    raise ValueError(f"Could not parse DSS D-part date: {d_part!r}")


def _parse_hhmm_time(time_text: str) -> Tuple[int, int]:
    clean = str(time_text).strip().replace(":", "")
    if not clean:
        return 0, 0
    if len(clean) <= 2:
        clean = clean.zfill(2) + "00"
    clean = clean.zfill(4)
    return int(clean[:2]), int(clean[2:4])


def _format_hecras_date(dt_value: datetime) -> str:
    return dt_value.strftime("%d%b%Y")


def _format_hecras_time(dt_value: datetime) -> str:
    return dt_value.strftime("%H%M")


def _format_hecras_simulation_date(
    start_dt: datetime,
    end_dt: datetime,
) -> str:
    return (
        f"{_format_hecras_date(start_dt)},{_format_hecras_time(start_dt)},"
        f"{_format_hecras_date(end_dt)},{_format_hecras_time(end_dt)}"
    )


def resolve_plan_simulation_date(
    config: RasMapperConfig,
    upstream_dss_path: str,
) -> Dict[str, Any]:
    manual_value = str(config.plan_simulation_date).strip()
    if not config.auto_plan_simulation_date and manual_value not in ("", ",,,"):
        return {
            "simulation_date": manual_value,
            "source": "manual_config",
            "upstream_dss_path": upstream_dss_path,
        }

    dss_parts = _parse_dss_path_parts(upstream_dss_path)
    if not dss_parts["D"]:
        raise ValueError(
            "Upstream DSS path is missing the D-part date: "
            f"{upstream_dss_path}. Rerun 'prepare' so boundary_candidates.csv "
            "can be rebuilt from the DSS catalog."
        )
    start_date = _parse_dss_d_part_date(dss_parts["D"])
    start_hour, start_minute = _parse_hhmm_time(config.simulation_start_time)
    start_dt = start_date.replace(
        hour=start_hour,
        minute=start_minute,
        second=0,
        microsecond=0,
    )
    start_dt += timedelta(hours=float(config.simulation_start_offset_hours))
    end_dt = start_dt + timedelta(hours=float(config.simulation_duration_hours))
    end_dt += timedelta(hours=float(config.simulation_end_offset_hours))

    return {
        "simulation_date": _format_hecras_simulation_date(start_dt, end_dt),
        "source": "dss_path_d_part",
        "start_datetime": start_dt,
        "end_datetime": end_dt,
        "duration_hours": float(config.simulation_duration_hours),
        "start_offset_hours": float(config.simulation_start_offset_hours),
        "end_offset_hours": float(config.simulation_end_offset_hours),
        "upstream_dss_path": upstream_dss_path,
        "dss_parts": dss_parts,
    }


def _extract_unsteady_template_tail(lines: Sequence[str]) -> List[str]:
    for index, line in enumerate(lines):
        if line.startswith(
            (
                "Met Point Raster Parameters=",
                "Precipitation Mode=",
                "Wind Mode=",
                "Air Density Mode=",
            )
        ):
            return list(lines[index:])

    return [
        "Met Point Raster Parameters=,,,,\n",
        "Precipitation Mode=Disable\n",
        "Wind Mode=No Wind Forces\n",
        "Air Density Mode=Specified\n",
        "Met BC=Precipitation|Expanded View=0\n",
        "Met BC=Precipitation|Point Interpolation=Nearest\n",
        "Met BC=Precipitation|Gridded Source=DSS\n",
        "Met BC=Air Density|Mode=Constant\n",
        "Met BC=Air Density|Expanded View=0\n",
        "Met BC=Air Density|Constant Value=1.225\n",
        "Met BC=Air Density|Constant Units=kg/m3\n",
        "Met BC=Air Pressure|Mode=Constant\n",
        "Met BC=Air Pressure|Expanded View=0\n",
        "Met BC=Air Pressure|Constant Value=1013.2\n",
        "Met BC=Air Pressure|Constant Units=mb\n",
    ]


def _load_boundary_config_values(config: RasMapperConfig) -> Dict[str, Any]:
    if not config.boundary_csv.exists():
        raise FileNotFoundError(
            f"Boundary candidate CSV not found: {config.boundary_csv}. "
            "Run 'prepare' before creating the unsteady plan."
        )

    boundary_df = pd.read_csv(config.boundary_csv)
    if "role" not in boundary_df.columns:
        raise ValueError(
            f"Boundary candidate CSV missing 'role' column: {config.boundary_csv}"
        )

    upstream = boundary_df[boundary_df["role"].astype(str) == "upstream"]
    downstream = boundary_df[boundary_df["role"].astype(str) == "downstream"]
    if upstream.empty:
        raise ValueError("No upstream boundary candidate found.")
    if downstream.empty:
        raise ValueError("No downstream boundary candidate found.")

    downstream_row = downstream.iloc[0]
    upstream_rows: List[Dict[str, Any]] = []
    for _, upstream_row in upstream.iterrows():
        raw_dss_path = str(upstream_row.get("dss_path", "")).strip()
        if not raw_dss_path or raw_dss_path.lower() == "nan":
            raise ValueError(
                "No upstream DSS path was selected for boundary "
                f"{upstream_row.get('bc_line_name', upstream_row.get('bc_name'))}. "
                "Check preferred_dss_a_part and preferred_dss_f_part, then "
                "rerun 'prepare'."
            )
        row_dict = upstream_row.to_dict()
        row_dict["dss_path"] = resolve_dss_path_from_catalog(
            config,
            raw_dss_path,
        )
        upstream_rows.append(row_dict)

    if config.downstream_friction_slope is not None:
        downstream_slope = float(config.downstream_friction_slope)
    else:
        raw_slope = downstream_row.get("normal_depth_slope", "")
        if pd.isna(raw_slope) or str(raw_slope).strip() == "":
            sections = load_cross_sections(config.cross_section_copy)
            downstream_slope = estimate_normal_depth_slope(sections)
        else:
            downstream_slope = float(raw_slope)

    return {
        "upstream_dss_path": upstream_rows[0]["dss_path"],
        "upstream_rows": upstream_rows,
        "downstream_row": downstream_row.to_dict(),
        "downstream_slope": downstream_slope,
        "boundary_rows": boundary_df.to_dict("records"),
    }


def _relative_project_path(path: Path, project_root: Path) -> str:
    try:
        rel = path.resolve().relative_to(project_root.resolve())
        return ".\\" + str(rel).replace("/", "\\")
    except ValueError:
        return str(path)


def _write_minimal_unsteady_hdf(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with h5py.File(path, "w") as handle:
        met_group = handle.require_group("Event Conditions/Meteorology")
        for name in (
            "Air Density",
            "Air Pressure",
            "Air Temperature",
            "Evapotranspiration",
            "Humidity",
            "Precipitation",
            "Wind Direction",
            "Wind Speed",
            "Wind Velocity X",
            "Wind Velocity Y",
        ):
            met_group.require_group(name)


def create_unsteady_file(config: RasMapperConfig) -> Dict[str, Any]:
    boundary_values = _load_boundary_config_values(config)
    template_lines: List[str] = []
    if config.template_unsteady_src and config.template_unsteady_src.exists():
        template_lines = config.template_unsteady_src.read_text(
            encoding="utf-8",
            errors="replace",
        ).splitlines(True)

    program_version = _get_template_program_version(template_lines)
    storage_area_name = resolve_storage_area_name(config)
    dss_file = (
        config.unsteady_dss_file_relative
        or _relative_project_path(config.dss_copy, config.project_root)
    )

    downstream_row = boundary_values["downstream_row"]
    downstream_bc_name = str(
        downstream_row.get("bc_line_name")
        or downstream_row.get("bc_name")
        or config.downstream_bc_name
    )
    flow_hydrograph_blocks: List[str] = []
    for upstream_row in boundary_values["upstream_rows"]:
        upstream_bc_name = str(
            upstream_row.get("bc_line_name")
            or upstream_row.get("bc_name")
            or config.upstream_bc_name
        )
        flow_hydrograph_blocks.extend(
            [
                _format_2d_boundary_location(
                    storage_area_name,
                    upstream_bc_name,
                ),
                f"Interval={config.upstream_flow_interval}\n",
                "Flow Hydrograph= 0 \n",
                "Stage Hydrograph TW Check=0\n",
                (
                    "Flow Hydrograph Slope= "
                    f"{config.upstream_flow_hydrograph_slope:.6g} \n"
                ),
                f"DSS File={dss_file}\n",
                f"DSS Path={upstream_row['dss_path']}\n",
                "Use DSS=True\n",
                "Use Fixed Start Time=False\n",
                "Fixed Start Date/Time=,\n",
                "Is Critical Boundary=False\n",
                "Critical Boundary Flow=\n",
            ]
        )

    lines = [
        f"Flow Title={config.unsteady_title[:32]}\n",
        f"Program Version={program_version}\n",
        "Use Restart= 0 \n",
        _format_2d_boundary_location(
            storage_area_name,
            downstream_bc_name,
        ),
        f"Friction Slope={boundary_values['downstream_slope']:.6g},0\n",
        *flow_hydrograph_blocks,
        *_extract_unsteady_template_tail(template_lines),
    ]

    config.unsteady_path.write_text("".join(lines), encoding="utf-8")

    if (
        config.template_unsteady_hdf_src
        and config.template_unsteady_hdf_src.exists()
    ):
        copy_file(config.template_unsteady_hdf_src, config.unsteady_hdf_path)
    else:
        _write_minimal_unsteady_hdf(config.unsteady_hdf_path)

    return {
        "unsteady_path": config.unsteady_path,
        "unsteady_hdf_path": config.unsteady_hdf_path,
        "storage_area_name": storage_area_name,
        "upstream_bc_name": config.upstream_bc_name,
        "upstream_bc_names": [
            str(row.get("bc_line_name") or row.get("bc_name"))
            for row in boundary_values["upstream_rows"]
        ],
        "downstream_bc_name": downstream_bc_name,
        "upstream_dss_path": boundary_values["upstream_dss_path"],
        "upstream_dss_paths": [
            row["dss_path"] for row in boundary_values["upstream_rows"]
        ],
        "dss_file": dss_file,
        "downstream_slope": boundary_values["downstream_slope"],
        "template_unsteady": config.template_unsteady_src,
        "template_unsteady_hdf": config.template_unsteady_hdf_src,
    }


def _minimal_plan_lines(
    config: RasMapperConfig,
    simulation_date: str,
) -> List[str]:
    hydrograph_output_interval = (
        config.plan_hydrograph_output_interval
        or config.plan_output_interval
    )
    detailed_output_interval = (
        config.plan_detailed_output_interval
        or config.plan_instantaneous_interval
    )
    return [
        f"Plan Title={config.plan_title[:64]}\n",
        "Program Version=6.60\n",
        f"Short Identifier={config.plan_short_identifier[:24]}\n",
        f"Simulation Date={simulation_date}\n",
        "Geom File=g01\n",
        f"Flow File=u{config.unsteady_id}\n",
        f"{config.plan_flow_regime}\n",
        f"Computation Interval={config.plan_computation_interval}\n",
        f"Output Interval={hydrograph_output_interval}\n",
        f"Instantaneous Interval={detailed_output_interval}\n",
        f"Mapping Interval={config.plan_mapping_interval}\n",
        "Computation Time Step Use Courant=        "
        f"{_bool_to_ras_flag(config.plan_use_courant_timestep)}\n",
        "Computation Time Step Use Time Series=    "
        f"{_bool_to_ras_flag(config.plan_use_time_series_timestep)}\n",
        f"Computation Time Step Max Courant={config.plan_max_courant:g}\n",
        f"Computation Time Step Min Courant={config.plan_min_courant:g}\n",
        "Computation Time Step Count To Double="
        f"{int(config.plan_steps_below_min_before_doubling)}\n",
        "Computation Time Step Max Doubling="
        f"{int(config.plan_max_doubling_base_timestep)}\n",
        "Computation Time Step Max Halving="
        f"{int(config.plan_max_halving_base_timestep)}\n",
        f"Computation Time Step Residence Courant={config.plan_residence_courant:g}\n",
        f"Run HTab= {_bool_to_ras_flag(config.plan_run_htab)} \n",
        f"Run UNet= {_bool_to_ras_flag(config.plan_run_unet)}\n",
        "Run Sediment= 0\n",
        f"Run PostProcess= {_bool_to_ras_flag(config.plan_run_postprocess)}\n",
        "Run WQNet= 0 \n",
        f"Run RASMapper= {_bool_to_ras_flag(config.plan_run_rasmapper)}\n",
        "UNET Theta= 1 \n",
        "UNET D2 Theta= 1 \n",
        "UNET D2 SolverType=PARDISO (Direct)\n",
        f"UNET D1 Cores= {int(config.plan_num_cores)} \n",
        f"UNET D2 Cores= {int(config.plan_num_cores)} \n",
        f"PS Cores={int(config.plan_num_cores)}\n",
    ]


def _set_plan_line(
    lines: List[str],
    prefix: str,
    value: str,
    insert_after_prefixes: Sequence[str],
) -> List[str]:
    return _upsert_line_after_prefix(
        lines,
        prefix,
        f"{prefix}{value}\n",
        insert_after_prefixes,
    )


def create_plan_file(
    config: RasMapperConfig,
    simulation_date: str,
) -> Dict[str, Any]:
    hydrograph_output_interval = (
        config.plan_hydrograph_output_interval
        or config.plan_output_interval
    )
    detailed_output_interval = (
        config.plan_detailed_output_interval
        or config.plan_instantaneous_interval
    )
    if config.template_plan_src and config.template_plan_src.exists():
        lines = config.template_plan_src.read_text(
            encoding="utf-8",
            errors="replace",
        ).splitlines(True)
    else:
        lines = _minimal_plan_lines(config, simulation_date)

    lines = _remove_lines_with_prefixes(lines, ("Flow File=", "Unsteady File="))
    lines = [
        line
        for line in lines
        if line.strip()
        not in ("Subcritical Flow", "Supercritical Flow", "Mixed Flow")
    ]
    lines = _set_plan_line(lines, "Plan Title=", config.plan_title[:64], ())
    lines = _set_plan_line(
        lines,
        "Short Identifier=",
        config.plan_short_identifier[:24],
        ("Plan Title=",),
    )
    lines = _set_plan_line(
        lines,
        "Simulation Date=",
        simulation_date,
        ("Short Identifier=",),
    )
    lines = _set_plan_line(lines, "Geom File=", "g01", ("Simulation Date=",))
    lines = _set_plan_line(
        lines,
        "Flow File=",
        f"u{config.unsteady_id}",
        ("Geom File=",),
    )
    lines = _upsert_line_after_prefix(
        lines,
        config.plan_flow_regime,
        f"{config.plan_flow_regime}\n",
        ("Flow File=",),
    )
    lines = _set_plan_line(
        lines,
        "Computation Interval=",
        config.plan_computation_interval,
        ("Flow File=", "Geom File="),
    )
    lines = _set_plan_line(
        lines,
        "Output Interval=",
        hydrograph_output_interval,
        ("Computation Interval=",),
    )
    lines = _set_plan_line(
        lines,
        "Instantaneous Interval=",
        detailed_output_interval,
        ("Output Interval=",),
    )
    lines = _set_plan_line(
        lines,
        "Mapping Interval=",
        config.plan_mapping_interval,
        ("Instantaneous Interval=", "Output Interval="),
    )
    lines = _set_plan_line(
        lines,
        "Computation Time Step Use Courant=",
        f"        {_bool_to_ras_flag(config.plan_use_courant_timestep)}",
        ("Mapping Interval=",),
    )
    lines = _set_plan_line(
        lines,
        "Computation Time Step Use Time Series=",
        f"    {_bool_to_ras_flag(config.plan_use_time_series_timestep)}",
        ("Computation Time Step Use Courant=",),
    )
    lines = _set_plan_line(
        lines,
        "Computation Time Step Max Courant=",
        f"{config.plan_max_courant:g}",
        ("Computation Time Step Use Time Series=",),
    )
    lines = _set_plan_line(
        lines,
        "Computation Time Step Min Courant=",
        f"{config.plan_min_courant:g}",
        ("Computation Time Step Max Courant=",),
    )
    lines = _set_plan_line(
        lines,
        "Computation Time Step Count To Double=",
        str(int(config.plan_steps_below_min_before_doubling)),
        ("Computation Time Step Min Courant=",),
    )
    lines = _set_plan_line(
        lines,
        "Computation Time Step Max Doubling=",
        str(int(config.plan_max_doubling_base_timestep)),
        ("Computation Time Step Count To Double=",),
    )
    lines = _set_plan_line(
        lines,
        "Computation Time Step Max Halving=",
        str(int(config.plan_max_halving_base_timestep)),
        ("Computation Time Step Max Doubling=",),
    )
    lines = _set_plan_line(
        lines,
        "Computation Time Step Residence Courant=",
        f"{config.plan_residence_courant:g}",
        ("Computation Time Step Max Halving=",),
    )
    lines = _set_plan_line(
        lines,
        "Run HTab=",
        f" {_bool_to_ras_flag(config.plan_run_htab)} ",
        ("Computation Time Step Residence Courant=", "Mapping Interval="),
    )
    lines = _set_plan_line(
        lines,
        "Run UNet=",
        f" {_bool_to_ras_flag(config.plan_run_unet)}",
        ("Run HTab=",),
    )
    lines = _set_plan_line(
        lines,
        "Run PostProcess=",
        f" {_bool_to_ras_flag(config.plan_run_postprocess)}",
        ("Run Sediment=", "Run UNet="),
    )
    lines = _set_plan_line(
        lines,
        "Run RASMapper=",
        f" {_bool_to_ras_flag(config.plan_run_rasmapper)}",
        ("Run WQNet=", "Run PostProcess="),
    )
    for prefix in ("UNET D1 Cores=", "UNET D2 Cores=", "PS Cores="):
        lines = _set_plan_line(
            lines,
            prefix,
            f" {int(config.plan_num_cores)} ",
            ("Run RASMapper=", "Run UNet="),
        )

    config.plan_path.write_text("".join(lines), encoding="utf-8")
    return {
        "plan_path": config.plan_path,
        "template_plan": config.template_plan_src,
        "plan_title": config.plan_title,
        "plan_short_identifier": config.plan_short_identifier,
        "simulation_date": simulation_date,
        "geom_file": "g01",
        "flow_file": f"u{config.unsteady_id}",
        "plan_flow_regime": config.plan_flow_regime,
        "computation_interval": config.plan_computation_interval,
        "hydrograph_output_interval": hydrograph_output_interval,
        "mapping_output_interval": config.plan_mapping_interval,
        "detailed_output_interval": detailed_output_interval,
        "use_courant_timestep": config.plan_use_courant_timestep,
        "max_courant": config.plan_max_courant,
        "min_courant": config.plan_min_courant,
        "steps_below_min_before_doubling": (
            config.plan_steps_below_min_before_doubling
        ),
        "max_doubling_base_timestep": (
            config.plan_max_doubling_base_timestep
        ),
        "max_halving_base_timestep": (
            config.plan_max_halving_base_timestep
        ),
        "run_htab": _bool_to_ras_flag(config.plan_run_htab),
        "run_unet": _bool_to_ras_flag(config.plan_run_unet),
        "run_rasmapper": _bool_to_ras_flag(config.plan_run_rasmapper),
    }


def ensure_project_has_unsteady_plan_references(
    config: RasMapperConfig,
) -> None:
    ensure_project_has_geom_reference(config)
    lines = config.project_file.read_text(encoding="utf-8").splitlines(True)
    lines = _upsert_project_line(
        lines,
        "Current Plan=",
        f"p{config.plan_id}",
        insert_after_prefixes=("Proj Title=",),
    )
    lines = _upsert_project_line(
        lines,
        "Unsteady File=",
        f"u{config.unsteady_id}",
        insert_after_prefixes=("Geom File=",),
    )
    lines = _upsert_project_line(
        lines,
        "Unsteady Title=",
        config.unsteady_title[:64],
        insert_after_prefixes=("Unsteady File=",),
    )
    lines = _upsert_project_line(
        lines,
        "Plan File=",
        f"p{config.plan_id}",
        insert_after_prefixes=("Unsteady Title=", "Unsteady File="),
    )
    lines = _upsert_project_line(
        lines,
        "Plan Title=",
        config.plan_title[:64],
        insert_after_prefixes=("Plan File=",),
    )
    config.project_file.write_text("".join(lines), encoding="utf-8")


def verify_unsteady_plan(config: RasMapperConfig) -> Dict[str, Any]:
    project_text = config.project_file.read_text(
        encoding="utf-8",
        errors="replace",
    )
    plan_text = config.plan_path.read_text(
        encoding="utf-8",
        errors="replace",
    )
    unsteady_text = config.unsteady_path.read_text(
        encoding="utf-8",
        errors="replace",
    )

    return {
        "project_has_current_plan": (
            f"Current Plan=p{config.plan_id}" in project_text
        ),
        "project_has_plan_entry": (
            f"Plan File=p{config.plan_id}" in project_text
        ),
        "project_has_unsteady_entry": (
            f"Unsteady File=u{config.unsteady_id}" in project_text
        ),
        "plan_has_geometry": "Geom File=g01" in plan_text,
        "plan_has_unsteady_flow_file": (
            f"Flow File=u{config.unsteady_id}" in plan_text
        ),
        "plan_has_no_steady_flow_file": "Flow File=f" not in plan_text,
        "plan_run_unet": any(
            line.startswith("Run UNet=") and "-1" in line
            for line in plan_text.splitlines()
        ),
        "unsteady_has_upstream_bc": config.upstream_bc_name in unsteady_text,
        "unsteady_has_downstream_bc": config.downstream_bc_name in unsteady_text,
        "unsteady_uses_dss": "Use DSS=True" in unsteady_text,
        "unsteady_hdf_exists": config.unsteady_hdf_path.exists(),
    }


def repair_unsteady_dss_paths(
    config: RasMapperConfig,
    unsteady_path: Path,
) -> Dict[str, Any]:
    if not unsteady_path.exists():
        return {"updated": False, "path": unsteady_path, "replacements": []}

    lines = unsteady_path.read_text(
        encoding="utf-8",
        errors="replace",
    ).splitlines(True)
    replacements: List[Dict[str, str]] = []
    repaired_lines: List[str] = []
    for line in lines:
        if not line.startswith("DSS Path="):
            repaired_lines.append(line)
            continue
        raw_path = line.split("=", 1)[1].strip()
        repaired_path = resolve_dss_path_from_catalog(config, raw_path)
        if repaired_path != raw_path:
            replacements.append({"from": raw_path, "to": repaired_path})
            repaired_lines.append(f"DSS Path={repaired_path}\n")
        else:
            repaired_lines.append(line)

    if replacements:
        unsteady_path.write_text("".join(repaired_lines), encoding="utf-8")

    return {
        "updated": bool(replacements),
        "path": unsteady_path,
        "replacements": replacements,
    }


def create_unsteady_plan(config: RasMapperConfig) -> Dict[str, Any]:
    validate_inputs(config)
    if not config.project_file.exists():
        raise FileNotFoundError(
            f"Project file not found: {config.project_file}. Run 'prepare' first."
        )
    if not config.geom_path.exists() or not config.geom_hdf_path.exists():
        raise FileNotFoundError(
            "Geometry files are missing. Run 'install-geometry' before "
            "creating the unsteady plan."
        )
    if config.template_unsteady_src and not config.template_unsteady_src.exists():
        raise FileNotFoundError(
            f"Template unsteady file not found: {config.template_unsteady_src}"
        )
    if (
        config.template_unsteady_hdf_src
        and not config.template_unsteady_hdf_src.exists()
    ):
        raise FileNotFoundError(
            "Template unsteady HDF file not found: "
            f"{config.template_unsteady_hdf_src}"
        )
    if config.template_plan_src and not config.template_plan_src.exists():
        raise FileNotFoundError(
            f"Template plan file not found: {config.template_plan_src}"
        )

    unsteady_summary = create_unsteady_file(config)
    dss_repair = repair_unsteady_dss_paths(config, config.unsteady_path)
    simulation_date_summary = resolve_plan_simulation_date(
        config,
        unsteady_summary["upstream_dss_path"],
    )
    plan_summary = create_plan_file(
        config,
        simulation_date_summary["simulation_date"],
    )
    ensure_project_has_unsteady_plan_references(config)
    verification = verify_unsteady_plan(config)

    summary = {
        "project_file": config.project_file,
        "unsteady": unsteady_summary,
        "dss_repair": dss_repair,
        "plan": plan_summary,
        "simulation_date": simulation_date_summary,
        "verification": verification,
    }
    config.unsteady_plan_summary_json.write_text(
        json.dumps(_to_jsonable(summary), indent=2),
        encoding="utf-8",
    )
    return summary


def _collect_ras_window_text(window: Any) -> List[str]:
    texts: List[str] = []
    title = str(window.window_text()).strip()
    if title:
        texts.append(title)
    try:
        descendants = window.descendants()
    except Exception:
        return texts

    for child in descendants:
        try:
            child_text = str(child.window_text()).strip()
            if child_text:
                texts.append(child_text)
            if child.class_name().endswith("ListBox"):
                for item_text in child.texts():
                    item_text = str(item_text).strip()
                    if item_text:
                        texts.append(item_text)
        except Exception:
            continue
    return texts


def _click_ras_button(window: Any, labels: Sequence[str]) -> bool:
    normalized = {label.lower().replace("&", "") for label in labels}
    try:
        descendants = window.descendants()
    except Exception:
        return False

    for child in descendants:
        try:
            child_key = str(child.window_text()).strip().lower()
            child_key = child_key.replace("&", "")
            if child_key not in normalized:
                continue
            child.click()
            return True
        except Exception:
            continue
    return False


def _collect_win32_window_text(hwnd: int) -> List[str]:
    import win32gui

    texts: List[str] = []
    title = win32gui.GetWindowText(hwnd).strip()
    if title:
        texts.append(title)

    def enum_child(child_hwnd: int, _: Any) -> None:
        text = win32gui.GetWindowText(child_hwnd).strip()
        if text:
            texts.append(text)

    try:
        win32gui.EnumChildWindows(hwnd, enum_child, None)
    except Exception:
        pass
    return texts


def _click_win32_button(hwnd: int, labels: Sequence[str]) -> bool:
    import win32con
    import win32gui

    normalized = {label.lower().replace("&", "") for label in labels}
    clicked = False

    def enum_child(child_hwnd: int, _: Any) -> None:
        nonlocal clicked
        if clicked:
            return
        text = win32gui.GetWindowText(child_hwnd).strip().lower()
        text = text.replace("&", "")
        if text not in normalized:
            return
        win32gui.SendMessage(child_hwnd, win32con.BM_CLICK, 0, 0)
        clicked = True

    try:
        enum_child(hwnd, None)
        win32gui.EnumChildWindows(hwnd, enum_child, None)
    except Exception:
        return False
    return clicked


def _handle_compute_dialogs_win32(
    process_id: int,
    auto_confirm_preprocessor: bool,
    messages: List[str],
) -> Optional[str]:
    try:
        import win32gui
        import win32process
    except Exception:
        return None

    fatal_message: Optional[str] = None

    def enum_window(hwnd: int, _: Any) -> None:
        nonlocal fatal_message
        try:
            _, window_pid = win32process.GetWindowThreadProcessId(hwnd)
            if window_pid != process_id or not win32gui.IsWindowVisible(hwnd):
                return
        except Exception:
            return

        texts = _collect_win32_window_text(hwnd)
        joined = "\n".join(texts)
        lower_joined = joined.lower()
        if not joined:
            return

        if "geometry preprocessor output file was not found" in lower_joined:
            if auto_confirm_preprocessor:
                if _click_win32_button(hwnd, ("Yes", "&Yes")):
                    messages.append(
                        "Accepted HEC-RAS geometry preprocessor prompt."
                    )
            return

        if "error parsing command line parameters" in lower_joined:
            fatal_message = joined
            _click_win32_button(hwnd, ("OK", "&OK", "Close"))
            return

        if "errors were found preparing unsteady flow data" in lower_joined:
            fatal_message = joined
            _click_win32_button(hwnd, ("Close", "OK", "&OK"))
            return

    try:
        win32gui.EnumWindows(enum_window, None)
    except Exception:
        return fatal_message
    return fatal_message


def _handle_compute_dialogs(
    process_id: int,
    auto_confirm_preprocessor: bool,
    messages: List[str],
) -> Optional[str]:
    try:
        from pywinauto import Desktop
    except Exception:
        return _handle_compute_dialogs_win32(
            process_id,
            auto_confirm_preprocessor,
            messages,
        )

    fatal_message: Optional[str] = None
    try:
        windows = Desktop(backend="win32").windows()
    except Exception:
        return None

    for window in windows:
        try:
            if window.process_id() != process_id or not window.is_visible():
                continue
        except Exception:
            continue

        texts = _collect_ras_window_text(window)
        joined = "\n".join(texts)
        lower_joined = joined.lower()
        if not joined:
            continue

        if "geometry preprocessor output file was not found" in lower_joined:
            if auto_confirm_preprocessor:
                if _click_ras_button(window, ("Yes", "&Yes")):
                    messages.append(
                        "Accepted HEC-RAS geometry preprocessor prompt."
                    )
            continue

        if "error parsing command line parameters" in lower_joined:
            fatal_message = joined
            _click_ras_button(window, ("OK", "&OK", "Close"))
            continue

        if "errors were found preparing unsteady flow data" in lower_joined:
            fatal_message = joined
            _click_ras_button(window, ("Close", "OK", "&OK"))
            continue

    return fatal_message


def _terminate_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _plan_data_errors_path(plan_file: Path) -> Path:
    return plan_file.with_name(plan_file.name + ".data_errors.txt")


def _read_plan_simulation_window(plan_file: Path) -> Tuple[datetime, datetime]:
    for line in plan_file.read_text(
        encoding="utf-8",
        errors="replace",
    ).splitlines():
        if not line.startswith("Simulation Date="):
            continue
        parts = [part.strip() for part in line.split("=", 1)[1].split(",")]
        if len(parts) != 4:
            break
        start = _parse_dss_d_part_date(parts[0])
        end = _parse_dss_d_part_date(parts[2])
        start_hour, start_minute = _parse_hhmm_time(parts[1])
        end_hour, end_minute = _parse_hhmm_time(parts[3])
        start = start.replace(hour=start_hour, minute=start_minute)
        end = end.replace(hour=end_hour, minute=end_minute)
        return start, end
    raise ValueError(f"Could not parse Simulation Date in {plan_file}")


def _write_plan_simulation_window(
    plan_file: Path,
    start_dt: datetime,
    end_dt: datetime,
) -> str:
    simulation_date = _format_hecras_simulation_date(start_dt, end_dt)
    lines = plan_file.read_text(
        encoding="utf-8",
        errors="replace",
    ).splitlines(True)
    lines = _set_plan_line(
        lines,
        "Simulation Date=",
        simulation_date,
        ("Short Identifier=",),
    )
    plan_file.write_text("".join(lines), encoding="utf-8")
    return simulation_date


def _parse_hecras_data_error_time(
    text: str,
    point_name: str,
) -> Optional[datetime]:
    pattern = (
        rf"{point_name}\s+time\s+series\s+point\s+is\s+at\s*:\s*"
        r"(\d{1,2}[A-Za-z]{3}\d{4})\s+(\d{1,2}:\d{2}:\d{2})"
    )
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if not match:
        return None
    date_part, time_part = match.groups()
    base = _parse_dss_d_part_date(date_part)
    hour, minute = _parse_hhmm_time(time_part[:5])
    return base.replace(hour=hour, minute=minute)


def _copy_compute_results_to_project(
    config: RasMapperConfig,
    compute_dir: Path,
) -> List[Path]:
    stems = [
        f"{config.project_name}.p{config.plan_id}",
        f"{config.project_name}.g01",
        f"{config.project_name}.b{config.plan_id}",
        f"{config.project_name}.bco{config.plan_id}",
        f"{config.project_name}.x{config.plan_id}",
        f"{config.project_name}.ic.o{config.plan_id}",
        f"{config.project_name}.dss",
    ]
    suffixes = ("", ".hdf")
    copied: List[Path] = []
    for stem in stems:
        for suffix in suffixes:
            source = compute_dir / f"{stem}{suffix}"
            if not source.exists() or not source.is_file():
                continue
            destination = config.project_root / source.name
            copy_file(source, destination)
            copied.append(destination)
    return copied


@log_call
def compute_plan(
    config: RasMapperConfig,
    output_dir: Optional[Path] = None,
    overwrite: bool = False,
    timeout_seconds: Optional[int] = None,
) -> Dict[str, Any]:
    validate_inputs(config)
    if not config.project_file.exists():
        raise FileNotFoundError(
            f"Project file not found: {config.project_file}. "
            "Run 'prepare' first."
        )
    if not config.plan_path.exists():
        raise FileNotFoundError(
            f"Plan file not found: {config.plan_path}. "
            "Run 'create-unsteady-plan' first."
        )

    geometry_sync = sync_geometry_to_source_shapes(config)

    destination = (
        output_dir.resolve()
        if output_dir is not None
        else config.default_compute_output_dir
    )
    if destination.exists():
        if not overwrite:
            raise FileExistsError(
                f"Compute output folder already exists: {destination}. "
                "Use --overwrite to replace it."
            )
        shutil.rmtree(destination)

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(config.project_root, destination)

    project_file = destination / config.project_file.name
    plan_file = destination / config.plan_path.name
    unsteady_file = destination / config.unsteady_path.name
    dss_repair = repair_unsteady_dss_paths(config, unsteady_file)
    result_hdf = destination / f"{config.project_name}.p{config.plan_id}.hdf"
    command = f'"{config.ras_exe}" -c "{project_file}" "{plan_file}"'
    timeout = (
        int(timeout_seconds)
        if timeout_seconds is not None
        else int(config.compute_timeout_seconds)
    )

    messages: List[str] = []
    fatal_message: Optional[str] = None
    attempt_summaries: List[Dict[str, Any]] = []
    start_time = time.time()
    return_code: Optional[int] = None
    success = False
    max_attempts = (
        4 if config.auto_adjust_simulation_window_from_dss else 1
    )

    for attempt in range(1, max_attempts + 1):
        data_errors_path = _plan_data_errors_path(plan_file)
        if data_errors_path.exists():
            data_errors_path.unlink()

        attempt_start = time.time()
        process = subprocess.Popen(command, cwd=str(destination))
        attempt_fatal: Optional[str] = None

        while process.poll() is None:
            attempt_fatal = _handle_compute_dialogs(
                process.pid,
                config.auto_confirm_geometry_preprocessor,
                messages,
            ) or attempt_fatal
            if attempt_fatal:
                time.sleep(2)
                _terminate_process(process)
                break
            if time.time() - start_time > timeout:
                attempt_fatal = (
                    f"HEC-RAS compute timed out after {timeout} seconds."
                )
                _terminate_process(process)
                break
            time.sleep(2)

        return_code = process.poll()
        data_error_text = ""
        if data_errors_path.exists():
            data_error_text = data_errors_path.read_text(
                encoding="utf-8",
                errors="replace",
            )

        attempt_summaries.append(
            {
                "attempt": attempt,
                "return_code": return_code,
                "elapsed_seconds": time.time() - attempt_start,
                "fatal_message": attempt_fatal,
                "data_errors": data_error_text,
                "simulation_date": next(
                    (
                        line.split("=", 1)[1].strip()
                        for line in plan_file.read_text(
                            encoding="utf-8",
                            errors="replace",
                        ).splitlines()
                        if line.startswith("Simulation Date=")
                    ),
                    "",
                ),
            }
        )

        if return_code == 0 and result_hdf.exists() and not attempt_fatal:
            success = True
            fatal_message = None
            break

        combined_error = "\n".join(
            text for text in (attempt_fatal, data_error_text) if text
        )
        fatal_message = combined_error or attempt_fatal
        if not config.auto_adjust_simulation_window_from_dss:
            break

        try:
            current_start, current_end = _read_plan_simulation_window(plan_file)
        except ValueError:
            break

        first_time = _parse_hecras_data_error_time(
            combined_error,
            "first",
        )
        last_time = _parse_hecras_data_error_time(combined_error, "last")

        adjusted = False
        if first_time and first_time > current_start:
            duration = current_end - current_start
            current_start = first_time
            current_end = max(current_end, current_start + duration)
            adjusted = True
        elif last_time and last_time < current_end:
            current_end = last_time
            adjusted = True
        elif "no data read from dss pathname" in combined_error.lower():
            current_end = current_start + timedelta(days=3)
            adjusted = True

        if not adjusted or current_end <= current_start:
            break

        new_simulation_date = _write_plan_simulation_window(
            plan_file,
            current_start,
            current_end,
        )
        messages.append(
            "Adjusted Simulation Date from DSS validation: "
            f"{new_simulation_date}"
        )

    elapsed_seconds = time.time() - start_time
    source_plan_simulation_date: Optional[str] = None
    copied_result_files: List[Path] = []
    if success and config.auto_adjust_simulation_window_from_dss:
        final_start, final_end = _read_plan_simulation_window(plan_file)
        source_plan_simulation_date = _write_plan_simulation_window(
            config.plan_path,
            final_start,
            final_end,
        )
    if success and config.copy_compute_results_to_project:
        copied_result_files = _copy_compute_results_to_project(
            config,
            destination,
        )
        _ensure_plan_result_layers(config)

    summary = {
        "success": success,
        "command": command,
        "output_dir": destination,
        "geometry_sync": geometry_sync,
        "project_file": project_file,
        "plan_file": plan_file,
        "dss_repair": dss_repair,
        "result_hdf": result_hdf if result_hdf.exists() else None,
        "return_code": return_code,
        "elapsed_seconds": elapsed_seconds,
        "messages": messages,
        "fatal_message": fatal_message,
        "attempts": attempt_summaries,
        "source_plan_simulation_date": source_plan_simulation_date,
        "copied_result_files": copied_result_files,
    }
    reports_dir = destination / "Reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "compute_summary.json").write_text(
        json.dumps(_to_jsonable(summary), indent=2),
        encoding="utf-8",
    )
    return summary


def set_geometry_title(geom_path: Path, title: str) -> None:
    lines = geom_path.read_text(encoding="utf-8", errors="replace").splitlines(True)
    lines = _upsert_project_line(lines, "Geom Title=", title)
    geom_path.write_text("".join(lines), encoding="utf-8")


def set_geometry_storage_area_name(geom_path: Path, storage_area_name: str) -> None:
    lines = geom_path.read_text(encoding="utf-8", errors="replace").splitlines(True)
    lines = _upsert_project_line(
        lines,
        "Storage Area=",
        f"{storage_area_name},,",
        insert_after_prefixes=("Viewing Rectangle=", "Program Version=", "Geom Title="),
    )
    lines = [
        (
            f"BC Line Storage Area={storage_area_name}\n"
            if line.startswith("BC Line Storage Area=")
            else line
        )
        for line in lines
    ]
    geom_path.write_text("".join(lines), encoding="utf-8")


def seed_geometry_from_reference(config: RasMapperConfig) -> None:
    copy_file(config.reference_geom_src, config.geom_path)
    set_geometry_title(config.geom_path, config.geometry_title)
    set_geometry_storage_area_name(
        config.geom_path,
        resolve_storage_area_name(config),
    )
    ensure_project_has_geom_reference(config)


def build_expected_region_mannings(
    region_df: pd.DataFrame,
    lookup_df: pd.DataFrame,
    default_value: Optional[float] = None,
) -> pd.DataFrame:
    updated = region_df.copy()
    updated["MainChannel"] = updated["MainChannel"].astype(float)
    if default_value is not None:
        updated["MainChannel"] = float(default_value)
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


def _collect_geometry_region_land_cover_names(
    geom_path: Path,
    geom_hdf_path: Path,
    lookup_df: pd.DataFrame,
) -> List[str]:
    names: List[str] = []
    seen: set[str] = set()

    def _append(name: Any) -> None:
        text = str(name).strip()
        if not text or text in seen:
            return
        seen.add(text)
        names.append(text)

    try:
        existing_region_df = GeomLandCover.get_region_mannings_n(geom_path)
        if not existing_region_df.empty:
            for name in existing_region_df["Land Cover Name"].tolist():
                _append(name)
    except Exception:
        pass

    calibration_path = "Geometry/Land Cover (Manning's n)/Calibration Table"
    if geom_hdf_path.exists():
        try:
            with h5py.File(geom_hdf_path, "r") as handle:
                if calibration_path in handle:
                    values = handle[calibration_path][:]
                    for row in values:
                        raw_name = row["Land Cover Name"]
                        if isinstance(raw_name, bytes):
                            _append(raw_name.decode("utf-8", errors="replace"))
                        else:
                            _append(raw_name)
        except Exception:
            pass

    _append("NoData")
    for _, row in lookup_df.iterrows():
        _append(row["KodText"])

    return names


def build_exact_region_mannings_from_lookup(
    geom_path: Path,
    geom_hdf_path: Path,
    lookup_df: pd.DataFrame,
    nodata_value: float,
    default_value: float,
) -> pd.DataFrame:
    existing = GeomLandCover.get_region_mannings_n(geom_path)
    if not existing.empty:
        region_name = str(existing["Region Name"].iloc[0])
        table_value = str(existing["Table Number"].iloc[0])
    else:
        region_name = "Manning's Region 1"
        table_value = "1"

    names = _collect_geometry_region_land_cover_names(
        geom_path,
        geom_hdf_path,
        lookup_df,
    )
    rows = []
    for name in names:
        if name == "NoData":
            value = float(nodata_value)
        else:
            value = float(default_value)
        rows.append(
            {
                "Table Number": table_value,
                "Land Cover Name": str(name).strip(),
                "MainChannel": value,
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
        config.geom_hdf_path,
        lookup_df,
        nodata_value=config.landcover_nodata_manning,
        default_value=config.region_default_manning,
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


def _distance(start: Tuple[float, float], end: Tuple[float, float]) -> float:
    return math.hypot(float(end[0]) - float(start[0]), float(end[1]) - float(start[1]))


def _format_ras_number(value: Any, decimals: int = 3) -> str:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return ""
    if math.isnan(numeric):
        return ""
    text = f"{numeric:.{decimals}f}".rstrip("0").rstrip(".")
    if text == "-0":
        return "0"
    return text


def _format_fixed_width_values(
    values: Sequence[Any],
    *,
    column_width: int = 8,
    decimals: int = 3,
    values_per_line: int = 10,
) -> List[str]:
    lines: List[str] = []
    for chunk in range(0, len(values), values_per_line):
        parts = [
            f"{_format_ras_number(value, decimals=decimals):>{column_width}}"
            for value in values[chunk:chunk + values_per_line]
        ]
        lines.append("".join(parts).rstrip() + "\n")
    return lines


def _is_blank_value(value: Any) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except TypeError:
        pass
    text = str(value).strip()
    return text == "" or text.casefold() in {"nan", "none", "null"}


def _row_string(row: pd.Series, name: str, default: str = "") -> str:
    if name not in row or _is_blank_value(row[name]):
        return default
    return str(row[name]).strip()


def _row_float(
    row: pd.Series,
    name: str,
    default: Optional[float] = None,
) -> float:
    if name not in row or _is_blank_value(row[name]):
        if default is None:
            raise ValueError(f"Structure row missing required value: {name}")
        return float(default)
    return float(row[name])


def _row_float_any(
    row: pd.Series,
    names: Sequence[str],
    default: Optional[float] = None,
) -> float:
    for name in names:
        if name in row and not _is_blank_value(row[name]):
            return float(row[name])
    if default is None:
        raise ValueError(
            "Structure row missing required value from any of: "
            f"{', '.join(names)}"
        )
    return float(default)


def _row_int(row: pd.Series, name: str, default: int = 0) -> int:
    if name not in row or _is_blank_value(row[name]):
        return int(default)
    return int(float(row[name]))


def _normalize_structure_type(structure_type: str) -> str:
    normalized = structure_type.strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "box": "box",
        "box_culvert": "box",
        "rectangular": "box",
        "rectangular_culvert": "box",
        "arch": "conspan_arch",
        "arch_culvert": "conspan_arch",
        "conspan": "conspan_arch",
        "conspan_arch": "conspan_arch",
        "con_span": "conspan_arch",
        "con_span_arch": "conspan_arch",
        "con/span": "conspan_arch",
        "con/span_arch": "conspan_arch",
        "kemer": "conspan_arch",
        "kemer_culvert": "conspan_arch",
        "circular": "circular",
        "circular_culvert": "circular",
        "circle": "circular",
        "pipe": "circular",
        "round": "circular",
        "round_culvert": "circular",
        "bridge": "bridge_deck",
        "bridge_deck": "bridge_deck",
        "deck": "bridge_deck",
        "deck_bridge": "bridge_deck",
        "deck_with_peir": "bridge_deck",
        "deck_with_pier": "bridge_deck",
    }
    return aliases.get(normalized, normalized)


def _read_structure_table(structure_csv: Path) -> pd.DataFrame:
    frame = pd.read_csv(structure_csv, sep=None, engine="python")
    if frame.empty:
        return frame
    frame = frame.copy()
    frame.columns = [str(column).strip() for column in frame.columns]
    source_columns = set(frame.columns)

    def is_blank(series: pd.Series) -> pd.Series:
        return series.isna() | series.astype(str).str.strip().isin(["", "nan", "None", "null"])

    def coalesce(target: str, *sources: str) -> None:
        if target in frame.columns:
            values = frame[target].copy()
        else:
            values = pd.Series(pd.NA, index=frame.index, dtype="object")
        for source in sources:
            if source not in frame.columns:
                continue
            values = values.where(~is_blank(values), frame[source])
        frame[target] = values

    coalesce("structure_name", "structure_id", "name")
    coalesce("structure_type", "type")
    coalesce("upstream_invert_elevation", "upstream_invert")
    coalesce("downstream_invert_elevation", "downstream_invert")
    coalesce("culvert_length", "culvert_len")
    coalesce("upstream_point_1_x", "upstream_x1")
    coalesce("upstream_point_1_y", "upstream_y1")
    coalesce("upstream_point_2_x", "upstream_x2")
    coalesce("upstream_point_2_y", "upstream_y2")
    coalesce("downstream_point_1_x", "downstream_x1")
    coalesce("downstream_point_1_y", "downstream_y1")
    coalesce("downstream_point_2_x", "downstream_x2")
    coalesce("downstream_point_2_y", "downstream_y2")
    coalesce("deck_weir_coefficient", "deck_weir")
    coalesce("deck_max_submerge", "max_submerge")
    coalesce("culvert_mannings_n", "culvert_mannings")
    coalesce("culvert_bottom_n", "culvert_bottom_mannings")
    coalesce("culvert_chart_number", "culvert_chart")
    coalesce("diameter", "Diameter", "min_span", "min_rise", "span_upstream", "rise_upstream")

    essentials_style = {
        "structure_id",
        "upstream_x1",
        "downstream_x1",
        "deck_max",
    }.issubset(source_columns)
    if essentials_style:
        if "deck_width" in source_columns:
            source_deck_width = frame["deck_width"].copy()
            mask = is_blank(frame["culvert_length"])
            frame.loc[mask, "culvert_length"] = source_deck_width.loc[mask]
            frame["deck_distance"] = source_deck_width.where(
                ~is_blank(source_deck_width),
                0.1,
            )
        if "min_span" in frame.columns:
            mask = is_blank(frame["culvert_length"])
            frame.loc[mask, "culvert_length"] = frame.loc[mask, "min_span"]
        if "deck_weir_coefficient" not in frame.columns:
            frame["deck_weir_coefficient"] = 1.4
        else:
            mask = is_blank(frame["deck_weir_coefficient"])
            frame.loc[mask, "deck_weir_coefficient"] = 1.4
        if "deck_max_submerge" not in frame.columns:
            frame["deck_max_submerge"] = 0.98
        else:
            mask = is_blank(frame["deck_max_submerge"])
            frame.loc[mask, "deck_max_submerge"] = 0.98

    for left, right in (
        ("upstream_point_1_x", "downstream_point_1_x"),
        ("upstream_point_1_y", "downstream_point_1_y"),
        ("upstream_point_2_x", "downstream_point_2_x"),
        ("upstream_point_2_y", "downstream_point_2_y"),
    ):
        if left not in frame.columns or right not in frame.columns:
            continue
        left_blank = is_blank(frame[left])
        right_blank = is_blank(frame[right])
        frame.loc[left_blank & ~right_blank, left] = frame.loc[
            left_blank & ~right_blank,
            right,
        ]
        frame.loc[right_blank & ~left_blank, right] = frame.loc[
            right_blank & ~left_blank,
            left,
        ]

    for target, base, addend in (
        ("low_chord_upstream_left_bank", "upstream_invert_elevation", "rise_upstream"),
        ("low_chord_upstream_right_bank", "upstream_invert_elevation", "rise_upstream"),
        ("low_chord_downstream_left_bank", "downstream_invert_elevation", "rise_downstream"),
        ("low_chord_downstream_right_bank", "downstream_invert_elevation", "rise_downstream"),
        ("high_chord_upstream_left_bank", "upstream_invert_elevation", "rise_upstream"),
        ("high_chord_upstream_right_bank", "upstream_invert_elevation", "rise_upstream"),
        ("high_chord_downstream_left_bank", "downstream_invert_elevation", "rise_downstream"),
        ("high_chord_downstream_right_bank", "downstream_invert_elevation", "rise_downstream"),
    ):
        coalesce(target, "upstream_elev" if "upstream" in target else "downstream_elev", "deck_max")
        if base not in frame.columns or addend not in frame.columns:
            continue
        mask = is_blank(frame[target])
        summed = pd.to_numeric(frame[base], errors="coerce") + pd.to_numeric(
            frame[addend],
            errors="coerce",
        )
        valid = mask & summed.notna()
        frame.loc[valid, target] = summed.loc[valid]

    frame["structure_type"] = frame["structure_type"].apply(
        lambda value: _normalize_structure_type(str(value or ""))
    )
    return frame


def _connection_line_from_structure(row: pd.Series) -> List[Tuple[float, float]]:
    upstream_1 = (
        _row_float(row, "upstream_point_1_x"),
        _row_float(row, "upstream_point_1_y"),
    )
    upstream_2 = (
        _row_float(row, "upstream_point_2_x"),
        _row_float(row, "upstream_point_2_y"),
    )
    downstream_1 = (
        _row_float(row, "downstream_point_1_x"),
        _row_float(row, "downstream_point_1_y"),
    )
    downstream_2 = (
        _row_float(row, "downstream_point_2_x"),
        _row_float(row, "downstream_point_2_y"),
    )
    line = [
        (
            0.5 * (upstream_1[0] + downstream_1[0]),
            0.5 * (upstream_1[1] + downstream_1[1]),
        ),
        (
            0.5 * (upstream_2[0] + downstream_2[0]),
            0.5 * (upstream_2[1] + downstream_2[1]),
        ),
    ]
    if _distance(line[0], line[1]) <= 1e-9:
        return [upstream_1, upstream_2]
    return line


def _structure_shape_defaults(row: pd.Series) -> Tuple[int, str, float, float, int, int]:
    structure_type = _row_string(row, "structure_type")
    if structure_type == "box":
        return (
            2,
            "BOX",
            _row_float(row, "min_rise"),
            _row_float(row, "min_span"),
            _row_int(row, "inlet_type", default=8),
            _row_int(row, "outlet_type", default=1),
        )
    if structure_type == "conspan_arch":
        return (
            9,
            "conspan_arch",
            _row_float(row, "min_rise"),
            _row_float(row, "min_span"),
            _row_int(row, "inlet_type", default=60),
            _row_int(row, "outlet_type", default=1),
        )
    if structure_type == "bridge_deck":
        return (0, "NONE", 0.0, 0.0, 0, 0)
    if structure_type in {"circular", "circle", "pipe", "round"}:
        diameter = _row_float_any(
            row,
            ("diameter", "Diameter", "min_span", "min_rise", "span_upstream", "rise_upstream"),
        )
        return (
            1,
            "CIRCULAR",
            diameter,
            diameter,
            _row_int(row, "inlet_type", default=1),
            _row_int(row, "outlet_type", default=1),
        )
    raise NotImplementedError(
        "Only Pipe/Circular, Box, Arch/ConSpan, and Bridge structures are "
        f"supported for SA/2D connections. Received {structure_type!r}."
    )


def load_structure_connections(config: RasMapperConfig) -> List[StructureConnection]:
    structure_csv = config.structure_csv_copy or config.structure_csv_src
    if structure_csv is None:
        return []
    if not structure_csv.exists() and config.structure_csv_src is not None:
        structure_csv = config.structure_csv_src
    if not structure_csv.exists():
        raise FileNotFoundError(f"Structure table was not found: {structure_csv}")

    frame = _read_structure_table(structure_csv)
    if frame.empty:
        return []

    required_columns = {
        "structure_type",
        "upstream_point_1_x",
        "upstream_point_1_y",
        "upstream_point_2_x",
        "upstream_point_2_y",
        "downstream_point_1_x",
        "downstream_point_1_y",
        "downstream_point_2_x",
        "downstream_point_2_y",
    }
    missing = sorted(required_columns - set(frame.columns))
    if missing:
        raise ValueError(f"Structure table is missing required columns: {missing}")

    structures: List[StructureConnection] = []
    for index, row in frame.iterrows():
        name = _row_string(row, "structure_name", default=f"Structure_{index + 1}")
        name = re.sub(r"[,/\\]+", "_", name).strip() or f"Structure_{index + 1}"
        line = _connection_line_from_structure(row)
        shape_code, shape_name, rise, span, inlet_type, outlet_type = (
            _structure_shape_defaults(row)
        )
        deck_width = _row_float(
            row,
            "deck_width",
            default=max(_distance(line[0], line[-1]), span, 0.1),
        )
        deck_max = _row_float_any(
            row,
            (
                "deck_max",
                "upstream_elev",
                "downstream_elev",
                "high_chord_upstream_left_bank",
                "high_chord_downstream_left_bank",
            ),
            default=max(
                _row_float(row, "upstream_invert_elevation", default=0.0) + rise,
                _row_float(row, "downstream_invert_elevation", default=0.0) + rise,
            ),
        )
        htab_hwmax = _row_float(row, "htab_hwmax", default=deck_max + 0.09)
        structures.append(
            StructureConnection(
                name=name,
                structure_type=_row_string(row, "structure_type"),
                line=line,
                culvert_shape_code=shape_code,
                culvert_shape_name=shape_name,
                culvert_rise=rise,
                culvert_span=span,
                culvert_length=_row_float_any(
                    row,
                    ("culvert_length", "bridge_width", "deck_width"),
                    default=max(_distance(line[0], line[-1]), 0.1),
                ),
                upstream_invert=_row_float(row, "upstream_invert_elevation", default=deck_max),
                downstream_invert=_row_float(row, "downstream_invert_elevation", default=deck_max),
                deck_distance=_row_float(row, "deck_distance", default=0.1),
                deck_width=deck_width,
                deck_weir_coefficient=_row_float(
                    row,
                    "deck_weir_coefficient",
                    default=1.4,
                ),
                deck_skew=_row_float(row, "deck_skew", default=0.0),
                deck_max_submerge=_row_float(row, "deck_max_submerge", default=0.98),
                culvert_mannings_n=_row_float(row, "culvert_mannings_n", default=0.02),
                culvert_bottom_n=_row_float(row, "culvert_bottom_n", default=0.025),
                entrance_loss=_row_float(row, "entrance_loss", default=0.5),
                exit_loss=_row_float(row, "exit_loss", default=1.0),
                inlet_type=inlet_type,
                outlet_type=outlet_type,
                culvert_chart_number=_row_int(row, "culvert_chart_number", default=0),
                num_barrels=max(1, _row_int(row, "num_barrels", default=1)),
                barrel_center_spacing=_row_float_any(
                    row,
                    ("barrel_center_spacing", "pipe_separation", "pipe_sepration"),
                    default=max(span * 1.5, 0.1),
                ),
                htab_hwmax=htab_hwmax,
            )
        )
    return structures


def _connection_station_values(structure: StructureConnection) -> List[float]:
    line_length = max(_distance(structure.line[0], structure.line[-1]), 0.0)
    center = 0.5 * line_length
    if structure.num_barrels <= 1:
        return [center, center]

    center_index = 0.5 * (structure.num_barrels - 1)
    values: List[float] = []
    for barrel_index in range(structure.num_barrels):
        offset = (barrel_index - center_index) * structure.barrel_center_spacing
        station = center + offset
        values.extend([station, station])
    return values


def _render_connection_bridge_block(structure: StructureConnection) -> List[str]:
    line_length = max(_distance(structure.line[0], structure.line[-1]), structure.deck_width)
    left_station = max(0.0, 0.5 * (line_length - structure.deck_width))
    right_station = left_station + structure.deck_width
    deck_elevation = max(
        structure.htab_hwmax - 0.09,
        structure.upstream_invert + structure.culvert_rise,
        structure.downstream_invert + structure.culvert_rise,
    )
    low_chord = min(
        structure.upstream_invert + structure.culvert_rise,
        structure.downstream_invert + structure.culvert_rise,
        deck_elevation,
    )
    block = [
        "Conn BR: Bridge=0,0,0,0, 0 ,0.3,0.5\n",
        "Conn BR: Pressure-Weir=,,,,\n",
        (
            "Conn BR: Deck Dist Width WeirC Skew NumUp NumDn "
            "MinLoCord MaxHiCord MaxSubmerge Is_Ogee\n"
        ),
        (
            f"{_format_ras_number(structure.deck_distance, decimals=2)},"
            f"{_format_ras_number(structure.deck_width, decimals=3)},"
            f"{_format_ras_number(structure.deck_weir_coefficient, decimals=3)},"
            f"{_format_ras_number(structure.deck_skew, decimals=3)}, "
            "2, 2, , , "
            f"{_format_ras_number(structure.deck_max_submerge, decimals=3)}, "
            "0, 0,0,,\n"
        ),
    ]
    for values in (
        [left_station, right_station],
        [deck_elevation, deck_elevation],
        [low_chord, low_chord],
        [left_station, right_station],
        [deck_elevation, deck_elevation],
        [low_chord, low_chord],
    ):
        block.extend(_format_fixed_width_values(values, values_per_line=len(values)))
    return block


def _render_connection_culvert_block(structure: StructureConnection) -> List[str]:
    stations = _connection_station_values(structure)
    if structure.num_barrels > 1:
        culvert_line = (
            "Connection Culv="
            f"{structure.culvert_shape_code},"
            f"{_format_ras_number(structure.culvert_rise, decimals=3)},"
            f"{_format_ras_number(structure.culvert_span, decimals=3)},"
            f"{_format_ras_number(structure.culvert_length, decimals=3)},"
            f"{_format_ras_number(structure.culvert_mannings_n, decimals=3)},"
            f"{_format_ras_number(structure.entrance_loss, decimals=3)},"
            f"{_format_ras_number(structure.exit_loss, decimals=3)},"
            f"{structure.inlet_type},"
            f"{structure.outlet_type},"
            f"{_format_ras_number(structure.upstream_invert, decimals=3)},"
            f"{_format_ras_number(structure.downstream_invert, decimals=3)}, "
            f"{structure.num_barrels} ,"
            f"{structure.name:<12}, "
            f"{structure.culvert_chart_number} ,"
            f"{_format_ras_number(structure.deck_distance, decimals=2)}\n"
        )
    else:
        culvert_line = (
            "Connection Culv="
            f"{structure.culvert_shape_code},"
            f"{_format_ras_number(structure.culvert_rise, decimals=3)},"
            f"{_format_ras_number(structure.culvert_span, decimals=3)},"
            f"{_format_ras_number(structure.culvert_length, decimals=3)},"
            f"{_format_ras_number(structure.culvert_mannings_n, decimals=3)},"
            f"{_format_ras_number(structure.entrance_loss, decimals=3)},"
            f"{_format_ras_number(structure.exit_loss, decimals=3)},"
            f"{structure.inlet_type},"
            f"{structure.outlet_type},"
            f"{_format_ras_number(structure.upstream_invert, decimals=3)},"
            f"{_format_ras_number(structure.downstream_invert, decimals=3)}, "
            f"{structure.num_barrels} ,"
            f"{structure.name:<12}, "
            f"{structure.culvert_chart_number} ,"
            f"{_format_ras_number(structure.deck_distance, decimals=2)}\n"
        )

    return [
        culvert_line,
        *_format_fixed_width_values(stations, values_per_line=len(stations)),
        f"Conn Culv Bottom n={_format_ras_number(structure.culvert_bottom_n, decimals=3)}\n",
    ]


def render_structure_connection_blocks(
    structures: Sequence[StructureConnection],
    storage_area_name: str,
) -> List[str]:
    blocks: List[str] = []
    timestamp = time.strftime("%b/%d/%Y %H:%M:%S")
    for structure in structures:
        middle = (
            0.5 * (structure.line[0][0] + structure.line[-1][0]),
            0.5 * (structure.line[0][1] + structure.line[-1][1]),
        )
        line_length = max(_distance(structure.line[0], structure.line[-1]), structure.deck_width)
        weir_elevation = max(
            structure.htab_hwmax - 0.09,
            structure.upstream_invert + structure.culvert_rise,
            structure.downstream_invert + structure.culvert_rise,
        )
        blocks.append(
            f"Connection={structure.name:<16},{middle[0]},{middle[1]}\n"
        )
        blocks.append("Connection Desc=\n")
        blocks.append(f"Connection Line={len(structure.line)}\n")
        blocks.extend(
            _fixed_width_xy_line(chunk)
            for chunk in _chunk_points(structure.line, 2)
        )
        blocks.append(f"Connection Last Edited Time={timestamp}\n")
        blocks.append("Conn CellSize Min=1\n")
        blocks.append("Conn Near Repeats=3\n")
        blocks.append("Conn Protection Radius=-1\n")
        blocks.append(f"Connection Up SA={storage_area_name:<16}\n")
        blocks.append(f"Connection Dn SA={storage_area_name:<16}\n")
        blocks.append("Conn Routing Type= 32 \n")
        blocks.append("Conn Use RC Family=False\n")
        blocks.append("Conn OverFlow Method 2D=False\n")
        blocks.append(
            f"Conn Weir WD={_format_ras_number(structure.deck_width, decimals=3)}\n"
        )
        blocks.append(
            f"Conn Weir Coef={_format_ras_number(structure.deck_weir_coefficient, decimals=3)}\n"
        )
        blocks.append("Conn Weir Is Ogee= 0 \n")
        blocks.append("Conn Simple Spill Pos Coef=0.05\n")
        blocks.append("Conn Simple Spill Neg Coef=0.05\n")
        blocks.append("Conn Weir SE= 2 \n")
        blocks.extend(
            _format_fixed_width_values(
                [0.0, weir_elevation, line_length, weir_elevation],
                values_per_line=4,
            )
        )
        if structure.structure_type == "bridge_deck":
            blocks.extend(_render_connection_bridge_block(structure))
        else:
            blocks.extend(_render_connection_culvert_block(structure))
        blocks.append(f"Conn HTab HWMax={_format_ras_number(structure.htab_hwmax, decimals=3)}\n")
        if structure.structure_type != "bridge_deck":
            blocks.append(f"Conn HTab TWMax={_format_ras_number(structure.htab_hwmax, decimals=3)}\n")
        blocks.append("\n")
    return blocks


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


def _spaced_bucket_key(
    point: Tuple[float, float],
    spacing: float,
) -> Tuple[int, int]:
    return (
        int(math.floor(point[0] / spacing)),
        int(math.floor(point[1] / spacing)),
    )


def _build_spaced_point_buckets(
    points: Sequence[Tuple[float, float]],
    min_spacing: float,
) -> Dict[Tuple[int, int], List[Tuple[float, float]]]:
    buckets: Dict[Tuple[int, int], List[Tuple[float, float]]] = {}
    for point in points:
        buckets.setdefault(_spaced_bucket_key(point, min_spacing), []).append(point)
    return buckets


def _append_spaced_point(
    points: List[Tuple[float, float]],
    buckets: Dict[Tuple[int, int], List[Tuple[float, float]]],
    point: Tuple[float, float],
    min_spacing: float,
) -> None:
    key = _spaced_bucket_key(point, min_spacing)
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for existing in buckets.get((key[0] + dx, key[1] + dy), []):
                if math.hypot(point[0] - existing[0], point[1] - existing[1]) < min_spacing:
                    return
    buckets.setdefault(key, []).append(point)
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
    min_point_spacing: float = 0.0,
) -> List[Tuple[float, float]]:
    seed_points: List[Tuple[float, float]] = []
    seen: set[Tuple[int, int]] = set()
    spaced_buckets: Dict[Tuple[int, int], List[Tuple[float, float]]] = {}

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
                            if min_point_spacing > 0:
                                _append_spaced_point(
                                    seed_points,
                                    spaced_buckets,
                                    candidate,
                                    min_point_spacing,
                                )
                            else:
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

    # Breakline refinement belongs in the BreakLine records below. Injecting
    # dense breakline-adjacent points here can make HEC-RAS report
    # near-duplicate points or negative-area cells during compute.

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


def _bounds_from_coord_sets(
    coord_sets: Sequence[Sequence[Tuple[float, float]]],
) -> Tuple[float, float, float, float]:
    xs: List[float] = []
    ys: List[float] = []
    for coords in coord_sets:
        for x, y in coords:
            xs.append(float(x))
            ys.append(float(y))
    if not xs or not ys:
        raise ValueError("No coordinates provided for extent calculation")
    return min(xs), max(xs), min(ys), max(ys)


def _expand_bounds(
    bounds: Tuple[float, float, float, float],
    pad_fraction: float = 0.2,
    min_pad: float = 100.0,
) -> Tuple[float, float, float, float]:
    xmin, xmax, ymin, ymax = bounds
    dx = max(xmax - xmin, 1.0)
    dy = max(ymax - ymin, 1.0)
    px = max(dx * pad_fraction, min_pad)
    py = max(dy * pad_fraction, min_pad)
    return xmin - px, xmax + px, ymin - py, ymax + py


def _update_viewing_rectangle(
    lines: List[str],
    coord_sets: Sequence[Sequence[Tuple[float, float]]],
    pad_fraction: float = 0.2,
    min_pad: float = 100.0,
) -> List[str]:
    xmin, xmax, ymin, ymax = _expand_bounds(
        _bounds_from_coord_sets(coord_sets),
        pad_fraction=pad_fraction,
        min_pad=min_pad,
    )
    value = f" {xmin} , {xmax} , {ymax} , {ymin} "
    return _upsert_project_line(lines, "Viewing Rectangle=", value)


def _set_rasmap_current_view(
    rasmap_path: Path,
    coord_sets: Sequence[Sequence[Tuple[float, float]]],
    pad_fraction: float = 0.2,
    min_pad: float = 100.0,
) -> None:
    if not rasmap_path.exists():
        return

    xmin, xmax, ymin, ymax = _expand_bounds(
        _bounds_from_coord_sets(coord_sets),
        pad_fraction=pad_fraction,
        min_pad=min_pad,
    )

    tree = ET.parse(rasmap_path)
    root = tree.getroot()
    current_view = root.find("CurrentView")
    if current_view is None:
        current_view = ET.Element("CurrentView")
        insert_before = root.find("VelocitySettings")
        if insert_before is None:
            root.append(current_view)
        else:
            root.insert(list(root).index(insert_before), current_view)

    values = {
        "MinX": xmin,
        "MaxX": xmax,
        "MinY": ymin,
        "MaxY": ymax,
    }
    for key, value in values.items():
        child = current_view.find(key)
        if child is None:
            child = ET.SubElement(current_view, key)
        child.text = str(value)

    tree.write(rasmap_path, encoding="utf-8", xml_declaration=False)


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

    outward_normal = _outward_unit_normal(polyline, ring)
    if outward_normal is None:
        return list(polyline)

    middle_index = len(polyline) // 2
    middle_point = polyline[middle_index]
    candidates = []
    for sign in (1.0, -1.0):
        ox = outward_normal[0] * offset_distance * sign
        oy = outward_normal[1] * offset_distance * sign
        shifted = [(point[0] + ox, point[1] + oy) for point in polyline]
        shifted_mid = (middle_point[0] + ox, middle_point[1] + oy)
        candidates.append((not _point_in_ring(shifted_mid, ring), shifted))

    for outside, shifted in candidates:
        if outside:
            return shifted
    return candidates[0][1]


def _outward_unit_normal(
    polyline: Sequence[Tuple[float, float]],
    ring: Sequence[Tuple[float, float]],
) -> Optional[Tuple[float, float]]:
    if len(polyline) < 2:
        return None

    start = polyline[0]
    end = polyline[-1]
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    seg_len = math.hypot(dx, dy)
    if seg_len == 0:
        return None

    nx = -dy / seg_len
    ny = dx / seg_len
    centroid = _polygon_centroid(ring)
    interior_side = _point_side_of_line(centroid, polyline)
    preferred_sign = -1.0 if interior_side == "left" else 1.0
    return (nx * preferred_sign, ny * preferred_sign)


def _polyline_length(coords: Sequence[Tuple[float, float]]) -> float:
    return sum(
        math.hypot(end[0] - start[0], end[1] - start[1])
        for start, end in zip(coords[:-1], coords[1:])
    )


def _polyline_direction(
    coords: Sequence[Tuple[float, float]],
) -> Optional[Tuple[float, float]]:
    if len(coords) < 2:
        return None

    start = coords[0]
    end = coords[-1]
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length = math.hypot(dx, dy)
    if length > 0:
        return (dx / length, dy / length)

    for start, end in zip(coords[:-1], coords[1:]):
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        length = math.hypot(dx, dy)
        if length > 0:
            return (dx / length, dy / length)
    return None


def _build_centered_segment(
    center: Tuple[float, float],
    direction: Tuple[float, float],
    total_length: float,
) -> List[Tuple[float, float]]:
    half = max(float(total_length), 0.0) / 2.0
    dx = direction[0] * half
    dy = direction[1] * half
    return [
        (center[0] - dx, center[1] - dy),
        (center[0] + dx, center[1] + dy),
    ]


def _endpoint_extension_direction(
    coords: Sequence[Tuple[float, float]],
    at_start: bool,
) -> Optional[Tuple[float, float]]:
    if len(coords) < 2:
        return None

    if at_start:
        anchor = coords[0]
        neighbors = coords[1:]
    else:
        anchor = coords[-1]
        neighbors = list(reversed(coords[:-1]))

    for neighbor in neighbors:
        dx = anchor[0] - neighbor[0]
        dy = anchor[1] - neighbor[1]
        length = math.hypot(dx, dy)
        if length > 0:
            return (dx / length, dy / length)
    return None


def _segment_ring_intersection_distance(
    start: Tuple[float, float],
    end: Tuple[float, float],
    ring: Sequence[Tuple[float, float]],
) -> Optional[float]:
    try:
        from shapely.geometry import LineString, Point, Polygon
    except ImportError:
        midpoint = ((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0)
        if _point_in_ring(midpoint, ring):
            return math.hypot(end[0] - start[0], end[1] - start[1]) / 2.0
        return None

    line = LineString([start, end])
    boundary = Polygon(ring).boundary
    intersection = line.intersection(boundary)
    candidates = _dedupe_xy_points(_extract_point_coords_from_geometry(intersection))
    if not candidates:
        midpoint = ((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0)
        if _point_in_ring(midpoint, ring):
            return line.length / 2.0
        return None

    distances = []
    for candidate in candidates:
        distance = float(line.project(Point(candidate)))
        if 1e-6 < distance < line.length - 1e-6:
            distances.append(distance)
    if not distances:
        return None
    return min(distances)


def _interpolate_closed_line_distance(
    line: Any,
    distance: float,
) -> Tuple[float, float]:
    line_length = float(line.length)
    if line_length <= 0:
        x, y = line.coords[0]
        return (float(x), float(y))
    point = line.interpolate(distance % line_length)
    return (float(point.x), float(point.y))


def _choose_closed_line_direction(
    line: Any,
    start_distance: float,
    desired_direction: Tuple[float, float],
) -> bool:
    line_length = float(line.length)
    if line_length <= 0:
        return True

    epsilon = min(2.0, max(line_length / 1000.0, 0.25))
    start_point = _interpolate_closed_line_distance(line, start_distance)
    forward_point = _interpolate_closed_line_distance(
        line,
        start_distance + epsilon,
    )
    backward_point = _interpolate_closed_line_distance(
        line,
        start_distance - epsilon,
    )

    def _dot(candidate: Tuple[float, float]) -> float:
        dx = candidate[0] - start_point[0]
        dy = candidate[1] - start_point[1]
        length = math.hypot(dx, dy)
        if length <= 0:
            return float("-inf")
        return (
            (dx / length) * desired_direction[0]
            + (dy / length) * desired_direction[1]
        )

    return _dot(forward_point) >= _dot(backward_point)


def _trace_closed_line(
    line: Any,
    start_distance: float,
    walk_length: float,
    forward: bool,
    step: float,
) -> List[Tuple[float, float]]:
    coords = [_interpolate_closed_line_distance(line, start_distance)]
    if walk_length <= 0:
        return coords

    distance = step
    sign = 1.0 if forward else -1.0
    while distance < walk_length - 1e-6:
        coords.append(
            _interpolate_closed_line_distance(
                line,
                start_distance + sign * distance,
            )
        )
        distance += step
    coords.append(
        _interpolate_closed_line_distance(
            line,
            start_distance + sign * walk_length,
        )
    )
    return _dedupe_consecutive_points(coords)


def _build_perimeter_follow_extension(
    anchor: Tuple[float, float],
    direction: Tuple[float, float],
    extension_length: float,
    ring: Sequence[Tuple[float, float]],
    clearance: float,
    at_start: bool,
) -> List[Tuple[float, float]]:
    if extension_length <= 0:
        return [anchor]

    candidate = (
        anchor[0] + direction[0] * extension_length,
        anchor[1] + direction[1] * extension_length,
    )
    hit_distance = _segment_ring_intersection_distance(anchor, candidate, ring)
    if hit_distance is None:
        return [candidate, anchor] if at_start else [anchor, candidate]

    safe_distance = max(0.0, min(extension_length, hit_distance - clearance))
    bend_base = (
        anchor[0] + direction[0] * safe_distance,
        anchor[1] + direction[1] * safe_distance,
    )

    try:
        from shapely.geometry import Point, Polygon
    except ImportError:
        return [bend_base, anchor] if at_start else [anchor, bend_base]

    outer_boundary = Polygon(ring).buffer(clearance, join_style=2).exterior
    start_distance = float(outer_boundary.project(Point(bend_base)))
    start_point = _interpolate_closed_line_distance(outer_boundary, start_distance)
    connector_length = math.hypot(
        start_point[0] - bend_base[0],
        start_point[1] - bend_base[1],
    )
    remaining = max(0.0, extension_length - safe_distance - connector_length)
    forward = _choose_closed_line_direction(
        outer_boundary,
        start_distance,
        direction,
    )
    perimeter_path = _trace_closed_line(
        outer_boundary,
        start_distance,
        remaining,
        forward=forward,
        step=max(1.0, clearance * 2.0),
    )
    outward_path = [anchor, bend_base]
    if connector_length > 1e-6:
        outward_path.append(start_point)
    outward_path.extend(perimeter_path[1:])
    outward_path = _dedupe_consecutive_points(outward_path)
    return list(reversed(outward_path)) if at_start else outward_path


def _build_downstream_boundary_from_cross_section(
    section: CrossSectionInfo,
    perimeter_ring: Sequence[Tuple[float, float]],
    boundary_offset_distance: float,
    length_multiplier: float,
) -> List[Tuple[float, float]]:
    shifted = _offset_polyline_outside_ring(
        section.points,
        perimeter_ring,
        offset_distance=boundary_offset_distance,
    )
    if len(shifted) < 2:
        return shifted

    total_length = _polyline_length(section.points) * max(length_multiplier, 1.0)
    extra_length = max(0.0, total_length - _polyline_length(shifted))
    if extra_length <= 0.0:
        return shifted

    extension = extra_length / 2.0
    clearance = max(boundary_offset_distance, 1.0)

    start_direction = _endpoint_extension_direction(shifted, at_start=True)
    end_direction = _endpoint_extension_direction(shifted, at_start=False)
    if start_direction is None or end_direction is None:
        return shifted

    start_points = _build_perimeter_follow_extension(
        shifted[0],
        start_direction,
        extension,
        perimeter_ring,
        clearance,
        at_start=True,
    )
    end_points = _build_perimeter_follow_extension(
        shifted[-1],
        end_direction,
        extension,
        perimeter_ring,
        clearance,
        at_start=False,
    )
    combined = [*start_points[:-1], *shifted, *end_points[1:]]
    return _dedupe_consecutive_points(combined)


def _extract_point_coords_from_geometry(geometry: Any) -> List[Tuple[float, float]]:
    geom_type = getattr(geometry, "geom_type", "")
    if not geom_type or geometry.is_empty:
        return []
    if geom_type == "Point":
        return [(float(geometry.x), float(geometry.y))]
    if geom_type == "MultiPoint":
        return [
            (float(point.x), float(point.y))
            for point in geometry.geoms
        ]
    if geom_type == "GeometryCollection":
        points: List[Tuple[float, float]] = []
        for child in geometry.geoms:
            points.extend(_extract_point_coords_from_geometry(child))
        return points
    return []


def _dedupe_xy_points(
    points: Sequence[Tuple[float, float]],
    precision: float = 1000.0,
) -> List[Tuple[float, float]]:
    deduped: List[Tuple[float, float]] = []
    seen: set[Tuple[int, int]] = set()
    for point in points:
        key = (
            int(round(point[0] * precision)),
            int(round(point[1] * precision)),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append((float(point[0]), float(point[1])))
    return deduped


def _polyline_subsegment(
    coords: Sequence[Tuple[float, float]],
    start_distance: float,
    end_distance: float,
) -> List[Tuple[float, float]]:
    from shapely.geometry import LineString
    from shapely.ops import substring

    line = LineString(coords)
    if start_distance > end_distance:
        start_distance, end_distance = end_distance, start_distance

    segment = substring(line, start_distance, end_distance)
    if segment.geom_type == "Point":
        return [(float(segment.x), float(segment.y))]
    return [
        (float(x), float(y))
        for x, y in segment.coords
    ]


def _limit_boundary_section_to_breaklines(
    section_points: Sequence[Tuple[float, float]],
    section_mean: Tuple[float, float],
    perimeter_ring: Sequence[Tuple[float, float]],
    breaklines: Sequence[Sequence[Tuple[float, float]]],
) -> List[Tuple[float, float]]:
    try:
        from shapely.geometry import LineString, Point, Polygon
    except ImportError:
        return list(section_points)

    if len(section_points) < 2 or len(breaklines) < 2:
        return list(section_points)

    section_line = LineString(section_points)
    perimeter_boundary = Polygon(perimeter_ring).boundary
    selected: List[Dict[str, Any]] = []

    for breakline in breaklines:
        intersection = LineString(breakline).intersection(perimeter_boundary)
        candidates = _dedupe_xy_points(
            _extract_point_coords_from_geometry(intersection)
        )
        if not candidates:
            continue

        anchor = min(
            candidates,
            key=lambda point: math.hypot(
                point[0] - section_mean[0],
                point[1] - section_mean[1],
            ),
        )
        distance = float(section_line.project(Point(anchor)))
        projected = section_line.interpolate(distance)
        selected.append(
            {
                "anchor": anchor,
                "distance": distance,
                "projected": (float(projected.x), float(projected.y)),
            }
        )

    if len(selected) < 2:
        return list(section_points)

    selected.sort(key=lambda item: item["distance"])
    start_info = selected[0]
    end_info = selected[-1]
    return _dedupe_consecutive_points(
        [start_info["anchor"], end_info["anchor"]]
    )


def build_boundary_lines_from_sources(
    sections: Sequence[CrossSectionInfo],
    perimeter_ring: Sequence[Tuple[float, float]],
    breaklines: Sequence[Sequence[Tuple[float, float]]],
    boundary_offset_distance: float,
    downstream_bc_length_multiplier: float,
    storage_area_name: str,
) -> List[Dict[str, Any]]:
    boundary_lines = []
    for role, section in (
        ("upstream BC", sections[0]),
        ("downstream BC", sections[-1]),
    ):
        if role == "downstream BC":
            coords = _build_downstream_boundary_from_cross_section(
                section,
                perimeter_ring,
                boundary_offset_distance=boundary_offset_distance,
                length_multiplier=downstream_bc_length_multiplier,
            )
        else:
            limited_section = _limit_boundary_section_to_breaklines(
                section.points,
                section.mean_point,
                perimeter_ring,
                breaklines,
            )
            coords = _offset_polyline_outside_ring(
                limited_section,
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


def _read_hdf_2d_cell_count(handle: h5py.File, base: str) -> int:
    centers_path = f"{base}/Cells Center Coordinate"
    if centers_path in handle:
        return int(handle[centers_path].shape[0])

    attrs_path = f"{base}/Attributes"
    if attrs_path in handle:
        attrs = handle[attrs_path][:]
        if len(attrs) and "Cell Count" in attrs.dtype.names:
            return int(attrs[0]["Cell Count"])

    cell_info_path = f"{base}/Cell Info"
    if cell_info_path in handle:
        cell_info = handle[cell_info_path][:]
        if len(cell_info):
            return int(cell_info[0][-1])

    cell_points_path = f"{base}/Cell Points"
    if cell_points_path in handle:
        return int(handle[cell_points_path].shape[0])

    raise RuntimeError(f"Could not determine mesh cell count under {base}")


def _read_hdf_2d_perimeter(handle: h5py.File, base: str) -> np.ndarray:
    perimeter_path = f"{base}/Perimeter"
    if perimeter_path in handle:
        return handle[perimeter_path][:]

    polygon_points_path = f"{base}/Polygon Points"
    if polygon_points_path in handle:
        return handle[polygon_points_path][:]

    raise RuntimeError(f"Could not determine mesh perimeter points under {base}")


def _read_hdf_2d_face_points(handle: h5py.File, base: str) -> np.ndarray:
    face_points_path = f"{base}/FacePoints Coordinate"
    if face_points_path in handle:
        return handle[face_points_path][:]

    cell_points_path = f"{base}/Cell Points"
    if cell_points_path in handle:
        return handle[cell_points_path][:]

    raise RuntimeError(f"Could not determine mesh face/cell points under {base}")


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
        face_points = _read_hdf_2d_face_points(handle, base)
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
    storage_area_name = resolve_storage_area_name(
        config,
        geom_path=config.geom_path,
        lines=lines,
    )
    lines = _upsert_project_line(
        lines,
        "Storage Area=",
        f"{storage_area_name},,",
        insert_after_prefixes=("Viewing Rectangle=", "Program Version=", "Geom Title="),
    )

    perimeter_ring = _ensure_closed_ring(load_polygon_rings(config.perimeter_src)[0])
    breaklines = [coords for _, coords in load_polyline_features(config.breakline_src)]
    structures = load_structure_connections(config)
    if config.boundary_shp.exists():
        boundary_lines = [
            {
                "name": str(
                    item["record"].get("bc_name")
                    or item["record"].get("name")
                    or f"BC Line {index}"
                ),
                "storage_area": storage_area_name,
                "coords": item["coords"],
            }
            for index, item in enumerate(
                _load_boundary_lines_from_shapefile(config.boundary_shp),
                start=1,
            )
        ]
    else:
        sections = load_cross_sections(config.cross_section_src)
        boundary_lines = build_boundary_lines_from_sources(
            sections,
            perimeter_ring,
            breaklines,
            boundary_offset_distance=config.boundary_offset_distance,
            downstream_bc_length_multiplier=(
                config.downstream_bc_length_multiplier
            ),
            storage_area_name=storage_area_name,
        )

    view_coord_sets = [
        perimeter_ring,
        *breaklines,
        *[structure.line for structure in structures],
        *[boundary["coords"] for boundary in boundary_lines],
    ]
    lines = _update_viewing_rectangle(lines, view_coord_sets)
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
        [
            *breakline_lines,
            *render_structure_connection_blocks(structures, storage_area_name),
        ],
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
            f"BC Line Text Position= {middle[0]} , {middle[1]} \n"
        )
    lines = _replace_block_between_prefixes(
        lines,
        "BC Line Name=",
        "LCMann Time=",
        bc_lines,
    )

    config.geom_path.write_text("".join(lines), encoding="utf-8")
    _set_rasmap_current_view(config.rasmap_path, view_coord_sets)

    return {
        "perimeter_points": len(perimeter_ring),
        "computation_points": len(computation_points),
        "storage_area_name": storage_area_name,
        "mesh_cell_size": config.mesh_cell_size,
        "breakline_counts": [len(coords) for coords in breaklines],
        "structure_connections": [
            {
                "name": structure.name,
                "type": structure.structure_type,
                "coords": structure.line,
                "num_barrels": structure.num_barrels,
            }
            for structure in structures
        ],
        "boundary_lines": [
            {
                "name": item["name"],
                "coords": item["coords"],
            }
            for item in boundary_lines
        ],
    }


def refresh_geometry_display_metadata(config: RasMapperConfig) -> None:
    if not config.geom_path.exists():
        return

    lines = config.geom_path.read_text(
        encoding="utf-8",
        errors="replace",
    ).splitlines(True)
    perimeter_ring = _ensure_closed_ring(load_polygon_rings(config.perimeter_src)[0])
    breaklines = [coords for _, coords in load_polyline_features(config.breakline_src)]
    structures = load_structure_connections(config)
    if config.boundary_shp.exists():
        boundary_lines = [
            {
                "name": str(item["record"].get("bc_name") or f"BC Line {index}"),
                "storage_area": str(
                    item["record"].get("storage")
                    or resolve_storage_area_name(
                        config,
                        geom_path=config.geom_path,
                        lines=lines,
                    )
                ),
                "coords": item["coords"],
            }
            for index, item in enumerate(
                _load_boundary_lines_from_shapefile(config.boundary_shp),
                start=1,
            )
        ]
    else:
        sections = load_cross_sections(config.cross_section_src)
        boundary_lines = build_boundary_lines_from_sources(
            sections,
            perimeter_ring,
            breaklines,
            boundary_offset_distance=config.boundary_offset_distance,
            downstream_bc_length_multiplier=(
                config.downstream_bc_length_multiplier
            ),
            storage_area_name=resolve_storage_area_name(
                config,
                geom_path=config.geom_path,
                lines=lines,
            ),
        )

    view_coord_sets = [
        perimeter_ring,
        *breaklines,
        *[structure.line for structure in structures],
        *[boundary["coords"] for boundary in boundary_lines],
    ]
    lines = _update_viewing_rectangle(lines, view_coord_sets)

    text_positions = [
        boundary["coords"][len(boundary["coords"]) // 2]
        for boundary in boundary_lines
    ]
    text_index = 0
    for index, line in enumerate(lines):
        if not line.startswith("BC Line Text Position="):
            continue
        if text_index >= len(text_positions):
            break
        text_point = text_positions[text_index]
        lines[index] = (
            f"BC Line Text Position= {text_point[0]} , {text_point[1]} \n"
        )
        text_index += 1

    config.geom_path.write_text("".join(lines), encoding="utf-8")
    _set_rasmap_current_view(config.rasmap_path, view_coord_sets)


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

    regen_kwargs = {
        "ras_object": ras_obj,
        "timeout": timeout,
        "close_after": True,
    }
    regen_signature = inspect.signature(MeshRegenerationWorkflow.regenerate_mesh)
    if "mannings_layer_name" in regen_signature.parameters:
        regen_kwargs["mannings_layer_name"] = "LandCover"

    result = MeshRegenerationWorkflow.regenerate_mesh(**regen_kwargs)

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
        n_cells = _read_hdf_2d_cell_count(handle, base)
        perimeter = _read_hdf_2d_perimeter(handle, base)
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
    refresh_geometry_display_metadata(config)
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

    expected_region_df = build_expected_region_mannings(
        region_df,
        lookup_df,
        default_value=config.region_default_manning,
    )
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

    expected_region_df = build_expected_region_mannings(
        region_df,
        lookup_df,
        default_value=config.region_default_manning,
    )
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
    ensure_project_has_geom_reference(config)
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
    for source, destination in zip(
        config.cross_section_srcs,
        config.cross_section_copies,
    ):
        copy_file(source, destination)
    if config.junction_bc_csv_src is not None and config.junction_bc_csv_copy is not None:
        copy_file(config.junction_bc_csv_src, config.junction_bc_csv_copy)
    if config.structure_csv_src is not None and config.structure_csv_copy is not None:
        copy_file(config.structure_csv_src, config.structure_csv_copy)

    write_minimal_project_file(config)
    ensure_project_has_geom_reference(config)
    write_minimal_rasmap(config)

    perimeter_ring = load_polygon_rings(config.perimeter_copy)[0]
    breakline_features = load_polyline_features(config.breakline_copy)
    breaklines = [coords for _, coords in breakline_features]
    cross_section_groups = load_cross_section_groups(config)
    sections = [
        section
        for group in cross_section_groups
        for section in group.sections
    ]

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
    dss_paths_by_group = choose_dss_paths_for_groups(
        dss_catalog,
        cross_section_groups,
        preferred_f_part=config.preferred_dss_f_part,
        fallback_path=preferred_dss_path,
    )
    dss_catalog.to_csv(config.dss_catalog_csv, index=False)

    boundary_df = create_boundary_artifacts(
        config,
        sections=sections,
        perimeter_ring=perimeter_ring,
        breaklines=breaklines,
        preferred_dss_path=preferred_dss_path,
        cross_section_groups=cross_section_groups,
        dss_paths_by_group=dss_paths_by_group,
    )
    boundary_features = _load_boundary_lines_from_shapefile(config.boundary_shp)
    _set_rasmap_current_view(
        config.rasmap_path,
        [
            perimeter_ring,
            *breaklines,
            *[item["coords"] for item in boundary_features],
        ],
    )

    generate_overview_plot(
        config,
        perimeter_ring=perimeter_ring,
        breaklines=breaklines,
        sections=sections,
    )

    downstream_rows = boundary_df[boundary_df["role"].astype(str) == "downstream"]
    downstream_slope = (
        float(downstream_rows.iloc[0]["normal_depth_slope"])
        if not downstream_rows.empty
        else estimate_normal_depth_slope(sections)
    )
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
        "cross_section_groups": [
            {
                "name": group.name,
                "source": group.source_path,
                "copy": group.copy_path,
                "section_count": len(group.sections),
            }
            for group in cross_section_groups
        ],
        "boundary_shp": config.boundary_shp,
        "dss_catalog_csv": config.dss_catalog_csv,
        "preferred_dss_path": preferred_dss_path,
        "dss_paths_by_group": dss_paths_by_group,
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
        "--results-root",
        type=Path,
        default=DEFAULT_RESULTS_ROOT,
        help="Root folder where compute output folders will be written.",
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
        default=None,
        help=(
            "Optional existing HEC-RAS landcover TIFF to register as the "
            "native LandCover map layer."
        ),
    )
    parser.add_argument(
        "--existing-landcover-hdf",
        type=Path,
        default=None,
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

    subparsers.add_parser(
        "create-unsteady-plan",
        help=(
            "Create/register .u## and .p## files for a 2D unsteady model "
            "using the prepared geometry and boundary artifacts."
        ),
    )

    compute_parser = subparsers.add_parser(
        "compute-plan",
        help="Run the current 2D unsteady plan and write results to a folder.",
    )
    compute_parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help=(
            "Optional compute output folder. Defaults to "
            "<results-root>/<project-name>_plan<plan-number>_run."
        ),
    )
    compute_parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace the compute output folder if it already exists.",
    )
    compute_parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="Maximum seconds to wait for HEC-RAS compute.",
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
        "results_root": DEFAULT_RESULTS_ROOT.resolve(),
        "project_name": DEFAULT_PROJECT_NAME,
        "ras_exe": DEFAULT_RAS_EXE.resolve(),
        "gdal_grid_exe": DEFAULT_GDAL_GRID.resolve(),
        "mesh_cell_size": 10.0,
        "existing_landcover_tif_name": None,
        "existing_landcover_hdf_name": None,
    }

    if args.config_json:
        config_json = args.config_json.resolve()
        values.update(load_study_area_overrides(config_json))

    cli_overrides = {
        "files_root": args.files_root.resolve(),
        "working_root": args.working_root.resolve(),
        "results_root": args.results_root.resolve(),
        "project_name": args.project_name,
        "ras_exe": args.ras_exe.resolve(),
        "gdal_grid_exe": args.gdal_grid_exe.resolve(),
        "mesh_cell_size": args.mesh_cell_size,
        "existing_landcover_tif_name": _resolved_optional_path_string(
            args.existing_landcover_tif
        ),
        "existing_landcover_hdf_name": _resolved_optional_path_string(
            args.existing_landcover_hdf
        ),
    }
    cli_defaults = {
        "files_root": DEFAULT_FILES_ROOT.resolve(),
        "working_root": DEFAULT_WORKING_ROOT.resolve(),
        "results_root": DEFAULT_RESULTS_ROOT.resolve(),
        "project_name": DEFAULT_PROJECT_NAME,
        "ras_exe": DEFAULT_RAS_EXE.resolve(),
        "gdal_grid_exe": DEFAULT_GDAL_GRID.resolve(),
        "mesh_cell_size": 10.0,
        "existing_landcover_tif_name": None,
        "existing_landcover_hdf_name": None,
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

        if args.command == "create-unsteady-plan":
            summary = create_unsteady_plan(config)
            print(json.dumps(_to_jsonable(summary), indent=2))
            return 0

        if args.command == "compute-plan":
            summary = compute_plan(
                config,
                output_dir=args.output_dir,
                overwrite=args.overwrite,
                timeout_seconds=args.timeout,
            )
            print(json.dumps(_to_jsonable(summary), indent=2))
            return 0 if summary["success"] else 1

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
        LOGGER.exception("rasmapper_v01.py failed")
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    exit_code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(exit_code)
