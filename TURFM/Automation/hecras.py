from __future__ import annotations

import logging
import math
import re
import sys
import time
import tomllib
import warnings
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, asdict, field, replace
from pathlib import Path
from typing import Any, Optional

import geopandas as gpd
import h5py
import numpy as np
import pandas as pd
from shapely.geometry import LineString, Point, box
from shapely.ops import linemerge, nearest_points, substring

def _find_repo_root(start_path: Path) -> Path:
    """Find the nearest ancestor containing the ras_commander package."""
    for candidate in (start_path, *start_path.parents):
        if (candidate / "ras_commander").is_dir():
            return candidate
    return start_path


REPO_ROOT = _find_repo_root(Path(__file__).resolve().parent)
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ras_commander import init_ras_project, RasCmdr
from ras_commander.geom import GeomBridge, GeomCrossSection, GeomCulvert, GeomParser
from ras_commander.hdf import HdfResultsPlan

try:
    from Automation.DTM import DTMChannelModifier
except ImportError:
    try:
        from DTM import DTMChannelModifier
    except ImportError:
        DTMChannelModifier = None

logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

try:
    import win32com.client

    WIN32COM_AVAILABLE = True
except ImportError:
    WIN32COM_AVAILABLE = False
    win32com = None


@dataclass
class SectionData:
    source_station: int
    river_station: float
    line: LineString
    station_elevation: pd.DataFrame
    left_bank_station: float
    right_bank_station: float
    left_bank_point: Point
    right_bank_point: Point
    channel_point: Point
    left_bank_measure: float = 0.0
    right_bank_measure: float = 0.0
    centerline_measure: float = 0.0
    left_reach_length: float = 0.0
    channel_reach_length: float = 0.0
    right_reach_length: float = 0.0
    hdf_left_bank_station: Optional[float] = None
    hdf_right_bank_station: Optional[float] = None


@dataclass
class BuildResult:
    project_folder: str
    project_file: str
    geometry_file: str
    flow_file: str
    plan_file: str
    sdf_file: str
    river: str
    reach: str
    section_count: int
    downstream_friction_slope: float
    flow_cms: float
    skipped_source_stations: list[int]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HydrologyPointSelection:
    point_name: str
    point_id: str
    buffer_distance: float
    candidate_count: int
    distance_to_river: float
    x: float
    y: float
    q_values: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FlowRunResult:
    return_period: str
    discharge_cms: float
    run_folder: str
    project_file: str
    plan_hdf_file: str
    compute_success: bool
    solution: str
    out_of_bank: Optional[bool]
    max_bank_excess_m: Optional[float]
    overflow_sections: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FlowScreeningResult:
    master_folder: str
    report_csv: str
    report_txt: str
    message: str
    candidate_point_count: int
    selected_point: Optional[dict[str, Any]] = None
    tested_return_periods: list[str] = field(default_factory=list)
    overflow_return_periods: list[str] = field(default_factory=list)
    run_results: list[dict[str, Any]] = field(default_factory=list)
    max_safe_return_period: Optional[str] = None
    max_safe_flow_cms: Optional[float] = None
    final_model_return_period: Optional[str] = None
    final_model_flow_cms: Optional[float] = None
    final_model_reason: Optional[str] = None
    final_build: Optional[dict[str, Any]] = None
    final_compute_success: Optional[bool] = None
    final_solution: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class JunctionReferenceTemplate:
    project_stem: str
    project_title: str
    geom_title: str
    junction_name: Optional[str] = None
    upstream_reaches: list[tuple[str, str]] = field(default_factory=list)
    downstream_reach: Optional[tuple[str, str]] = None
    junction_lengths_angles: list[str] = field(default_factory=list)


