from __future__ import annotations

import logging
import sys
import time
import warnings
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Optional

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import LineString, Point
from shapely.ops import linemerge, nearest_points

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ras_commander import init_ras_project, RasCmdr
from ras_commander.geom import GeomCrossSection, GeomParser
from ras_commander.hdf import HdfResultsPlan

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

    def __init__(
        self,
        hecras_version: str = "RAS66.HECRASController",
        ras_exe_path: Optional[str | Path] = None,
    ) -> None:
        self.hecras_version = hecras_version
        self.ras_exe_path = (
            Path(ras_exe_path)
            if ras_exe_path is not None
            else Path(r"C:\Program Files (x86)\HEC\HEC-RAS\6.6\Ras.exe")
        )
        self.hec = None
        self.project_path: Optional[Path] = None
        self.project_name: Optional[str] = None
        self.last_build: Optional[BuildResult] = None
        self.source_crs: str = "UNKNOWN"

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
    ) -> BuildResult:
        """
        Build a minimal steady 1D HEC-RAS project in `project_folder`.
        """
        geometry_context = self._prepare_geometry_context(
            cross_section_csv=cross_section_csv,
            bank_lines_shp=bank_lines_shp,
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

        geometry_context = self._prepare_geometry_context(
            cross_section_csv=cross_section_csv,
            bank_lines_shp=bank_lines_shp,
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
                profile_name=profile_name,
                flow_cms=discharge_cms,
                channel_mannings_n=channel_mannings_n,
                overbank_mannings_n=overbank_mannings_n,
                expansion_coeff=expansion_coeff,
                contraction_coeff=contraction_coeff,
                geometry_context=geometry_context,
            )
            compute_info = self.compute_project(
                project_folder=run_folder,
                project_stem=project_stem,
                plan_number="01",
            )
            out_of_bank = None
            max_bank_excess_m = None
            overflow_sections: list[str] = []
            note = ""
            if compute_info["compute_success"] and compute_info["plan_hdf_file"]:
                overflow_info = self._evaluate_steady_outflow(
                    plan_hdf_path=Path(compute_info["plan_hdf_file"]),
                    sections=geometry_context["sections"],
                    profile_name=profile_name,
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
                profile_name=profile_name,
                flow_cms=final_model_run.discharge_cms,
                channel_mannings_n=channel_mannings_n,
                overbank_mannings_n=overbank_mannings_n,
                expansion_coeff=expansion_coeff,
                contraction_coeff=contraction_coeff,
                geometry_context=geometry_context,
            )
            final_build = final_build_obj.to_dict()
            final_compute_info = self.compute_project(
                project_folder=project_folder,
                project_stem=project_stem,
                plan_number="01",
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

    def compute_project(
        self,
        project_folder: str | Path,
        project_stem: str,
        plan_number: str = "01",
    ) -> dict[str, Any]:
        project_folder = Path(project_folder)
        init_ras_project(
            project_folder,
            str(self.ras_exe_path),
            load_results_summary=False,
        )
        compute_result = RasCmdr.compute_plan(
            plan_number,
            clear_geompre=True,
            force_rerun=True,
            verify=True,
        )
        normalized_plan_number = self._normalize_plan_number(plan_number)
        plan_hdf_path = project_folder / f"{project_stem}.p{normalized_plan_number}.hdf"
        solution = ""
        if plan_hdf_path.exists():
            try:
                solution_df = HdfResultsPlan.get_steady_info(plan_hdf_path)
                solution = str(solution_df.iloc[0].get("Solution", ""))
            except Exception as exc:
                solution = f"Could not read steady info: {exc}"

        return {
            "compute_success": bool(compute_result),
            "compute_result": repr(compute_result),
            "plan_hdf_file": str(plan_hdf_path) if plan_hdf_path.exists() else "",
            "solution": solution,
        }

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

        result: dict[str, Any] = {
            "ras_exe_exists": self.ras_exe_path.exists(),
            "project_initialized": ras_obj.is_initialized,
            "sdf_exists": (project_folder / "RASImport.sdf").exists(),
            "plan_count": int(len(ras_obj.plan_df)),
            "geom_count": int(len(ras_obj.geom_df)),
            "flow_count": int(len(ras_obj.flow_df)),
            "cross_section_count": int(len(xs_df)),
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
        friction_slope = self._estimate_downstream_friction_slope(sections)
        return {
            "river": river,
            "reach": reach,
            "sections": sections,
            "centerline_points": centerline_points,
            "centerline_measures": centerline_measures,
            "centerline_line": LineString(centerline_points),
            "friction_slope": friction_slope,
            "skipped_source_stations": skipped_stations,
            "bank_station_mode": bank_station_mode,
            "river_line_method": river_line_method,
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
    ) -> BuildResult:
        project_folder = Path(project_folder)
        project_folder.mkdir(parents=True, exist_ok=True)

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
        river = geometry_context["river"]
        reach = geometry_context["reach"]
        sections = geometry_context["sections"]
        centerline_points = geometry_context["centerline_points"]
        centerline_measures = geometry_context["centerline_measures"]
        friction_slope = geometry_context["friction_slope"]

        self._write_geometry_file(
            geometry_path=geometry_path,
            geom_title=project_title,
            river=river,
            reach=reach,
            sections=sections,
            centerline_points=centerline_points,
            expansion_coeff=expansion_coeff,
            contraction_coeff=contraction_coeff,
            channel_mannings_n=channel_mannings_n,
            overbank_mannings_n=overbank_mannings_n,
        )
        self._write_steady_flow_file(
            flow_path=flow_path,
            flow_title=project_title,
            river=river,
            reach=reach,
            upstream_river_station=sections[0].river_station,
            downstream_river_station=sections[-1].river_station,
            flow_cms=flow_cms,
            profile_name=profile_name,
            friction_slope=friction_slope,
        )
        self._write_plan_file(
            plan_path=plan_path,
            plan_title=project_title,
            project_title=project_title,
        )
        self._write_project_file(
            project_path=project_path,
            project_title=project_title,
            geom_title=project_title,
            flow_title=project_title,
            plan_title=project_title,
        )
        self._write_sdf_file(
            sdf_path=sdf_path,
            project_title=project_title,
            river=river,
            reach=reach,
            sections=sections,
            centerline_points=centerline_points,
            centerline_measures=centerline_measures,
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
            skipped_source_stations=list(geometry_context["skipped_source_stations"]),
        )
        return self.last_build

    @staticmethod
    def _read_cross_sections(cross_section_csv: str | Path) -> pd.DataFrame:
        csv_path = Path(cross_section_csv)
        df = pd.read_csv(csv_path)

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
            return (
                f"{point_text} Maximum safe flow was {max_safe_run.return_period} "
                f"= {self._format_number(max_safe_run.discharge_cms, decimals=3)} "
                f"cms.{stop_text}"
            )
        if final_model_run is not None and final_model_reason:
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
        text = str(value).strip()
        if not text:
            return "RIVER"
        parts = [part.strip() for part in text.split("-") if part.strip()]
        return parts[-1] if parts else text

    @staticmethod
    def _normalize_reach_name(
        value: Any,
        river_name: str,
        raw_river_name: Optional[str] = None,
    ) -> str:
        text = str(value).strip()
        if not text:
            text = "REACH"
        normalized = ""
        if raw_river_name:
            raw_river_name = str(raw_river_name).strip()
            if raw_river_name and text.startswith(raw_river_name):
                suffix = text[len(raw_river_name):].strip()
                normalized = f"{river_name}{suffix}"
        if not normalized:
            parts = [part.strip() for part in text.split("-") if part.strip()]
            normalized = parts[-1] if parts else text
        if normalized == river_name:
            normalized = f"{normalized}1"
        return normalized

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
        bank_gdf = gpd.read_file(bank_lines_shp)
        if bank_gdf.crs:
            self.source_crs = str(bank_gdf.crs)
        bank_lines = [
            self._coerce_line_string(geom)
            for geom in bank_gdf.geometry
            if geom is not None and not geom.is_empty
        ]
        if len(bank_lines) < 2:
            raise ValueError("Bank line shapefile must contain at least two lines.")

        grouped_bank_lines = self._group_bank_lines_by_connectivity(bank_lines)
        return self._build_section_inputs_from_selected_bank_lines(
            cross_section_df,
            grouped_bank_lines,
        )

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
        self._assign_bank_sides(section_inputs)
        left_bank_line = self._orient_bank_line(section_inputs, side_key="left")
        right_bank_line = self._orient_bank_line(section_inputs, side_key="right")
        return section_inputs, left_bank_line, right_bank_line

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
            "Program Version=6.60\n",
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

        for section in sections:
            lines.extend(
                self._render_cross_section_block(
                    section=section,
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
    ) -> None:
        content = [
            f"Flow Title={flow_title}\n",
            "Program Version=6.60\n",
            "Number of Profiles= 1 \n",
            f"Profile Names={profile_name}\n",
            (
                "River Rch & RM="
                f"{river},{reach:<16},"
                f"{HECRAS._format_river_station(upstream_river_station):<8}\n"
            ),
            f"{HECRAS._format_number(flow_cms, decimals=3):>8}\n",
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
    def _write_plan_file(
        plan_path: Path,
        plan_title: str,
        project_title: str,
    ) -> None:
        content = [
            f"Plan Title={plan_title}\n",
            "Program Version=6.60\n",
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