@dataclass
class StructureData:
    structure_type: str
    name: str
    river_station: float
    culvert_shape_code: int
    culvert_shape_name: str
    culvert_span: float
    culvert_rise: float
    culvert_length: float
    upstream_invert: float
    downstream_invert: float
    upstream_opening_station: float
    downstream_opening_station: float
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
    upstream_deck_stations: list[float]
    upstream_deck_elevations: list[float]
    upstream_low_chords: list[float]
    downstream_deck_stations: list[float]
    downstream_deck_elevations: list[float]
    downstream_low_chords: list[float]
    upstream_section_station: float
    downstream_section_station: float
    htab_hwmax: float = 0.0
    htab_max_flow: float = 50.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class HECRAS:
    """
    Build and optionally run a simple 1D steady HEC-RAS project.

    The main workflow is:
    1. Read cross section XYZ data from CSV.
    2. Read two bank polylines from a shapefile.
    3. Derive bank stations, a sampled bank-midpoint centerline, and
       reach lengths.
    4. Write a minimal `.prj`, `.g01`, `.f01`, `.p01`, and `RASImport.sdf`
       set.
    """

    LOWER_REACH_SUFFIX = "-L"
    LEGACY_LOWER_REACH_SUFFIX = "-Lower"
    BANK_LINE_OFFSET_M = 0.2

    def __init__(
        self,
        hecras_version: str = "RAS67.HECRASController",
        ras_exe_path: Optional[str | Path] = None,
    ) -> None:
        self.hecras_version = hecras_version
        self.ras_exe_path = (
            Path(ras_exe_path)
            if ras_exe_path is not None
            else Path(r"C:\Program Files (x86)\HEC\HEC-RAS\6.7 Beta 4\Ras.exe")
        )
        self.hec = None
        self.project_path: Optional[Path] = None
        self.project_name: Optional[str] = None
        self.last_build: Optional[BuildResult] = None
        self.source_crs: str = "UNKNOWN"
        self.target_projection_crs: Optional[str] = None

    def connect(self) -> bool:
        """Establish a COM connection to HEC-RAS if win32com is available."""
        if not WIN32COM_AVAILABLE:
            logger.warning("win32com is not available; COM automation is disabled.")
            return False

        try:
            self.hec = win32com.client.gencache.EnsureDispatch(
                self.hecras_version
            )
            logger.info("Connected to HEC-RAS controller: %s", self.hecras_version)
            return True
        except Exception as exc:
            logger.error("Failed to connect to HEC-RAS controller: %s", exc)
            return False

    def disconnect(self) -> None:
        """Close the HEC-RAS COM connection."""
        if self.hec is None:
            return

        try:
            self.hec.Project_Save()
            self.hec.QuitRas()
        except Exception as exc:
            logger.warning("Error while closing HEC-RAS: %s", exc)
        finally:
            self.hec = None

    def open_project(self, project_path: str | Path, project_name: str) -> bool:
        """Open an existing HEC-RAS project through COM."""
        if self.hec is None and not self.connect():
            return False

        prj_path = Path(project_path) / f"{project_name}.prj"
        try:
            self.hec.Project_Open(str(prj_path))
            self.project_path = Path(project_path)
            self.project_name = project_name
            logger.info("Opened project in HEC-RAS: %s", prj_path)
            return True
        except Exception as exc:
            logger.error("Failed to open project %s: %s", prj_path, exc)
            return False

    def save_project(self) -> None:
        """Save the currently open HEC-RAS project."""
        if self.hec is None:
            return

        try:
            self.hec.Project_Save()
        except Exception as exc:
            logger.error("Failed to save project: %s", exc)

    def run_simulation(self) -> tuple[bool, str]:
        """Run the active HEC-RAS plan through COM."""
        if self.hec is None:
            return False, "HEC-RAS controller is not connected."

        try:
            result = self.hec.Compute_CurrentPlan()
            return True, str(result)
        except Exception as exc:
            return False, str(exc)

    def show_window(self, delay_seconds: int = 3) -> None:
        """Show the HEC-RAS window if COM automation is active."""
        if self.hec is None:
            return

        try:
            self.hec.ShowRAS()
            time.sleep(delay_seconds)
        except Exception as exc:
            logger.warning("Could not show HEC-RAS window: %s", exc)

    def build_steady_1d_model(
        self,
        project_folder: str | Path,
        project_stem: str,
        project_title: str,
        cross_section_csv: str | Path,
        bank_lines_shp: str | Path,
        structure_csv: Optional[str | Path] = None,
        flow_cms: float = 13.0,
        projection_file: Optional[str | Path] = None,
        profile_name: str = "PF 1",
        min_section_spacing: float = 0.0,
        channel_mannings_n: float = 0.025,
        overbank_mannings_n: float = 0.025,
        expansion_coeff: float = 0.30,
        contraction_coeff: float = 0.10,
        river_station_step: float = 100.0,
        centerline_samples_per_segment: int = 6,
        bank_station_mode: str = "snap",
        river_line_method: str = "simple_distance",
        return_period: Optional[str] = None,
    ) -> BuildResult:
        """
        Build a minimal steady 1D HEC-RAS project in `project_folder`.
        """
        self._set_target_projection_crs(projection_file)
        geometry_context = self._prepare_geometry_context(
            cross_section_csv=cross_section_csv,
            bank_lines_shp=bank_lines_shp,
            structure_csv=structure_csv,
            min_section_spacing=min_section_spacing,
            river_station_step=river_station_step,
            centerline_samples_per_segment=centerline_samples_per_segment,
            bank_station_mode=bank_station_mode,
            river_line_method=river_line_method,
        )
        return self._write_model_files(
            project_folder=project_folder,
            project_stem=project_stem,
            project_title=project_title,
            projection_file=projection_file,
            profile_name=profile_name,
            flow_cms=flow_cms,
            channel_mannings_n=channel_mannings_n,
            overbank_mannings_n=overbank_mannings_n,
            expansion_coeff=expansion_coeff,
            contraction_coeff=contraction_coeff,
            geometry_context=geometry_context,
            return_period=return_period,
        )

    def screen_steady_flows_from_kmz(
        self,
        project_folder: str | Path,
        project_stem: str,
        project_title: str,
        cross_section_csv: str | Path,
        bank_lines_shp: str | Path,
        hydrology_kmz: str | Path,
        buffer_distance: float,
        structure_csv: Optional[str | Path] = None,
        projection_file: Optional[str | Path] = None,
        profile_name: str = "PF 1",
        min_section_spacing: float = 0.0,
        channel_mannings_n: float = 0.025,
        overbank_mannings_n: float = 0.025,
        expansion_coeff: float = 0.30,
        contraction_coeff: float = 0.10,
        river_station_step: float = 100.0,
        centerline_samples_per_segment: int = 6,
        outflow_tolerance_m: float = 0.05,
        return_periods: Optional[list[str]] = None,
        bank_station_mode: str = "snap",
        river_line_method: str = "simple_distance",
        all_flows_in_single_plan: bool = False,
        prepare_geometry_hdf: bool = True,
    ) -> FlowScreeningResult:
        project_folder = Path(project_folder)
        project_folder.mkdir(parents=True, exist_ok=True)
        report_csv = project_folder / "flow_screening_report.csv"
        report_txt = project_folder / "flow_screening_report.txt"
        requested_return_periods = list(
            return_periods
            if return_periods is not None
            else ["Q1000", "Q500", "Q100", "Q50", "Q25", "Q10", "Q5"]
        )

        self._set_target_projection_crs(projection_file)
        geometry_context = self._prepare_geometry_context(
            cross_section_csv=cross_section_csv,
            bank_lines_shp=bank_lines_shp,
            structure_csv=structure_csv,
            min_section_spacing=min_section_spacing,
            river_station_step=river_station_step,
            centerline_samples_per_segment=centerline_samples_per_segment,
            bank_station_mode=bank_station_mode,
            river_line_method=river_line_method,
        )
        selection = self._select_hydrology_point(
            hydrology_kmz=hydrology_kmz,
            centerline_line=geometry_context["centerline_line"],
            buffer_distance=buffer_distance,
        )

        if selection is None:
            message = (
                "No hydrology points fell within "
                f"{self._format_number(buffer_distance, decimals=2)} m of the "
                "river line. Increase the buffer size and rerun the notebook."
            )
            self._write_flow_screening_csv(report_csv=report_csv, run_results=[])
            self._write_flow_screening_text_report(
                report_txt=report_txt,
                message=message,
                buffer_distance=buffer_distance,
                selection=None,
                run_results=[],
                max_safe_run=None,
                final_model_run=None,
            )
            return FlowScreeningResult(
                master_folder=str(project_folder),
                report_csv=str(report_csv),
                report_txt=str(report_txt),
                message=message,
                candidate_point_count=0,
                tested_return_periods=[],
            )

        if all_flows_in_single_plan:
            return self._screen_steady_flows_in_single_plan(
                project_folder=project_folder,
                project_stem=project_stem,
                project_title=project_title,
                projection_file=projection_file,
                geometry_context=geometry_context,
                selection=selection,
                requested_return_periods=requested_return_periods,
                report_csv=report_csv,
                report_txt=report_txt,
                buffer_distance=buffer_distance,
                channel_mannings_n=channel_mannings_n,
                overbank_mannings_n=overbank_mannings_n,
                expansion_coeff=expansion_coeff,
                contraction_coeff=contraction_coeff,
                outflow_tolerance_m=outflow_tolerance_m,
                prepare_geometry_hdf=prepare_geometry_hdf,
            )

        run_root = project_folder / "runs"
        run_root.mkdir(parents=True, exist_ok=True)
        run_results: list[FlowRunResult] = []
        executed_return_periods: list[str] = []
        screening_stopped_early = False
        for return_period in requested_return_periods:
            executed_return_periods.append(return_period)
            discharge_cms = selection.q_values.get(return_period)
            if discharge_cms is None or not np.isfinite(discharge_cms):
                run_results.append(
                    FlowRunResult(
                        return_period=return_period,
                        discharge_cms=np.nan,
                        run_folder="",
                        project_file="",
                        plan_hdf_file="",
                        compute_success=False,
                        solution="",
                        out_of_bank=None,
                        max_bank_excess_m=None,
                        note="No discharge value was available in the KMZ point.",
                    )
                )
                continue

            run_folder = run_root / self._flow_run_folder_name(
                return_period,
                discharge_cms,
            )
            build_result = self._write_model_files(
                project_folder=run_folder,
                project_stem=project_stem,
                project_title=project_title,
                projection_file=projection_file,
                profile_name=return_period,
                flow_cms=discharge_cms,
                channel_mannings_n=channel_mannings_n,
                overbank_mannings_n=overbank_mannings_n,
                expansion_coeff=expansion_coeff,
                contraction_coeff=contraction_coeff,
                geometry_context=geometry_context,
                return_period=return_period,
            )
            compute_info = self.compute_project(
                project_folder=run_folder,
                project_stem=project_stem,
                plan_number="01",
                geometry_context=geometry_context,
                prepare_geometry_hdf=prepare_geometry_hdf,
            )
            out_of_bank = None
            max_bank_excess_m = None
            overflow_sections: list[str] = []
            note = ""
            if compute_info["compute_success"] and compute_info["plan_hdf_file"]:
                overflow_info = self._evaluate_steady_outflow(
                    plan_hdf_path=Path(compute_info["plan_hdf_file"]),
                    sections=geometry_context["sections"],
                    profile_name=return_period,
                    tolerance_m=outflow_tolerance_m,
                )
                out_of_bank = overflow_info["out_of_bank"]
                max_bank_excess_m = overflow_info["max_bank_excess_m"]
                overflow_sections = overflow_info["overflow_sections"]
                note = overflow_info["note"]
            else:
                note = "HEC-RAS compute did not produce steady results."

            run_results.append(
                FlowRunResult(
                    return_period=return_period,
                    discharge_cms=float(discharge_cms),
                    run_folder=str(run_folder),
                    project_file=build_result.project_file,
                    plan_hdf_file=str(compute_info["plan_hdf_file"]),
                    compute_success=bool(compute_info["compute_success"]),
                    solution=str(compute_info["solution"]),
                    out_of_bank=out_of_bank,
                    max_bank_excess_m=max_bank_excess_m,
                    overflow_sections=overflow_sections,
                    note=note,
                )
            )

            if compute_info["compute_success"] and out_of_bank is False:
                screening_stopped_early = True
                break

        max_safe_run = next(
            (
                result
                for result in run_results
                if result.compute_success and result.out_of_bank is False
            ),
            None,
        )
        final_model_run = max_safe_run
        final_model_reason = None
        if final_model_run is not None:
            final_model_reason = "max_safe_flow"
        else:
            successful_runs = [
                result for result in run_results if result.compute_success
            ]
            if successful_runs:
                final_model_run = successful_runs[-1]
                final_model_reason = "lowest_successful_flow_for_review"

        final_build = None
        final_compute_success = None
        final_solution = None
        if final_model_run is not None and np.isfinite(final_model_run.discharge_cms):
            final_build_obj = self._write_model_files(
                project_folder=project_folder,
                project_stem=project_stem,
                project_title=project_title,
                projection_file=projection_file,
                profile_name=final_model_run.return_period,
                flow_cms=final_model_run.discharge_cms,
                channel_mannings_n=channel_mannings_n,
                overbank_mannings_n=overbank_mannings_n,
                expansion_coeff=expansion_coeff,
                contraction_coeff=contraction_coeff,
                geometry_context=geometry_context,
                return_period=final_model_run.return_period,
            )
            final_build = final_build_obj.to_dict()
            final_compute_info = self.compute_project(
                project_folder=project_folder,
                project_stem=project_stem,
                plan_number="01",
                geometry_context=geometry_context,
                prepare_geometry_hdf=prepare_geometry_hdf,
            )
            final_compute_success = bool(final_compute_info["compute_success"])
            final_solution = str(final_compute_info["solution"])

        overflow_return_periods = [
            result.return_period
            for result in run_results
            if result.out_of_bank is True
        ]
        message = self._build_flow_screening_message(
            selection=selection,
            max_safe_run=max_safe_run,
            final_model_run=final_model_run,
            final_model_reason=final_model_reason,
            buffer_distance=buffer_distance,
            screening_stopped_early=screening_stopped_early,
            all_flows_in_single_plan=False,
        )
        self._write_flow_screening_csv(report_csv=report_csv, run_results=run_results)
        self._write_flow_screening_text_report(
            report_txt=report_txt,
            message=message,
            buffer_distance=buffer_distance,
            selection=selection,
            run_results=run_results,
            max_safe_run=max_safe_run,
            final_model_run=final_model_run,
        )
        return FlowScreeningResult(
            master_folder=str(project_folder),
            report_csv=str(report_csv),
            report_txt=str(report_txt),
            message=message,
            candidate_point_count=selection.candidate_count,
            selected_point=selection.to_dict(),
            tested_return_periods=executed_return_periods,
            overflow_return_periods=overflow_return_periods,
            run_results=[result.to_dict() for result in run_results],
            max_safe_return_period=(
                max_safe_run.return_period if max_safe_run is not None else None
            ),
            max_safe_flow_cms=(
                max_safe_run.discharge_cms if max_safe_run is not None else None
            ),
            final_model_return_period=(
                final_model_run.return_period if final_model_run is not None else None
            ),
            final_model_flow_cms=(
                final_model_run.discharge_cms if final_model_run is not None else None
            ),
            final_model_reason=final_model_reason,
            final_build=final_build,
            final_compute_success=final_compute_success,
            final_solution=final_solution,
        )

    def _screen_steady_flows_in_single_plan(
        self,
        project_folder: Path,
        project_stem: str,
        project_title: str,
        projection_file: Optional[str | Path],
        geometry_context: dict[str, Any],
        selection: HydrologyPointSelection,
        requested_return_periods: list[str],
        report_csv: Path,
        report_txt: Path,
        buffer_distance: float,
        channel_mannings_n: float,
        overbank_mannings_n: float,
        expansion_coeff: float,
        contraction_coeff: float,
        outflow_tolerance_m: float,
        prepare_geometry_hdf: bool = True,
    ) -> FlowScreeningResult:
        flow_profiles: list[tuple[str, float]] = []
        missing_results: dict[str, FlowRunResult] = {}
        for return_period in requested_return_periods:
            discharge_cms = selection.q_values.get(return_period)
            if discharge_cms is None or not np.isfinite(discharge_cms):
                missing_results[return_period] = FlowRunResult(
                    return_period=return_period,
                    discharge_cms=np.nan,
                    run_folder="",
                    project_file="",
                    plan_hdf_file="",
                    compute_success=False,
                    solution="",
                    out_of_bank=None,
                    max_bank_excess_m=None,
                    note="No discharge value was available in the KMZ point.",
                )
                continue
            flow_profiles.append((return_period, float(discharge_cms)))

        run_results: list[FlowRunResult] = []
        if not flow_profiles:
            for return_period in requested_return_periods:
                run_results.append(missing_results[return_period])
            message = (
                f"Selected hydrology point {selection.point_name} "
                f"({selection.point_id}) had no usable design discharge values."
            )
            self._write_flow_screening_csv(report_csv=report_csv, run_results=run_results)
            self._write_flow_screening_text_report(
                report_txt=report_txt,
                message=message,
                buffer_distance=buffer_distance,
                selection=selection,
                run_results=run_results,
                max_safe_run=None,
                final_model_run=None,
            )
            return FlowScreeningResult(
                master_folder=str(project_folder),
                report_csv=str(report_csv),
                report_txt=str(report_txt),
                message=message,
                candidate_point_count=selection.candidate_count,
                selected_point=selection.to_dict(),
                tested_return_periods=requested_return_periods,
                run_results=[result.to_dict() for result in run_results],
            )

        first_profile, first_flow = flow_profiles[0]
        build_result = self._write_model_files(
            project_folder=project_folder,
            project_stem=project_stem,
            project_title=project_title,
            projection_file=projection_file,
            profile_name=first_profile,
            flow_cms=first_flow,
            channel_mannings_n=channel_mannings_n,
            overbank_mannings_n=overbank_mannings_n,
            expansion_coeff=expansion_coeff,
            contraction_coeff=contraction_coeff,
            geometry_context=geometry_context,
            return_period=first_profile,
            flow_profiles=flow_profiles,
        )
        compute_info = self.compute_project(
            project_folder=project_folder,
            project_stem=project_stem,
            plan_number="01",
            geometry_context=geometry_context,
            prepare_geometry_hdf=prepare_geometry_hdf,
        )
        flow_lookup = dict(flow_profiles)
        for return_period in requested_return_periods:
            if return_period in missing_results:
                run_results.append(missing_results[return_period])
                continue

            out_of_bank = None
            max_bank_excess_m = None
            overflow_sections: list[str] = []
            note = ""
            if compute_info["compute_success"] and compute_info["plan_hdf_file"]:
                try:
                    overflow_info = self._evaluate_steady_outflow(
                        plan_hdf_path=Path(compute_info["plan_hdf_file"]),
                        sections=geometry_context["sections"],
                        profile_name=return_period,
                        tolerance_m=outflow_tolerance_m,
                    )
                    out_of_bank = overflow_info["out_of_bank"]
                    max_bank_excess_m = overflow_info["max_bank_excess_m"]
                    overflow_sections = overflow_info["overflow_sections"]
                    note = overflow_info["note"]
                except Exception as exc:
                    note = f"Could not evaluate profile {return_period}: {exc}"
            else:
                note = "HEC-RAS compute did not produce steady results."

            run_results.append(
                FlowRunResult(
                    return_period=return_period,
                    discharge_cms=float(flow_lookup[return_period]),
                    run_folder=str(project_folder),
                    project_file=build_result.project_file,
                    plan_hdf_file=str(compute_info["plan_hdf_file"]),
                    compute_success=bool(compute_info["compute_success"]),
                    solution=str(compute_info["solution"]),
                    out_of_bank=out_of_bank,
                    max_bank_excess_m=max_bank_excess_m,
                    overflow_sections=overflow_sections,
                    note=note,
                )
            )

        max_safe_run = next(
            (
                result
                for result in run_results
                if result.compute_success and result.out_of_bank is False
            ),
            None,
        )
        final_model_run = max_safe_run
        final_model_reason = "max_safe_flow" if max_safe_run is not None else None
        if final_model_run is None:
            successful_runs = [
                result for result in run_results if result.compute_success
            ]
            if successful_runs:
                final_model_run = successful_runs[-1]
                final_model_reason = "lowest_successful_flow_for_review"

        overflow_return_periods = [
            result.return_period
            for result in run_results
            if result.out_of_bank is True
        ]
        message = self._build_flow_screening_message(
            selection=selection,
            max_safe_run=max_safe_run,
            final_model_run=final_model_run,
            final_model_reason=final_model_reason,
            buffer_distance=buffer_distance,
            screening_stopped_early=False,
            all_flows_in_single_plan=True,
        )
        self._write_flow_screening_csv(report_csv=report_csv, run_results=run_results)
        self._write_flow_screening_text_report(
            report_txt=report_txt,
            message=message,
            buffer_distance=buffer_distance,
            selection=selection,
            run_results=run_results,
            max_safe_run=max_safe_run,
            final_model_run=final_model_run,
        )
        return FlowScreeningResult(
            master_folder=str(project_folder),
            report_csv=str(report_csv),
            report_txt=str(report_txt),
            message=message,
            candidate_point_count=selection.candidate_count,
            selected_point=selection.to_dict(),
            tested_return_periods=requested_return_periods,
            overflow_return_periods=overflow_return_periods,
            run_results=[result.to_dict() for result in run_results],
            max_safe_return_period=(
                max_safe_run.return_period if max_safe_run is not None else None
            ),
            max_safe_flow_cms=(
                max_safe_run.discharge_cms if max_safe_run is not None else None
            ),
            final_model_return_period=(
                final_model_run.return_period if final_model_run is not None else None
            ),
            final_model_flow_cms=(
                final_model_run.discharge_cms if final_model_run is not None else None
            ),
            final_model_reason=final_model_reason,
            final_build=build_result.to_dict(),
            final_compute_success=bool(compute_info["compute_success"]),
            final_solution=str(compute_info["solution"]),
        )

    def screen_steady_junction_flows_from_kmz(
        self,
        project_folder: str | Path,
        project_stem: str,
        project_title: str,
        main_cross_section_csv: str | Path,
        main_bank_lines_shp: str | Path,
        tributary_cross_section_csv: str | Path,
        tributary_bank_lines_shp: str | Path,
        hydrology_kmz: str | Path,
        buffer_distance: float,
        main_structure_csv: Optional[str | Path] = None,
        tributary_structure_csv: Optional[str | Path] = None,
        combined_bank_lines_shp: Optional[str | Path] = None,
        additional_reaches: Optional[list[dict[str, Any]]] = None,
        reference_geometry_file: Optional[str | Path] = None,
        projection_file: Optional[str | Path] = None,
        profile_name: str = "PF 1",
        min_section_spacing: float = 0.0,
        channel_mannings_n: float = 0.025,
        overbank_mannings_n: float = 0.025,
        expansion_coeff: float = 0.30,
        contraction_coeff: float = 0.10,
        river_station_step: float = 100.0,
        centerline_samples_per_segment: int = 6,
        outflow_tolerance_m: float = 0.05,
        return_periods: Optional[list[str]] = None,
        bank_station_mode: str = "snap",
        river_line_method: str = "simple_distance",
        all_flows_in_single_plan: bool = False,
        prepare_geometry_hdf: bool = True,
    ) -> FlowScreeningResult:
        project_folder = Path(project_folder)
        project_folder.mkdir(parents=True, exist_ok=True)
        report_csv = project_folder / "flow_screening_report.csv"
        report_txt = project_folder / "flow_screening_report.txt"
        requested_return_periods = list(
            return_periods
            if return_periods is not None
            else ["Q1000", "Q500", "Q100", "Q50", "Q25", "Q10", "Q5"]
        )

        self._set_target_projection_crs(projection_file)
        geometry_context = self._prepare_junction_geometry_context(
            main_cross_section_csv=main_cross_section_csv,
            main_bank_lines_shp=main_bank_lines_shp,
            tributary_cross_section_csv=tributary_cross_section_csv,
            tributary_bank_lines_shp=tributary_bank_lines_shp,
            main_structure_csv=main_structure_csv,
            tributary_structure_csv=tributary_structure_csv,
            combined_bank_lines_shp=combined_bank_lines_shp,
            additional_reaches=additional_reaches,
            reference_geometry_file=reference_geometry_file,
            min_section_spacing=min_section_spacing,
            river_station_step=river_station_step,
            centerline_samples_per_segment=centerline_samples_per_segment,
            bank_station_mode=bank_station_mode,
            river_line_method=river_line_method,
        )

        main_selection = self._select_hydrology_point(
            hydrology_kmz=hydrology_kmz,
            centerline_line=geometry_context["main_reach"]["centerline_line"],
            buffer_distance=buffer_distance,
        )
        tributary_selection = self._select_hydrology_point(
            hydrology_kmz=hydrology_kmz,
            centerline_line=geometry_context["tributary_reach"]["centerline_line"],
            buffer_distance=buffer_distance,
        )
        lower_selection = self._select_hydrology_point(
            hydrology_kmz=hydrology_kmz,
            centerline_line=geometry_context["lower_reach"]["centerline_line"],
            buffer_distance=buffer_distance,
        )

        if main_selection is None or tributary_selection is None:
            missing = []
            if main_selection is None:
                missing.append("main")
            if tributary_selection is None:
                missing.append("tributary")
            message = (
                "No hydrology point fell within "
                f"{self._format_number(buffer_distance, decimals=2)} m of the "
                f"{' and '.join(missing)} river line(s)."
            )
            self._write_flow_screening_csv(report_csv=report_csv, run_results=[])
            report_txt.write_text(message, encoding="utf-8")
            return FlowScreeningResult(
                master_folder=str(project_folder),
                report_csv=str(report_csv),
                report_txt=str(report_txt),
                message=message,
                candidate_point_count=0,
                tested_return_periods=[],
            )
        if lower_selection is not None and (
            lower_selection.point_id == main_selection.point_id
            or lower_selection.point_id == tributary_selection.point_id
        ):
            lower_selection = None
        independent_selections: list[tuple[dict[str, Any], HydrologyPointSelection]] = []
        missing_independent_reaches: list[str] = []
        for independent_reach in geometry_context.get("independent_reaches", []):
            independent_selection = self._select_hydrology_point(
                hydrology_kmz=hydrology_kmz,
                centerline_line=independent_reach["centerline_line"],
                buffer_distance=buffer_distance,
            )
            if independent_selection is None:
                missing_independent_reaches.append(
                    str(
                        independent_reach.get("selection_label")
                        or f"{independent_reach['river']}/{independent_reach['reach']}"
                    )
                )
                continue
            independent_selections.append((independent_reach, independent_selection))

        if missing_independent_reaches:
            message = (
                "No hydrology point fell within "
                f"{self._format_number(buffer_distance, decimals=2)} m of the "
                "independent river line(s): "
                f"{', '.join(missing_independent_reaches)}."
            )
            self._write_flow_screening_csv(report_csv=report_csv, run_results=[])
            report_txt.write_text(message, encoding="utf-8")
            return FlowScreeningResult(
                master_folder=str(project_folder),
                report_csv=str(report_csv),
                report_txt=str(report_txt),
                message=message,
                candidate_point_count=0,
                tested_return_periods=[],
            )

        if all_flows_in_single_plan:
            return self._screen_steady_junction_flows_in_single_plan(
                project_folder=project_folder,
                project_stem=project_stem,
                project_title=project_title,
                projection_file=projection_file,
                geometry_context=geometry_context,
                main_selection=main_selection,
                tributary_selection=tributary_selection,
                lower_selection=lower_selection,
                independent_selections=independent_selections,
                requested_return_periods=requested_return_periods,
                report_csv=report_csv,
                report_txt=report_txt,
                buffer_distance=buffer_distance,
                channel_mannings_n=channel_mannings_n,
                overbank_mannings_n=overbank_mannings_n,
                expansion_coeff=expansion_coeff,
                contraction_coeff=contraction_coeff,
                outflow_tolerance_m=outflow_tolerance_m,
                prepare_geometry_hdf=prepare_geometry_hdf,
            )

        run_root = project_folder / "runs"
        run_root.mkdir(parents=True, exist_ok=True)
        run_results: list[FlowRunResult] = []
        executed_return_periods: list[str] = []
        screening_stopped_early = False

        for return_period in requested_return_periods:
            executed_return_periods.append(return_period)
            main_flow = main_selection.q_values.get(return_period)
            tributary_flow = tributary_selection.q_values.get(return_period)
            lower_point_flow = (
                lower_selection.q_values.get(return_period)
                if lower_selection is not None
                else None
            )
            if (
                main_flow is None
                or tributary_flow is None
                or not np.isfinite(main_flow)
                or not np.isfinite(tributary_flow)
            ):
                run_results.append(
                    FlowRunResult(
                        return_period=return_period,
                        discharge_cms=np.nan,
                        run_folder="",
                        project_file="",
                        plan_hdf_file="",
                        compute_success=False,
                        solution="",
                        out_of_bank=None,
                        max_bank_excess_m=None,
                        note=(
                            "Missing discharge value for one or both upstream "
                            f"hydrology points at {return_period}."
                        ),
                    )
                )
                continue

            independent_flow_values: dict[str, float] = {}
            independent_flow_notes: list[dict[str, Any]] = []
            missing_independent_flows: list[str] = []
            for independent_reach, independent_selection in independent_selections:
                independent_flow = independent_selection.q_values.get(return_period)
                label = str(
                    independent_reach.get("selection_label")
                    or independent_selection.point_name
                )
                if independent_flow is None or not np.isfinite(independent_flow):
                    missing_independent_flows.append(label)
                    continue
                flow_key = str(independent_reach["flow_key"])
                independent_flow_values[flow_key] = float(independent_flow)
                independent_flow_notes.append(
                    {
                        "label": label,
                        "point": independent_selection.point_name,
                        "flow_key": flow_key,
                        "flow": float(independent_flow),
                    }
                )
            if missing_independent_flows:
                missing_results[return_period] = FlowRunResult(
                    return_period=return_period,
                    discharge_cms=np.nan,
                    run_folder="",
                    project_file="",
                    plan_hdf_file="",
                    compute_success=False,
                    solution="",
                    out_of_bank=None,
                    max_bank_excess_m=None,
                    note=(
                        "Missing discharge value for independent reach(es) "
                        f"{', '.join(missing_independent_flows)} at {return_period}."
                    ),
                )
                continue

            upstream_sum_flow = float(main_flow) + float(tributary_flow)
            lower_flow_source = "sum_of_upstream"
            lower_flow = upstream_sum_flow
            if lower_point_flow is not None and np.isfinite(lower_point_flow):
                lower_point_flow = float(lower_point_flow)
                if lower_point_flow > upstream_sum_flow:
                    lower_flow = lower_point_flow
                    lower_flow_source = "downstream_main_point"
            run_folder = run_root / self._flow_run_folder_name(
                return_period,
                lower_flow,
            )
            logger.info(
                "Junction flow selection %s | main=%s cms | tributary=%s cms | "
                "sum=%s cms | downstream_point=%s | chosen_lower=%s cms | "
                "source=%s",
                return_period,
                self._format_number(main_flow, 3),
                self._format_number(tributary_flow, 3),
                self._format_number(upstream_sum_flow, 3),
                (
                    f"{self._format_number(lower_point_flow, 3)} cms"
                    if lower_point_flow is not None and np.isfinite(lower_point_flow)
                    else "NA"
                ),
                self._format_number(lower_flow, 3),
                lower_flow_source,
            )
            build_result = self._write_model_files(
                project_folder=run_folder,
                project_stem=project_stem,
                project_title=project_title,
                projection_file=projection_file,
                profile_name=return_period,
                flow_cms=lower_flow,
                channel_mannings_n=channel_mannings_n,
                overbank_mannings_n=overbank_mannings_n,
                expansion_coeff=expansion_coeff,
                contraction_coeff=contraction_coeff,
                geometry_context=geometry_context,
                flow_profile={
                    "main": float(main_flow),
                    "tributary": float(tributary_flow),
                    "lower": float(lower_flow),
                },
                return_period=return_period,
            )
            compute_info = self.compute_project(
                project_folder=run_folder,
                project_stem=project_stem,
                plan_number="01",
                geometry_context=geometry_context,
                prepare_geometry_hdf=prepare_geometry_hdf,
            )
            out_of_bank = None
            max_bank_excess_m = None
            overflow_sections: list[str] = []
            note = (
                "main="
                f"{self._format_number(main_flow, 3)} cms | tributary="
                f"{self._format_number(tributary_flow, 3)} cms | upstream_sum="
                f"{self._format_number(upstream_sum_flow, 3)} cms | downstream_point="
                f"{self._format_number(lower_point_flow, 3) if lower_point_flow is not None and np.isfinite(lower_point_flow) else 'NA'}"
                " cms"
                " | lower_source="
                f"{lower_flow_source} | lower="
                f"{self._format_number(lower_flow, 3)} cms"
            )
            if compute_info["compute_success"] and compute_info["plan_hdf_file"]:
                overflow_info = self._evaluate_steady_outflow(
                    plan_hdf_path=Path(compute_info["plan_hdf_file"]),
                    sections=geometry_context["all_sections"],
                    profile_name=return_period,
                    tolerance_m=outflow_tolerance_m,
                )
                out_of_bank = overflow_info["out_of_bank"]
                max_bank_excess_m = overflow_info["max_bank_excess_m"]
                overflow_sections = overflow_info["overflow_sections"]
                note = f"{note} | {overflow_info['note']}"
            else:
                note = f"{note} | HEC-RAS compute did not produce steady results."

            run_results.append(
                FlowRunResult(
                    return_period=return_period,
                    discharge_cms=float(lower_flow),
                    run_folder=str(run_folder),
                    project_file=build_result.project_file,
                    plan_hdf_file=str(compute_info["plan_hdf_file"]),
                    compute_success=bool(compute_info["compute_success"]),
                    solution=str(compute_info["solution"]),
                    out_of_bank=out_of_bank,
                    max_bank_excess_m=max_bank_excess_m,
                    overflow_sections=overflow_sections,
                    note=note,
                )
            )

            if compute_info["compute_success"] and out_of_bank is False:
                screening_stopped_early = True
                break

        max_safe_run = next(
            (
                result
                for result in run_results
                if result.compute_success and result.out_of_bank is False
            ),
            None,
        )
        final_model_run = max_safe_run
        final_model_reason = "max_safe_flow" if max_safe_run is not None else None
        if final_model_run is None:
            finite_runs = [
                result
                for result in run_results
                if np.isfinite(result.discharge_cms)
            ]
            if finite_runs:
                final_model_run = finite_runs[-1]
                final_model_reason = "lowest_tested_flow_for_review"
        final_build = None
        final_compute_success = None
        final_solution = None

        if final_model_run is not None:
            flow_triplet = self._junction_flow_triplet_from_note(final_model_run.note)
            final_build_obj = self._write_model_files(
                project_folder=project_folder,
                project_stem=project_stem,
                project_title=project_title,
                projection_file=projection_file,
                profile_name=final_model_run.return_period,
                flow_cms=final_model_run.discharge_cms,
                channel_mannings_n=channel_mannings_n,
                overbank_mannings_n=overbank_mannings_n,
                expansion_coeff=expansion_coeff,
                contraction_coeff=contraction_coeff,
                geometry_context=geometry_context,
                flow_profile=flow_triplet,
                return_period=final_model_run.return_period,
            )
            final_build = final_build_obj.to_dict()
            final_compute_info = self.compute_project(
                project_folder=project_folder,
                project_stem=project_stem,
                plan_number="01",
                geometry_context=geometry_context,
                prepare_geometry_hdf=prepare_geometry_hdf,
            )
            final_compute_success = bool(final_compute_info["compute_success"])
            final_solution = str(final_compute_info["solution"])

        overflow_return_periods = [
            result.return_period
            for result in run_results
            if result.out_of_bank is True
        ]
        message = self._build_junction_flow_screening_message(
            main_selection=main_selection,
            tributary_selection=tributary_selection,
            lower_selection=lower_selection,
            max_safe_run=max_safe_run,
            buffer_distance=buffer_distance,
            screening_stopped_early=screening_stopped_early,
        )
        self._write_flow_screening_csv(report_csv=report_csv, run_results=run_results)
        self._write_junction_flow_screening_text_report(
            report_txt=report_txt,
            message=message,
            buffer_distance=buffer_distance,
            main_selection=main_selection,
            tributary_selection=tributary_selection,
            lower_selection=lower_selection,
            run_results=run_results,
            max_safe_run=max_safe_run,
        )
        return FlowScreeningResult(
            master_folder=str(project_folder),
            report_csv=str(report_csv),
            report_txt=str(report_txt),
            message=message,
            candidate_point_count=(
                main_selection.candidate_count
                + tributary_selection.candidate_count
                + (lower_selection.candidate_count if lower_selection is not None else 0)
                + sum(
                    selection.candidate_count
                    for _, selection in independent_selections
                )
            ),
            selected_point={
                "main": main_selection.to_dict(),
                "tributary": tributary_selection.to_dict(),
                "lower": (
                    lower_selection.to_dict() if lower_selection is not None else None
                ),
                "independent": [
                    {
                        "river": reach["river"],
                        "reach": reach["reach"],
                        "selection": selection.to_dict(),
                    }
                    for reach, selection in independent_selections
                ],
            },
            tested_return_periods=executed_return_periods,
            overflow_return_periods=overflow_return_periods,
            run_results=[result.to_dict() for result in run_results],
            max_safe_return_period=(
                max_safe_run.return_period if max_safe_run is not None else None
            ),
            max_safe_flow_cms=(
                max_safe_run.discharge_cms if max_safe_run is not None else None
            ),
            final_model_return_period=(
                final_model_run.return_period if final_model_run is not None else None
            ),
            final_model_flow_cms=(
                final_model_run.discharge_cms if final_model_run is not None else None
            ),
            final_model_reason=final_model_reason,
            final_build=final_build,
            final_compute_success=final_compute_success,
            final_solution=final_solution,
        )

    def _screen_steady_junction_flows_in_single_plan(
        self,
        project_folder: Path,
        project_stem: str,
        project_title: str,
        projection_file: Optional[str | Path],
        geometry_context: dict[str, Any],
        main_selection: HydrologyPointSelection,
        tributary_selection: HydrologyPointSelection,
        lower_selection: Optional[HydrologyPointSelection],
        independent_selections: list[tuple[dict[str, Any], HydrologyPointSelection]],
        requested_return_periods: list[str],
        report_csv: Path,
        report_txt: Path,
        buffer_distance: float,
        channel_mannings_n: float,
        overbank_mannings_n: float,
        expansion_coeff: float,
        contraction_coeff: float,
        outflow_tolerance_m: float,
        prepare_geometry_hdf: bool = True,
    ) -> FlowScreeningResult:
        flow_profiles: list[tuple[str, dict[str, float]]] = []
        flow_lookup: dict[str, dict[str, Any]] = {}
        missing_results: dict[str, FlowRunResult] = {}

        for return_period in requested_return_periods:
            main_flow = main_selection.q_values.get(return_period)
            tributary_flow = tributary_selection.q_values.get(return_period)
            lower_point_flow = (
                lower_selection.q_values.get(return_period)
                if lower_selection is not None
                else None
            )
            if (
                main_flow is None
                or tributary_flow is None
                or not np.isfinite(main_flow)
                or not np.isfinite(tributary_flow)
            ):
                missing_results[return_period] = FlowRunResult(
                    return_period=return_period,
                    discharge_cms=np.nan,
                    run_folder="",
                    project_file="",
                    plan_hdf_file="",
                    compute_success=False,
                    solution="",
                    out_of_bank=None,
                    max_bank_excess_m=None,
                    note=(
                        "Missing discharge value for one or both upstream "
                        f"hydrology points at {return_period}."
                    ),
                )
                continue

            independent_flow_values: dict[str, float] = {}
            independent_flow_notes: list[dict[str, Any]] = []
            missing_independent_flows: list[str] = []
            for independent_reach, independent_selection in independent_selections:
                independent_flow = independent_selection.q_values.get(return_period)
                label = str(
                    independent_reach.get("selection_label")
                    or independent_selection.point_name
                )
                if independent_flow is None or not np.isfinite(independent_flow):
                    missing_independent_flows.append(label)
                    continue
                flow_key = str(independent_reach["flow_key"])
                independent_flow_values[flow_key] = float(independent_flow)
                independent_flow_notes.append(
                    {
                        "label": label,
                        "point": independent_selection.point_name,
                        "flow_key": flow_key,
                        "flow": float(independent_flow),
                    }
                )
            if missing_independent_flows:
                missing_results[return_period] = FlowRunResult(
                    return_period=return_period,
                    discharge_cms=np.nan,
                    run_folder="",
                    project_file="",
                    plan_hdf_file="",
                    compute_success=False,
                    solution="",
                    out_of_bank=None,
                    max_bank_excess_m=None,
                    note=(
                        "Missing discharge value for independent reach(es) "
                        f"{', '.join(missing_independent_flows)} at {return_period}."
                    ),
                )
                continue

            upstream_sum_flow = float(main_flow) + float(tributary_flow)
            lower_flow_source = "sum_of_upstream"
            lower_flow = upstream_sum_flow
            if lower_point_flow is not None and np.isfinite(lower_point_flow):
                lower_point_flow = float(lower_point_flow)
                if lower_point_flow > upstream_sum_flow:
                    lower_flow = lower_point_flow
                    lower_flow_source = "downstream_main_point"

            flow_profile = {
                "main": float(main_flow),
                "tributary": float(tributary_flow),
                "lower": float(lower_flow),
                **independent_flow_values,
            }
            flow_profiles.append((return_period, flow_profile))
            flow_lookup[return_period] = {
                **flow_profile,
                "upstream_sum": float(upstream_sum_flow),
                "lower_point": lower_point_flow,
                "lower_source": lower_flow_source,
                "independent": independent_flow_notes,
            }
            logger.info(
                "Junction flow profile %s | main=%s cms | tributary=%s cms | "
                "sum=%s cms | downstream_point=%s | chosen_lower=%s cms | "
                "source=%s",
                return_period,
                self._format_number(main_flow, 3),
                self._format_number(tributary_flow, 3),
                self._format_number(upstream_sum_flow, 3),
                (
                    f"{self._format_number(lower_point_flow, 3)} cms"
                    if lower_point_flow is not None and np.isfinite(lower_point_flow)
                    else "NA"
                ),
                self._format_number(lower_flow, 3),
                lower_flow_source,
            )

        run_results: list[FlowRunResult] = []
        if not flow_profiles:
            for return_period in requested_return_periods:
                run_results.append(missing_results[return_period])
            message = (
                f"Selected main hydrology point {main_selection.point_name} and "
                f"tributary point {tributary_selection.point_name}, but no usable "
                "coupled design discharge values were available."
            )
            self._write_flow_screening_csv(report_csv=report_csv, run_results=run_results)
            self._write_junction_flow_screening_text_report(
                report_txt=report_txt,
                message=message,
                buffer_distance=buffer_distance,
                main_selection=main_selection,
                tributary_selection=tributary_selection,
                lower_selection=lower_selection,
                run_results=run_results,
                max_safe_run=None,
            )
            return FlowScreeningResult(
                master_folder=str(project_folder),
                report_csv=str(report_csv),
                report_txt=str(report_txt),
                message=message,
                candidate_point_count=(
                    main_selection.candidate_count
                    + tributary_selection.candidate_count
                    + (lower_selection.candidate_count if lower_selection is not None else 0)
                    + sum(
                        selection.candidate_count
                        for _, selection in independent_selections
                    )
                ),
                selected_point={
                    "main": main_selection.to_dict(),
                    "tributary": tributary_selection.to_dict(),
                    "lower": (
                        lower_selection.to_dict() if lower_selection is not None else None
                    ),
                    "independent": [
                        {
                            "river": reach["river"],
                            "reach": reach["reach"],
                            "selection": selection.to_dict(),
                        }
                        for reach, selection in independent_selections
                    ],
                },
                tested_return_periods=requested_return_periods,
                run_results=[result.to_dict() for result in run_results],
            )

        first_profile, first_flow_profile = flow_profiles[0]
        build_result = self._write_model_files(
            project_folder=project_folder,
            project_stem=project_stem,
            project_title=project_title,
            projection_file=projection_file,
            profile_name=first_profile,
            flow_cms=first_flow_profile["lower"],
            channel_mannings_n=channel_mannings_n,
            overbank_mannings_n=overbank_mannings_n,
            expansion_coeff=expansion_coeff,
            contraction_coeff=contraction_coeff,
            geometry_context=geometry_context,
            flow_profile=first_flow_profile,
            return_period=first_profile,
            flow_profiles=flow_profiles,
        )
        compute_info = self.compute_project(
            project_folder=project_folder,
            project_stem=project_stem,
            plan_number="01",
            geometry_context=geometry_context,
            prepare_geometry_hdf=prepare_geometry_hdf,
        )

        for return_period in requested_return_periods:
            if return_period in missing_results:
                run_results.append(missing_results[return_period])
                continue

            flow_info = flow_lookup[return_period]
            lower_point_flow = flow_info["lower_point"]
            note = (
                "main="
                f"{self._format_number(flow_info['main'], 3)} cms | tributary="
                f"{self._format_number(flow_info['tributary'], 3)} cms | upstream_sum="
                f"{self._format_number(flow_info['upstream_sum'], 3)} cms | downstream_point="
                f"{self._format_number(lower_point_flow, 3) if lower_point_flow is not None and np.isfinite(lower_point_flow) else 'NA'}"
                " cms"
                " | lower_source="
                f"{flow_info['lower_source']} | lower="
                f"{self._format_number(flow_info['lower'], 3)} cms"
            )
            for independent_info in flow_info.get("independent", []):
                note = (
                    f"{note} | {independent_info['label']}="
                    f"{self._format_number(independent_info['flow'], 3)} cms"
                )
            out_of_bank = None
            max_bank_excess_m = None
            overflow_sections: list[str] = []
            if compute_info["compute_success"] and compute_info["plan_hdf_file"]:
                try:
                    overflow_info = self._evaluate_steady_outflow(
                        plan_hdf_path=Path(compute_info["plan_hdf_file"]),
                        sections=geometry_context["all_sections"],
                        profile_name=return_period,
                        tolerance_m=outflow_tolerance_m,
                    )
                    out_of_bank = overflow_info["out_of_bank"]
                    max_bank_excess_m = overflow_info["max_bank_excess_m"]
                    overflow_sections = overflow_info["overflow_sections"]
                    note = f"{note} | {overflow_info['note']}"
                except Exception as exc:
                    note = f"{note} | Could not evaluate profile {return_period}: {exc}"
            else:
                note = f"{note} | HEC-RAS compute did not produce steady results."

            run_results.append(
                FlowRunResult(
                    return_period=return_period,
                    discharge_cms=float(flow_info["lower"]),
                    run_folder=str(project_folder),
                    project_file=build_result.project_file,
                    plan_hdf_file=str(compute_info["plan_hdf_file"]),
                    compute_success=bool(compute_info["compute_success"]),
                    solution=str(compute_info["solution"]),
                    out_of_bank=out_of_bank,
                    max_bank_excess_m=max_bank_excess_m,
                    overflow_sections=overflow_sections,
                    note=note,
                )
            )

        max_safe_run = next(
            (
                result
                for result in run_results
                if result.compute_success and result.out_of_bank is False
            ),
            None,
        )
        final_model_run = max_safe_run
        final_model_reason = "max_safe_flow" if max_safe_run is not None else None
        if final_model_run is None:
            successful_runs = [
                result for result in run_results if result.compute_success
            ]
            if successful_runs:
                final_model_run = successful_runs[-1]
                final_model_reason = "lowest_successful_flow_for_review"

        overflow_return_periods = [
            result.return_period
            for result in run_results
            if result.out_of_bank is True
        ]
        message = self._build_junction_flow_screening_message(
            main_selection=main_selection,
            tributary_selection=tributary_selection,
            lower_selection=lower_selection,
            max_safe_run=max_safe_run,
            buffer_distance=buffer_distance,
            screening_stopped_early=False,
            all_flows_in_single_plan=True,
        )
        self._write_flow_screening_csv(report_csv=report_csv, run_results=run_results)
        self._write_junction_flow_screening_text_report(
            report_txt=report_txt,
            message=message,
            buffer_distance=buffer_distance,
            main_selection=main_selection,
            tributary_selection=tributary_selection,
            lower_selection=lower_selection,
            run_results=run_results,
            max_safe_run=max_safe_run,
        )
        return FlowScreeningResult(
            master_folder=str(project_folder),
            report_csv=str(report_csv),
            report_txt=str(report_txt),
            message=message,
            candidate_point_count=(
                main_selection.candidate_count
                + tributary_selection.candidate_count
                + (lower_selection.candidate_count if lower_selection is not None else 0)
                + sum(
                    selection.candidate_count
                    for _, selection in independent_selections
                )
            ),
            selected_point={
                "main": main_selection.to_dict(),
                "tributary": tributary_selection.to_dict(),
                "lower": (
                    lower_selection.to_dict() if lower_selection is not None else None
                ),
                "independent": [
                    {
                        "river": reach["river"],
                        "reach": reach["reach"],
                        "selection": selection.to_dict(),
                    }
                    for reach, selection in independent_selections
                ],
            },
            tested_return_periods=requested_return_periods,
            overflow_return_periods=overflow_return_periods,
            run_results=[result.to_dict() for result in run_results],
            max_safe_return_period=(
                max_safe_run.return_period if max_safe_run is not None else None
            ),
            max_safe_flow_cms=(
                max_safe_run.discharge_cms if max_safe_run is not None else None
            ),
            final_model_return_period=(
                final_model_run.return_period if final_model_run is not None else None
            ),
            final_model_flow_cms=(
                final_model_run.discharge_cms if final_model_run is not None else None
            ),
            final_model_reason=final_model_reason,
            final_build=build_result.to_dict(),
            final_compute_success=bool(compute_info["compute_success"]),
            final_solution=str(compute_info["solution"]),
        )

    def compute_project(
        self,
        project_folder: str | Path,
        project_stem: str,
        plan_number: str = "01",
        geometry_context: Optional[dict[str, Any]] = None,
        prepare_geometry_hdf: bool = True,
    ) -> dict[str, Any]:
        project_folder = Path(project_folder)
        normalized_plan_number = self._normalize_plan_number(plan_number)
        data_error_path = project_folder / f"{project_stem}.p{normalized_plan_number}.data_errors.txt"
        if data_error_path.exists():
            try:
                data_error_path.unlink()
            except OSError:
                logger.warning("Could not remove stale HEC-RAS data error file: %s", data_error_path)
        init_ras_project(
            project_folder,
            str(self.ras_exe_path),
            load_results_summary=False,
        )
        geometry_hdf_prepared = False
        if (
            prepare_geometry_hdf
            and normalized_plan_number == "01"
            and geometry_context is not None
        ):
            self._prepare_corrected_geometry_hdf(
                project_folder=project_folder,
                project_stem=project_stem,
                geometry_context=geometry_context,
            )
            init_ras_project(
                project_folder,
                str(self.ras_exe_path),
                load_results_summary=False,
            )
            geometry_hdf_prepared = True

        compute_result = RasCmdr.compute_plan(
            plan_number,
            clear_geompre=not geometry_hdf_prepared,
            force_rerun=True,
            verify=True,
        )
        plan_hdf_path = project_folder / f"{project_stem}.p{normalized_plan_number}.hdf"
        if geometry_hdf_prepared and geometry_context is not None:
            geometry_hdf_path = project_folder / f"{project_stem}.g01.hdf"
            self._patch_project_hdf_geometry(
                hdf_path=geometry_hdf_path,
                geometry_context=geometry_context,
            )
            if plan_hdf_path.exists():
                self._patch_project_hdf_geometry(
                    hdf_path=plan_hdf_path,
                    geometry_context=geometry_context,
                )

        solution = ""
        if plan_hdf_path.exists():
            try:
                solution_df = HdfResultsPlan.get_steady_info(plan_hdf_path)
                solution = str(solution_df.iloc[0].get("Solution", ""))
            except Exception as exc:
                solution = f"Could not read steady info: {exc}"

        # RasCmdr can return False even when HEC-RAS wrote a valid steady HDF
        # and the HDF itself reports a successful steady solve. Use that as the
        # screening signal so the loop can stop on the first in-bank result.
        steady_solution_success = (
            plan_hdf_path.exists()
            and "finished successfully" in solution.casefold()
        )
        compute_success = bool(compute_result) or steady_solution_success

        return {
            "compute_success": compute_success,
            "compute_result": repr(compute_result),
            "plan_hdf_file": str(plan_hdf_path) if plan_hdf_path.exists() else "",
            "solution": solution,
        }

    def _prepare_corrected_geometry_hdf(
        self,
        project_folder: Path,
        project_stem: str,
        geometry_context: dict[str, Any],
    ) -> None:
        geometry_hdf_path = project_folder / f"{project_stem}.g01.hdf"
        if not self._geometry_context_sections(geometry_context):
            logger.info("Skipping geometry-HDF correction; no sections are available.")
            return

        init_ras_project(
            project_folder,
            str(self.ras_exe_path),
            load_results_summary=False,
        )
        RasCmdr.compute_plan(
            "01",
            clear_geompre=True,
            force_geompre=True,
            force_rerun=True,
            verify=False,
        )
        self._delete_plan_compute_outputs(
            project_folder=project_folder,
            project_stem=project_stem,
            plan_number="01",
            keep_geometry_hdf=True,
        )
        if geometry_hdf_path.exists():
            self._patch_project_hdf_geometry(
                hdf_path=geometry_hdf_path,
                geometry_context=geometry_context,
            )
            logger.info(
                "Created and patched geometry HDF using p01 pre-run: %s",
                geometry_hdf_path,
            )
        else:
            logger.warning(
                "Plan p01 pre-run did not create geometry HDF: %s",
                geometry_hdf_path,
            )

    @staticmethod
    def _delete_plan_compute_outputs(
        project_folder: Path,
        project_stem: str,
        plan_number: str,
        keep_geometry_hdf: bool = True,
    ) -> None:
        plan_number = HECRAS._normalize_plan_number(plan_number)
        suffixes = [
            f".p{plan_number}.hdf",
            f".p{plan_number}.tmp.hdf",
            f".b{plan_number}",
            f".c{plan_number}",
            f".O{plan_number}",
            f".o{plan_number}",
            f".r{plan_number}",
            f".comp_msgs{plan_number}.txt",
            f".p{plan_number}.data_errors.txt",
        ]
        if not keep_geometry_hdf:
            suffixes.append(f".g{plan_number}.hdf")
        for suffix in suffixes:
            path = project_folder / f"{project_stem}{suffix}"
            if path.exists():
                try:
                    path.unlink()
                except OSError as exc:
                    logger.warning("Could not delete pre-run output %s: %s", path, exc)

    def _patch_project_hdf_geometry(
        self,
        hdf_path: Path,
        geometry_context: dict[str, Any],
    ) -> None:
        if not hdf_path.exists():
            return

        raw_bank_lines, projected_bank_lines = self._geometry_context_hdf_bank_lines(
            geometry_context
        )
        left_bank_line, right_bank_line = self._geometry_context_bank_pair(
            geometry_context
        )
        if raw_bank_lines or projected_bank_lines or (
            left_bank_line is not None and right_bank_line is not None
        ):
            if left_bank_line is None or right_bank_line is None:
                line_pair = list(projected_bank_lines or raw_bank_lines)[:2]
                if len(line_pair) < 2:
                    logger.info(
                        "Skipping HDF bank-line patch for %s; bank pair missing.",
                        hdf_path,
                    )
                else:
                    left_bank_line, right_bank_line = line_pair
            if left_bank_line is not None and right_bank_line is not None:
                self._patch_geometry_hdf_bank_lines(
                    hdf_path,
                    raw_bank_lines=raw_bank_lines,
                    projected_bank_lines=projected_bank_lines,
                    left_bank_line=left_bank_line,
                    right_bank_line=right_bank_line,
                    centerline_line=(
                        None
                        if projected_bank_lines
                        else self._geometry_context_centerline(geometry_context)
                    ),
                )

        self._patch_geometry_hdf_cross_sections(
            hdf_path,
            self._geometry_context_sections(geometry_context),
        )

    @staticmethod
    def _geometry_context_sections(
        geometry_context: dict[str, Any],
    ) -> list[SectionData]:
        if geometry_context.get("junction"):
            return list(geometry_context.get("all_sections") or [])
        return list(geometry_context.get("sections") or [])

    @staticmethod
    def _geometry_context_bank_pair(
        geometry_context: dict[str, Any],
    ) -> tuple[Optional[LineString], Optional[LineString]]:
        left = geometry_context.get("left_bank_line")
        right = geometry_context.get("right_bank_line")
        if left is not None and right is not None:
            return left, right
        for reach in geometry_context.get("reaches") or []:
            left = reach.get("left_bank_line")
            right = reach.get("right_bank_line")
            if left is not None and right is not None:
                return left, right
        return None, None

    @staticmethod
    def _geometry_context_centerline(
        geometry_context: dict[str, Any],
    ) -> Optional[LineString]:
        centerline = geometry_context.get("centerline_line")
        if centerline is not None:
            return centerline
        for reach in geometry_context.get("reaches") or []:
            centerline = reach.get("centerline_line")
            if centerline is not None:
                return centerline
        return None

    @staticmethod
    def _geometry_context_hdf_bank_lines(
        geometry_context: dict[str, Any],
    ) -> tuple[list[LineString], list[LineString]]:
        raw_lines = list(geometry_context.get("hdf_bank_lines_raw") or [])
        projected_lines = list(geometry_context.get("hdf_bank_lines_projected") or [])
        if raw_lines or projected_lines:
            return raw_lines, projected_lines

        if not geometry_context.get("junction"):
            return raw_lines, projected_lines

        for reach in geometry_context.get("reaches") or []:
            raw_lines.extend(reach.get("hdf_bank_lines_raw") or [])
            projected_lines.extend(reach.get("hdf_bank_lines_projected") or [])
        return raw_lines, projected_lines

    @staticmethod
    def _patch_geometry_hdf_bank_lines(
        geometry_hdf_path: Path,
        left_bank_line: LineString,
        right_bank_line: LineString,
        raw_bank_lines: Optional[list[LineString]] = None,
        projected_bank_lines: Optional[list[LineString]] = None,
        centerline_line: Optional[LineString] = None,
        outward_offset_m: float = 0.2,
    ) -> None:
        projected_lines = list(projected_bank_lines or [left_bank_line, right_bank_line])
        raw_lines = list(raw_bank_lines or [])
        if not projected_lines:
            return
        if centerline_line is not None and outward_offset_m:
            projected_lines = [
                HECRAS._offset_line_away_from_centerline(
                    line=line,
                    centerline_line=centerline_line,
                    offset_distance=float(outward_offset_m),
                )
                for line in projected_lines
            ]
            raw_lines = []

        def polyline_arrays(
            lines: list[LineString],
        ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
            coords_by_line = [
                np.asarray(
                    [(float(x), float(y)) for x, y in line.coords],
                    dtype=np.float64,
                )
                for line in lines
                if line is not None and not line.is_empty and len(line.coords) >= 2
            ]
            if not coords_by_line:
                return (
                    np.empty((0, 2), dtype=np.float64),
                    np.empty((0, 4), dtype=np.int32),
                    np.empty((0, 2), dtype=np.int32),
                )
            all_points = np.vstack(coords_by_line)
            polyline_info = np.zeros((len(coords_by_line), 4), dtype=np.int32)
            polyline_parts = np.zeros((len(coords_by_line), 2), dtype=np.int32)
            start = 0
            for idx, coords in enumerate(coords_by_line):
                count = int(len(coords))
                polyline_info[idx] = [start, count, idx, 1]
                polyline_parts[idx] = [0, count]
                start += count
            return all_points, polyline_info, polyline_parts

        if raw_lines:
            bank_line_groups = raw_lines + projected_lines
        else:
            bank_line_groups = projected_lines
        bank_points, bank_info, bank_parts = polyline_arrays(bank_line_groups)
        flow_points, flow_info, flow_parts = polyline_arrays(projected_lines)

        dataset_attrs = {
            "info": {
                "Column": np.asarray(
                    [
                        b"Point Starting Index",
                        b"Point Count",
                        b"Part Starting Index",
                        b"Part Count",
                    ],
                    dtype="S20",
                ),
                "Feature Type": np.bytes_("Polyline"),
                "Row": np.bytes_("Feature"),
            },
            "parts": {
                "Column": np.asarray(
                    [b"Point Starting Index", b"Point Count"],
                    dtype="S20",
                ),
                "Row": np.bytes_("Part"),
            },
            "points": {
                "Column": np.asarray([b"X", b"Y"], dtype="S1"),
                "Row": np.bytes_("Points"),
            },
        }

        def replace_dataset(group, dataset_name: str, values: np.ndarray, attrs: dict):
            if dataset_name in group:
                del group[dataset_name]
            dataset = HECRAS._create_hecras_hdf_dataset(
                group,
                dataset_name,
                values,
            )
            for key, value in attrs.items():
                dataset.attrs[key] = value

        with h5py.File(geometry_hdf_path, "r+") as hdf:
            bank_group = hdf.require_group("Geometry/River Bank Lines")
            replace_dataset(
                bank_group,
                "Polyline Points",
                bank_points,
                dataset_attrs["points"],
            )
            replace_dataset(
                bank_group,
                "Polyline Info",
                bank_info,
                dataset_attrs["info"],
            )
            replace_dataset(
                bank_group,
                "Polyline Parts",
                bank_parts,
                dataset_attrs["parts"],
            )

            flow_group = hdf.require_group("Geometry/River Flow Paths")
            replace_dataset(
                flow_group,
                "Flow Path Lines Points",
                flow_points,
                dataset_attrs["points"],
            )
            replace_dataset(
                flow_group,
                "Flow Path Lines Info",
                flow_info,
                dataset_attrs["info"],
            )
            replace_dataset(
                flow_group,
                "Flow Path Lines Parts",
                flow_parts,
                dataset_attrs["parts"],
            )

    @staticmethod
    def _offset_line_away_from_centerline(
        line: LineString,
        centerline_line: LineString,
        offset_distance: float,
    ) -> LineString:
        if (
            line is None
            or line.is_empty
            or centerline_line is None
            or centerline_line.is_empty
            or not offset_distance
        ):
            return line

        shifted_coords: list[tuple[float, float]] = []
        for x_value, y_value in line.coords:
            point = Point(float(x_value), float(y_value))
            center_measure = float(centerline_line.project(point))
            center_point = centerline_line.interpolate(center_measure)
            dx = float(point.x - center_point.x)
            dy = float(point.y - center_point.y)
            length = math.hypot(dx, dy)
            if length <= 1e-9:
                shifted_coords.append((float(point.x), float(point.y)))
                continue
            scale = float(offset_distance) / length
            shifted_coords.append(
                (
                    float(point.x + dx * scale),
                    float(point.y + dy * scale),
                )
            )

        if len(shifted_coords) < 2:
            return line
        return LineString(shifted_coords)

    @staticmethod
    def _hecras_hdf_bank_station(value: float) -> float:
        return round(float(value), 1)

    @staticmethod
    def _hecras_hdf_chunks(values: np.ndarray) -> Optional[tuple[int, ...]]:
        if values.shape == ():
            return None
        if values.ndim == 1:
            return (max(int(values.shape[0]), 1),)
        if values.ndim == 2:
            columns = max(int(values.shape[1]), 1)
            if columns == 2:
                return (min(max(int(values.shape[0]), 1), 8192), columns)
            if columns == 4:
                return (max(int(values.shape[0]), 1), columns)
            return (max(int(values.shape[0]), 1), columns)
        return tuple(max(int(size), 1) for size in values.shape)

    @staticmethod
    def _create_hecras_hdf_dataset(group, dataset_name: str, values: np.ndarray):
        chunks = HECRAS._hecras_hdf_chunks(values)
        if chunks is None:
            return group.create_dataset(dataset_name, data=values)
        maxshape = tuple(None for _ in values.shape)
        return group.create_dataset(
            dataset_name,
            data=values,
            chunks=chunks,
            maxshape=maxshape,
            compression="gzip",
        )

    @staticmethod
    def _insert_hdf_station_elevation_points(
        station_elevation: np.ndarray,
        required_stations: list[float],
    ) -> np.ndarray:
        if station_elevation.size == 0:
            return station_elevation.astype(np.float32)

        rows = [
            (float(station), float(elevation))
            for station, elevation in station_elevation
        ]
        existing_stations = [station for station, _ in rows]
        source_stations = list(existing_stations)
        source_elevations = [elevation for _, elevation in rows]

        for station in required_stations:
            station = float(station)
            if any(
                math.isclose(station, existing, abs_tol=0.0015)
                or (
                    station >= existing
                    and math.isclose(station, existing, abs_tol=0.005)
                )
                for existing in existing_stations
            ):
                continue
            elevation = HECRAS._interpolate_profile_elevation(
                source_stations,
                source_elevations,
                station,
            )
            rows.append((station, elevation))
            existing_stations.append(station)

        rows.sort(key=lambda item: item[0])
        return np.asarray(rows, dtype=np.float32)

    @staticmethod
    def _patch_geometry_hdf_cross_sections(
        geometry_hdf_path: Path,
        sections: list[SectionData],
    ) -> None:
        if not sections:
            return

        with h5py.File(geometry_hdf_path, "r+") as hdf:
            cross_sections_path = "Geometry/Cross Sections"
            if cross_sections_path not in hdf:
                return
            group = hdf[cross_sections_path]
            required_datasets = {
                "Attributes",
                "Station Elevation Info",
                "Station Elevation Values",
                "Manning's n Values",
            }
            if not required_datasets.issubset(group.keys()):
                return

            attrs = group["Attributes"][()]
            station_info = group["Station Elevation Info"][()]
            station_values = group["Station Elevation Values"][()]
            mann_values = group["Manning's n Values"][()]

            updated_station_values: list[np.ndarray] = []
            updated_station_info = np.zeros_like(station_info)
            station_start = 0
            for idx, section in enumerate(sections[: len(station_info)]):
                rounded_left = HECRAS._hecras_hdf_bank_station(
                    section.hdf_left_bank_station
                    if section.hdf_left_bank_station is not None
                    else section.left_bank_station
                )
                rounded_right = HECRAS._hecras_hdf_bank_station(
                    section.hdf_right_bank_station
                    if section.hdf_right_bank_station is not None
                    else section.right_bank_station
                )
                source_start = int(station_info[idx][0])
                source_count = int(station_info[idx][1])
                section_values = station_values[
                    source_start : source_start + source_count
                ]
                patched_values = HECRAS._insert_hdf_station_elevation_points(
                    section_values,
                    [rounded_left, rounded_right],
                )
                updated_station_values.append(patched_values)
                updated_station_info[idx] = [station_start, len(patched_values)]
                station_start += len(patched_values)

                for field_name, field_value in (
                    ("Left Bank", rounded_left),
                    ("Right Bank", rounded_right),
                    ("Left Levee Sta", rounded_left),
                    ("Right Levee Sta", rounded_right),
                    (
                        "Left Levee Elev",
                        HECRAS._interpolate_profile_elevation(
                            patched_values[:, 0].astype(float).tolist(),
                            patched_values[:, 1].astype(float).tolist(),
                            rounded_left,
                        ),
                    ),
                    (
                        "Right Levee Elev",
                        HECRAS._interpolate_profile_elevation(
                            patched_values[:, 0].astype(float).tolist(),
                            patched_values[:, 1].astype(float).tolist(),
                            rounded_right,
                        ),
                    ),
                ):
                    if field_name in attrs.dtype.names:
                        attrs[idx][field_name] = field_value

                mann_idx = idx * 3
                if mann_idx + 2 < len(mann_values):
                    mann_values[mann_idx + 1][0] = rounded_left
                    mann_values[mann_idx + 2][0] = rounded_right

            if len(sections) < len(station_info):
                for idx in range(len(sections), len(station_info)):
                    source_start = int(station_info[idx][0])
                    source_count = int(station_info[idx][1])
                    section_values = station_values[
                        source_start : source_start + source_count
                    ].astype(np.float32)
                    updated_station_values.append(section_values)
                    updated_station_info[idx] = [station_start, len(section_values)]
                    station_start += len(section_values)

            concatenated_station_values = (
                np.vstack(updated_station_values).astype(np.float32)
                if updated_station_values
                else np.empty((0, 2), dtype=np.float32)
            )

            for dataset_name, values in (
                ("Attributes", attrs),
                ("Station Elevation Info", updated_station_info),
                ("Station Elevation Values", concatenated_station_values),
                ("Manning's n Values", mann_values),
            ):
                existing_attrs = dict(group[dataset_name].attrs)
                del group[dataset_name]
                dataset = HECRAS._create_hecras_hdf_dataset(
                    group,
                    dataset_name,
                    values,
                )
                for key, value in existing_attrs.items():
                    dataset.attrs[key] = value

    def smoke_test(
        self,
        project_folder: str | Path,
        project_stem: str,
        run_compute: bool = False,
        plan_number: str = "01",
    ) -> dict[str, Any]:
        """
        Verify that ras-commander can parse the generated project.

        When `run_compute=True`, this also attempts a real HEC-RAS plan run.
        """
        project_folder = Path(project_folder)
        geom_path = project_folder / f"{project_stem}.g01"

        ras_obj = init_ras_project(
            project_folder,
            str(self.ras_exe_path),
            load_results_summary=False,
        )

        xs_df = GeomCrossSection.get_cross_sections(geom_path)
        bridge_df = GeomBridge.get_bridges(geom_path)
        culvert_df = GeomCulvert.get_all(geom_path)

        result: dict[str, Any] = {
            "ras_exe_exists": self.ras_exe_path.exists(),
            "project_initialized": ras_obj.is_initialized,
            "sdf_exists": (project_folder / "RASImport.sdf").exists(),
            "plan_count": int(len(ras_obj.plan_df)),
            "geom_count": int(len(ras_obj.geom_df)),
            "flow_count": int(len(ras_obj.flow_df)),
            "cross_section_count": int(len(xs_df)),
            "bridge_count": int(len(bridge_df)),
            "culvert_count": int(len(culvert_df)),
            "plan_numbers": list(ras_obj.plan_df["plan_number"].astype(str)),
            "geom_numbers": list(ras_obj.geom_df["geom_number"].astype(str)),
            "flow_numbers": list(ras_obj.flow_df["flow_number"].astype(str)),
        }

        if run_compute:
            compute_info = self.compute_project(
                project_folder=project_folder,
                project_stem=project_stem,
                plan_number=plan_number,
            )
            result["compute_success"] = compute_info["compute_success"]
            result["compute_result"] = compute_info["compute_result"]
            result["plan_hdf_file"] = compute_info["plan_hdf_file"]
            result["solution"] = compute_info["solution"]

        return result

    def _prepare_geometry_context(
        self,
        cross_section_csv: str | Path,
        bank_lines_shp: str | Path,
        structure_csv: Optional[str | Path],
        min_section_spacing: float,
        river_station_step: float,
        centerline_samples_per_segment: int,
        bank_station_mode: str,
        river_line_method: str,
    ) -> dict[str, Any]:
        bank_station_mode = self._normalize_bank_station_mode(bank_station_mode)
        river_line_method = self._normalize_river_line_method(river_line_method)
        cross_section_df = self._read_cross_sections(cross_section_csv)
        raw_river_name = str(cross_section_df["River"].iloc[0]).strip()
        raw_reach_name = str(cross_section_df["Reach"].iloc[0]).strip()
        river = self._normalize_river_name(raw_river_name)
        reach = self._normalize_reach_name(
            raw_reach_name,
            river_name=river,
            raw_river_name=raw_river_name,
        )

        section_inputs, left_bank_line, right_bank_line = self._build_section_inputs(
            cross_section_df,
            bank_lines_shp,
        )
        filtered_inputs, skipped_stations = self._filter_near_duplicate_sections(
            section_inputs,
            min_section_spacing=min_section_spacing,
        )
        sections = self._finalize_sections(
            filtered_inputs=filtered_inputs,
            river_station_step=river_station_step,
            bank_station_mode=bank_station_mode,
        )
        centerline_points, centerline_measures, section_centerline_measures = (
            self._build_centerline_geometry(
                filtered_inputs,
                left_bank_line=left_bank_line,
                right_bank_line=right_bank_line,
                samples_per_segment=centerline_samples_per_segment,
                river_line_method=river_line_method,
            )
        )
        self._populate_reach_lengths(
            sections,
            section_centerline_measures=section_centerline_measures,
        )
        self._assign_river_stations(sections)
        centerline_line = LineString(centerline_points)
        hdf_bank_lines_raw, hdf_bank_lines_projected = (
            self._load_bank_line_features_for_hdf(bank_lines_shp)
        )
        if hdf_bank_lines_projected:
            hdf_bank_lines_projected = self._offset_hdf_bank_lines(
                hdf_bank_lines_projected,
                centerlines=[centerline_line],
            )
            hdf_bank_lines_raw = []
        structures = self._build_structures(
            structure_csv=structure_csv,
            sections=sections,
            centerline_line=centerline_line,
        )
        friction_slope = self._estimate_downstream_friction_slope(sections)
        return {
            "river": river,
            "reach": reach,
            "sections": sections,
            "structures": structures,
            "filtered_inputs": filtered_inputs,
            "left_bank_line": left_bank_line,
            "right_bank_line": right_bank_line,
            "hdf_bank_lines_raw": hdf_bank_lines_raw,
            "hdf_bank_lines_projected": hdf_bank_lines_projected,
            "centerline_points": centerline_points,
            "centerline_measures": centerline_measures,
            "centerline_line": centerline_line,
            "friction_slope": friction_slope,
            "skipped_source_stations": skipped_stations,
            "bank_station_mode": bank_station_mode,
            "river_line_method": river_line_method,
        }

    def _prepare_junction_geometry_context(
        self,
        main_cross_section_csv: str | Path,
        main_bank_lines_shp: str | Path,
        tributary_cross_section_csv: str | Path,
        tributary_bank_lines_shp: str | Path,
        main_structure_csv: Optional[str | Path],
        tributary_structure_csv: Optional[str | Path],
        combined_bank_lines_shp: Optional[str | Path],
        additional_reaches: Optional[list[dict[str, Any]]],
        reference_geometry_file: Optional[str | Path],
        min_section_spacing: float,
        river_station_step: float,
        centerline_samples_per_segment: int,
        bank_station_mode: str,
        river_line_method: str,
    ) -> dict[str, Any]:
        reference_template = self._load_junction_reference_template(
            reference_geometry_file
        )
        main_context = self._prepare_geometry_context(
            cross_section_csv=main_cross_section_csv,
            bank_lines_shp=main_bank_lines_shp,
            structure_csv=main_structure_csv,
            min_section_spacing=min_section_spacing,
            river_station_step=river_station_step,
            centerline_samples_per_segment=centerline_samples_per_segment,
            bank_station_mode=bank_station_mode,
            river_line_method=river_line_method,
        )
        tributary_context = self._prepare_geometry_context(
            cross_section_csv=tributary_cross_section_csv,
            bank_lines_shp=tributary_bank_lines_shp,
            structure_csv=tributary_structure_csv,
            min_section_spacing=min_section_spacing,
            river_station_step=river_station_step,
            centerline_samples_per_segment=centerline_samples_per_segment,
            bank_station_mode=bank_station_mode,
            river_line_method=river_line_method,
        )

        main_df = self._read_cross_sections(main_cross_section_csv)
        tributary_df = self._read_cross_sections(tributary_cross_section_csv)
        local_box = self._junction_box_from_downstream_sections(tributary_df)
        main_original_lines = self._load_bank_lines(main_bank_lines_shp)
        tributary_original_lines = self._load_bank_lines(tributary_bank_lines_shp)
        combined_bank_lines: list[LineString] = []
        if combined_bank_lines_shp is not None and Path(combined_bank_lines_shp).exists():
            combined_bank_lines = self._load_bank_lines(combined_bank_lines_shp)
        main_opening = self._identify_local_opening_pair(
            cross_section_df=main_df,
            bank_lines=main_original_lines,
            local_box=local_box,
        )
        tributary_opening = self._identify_local_opening_pair(
            cross_section_df=tributary_df,
            bank_lines=tributary_original_lines,
            local_box=local_box,
            use_downstream_sections=True,
        )
        if main_opening is None or tributary_opening is None:
            raise ValueError("Could not identify local confluence openings.")

        if combined_bank_lines:
            grouped_tributary_lines = self._select_combined_reach_bank_lines(
                cross_section_df=tributary_df,
                bank_lines=combined_bank_lines,
                local_box=local_box,
            )
            grouped_tributary_lines = self._offset_bank_lines_with_dtm(
                bank_lines=grouped_tributary_lines,
                fallback_centerline=tributary_context["centerline_line"],
            )
        else:
            snapped_tributary_lines = self._remap_opening_pair(
                bank_lines=tributary_original_lines,
                source_pair=tributary_opening,
                target_pair=main_opening,
            )
            candidate_tributary_lines = snapped_tributary_lines
            if DTMChannelModifier is not None:
                try:
                    snapped_gdf = gpd.GeoDataFrame(
                        geometry=snapped_tributary_lines,
                        crs=self.source_crs if self.source_crs != "UNKNOWN" else None,
                    )
                    candidate_tributary_lines = self._line_strings_from_geometry(
                        DTMChannelModifier.clean_and_merge_banklines(
                            snapped_gdf,
                            bridge_junctions=True,
                        )
                    )
                except Exception as exc:
                    logger.warning(
                        "DTM cleanup of remapped junction bank lines failed; "
                        "using selected raw remapped lines: %s",
                        exc,
                    )
            grouped_tributary_lines = self._select_reach_bank_line_pair(
                cross_section_df=tributary_df,
                bank_lines=candidate_tributary_lines,
            )
            grouped_tributary_lines = self._offset_bank_lines_with_dtm(
                bank_lines=grouped_tributary_lines,
                fallback_centerline=tributary_context["centerline_line"],
            )
        tributary_inputs, tributary_left_bank, tributary_right_bank = (
            self._build_section_inputs_from_selected_bank_lines(
                tributary_df,
                grouped_tributary_lines,
            )
        )
        tributary_inputs, tributary_skipped = self._filter_near_duplicate_sections(
            tributary_inputs,
            min_section_spacing=min_section_spacing,
        )
        tributary_sections = self._finalize_sections(
            filtered_inputs=tributary_inputs,
            river_station_step=river_station_step,
            bank_station_mode=bank_station_mode,
        )
        main_split = self._split_main_sections_for_junction(
            main_context["sections"],
            local_box=local_box,
        )
        junction_point = self._resolve_junction_point(
            tributary_sections=tributary_sections,
            main_sections=main_context["sections"][: main_split["upstream_end_index"] + 1],
            fallback_point=Point(
                (main_opening["left_point"].x + main_opening["right_point"].x) / 2.0,
                (main_opening["left_point"].y + main_opening["right_point"].y) / 2.0,
            ),
            reference_geometry_file=reference_geometry_file,
            local_box=local_box,
        )
        tributary_sections = self._extend_tributary_to_junction(
            tributary_sections=tributary_sections,
            junction_point=junction_point,
        )
        (
            tributary_centerline_points,
            tributary_centerline_measures,
            tributary_section_measures,
        ) = self._build_tributary_centerline_from_reference_context(
            reference_context=tributary_context,
            junction_sections=tributary_sections,
            junction_point=junction_point,
        )
        self._populate_reach_lengths(
            tributary_sections,
            section_centerline_measures=tributary_section_measures,
        )
        tributary_sections = self._drop_invalid_nonterminal_sections(
            tributary_sections,
            min_positive_length=0.01,
        )
        tributary_section_measures = [
            float(section.centerline_measure) for section in tributary_sections
        ]
        self._populate_reach_lengths(
            tributary_sections,
            section_centerline_measures=tributary_section_measures,
        )
        self._assign_river_stations(tributary_sections)
        self._validate_reach_lengths(
            tributary_sections,
            river=tributary_context["river"],
            reach=tributary_context["reach"],
        )
        tributary_context = {
            **tributary_context,
            "sections": tributary_sections,
            "structures": self._build_structures(
                structure_csv=tributary_structure_csv,
                sections=tributary_sections,
                centerline_line=LineString(tributary_centerline_points),
            ),
            "centerline_points": tributary_centerline_points,
            "centerline_measures": tributary_centerline_measures,
            "centerline_line": LineString(tributary_centerline_points),
            "friction_slope": self._estimate_downstream_friction_slope(
                tributary_sections
            ),
            "skipped_source_stations": tributary_skipped,
            "left_bank_line": tributary_left_bank,
            "right_bank_line": tributary_right_bank,
            "hdf_bank_lines_raw": [],
            "hdf_bank_lines_projected": list(grouped_tributary_lines),
            "flow_key": "tributary",
            "boundary_role": "junction_upstream",
        }

        main_upper = self._build_reach_context_from_existing_context(
            geometry_context=main_context,
            start_index=0,
            end_index=main_split["upstream_end_index"],
            reach_name=main_context["reach"],
            river_station_offset=0.0,
            preserve_river_stations=True,
        )
        lower_reach_name = self._lower_reach_name(main_context["reach"])
        lower_context = self._build_reach_context_from_existing_context(
            geometry_context=main_context,
            start_index=main_split["downstream_start_index"],
            end_index=len(main_context["sections"]) - 1,
            reach_name=lower_reach_name,
            river_station_offset=0.0,
            preserve_river_stations=True,
        )
        main_upper, lower_context = self._split_main_centerline_at_junction(
            main_context=main_context,
            main_upper=main_upper,
            lower_context=lower_context,
            main_split=main_split,
            junction_point=junction_point,
        )

        naming_template = reference_template
        if naming_template is None:
            naming_template = self._infer_junction_naming_template(
                main_river=main_context["river"],
                main_reach=main_context["reach"],
                tributary_river=tributary_context["river"],
                tributary_reach=tributary_context["reach"],
            )

        if reference_template is not None:
            (
                tributary_context,
                main_upper,
                lower_context,
            ) = self._apply_reference_names_to_junction_context(
                reference_template=reference_template,
                tributary_context=tributary_context,
                main_upper=main_upper,
                lower_context=lower_context,
            )

        tributary_context["centerline_points"] = self._replace_endpoint(
            tributary_context["centerline_points"],
            index=-1,
            point=(float(junction_point.x), float(junction_point.y)),
        )
        tributary_context["centerline_line"] = LineString(
            tributary_context["centerline_points"]
        )
        main_upper["flow_key"] = "main"
        main_upper["boundary_role"] = "junction_upstream"
        lower_context["flow_key"] = "lower"
        lower_context["boundary_role"] = "junction_downstream"

        independent_reaches = self._prepare_independent_reach_contexts(
            additional_reaches=additional_reaches or [],
            min_section_spacing=min_section_spacing,
            river_station_step=river_station_step,
            centerline_samples_per_segment=centerline_samples_per_segment,
            bank_station_mode=bank_station_mode,
            river_line_method=river_line_method,
            existing_reaches=[tributary_context, main_upper, lower_context],
        )

        reaches = [
            tributary_context,
            main_upper,
            lower_context,
        ] + independent_reaches
        all_sections = (
            tributary_context["sections"]
            + main_upper["sections"]
            + lower_context["sections"]
            + [
                section
                for independent_reach in independent_reaches
                for section in independent_reach["sections"]
            ]
        )
        hdf_bank_lines_raw, hdf_bank_lines_projected = (
            self._prepare_junction_hdf_bank_lines(
                combined_bank_lines_shp=combined_bank_lines_shp,
                reaches=reaches,
            )
        )
        return {
            "junction": {
                "name": (
                    naming_template.junction_name
                    if naming_template is not None
                    and naming_template.junction_name
                    else "Junction 1"
                ),
                "x": float(junction_point.x),
                "y": float(junction_point.y),
                "upstream_reaches": [
                    (tributary_context["river"], tributary_context["reach"]),
                    (main_upper["river"], main_upper["reach"]),
                ],
                "downstream_river": lower_context["river"],
                "downstream_reach": lower_context["reach"],
                "junction_lengths_angles": (
                    list(naming_template.junction_lengths_angles)
                    if naming_template is not None
                    else []
                ),
            },
            "river": main_context["river"],
            "reaches": reaches,
            "main_reach": main_upper,
            "tributary_reach": tributary_context,
            "lower_reach": lower_context,
            "independent_reaches": independent_reaches,
            "all_sections": all_sections,
            "hdf_bank_lines_raw": hdf_bank_lines_raw,
            "hdf_bank_lines_projected": hdf_bank_lines_projected,
            "local_box": local_box,
            "combined_bank_lines": combined_bank_lines,
            "main_opening": main_opening,
            "tributary_opening": tributary_opening,
            "reference_template": naming_template,
            "skipped_source_stations": sorted(
                set(main_context.get("skipped_source_stations", []))
                | set(tributary_context.get("skipped_source_stations", []))
                | {
                    station
                    for independent_reach in independent_reaches
                    for station in independent_reach.get("skipped_source_stations", [])
                }
            ),
        }

    def _prepare_junction_hdf_bank_lines(
        self,
        combined_bank_lines_shp: Optional[str | Path],
        reaches: list[dict[str, Any]],
    ) -> tuple[list[LineString], list[LineString]]:
        centerlines = [
            reach["centerline_line"]
            for reach in reaches
            if reach.get("centerline_line") is not None
        ]
        if combined_bank_lines_shp is not None and Path(combined_bank_lines_shp).exists():
            raw_lines, projected_lines = self._load_bank_line_features_for_hdf(
                combined_bank_lines_shp
            )
            if projected_lines:
                projected_lines = self._offset_hdf_bank_lines(
                    projected_lines,
                    centerlines=centerlines,
                )
                raw_lines = []
            return raw_lines, projected_lines

        raw_lines: list[LineString] = []
        projected_lines: list[LineString] = []
        for reach in reaches:
            raw_lines.extend(reach.get("hdf_bank_lines_raw") or [])
            projected_lines.extend(reach.get("hdf_bank_lines_projected") or [])

        if projected_lines or raw_lines:
            return raw_lines, projected_lines

        for reach in reaches:
            left_bank_line = reach.get("left_bank_line")
            right_bank_line = reach.get("right_bank_line")
            if left_bank_line is not None and right_bank_line is not None:
                projected_lines.extend([left_bank_line, right_bank_line])
        return [], projected_lines

    def _prepare_independent_reach_contexts(
        self,
        additional_reaches: list[dict[str, Any]],
        min_section_spacing: float,
        river_station_step: float,
        centerline_samples_per_segment: int,
        bank_station_mode: str,
        river_line_method: str,
        existing_reaches: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        prepared_reaches: list[dict[str, Any]] = []
        used_names = {
            (str(reach["river"]), str(reach["reach"]))
            for reach in existing_reaches
        }

        for index, reach_input in enumerate(additional_reaches, start=1):
            cross_section_csv = reach_input.get("cross_section_csv")
            bank_lines_shp = reach_input.get("bank_lines_shp")
            if cross_section_csv is None or bank_lines_shp is None:
                continue

            reach_context = self._prepare_geometry_context(
                cross_section_csv=cross_section_csv,
                bank_lines_shp=bank_lines_shp,
                structure_csv=reach_input.get("structure_csv"),
                min_section_spacing=min_section_spacing,
                river_station_step=river_station_step,
                centerline_samples_per_segment=centerline_samples_per_segment,
                bank_station_mode=bank_station_mode,
                river_line_method=river_line_method,
            )
            river, reach = self._unique_reach_identity(
                river=str(reach_context["river"]),
                reach=str(reach_context["reach"]),
                used_names=used_names,
                suffix=f"I{index}",
            )
            used_names.add((river, reach))
            prepared_reaches.append(
                {
                    **reach_context,
                    "river": river,
                    "reach": reach,
                    "flow_key": f"independent_{index}",
                    "boundary_role": "independent",
                    "selection_label": str(
                        reach_input.get("name")
                        or reach_input.get("project_stem")
                        or f"{river}/{reach}"
                    ),
                }
            )

        return prepared_reaches

    @staticmethod
    def _unique_reach_identity(
        river: str,
        reach: str,
        used_names: set[tuple[str, str]],
        suffix: str,
    ) -> tuple[str, str]:
        base_river = river or "RIVER"
        base_reach = reach or base_river
        if (base_river, base_reach) not in used_names:
            return base_river, base_reach

        counter = 1
        while True:
            river_candidate = HECRAS._hecras_identifier(
                f"{base_river}-{suffix}{counter}",
                default=base_river,
            )
            reach_candidate = HECRAS._hecras_identifier(
                f"{base_reach}-{suffix}{counter}",
                default=base_reach,
            )
            if (river_candidate, reach_candidate) not in used_names:
                return river_candidate, reach_candidate
            counter += 1

    @staticmethod
    def _replace_reach_identity(
        reach_context: dict[str, Any],
        river: str,
        reach: str,
    ) -> dict[str, Any]:
        updated = dict(reach_context)
        updated["river"] = river
        updated["reach"] = reach
        return updated

    def _apply_reference_names_to_junction_context(
        self,
        reference_template: JunctionReferenceTemplate,
        tributary_context: dict[str, Any],
        main_upper: dict[str, Any],
        lower_context: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        upstream_reaches = reference_template.upstream_reaches
        downstream_reach = reference_template.downstream_reach
        if len(upstream_reaches) >= 2:
            tributary_context = self._replace_reach_identity(
                tributary_context,
                river=upstream_reaches[0][0],
                reach=upstream_reaches[0][1],
            )
            main_upper = self._replace_reach_identity(
                main_upper,
                river=upstream_reaches[1][0],
                reach=upstream_reaches[1][1],
            )
        if downstream_reach is not None:
            lower_context = self._replace_reach_identity(
                lower_context,
                river=downstream_reach[0],
                reach=self._normalize_lower_reach_name(
                    main_reach=main_upper["reach"],
                    candidate_reach=downstream_reach[1],
                ),
            )
        return tributary_context, main_upper, lower_context

    def _build_reach_context_from_existing_context(
        self,
        geometry_context: dict[str, Any],
        start_index: int,
        end_index: int,
        reach_name: str,
        river_station_offset: float,
        preserve_river_stations: bool = False,
    ) -> dict[str, Any]:
        subset = [self._clone_section(section) for section in geometry_context["sections"][start_index : end_index + 1]]
        if len(subset) < 2:
            raise ValueError("Each junction reach must contain at least two sections.")

        start_measure = float(geometry_context["sections"][start_index].centerline_measure)
        end_measure = float(geometry_context["sections"][end_index].centerline_measure)
        centerline_segment = self._substring_line(
            geometry_context["centerline_line"],
            start_measure,
            end_measure,
        )
        centerline_points = [
            (float(x), float(y)) for x, y in centerline_segment.coords
        ]
        centerline_measures = self._cumulative_stationing(centerline_points)
        section_centerline_measures = [
            float(section.centerline_measure - start_measure) for section in subset
        ]
        self._populate_reach_lengths(subset, section_centerline_measures)
        subset = self._drop_invalid_nonterminal_sections(
            subset,
            min_positive_length=0.01,
        )
        section_centerline_measures = [
            float(section.centerline_measure) for section in subset
        ]
        self._populate_reach_lengths(subset, section_centerline_measures)
        if not preserve_river_stations:
            self._assign_river_stations(subset)
            subset = self._offset_reach_river_stations(subset, river_station_offset)
        self._validate_reach_lengths(
            subset,
            river=geometry_context["river"],
            reach=reach_name,
        )
        return {
            "river": geometry_context["river"],
            "reach": reach_name,
            "sections": subset,
            "structures": self._offset_reach_structures(
                self._select_structures_for_sections(
                    geometry_context.get("structures", []),
                    subset,
                ),
                0.0 if preserve_river_stations else river_station_offset,
            ),
            "centerline_points": centerline_points,
            "centerline_measures": centerline_measures,
            "centerline_line": LineString(centerline_points),
            "left_bank_line": geometry_context.get("left_bank_line"),
            "right_bank_line": geometry_context.get("right_bank_line"),
            "hdf_bank_lines_raw": geometry_context.get("hdf_bank_lines_raw", []),
            "hdf_bank_lines_projected": geometry_context.get(
                "hdf_bank_lines_projected",
                [],
            ),
            "friction_slope": self._estimate_downstream_friction_slope(subset),
        }

    @staticmethod
    def _select_structures_for_sections(
        structures: list[StructureData],
        sections: list[SectionData],
    ) -> list[StructureData]:
        if not structures or not sections:
            return []

        river_stations = [float(section.river_station) for section in sections]
        reach_min = min(river_stations)
        reach_max = max(river_stations)
        selected: list[StructureData] = []
        for structure in structures:
            if (
                reach_min - 1e-6
                <= float(structure.downstream_section_station)
                <= reach_max + 1e-6
                and reach_min - 1e-6
                <= float(structure.upstream_section_station)
                <= reach_max + 1e-6
            ):
                selected.append(replace(structure))
        return selected

    @staticmethod
    def _offset_reach_structures(
        structures: list[StructureData],
        offset: float,
    ) -> list[StructureData]:
        adjusted: list[StructureData] = []
        for structure in structures:
            adjusted.append(
                replace(
                    structure,
                    river_station=float(structure.river_station) + offset,
                    upstream_section_station=float(structure.upstream_section_station)
                    + offset,
                    downstream_section_station=float(structure.downstream_section_station)
                    + offset,
                )
            )
        return adjusted

    def _split_main_centerline_at_junction(
        self,
        main_context: dict[str, Any],
        main_upper: dict[str, Any],
        lower_context: dict[str, Any],
        main_split: dict[str, int],
        junction_point: Point,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        main_line = main_context["centerline_line"]
        upstream_end_measure = float(
            main_context["sections"][main_split["upstream_end_index"]].centerline_measure
        )
        downstream_start_measure = float(
            main_context["sections"][main_split["downstream_start_index"]].centerline_measure
        )
        projected_measure = float(main_line.project(junction_point))
        split_measure = min(
            max(projected_measure, upstream_end_measure),
            downstream_start_measure,
        )

        upper_segment = self._substring_line(
            main_line,
            0.0,
            split_measure,
        )
        lower_segment = self._substring_line(
            main_line,
            split_measure,
            float(main_line.length),
        )

        upper_points = [
            (float(x), float(y)) for x, y in upper_segment.coords
        ]
        lower_points = [
            (float(x), float(y)) for x, y in lower_segment.coords
        ]

        main_upper["centerline_points"] = upper_points
        main_upper["centerline_measures"] = self._cumulative_stationing(upper_points)
        main_upper["centerline_line"] = LineString(upper_points)

        lower_context["centerline_points"] = lower_points
        lower_context["centerline_measures"] = self._cumulative_stationing(lower_points)
        lower_context["centerline_line"] = LineString(lower_points)
        return main_upper, lower_context

    def _build_tributary_centerline_from_reference_context(
        self,
        reference_context: dict[str, Any],
        junction_sections: list[SectionData],
        junction_point: Point,
    ) -> tuple[list[tuple[float, float]], list[float], list[float]]:
        if len(junction_sections) < 2:
            raise ValueError("Tributary junction reach requires at least two sections.")

        original_sections = reference_context["sections"]
        source_to_measure = {
            int(section.source_station): float(section.centerline_measure)
            for section in original_sections
        }
        first_source = int(junction_sections[0].source_station)
        last_real_source = int(junction_sections[-2].source_station)
        start_measure = source_to_measure[first_source]
        end_measure = source_to_measure[last_real_source]

        segment = self._substring_line(
            reference_context["centerline_line"],
            start_measure,
            end_measure,
        )
        centerline_points = [
            (float(x), float(y)) for x, y in segment.coords
        ]
        centerline_points = self._append_junction_to_centerline(
            centerline_points,
            junction_point,
        )
        centerline_measures = self._cumulative_stationing(centerline_points)

        section_measures: list[float] = []
        for section in junction_sections[:-1]:
            section_measures.append(
                source_to_measure[int(section.source_station)] - start_measure
            )
        section_measures.append(centerline_measures[-1])
        return centerline_points, centerline_measures, section_measures

    @staticmethod
    def _clone_section(section: SectionData) -> SectionData:
        return replace(
            section,
            station_elevation=section.station_elevation.copy(),
            line=LineString(section.line.coords),
            left_bank_point=Point(section.left_bank_point),
            right_bank_point=Point(section.right_bank_point),
            channel_point=Point(section.channel_point),
        )

    @staticmethod
    def _offset_reach_river_stations(
        sections: list[SectionData],
        offset: float,
    ) -> list[SectionData]:
        adjusted: list[SectionData] = []
        for section in sections:
            adjusted.append(replace(section, river_station=section.river_station + offset))
        return adjusted

    def _split_main_sections_for_junction(
        self,
        sections: list[SectionData],
        local_box,
    ) -> dict[str, int]:
        matched = [
            idx
            for idx, section in enumerate(sections)
            if section.line.intersects(local_box) or local_box.contains(section.channel_point)
        ]
        if len(matched) < 2:
            raise ValueError("Could not identify the main-stem sections surrounding the junction.")
        upstream_end_index = min(matched)
        downstream_start_index = max(matched)
        if downstream_start_index <= upstream_end_index:
            raise ValueError("Invalid main-stem junction section ordering.")
        return {
            "upstream_end_index": upstream_end_index,
            "downstream_start_index": downstream_start_index,
        }

    def _load_bank_lines(self, bank_lines_shp: str | Path) -> list[LineString]:
        bank_gdf = self._load_bank_gdf(bank_lines_shp)
        return [
            self._coerce_line_string(geom)
            for geom in bank_gdf.geometry
            if geom is not None and not geom.is_empty
        ]

    def _load_bank_gdf(self, bank_lines_shp: str | Path) -> gpd.GeoDataFrame:
        bank_gdf = gpd.read_file(bank_lines_shp)
        return self._project_bank_gdf_to_model_crs(bank_gdf)

    def _load_bank_line_features_for_hdf(
        self,
        bank_lines_shp: str | Path,
    ) -> tuple[list[LineString], list[LineString]]:
        """Return source-CRS and model-CRS bank line features for HDF GIS layers."""
        raw_gdf = gpd.read_file(bank_lines_shp)
        raw_feature_gdf = raw_gdf.iloc[::-1]
        raw_lines = [
            self._coerce_line_string(geom)
            for geom in raw_feature_gdf.geometry
            if geom is not None and not geom.is_empty
        ]
        projected_gdf = self._project_bank_gdf_to_model_crs(raw_gdf)
        projected_feature_gdf = projected_gdf.iloc[::-1]
        projected_lines = [
            self._coerce_line_string(geom)
            for geom in projected_feature_gdf.geometry
            if geom is not None and not geom.is_empty
        ]

        if raw_gdf.crs is None or raw_gdf.crs == projected_gdf.crs:
            return [], projected_lines
        return raw_lines, projected_lines

    def _offset_hdf_bank_lines(
        self,
        bank_lines: list[LineString],
        centerlines: list[LineString],
        offset_m: float | None = None,
    ) -> list[LineString]:
        if not bank_lines:
            return []
        offset_distance = self.BANK_LINE_OFFSET_M if offset_m is None else float(offset_m)
        if abs(offset_distance) <= 1e-9:
            return bank_lines

        valid_centerlines = [
            centerline
            for centerline in centerlines
            if centerline is not None and not centerline.is_empty
        ]
        if not valid_centerlines:
            return bank_lines

        if DTMChannelModifier is not None:
            try:
                if (
                    len(valid_centerlines) > 1
                    and hasattr(DTMChannelModifier, "_offset_junction_bank_lines_outward")
                ):
                    offset_lines = DTMChannelModifier._offset_junction_bank_lines_outward(
                        bank_lines,
                        centerlines=valid_centerlines,
                        offset_m=offset_distance,
                    )
                    if isinstance(offset_lines, list):
                        clean_lines = [
                            line
                            for line in offset_lines
                            if line is not None
                            and not line.is_empty
                            and isinstance(line, LineString)
                        ]
                    else:
                        clean_lines = self._line_strings_from_geometry(offset_lines)
                    return clean_lines or bank_lines
                if hasattr(DTMChannelModifier, "_get_outward_offset_line"):
                    centerline = valid_centerlines[0]
                    return [
                        DTMChannelModifier._get_outward_offset_line(
                            bank_line,
                            centerline,
                            offset_distance,
                        )
                        for bank_line in bank_lines
                        if bank_line is not None and not bank_line.is_empty
                    ]
            except Exception as exc:
                logger.warning(
                    "Could not apply DTM %.3f m HDF bank offset; using fallback "
                    "point-offset method: %s",
                    offset_distance,
                    exc,
                )

        offset_lines: list[LineString] = []
        for bank_line in bank_lines:
            if bank_line is None or bank_line.is_empty:
                continue
            centerline = min(
                valid_centerlines,
                key=lambda candidate: float(bank_line.distance(candidate)),
            )
            offset_lines.append(
                HECRAS._offset_line_away_from_centerline(
                    line=bank_line,
                    centerline_line=centerline,
                    offset_distance=offset_distance,
                )
            )
        return offset_lines or bank_lines

    def _prepare_dtm_bank_lines_for_reach(
        self,
        cross_section_df: pd.DataFrame,
        bank_lines_shp: str | Path,
    ) -> Optional[list[LineString]]:
        if DTMChannelModifier is None:
            return None

        try:
            bank_gdf = self._load_bank_gdf(bank_lines_shp)
            prepared_gdf = DTMChannelModifier.clean_and_merge_banklines(
                bank_gdf,
                bridge_junctions=True,
            )
            prepared_lines = self._line_strings_from_geometry(prepared_gdf)
            selected_lines = self._select_reach_bank_line_pair(
                cross_section_df=cross_section_df,
                bank_lines=prepared_lines,
            )
            return self._offset_bank_lines_with_dtm(
                bank_lines=selected_lines,
                fallback_centerline=None,
            )
        except Exception as exc:
            logger.warning(
                "DTM bank-line preparation failed for %s; falling back to raw "
                "SEV_USTU grouping: %s",
                bank_lines_shp,
                exc,
            )
            return None

    def _offset_bank_lines_with_dtm(
        self,
        bank_lines: list[LineString],
        fallback_centerline: Optional[LineString],
        offset_m: float | None = None,
    ) -> list[LineString]:
        if not bank_lines:
            return []
        if DTMChannelModifier is None:
            return bank_lines

        offset_distance = self.BANK_LINE_OFFSET_M if offset_m is None else float(offset_m)
        if abs(offset_distance) <= 1e-9:
            return bank_lines

        try:
            centerline = fallback_centerline
            if centerline is None:
                banks_gdf = gpd.GeoDataFrame(
                    geometry=bank_lines[:2],
                    crs=self.source_crs if self.source_crs != "UNKNOWN" else None,
                )
                centerline = DTMChannelModifier.generate_centerline_from_banks(
                    banks_gdf
                ).geometry.iloc[0]

            if hasattr(DTMChannelModifier, "_get_outward_offset_line"):
                offset_lines = [
                    DTMChannelModifier._get_outward_offset_line(
                        bank_line,
                        centerline,
                        offset_distance,
                    )
                    for bank_line in bank_lines[:2]
                    if bank_line is not None and not bank_line.is_empty
                ]
            else:
                offset_lines = DTMChannelModifier._offset_bank_lines_outward(
                    bank_lines,
                    centerline=centerline,
                    offset_m=offset_distance,
                )
            if len(offset_lines) >= 2:
                return offset_lines[:2]
        except Exception as exc:
            logger.warning(
                "Could not apply DTM %.3f m outward bank offset; using "
                "prepared bank lines without offset: %s",
                offset_distance,
                exc,
            )
        return bank_lines[:2]

    @staticmethod
    def _line_strings_from_geometry(geometry_input: Any) -> list[LineString]:
        if DTMChannelModifier is not None:
            return DTMChannelModifier._line_strings(geometry_input)

        geometries = (
            geometry_input.geometry
            if isinstance(geometry_input, gpd.GeoDataFrame)
            else [geometry_input]
        )
        lines: list[LineString] = []
        for geom in geometries:
            if geom is None or geom.is_empty:
                continue
            if geom.geom_type == "LineString":
                lines.append(geom)
            elif geom.geom_type == "MultiLineString":
                lines.extend(list(geom.geoms))
        return sorted(lines, key=lambda item: item.length, reverse=True)

    def _select_reach_bank_line_pair(
        self,
        cross_section_df: pd.DataFrame,
        bank_lines: list[LineString],
    ) -> list[LineString]:
        if len(bank_lines) < 2:
            raise ValueError("Bank line preparation produced fewer than two lines.")
        if len(bank_lines) == 2:
            return list(bank_lines)

        section_lines: list[LineString] = []
        for _, group in cross_section_df.groupby("Station", sort=True):
            if len(group) < 2:
                continue
            section_line = LineString(list(zip(group["X"], group["Y"])))
            if section_line.length > 0.0:
                section_lines.append(section_line)
        if not section_lines:
            raise ValueError("No usable cross sections were available for bank selection.")

        median_section_length = float(np.median([line.length for line in section_lines]))
        near_tolerance = max(2.0, min(10.0, median_section_length * 0.25))
        pair_scores: list[tuple[tuple[float, ...], int, int]] = []
        for left_index in range(len(bank_lines)):
            for right_index in range(left_index + 1, len(bank_lines)):
                left_line = bank_lines[left_index]
                right_line = bank_lines[right_index]
                near_count = 0
                widths = []
                bank_distances = []
                for section_line in section_lines:
                    left_distance = float(section_line.distance(left_line))
                    right_distance = float(section_line.distance(right_line))
                    if left_distance <= near_tolerance and right_distance <= near_tolerance:
                        near_count += 1
                    left_point = self._line_to_bank_point(section_line, left_line)
                    right_point = self._line_to_bank_point(section_line, right_line)
                    width = abs(
                        self._project_distance(section_line, left_point)
                        - self._project_distance(section_line, right_point)
                    )
                    if width > 1e-6:
                        widths.append(float(width))
                        bank_distances.append(left_distance + right_distance)

                if not widths:
                    continue

                mean_width = float(np.mean(widths))
                width_variation = float(np.std(widths) / max(mean_width, 1e-6))
                mean_bank_distance = float(np.mean(bank_distances)) if bank_distances else np.inf
                pair_scores.append(
                    (
                        (
                            float(near_count),
                            float(len(widths)),
                            -mean_bank_distance,
                            -width_variation,
                            mean_width,
                        ),
                        left_index,
                        right_index,
                    )
                )

        if not pair_scores:
            raise ValueError("Could not identify a bank-line pair for the cross sections.")

        _, left_index, right_index = max(pair_scores, key=lambda item: item[0])
        logger.info(
            "Selected bank-line pair %s/%s from %s prepared SEV_USTU lines.",
            left_index + 1,
            right_index + 1,
            len(bank_lines),
        )
        return [bank_lines[left_index], bank_lines[right_index]]

    def _load_model_projection_crs(self) -> str:
        if self.target_projection_crs:
            return self.target_projection_crs

        return ""

    def _set_target_projection_crs(
        self,
        projection_file: Optional[str | Path],
    ) -> None:
        if projection_file is None:
            return

        projection_path = Path(projection_file)
        if not projection_path.exists():
            return

        target_projection_crs = projection_path.read_text(encoding="utf-8").strip()
        if target_projection_crs:
            self.target_projection_crs = target_projection_crs

    def _project_bank_gdf_to_model_crs(self, bank_gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        target_crs = self._load_model_projection_crs()
        if not target_crs:
            if bank_gdf.crs:
                self.source_crs = str(bank_gdf.crs)
            return bank_gdf

        if bank_gdf.crs is None:
            logger.warning(
                "Bank line shapefile has no CRS; assigning model projection without "
                "coordinate transformation."
            )
            projected_gdf = bank_gdf.set_crs(target_crs, allow_override=True)
        elif bank_gdf.crs == target_crs:
            projected_gdf = bank_gdf
        else:
            projected_gdf = bank_gdf.to_crs(target_crs)

        if projected_gdf.crs:
            self.source_crs = str(projected_gdf.crs)
        return projected_gdf

    def _select_combined_reach_bank_lines(
        self,
        cross_section_df: pd.DataFrame,
        bank_lines: list[LineString],
        local_box,
    ) -> list[LineString]:
        station_values = sorted(
            int(value) for value in cross_section_df["Station"].dropna().unique()
        )
        feature_infos: list[dict[str, Any]] = []
        for idx, bank_line in enumerate(bank_lines):
            intersected_stations: list[int] = []
            local_overlap = 0.0
            for station in station_values:
                station_df = cross_section_df[cross_section_df["Station"] == station]
                if len(station_df) < 2:
                    continue
                xs_line = LineString(list(zip(station_df["X"], station_df["Y"])))
                if not xs_line.intersection(bank_line).is_empty:
                    intersected_stations.append(station)
            if bank_line.intersects(local_box):
                local_overlap = float(max(bank_line.intersection(local_box).length, 1.0))
            if intersected_stations:
                feature_infos.append(
                    {
                        "index": idx,
                        "line": bank_line,
                        "stations": intersected_stations,
                        "count": len(intersected_stations),
                        "min_station": min(intersected_stations),
                        "max_station": max(intersected_stations),
                        "local_overlap": local_overlap,
                        "endpoint_distance_to_box": min(
                            Point(bank_line.coords[0]).distance(local_box),
                            Point(bank_line.coords[-1]).distance(local_box),
                        ),
                    }
                )

        if len(feature_infos) < 2:
            logger.info(
                "Combined junction shapefile did not expose two direct "
                "cross-section-intersecting bank features; selecting by reach "
                "cross-section proximity."
            )
            return self._select_reach_bank_line_pair(cross_section_df, bank_lines)

        last_station = station_values[-1]
        penultimate_station = station_values[-2] if len(station_values) >= 2 else last_station
        full_candidates = [
            info
            for info in feature_infos
            if info["count"] >= max(3, len(station_values) // 2)
        ]
        junction_candidates = [
            info
            for info in feature_infos
            if info["local_overlap"] > 0.0
            and info["max_station"] >= penultimate_station
            and info["count"] <= 3
        ]
        if not full_candidates or not junction_candidates:
            logger.info(
                "Could not separate full tributary banks from junction-local bank "
                "segments in the combined shapefile; selecting by reach "
                "cross-section proximity."
            )
            return self._select_reach_bank_line_pair(cross_section_df, bank_lines)

        full_candidates.sort(
            key=lambda info: (-info["count"], info["min_station"], info["index"])
        )
        junction_candidates.sort(
            key=lambda info: (
                -info["local_overlap"],
                -info["max_station"],
                info["count"],
                info["index"],
            )
        )

        full_bank = full_candidates[0]
        junction_bank = next(
            (
                info
                for info in junction_candidates
                if info["index"] != full_bank["index"]
            ),
            None,
        )
        if junction_bank is None:
            logger.info(
                "Could not find a distinct junction-local bank segment in the "
                "combined shapefile; selecting by reach cross-section proximity."
            )
            return self._select_reach_bank_line_pair(cross_section_df, bank_lines)

        self._last_combined_bank_selection = {
            "full_bank": full_bank,
            "junction_bank": junction_bank,
            "feature_infos": feature_infos,
        }
        return [full_bank["line"], junction_bank["line"]]

    def _junction_box_from_downstream_sections(
        self,
        cross_section_df: pd.DataFrame,
        pad_factor: float = 1.0,
    ):
        station_values = sorted(
            int(value) for value in cross_section_df["Station"].dropna().unique()
        )
        if len(station_values) < 2:
            raise ValueError("Need at least two sections to derive a junction box.")
        xs_a_df = cross_section_df[cross_section_df["Station"] == station_values[-2]]
        xs_b_df = cross_section_df[cross_section_df["Station"] == station_values[-1]]
        xs_a = LineString(list(zip(xs_a_df["X"], xs_a_df["Y"])))
        xs_b = LineString(list(zip(xs_b_df["X"], xs_b_df["Y"])))
        return self._junction_box(xs_a, xs_b, pad_factor=pad_factor)

    def _identify_local_opening_pair(
        self,
        cross_section_df: pd.DataFrame,
        bank_lines: list[LineString],
        local_box,
        use_downstream_sections: bool = False,
    ) -> Optional[dict[str, Any]]:
        sampled_widths, average_width = self._sample_bank_widths(
            cross_section_df=cross_section_df,
            bank_lines=bank_lines,
            sample_count=5,
        )
        if not sampled_widths or average_width <= 0.0:
            return None
        opening_pairs = self._find_bank_opening_pairs(
            bank_lines=bank_lines,
            average_width=average_width,
            width_factor=3.0,
        )
        candidates = [
            pair
            for pair in opening_pairs
            if local_box.contains(pair["left_point"])
            and local_box.contains(pair["right_point"])
        ]
        if not candidates:
            return None
        if use_downstream_sections:
            return min(
                candidates,
                key=lambda pair: (
                    pair["left_point"].y + pair["right_point"].y,
                    pair["left_point"].x + pair["right_point"].x,
                ),
            )
        return min(
            candidates,
            key=lambda pair: pair["gap_distance"],
        )

    def _remap_opening_pair(
        self,
        bank_lines: list[LineString],
        source_pair: dict[str, Any],
        target_pair: dict[str, Any],
    ) -> list[LineString]:
        left_end_index = self._matching_endpoint_index(
            bank_lines[source_pair["left_line_index"]],
            source_pair["left_point"],
        )
        right_end_index = self._matching_endpoint_index(
            bank_lines[source_pair["right_line_index"]],
            source_pair["right_point"],
        )
        source_options = [
            (
                source_pair["left_line_index"],
                left_end_index,
                source_pair["left_point"],
            ),
            (
                source_pair["right_line_index"],
                right_end_index,
                source_pair["right_point"],
            ),
        ]
        target_points = [target_pair["left_point"], target_pair["right_point"]]
        direct_cost = source_options[0][2].distance(target_points[0]) + source_options[1][2].distance(target_points[1])
        cross_cost = source_options[0][2].distance(target_points[1]) + source_options[1][2].distance(target_points[0])
        if cross_cost < direct_cost:
            target_points = [target_pair["right_point"], target_pair["left_point"]]

        updated = list(bank_lines)
        for (line_index, end_index, _), target in zip(source_options, target_points):
            coords = list(updated[line_index].coords)
            if end_index == 0:
                coords[0] = (float(target.x), float(target.y))
            else:
                coords[-1] = (float(target.x), float(target.y))
            updated[line_index] = LineString(coords)
        return updated

    @staticmethod
    def _matching_endpoint_index(
        line: LineString,
        point: Point,
    ) -> int:
        start_point = Point(line.coords[0])
        end_point = Point(line.coords[-1])
        return 0 if start_point.distance(point) <= end_point.distance(point) else 1

    @staticmethod
    def _replace_endpoint(
        points: list[tuple[float, float]],
        index: int,
        point: tuple[float, float],
    ) -> list[tuple[float, float]]:
        updated = list(points)
        updated[index] = point
        return updated

    @staticmethod
    def _append_junction_to_centerline(
        centerline_points: list[tuple[float, float]],
        junction_point: Point,
    ) -> list[tuple[float, float]]:
        updated = list(centerline_points)
        terminal = (float(junction_point.x), float(junction_point.y))
        if not updated:
            return [terminal]
        if HECRAS._points_are_close(updated[-1], terminal):
            updated[-1] = terminal
            return updated
        updated.append(terminal)
        return updated

    @staticmethod
    def _prepend_junction_to_centerline(
        centerline_points: list[tuple[float, float]],
        junction_point: Point,
    ) -> list[tuple[float, float]]:
        updated = list(centerline_points)
        terminal = (float(junction_point.x), float(junction_point.y))
        if not updated:
            return [terminal]
        if HECRAS._points_are_close(updated[0], terminal):
            updated[0] = terminal
            return updated
        return [terminal, *updated]

    def _resolve_junction_point(
        self,
        tributary_sections: list[SectionData],
        main_sections: list[SectionData],
        fallback_point: Point,
        reference_geometry_file: Optional[str | Path],
        local_box,
    ) -> Point:
        del reference_geometry_file
        if len(tributary_sections) >= 2 and len(main_sections) >= 2:
            intersection = self._extended_reach_intersection(
                tributary_sections[-2].channel_point,
                tributary_sections[-1].channel_point,
                main_sections[-2].channel_point,
                main_sections[-1].channel_point,
            )
            if intersection is not None and local_box.buffer(60.0).contains(intersection):
                return intersection

        if len(tributary_sections) >= 2:
            previous_point = tributary_sections[-2].channel_point
            last_point = tributary_sections[-1].channel_point
            return self._extend_point_along_direction(
                previous_point,
                last_point,
                extension_distance=min(
                    max(previous_point.distance(last_point) * 0.22, 8.0),
                    15.0,
                ),
            )
        return fallback_point

    @staticmethod
    def _extend_point_along_direction(
        start_point: Point,
        end_point: Point,
        extension_distance: float,
    ) -> Point:
        dx = float(end_point.x - start_point.x)
        dy = float(end_point.y - start_point.y)
        segment_length = math.hypot(dx, dy)
        if segment_length <= 1e-6:
            return Point(end_point.x, end_point.y)
        return Point(
            float(end_point.x + dx / segment_length * extension_distance),
            float(end_point.y + dy / segment_length * extension_distance),
        )

    @staticmethod
    def _extended_reach_intersection(
        left_prev: Point,
        left_last: Point,
        right_prev: Point,
        right_last: Point,
        extension_distance: float = 250.0,
    ) -> Optional[Point]:
        left_end = HECRAS._extend_point_along_direction(
            left_prev,
            left_last,
            extension_distance,
        )
        right_end = HECRAS._extend_point_along_direction(
            right_prev,
            right_last,
            extension_distance,
        )
        left_line = LineString(
            [
                (float(left_last.x), float(left_last.y)),
                (float(left_end.x), float(left_end.y)),
            ]
        )
        right_line = LineString(
            [
                (float(right_last.x), float(right_last.y)),
                (float(right_end.x), float(right_end.y)),
            ]
        )
        intersection = left_line.intersection(right_line)
        if intersection.is_empty:
            return None
        if intersection.geom_type == "Point":
            return Point(intersection.x, intersection.y)
        if intersection.geom_type == "MultiPoint":
            return Point(list(intersection.geoms)[0])
        return None

    @staticmethod
    def _read_reference_junction_point(
        reference_geometry_file: str | Path,
    ) -> Optional[Point]:
        geometry_path = Path(reference_geometry_file)
        if not geometry_path.exists():
            return None
        for line in geometry_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("Junct X Y & Text X Y="):
                payload = line.split("=", 1)[1]
                parts = [part.strip() for part in payload.split(",") if part.strip()]
                if len(parts) >= 2:
                    try:
                        return Point(float(parts[0]), float(parts[1]))
                    except ValueError:
                        return None
        return None

    @staticmethod
    def _parse_reference_reach_line(
        line: str,
    ) -> Optional[tuple[str, str]]:
        if "=" not in line:
            return None
        payload = line.split("=", 1)[1]
        parts = [part.strip() for part in payload.split(",", 1)]
        if len(parts) != 2:
            return None
        if not parts[0] or not parts[1]:
            return None
        return parts[0], parts[1]

    @classmethod
    def _load_junction_reference_template(
        cls,
        reference_geometry_file: Optional[str | Path],
    ) -> Optional[JunctionReferenceTemplate]:
        if reference_geometry_file is None:
            return None

        geometry_path = Path(reference_geometry_file)
        if not geometry_path.exists():
            return None

        project_stem = geometry_path.stem
        project_title = project_stem
        prj_path = geometry_path.with_suffix(".prj")
        if prj_path.exists():
            for line in prj_path.read_text(
                encoding="utf-8",
                errors="ignore",
            ).splitlines():
                if line.startswith("Proj Title="):
                    project_title = line.split("=", 1)[1].strip() or project_stem
                    break

        geom_title = project_stem
        junction_name: Optional[str] = None
        upstream_reaches: list[tuple[str, str]] = []
        downstream_reach: Optional[tuple[str, str]] = None
        junction_lengths_angles: list[str] = []

        for line in geometry_path.read_text(
            encoding="utf-8",
            errors="ignore",
        ).splitlines():
            if line.startswith("Geom Title="):
                geom_title = line.split("=", 1)[1].strip() or project_stem
            elif line.startswith("Junct Name="):
                junction_name = line.split("=", 1)[1].strip() or None
            elif line.startswith("Up River,Reach="):
                reach_info = cls._parse_reference_reach_line(line)
                if reach_info is not None:
                    upstream_reaches.append(reach_info)
            elif line.startswith("Dn River,Reach="):
                downstream_reach = cls._parse_reference_reach_line(line)
            elif line.startswith("Junc L&A="):
                junction_lengths_angles.append(line.split("=", 1)[1].strip())

        return JunctionReferenceTemplate(
            project_stem=project_stem,
            project_title=project_title,
            geom_title=geom_title,
            junction_name=junction_name,
            upstream_reaches=upstream_reaches,
            downstream_reach=downstream_reach,
            junction_lengths_angles=junction_lengths_angles,
        )

    @staticmethod
    def _split_name_chunks(value: str) -> list[str]:
        return re.findall(r"[A-Za-z]+|\d+", str(value))

    @classmethod
    def infer_junction_project_stem(
        cls,
        main_river: str,
        tributary_river: str,
    ) -> str:
        main_chunks = cls._split_name_chunks(str(main_river).replace("-", " "))
        tributary_chunks = cls._split_name_chunks(str(tributary_river).replace("-", " "))
        common: list[str] = []
        for main_chunk, trib_chunk in zip(main_chunks, tributary_chunks):
            if main_chunk.casefold() != trib_chunk.casefold():
                break
            common.append(main_chunk)

        if common:
            base = "".join(common).title()
            main_suffix = "".join(main_chunks[len(common):])
            trib_suffix = "".join(tributary_chunks[len(common):])
            stem = f"{base}{main_suffix}{trib_suffix}"
            if stem:
                return stem

        main_clean = re.sub(r"[^A-Za-z0-9]+", "", str(main_river)).title()
        trib_clean = re.sub(r"[^A-Za-z0-9]+", "", str(tributary_river)).title()
        return f"{main_clean}{trib_clean}" or "JunctionModel"

    @staticmethod
    def infer_junction_geom_title(project_stem: str) -> str:
        return f"MEVCUT_DURUM_{project_stem}_1D"

    @staticmethod
    def infer_junction_name(project_stem: str) -> str:
        match = re.match(r"^([A-Za-z]+)(.*)$", project_stem)
        if match:
            prefix, suffix = match.groups()
        else:
            prefix, suffix = project_stem, ""
        compact_prefix = prefix[: max(0, 8 - len(suffix))]
        compact = f"{compact_prefix}{suffix}"[:8] or project_stem[:8]
        return f"{compact}_junc"

    @classmethod
    def _lower_reach_name(cls, main_reach: str) -> str:
        reach = str(main_reach).strip()
        if reach.endswith(cls.LOWER_REACH_SUFFIX):
            return reach
        if reach.endswith(cls.LEGACY_LOWER_REACH_SUFFIX):
            reach = reach[: -len(cls.LEGACY_LOWER_REACH_SUFFIX)]
        return f"{reach}{cls.LOWER_REACH_SUFFIX}"

    @classmethod
    def _normalize_lower_reach_name(
        cls,
        main_reach: str,
        candidate_reach: Optional[str] = None,
    ) -> str:
        reach = str(candidate_reach).strip() if candidate_reach else ""
        if not reach:
            return cls._lower_reach_name(main_reach)
        if reach.endswith(cls.LEGACY_LOWER_REACH_SUFFIX):
            return cls._lower_reach_name(
                reach[: -len(cls.LEGACY_LOWER_REACH_SUFFIX)]
            )
        return reach

    @classmethod
    def _infer_junction_naming_template(
        cls,
        main_river: str,
        main_reach: str,
        tributary_river: str,
        tributary_reach: str,
    ) -> JunctionReferenceTemplate:
        project_stem = cls.infer_junction_project_stem(
            main_river=main_river,
            tributary_river=tributary_river,
        )
        return JunctionReferenceTemplate(
            project_stem=project_stem,
            project_title=project_stem,
            geom_title=cls.infer_junction_geom_title(project_stem),
            junction_name=cls.infer_junction_name(project_stem),
            upstream_reaches=[
                (tributary_river, tributary_reach),
                (main_river, main_reach),
            ],
            downstream_reach=(main_river, cls._lower_reach_name(main_reach)),
        )

    @staticmethod
    def _extend_tributary_to_junction(
        tributary_sections: list[SectionData],
        junction_point: Point,
    ) -> list[SectionData]:
        if len(tributary_sections) < 1:
            return tributary_sections

        last_section = tributary_sections[-1]
        shift_x = float(junction_point.x - last_section.channel_point.x)
        shift_y = float(junction_point.y - last_section.channel_point.y)
        if math.hypot(shift_x, shift_y) <= 1e-6:
            return tributary_sections

        translated_coords = [
            (float(x + shift_x), float(y + shift_y))
            for x, y in last_section.line.coords
        ]
        translated_left = Point(
            float(last_section.left_bank_point.x + shift_x),
            float(last_section.left_bank_point.y + shift_y),
        )
        translated_right = Point(
            float(last_section.right_bank_point.x + shift_x),
            float(last_section.right_bank_point.y + shift_y),
        )
        translated_channel = Point(float(junction_point.x), float(junction_point.y))
        extension_left = last_section.left_bank_point.distance(translated_left)
        extension_right = last_section.right_bank_point.distance(translated_right)
        extension_channel = last_section.channel_point.distance(translated_channel)
        synthetic_section = replace(
            last_section,
            line=LineString(translated_coords),
            left_bank_point=translated_left,
            right_bank_point=translated_right,
            channel_point=translated_channel,
            left_bank_measure=last_section.left_bank_measure + extension_left,
            right_bank_measure=last_section.right_bank_measure + extension_right,
            centerline_measure=last_section.centerline_measure + extension_channel,
        )
        return list(tributary_sections) + [synthetic_section]

    @staticmethod
    def _drop_invalid_nonterminal_sections(
        sections: list[SectionData],
        min_positive_length: float,
    ) -> list[SectionData]:
        if len(sections) <= 2:
            return sections

        filtered = list(sections)
        changed = True
        while changed and len(filtered) > 2:
            changed = False
            invalid_idx: Optional[int] = None
            for idx, section in enumerate(filtered[:-1]):
                if (
                    section.left_reach_length <= min_positive_length
                    or section.channel_reach_length <= min_positive_length
                    or section.right_reach_length <= min_positive_length
                ):
                    invalid_idx = idx
            if invalid_idx is not None:
                filtered.pop(invalid_idx)
                changed = True
        return filtered

    @staticmethod
    def _validate_reach_lengths(
        sections: list[SectionData],
        river: str,
        reach: str,
        min_positive_length: float = 1e-6,
    ) -> None:
        for section in sections[:-1]:
            if (
                section.left_reach_length <= min_positive_length
                or section.channel_reach_length <= min_positive_length
                or section.right_reach_length <= min_positive_length
            ):
                raise ValueError(
                    "Invalid non-terminal reach lengths detected for "
                    f"{river}/{reach} at source station {section.source_station}: "
                    f"L={section.left_reach_length:.3f}, "
                    f"Ch={section.channel_reach_length:.3f}, "
                    f"R={section.right_reach_length:.3f}"
                )

    def _build_junction_flow_screening_message(
        self,
        main_selection: HydrologyPointSelection,
        tributary_selection: HydrologyPointSelection,
        lower_selection: Optional[HydrologyPointSelection],
        max_safe_run: Optional[FlowRunResult],
        buffer_distance: float,
        screening_stopped_early: bool,
        all_flows_in_single_plan: bool = False,
    ) -> str:
        single_plan_text = (
            " All tested flows were computed as profiles in one plan."
            if all_flows_in_single_plan
            else ""
        )
        lower_text = (
            f" A distinct downstream main-reach point ({lower_selection.point_name}) "
            "was also available for lower-reach flow checks."
            if lower_selection is not None
            else ""
        )
        if max_safe_run is None:
            return (
                "No coupled junction run stayed within bank for the tested "
                f"return periods using a {self._format_number(buffer_distance, 2)} m "
                f"hydrology search buffer.{single_plan_text}{lower_text}"
            )
        if all_flows_in_single_plan:
            suffix = "with all tested flows computed as profiles in one plan."
        else:
            suffix = (
                "and screening stopped early."
                if screening_stopped_early
                else "after testing all return periods."
            )
        return (
            f"Selected main hydrology point {main_selection.point_name} and "
            f"tributary point {tributary_selection.point_name}. "
            f"Maximum safe coupled run was {max_safe_run.return_period} "
            f"({self._format_number(max_safe_run.discharge_cms, 3)} cms lower reach), "
            f"{suffix}{lower_text}"
        )

    def _write_junction_flow_screening_text_report(
        self,
        report_txt: Path,
        message: str,
        buffer_distance: float,
        main_selection: HydrologyPointSelection,
        tributary_selection: HydrologyPointSelection,
        lower_selection: Optional[HydrologyPointSelection],
        run_results: list[FlowRunResult],
        max_safe_run: Optional[FlowRunResult],
    ) -> None:
        lines = [
            "Junction steady-flow screening\n",
            f"Buffer distance: {self._format_number(buffer_distance, 2)} m\n",
            "\n",
            f"{message}\n",
            "\n",
            f"Main point: {main_selection.point_name}\n",
            f"Tributary point: {tributary_selection.point_name}\n",
            (
                f"Lower-reach point: {lower_selection.point_name}\n"
                if lower_selection is not None
                else "Lower-reach point: none selected\n"
            ),
            "\n",
        ]
        for result in run_results:
            lines.append(
                f"{result.return_period}: "
                f"{self._format_number(result.discharge_cms, 3) if np.isfinite(result.discharge_cms) else 'nan'} cms | "
                f"success={result.compute_success} | out_of_bank={result.out_of_bank} | "
                f"{result.note}\n"
            )
        if max_safe_run is not None:
            lines.append(f"\nMax safe run: {max_safe_run.return_period}\n")
        report_txt.write_text("".join(lines), encoding="utf-8")

    @staticmethod
    def _junction_flow_triplet_from_note(note: str) -> dict[str, float]:
        values: dict[str, float] = {}
        for part in note.split("|"):
            if "=" not in part:
                continue
            key, value = part.strip().split("=", 1)
            if not value.strip().endswith("cms"):
                continue
            number_text = value.strip()[:-3].strip()
            try:
                values[key.strip()] = float(number_text)
            except ValueError:
                continue
        return values

    @staticmethod
    def _hydrologic_project_code(project_stem: str, project_title: str) -> str:
        """Return the internal model name used in HEC-RAS titles."""
        source = str(project_title or project_stem).strip()
        source = re.sub(r"(?i)_rev(?:ision)?[_-]?v?\d.*$", "", source)
        source = re.sub(r"(?i)_tum(?:_rev\d*)?.*$", "", source)
        source = re.sub(r"(?i)_circularstr\d+.*$", "", source)

        reference_names = {
            "BUR-BUR-TEF-TEFENNI1": "BUR-BUR-TEF-TEFENNI",
        }
        return reference_names.get(source, source)

    @staticmethod
    def _normalize_return_period_name(
        profile_name: str,
        return_period: Optional[str],
    ) -> str:
        candidate = str(return_period or profile_name or "").strip()
        if re.fullmatch(r"(?i)q\d+", candidate):
            return candidate.upper()
        return candidate or "PF 1"

    @classmethod
    def _build_model_naming(
        cls,
        project_stem: str,
        project_title: str,
        profile_name: str,
        return_period: Optional[str],
    ) -> dict[str, str]:
        hydrologic_code = cls._hydrologic_project_code(
            project_stem=project_stem,
            project_title=project_title,
        )
        profile = cls._normalize_return_period_name(
            profile_name=profile_name,
            return_period=return_period,
        )
        has_frequency_profile = bool(re.fullmatch(r"Q\d+", profile))
        flow_title = f"{profile}_Steady" if has_frequency_profile else "Steady"
        plan_title = (
            f"{hydrologic_code}_{profile}"
            if has_frequency_profile
            else f"{hydrologic_code}_Steady"
        )
        return {
            "project_title": hydrologic_code,
            "geom_title": "MEVCUT_DURUM_1D",
            "flow_title": flow_title,
            "profile_name": profile,
            "plan_title": plan_title,
        }

    def _write_model_files(
        self,
        project_folder: str | Path,
        project_stem: str,
        project_title: str,
        projection_file: Optional[str | Path],
        profile_name: str,
        flow_cms: float,
        channel_mannings_n: float,
        overbank_mannings_n: float,
        expansion_coeff: float,
        contraction_coeff: float,
        geometry_context: dict[str, Any],
        flow_profile: Optional[dict[str, float]] = None,
        return_period: Optional[str] = None,
        flow_profiles: Optional[list[tuple[str, Any]]] = None,
    ) -> BuildResult:
        project_folder = Path(project_folder)
        project_folder.mkdir(parents=True, exist_ok=True)
        naming = self._build_model_naming(
            project_stem=project_stem,
            project_title=project_title,
            profile_name=profile_name,
            return_period=return_period,
        )

        if projection_file is not None:
            projection_file = Path(projection_file)
            target_projection = project_folder / projection_file.name
            if (
                projection_file.exists()
                and projection_file.resolve() != target_projection.resolve()
            ):
                target_projection.write_bytes(projection_file.read_bytes())

        geometry_path = project_folder / f"{project_stem}.g01"
        flow_path = project_folder / f"{project_stem}.f01"
        plan_path = project_folder / f"{project_stem}.p01"
        project_path = project_folder / f"{project_stem}.prj"
        sdf_path = project_folder / "RASImport.sdf"
        if geometry_context.get("junction"):
            reference_template = geometry_context.get("reference_template")
            reaches = geometry_context["reaches"]
            self._write_junction_geometry_file(
                geometry_path=geometry_path,
                geom_title=(
                    reference_template.geom_title
                    if reference_template is not None
                    else naming["geom_title"]
                ),
                reaches=reaches,
                junction=geometry_context["junction"],
                expansion_coeff=expansion_coeff,
                contraction_coeff=contraction_coeff,
                channel_mannings_n=channel_mannings_n,
                overbank_mannings_n=overbank_mannings_n,
            )
            self._write_junction_steady_flow_file(
                flow_path=flow_path,
                flow_title=naming["flow_title"],
                reaches=reaches,
                junction=geometry_context["junction"],
                profile_name=naming["profile_name"],
                flow_profile=flow_profile or {},
                flow_profiles=flow_profiles,
            )
            self._write_junction_sdf_file(
                sdf_path=sdf_path,
                reaches=reaches,
            )
            river = geometry_context["river"]
            reach = geometry_context["junction"]["downstream_reach"]
            sections = geometry_context["all_sections"]
            friction_slope = geometry_context["lower_reach"]["friction_slope"]
            centerline_points = geometry_context["lower_reach"]["centerline_points"]
        else:
            river = geometry_context["river"]
            reach = geometry_context["reach"]
            sections = geometry_context["sections"]
            structures = geometry_context.get("structures", [])
            centerline_points = geometry_context["centerline_points"]
            centerline_measures = geometry_context["centerline_measures"]
            friction_slope = geometry_context["friction_slope"]

            self._write_geometry_file(
                geometry_path=geometry_path,
                geom_title=naming["geom_title"],
                river=river,
                reach=reach,
                sections=sections,
                structures=structures,
                centerline_points=centerline_points,
                expansion_coeff=expansion_coeff,
                contraction_coeff=contraction_coeff,
                channel_mannings_n=channel_mannings_n,
                overbank_mannings_n=overbank_mannings_n,
            )
            self._write_steady_flow_file(
                flow_path=flow_path,
                flow_title=naming["flow_title"],
                river=river,
                reach=reach,
                upstream_river_station=sections[0].river_station,
                downstream_river_station=sections[-1].river_station,
                flow_cms=flow_cms,
                profile_name=naming["profile_name"],
                friction_slope=friction_slope,
                flow_profiles=flow_profiles,
            )
            self._write_sdf_file(
                sdf_path=sdf_path,
                project_title=naming["project_title"],
                river=river,
                reach=reach,
                sections=sections,
                centerline_points=centerline_points,
                centerline_measures=centerline_measures,
            )
        self._write_plan_file(
            plan_path=plan_path,
            plan_title=naming["plan_title"],
            project_title=naming["project_title"],
        )
        self._write_project_file(
            project_path=project_path,
            project_title=naming["project_title"],
            geom_title=naming["geom_title"],
            flow_title=naming["flow_title"],
            plan_title=naming["plan_title"],
        )
        self.project_path = project_folder
        self.project_name = project_stem
        self.last_build = BuildResult(
            project_folder=str(project_folder),
            project_file=str(project_path),
            geometry_file=str(geometry_path),
            flow_file=str(flow_path),
            plan_file=str(plan_path),
            sdf_file=str(sdf_path),
            river=river,
            reach=reach,
            section_count=len(sections),
            downstream_friction_slope=friction_slope,
            flow_cms=flow_cms,
            skipped_source_stations=list(
                geometry_context.get("skipped_source_stations", [])
            ),
        )
        return self.last_build

    def _read_structure_table(self, structure_path: Path) -> pd.DataFrame:
        if structure_path.suffix.lower() == ".toml":
            with structure_path.open("rb") as handle:
                config = tomllib.load(handle)
            rows: list[dict[str, Any]] = []
            for section_name, section_config in config.items():
                if not isinstance(section_config, dict):
                    continue
                if not bool(section_config.get("is_present", False)):
                    continue
                entries = section_config.get("structures")
                if entries is None:
                    entries = [
                        {
                            key: value
                            for key, value in section_config.items()
                            if key != "is_present"
                        }
                    ]
                if not isinstance(entries, list):
                    raise ValueError(
                        f"Structure TOML section [{section_name}] must use "
                        f"[[{section_name}.structures]] entries."
                    )
                for entry in entries:
                    if not isinstance(entry, dict):
                        raise ValueError(
                            f"Structure TOML section [{section_name}] contains "
                            "a non-table structure entry."
                        )
                    row = dict(entry)
                    row.setdefault("structure_type", section_name)
                    rows.append(row)
            return self._normalize_structure_table(pd.DataFrame(rows))

        structure_df = pd.read_csv(structure_path, sep=None, engine="python")
        return self._normalize_structure_table(structure_df)

    @staticmethod
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

    def _normalize_structure_table(self, structure_df: pd.DataFrame) -> pd.DataFrame:
        if structure_df.empty:
            return structure_df

        structure_df = structure_df.copy()
        structure_df.columns = [str(column).strip() for column in structure_df.columns]
        source_columns = set(structure_df.columns)

        def is_blank(series: pd.Series) -> pd.Series:
            return series.isna() | series.astype(str).str.strip().eq("")

        def coalesce(target: str, *sources: str) -> None:
            if target in structure_df.columns:
                values = structure_df[target].copy()
            else:
                values = pd.Series(pd.NA, index=structure_df.index, dtype="object")
            for source in sources:
                if source not in structure_df.columns:
                    continue
                values = values.where(~is_blank(values), structure_df[source])
            structure_df[target] = values

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
        coalesce("culvert_mannings_n", "culvert_mannings")
        coalesce("culvert_bottom_n", "culvert_bottom_mannings")
        coalesce("culvert_chart_number", "culvert_chart")
        coalesce("opening_offset_from_left_bank", "opening_offset")
        coalesce("opening_station_method", "opening_strategy")
        coalesce("diameter", "Diameter", "min_span", "min_rise", "span_upstream", "rise_upstream")

        essentials_style = {
            "structure_id",
            "upstream_x1",
            "downstream_x1",
            "deck_max",
        }.issubset(source_columns)
        if essentials_style:
            if "deck_distance" in source_columns:
                mask = is_blank(structure_df["culvert_length"])
                structure_df.loc[mask, "culvert_length"] = structure_df.loc[
                    mask,
                    "deck_distance",
                ]
            if "deck_width" in source_columns:
                source_deck_width = structure_df["deck_width"].copy()
                mask = is_blank(structure_df["culvert_length"])
                structure_df.loc[mask, "culvert_length"] = source_deck_width.loc[mask]
                structure_df["deck_distance"] = source_deck_width.where(
                    ~is_blank(source_deck_width),
                    0.1,
                )
            if "min_span" in structure_df.columns:
                mask = is_blank(structure_df["culvert_length"])
                structure_df.loc[mask, "culvert_length"] = structure_df.loc[
                    mask,
                    "min_span",
                ]
            if "deck_weir_coefficient" in structure_df.columns:
                mask = is_blank(structure_df["deck_weir_coefficient"])
                structure_df.loc[mask, "deck_weir_coefficient"] = 1.4
            else:
                structure_df["deck_weir_coefficient"] = 1.4
            structure_df["deck_max_submerge"] = 0.98
            structure_df["use_bank_point_station"] = True
            if "preserve_endpoint_order" not in structure_df.columns:
                structure_df["preserve_endpoint_order"] = True

        for left, right in (
            ("upstream_point_1_x", "downstream_point_1_x"),
            ("upstream_point_1_y", "downstream_point_1_y"),
            ("upstream_point_2_x", "downstream_point_2_x"),
            ("upstream_point_2_y", "downstream_point_2_y"),
        ):
            if left in structure_df.columns and right in structure_df.columns:
                left_blank = is_blank(structure_df[left])
                right_blank = is_blank(structure_df[right])
                structure_df.loc[left_blank & ~right_blank, left] = structure_df.loc[
                    left_blank & ~right_blank,
                    right,
                ]
                structure_df.loc[right_blank & ~left_blank, right] = structure_df.loc[
                    right_blank & ~left_blank,
                    left,
                ]

        normalized_types = structure_df["structure_type"].apply(
            lambda value: self._normalize_structure_type(str(value or ""))
        )
        structure_df["structure_type"] = normalized_types

        def fill_from_sum(target: str, base: str, addend: str) -> None:
            if target not in structure_df.columns:
                structure_df[target] = pd.NA
            target_mask = is_blank(structure_df[target])
            if base not in structure_df.columns or addend not in structure_df.columns:
                return
            base_values = pd.to_numeric(structure_df[base], errors="coerce")
            addend_values = pd.to_numeric(structure_df[addend], errors="coerce")
            summed = base_values + addend_values
            valid_mask = target_mask & summed.notna()
            structure_df.loc[valid_mask, target] = summed.loc[valid_mask]

        def set_bridge_sum(target: str, base: str, addend: str) -> None:
            if target not in structure_df.columns:
                structure_df[target] = pd.NA
            if base not in structure_df.columns or addend not in structure_df.columns:
                return
            base_values = pd.to_numeric(structure_df[base], errors="coerce")
            addend_values = pd.to_numeric(structure_df[addend], errors="coerce")
            summed = base_values + addend_values
            valid_mask = normalized_types.eq("bridge_deck") & summed.notna()
            structure_df.loc[valid_mask, target] = summed.loc[valid_mask]

        for target in (
            "low_chord_upstream_left_bank",
            "low_chord_upstream_right_bank",
        ):
            coalesce(target, "upstream_invert_elevation")
            set_bridge_sum(target, "upstream_invert_elevation", "rise_upstream")
        for target in (
            "low_chord_downstream_left_bank",
            "low_chord_downstream_right_bank",
        ):
            coalesce(target, "downstream_invert_elevation")
            set_bridge_sum(target, "downstream_invert_elevation", "rise_downstream")

        for target in (
            "high_chord_upstream_left_bank",
            "high_chord_upstream_right_bank",
        ):
            coalesce(target, "upstream_elev", "deck_max")
            fill_from_sum(target, "upstream_invert_elevation", "rise_upstream")
        for target in (
            "high_chord_downstream_left_bank",
            "high_chord_downstream_right_bank",
        ):
            coalesce(target, "downstream_elev", "deck_max")
            fill_from_sum(target, "downstream_invert_elevation", "rise_downstream")

        coalesce("bridge_width", "culvert_length")
        return structure_df

    def _build_structures(
        self,
        structure_csv: Optional[str | Path],
        sections: list[SectionData],
        centerline_line: LineString,
    ) -> list[StructureData]:
        if structure_csv is None:
            return []

        structure_path = Path(structure_csv)
        if not structure_path.exists():
            raise FileNotFoundError(f"Structure table was not found: {structure_path}")

        structure_df = self._read_structure_table(structure_path)
        if structure_df.empty:
            return []
        structure_df.columns = [str(column).strip() for column in structure_df.columns]
        common_required_columns = {
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
        missing_columns = sorted(common_required_columns.difference(structure_df.columns))
        if missing_columns:
            raise ValueError(
                "Structure table is missing required columns: "
                f"{missing_columns}"
            )

        structures: list[StructureData] = []
        downstream_measure = float(sections[-1].centerline_measure)
        for idx, row in structure_df.iterrows():
            structure_type = self._normalize_structure_type(
                self._row_string(row, "structure_type", default="")
            )
            num_barrels = self._row_int(row, "num_barrels", default=1)
            if structure_type == "box":
                if "culvert_length" not in structure_df.columns:
                    raise ValueError(
                        "Box structure table is missing required column: "
                        "['culvert_length']"
                    )
                box_required_columns = {
                    "upstream_invert_elevation",
                    "downstream_invert_elevation",
                    "min_rise",
                    "min_span",
                }
                missing_box_columns = sorted(
                    box_required_columns.difference(structure_df.columns)
                )
                if missing_box_columns:
                    raise ValueError(
                        "Box structure table is missing required columns: "
                        f"{missing_box_columns}"
                    )
                culvert_shape_code = 2
                culvert_shape_name = "BOX"
                culvert_span = self._row_float(row, "min_span")
                culvert_rise = self._row_float(row, "min_rise")
                default_inlet_type = 8
                default_outlet_type = 1
            elif structure_type == "conspan_arch":
                if "culvert_length" not in structure_df.columns:
                    raise ValueError(
                        "ConSpan arch structure table is missing required column: "
                        "['culvert_length']"
                    )
                conspan_required_columns = {
                    "upstream_invert_elevation",
                    "downstream_invert_elevation",
                    "min_rise",
                    "min_span",
                }
                missing_conspan_columns = sorted(
                    conspan_required_columns.difference(structure_df.columns)
                )
                if missing_conspan_columns:
                    raise ValueError(
                        "ConSpan arch structure table is missing required columns: "
                        f"{missing_conspan_columns}"
                    )
                culvert_shape_code = 9
                culvert_shape_name = "BOX"
                culvert_span = self._row_float(row, "min_span")
                culvert_rise = self._row_float(row, "min_rise")
                default_inlet_type = 60
                default_outlet_type = 1
            elif structure_type in {"circular", "circle", "pipe", "round"}:
                if "culvert_length" not in structure_df.columns:
                    raise ValueError(
                        "Circular structure table is missing required column: "
                        "['culvert_length']"
                    )
                if not any(
                    column in structure_df.columns
                    for column in ("Diameter", "diameter", "min_span", "min_rise")
                ):
                    raise ValueError(
                        "Circular structure table must include a Diameter column."
                    )
                culvert_shape_code = 1
                culvert_shape_name = "CIRCULAR"
                culvert_rise = self._row_float_any(
                    row,
                    ("Diameter", "diameter", "min_span", "min_rise"),
                )
                culvert_span = culvert_rise
                default_inlet_type = 1
                default_outlet_type = 1
            elif structure_type == "bridge_deck":
                culvert_shape_code = 0
                culvert_shape_name = "NONE"
                culvert_span = 0.0
                culvert_rise = 0.0
                default_inlet_type = 0
                default_outlet_type = 0
            else:
                raise NotImplementedError(
                    "Only box/ConSpan/circular culverts and bridge_deck structures are "
                    "currently implemented. "
                    f"Received {structure_type!r} in {structure_path.name}."
                )

            upstream_points = [
                Point(
                    self._row_float(row, "upstream_point_1_x"),
                    self._row_float(row, "upstream_point_1_y"),
                ),
                Point(
                    self._row_float(row, "upstream_point_2_x"),
                    self._row_float(row, "upstream_point_2_y"),
                ),
            ]
            downstream_points = [
                Point(
                    self._row_float(row, "downstream_point_1_x"),
                    self._row_float(row, "downstream_point_1_y"),
                ),
                Point(
                    self._row_float(row, "downstream_point_2_x"),
                    self._row_float(row, "downstream_point_2_y"),
                ),
            ]
            upstream_midpoint = LineString(upstream_points).centroid
            downstream_midpoint = LineString(downstream_points).centroid
            upstream_target_measure = float(centerline_line.project(upstream_midpoint))
            downstream_target_measure = float(centerline_line.project(downstream_midpoint))

            nearest_upstream_section = min(
                sections,
                key=lambda section: abs(
                    section.centerline_measure - upstream_target_measure
                ),
            )
            nearest_downstream_section = min(
                sections,
                key=lambda section: abs(
                    section.centerline_measure - downstream_target_measure
                ),
            )
            preserve_endpoint_order = self._row_bool(
                row,
                "preserve_endpoint_order",
                default=False,
            )
            upstream_section = nearest_upstream_section
            downstream_section = nearest_downstream_section
            if (
                (
                    structure_type in {"circular", "circle", "pipe", "round"}
                    or not preserve_endpoint_order
                )
                and upstream_section.centerline_measure
                > downstream_section.centerline_measure
            ):
                upstream_section, downstream_section = downstream_section, upstream_section
            if (
                upstream_section is downstream_section
                or abs(
                    float(upstream_section.river_station)
                    - float(downstream_section.river_station)
                )
                <= 2.0
            ):
                upstream_section, downstream_section = (
                    self._select_structure_bracketing_sections(
                        sections=sections,
                        upstream_target_measure=upstream_target_measure,
                        downstream_target_measure=downstream_target_measure,
                    )
                )

            structure_measure = 0.5 * (
                upstream_section.centerline_measure
                + downstream_section.centerline_measure
            )
            river_station = float(
                HECRAS._round_half_up(downstream_measure - structure_measure)
            )
            lower_station = min(
                float(upstream_section.river_station),
                float(downstream_section.river_station),
            )
            upper_station = max(
                float(upstream_section.river_station),
                float(downstream_section.river_station),
            )
            if upper_station - lower_station <= 2.0:
                river_station = 0.5 * (upper_station + lower_station)
            else:
                river_station = min(
                    river_station,
                    upper_station - 1.0,
                )
                river_station = max(
                    river_station,
                    lower_station + 1.0,
                )
            if float(upstream_section.river_station) >= float(downstream_section.river_station):
                upstream_face_section = upstream_section
                downstream_face_section = downstream_section
            else:
                upstream_face_section = downstream_section
                downstream_face_section = upstream_section

            upstream_opening_start_station = self._resolve_opening_start_station(
                left_bank_station=float(upstream_face_section.left_bank_station),
                right_bank_station=float(upstream_face_section.right_bank_station),
                opening_span=culvert_span,
                opening_offset=self._row_float(
                    row,
                    "opening_offset_from_left_bank",
                    default=0.5,
                ),
            )
            downstream_opening_start_station = self._resolve_opening_start_station(
                left_bank_station=float(downstream_face_section.left_bank_station),
                right_bank_station=float(downstream_face_section.right_bank_station),
                opening_span=culvert_span,
                opening_offset=self._row_float(
                    row,
                    "opening_offset_from_left_bank",
                    default=0.5,
                ),
            )
            upstream_opening_station = upstream_opening_start_station + 0.5 * culvert_span
            downstream_opening_station = (
                downstream_opening_start_station + 0.5 * culvert_span
            )
            if self._row_bool(
                row,
                "use_lower_two_points_for_culvert_placement",
                default=False,
            ):
                upstream_opening_station = self._lower_two_point_center_station(
                    upstream_face_section,
                )
                downstream_opening_station = self._lower_two_point_center_station(
                    downstream_face_section,
                )
            opening_station_method = self._row_string(
                row,
                "opening_station_method",
                default="bank_offset",
            ).lower()
            if opening_station_method == "centerline_half_length":
                opening_station_method = "bank_offset"
            if opening_station_method not in {
                "bank_offset",
                "csv_override",
                "centerline_midpoint",
            }:
                raise ValueError(
                    "Unsupported opening_station_method "
                    f"{opening_station_method!r} in {structure_path.name}."
                )
            if opening_station_method == "centerline_midpoint":
                upstream_opening_station = self._opening_station_from_centerline_face(
                    section=upstream_face_section,
                    other_section=downstream_face_section,
                    centerline_line=centerline_line,
                    culvert_length=self._row_float_any(
                        row,
                        ("culvert_length", "bridge_width"),
                    ),
                )
                downstream_opening_station = self._opening_station_from_centerline_face(
                    section=downstream_face_section,
                    other_section=upstream_face_section,
                    centerline_line=centerline_line,
                    culvert_length=self._row_float_any(
                        row,
                        ("culvert_length", "bridge_width"),
                    ),
                )
            upstream_opening_station = self._row_float(
                row,
                "upstream_opening_station",
                default=upstream_opening_station,
            )
            downstream_opening_station = self._row_float(
                row,
                "downstream_opening_station",
                default=downstream_opening_station,
            )

            upstream_left_bank_elevation = self._elevation_at_station(
                upstream_face_section,
                float(upstream_face_section.left_bank_station),
            )
            upstream_right_bank_elevation = self._elevation_at_station(
                upstream_face_section,
                float(upstream_face_section.right_bank_station),
            )
            downstream_left_bank_elevation = self._elevation_at_station(
                downstream_face_section,
                float(downstream_face_section.left_bank_station),
            )
            downstream_right_bank_elevation = self._elevation_at_station(
                downstream_face_section,
                float(downstream_face_section.right_bank_station),
            )

            if structure_type in {"circular", "circle", "pipe", "round"}:
                use_bank_point_for_invert = self._row_bool(
                    row,
                    "use_bank_point_for_invert",
                    default=False,
                )
                if use_bank_point_for_invert:
                    upstream_invert = (
                        self._minimum_elevation_between_banks(upstream_face_section)
                        + culvert_rise
                    )
                    downstream_invert = (
                        self._minimum_elevation_between_banks(downstream_face_section)
                        + culvert_rise
                    )
                else:
                    try:
                        upstream_invert = self._row_float(
                            row,
                            "upstream_invert_elevation",
                        )
                    except ValueError:
                        upstream_invert = (
                            self._minimum_elevation_between_banks(upstream_face_section)
                            + culvert_rise
                        )
                    try:
                        downstream_invert = self._row_float(
                            row,
                            "downstream_invert_elevation",
                        )
                    except ValueError:
                        downstream_invert = (
                            self._minimum_elevation_between_banks(downstream_face_section)
                            + culvert_rise
                        )
            else:
                upstream_invert = self._row_float(
                    row,
                    "upstream_invert_elevation",
                    default=0.0,
                )
                downstream_invert = self._row_float(
                    row,
                    "downstream_invert_elevation",
                    default=0.0,
                )

            upstream_deck_stations = [
                float(upstream_face_section.left_bank_station),
                float(upstream_face_section.right_bank_station),
            ]
            downstream_deck_stations = [
                float(downstream_face_section.left_bank_station),
                float(downstream_face_section.right_bank_station),
            ]
            upstream_deck_elevations = [
                upstream_left_bank_elevation,
                upstream_right_bank_elevation,
            ]
            downstream_deck_elevations = [
                downstream_left_bank_elevation,
                downstream_right_bank_elevation,
            ]
            low_chord = self._structure_low_chord_below_sections(
                upstream_section,
                downstream_section,
            )
            upstream_low_chord = low_chord
            downstream_low_chord = low_chord
            if structure_type == "bridge_deck":
                use_bank_point_station = self._row_bool(
                    row,
                    "use_bank_point_station",
                    default=False,
                )
                if not use_bank_point_station:
                    upstream_deck_stations = [
                        self._row_float(row, "upstream_deck_left_station"),
                        self._row_float(row, "upstream_deck_right_station"),
                    ]
                    downstream_deck_stations = [
                        self._row_float(row, "downstream_deck_left_station"),
                        self._row_float(row, "downstream_deck_right_station"),
                    ]
                upstream_deck_elevations = [
                    self._row_float(row, "high_chord_upstream_left_bank"),
                    self._row_float(row, "high_chord_upstream_right_bank"),
                ]
                downstream_deck_elevations = [
                    self._row_float(row, "high_chord_downstream_left_bank"),
                    self._row_float(row, "high_chord_downstream_right_bank"),
                ]
                upstream_low_chord = self._row_float(
                    row,
                    "low_chord_upstream_left_bank",
                )
                upstream_right_low_chord = self._row_float(
                    row,
                    "low_chord_upstream_right_bank",
                )
                downstream_low_chord = self._row_float(
                    row,
                    "low_chord_downstream_left_bank",
                )
                downstream_right_low_chord = self._row_float(
                    row,
                    "low_chord_downstream_right_bank",
                )
            else:
                use_bank_point_for_elevation = self._row_bool(
                    row,
                    "use_bank_point_for_elevation",
                    default=True,
                )
                if not use_bank_point_for_elevation:
                    upstream_deck_elevations = [
                        self._row_float(row, "high_chord_upstream_left_bank"),
                        self._row_float(row, "high_chord_upstream_right_bank"),
                    ]
                    downstream_deck_elevations = [
                        self._row_float(row, "high_chord_downstream_left_bank"),
                        self._row_float(row, "high_chord_downstream_right_bank"),
                    ]
                upstream_right_low_chord = upstream_low_chord
                downstream_right_low_chord = downstream_low_chord
            structure_name = self._row_string(
                row,
                "structure_name",
                default=f"Structure_{idx + 1}",
            )
            structure_name = re.sub(r"[,/\\]+", "_", structure_name).strip() or f"Structure_{idx + 1}"

            structures.append(
                StructureData(
                    structure_type=structure_type,
                    name=structure_name,
                    river_station=river_station,
                    culvert_shape_code=culvert_shape_code,
                    culvert_shape_name=culvert_shape_name,
                    culvert_span=culvert_span,
                    culvert_rise=culvert_rise,
                    culvert_length=self._row_float_any(
                        row,
                        ("culvert_length", "bridge_width"),
                    ),
                    upstream_invert=upstream_invert,
                    downstream_invert=downstream_invert,
                    upstream_opening_station=upstream_opening_station,
                    downstream_opening_station=downstream_opening_station,
                    deck_distance=self._row_float(row, "deck_distance", default=0.2),
                    deck_width=self._row_float(row, "deck_width", default=0.0),
                    deck_weir_coefficient=self._row_float(
                        row,
                        "deck_weir_coefficient",
                        default=1.4,
                    ),
                    deck_skew=self._row_float(row, "deck_skew", default=0.0),
                    deck_max_submerge=self._row_float(
                        row,
                        "deck_max_submerge",
                        default=0.98,
                    ),
                    culvert_mannings_n=self._row_float(
                        row,
                        "culvert_mannings_n",
                        default=0.02,
                    ),
                    culvert_bottom_n=self._row_float(
                        row,
                        "culvert_bottom_n",
                        default=0.025,
                    ),
                    entrance_loss=self._row_float(row, "entrance_loss", default=0.5),
                    exit_loss=self._row_float(row, "exit_loss", default=1.0),
                    inlet_type=self._row_int(
                        row,
                        "inlet_type",
                        default=default_inlet_type,
                    ),
                    outlet_type=self._row_int(
                        row,
                        "outlet_type",
                        default=default_outlet_type,
                    ),
                    culvert_chart_number=self._row_int(
                        row,
                        "culvert_chart_number",
                        default=0,
                    ),
                    num_barrels=num_barrels,
                    barrel_center_spacing=self._row_float_any(
                        row,
                        ("pipe_sepration", "pipe_separation", "barrel_center_spacing"),
                        default=culvert_span * 1.5,
                    ),
                    upstream_deck_stations=upstream_deck_stations,
                    upstream_deck_elevations=upstream_deck_elevations,
                    upstream_low_chords=[upstream_low_chord, upstream_right_low_chord],
                    downstream_deck_stations=downstream_deck_stations,
                    downstream_deck_elevations=downstream_deck_elevations,
                    downstream_low_chords=[
                        downstream_low_chord,
                        downstream_right_low_chord,
                    ],
                    upstream_section_station=float(upstream_section.river_station),
                    downstream_section_station=float(downstream_section.river_station),
                    htab_hwmax=self._row_float(
                        row,
                        "htab_hwmax",
                        default=max(
                            upstream_deck_elevations
                            + downstream_deck_elevations
                            + [
                                upstream_low_chord,
                                upstream_right_low_chord,
                                downstream_low_chord,
                                downstream_right_low_chord,
                            ]
                        )
                        + 0.09,
                    ),
                    htab_max_flow=self._row_float(
                        row,
                        "htab_max_flow",
                        default=50.0,
                    ),
                )
            )

        return structures

    @staticmethod
    def _select_structure_bracketing_sections(
        sections: list[SectionData],
        upstream_target_measure: float,
        downstream_target_measure: float,
    ) -> tuple[SectionData, SectionData]:
        if len(sections) < 2:
            raise ValueError("At least two cross sections are required for structures.")

        ordered_sections = sorted(
            sections,
            key=lambda section: float(section.centerline_measure),
        )
        structure_measure = 0.5 * (
            float(upstream_target_measure) + float(downstream_target_measure)
        )
        first_measure = float(ordered_sections[0].centerline_measure)
        last_measure = float(ordered_sections[-1].centerline_measure)
        if structure_measure <= first_measure:
            return ordered_sections[0], ordered_sections[1]
        if structure_measure >= last_measure:
            return ordered_sections[-2], ordered_sections[-1]

        for idx, downstream_section in enumerate(ordered_sections[1:], start=1):
            upstream_section = ordered_sections[idx - 1]
            upstream_measure = float(upstream_section.centerline_measure)
            downstream_measure = float(downstream_section.centerline_measure)
            if upstream_measure <= structure_measure <= downstream_measure:
                return upstream_section, downstream_section

        return ordered_sections[-2], ordered_sections[-1]

    @staticmethod
    def _opening_station_from_centerline_face(
        section: SectionData,
        other_section: SectionData,
        centerline_line: LineString,
        culvert_length: float,
    ) -> float:
        midpoint_measure = 0.5 * (
            float(section.centerline_measure) + float(other_section.centerline_measure)
        )
        half_length = 0.5 * float(culvert_length)
        face_measures = [
            max(0.0, midpoint_measure - half_length),
            min(float(centerline_line.length), midpoint_measure + half_length),
        ]
        face_measure = min(
            face_measures,
            key=lambda measure: abs(measure - float(section.centerline_measure)),
        )
        face_point = centerline_line.interpolate(face_measure)
        return float(section.line.project(face_point))

    @staticmethod
    def _structure_low_chord_below_sections(
        upstream_section: SectionData,
        downstream_section: SectionData,
        clearance: float = 10.0,
    ) -> float:
        section_min = min(
            float(upstream_section.station_elevation["Elevation"].min()),
            float(downstream_section.station_elevation["Elevation"].min()),
        )
        return section_min - float(clearance)

    @staticmethod
    def _max_bank_elevation(section: SectionData) -> float:
        left_elevation = HECRAS._levee_elevation_at_station(
            section.station_elevation,
            float(section.left_bank_station),
        )
        right_elevation = HECRAS._levee_elevation_at_station(
            section.station_elevation,
            float(section.right_bank_station),
        )
        return max(left_elevation, right_elevation)

    @staticmethod
    def _elevation_at_station(section: SectionData, station: float) -> float:
        return HECRAS._levee_elevation_at_station(
            section.station_elevation,
            float(station),
        )

    @staticmethod
    def _profile_stations_between_banks(section: SectionData) -> list[float]:
        station_values = section.station_elevation["Station"].to_numpy(dtype=float)
        left_station = float(section.left_bank_station)
        right_station = float(section.right_bank_station)
        internal = [
            float(station)
            for station in station_values
            if left_station < float(station) < right_station
        ]
        if internal:
            return internal
        return [0.5 * (left_station + right_station)]

    @staticmethod
    def _minimum_elevation_between_banks(section: SectionData) -> float:
        stations = section.station_elevation["Station"].to_numpy(dtype=float)
        elevations = section.station_elevation["Elevation"].to_numpy(dtype=float)
        left_station = float(section.left_bank_station)
        right_station = float(section.right_bank_station)
        values = [
            float(elevation)
            for station, elevation in zip(stations, elevations)
            if left_station <= float(station) <= right_station
        ]
        if not values:
            values = [
                HECRAS._elevation_at_station(section, left_station),
                HECRAS._elevation_at_station(section, right_station),
            ]
        return min(values)

    @staticmethod
    def _lower_two_point_center_station(section: SectionData) -> float:
        station_elevation = section.station_elevation.copy()
        left_station = float(section.left_bank_station)
        right_station = float(section.right_bank_station)
        between_banks = station_elevation[
            (station_elevation["Station"].astype(float) >= left_station)
            & (station_elevation["Station"].astype(float) <= right_station)
        ].copy()
        if len(between_banks) < 2:
            return 0.5 * (left_station + right_station)
        between_banks["Station"] = between_banks["Station"].astype(float)
        between_banks["Elevation"] = between_banks["Elevation"].astype(float)
        lowest_two = between_banks.sort_values(
            by=["Elevation", "Station"],
            ascending=[True, True],
        ).head(2)
        return float(lowest_two["Station"].mean())

    @staticmethod
    def _barrel_center_station_from_profile(
        profile_stations: list[float],
        section: SectionData,
    ) -> float:
        if len(profile_stations) >= 2:
            sorted_stations = sorted(profile_stations)
            center_index = 0.5 * (len(sorted_stations) - 1)
            lower_index = int(math.floor(center_index))
            upper_index = int(math.ceil(center_index))
            return 0.5 * (
                float(sorted_stations[lower_index])
                + float(sorted_stations[upper_index])
            )
        if profile_stations:
            return float(profile_stations[0])
        return 0.5 * (
            float(section.left_bank_station) + float(section.right_bank_station)
        )

    @staticmethod
    def _row_float(
        row: pd.Series,
        column: str,
        default: Optional[float] = None,
    ) -> float:
        value = row.get(column, default)
        if isinstance(value, str):
            value = value.strip()
        if value in ("", None) or pd.isna(value):
            if default is None:
                raise ValueError(f"Missing required numeric value for {column!r}")
            return float(default)
        return float(value)

    @staticmethod
    def _row_float_any(
        row: pd.Series,
        columns: tuple[str, ...],
        default: Optional[float] = None,
    ) -> float:
        for column in columns:
            if column in row.index:
                value = row.get(column)
                if isinstance(value, str):
                    value = value.strip()
                if value in ("", None) or pd.isna(value):
                    continue
                return float(value)
        if default is None:
            raise ValueError(
                "Missing required numeric value for one of "
                f"{', '.join(repr(column) for column in columns)}"
            )
        return float(default)

    @staticmethod
    def _row_int(
        row: pd.Series,
        column: str,
        default: int,
    ) -> int:
        return int(round(HECRAS._row_float(row, column, default=float(default))))

    @staticmethod
    def _row_string(
        row: pd.Series,
        column: str,
        default: str,
    ) -> str:
        value = row.get(column, default)
        if value is None or pd.isna(value):
            return default
        text = str(value).strip()
        return text if text else default

    @staticmethod
    def _row_bool(
        row: pd.Series,
        column: str,
        default: bool = False,
    ) -> bool:
        value = row.get(column, default)
        if isinstance(value, str):
            text = value.strip().lower()
            if text in {"1", "true", "yes", "y"}:
                return True
            if text in {"0", "false", "no", "n"}:
                return False
        if value is None or pd.isna(value):
            return default
        return bool(value)

    @staticmethod
    def _resolve_opening_start_station(
        left_bank_station: float,
        right_bank_station: float,
        opening_span: float,
        opening_offset: float,
    ) -> float:
        channel_width = max(float(right_bank_station) - float(left_bank_station), 0.0)
        clear_width = max(channel_width - float(opening_span), 0.0)
        offset_ratio = min(max(float(opening_offset), 0.0), 1.0)
        return float(left_bank_station) + clear_width * offset_ratio

    @staticmethod
    def _deck_low_chord_below_section_min(
        section: SectionData,
        clearance: float = 0.1,
    ) -> float:
        return float(section.station_elevation["Elevation"].min()) - float(clearance)

    @staticmethod
    def _read_cross_sections(cross_section_csv: str | Path) -> pd.DataFrame:
        csv_path = Path(cross_section_csv)
        df = pd.read_csv(csv_path, sep=None, engine="python")

        required = {"River", "Reach", "Station", "X", "Y", "Z"}
        missing = required.difference(df.columns)
        if missing:
            raise ValueError(
                f"Cross section CSV is missing required columns: {sorted(missing)}"
            )

        rivers = df["River"].dropna().unique()
        reaches = df["Reach"].dropna().unique()
        if len(rivers) != 1 or len(reaches) != 1:
            raise ValueError(
                "This builder currently supports exactly one river and one reach."
            )

        duplicate_profile_point_mask = df.duplicated(
            subset=["River", "Reach", "Station", "X", "Y", "Z"],
            keep="first",
        )
        if duplicate_profile_point_mask.any():
            duplicate_count = int(duplicate_profile_point_mask.sum())
            logger.info(
                "Removed %s duplicate cross-section row(s) with identical "
                "River/Reach/Station/X/Y/Z values from %s.",
                duplicate_count,
                csv_path.name,
            )
            df = df.loc[~duplicate_profile_point_mask].copy()

        return df

    def _select_hydrology_point(
        self,
        hydrology_kmz: str | Path,
        centerline_line: LineString,
        buffer_distance: float,
    ) -> Optional[HydrologyPointSelection]:
        points_gdf = self._load_hydrology_points(
            hydrology_kmz=hydrology_kmz,
            target_crs=self.source_crs,
        )
        if points_gdf.empty:
            return None

        points_gdf = points_gdf.copy()
        points_gdf["distance_to_river"] = points_gdf.geometry.distance(
            centerline_line
        )
        candidate_gdf = points_gdf[
            points_gdf["distance_to_river"] <= float(buffer_distance)
        ].copy()
        if candidate_gdf.empty:
            return None

        candidate_gdf.sort_values(
            by=["distance_to_river", "point_name"],
            ascending=[True, True],
            inplace=True,
        )
        selected = candidate_gdf.iloc[0]
        q_values = {
            return_period: float(selected[return_period])
            for return_period in [
                "Q5",
                "Q10",
                "Q25",
                "Q50",
                "Q100",
                "Q500",
                "Q1000",
            ]
            if return_period in selected.index and pd.notna(selected[return_period])
        }
        return HydrologyPointSelection(
            point_name=str(selected.get("point_name", "")),
            point_id=str(selected.get("point_id", "")),
            buffer_distance=float(buffer_distance),
            candidate_count=int(len(candidate_gdf)),
            distance_to_river=float(selected["distance_to_river"]),
            x=float(selected.geometry.x),
            y=float(selected.geometry.y),
            q_values=q_values,
        )

    @staticmethod
    def _load_hydrology_points(
        hydrology_kmz: str | Path,
        target_crs: Optional[str] = None,
    ) -> gpd.GeoDataFrame:
        kmz_path = Path(hydrology_kmz)
        if not kmz_path.exists():
            raise FileNotFoundError(f"Hydrology KMZ not found: {kmz_path}")

        if kmz_path.suffix.lower() == ".kmz":
            with zipfile.ZipFile(kmz_path) as zf:
                kml_members = [
                    name for name in zf.namelist() if name.lower().endswith(".kml")
                ]
                if not kml_members:
                    raise ValueError(f"No KML file was found inside {kmz_path.name}")
                xml_bytes = zf.read(kml_members[0])
        else:
            xml_bytes = kmz_path.read_bytes()

        root = ET.fromstring(xml_bytes)
        ns = {"kml": "http://www.opengis.net/kml/2.2"}
        rows: list[dict[str, Any]] = []
        for index, placemark in enumerate(root.findall(".//kml:Placemark", ns)):
            point_node = placemark.find(".//kml:Point", ns)
            if point_node is None:
                continue

            coordinates_node = point_node.find("kml:coordinates", ns)
            if coordinates_node is None or not coordinates_node.text:
                continue
            coordinate_values = coordinates_node.text.strip().split(",")
            if len(coordinate_values) < 2:
                continue

            simple_data = {
                node.attrib.get("name", ""): (node.text or "").strip()
                for node in placemark.findall(".//kml:SimpleData", ns)
            }
            point_name = (
                simple_data.get("NOKTA_ADI")
                or simple_data.get("Yerlesim_Ad")
                or simple_data.get("Yrlesm_Ad")
                or f"Point_{index + 1}"
            )
            point_id = (
                simple_data.get("NOKTA_ADI")
                or simple_data.get("Nokta_Adi")
                or f"Point_{index + 1}"
            )
            row = {
                **simple_data,
                "point_name": str(point_name),
                "point_id": str(point_id),
                "geometry": Point(
                    float(coordinate_values[0]),
                    float(coordinate_values[1]),
                ),
            }
            rows.append(row)

        gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")
        for column in ["Q2", "Q5", "Q10", "Q25", "Q50", "Q100", "Q500", "Q1000"]:
            if column in gdf.columns:
                gdf[column] = pd.to_numeric(gdf[column], errors="coerce")

        if target_crs and str(target_crs).upper() != "UNKNOWN":
            gdf = gdf.to_crs(target_crs)
        return gdf

    def _evaluate_steady_outflow(
        self,
        plan_hdf_path: Path,
        sections: list[SectionData],
        profile_name: str,
        tolerance_m: float,
    ) -> dict[str, Any]:
        wse_df = HdfResultsPlan.get_steady_wse(
            plan_hdf_path,
            profile_name=profile_name,
        )
        overflow_sections: list[str] = []
        max_bank_excess = 0.0
        for _, row in wse_df.iterrows():
            station_value = self._station_to_float(row["Station"])
            section = min(
                sections,
                key=lambda current: abs(current.river_station - station_value),
            )
            wse = float(row["WSE"])
            left_info = self._evaluate_overbank_side(
                section=section,
                side="left",
                wse=wse,
                tolerance_m=tolerance_m,
            )
            right_info = self._evaluate_overbank_side(
                section=section,
                side="right",
                wse=wse,
                tolerance_m=tolerance_m,
            )

            local_max_excess = max(left_info["excess_m"], right_info["excess_m"])
            if local_max_excess > max_bank_excess:
                max_bank_excess = local_max_excess

            section_label = self._format_river_station(section.river_station)
            if left_info["overflow"]:
                overflow_sections.append(f"{section_label}:L")
            if right_info["overflow"]:
                overflow_sections.append(f"{section_label}:R")

        out_of_bank = bool(overflow_sections)
        overflow_xs_count = len({item.split(":")[0] for item in overflow_sections})
        if out_of_bank:
            note = (
                "Computed water surface inundated ground beyond the bank "
                f"stations at {overflow_xs_count} cross section(s)."
            )
        else:
            note = (
                "Computed water surface did not inundate overbank ground beyond "
                "either bank station."
            )

        return {
            "out_of_bank": out_of_bank,
            "max_bank_excess_m": (
                float(max_bank_excess) if out_of_bank else 0.0
            ),
            "overflow_sections": overflow_sections,
            "note": note,
        }

    def _evaluate_overbank_side(
        self,
        section: SectionData,
        side: str,
        wse: float,
        tolerance_m: float,
        station_tol: float = 1e-6,
    ) -> dict[str, Any]:
        if side not in {"left", "right"}:
            raise ValueError("side must be 'left' or 'right'")

        station_values = section.station_elevation["Station"].to_numpy(dtype=float)
        elevation_values = section.station_elevation["Elevation"].to_numpy(dtype=float)
        if side == "left":
            bank_station = float(section.left_bank_station)
            outside_mask = station_values < bank_station - station_tol
        else:
            bank_station = float(section.right_bank_station)
            outside_mask = station_values > bank_station + station_tol

        levee_elevation = self._levee_elevation_at_station(
            section.station_elevation,
            bank_station,
        )
        levee_excess = float(wse - levee_elevation)
        if levee_excess <= float(tolerance_m):
            return {
                "overflow": False,
                "excess_m": 0.0,
                "control_elevation_m": float(levee_elevation),
            }

        if not np.any(outside_mask):
            return {
                "overflow": False,
                "excess_m": 0.0,
                "control_elevation_m": float(levee_elevation),
            }

        side_elevations = elevation_values[outside_mask]
        side_min_elevation = float(np.min(side_elevations))
        control_elevation = max(float(levee_elevation), side_min_elevation)
        excess_m = float(wse - control_elevation)
        return {
            "overflow": excess_m > float(tolerance_m),
            "excess_m": max(excess_m, 0.0),
            "control_elevation_m": float(control_elevation),
        }

    def _build_flow_screening_message(
        self,
        selection: HydrologyPointSelection,
        max_safe_run: Optional[FlowRunResult],
        final_model_run: Optional[FlowRunResult],
        final_model_reason: Optional[str],
        buffer_distance: float,
        screening_stopped_early: bool,
        all_flows_in_single_plan: bool = False,
    ) -> str:
        point_text = (
            f"Selected hydrology point {selection.point_name} "
            f"({selection.point_id}) at "
            f"{self._format_number(selection.distance_to_river, decimals=2)} m "
            f"from the river line using a "
            f"{self._format_number(buffer_distance, decimals=2)} m buffer."
        )
        if max_safe_run is not None:
            stop_text = (
                " Screening stopped after the first in-bank run because all "
                "lower discharges are assumed safe."
                if screening_stopped_early
                else ""
            )
            single_plan_text = (
                " All tested flows were computed as profiles in one plan."
                if all_flows_in_single_plan
                else ""
            )
            return (
                f"{point_text} Maximum safe flow was {max_safe_run.return_period} "
                f"= {self._format_number(max_safe_run.discharge_cms, decimals=3)} "
                f"cms.{stop_text}{single_plan_text}"
            )
        if final_model_run is not None and final_model_reason:
            if all_flows_in_single_plan:
                return (
                    f"{point_text} No tested flow stayed in-bank. "
                    "The files written in code_generated contain all tested "
                    "flows as profiles in one plan."
                )
            return (
                f"{point_text} No tested flow stayed in-bank. "
                "The files written in code_generated use "
                f"{final_model_run.return_period} = "
                f"{self._format_number(final_model_run.discharge_cms, decimals=3)} "
                "cms as the lowest successful review case."
            )
        return f"{point_text} No successful HEC-RAS run was produced."

    @staticmethod
    def _write_flow_screening_csv(
        report_csv: Path,
        run_results: list[FlowRunResult],
    ) -> None:
        rows = []
        for result in run_results:
            rows.append(
                {
                    "return_period": result.return_period,
                    "discharge_cms": result.discharge_cms,
                    "compute_success": result.compute_success,
                    "solution": result.solution,
                    "out_of_bank": result.out_of_bank,
                    "max_bank_excess_m": result.max_bank_excess_m,
                    "overflow_sections": ",".join(result.overflow_sections),
                    "run_folder": result.run_folder,
                    "project_file": result.project_file,
                    "plan_hdf_file": result.plan_hdf_file,
                    "note": result.note,
                }
            )
        pd.DataFrame(rows).to_csv(report_csv, index=False)

    def _write_flow_screening_text_report(
        self,
        report_txt: Path,
        message: str,
        buffer_distance: float,
        selection: Optional[HydrologyPointSelection],
        run_results: list[FlowRunResult],
        max_safe_run: Optional[FlowRunResult],
        final_model_run: Optional[FlowRunResult],
    ) -> None:
        lines = [
            "TURFM Flow Screening Report\n",
            f"Generated: {self._hecras_timestamp()}\n",
            "\n",
            f"{message}\n",
            "\n",
        ]
        if selection is not None:
            lines.extend(
                [
                    "Selected hydrology point\n",
                    (
                        f"- Name: {selection.point_name} "
                        f"({selection.point_id})\n"
                    ),
                    (
                        "- River distance: "
                        f"{self._format_number(selection.distance_to_river, 2)} m\n"
                    ),
                    (
                        "- Buffer distance: "
                        f"{self._format_number(buffer_distance, 2)} m\n"
                    ),
                    f"- Candidate points in buffer: {selection.candidate_count}\n",
                    "\n",
                ]
            )

        if run_results:
            lines.append("Flow runs\n")
            for result in run_results:
                discharge_text = (
                    "NA"
                    if not np.isfinite(result.discharge_cms)
                    else self._format_number(result.discharge_cms, decimals=3)
                )
                lines.append(
                    (
                        f"- {result.return_period}: {discharge_text} cms | "
                        f"compute_success={result.compute_success} | "
                        f"out_of_bank={result.out_of_bank} | "
                        f"solution={result.solution or 'NA'}\n"
                    )
                )
                if result.overflow_sections:
                    lines.append(
                        "  Overflow sections: "
                        f"{', '.join(result.overflow_sections)}\n"
                    )
                if result.note:
                    lines.append(f"  Note: {result.note}\n")
            lines.append("\n")

        if max_safe_run is not None:
            lines.append(
                "Maximum safe flow\n"
                f"- {max_safe_run.return_period}: "
                f"{self._format_number(max_safe_run.discharge_cms, 3)} cms\n"
            )
        elif final_model_run is not None:
            lines.append(
                "Maximum safe flow\n"
                "- None of the tested flows stayed within bank lines.\n"
            )
            lines.append(
                "Final model in code_generated\n"
                f"- {final_model_run.return_period}: "
                f"{self._format_number(final_model_run.discharge_cms, 3)} cms\n"
            )

        report_txt.write_text("".join(lines), encoding="utf-8")

    @staticmethod
    def _station_to_float(value: Any) -> float:
        if isinstance(value, (int, float, np.integer, np.floating)):
            return float(value)
        return float(str(value).strip())

    @staticmethod
    def _normalize_river_name(value: Any) -> str:
        return HECRAS._hecras_identifier(value, default="RIVER")

    @staticmethod
    def _normalize_reach_name(
        value: Any,
        river_name: str,
        raw_river_name: Optional[str] = None,
    ) -> str:
        return HECRAS._hecras_identifier(value, default=river_name)

    @staticmethod
    def _hecras_identifier(value: Any, default: str = "RIVER", max_length: int = 16) -> str:
        text = re.sub(r"\s+", "-", str(value or "").strip())
        text = text.strip("-_")
        if not text:
            text = str(default or "RIVER").strip() or "RIVER"
        if len(text) <= max_length:
            return text

        chunks = [chunk for chunk in re.split(r"[-_\s]+", text) if chunk]
        for chunk_count in range(1, len(chunks) + 1):
            candidate = "-".join(chunks[-chunk_count:])
            if len(candidate) <= max_length:
                return candidate

        compact = re.sub(r"[^0-9A-Za-z]+", "", text)
        return (compact[-max_length:] or text[-max_length:] or str(default))[:max_length]

    @staticmethod
    def _normalize_plan_number(plan_number: str | int) -> str:
        return f"{int(plan_number):02d}"

    @staticmethod
    def _flow_run_folder_name(return_period: str, discharge_cms: float) -> str:
        discharge_text = (
            f"{float(discharge_cms):.3f}".replace(".", "p").replace("-", "neg")
        )
        return f"{return_period}_{discharge_text}cms"

    def _build_section_inputs(
        self,
        cross_section_df: pd.DataFrame,
        bank_lines_shp: str | Path,
    ) -> tuple[list[dict[str, Any]], LineString, LineString]:
        prepared_bank_lines = self._prepare_dtm_bank_lines_for_reach(
            cross_section_df=cross_section_df,
            bank_lines_shp=bank_lines_shp,
        )
        if prepared_bank_lines is not None:
            return self._build_section_inputs_from_selected_bank_lines(
                cross_section_df,
                prepared_bank_lines,
            )

        bank_lines = self._load_bank_lines(bank_lines_shp)
        if len(bank_lines) < 2:
            raise ValueError("Bank line shapefile must contain at least two lines.")

        bank_lines = self._repair_bank_openings_if_needed(
            cross_section_df=cross_section_df,
            bank_lines=bank_lines,
        )

        grouped_bank_lines = self._group_bank_lines_by_connectivity(bank_lines)
        return self._build_section_inputs_from_selected_bank_lines(
            cross_section_df,
            grouped_bank_lines,
        )

    def _repair_bank_openings_if_needed(
        self,
        cross_section_df: pd.DataFrame,
        bank_lines: list[LineString],
        sample_count: int = 5,
        width_factor: float = 3.0,
    ) -> list[LineString]:
        sampled_widths, average_width = self._sample_bank_widths(
            cross_section_df=cross_section_df,
            bank_lines=bank_lines,
            sample_count=sample_count,
        )
        if average_width <= 0.0:
            return bank_lines

        opening_pairs = self._find_bank_opening_pairs(
            bank_lines=bank_lines,
            average_width=average_width,
            width_factor=width_factor,
        )
        if len(opening_pairs) <= 2:
            return bank_lines

        repaired_bank_lines = list(bank_lines)
        repaired = self._repair_single_bank_opening(
            cross_section_df=cross_section_df,
            bank_lines=repaired_bank_lines,
            average_width=average_width,
            width_factor=width_factor,
        )
        if repaired is None:
            logger.warning(
                "Detected %s bank openings larger than exact joins, but could not "
                "build an in-memory repair.",
                len(opening_pairs),
            )
            return bank_lines

        repaired_opening_pairs = self._find_bank_opening_pairs(
            bank_lines=repaired,
            average_width=average_width,
            width_factor=width_factor,
        )
        logger.info(
            "Bank opening repair applied: sampled widths=%s | avg_width=%.3f m | "
            "openings_before=%s | openings_after=%s",
            [round(value, 3) for value in sampled_widths],
            average_width,
            len(opening_pairs),
            len(repaired_opening_pairs),
        )
        return repaired

    def _build_section_inputs_from_selected_bank_lines(
        self,
        cross_section_df: pd.DataFrame,
        bank_lines: list[LineString],
    ) -> tuple[list[dict[str, Any]], LineString, LineString]:

        section_inputs: list[dict[str, Any]] = []
        for source_station, group in cross_section_df.groupby("Station", sort=True):
            xs_points = list(zip(group["X"], group["Y"]))
            zs = group["Z"].astype(float).tolist()
            if len(xs_points) < 2:
                continue

            line = LineString(xs_points)
            if line.length <= 0:
                continue

            bank_entries = []
            for bank_index, bank_line in enumerate(bank_lines[:2]):
                bank_point = self._line_to_bank_point(line, bank_line)
                bank_station = self._project_distance(line, bank_point)
                bank_entries.append(
                    {
                        "bank_index": bank_index,
                        "point": bank_point,
                        "station": bank_station,
                        "line": bank_line,
                    }
                )

            midpoint = Point(
                (bank_entries[0]["point"].x + bank_entries[1]["point"].x) / 2.0,
                (bank_entries[0]["point"].y + bank_entries[1]["point"].y) / 2.0,
            )

            section_inputs.append(
                {
                    "source_station": int(source_station),
                    "points": xs_points,
                    "z_values": zs,
                    "bank_entries": bank_entries,
                    "channel_point": midpoint,
                    "min_z": float(np.min(zs)),
                }
            )

        section_inputs.sort(key=lambda item: item["source_station"])
        self._orient_sections(section_inputs)
        self._ensure_upstream_to_downstream_section_order(section_inputs)
        self._assign_bank_sides(section_inputs)
        left_bank_line = self._orient_bank_line(section_inputs, side_key="left")
        right_bank_line = self._orient_bank_line(section_inputs, side_key="right")
        return section_inputs, left_bank_line, right_bank_line

    @staticmethod
    def _ensure_upstream_to_downstream_section_order(
        section_inputs: list[dict[str, Any]],
        elevation_tolerance: float = 0.25,
    ) -> None:
        if len(section_inputs) < 2:
            return

        first_elevation = float(section_inputs[0]["min_z"])
        last_elevation = float(section_inputs[-1]["min_z"])
        if first_elevation + float(elevation_tolerance) < last_elevation:
            logger.info(
                "Cross-section order appears downstream-to-upstream "
                "(first min Z %.3f < last min Z %.3f); reversing in memory for "
                "HEC-RAS upstream-to-downstream reach direction.",
                first_elevation,
                last_elevation,
            )
            section_inputs.reverse()

    def _sample_bank_widths(
        self,
        cross_section_df: pd.DataFrame,
        bank_lines: list[LineString],
        sample_count: int = 5,
    ) -> tuple[list[float], float]:
        station_values = sorted(
            int(value) for value in cross_section_df["Station"].dropna().unique()
        )
        if not station_values:
            return [], 0.0

        if len(station_values) <= sample_count:
            sample_stations = station_values
        else:
            sample_stations = []
            for idx in range(sample_count):
                fraction = idx / max(sample_count - 1, 1)
                station_index = round(fraction * (len(station_values) - 1))
                sample_stations.append(station_values[station_index])
            sample_stations = list(dict.fromkeys(sample_stations))

        widths: list[float] = []
        for station in sample_stations:
            station_df = cross_section_df[cross_section_df["Station"] == station]
            if len(station_df) < 2:
                continue
            xs_line = LineString(list(zip(station_df["X"], station_df["Y"])))
            intersections = self._cross_section_bank_intersections(xs_line, bank_lines)
            if len(intersections) < 2:
                continue
            min_distance = min(
                left_point.distance(right_point)
                for left_idx, (_, left_point) in enumerate(intersections)
                for right_point in [point for _, point in intersections[left_idx + 1:]]
            )
            if min_distance > 0.0:
                widths.append(float(min_distance))

        if not widths:
            return [], 0.0
        return widths, float(sum(widths) / len(widths))

    @staticmethod
    def _cross_section_bank_intersections(
        xs_line: LineString,
        bank_lines: list[LineString],
    ) -> list[tuple[int, Point]]:
        intersections: list[tuple[int, Point]] = []
        for bank_idx, bank_line in enumerate(bank_lines):
            intersection = xs_line.intersection(bank_line)
            if intersection.is_empty:
                continue
            if intersection.geom_type == "Point":
                intersections.append((bank_idx, intersection))
            elif intersection.geom_type == "MultiPoint":
                for point in intersection.geoms:
                    intersections.append((bank_idx, point))
        return intersections

    @staticmethod
    def _open_bank_endpoints(
        bank_lines: list[LineString],
        snap_tolerance: float = 1e-6,
    ) -> list[tuple[int, int, Point]]:
        endpoints: list[tuple[int, int, Point]] = []
        for line_idx, bank_line in enumerate(bank_lines):
            coords = list(bank_line.coords)
            endpoints.append((line_idx, 0, Point(coords[0])))
            endpoints.append((line_idx, 1, Point(coords[-1])))

        open_points: list[tuple[int, int, Point]] = []
        for line_idx, end_idx, point in endpoints:
            duplicate_count = sum(
                1
                for _, _, other_point in endpoints
                if point.distance(other_point) <= snap_tolerance
            )
            if duplicate_count == 1:
                open_points.append((line_idx, end_idx, point))
        return open_points

    def _find_bank_opening_pairs(
        self,
        bank_lines: list[LineString],
        average_width: float,
        width_factor: float = 3.0,
        snap_tolerance: float = 1e-6,
    ) -> list[dict[str, Any]]:
        open_points = self._open_bank_endpoints(
            bank_lines,
            snap_tolerance=snap_tolerance,
        )
        candidates: list[tuple[float, int, int]] = []
        max_gap = float(average_width) * float(width_factor)
        for left_idx in range(len(open_points)):
            for right_idx in range(left_idx + 1, len(open_points)):
                left = open_points[left_idx]
                right = open_points[right_idx]
                if left[0] == right[0]:
                    continue
                gap = float(left[2].distance(right[2]))
                if snap_tolerance < gap <= max_gap:
                    candidates.append((gap, left_idx, right_idx))

        candidates.sort(key=lambda item: item[0])
        used_indices: set[int] = set()
        opening_pairs: list[dict[str, Any]] = []
        for gap, left_idx, right_idx in candidates:
            if left_idx in used_indices or right_idx in used_indices:
                continue
            used_indices.add(left_idx)
            used_indices.add(right_idx)
            left = open_points[left_idx]
            right = open_points[right_idx]
            opening_pairs.append(
                {
                    "gap_distance": gap,
                    "left_line_index": left[0],
                    "right_line_index": right[0],
                    "left_point": left[2],
                    "right_point": right[2],
                }
            )
        return opening_pairs

    def _repair_single_bank_opening(
        self,
        cross_section_df: pd.DataFrame,
        bank_lines: list[LineString],
        average_width: float,
        width_factor: float = 3.0,
    ) -> Optional[list[LineString]]:
        station_values = sorted(
            int(value) for value in cross_section_df["Station"].dropna().unique()
        )
        for station_idx in range(len(station_values) - 1):
            station_a = station_values[station_idx]
            station_b = station_values[station_idx + 1]
            xs_a_df = cross_section_df[cross_section_df["Station"] == station_a]
            xs_b_df = cross_section_df[cross_section_df["Station"] == station_b]
            if len(xs_a_df) < 2 or len(xs_b_df) < 2:
                continue

            xs_a = LineString(list(zip(xs_a_df["X"], xs_a_df["Y"])))
            xs_b = LineString(list(zip(xs_b_df["X"], xs_b_df["Y"])))
            local_box = self._junction_box(
                xs_a,
                xs_b,
                pad_factor=1.0,
            )
            connector_info = self._identify_local_opening_from_sections(
                xs_a=xs_a,
                xs_b=xs_b,
                bank_lines=bank_lines,
                local_box=local_box,
                average_width=average_width,
                width_factor=width_factor,
            )
            if connector_info is None:
                continue

            (
                intact_bank_idx,
                intact_a,
                intact_b,
                broken_idx_a,
                broken_a,
                broken_idx_b,
                broken_b,
            ) = connector_info
            connector = self._build_parallel_bank_connector(
                bank_lines=bank_lines,
                intact_bank_idx=intact_bank_idx,
                intact_a=intact_a,
                intact_b=intact_b,
                broken_a=broken_a,
                broken_b=broken_b,
            )
            trimmed_a, trimmed_b = self._trim_local_broken_bank_lines(
                bank_lines=bank_lines,
                broken_idx_a=broken_idx_a,
                broken_a=broken_a,
                broken_idx_b=broken_idx_b,
                broken_b=broken_b,
            )
            merged_broken_bank = self._merge_bank_line_parts(
                trimmed_a,
                connector,
                trimmed_b,
            )

            repaired = list(bank_lines)
            for drop_idx in sorted({broken_idx_a, broken_idx_b}, reverse=True):
                repaired.pop(drop_idx)
            repaired.append(merged_broken_bank)
            return repaired
        return None

    @staticmethod
    def _junction_box(
        xs_a: LineString,
        xs_b: LineString,
        pad_factor: float = 1.0,
    ):
        minx = min(xs_a.bounds[0], xs_b.bounds[0])
        miny = min(xs_a.bounds[1], xs_b.bounds[1])
        maxx = max(xs_a.bounds[2], xs_b.bounds[2])
        maxy = max(xs_a.bounds[3], xs_b.bounds[3])
        pad = max(float(xs_a.length), float(xs_b.length)) * float(pad_factor)
        return box(minx - pad, miny - pad, maxx + pad, maxy + pad)

    @staticmethod
    def _line_has_endpoint_in_box(bank_line: LineString, local_box) -> bool:
        coords = list(bank_line.coords)
        return bool(
            local_box.contains(Point(coords[0]))
            or local_box.contains(Point(coords[-1]))
        )

    def _identify_local_opening_from_sections(
        self,
        xs_a: LineString,
        xs_b: LineString,
        bank_lines: list[LineString],
        local_box,
        average_width: float,
        width_factor: float,
    ) -> Optional[tuple[int, Point, Point, int, Point, int, Point]]:
        intersections_a = self._cross_section_bank_intersections(xs_a, bank_lines)
        intersections_b = self._cross_section_bank_intersections(xs_b, bank_lines)
        if len(intersections_a) < 2 or len(intersections_b) < 2:
            return None

        common_bank_ids = {idx for idx, _ in intersections_a}.intersection(
            idx for idx, _ in intersections_b
        )
        if len(common_bank_ids) != 1:
            return None

        intact_bank_idx = next(iter(common_bank_ids))
        intact_a = next(point for idx, point in intersections_a if idx == intact_bank_idx)
        intact_b = next(point for idx, point in intersections_b if idx == intact_bank_idx)
        broken_candidates_a = [
            (idx, point)
            for idx, point in intersections_a
            if idx != intact_bank_idx
            and self._line_has_endpoint_in_box(bank_lines[idx], local_box)
        ]
        broken_candidates_b = [
            (idx, point)
            for idx, point in intersections_b
            if idx != intact_bank_idx
            and self._line_has_endpoint_in_box(bank_lines[idx], local_box)
        ]
        if len(broken_candidates_a) != 1 or len(broken_candidates_b) != 1:
            return None

        broken_idx_a, broken_a = broken_candidates_a[0]
        broken_idx_b, broken_b = broken_candidates_b[0]
        return (
            intact_bank_idx,
            intact_a,
            intact_b,
            broken_idx_a,
            broken_a,
            broken_idx_b,
            broken_b,
        )

    @staticmethod
    def _sample_linestring(
        line: LineString,
        sample_count: int = 25,
    ) -> list[Point]:
        sample_count = max(int(sample_count), 2)
        distances = np.linspace(0.0, float(line.length), sample_count)
        return [line.interpolate(float(distance)) for distance in distances]

    def _build_parallel_bank_connector(
        self,
        bank_lines: list[LineString],
        intact_bank_idx: int,
        intact_a: Point,
        intact_b: Point,
        broken_a: Point,
        broken_b: Point,
    ) -> LineString:
        intact_line = bank_lines[intact_bank_idx]
        start_distance = float(intact_line.project(intact_a))
        end_distance = float(intact_line.project(intact_b))
        intact_segment = self._substring_line(
            intact_line,
            min(start_distance, end_distance),
            max(start_distance, end_distance),
        )
        if start_distance > end_distance:
            intact_segment = LineString(list(intact_segment.coords)[::-1])

        vector_start = (
            float(broken_a.x - intact_a.x),
            float(broken_a.y - intact_a.y),
        )
        vector_end = (
            float(broken_b.x - intact_b.x),
            float(broken_b.y - intact_b.y),
        )

        warped_points: list[tuple[float, float]] = []
        sampled_points = self._sample_linestring(intact_segment, sample_count=25)
        max_index = len(sampled_points) - 1
        for idx, point in enumerate(sampled_points):
            fraction = 0.0 if max_index == 0 else idx / max_index
            x_offset = vector_start[0] * (1.0 - fraction) + vector_end[0] * fraction
            y_offset = vector_start[1] * (1.0 - fraction) + vector_end[1] * fraction
            warped_points.append((float(point.x + x_offset), float(point.y + y_offset)))
        return LineString(warped_points)

    def _trim_local_broken_bank_lines(
        self,
        bank_lines: list[LineString],
        broken_idx_a: int,
        broken_a: Point,
        broken_idx_b: int,
        broken_b: Point,
    ) -> tuple[LineString, LineString]:
        line_a = bank_lines[broken_idx_a]
        line_b = bank_lines[broken_idx_b]
        proj_a = float(line_a.project(broken_a))
        proj_b = float(line_b.project(broken_b))

        start_a = Point(line_a.coords[0])
        end_a = Point(line_a.coords[-1])
        start_b = Point(line_b.coords[0])
        end_b = Point(line_b.coords[-1])

        trimmed_a = (
            self._substring_line(line_a, proj_a, float(line_a.length))
            if start_a.distance(broken_a) < end_a.distance(broken_a)
            else self._substring_line(line_a, 0.0, proj_a)
        )
        trimmed_b = (
            self._substring_line(line_b, proj_b, float(line_b.length))
            if start_b.distance(broken_b) < end_b.distance(broken_b)
            else self._substring_line(line_b, 0.0, proj_b)
        )

        trimmed_a = LineString(trimmed_a.coords)
        trimmed_b = LineString(trimmed_b.coords)
        if Point(trimmed_a.coords[-1]).distance(broken_a) > Point(trimmed_a.coords[0]).distance(broken_a):
            trimmed_a = LineString(list(trimmed_a.coords)[::-1])
        if Point(trimmed_b.coords[0]).distance(broken_b) > Point(trimmed_b.coords[-1]).distance(broken_b):
            trimmed_b = LineString(list(trimmed_b.coords)[::-1])
        return trimmed_a, trimmed_b

    @staticmethod
    def _merge_bank_line_parts(
        first: LineString,
        connector: LineString,
        second: LineString,
    ) -> LineString:
        merged_coords: list[tuple[float, float]] = list(first.coords)
        for coord in list(connector.coords)[1:]:
            if coord != merged_coords[-1]:
                merged_coords.append(coord)
        for coord in list(second.coords)[1:]:
            if coord != merged_coords[-1]:
                merged_coords.append(coord)
        return LineString(merged_coords)

    @staticmethod
    def _substring_line(
        line: LineString,
        start_distance: float,
        end_distance: float,
    ) -> LineString:
        clipped = substring(line, start_distance, end_distance)
        if clipped.geom_type == "LineString":
            return clipped
        if clipped.geom_type == "MultiLineString":
            return max(clipped.geoms, key=lambda geom: geom.length)
        raise ValueError("Could not extract a line substring from bank geometry.")

    def _group_bank_lines_by_connectivity(
        self,
        bank_lines: list[LineString],
        endpoint_tolerance: float = 0.05,
    ) -> list[LineString]:
        if len(bank_lines) == 2:
            return list(bank_lines)

        endpoint_to_node, node_coords = self._cluster_bank_line_endpoints(
            bank_lines,
            endpoint_tolerance=endpoint_tolerance,
        )
        segments = []
        for segment_id, bank_line in enumerate(bank_lines):
            segments.append(
                {
                    "segment_id": segment_id,
                    "line": bank_line,
                    "start_node": endpoint_to_node[segment_id * 2],
                    "end_node": endpoint_to_node[segment_id * 2 + 1],
                }
            )

        components = self._collect_bank_segment_components(segments)
        if len(components) == 1:
            grouped_lines = self._group_single_bank_component_by_stricter_tolerance(
                bank_lines,
                endpoint_tolerance=endpoint_tolerance,
            )
            if grouped_lines is not None:
                return grouped_lines

        if len(components) != 2:
            raise ValueError(
                "Bank line shapefile must reduce to exactly two connected "
                f"components; found {len(components)}."
            )

        grouped_lines: list[LineString] = []
        for component_segment_ids in components:
            _, grouped_line = self._order_bank_component_segments(
                component_segment_ids,
                segments,
                node_coords,
            )
            grouped_lines.append(grouped_line)

        return grouped_lines

    def _group_single_bank_component_by_stricter_tolerance(
        self,
        bank_lines: list[LineString],
        endpoint_tolerance: float,
    ) -> Optional[list[LineString]]:
        tolerances = [
            endpoint_tolerance / 10.0,
            endpoint_tolerance / 100.0,
            endpoint_tolerance / 1000.0,
            endpoint_tolerance / 10000.0,
            endpoint_tolerance / 100000.0,
            0.0,
        ]
        seen_tolerances: set[float] = set()
        for stricter_tolerance in tolerances:
            if stricter_tolerance in seen_tolerances:
                continue
            seen_tolerances.add(stricter_tolerance)

            endpoint_to_node, node_coords = self._cluster_bank_line_endpoints(
                bank_lines,
                endpoint_tolerance=stricter_tolerance,
            )
            segments = []
            for segment_id, bank_line in enumerate(bank_lines):
                segments.append(
                    {
                        "segment_id": segment_id,
                        "line": bank_line,
                        "start_node": endpoint_to_node[segment_id * 2],
                        "end_node": endpoint_to_node[segment_id * 2 + 1],
                    }
                )

            components = self._collect_bank_segment_components(segments)
            if len(components) != 2:
                continue

            grouped_lines: list[LineString] = []
            try:
                for component_segment_ids in components:
                    _, grouped_line = self._order_bank_component_segments(
                        component_segment_ids,
                        segments,
                        node_coords,
                    )
                    grouped_lines.append(grouped_line)
            except ValueError:
                continue

            return grouped_lines

        return None

    @staticmethod
    def _find_bank_endpoint_root(parent: list[int], idx: int) -> int:
        while parent[idx] != idx:
            parent[idx] = parent[parent[idx]]
            idx = parent[idx]
        return idx

    @staticmethod
    def _union_bank_endpoint_roots(
        parent: list[int],
        left: int,
        right: int,
    ) -> None:
        left_root = HECRAS._find_bank_endpoint_root(parent, left)
        right_root = HECRAS._find_bank_endpoint_root(parent, right)
        if left_root != right_root:
            parent[right_root] = left_root

    @staticmethod
    def _cluster_bank_line_endpoints(
        bank_lines: list[LineString],
        endpoint_tolerance: float,
    ) -> tuple[dict[int, int], dict[int, tuple[float, float]]]:
        endpoints: list[Point] = []
        for bank_line in bank_lines:
            coords = list(bank_line.coords)
            endpoints.append(Point(coords[0]))
            endpoints.append(Point(coords[-1]))

        parent = list(range(len(endpoints)))
        for idx in range(len(endpoints)):
            for other_idx in range(idx + 1, len(endpoints)):
                if endpoints[idx].distance(endpoints[other_idx]) <= endpoint_tolerance:
                    HECRAS._union_bank_endpoint_roots(parent, idx, other_idx)

        root_to_members: dict[int, list[int]] = {}
        for idx in range(len(endpoints)):
            root = HECRAS._find_bank_endpoint_root(parent, idx)
            root_to_members.setdefault(root, []).append(idx)

        endpoint_to_node: dict[int, int] = {}
        node_coords: dict[int, tuple[float, float]] = {}
        for node_id, members in enumerate(root_to_members.values()):
            x_coord = sum(endpoints[idx].x for idx in members) / len(members)
            y_coord = sum(endpoints[idx].y for idx in members) / len(members)
            node_coords[node_id] = (float(x_coord), float(y_coord))
            for idx in members:
                endpoint_to_node[idx] = node_id

        return endpoint_to_node, node_coords

    @staticmethod
    def _collect_bank_segment_components(
        segments: list[dict[str, Any]],
    ) -> list[list[int]]:
        node_to_segments: dict[int, set[int]] = {}
        for segment in segments:
            node_to_segments.setdefault(segment["start_node"], set()).add(
                segment["segment_id"]
            )
            node_to_segments.setdefault(segment["end_node"], set()).add(
                segment["segment_id"]
            )

        adjacency: dict[int, set[int]] = {
            segment["segment_id"]: set() for segment in segments
        }
        for segment_ids in node_to_segments.values():
            for segment_id in segment_ids:
                adjacency[segment_id].update(segment_ids.difference({segment_id}))

        components: list[list[int]] = []
        seen: set[int] = set()
        for segment in segments:
            segment_id = segment["segment_id"]
            if segment_id in seen:
                continue

            stack = [segment_id]
            component: list[int] = []
            while stack:
                current = stack.pop()
                if current in seen:
                    continue
                seen.add(current)
                component.append(current)
                stack.extend(sorted(adjacency[current].difference(seen)))

            components.append(sorted(component))

        return components

    @staticmethod
    def _orient_bank_component_segment(
        segment: dict[str, Any],
        current_node: int,
        node_coords: dict[int, tuple[float, float]],
    ) -> tuple[list[tuple[float, float]], int]:
        coords = list(segment["line"].coords)
        if segment["start_node"] == current_node:
            coords[0] = node_coords[segment["start_node"]]
            coords[-1] = node_coords[segment["end_node"]]
            return [(float(x), float(y)) for x, y in coords], segment["end_node"]

        reversed_coords = list(reversed(coords))
        reversed_coords[0] = node_coords[segment["end_node"]]
        reversed_coords[-1] = node_coords[segment["start_node"]]
        return (
            [(float(x), float(y)) for x, y in reversed_coords],
            segment["start_node"],
        )

    @staticmethod
    def _order_bank_component_segments(
        component_segment_ids: list[int],
        segments: list[dict[str, Any]],
        node_coords: dict[int, tuple[float, float]],
    ) -> tuple[list[int], LineString]:
        segment_by_id = {segment["segment_id"]: segment for segment in segments}
        node_to_segments: dict[int, list[int]] = {}
        for segment_id in component_segment_ids:
            segment = segment_by_id[segment_id]
            node_to_segments.setdefault(segment["start_node"], []).append(segment_id)
            node_to_segments.setdefault(segment["end_node"], []).append(segment_id)

        terminal_nodes = [
            node_id
            for node_id, attached_segments in node_to_segments.items()
            if len(attached_segments) == 1
        ]
        if len(terminal_nodes) != 2:
            raise ValueError(
                "Each grouped bank component must be a simple chain with two "
                "terminal endpoints."
            )

        start_node = min(
            terminal_nodes,
            key=lambda node_id: (node_coords[node_id][1], node_coords[node_id][0]),
        )

        ordered_segment_ids: list[int] = []
        merged_coords: list[tuple[float, float]] = []
        used_segments: set[int] = set()
        current_node = start_node

        while True:
            candidates = [
                segment_id
                for segment_id in node_to_segments[current_node]
                if segment_id not in used_segments
            ]
            if not candidates:
                break

            next_segment_id = sorted(candidates)[0]
            segment = segment_by_id[next_segment_id]
            coords, next_node = HECRAS._orient_bank_component_segment(
                segment,
                current_node=current_node,
                node_coords=node_coords,
            )

            if not merged_coords:
                merged_coords.extend(coords)
            else:
                merged_coords.extend(coords[1:])

            ordered_segment_ids.append(next_segment_id)
            used_segments.add(next_segment_id)
            current_node = next_node

        return ordered_segment_ids, LineString(merged_coords)

    @staticmethod
    def _coerce_line_string(geometry: Any) -> LineString:
        if geometry.geom_type == "LineString":
            return LineString(geometry)

        if geometry.geom_type == "MultiLineString":
            merged = linemerge(geometry)
            if merged.geom_type == "LineString":
                return LineString(merged)
            if hasattr(merged, "geoms"):
                longest = max(merged.geoms, key=lambda geom: geom.length)
                return LineString(longest)

        raise ValueError("Bank line shapefile must contain line geometries.")

    @staticmethod
    def _line_to_bank_point(xs_line: LineString, bank_line: LineString) -> Point:
        intersection = xs_line.intersection(bank_line)
        if intersection.is_empty:
            point_on_xs, _ = nearest_points(xs_line, bank_line)
            return Point(point_on_xs.x, point_on_xs.y)

        if intersection.geom_type == "Point":
            return Point(intersection.x, intersection.y)

        if intersection.geom_type == "MultiPoint":
            points = list(intersection.geoms)
            distances = [xs_line.project(point) for point in points]
            return points[int(np.argmin(distances))]

        if intersection.geom_type == "LineString":
            coords = list(intersection.coords)
            return Point(coords[0])

        if hasattr(intersection, "geoms"):
            point_geoms = [geom for geom in intersection.geoms if geom.geom_type == "Point"]
            if point_geoms:
                distances = [xs_line.project(point) for point in point_geoms]
                return point_geoms[int(np.argmin(distances))]

        point_on_xs, _ = nearest_points(xs_line, bank_line)
        return Point(point_on_xs.x, point_on_xs.y)

    @staticmethod
    def _project_distance(line: LineString, point: Point) -> float:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            return float(line.project(point))

    @staticmethod
    def _orient_sections(section_inputs: list[dict[str, Any]]) -> None:
        midpoints = [item["channel_point"] for item in section_inputs]
        for idx, section in enumerate(section_inputs):
            if idx < len(section_inputs) - 1:
                downstream_vector = np.array(
                    [
                        midpoints[idx + 1].x - midpoints[idx].x,
                        midpoints[idx + 1].y - midpoints[idx].y,
                    ]
                )
            else:
                downstream_vector = np.array(
                    [
                        midpoints[idx].x - midpoints[idx - 1].x,
                        midpoints[idx].y - midpoints[idx - 1].y,
                    ]
                )

            start = np.array(section["points"][0])
            end = np.array(section["points"][-1])
            xs_vector = end - start
            cross_z = (
                downstream_vector[0] * xs_vector[1]
                - downstream_vector[1] * xs_vector[0]
            )

            if cross_z > 0:
                section["points"] = list(reversed(section["points"]))
                section["z_values"] = list(reversed(section["z_values"]))
                reversed_line = LineString(section["points"])
                section["bank_entries"] = [
                    {
                        **entry,
                        "station": HECRAS._project_distance(
                            reversed_line,
                            entry["point"],
                        ),
                    }
                    for entry in section["bank_entries"]
                ]

    @staticmethod
    def _assign_bank_sides(
        section_inputs: list[dict[str, Any]],
    ) -> tuple[int, int]:
        first_section = section_inputs[0]
        sorted_entries = sorted(
            first_section["bank_entries"],
            key=lambda entry: entry["station"],
        )
        left_bank_index = sorted_entries[0]["bank_index"]
        right_bank_index = sorted_entries[1]["bank_index"]

        for section in section_inputs:
            entry_map = {
                entry["bank_index"]: entry for entry in section["bank_entries"]
            }
            left_entry = entry_map[left_bank_index]
            right_entry = entry_map[right_bank_index]

            if left_entry["station"] > right_entry["station"]:
                left_entry, right_entry = right_entry, left_entry

            section["left_bank_entry"] = left_entry
            section["right_bank_entry"] = right_entry
            section["line"] = LineString(section["points"])

        return left_bank_index, right_bank_index

    @staticmethod
    def _orient_bank_line(
        section_inputs: list[dict[str, Any]],
        side_key: str,
    ) -> LineString:
        line = LineString(section_inputs[0][f"{side_key}_bank_entry"]["line"])
        point_key = f"{side_key}_bank_entry"

        forward_measures = [
            HECRAS._project_distance(line, section[point_key]["point"])
            for section in section_inputs
        ]

        reversed_line = LineString(list(line.coords)[::-1])
        reversed_measures = [
            HECRAS._project_distance(reversed_line, section[point_key]["point"])
            for section in section_inputs
        ]

        forward_descents = HECRAS._count_measure_descents(forward_measures)
        reversed_descents = HECRAS._count_measure_descents(reversed_measures)
        use_reversed = False
        if reversed_descents < forward_descents:
            use_reversed = True
        elif reversed_descents == forward_descents:
            use_reversed = reversed_measures[-1] > reversed_measures[0]
            if forward_measures[-1] > forward_measures[0]:
                use_reversed = False

        measures = forward_measures
        if use_reversed:
            line = reversed_line
            measures = reversed_measures

        for section, measure in zip(section_inputs, measures):
            section[f"{side_key}_bank_measure"] = float(measure)

        return line

    @staticmethod
    def _count_measure_descents(
        measures: list[float],
        tol: float = 1e-6,
    ) -> int:
        return sum(
            1
            for idx in range(1, len(measures))
            if measures[idx] < measures[idx - 1] - tol
        )

    @staticmethod
    def _filter_near_duplicate_sections(
        section_inputs: list[dict[str, Any]],
        min_section_spacing: float,
    ) -> tuple[list[dict[str, Any]], list[int]]:
        if min_section_spacing <= 0:
            return list(section_inputs), []

        filtered: list[dict[str, Any]] = []
        skipped: list[int] = []
        previous_midpoint: Optional[Point] = None

        for section in section_inputs:
            midpoint = section["channel_point"]
            if previous_midpoint is not None:
                spacing = previous_midpoint.distance(midpoint)
                if spacing < min_section_spacing:
                    skipped.append(section["source_station"])
                    continue

            filtered.append(section)
            previous_midpoint = midpoint

        if len(filtered) < 2:
            raise ValueError("At least two cross sections are required.")

        return filtered, skipped

    @staticmethod
    def _normalize_bank_station_mode(bank_station_mode: str) -> str:
        normalized = str(bank_station_mode).strip().lower()
        if normalized not in {"snap", "interpolate"}:
            raise ValueError(
                "bank_station_mode must be either 'snap' or 'interpolate'."
            )
        return normalized

    @staticmethod
    def _normalize_river_line_method(river_line_method: str) -> str:
        normalized = str(river_line_method).strip().lower()
        if normalized not in {"simple_distance", "perpendicular"}:
            raise ValueError(
                "river_line_method must be either 'simple_distance' or "
                "'perpendicular'."
            )
        return normalized

    @staticmethod
    def _finalize_sections(
        filtered_inputs: list[dict[str, Any]],
        river_station_step: float,
        bank_station_mode: str,
    ) -> list[SectionData]:
        sections: list[SectionData] = []
        bank_station_mode = HECRAS._normalize_bank_station_mode(
            bank_station_mode
        )

        for section in filtered_inputs:
            raw_left_bank_station = float(section["left_bank_entry"]["station"])
            raw_right_bank_station = float(section["right_bank_entry"]["station"])
            station_elevation = HECRAS._build_station_elevation(
                section["points"],
                section["z_values"],
                required_stations=(
                    [raw_left_bank_station, raw_right_bank_station]
                    if bank_station_mode == "interpolate"
                    else None
                ),
            )
            if bank_station_mode == "interpolate":
                left_bank_station = HECRAS._snap_station_to_profile_value(
                    raw_left_bank_station,
                    station_elevation,
                )
                right_bank_station = HECRAS._snap_station_to_profile_value(
                    raw_right_bank_station,
                    station_elevation,
                )
            else:
                left_bank_station = HECRAS._snap_station_to_profile_value(
                    raw_left_bank_station,
                    station_elevation,
                )
                right_bank_station = HECRAS._snap_station_to_profile_value(
                    raw_right_bank_station,
                    station_elevation,
                )
                left_bank_station = HECRAS._refine_bank_station(
                    raw_station=raw_left_bank_station,
                    snapped_station=left_bank_station,
                    station_elevation=station_elevation,
                    side="left",
                )
                right_bank_station = HECRAS._refine_bank_station(
                    raw_station=raw_right_bank_station,
                    snapped_station=right_bank_station,
                    station_elevation=station_elevation,
                    side="right",
                )
            if left_bank_station >= right_bank_station:
                left_bank_station = HECRAS._snap_station_to_profile_value(
                    raw_left_bank_station,
                    station_elevation,
                )
                right_bank_station = HECRAS._snap_station_to_profile_value(
                    raw_right_bank_station,
                    station_elevation,
                )

            sections.append(
                SectionData(
                    source_station=section["source_station"],
                    river_station=0.0,
                    line=section["line"],
                    station_elevation=station_elevation,
                    left_bank_station=left_bank_station,
                    right_bank_station=right_bank_station,
                    left_bank_point=Point(section["left_bank_entry"]["point"]),
                    right_bank_point=Point(section["right_bank_entry"]["point"]),
                    channel_point=Point(section["channel_point"]),
                    left_bank_measure=float(section["left_bank_measure"]),
                    right_bank_measure=float(section["right_bank_measure"]),
                    hdf_left_bank_station=(
                        float(section["hdf_left_bank_station"])
                        if "hdf_left_bank_station" in section
                        else None
                    ),
                    hdf_right_bank_station=(
                        float(section["hdf_right_bank_station"])
                        if "hdf_right_bank_station" in section
                        else None
                    ),
                )
            )

        return sections

    @staticmethod
    def _build_centerline_geometry(
        filtered_inputs: list[dict[str, Any]],
        left_bank_line: LineString,
        right_bank_line: LineString,
        samples_per_segment: int = 6,
        river_line_method: str = "simple_distance",
    ) -> tuple[list[tuple[float, float]], list[float], list[float]]:
        river_line_method = HECRAS._normalize_river_line_method(river_line_method)
        if river_line_method == "perpendicular":
            return HECRAS._build_centerline_geometry_perpendicular(
                filtered_inputs=filtered_inputs,
                left_bank_line=left_bank_line,
                right_bank_line=right_bank_line,
                samples_per_segment=samples_per_segment,
            )
        return HECRAS._build_centerline_geometry_simple_distance(
            filtered_inputs=filtered_inputs,
            left_bank_line=left_bank_line,
            right_bank_line=right_bank_line,
            samples_per_segment=samples_per_segment,
        )

    @staticmethod
    def _build_centerline_geometry_simple_distance(
        filtered_inputs: list[dict[str, Any]],
        left_bank_line: LineString,
        right_bank_line: LineString,
        samples_per_segment: int = 6,
    ) -> tuple[list[tuple[float, float]], list[float], list[float]]:
        if not filtered_inputs:
            return [], [], []

        samples_per_segment = max(int(samples_per_segment), 1)
        start_point = filtered_inputs[0]["channel_point"]
        centerline_points = [(float(start_point.x), float(start_point.y))]
        centerline_measures = [0.0]
        section_measures = [0.0]

        for idx in range(len(filtered_inputs) - 1):
            current = filtered_inputs[idx]
            next_section = filtered_inputs[idx + 1]
            segment_points: list[tuple[float, float]] = []

            for sample_idx in range(1, samples_per_segment):
                fraction = sample_idx / samples_per_segment
                left_measure = (
                    current["left_bank_measure"]
                    + fraction
                    * (
                        next_section["left_bank_measure"]
                        - current["left_bank_measure"]
                    )
                )
                right_measure = (
                    current["right_bank_measure"]
                    + fraction
                    * (
                        next_section["right_bank_measure"]
                        - current["right_bank_measure"]
                    )
                )
                left_point = left_bank_line.interpolate(left_measure)
                right_point = right_bank_line.interpolate(right_measure)
                segment_points.append(
                    (
                        (left_point.x + right_point.x) / 2.0,
                        (left_point.y + right_point.y) / 2.0,
                    )
                )

            next_midpoint = next_section["channel_point"]
            segment_points.append(
                (float(next_midpoint.x), float(next_midpoint.y))
            )

            for point in segment_points:
                if HECRAS._points_are_close(centerline_points[-1], point):
                    continue

                previous = Point(centerline_points[-1])
                current_point = Point(point)
                centerline_points.append(point)
                centerline_measures.append(
                    centerline_measures[-1] + previous.distance(current_point)
                )

            section_measures.append(centerline_measures[-1])

        return centerline_points, centerline_measures, section_measures

    @staticmethod
    def _build_centerline_geometry_perpendicular(
        filtered_inputs: list[dict[str, Any]],
        left_bank_line: LineString,
        right_bank_line: LineString,
        samples_per_segment: int = 6,
    ) -> tuple[list[tuple[float, float]], list[float], list[float]]:
        if not filtered_inputs:
            return [], [], []

        samples_per_segment = max(int(samples_per_segment), 1)
        sample_count = max(samples_per_segment * max(len(filtered_inputs) - 1, 1), 1)

        centerline_points: list[tuple[float, float]] = []
        first_channel_point = filtered_inputs[0]["channel_point"]
        last_channel_point = filtered_inputs[-1]["channel_point"]

        for sample_idx in range(sample_count + 1):
            fraction = sample_idx / sample_count
            left_point = left_bank_line.interpolate(fraction, normalized=True)
            right_point = right_bank_line.interpolate(fraction, normalized=True)
            point = (
                (left_point.x + right_point.x) / 2.0,
                (left_point.y + right_point.y) / 2.0,
            )
            if centerline_points and HECRAS._points_are_close(centerline_points[-1], point):
                continue
            centerline_points.append(point)

        start_point = (float(first_channel_point.x), float(first_channel_point.y))
        end_point = (float(last_channel_point.x), float(last_channel_point.y))
        if centerline_points:
            centerline_points[0] = start_point
            centerline_points[-1] = end_point
        else:
            centerline_points = [start_point, end_point]

        deduped_points: list[tuple[float, float]] = []
        for point in centerline_points:
            if deduped_points and HECRAS._points_are_close(deduped_points[-1], point):
                continue
            deduped_points.append(point)
        centerline_points = deduped_points

        if len(centerline_points) == 1:
            centerline_points.append(end_point)

        centerline_measures = HECRAS._cumulative_stationing(centerline_points)
        centerline_line = LineString(centerline_points)

        section_measures: list[float] = []
        for idx, section in enumerate(filtered_inputs):
            if idx == 0:
                section_measures.append(0.0)
                continue
            if idx == len(filtered_inputs) - 1:
                section_measures.append(centerline_measures[-1])
                continue
            section_measures.append(float(centerline_line.project(section["channel_point"])))

        return centerline_points, centerline_measures, section_measures

    @staticmethod
    def _points_are_close(
        first: tuple[float, float],
        second: tuple[float, float],
        tol: float = 1e-6,
    ) -> bool:
        return bool(
            np.isclose(first[0], second[0], atol=tol, rtol=0.0)
            and np.isclose(first[1], second[1], atol=tol, rtol=0.0)
        )

    @staticmethod
    def _cumulative_stationing(points: list[tuple[float, float]]) -> list[float]:
        stations = [0.0]
        for idx in range(1, len(points)):
            start = Point(points[idx - 1])
            end = Point(points[idx])
            stations.append(stations[-1] + start.distance(end))
        return stations

    @staticmethod
    def _build_station_elevation(
        points: list[tuple[float, float]],
        z_values: list[float],
        required_stations: Optional[list[float]] = None,
    ) -> pd.DataFrame:
        stations = HECRAS._cumulative_stationing(points)
        simplified = HECRAS._simplify_profile_points(stations, z_values)
        if required_stations:
            simplified = HECRAS._insert_required_profile_stations(
                simplified,
                stations,
                z_values,
                required_stations,
            )
        rounded = HECRAS._round_profile_points(simplified)
        return pd.DataFrame(rounded, columns=["Station", "Elevation"])

    @staticmethod
    def _simplify_profile_points(
        stations: list[float],
        elevations: list[float],
        tol: float = 1e-9,
    ) -> list[tuple[float, float]]:
        points: list[tuple[float, float]] = []
        for station, elevation in zip(stations, elevations):
            point = (float(station), float(elevation))
            if points and np.isclose(points[-1][0], point[0], atol=tol) and np.isclose(
                points[-1][1], point[1], atol=tol
            ):
                continue
            points.append(point)

        if len(points) <= 2:
            return points

        simplified = [points[0]]
        for idx in range(1, len(points) - 1):
            prev_point = simplified[-1]
            current_point = points[idx]
            next_point = points[idx + 1]

            if HECRAS._is_collinear_profile_point(
                prev_point,
                current_point,
                next_point,
                tol=tol,
            ):
                continue

            simplified.append(current_point)

        simplified.append(points[-1])
        return simplified

    @staticmethod
    def _is_collinear_profile_point(
        prev_point: tuple[float, float],
        current_point: tuple[float, float],
        next_point: tuple[float, float],
        tol: float = 1e-9,
    ) -> bool:
        cross_product = (
            (current_point[0] - prev_point[0]) * (next_point[1] - prev_point[1])
            - (current_point[1] - prev_point[1]) * (next_point[0] - prev_point[0])
        )
        if abs(cross_product) > tol:
            return False

        return (
            min(prev_point[0], next_point[0]) - tol
            <= current_point[0]
            <= max(prev_point[0], next_point[0]) + tol
            and min(prev_point[1], next_point[1]) - tol
            <= current_point[1]
            <= max(prev_point[1], next_point[1]) + tol
        )

    @staticmethod
    def _insert_required_profile_stations(
        simplified_points: list[tuple[float, float]],
        full_stations: list[float],
        full_elevations: list[float],
        required_stations: list[float],
        tol: float = 1e-9,
    ) -> list[tuple[float, float]]:
        points = list(simplified_points)

        for required_station in required_stations:
            if any(np.isclose(required_station, point[0], atol=tol) for point in points):
                continue

            elevation = HECRAS._interpolate_profile_elevation(
                full_stations,
                full_elevations,
                required_station,
            )
            insert_at = 0
            while insert_at < len(points) and points[insert_at][0] < required_station:
                insert_at += 1
            points.insert(insert_at, (float(required_station), float(elevation)))

        return points

    @staticmethod
    def _round_profile_points(
        points: list[tuple[float, float]],
        station_decimals: int = 3,
        elevation_decimals: int = 3,
        tol: float = 1e-9,
    ) -> list[tuple[float, float]]:
        rounded_points: list[tuple[float, float]] = []
        for station, elevation in points:
            rounded_point = (
                round(float(station), station_decimals),
                round(float(elevation), elevation_decimals),
            )
            if rounded_points and np.isclose(
                rounded_points[-1][0],
                rounded_point[0],
                atol=tol,
            ) and np.isclose(
                rounded_points[-1][1],
                rounded_point[1],
                atol=tol,
            ):
                continue
            rounded_points.append(rounded_point)

        return rounded_points

    @staticmethod
    def _interpolate_profile_elevation(
        stations: list[float],
        elevations: list[float],
        target_station: float,
        tol: float = 1e-9,
    ) -> float:
        if target_station <= stations[0]:
            return float(elevations[0])
        if target_station >= stations[-1]:
            return float(elevations[-1])

        for idx, station in enumerate(stations):
            if np.isclose(station, target_station, atol=tol):
                return float(elevations[idx])

        for idx in range(len(stations) - 1):
            start_station = float(stations[idx])
            end_station = float(stations[idx + 1])
            start_elevation = float(elevations[idx])
            end_elevation = float(elevations[idx + 1])

            if np.isclose(start_station, end_station, atol=tol):
                continue

            if start_station <= target_station <= end_station:
                ratio = (target_station - start_station) / (
                    end_station - start_station
                )
                return start_elevation + ratio * (
                    end_elevation - start_elevation
                )

        return float(elevations[-1])

    @staticmethod
    def _snap_station_to_profile_value(
        station: float,
        station_elevation: pd.DataFrame,
    ) -> float:
        profile_stations = station_elevation["Station"].to_numpy(dtype=float)
        nearest_index = int(np.argmin(np.abs(profile_stations - float(station))))
        return float(profile_stations[nearest_index])

    @staticmethod
    def _refine_bank_station(
        raw_station: float,
        snapped_station: float,
        station_elevation: pd.DataFrame,
        side: str,
        search_pairs: int = 4,
        min_elevation_jump: float = 0.75,
        min_slope: float = 0.35,
        min_drop: float = 0.05,
    ) -> float:
        if side not in {"left", "right"}:
            raise ValueError("side must be 'left' or 'right'")

        stations = station_elevation["Station"].to_numpy(dtype=float)
        elevations = station_elevation["Elevation"].to_numpy(dtype=float)
        if len(stations) < 2:
            return float(snapped_station)

        snapped_index = int(np.argmin(np.abs(stations - float(snapped_station))))
        start_index = max(0, snapped_index - search_pairs)
        end_index = min(len(stations) - 1, snapped_index + search_pairs)
        best_station = float(snapped_station)
        best_score: Optional[tuple[float, float]] = None

        for idx in range(start_index, end_index):
            station_0 = float(stations[idx])
            station_1 = float(stations[idx + 1])
            elevation_0 = float(elevations[idx])
            elevation_1 = float(elevations[idx + 1])
            delta_station = abs(station_1 - station_0)
            delta_elevation = elevation_1 - elevation_0
            local_slope = abs(delta_elevation) / max(delta_station, 1e-6)

            has_bank_break = (
                abs(delta_elevation) >= min_elevation_jump
                or local_slope >= min_slope
            )
            if not has_bank_break:
                continue

            if side == "left":
                if elevation_0 <= elevation_1 + min_drop:
                    continue
                candidate_station = station_0
            else:
                if elevation_1 <= elevation_0 + min_drop:
                    continue
                candidate_station = station_1

            score = (
                abs(candidate_station - float(raw_station)),
                -abs(delta_elevation),
            )
            if best_score is None or score < best_score:
                best_station = candidate_station
                best_score = score

        return float(best_station)

    @staticmethod
    def _populate_reach_lengths(
        sections: list[SectionData],
        section_centerline_measures: list[float],
    ) -> None:
        for idx, section in enumerate(sections):
            section.centerline_measure = float(section_centerline_measures[idx])
            if idx == len(sections) - 1:
                section.left_reach_length = 0.0
                section.channel_reach_length = 0.0
                section.right_reach_length = 0.0
                continue

            next_section = sections[idx + 1]
            left_distance = abs(
                next_section.left_bank_measure - section.left_bank_measure
            )
            channel_distance = (
                section_centerline_measures[idx + 1]
                - section_centerline_measures[idx]
            )
            right_distance = abs(
                next_section.right_bank_measure - section.right_bank_measure
            )
            if channel_distance <= 1e-6:
                channel_distance = 0.5 * (left_distance + right_distance)

            section.left_reach_length = float(left_distance)
            section.channel_reach_length = float(channel_distance)
            section.right_reach_length = float(right_distance)

    @staticmethod
    def _assign_river_stations(sections: list[SectionData]) -> None:
        if not sections:
            return

        sections[-1].river_station = 0.0
        cumulative_distance = 0.0
        for idx in range(len(sections) - 2, -1, -1):
            cumulative_distance += sections[idx].channel_reach_length
            proposed_station = float(HECRAS._round_half_up(cumulative_distance))
            minimum_station = sections[idx + 1].river_station + 1.0
            sections[idx].river_station = max(proposed_station, minimum_station)

    @staticmethod
    def _round_half_up(value: float) -> int:
        return int(np.floor(float(value) + 0.5))

    @staticmethod
    def _format_number(value: float, decimals: int = 3) -> str:
        text = f"{float(value):.{decimals}f}"
        text = text.rstrip("0").rstrip(".")
        if text == "-0":
            return "0"
        return text

    @staticmethod
    def _format_river_station(value: float) -> str:
        rounded = HECRAS._round_half_up(value)
        if abs(float(value) - rounded) < 1e-6:
            return str(rounded)
        return HECRAS._format_number(value, decimals=1)

    @staticmethod
    def _hecras_timestamp() -> str:
        return time.strftime("%b/%d/%Y %H:%M:%S")

    @staticmethod
    def _viewing_rectangle(
        sections: list[SectionData],
        centerline_points: Optional[list[tuple[float, float]]] = None,
        padding: float = 10.0,
    ) -> tuple[float, float, float, float]:
        xs_points = [(x, y) for section in sections for x, y in section.line.coords]
        if centerline_points:
            xs_points.extend(centerline_points)
        else:
            xs_points.extend(
                (section.channel_point.x, section.channel_point.y)
                for section in sections
            )
        xmin = min(point[0] for point in xs_points) - padding
        xmax = max(point[0] for point in xs_points) + padding
        ymin = min(point[1] for point in xs_points) - padding
        ymax = max(point[1] for point in xs_points) + padding
        return xmin, xmax, ymax, ymin

    def _write_junction_geometry_file(
        self,
        geometry_path: Path,
        geom_title: str,
        reaches: list[dict[str, Any]],
        junction: dict[str, Any],
        expansion_coeff: float,
        contraction_coeff: float,
        channel_mannings_n: float,
        overbank_mannings_n: float,
    ) -> None:
        all_sections = [section for reach in reaches for section in reach["sections"]]
        all_centerline_points = [
            point for reach in reaches for point in reach["centerline_points"]
        ]
        xmin, xmax, ymax, ymin = self._viewing_rectangle(
            all_sections,
            centerline_points=all_centerline_points,
        )
        lines = [
            f"Geom Title={geom_title}\n",
            "Program Version=6.70\n",
            (
                "Viewing Rectangle= "
                f"{xmin:.4f} , {xmax:.4f} , {ymax:.3f} , {ymin:.3f} \n"
            ),
            "\n",
            f"Junct Name={junction['name']:<16}\n",
            "Junct Desc=, 0 , 0 , 0 ,0\n",
            (
                "Junct X Y & Text X Y="
                f"{junction['x']:.7f},{junction['y']:.7f},,\n"
            ),
        ]
        for river, reach in junction["upstream_reaches"]:
            lines.append(f"Up River,Reach={river:<16},{reach:<16}\n")
        lines.append(
            "Dn River,Reach="
            f"{junction.get('downstream_river', reaches[-1]['river']):<16},"
            f"{junction['downstream_reach']:<16}\n"
        )
        junction_lengths_angles = junction.get("junction_lengths_angles") or []
        if junction_lengths_angles:
            lines.extend(
                [f"Junc L&A={value}\n" for value in junction_lengths_angles]
            )
        else:
            lines.extend(["Junc L&A=0,0\n", "Junc L&A=0,0\n"])
        lines.append("\n")
        for reach_context in reaches:
            lines.extend(
                self._render_reach_block(
                    river=reach_context["river"],
                    reach=reach_context["reach"],
                    sections=reach_context["sections"],
                    structures=reach_context.get("structures", []),
                    centerline_points=reach_context["centerline_points"],
                    expansion_coeff=expansion_coeff,
                    contraction_coeff=contraction_coeff,
                    channel_mannings_n=channel_mannings_n,
                    overbank_mannings_n=overbank_mannings_n,
                )
            )
        lines.extend(
            [
                "LCMann Time=Dec/30/1899 00:00:00\n",
                "LCMann Region Time=Dec/30/1899 00:00:00\n",
                "LCMann Table=0\n",
                "Chan Stop Cuts=-1 \n",
                "\n",
                "\n",
                "\n",
                "Use User Specified Reach Order=0\n",
                "GIS Ratio Cuts To Invert=-1\n",
                "GIS Limit At Bridges=0\n",
                "Composite Channel Slope=5\n",
            ]
        )
        geometry_path.write_text("".join(lines), encoding="utf-8")

    def _render_reach_block(
        self,
        river: str,
        reach: str,
        sections: list[SectionData],
        structures: list[StructureData],
        centerline_points: list[tuple[float, float]],
        expansion_coeff: float,
        contraction_coeff: float,
        channel_mannings_n: float,
        overbank_mannings_n: float,
    ) -> list[str]:
        reach_xy_values: list[float] = []
        for x_value, y_value in centerline_points:
            reach_xy_values.extend([x_value, y_value])
        lines = [
            f"River Reach={river:<16},{reach:<16}\n",
            f"Reach XY= {len(centerline_points)} \n",
        ]
        lines.extend(
            GeomParser.format_fixed_width(
                reach_xy_values,
                column_width=16,
                values_per_line=4,
                precision=3,
            )
        )
        lines.extend(
            [
                "Rch Text X Y=,\n",
                "Reverse River Text= 0 \n",
                "\n",
            ]
        )
        node_entries: list[tuple[str, float, Any]] = [
            ("xs", section.river_station, section) for section in sections
        ]
        node_entries.extend(
            ("structure", structure.river_station, structure)
            for structure in structures
        )
        node_entries.sort(key=lambda entry: (-entry[1], entry[0]))

        for node_type, _, node_data in node_entries:
            if node_type == "xs":
                lines.extend(
                    self._render_cross_section_block(
                        section=node_data,
                        expansion_coeff=expansion_coeff,
                        contraction_coeff=contraction_coeff,
                        channel_mannings_n=channel_mannings_n,
                        overbank_mannings_n=overbank_mannings_n,
                    )
                )
            else:
                lines.extend(self._render_structure_block(node_data))
        return lines

    @staticmethod
    def _compute_htab_parameters(section: SectionData) -> tuple[str, str, int]:
        min_elev = float(section.station_elevation["Elevation"].min())
        max_elev = float(section.station_elevation["Elevation"].max())
        starting_elev = min_elev + 0.15
        table_count = 20
        if max_elev <= starting_elev:
            increment = 0.10
        else:
            increment = max((max_elev - starting_elev) / table_count, 0.10)

        return (
            HECRAS._format_number(starting_elev, decimals=4),
            HECRAS._format_number(increment, decimals=2),
            table_count,
        )

    @staticmethod
    def _estimate_downstream_friction_slope(sections: list[SectionData]) -> float:
        distances = [0.0]
        bed_elevations = [float(section.station_elevation["Elevation"].min()) for section in sections]

        for idx in range(1, len(sections)):
            distances.append(
                distances[-1] + sections[idx - 1].channel_reach_length
            )

        usable_rows = [
            (dist, elev)
            for dist, elev in zip(distances, bed_elevations)
            if dist > 1e-6 or len(distances) == 1
        ]

        x = np.array([row[0] for row in usable_rows], dtype=float)
        y = np.array([row[1] for row in usable_rows], dtype=float)

        if len(x) < 2:
            return 0.001

        slope = abs(float(np.polyfit(x, y, 1)[0]))
        return max(slope, 1e-4)

    def _write_geometry_file(
        self,
        geometry_path: Path,
        geom_title: str,
        river: str,
        reach: str,
        sections: list[SectionData],
        structures: list[StructureData],
        centerline_points: list[tuple[float, float]],
        expansion_coeff: float,
        contraction_coeff: float,
        channel_mannings_n: float,
        overbank_mannings_n: float,
    ) -> None:
        xmin, xmax, ymax, ymin = self._viewing_rectangle(
            sections,
            centerline_points=centerline_points,
        )
        reach_xy_values: list[float] = []
        for x_value, y_value in centerline_points:
            reach_xy_values.extend([x_value, y_value])

        lines = [
            f"Geom Title={geom_title}\n",
            "Program Version=6.70\n",
            (
                "Viewing Rectangle= "
                f"{xmin:.4f} , {xmax:.4f} , {ymax:.3f} , {ymin:.3f} \n"
            ),
            "\n",
            f"River Reach={river:<16},{reach:<16}\n",
            f"Reach XY= {len(centerline_points)} \n",
        ]
        lines.extend(
            GeomParser.format_fixed_width(
                reach_xy_values,
                column_width=16,
                values_per_line=4,
                precision=3,
            )
        )
        lines.extend(
            [
                "Rch Text X Y=,\n",
                "Reverse River Text= 0 \n",
                "\n",
            ]
        )

        node_entries: list[tuple[str, float, Any]] = [
            ("xs", section.river_station, section) for section in sections
        ]
        node_entries.extend(
            ("structure", structure.river_station, structure)
            for structure in structures
        )
        node_entries.sort(key=lambda entry: (-entry[1], entry[0]))

        for node_type, _, node_data in node_entries:
            if node_type == "xs":
                lines.extend(
                    self._render_cross_section_block(
                        section=node_data,
                        expansion_coeff=expansion_coeff,
                        contraction_coeff=contraction_coeff,
                        channel_mannings_n=channel_mannings_n,
                        overbank_mannings_n=overbank_mannings_n,
                    )
                )
            else:
                lines.extend(self._render_structure_block(node_data))

        lines.extend(
            [
                "LCMann Time=Dec/30/1899 00:00:00\n",
                "LCMann Region Time=Dec/30/1899 00:00:00\n",
                "LCMann Table=0\n",
                "Chan Stop Cuts=-1 \n",
                "\n",
                "\n",
                "\n",
                "Use User Specified Reach Order=0\n",
                "GIS Ratio Cuts To Invert=-1\n",
                "GIS Limit At Bridges=0\n",
                "Composite Channel Slope=5\n",
            ]
        )
        geometry_path.write_text("".join(lines), encoding="utf-8")

    @staticmethod
    def _render_cross_section_block(
        section: SectionData,
        expansion_coeff: float,
        contraction_coeff: float,
        channel_mannings_n: float,
        overbank_mannings_n: float,
    ) -> list[str]:
        timestamp = HECRAS._hecras_timestamp()
        cut_line_values: list[float] = []
        for x, y in section.line.coords:
            cut_line_values.extend([x, y])

        sta_elev_values: list[float] = []
        for _, row in section.station_elevation.iterrows():
            sta_elev_values.extend([float(row["Station"]), float(row["Elevation"])])

        mann_values = [
            0.0,
            overbank_mannings_n,
            0.0,
            section.left_bank_station,
            channel_mannings_n,
            0.0,
            section.right_bank_station,
            overbank_mannings_n,
            0.0,
        ]

        rm_label = HECRAS._format_river_station(section.river_station)
        if max(
            section.left_reach_length,
            section.channel_reach_length,
            section.right_reach_length,
        ) > 0:
            reach_label = (
                f"{HECRAS._format_number(section.left_reach_length, decimals=1)},"
                f"{HECRAS._format_number(section.channel_reach_length, decimals=1)},"
                f"{HECRAS._format_number(section.right_reach_length, decimals=1)}"
            )
        else:
            reach_label = ",,"

        htab_start, htab_increment, htab_count = HECRAS._compute_htab_parameters(
            section
        )

        block = [
            (
                "Type RM Length L Ch R = "
                f"1 ,{rm_label:<8},{reach_label}\n"
            ),
            f"XS GIS Cut Line={len(section.line.coords)}\n",
        ]
        block.extend(
            GeomParser.format_fixed_width(
                cut_line_values,
                column_width=16,
                values_per_line=4,
                precision=3,
            )
        )
        block.append(f"Node Last Edited Time={timestamp}\n")
        block.append(f"#Sta/Elev= {len(section.station_elevation)} \n")
        block.extend(
            GeomParser.format_fixed_width(
                sta_elev_values,
                column_width=8,
                values_per_line=10,
                precision=3,
            )
        )
        block.append("#Mann= 3 ,0,0\n")
        block.extend(
            GeomParser.format_fixed_width(
                mann_values,
                column_width=8,
                values_per_line=9,
                precision=3,
            )
        )
        block.append(HECRAS._render_bank_crest_levee_line(section))
        block.append(
            "Bank Sta="
            f"{HECRAS._format_number(section.left_bank_station, decimals=3)},"
            f"{HECRAS._format_number(section.right_bank_station, decimals=3)}\n"
        )
        block.append("XS Rating Curve= 0 ,0\n")
        block.append(
            "XS HTab Starting El and Incr="
            f"{htab_start},{htab_increment}, {htab_count} \n"
        )
        block.append("XS HTab Horizontal Distribution= 5 , 5 , 5 \n")
        block.append(
            "Exp/Cntr="
            f"{HECRAS._format_number(expansion_coeff, decimals=1)},"
            f"{HECRAS._format_number(contraction_coeff, decimals=1)}\n"
        )
        block.append("\n")
        return block

    @staticmethod
    def _render_structure_block(structure: StructureData) -> list[str]:
        timestamp = HECRAS._hecras_timestamp()
        rm_label = HECRAS._format_river_station(structure.river_station)

        block = [
            (
                f"Type RM Length L Ch R = "
                f"{'3' if structure.structure_type == 'bridge_deck' else '2'} "
                f",{rm_label:<8},,,\n"
            ),
            "BEGIN DESCRIPTION:\n",
            f"{structure.name}\n",
            "END DESCRIPTION:\n",
            f"Node Last Edited Time={timestamp}\n",
            "Bridge Culvert--1,0,-1,-1, 0 \n",
            (
                "Deck Dist Width WeirC Skew NumUp NumDn "
                "MinLoCord MaxHiCord MaxSubmerge Is_Ogee\n"
            ),
            (
                f"{HECRAS._format_number(structure.deck_distance, decimals=2)},"
                f"{HECRAS._format_number(structure.culvert_length, decimals=3)},"
                f"{HECRAS._format_number(structure.deck_weir_coefficient, decimals=3)},"
                f"{HECRAS._format_number(structure.deck_skew, decimals=3)},"
                "2,2,,,"
                f"{HECRAS._format_number(structure.deck_max_submerge, decimals=3)},"
                "0,0,0,,\n"
            ),
        ]
        block.extend(HECRAS._format_structure_series(structure.upstream_deck_stations))
        block.extend(
            HECRAS._format_structure_series(structure.upstream_deck_elevations)
        )
        block.extend(HECRAS._format_structure_series(structure.upstream_low_chords))
        block.extend(HECRAS._format_structure_series(structure.downstream_deck_stations))
        block.extend(
            HECRAS._format_structure_series(structure.downstream_deck_elevations)
        )
        block.extend(HECRAS._format_structure_series(structure.downstream_low_chords))
        block.extend(
            [
                "BR Coef=-1 , 0 , 0 ,, 0 ,,,0.8,-1,,0,\n",
                "WSPro=,,,, 1 ,,,, 0 ,,,, 0 ,,,,-1 ,-1 ,-1 , 0 , 0 , 0 , 0 , 0 \n",
            ]
        )
        if structure.structure_type == "bridge_deck":
            block.extend(
                [
                    "BC Design=,, 0 ,, 0 ,,,,,,\n",
                    f"BC HTab HWMax={HECRAS._format_number(structure.htab_hwmax, decimals=3)}\n",
                    f"BC HTab MaxFlow={HECRAS._format_number(structure.htab_max_flow, decimals=3)}\n",
                    "BC Use User HTab Curves=0\n",
                    "BC User HTab FreeFlow(D)= 0 \n",
                    "\n",
                ]
            )
            return block

        culvert_station_values = HECRAS._culvert_station_values(structure)
        barrel_size = max(structure.culvert_span, structure.culvert_rise)
        barrel_size_label = HECRAS._format_number(barrel_size, decimals=2).replace(
            ".",
            "p",
        )
        barrel_name = f"{structure.culvert_shape_name}_{barrel_size_label}"
        if structure.structure_type == "conspan_arch":
            barrel_name = "conspan_arch"
        if structure.num_barrels > 1:
            block.append(
                "Multiple Barrel Culv="
                f"{structure.culvert_shape_code},"
                f"{HECRAS._format_number(structure.culvert_rise, decimals=3)},"
                f"{HECRAS._format_number(structure.culvert_span, decimals=3)},"
                f"{HECRAS._format_number(structure.culvert_length, decimals=3)},"
                f"{HECRAS._format_number(structure.culvert_mannings_n, decimals=3)},"
                f"{HECRAS._format_number(structure.entrance_loss, decimals=3)},"
                f"{HECRAS._format_number(structure.exit_loss, decimals=3)},"
                f"{structure.inlet_type},"
                f"{structure.outlet_type},"
                f"{HECRAS._format_number(structure.upstream_invert, decimals=3)},"
                f"{HECRAS._format_number(structure.downstream_invert, decimals=3)},"
                f"{structure.num_barrels},"
                f"{structure.name},"
                f"{structure.culvert_chart_number},"
                f"{HECRAS._format_number(structure.deck_distance, decimals=2)}\n"
            )
        else:
            block.append(
                "Culvert="
                f"{structure.culvert_shape_code},"
                f"{HECRAS._format_number(structure.culvert_rise, decimals=3)},"
                f"{HECRAS._format_number(structure.culvert_span, decimals=3)},"
                f"{HECRAS._format_number(structure.culvert_length, decimals=3)},"
                f"{HECRAS._format_number(structure.culvert_mannings_n, decimals=3)},"
                f"{HECRAS._format_number(structure.entrance_loss, decimals=3)},"
                f"{HECRAS._format_number(structure.exit_loss, decimals=3)},"
                f"{structure.inlet_type},"
                f"{structure.outlet_type},"
                f"{HECRAS._format_number(structure.upstream_invert, decimals=3)},"
                f"{HECRAS._format_number(structure.upstream_opening_station, decimals=3)},"
                f"{HECRAS._format_number(structure.downstream_invert, decimals=3)},"
                f"{HECRAS._format_number(structure.downstream_opening_station, decimals=3)},"
                f"{structure.name},"
                f"{structure.culvert_chart_number},"
                f"{HECRAS._format_number(structure.deck_distance, decimals=2)}\n"
            )
        block.extend(HECRAS._format_structure_series(culvert_station_values))
        for barrel_index in range(1, structure.num_barrels + 1):
            barrel_label = barrel_name
            if structure.structure_type == "conspan_arch":
                barrel_label = f"{barrel_name}_{barrel_index}"
            block.append(
                f"BC Culvert Barrel={barrel_index},{barrel_label},0\n"
            )
        block.extend(
            [
                (
                    "Culvert Bottom n="
                    f"{HECRAS._format_number(structure.culvert_bottom_n, decimals=3)}\n"
                ),
                "BC Design=,, 0 ,, 0 ,,,,,,\n",
                "BC Use User HTab Curves=0\n",
                "BC User HTab FreeFlow(D)= 0 \n",
                "\n",
            ]
        )
        return block

    @staticmethod
    def _culvert_station_values(structure: StructureData) -> list[float]:
        if structure.num_barrels <= 1:
            return [
                structure.upstream_opening_station,
                structure.downstream_opening_station,
            ]

        center_index = 0.5 * (structure.num_barrels - 1)
        values: list[float] = []
        for barrel_index in range(structure.num_barrels):
            offset = (barrel_index - center_index) * structure.barrel_center_spacing
            values.extend(
                [
                    structure.upstream_opening_station + offset,
                    structure.downstream_opening_station + offset,
                ]
            )
        return values

    @staticmethod
    def _format_structure_series(values: list[float]) -> list[str]:
        return GeomParser.format_fixed_width(
            values,
            column_width=8,
            values_per_line=len(values),
            precision=3,
        )

    @staticmethod
    def _render_bank_crest_levee_line(section: SectionData) -> str:
        left_levee_elev = HECRAS._levee_elevation_at_station(
            section.station_elevation,
            section.left_bank_station,
        )
        right_levee_elev = HECRAS._levee_elevation_at_station(
            section.station_elevation,
            section.right_bank_station,
        )
        return (
            "Levee="
            f"-1,{HECRAS._format_number(section.left_bank_station, decimals=3)},"
            f"{HECRAS._format_number(left_levee_elev, decimals=3)},"
            f"-1,{HECRAS._format_number(section.right_bank_station, decimals=3)},"
            f"{HECRAS._format_number(right_levee_elev, decimals=3)},,\n"
        )

    @staticmethod
    def _levee_elevation_at_station(
        station_elevation: pd.DataFrame,
        station: float,
        tol: float = 1e-6,
    ) -> float:
        station_values = station_elevation["Station"].to_numpy(dtype=float)
        elevation_values = station_elevation["Elevation"].to_numpy(dtype=float)
        exact_mask = np.isclose(station_values, float(station), atol=tol, rtol=0.0)
        if np.any(exact_mask):
            return float(np.max(elevation_values[exact_mask]))

        return HECRAS._interpolate_profile_elevation(
            station_values.tolist(),
            elevation_values.tolist(),
            float(station),
        )

    @staticmethod
    def _write_steady_flow_file(
        flow_path: Path,
        flow_title: str,
        river: str,
        reach: str,
        upstream_river_station: float,
        downstream_river_station: float,
        flow_cms: float,
        profile_name: str,
        friction_slope: float,
        flow_profiles: Optional[list[tuple[str, float]]] = None,
    ) -> None:
        profiles = (
            [(profile_name, float(flow_cms))]
            if flow_profiles is None
            else [(str(name), float(value)) for name, value in flow_profiles]
        )
        profile_names = ",".join(name for name, _ in profiles)
        flow_values_line = "".join(
            f"{HECRAS._format_number(value, decimals=3):>8}"
            for _, value in profiles
        )
        content = [
            f"Flow Title={flow_title}\n",
            "Program Version=6.70\n",
            f"Number of Profiles= {len(profiles)} \n",
            f"Profile Names={profile_names}\n",
            (
                "River Rch & RM="
                f"{river},{reach:<16},"
                f"{HECRAS._format_river_station(upstream_river_station):<8}\n"
            ),
            f"{flow_values_line}\n",
            (
                "Boundary for River Rch & Prof#="
                f"{river},{reach:<16}, 1 \n"
            ),
            "Up Type= 3 \n",
            f"Up Slope={HECRAS._format_number(friction_slope, decimals=5)}\n",
            "Dn Type= 3 \n",
            f"Dn Slope={HECRAS._format_number(friction_slope, decimals=5)}\n",
            "DSS Import StartDate=\n",
            "DSS Import StartTime=\n",
            "DSS Import EndDate=\n",
            "DSS Import EndTime=\n",
            "DSS Import GetInterval= 0 \n",
            "DSS Import Interval=\n",
            "DSS Import GetPeak= 0 \n",
            "DSS Import FillOption= 0 \n",
        ]
        flow_path.write_text("".join(content), encoding="utf-8")

    @staticmethod
    def _write_junction_steady_flow_file(
        flow_path: Path,
        flow_title: str,
        reaches: list[dict[str, Any]],
        junction: dict[str, Any],
        profile_name: str,
        flow_profile: dict[str, float],
        flow_profiles: Optional[list[tuple[str, Any]]] = None,
    ) -> None:
        profiles = (
            [(profile_name, dict(flow_profile))]
            if flow_profiles is None
            else [
                (str(name), dict(values))
                for name, values in flow_profiles
            ]
        )
        profile_names = ",".join(name for name, _ in profiles)
        content = [
            f"Flow Title={flow_title}\n",
            "Program Version=6.70\n",
            f"Number of Profiles= {len(profiles)} \n",
            f"Profile Names={profile_names}\n",
        ]
        reach_flow_keys = ["tributary", "main", "lower"]
        for reach_index, reach_context in enumerate(reaches):
            upstream_station = reach_context["sections"][0].river_station
            flow_key = str(
                reach_context.get(
                    "flow_key",
                    reach_flow_keys[reach_index]
                    if reach_index < len(reach_flow_keys)
                    else f"independent_{reach_index - len(reach_flow_keys) + 1}",
                )
            )
            flow_values_line = "".join(
                f"{HECRAS._format_number(values.get(flow_key, 0.0), decimals=3):>8}"
                for _, values in profiles
            )
            content.extend(
                [
                    (
                        "River Rch & RM="
                        f"{reach_context['river']},{reach_context['reach']:<16},"
                        f"{HECRAS._format_river_station(upstream_station):<8}\n"
                    ),
                    f"{flow_values_line}\n",
                ]
            )

        for reach_index, reach_context in enumerate(reaches):
            flow_key = str(
                reach_context.get(
                    "flow_key",
                    reach_flow_keys[reach_index] if reach_index < len(reach_flow_keys) else "independent",
                )
            )
            boundary_role = str(
                reach_context.get(
                    "boundary_role",
                    (
                        "junction_downstream"
                        if flow_key == "lower"
                        else "junction_upstream"
                        if flow_key in {"main", "tributary"}
                        else "independent"
                    ),
                )
            )
            content.append(
                "Boundary for River Rch & Prof#="
                f"{reach_context['river']},{reach_context['reach']:<16}, 1 \n"
            )
            if boundary_role == "junction_downstream":
                content.extend(
                    [
                        "Up Type= 0 \n",
                        "Dn Type= 3 \n",
                        "Dn Slope="
                        f"{HECRAS._format_number(reach_context['friction_slope'], decimals=5)}\n",
                    ]
                )
            elif boundary_role == "junction_upstream":
                content.extend(
                    [
                        "Up Type= 3 \n",
                        "Up Slope="
                        f"{HECRAS._format_number(reach_context['friction_slope'], decimals=5)}\n",
                        "Dn Type= 0 \n",
                    ]
                )
            else:
                content.extend(
                    [
                        "Up Type= 3 \n",
                        "Up Slope="
                        f"{HECRAS._format_number(reach_context['friction_slope'], decimals=5)}\n",
                        "Dn Type= 3 \n",
                        "Dn Slope="
                        f"{HECRAS._format_number(reach_context['friction_slope'], decimals=5)}\n",
                    ]
                )
        content.extend(
            [
                "DSS Import StartDate=\n",
                "DSS Import StartTime=\n",
                "DSS Import EndDate=\n",
                "DSS Import EndTime=\n",
                "DSS Import GetInterval= 0 \n",
                "DSS Import Interval=\n",
                "DSS Import GetPeak= 0 \n",
                "DSS Import FillOption= 0 \n",
            ]
        )
        flow_path.write_text("".join(content), encoding="utf-8")

    @staticmethod
    def _write_plan_file(
        plan_path: Path,
        plan_title: str,
        project_title: str,
    ) -> None:
        content = [
            f"Plan Title={plan_title}\n",
            "Program Version=6.70\n",
            f"Short Identifier={plan_title[:64]:<64}\n",
            "Simulation Date=,,,\n",
            "Geom File=g01\n",
            "Flow File=f01\n",
            "Mixed Flow\n",
            "K Sum by GR= 0 \n",
            "Std Step Tol= 0.003 \n",
            "Critical Tol= 0.003 \n",
            "Num of Std Step Trials= 20 \n",
            "Max Error Tol= 0.1 \n",
            "Flow Tol Ratio= 0.001 \n",
            "Split Flow NTrial= 30 \n",
            "Split Flow Tol= 0.006 \n",
            "Split Flow Ratio= 0.02 \n",
            "Log Output Level= 0 \n",
            "Friction Slope Method= 1 \n",
            "Unsteady Friction Slope Method= 2 \n",
            "Unsteady Bridges Friction Slope Method= 1 \n",
            "Parabolic Critical Depth\n",
            "Global Vel Dist= 0 , 0 , 0 \n",
            "Global Log Level= 0 \n",
            "CheckData=True\n",
            "Encroach Param=-1 ,0,0, 0 \n",
            "Flow Ratio Target=\n",
            "Flow Ratio Tolerance=0.1\n",
            "Flow Ratio Initial Ratio=\n",
            "Flow Ratio Min Ratio=0.5\n",
            "Flow Ratio Max Ratio=4\n",
            "Flow Ratio Max Iterations= 10 \n",
            "Flow Ratio Reference=\n",
            "Computation Interval=1MIN\n",
            "Output Interval=1HOUR\n",
            "Instantaneous Interval=1HOUR\n",
            "Mapping Interval=1HOUR\n",
            "Computation Time Step Use Courant=        0\n",
            "Computation Time Step Use Time Series=    0\n",
            "Computation Time Step Max Courant=\n",
            "Computation Time Step Min Courant=\n",
            "Computation Time Step Count To Double=0\n",
            "Computation Time Step Max Doubling=0\n",
            "Computation Time Step Max Halving=0\n",
            "Computation Time Step Residence Courant=0\n",
            "Run HTab= 0 \n",
            "Run UNet= 0\n",
            "Run Sediment= 0\n",
            "Run PostProcess= 0\n",
            "Run WQNet= 0 \n",
            "Run RASMapper= 0\n",
            "UNET Theta= 1 \n",
            "UNET Theta Warmup= 1 \n",
            "UNET ZTol= 0.006 \n",
            "UNET ZSATol= 0.006 \n",
            "UNET QTol=\n",
            "UNET MxIter= 20 \n",
            "UNET Max Iter WO Improvement= 0 \n",
            "UNET MaxInSteps= 0 \n",
            "UNET DtIC= 0 \n",
            "UNET DtMin= 0 \n",
            "UNET MaxCRTS= 20 \n",
            "UNET WFStab= 2 \n",
            "UNET SFStab= 1 \n",
            "UNET WFX= 1 \n",
            "UNET SFX= 1 \n",
            "UNET Gravity=9.80665\n",
            "UNET 1D Methodology=Finite Difference\n",
            "UNET DSS MLevel= 4 \n",
            "UNET Pardiso=0\n",
            "UNET DZMax Abort= 30 \n",
            "UNET Use Existing IB Tables=-1 \n",
            "UNET Froude Reduction=False\n",
            "UNET Froude Limit= 0.8 \n",
            "UNET Froude Power= 4 \n",
            "UNET D1 Cores= 0 \n",
            "UNET WindReference=Eulerian\n",
            "UNET WindDragFormulation=Hsu (1988)\n",
            "UNET D2 Coriolis=0\n",
            "UNET D2 Cores= 0 \n",
            "UNET D2 Theta= 1 \n",
            "UNET D2 Theta Warmup= 1 \n",
            "UNET D2 Z Tol= 0.003 \n",
            "UNET D2 Volume Tol= 0.003 \n",
            "UNET D2 Max Iterations= 20 \n",
            "UNET D2 Advanced Convergence=0\n",
            "UNET D2 WS Max Tol=0.045\n",
            "UNET D2 WS RMS Tol=0.006\n",
            "UNET D2 WS Stall Tol=1\n",
            "UNET D2 Equation= 0 \n",
            "UNET D2 TotalICTime=\n",
            "UNET D2 RampUpFraction=0.1\n",
            "UNET D2 TimeSlices= 1 \n",
            "UNET D2 Turbulence Formulation=None\n",
            "UNET D2 Eddy Viscosity=0.3\n",
            "UNET D2 Transverse Eddy Viscosity=0.1\n",
            "UNET D2 Smagorinsky Mixing=0.05\n",
            "UNET D2 BCVolumeCheck=0\n",
            "UNET D2 Latitude=\n",
            "UNET D2 Cores=0\n",
            "UNET D2 SolverType=PARDISO (Direct)\n",
            "UNET D2 Minimum Iterations= 3 \n",
            "UNET D2 Maximum Iterations= 30 \n",
            "UNET D2 Restart Number= 10 \n",
            "UNET D2 Relaxation Coeff=1.3\n",
            "UNET D2 SOR Precondition Iterations= 10 \n",
            "UNET D2 ILUT Maximum Fill= 8 \n",
            "UNET D2 ILUT Tolerance=1E-08\n",
            "UNET D2 Convergence Tolerance=0.00001\n",
            "PS Theta= 1 \n",
            "PS WS Tol= 0.003 \n",
            "PS Volume Tol= 0.003 \n",
            "PS Max Iterations= 20 \n",
            "PS Equation= 0 \n",
            "PS Advance Time Step=-1\n",
            "PS Target Courant=0.9\n",
            "PS Time Slices= 1 \n",
            "PS Iterate With 2D=0\n",
            "PS Project Initial WSE from DS=-1\n",
            "PS Ramp Up Initial WSE from US=-1\n",
            "PS Cores=0\n",
        ]
        plan_path.write_text("".join(content), encoding="utf-8")

    @staticmethod
    def _write_project_file(
        project_path: Path,
        project_title: str,
        geom_title: str,
        flow_title: str,
        plan_title: str,
    ) -> None:
        content = [
            f"Proj Title={project_title}\n",
            "Current Plan=p01\n",
            "Default Exp/Contr=0.3,0.1\n",
            "SI Units\n",
            "Geom File=g01\n",
            "Flow File=f01\n",
            "Plan File=p01\n",
            "Y Axis Title=Elevation\n",
            "X Axis Title(PF)=Main Channel Distance\n",
            "X Axis Title(XS)=Station\n",
            "BEGIN DESCRIPTION:\n",
            "\n",
            "END DESCRIPTION:\n",
            "DSS Start Date=\n",
            "DSS Start Time=\n",
            "DSS End Date=\n",
            "DSS End Time=\n",
            "DSS Export Filename=\n",
            "DSS Export Rating Curves= 0 \n",
            "DSS Export Rating Curve Sorted= 0 \n",
            "DSS Export Volume Flow Curves= 0 \n",
            "DXF Filename=\n",
            "DXF OffsetX= 0 \n",
            "DXF OffsetY= 0 \n",
            "DXF ScaleX= 1 \n",
            "DXF ScaleY= 10 \n",
            "GIS Export Profiles= 0 \n",
        ]
        project_path.write_text("".join(content), encoding="utf-8")

    def _write_sdf_file(
        self,
        sdf_path: Path,
        project_title: str,
        river: str,
        reach: str,
        sections: list[SectionData],
        centerline_points: list[tuple[float, float]],
        centerline_measures: list[float],
    ) -> None:
        all_xy: list[tuple[float, float]] = []
        for section in sections:
            all_xy.extend([(x, y) for x, y in section.line.coords])
        all_xy.extend(centerline_points)

        xmin = min(x for x, _ in all_xy)
        ymin = min(y for _, y in all_xy)
        xmax = max(x for x, _ in all_xy)
        ymax = max(y for _, y in all_xy)

        upstream_min_z = float(sections[0].station_elevation["Elevation"].min())
        downstream_min_z = float(sections[-1].station_elevation["Elevation"].min())
        centerline_station_values = self._build_centerline_station_values(
            centerline_measures,
            sections,
        )

        lines = [
            "BEGIN HEADER:\n",
            "UNITS: METRIC\n",
            "DTM TYPE: GRID\n",
            "DTM: UNKNOWN\n",
            "STREAM LAYER: Centerline\n",
            "NUMBER OF REACHES: 1\n",
            "CROSS-SECTION LAYER: CrossSections\n",
            f"NUMBER OF CROSS-SECTIONS: {len(sections)}\n",
            f"MAP PROJECTION: {self.source_crs}\n",
            "PROJECTION ZONE: UNKNOWN\n",
            "DATUM: UNKNOWN\n",
            "VERTICAL DATUM: UNKNOWN\n",
            "BEGIN SPATIAL EXTENT:\n",
            f"Xmin: {xmin:.3f}\n",
            f"Ymin: {ymin:.3f}\n",
            f"Xmax: {xmax:.3f}\n",
            f"Ymax: {ymax:.3f}\n",
            "END SPATIAL EXTENT:\n",
            "END HEADER:\n",
            "BEGIN STREAM NETWORK:\n",
            (
                "ENDPOINT: "
                f"{centerline_points[0][0]:.3f},"
                f"{centerline_points[0][1]:.3f},"
                f"{upstream_min_z:.3f},1\n"
            ),
            (
                "ENDPOINT: "
                f"{centerline_points[-1][0]:.3f},"
                f"{centerline_points[-1][1]:.3f},"
                f"{downstream_min_z:.3f},2\n"
            ),
            "REACH:\n",
            f"STREAM ID: {river}\n",
            f"REACH ID: {reach}\n",
            "FROM POINT: 1\n",
            "TO POINT: 2\n",
            "CENTERLINE:\n",
        ]

        for (x_value, y_value), station_value in zip(
            centerline_points,
            centerline_station_values,
        ):
            lines.append(
                f"{x_value:.3f},"
                f"{y_value:.3f},"
                "NULL,"
                f"{station_value:.3f}\n"
            )

        lines.extend(
            [
                "END:\n",
                "END STREAM NETWORK:\n",
                "BEGIN CROSS-SECTIONS:\n",
            ]
        )

        for section in sections:
            left_fraction = section.left_bank_station / section.line.length
            right_fraction = section.right_bank_station / section.line.length
            lines.extend(
                [
                    "CROSS-SECTION:\n",
                    f"STREAM ID: {river}\n",
                    f"REACH ID: {reach}\n",
                    f"STATION: {self._sdf_station_value(section):.3f}\n",
                    f"NODE NAME: XS_{section.source_station}\n",
                    "CUT LINE:\n",
                ]
            )

            for x, y in section.line.coords:
                lines.append(f"{x:.3f},{y:.3f}\n")

            lines.append("SURFACE LINE:\n")
            for (x, y), (_, row) in zip(
                section.line.coords,
                section.station_elevation.iterrows(),
            ):
                lines.append(f"{x:.3f},{y:.3f},{float(row['Elevation']):.3f}\n")

            lines.extend(
                [
                    "BANK POSITIONS:\n",
                    f"{left_fraction:.6f},{right_fraction:.6f}\n",
                    "REACH LENGTHS:\n",
                    f"{section.left_reach_length:.3f},"
                    f"{section.channel_reach_length:.3f},"
                    f"{section.right_reach_length:.3f}\n",
                    "N VALUES:\n",
                    f"0.000000,0.050000\n",
                    f"{left_fraction:.6f},0.035000\n",
                    f"{right_fraction:.6f},0.050000\n",
                    "END:\n",
                ]
            )

        lines.extend(
            [
                "END CROSS-SECTIONS:\n",
            ]
        )

        sdf_path.write_text("".join(lines), encoding="utf-8")

    def _write_junction_sdf_file(
        self,
        sdf_path: Path,
        reaches: list[dict[str, Any]],
    ) -> None:
        all_xy: list[tuple[float, float]] = []
        for reach in reaches:
            for section in reach["sections"]:
                all_xy.extend([(x, y) for x, y in section.line.coords])
            all_xy.extend(reach["centerline_points"])

        xmin = min(x for x, _ in all_xy)
        ymin = min(y for _, y in all_xy)
        xmax = max(x for x, _ in all_xy)
        ymax = max(y for _, y in all_xy)
        lines = [
            "BEGIN HEADER:\n",
            "UNITS: METRIC\n",
            "DTM TYPE: GRID\n",
            "DTM: UNKNOWN\n",
            "STREAM LAYER: Centerline\n",
            f"NUMBER OF REACHES: {len(reaches)}\n",
            "CROSS-SECTION LAYER: CrossSections\n",
            f"NUMBER OF CROSS-SECTIONS: {sum(len(reach['sections']) for reach in reaches)}\n",
            f"MAP PROJECTION: {self.source_crs}\n",
            "PROJECTION ZONE: UNKNOWN\n",
            "DATUM: UNKNOWN\n",
            "VERTICAL DATUM: UNKNOWN\n",
            "BEGIN SPATIAL EXTENT:\n",
            f"Xmin: {xmin:.3f}\n",
            f"Ymin: {ymin:.3f}\n",
            f"Xmax: {xmax:.3f}\n",
            f"Ymax: {ymax:.3f}\n",
            "END SPATIAL EXTENT:\n",
            "END HEADER:\n",
            "BEGIN STREAM NETWORK:\n",
        ]
        endpoint_id = 1
        for reach in reaches:
            upstream_min_z = float(reach["sections"][0].station_elevation["Elevation"].min())
            downstream_min_z = float(reach["sections"][-1].station_elevation["Elevation"].min())
            from_point = endpoint_id
            to_point = endpoint_id + 1
            endpoint_id += 2
            lines.extend(
                [
                    (
                        "ENDPOINT: "
                        f"{reach['centerline_points'][0][0]:.3f},"
                        f"{reach['centerline_points'][0][1]:.3f},"
                        f"{upstream_min_z:.3f},{from_point}\n"
                    ),
                    (
                        "ENDPOINT: "
                        f"{reach['centerline_points'][-1][0]:.3f},"
                        f"{reach['centerline_points'][-1][1]:.3f},"
                        f"{downstream_min_z:.3f},{to_point}\n"
                    ),
                    "REACH:\n",
                    f"STREAM ID: {reach['river']}\n",
                    f"REACH ID: {reach['reach']}\n",
                    f"FROM POINT: {from_point}\n",
                    f"TO POINT: {to_point}\n",
                    "CENTERLINE:\n",
                ]
            )
            centerline_station_values = self._build_centerline_station_values(
                reach["centerline_measures"],
                reach["sections"],
            )
            for (x_value, y_value), station_value in zip(
                reach["centerline_points"],
                centerline_station_values,
            ):
                lines.append(
                    f"{x_value:.3f},{y_value:.3f},NULL,{station_value:.3f}\n"
                )
            lines.append("END:\n")
        lines.extend(["END STREAM NETWORK:\n", "BEGIN CROSS-SECTIONS:\n"])
        for reach in reaches:
            for section in reach["sections"]:
                left_fraction = section.left_bank_station / section.line.length
                right_fraction = section.right_bank_station / section.line.length
                lines.extend(
                    [
                        "CROSS-SECTION:\n",
                        f"STREAM ID: {reach['river']}\n",
                        f"REACH ID: {reach['reach']}\n",
                        f"STATION: {self._sdf_station_value(section):.3f}\n",
                        f"NODE NAME: XS_{section.source_station}\n",
                        "CUT LINE:\n",
                    ]
                )
                for x, y in section.line.coords:
                    lines.append(f"{x:.3f},{y:.3f}\n")
                lines.append("SURFACE LINE:\n")
                for (x, y), (_, row) in zip(
                    section.line.coords,
                    section.station_elevation.iterrows(),
                ):
                    lines.append(f"{x:.3f},{y:.3f},{float(row['Elevation']):.3f}\n")
                lines.extend(
                    [
                        "BANK POSITIONS:\n",
                        f"{left_fraction:.6f},{right_fraction:.6f}\n",
                        "REACH LENGTHS:\n",
                        f"{section.left_reach_length:.3f},"
                        f"{section.channel_reach_length:.3f},"
                        f"{section.right_reach_length:.3f}\n",
                        "N VALUES:\n",
                        "0.000000,0.050000\n",
                        f"{left_fraction:.6f},0.035000\n",
                        f"{right_fraction:.6f},0.050000\n",
                        "END:\n",
                    ]
                )
        lines.append("END CROSS-SECTIONS:\n")
        sdf_path.write_text("".join(lines), encoding="utf-8")

    @staticmethod
    def _sdf_station_value(section: SectionData) -> float:
        return section.river_station

    @staticmethod
    def _build_centerline_station_values(
        centerline_measures: list[float],
        sections: list[SectionData],
        tol: float = 1e-9,
    ) -> list[float]:
        if not centerline_measures:
            return []

        section_measures = [section.centerline_measure for section in sections]
        station_values = [section.river_station for section in sections]
        interpolated: list[float] = []
        segment_index = 0

        for measure in centerline_measures:
            while (
                segment_index < len(section_measures) - 2
                and measure > section_measures[segment_index + 1] + tol
            ):
                segment_index += 1

            start_measure = section_measures[segment_index]
            end_measure = section_measures[min(segment_index + 1, len(section_measures) - 1)]
            start_station = station_values[segment_index]
            end_station = station_values[min(segment_index + 1, len(station_values) - 1)]

            if np.isclose(start_measure, end_measure, atol=tol):
                interpolated.append(float(start_station))
                continue

            fraction = (measure - start_measure) / (end_measure - start_measure)
            interpolated.append(
                float(start_station + fraction * (end_station - start_station))
            )

        return interpolated
