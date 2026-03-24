from __future__ import annotations

import logging
import sys
import time
import warnings
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Optional

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import LineString, Point
from shapely.ops import nearest_points

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from ras_commander import init_ras_project, RasCmdr
    from ras_commander.geom import GeomCrossSection

    RAS_COMMANDER_AVAILABLE = True
except ImportError:
    init_ras_project = None
    RasCmdr = None
    GeomCrossSection = None
    RAS_COMMANDER_AVAILABLE = False

logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )


def _format_fixed_width(
    values: list[float],
    *,
    column_width: int,
    values_per_line: int,
    precision: int,
) -> list[str]:
    lines: list[str] = []
    for start in range(0, len(values), values_per_line):
        chunk = values[start : start + values_per_line]
        formatted = "".join(
            f"{float(value):{column_width}.{precision}f}" for value in chunk
        )
        lines.append(f"{formatted}\n")
    return lines

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


class HECRAS:
    """
    Build and optionally run a simple 1D steady HEC-RAS project.

    The main workflow is:
    1. Read cross section XYZ data from CSV.
    2. Read two bank polylines from a shapefile.
    3. Derive bank stations, centerline, and reach lengths.
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
    ) -> BuildResult:
        """
        Build a minimal steady 1D HEC-RAS project in `project_folder`.
        """
        project_folder = Path(project_folder)
        project_folder.mkdir(parents=True, exist_ok=True)

        if projection_file is not None:
            projection_file = Path(projection_file)
            target_projection = project_folder / projection_file.name
            if projection_file.exists() and projection_file.resolve() != target_projection:
                target_projection.write_bytes(projection_file.read_bytes())

        cross_section_df = self._read_cross_sections(cross_section_csv)
        river = str(cross_section_df["River"].iloc[0])
        reach = str(cross_section_df["Reach"].iloc[0])

        section_inputs = self._build_section_inputs(cross_section_df, bank_lines_shp)
        filtered_inputs, skipped_stations = self._filter_near_duplicate_sections(
            section_inputs,
            min_section_spacing=min_section_spacing,
        )

        sections = self._finalize_sections(
            filtered_inputs=filtered_inputs,
            river_station_step=river_station_step,
        )
        self._populate_reach_lengths(sections)
        self._assign_river_stations(sections)
        friction_slope = self._estimate_downstream_friction_slope(sections)

        geometry_path = project_folder / f"{project_stem}.g01"
        flow_path = project_folder / f"{project_stem}.f01"
        plan_path = project_folder / f"{project_stem}.p01"
        project_path = project_folder / f"{project_stem}.prj"
        sdf_path = project_folder / "RASImport.sdf"

        self._write_geometry_file(
            geometry_path=geometry_path,
            geom_title=project_title,
            river=river,
            reach=reach,
            sections=sections,
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
            skipped_source_stations=skipped_stations,
        )
        return self.last_build

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
        if not RAS_COMMANDER_AVAILABLE:
            raise ImportError(
                "ras_commander is required for smoke_test(), but it is not "
                "available in the current environment."
            )

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
            compute_result = RasCmdr.compute_plan(
                plan_number,
                clear_geompre=True,
                force_rerun=True,
                verify=True,
            )
            result["compute_success"] = bool(compute_result)
            result["compute_result"] = repr(compute_result)

        return result

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

    def _build_section_inputs(
        self,
        cross_section_df: pd.DataFrame,
        bank_lines_shp: str | Path,
    ) -> list[dict[str, Any]]:
        bank_gdf = gpd.read_file(bank_lines_shp)
        if bank_gdf.crs:
            self.source_crs = str(bank_gdf.crs)
        bank_lines = [
            geom for geom in bank_gdf.geometry if geom is not None and not geom.is_empty
        ]
        if len(bank_lines) < 2:
            raise ValueError("Bank line shapefile must contain at least two lines.")

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
        return section_inputs

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
    def _assign_bank_sides(section_inputs: list[dict[str, Any]]) -> None:
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
    def _finalize_sections(
        filtered_inputs: list[dict[str, Any]],
        river_station_step: float,
    ) -> list[SectionData]:
        sections: list[SectionData] = []

        for section in filtered_inputs:
            raw_left_bank_station = float(section["left_bank_entry"]["station"])
            raw_right_bank_station = float(section["right_bank_entry"]["station"])
            station_elevation = HECRAS._build_station_elevation(
                section["points"],
                section["z_values"],
                required_stations=[
                    raw_left_bank_station,
                    raw_right_bank_station,
                ],
            )
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
                )
            )

        return sections

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
    def _populate_reach_lengths(sections: list[SectionData]) -> None:
        for idx, section in enumerate(sections):
            if idx == len(sections) - 1:
                section.left_reach_length = 0.0
                section.channel_reach_length = 0.0
                section.right_reach_length = 0.0
                continue

            next_section = sections[idx + 1]
            channel_distance = section.channel_point.distance(
                next_section.channel_point
            )
            section.left_reach_length = channel_distance
            section.channel_reach_length = channel_distance
            section.right_reach_length = channel_distance

    @staticmethod
    def _assign_river_stations(sections: list[SectionData]) -> None:
        if not sections:
            return

        sections[-1].river_station = 0.0
        cumulative_distance = 0.0
        for idx in range(len(sections) - 2, -1, -1):
            cumulative_distance += sections[idx].channel_reach_length
            sections[idx].river_station = float(
                HECRAS._round_half_up(cumulative_distance)
            )

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
        padding: float = 10.0,
    ) -> tuple[float, float, float, float]:
        xs_points = [(x, y) for section in sections for x, y in section.line.coords]
        xs_points.extend(
            (section.channel_point.x, section.channel_point.y) for section in sections
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
        expansion_coeff: float,
        contraction_coeff: float,
        channel_mannings_n: float,
        overbank_mannings_n: float,
    ) -> None:
        xmin, xmax, ymax, ymin = self._viewing_rectangle(sections)
        reach_xy_values: list[float] = []
        for section in sections:
            reach_xy_values.extend([section.channel_point.x, section.channel_point.y])

        lines = [
            f"Geom Title={geom_title}\n",
            "Program Version=6.60\n",
            (
                "Viewing Rectangle= "
                f"{xmin:.4f} , {xmax:.4f} , {ymax:.3f} , {ymin:.3f} \n"
            ),
            "\n",
            f"River Reach={river:<16},{reach:<16}\n",
            f"Reach XY= {len(sections)} \n",
        ]
        lines.extend(
            _format_fixed_width(
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
        if section.channel_reach_length > 0:
            reach_label = (
                f"{HECRAS._format_number(section.channel_reach_length, decimals=1)},"
                f"{HECRAS._format_number(section.channel_reach_length, decimals=1)},"
                f"{HECRAS._format_number(section.channel_reach_length, decimals=1)}"
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
            _format_fixed_width(
                cut_line_values,
                column_width=16,
                values_per_line=4,
                precision=3,
            )
        )
        block.append(f"Node Last Edited Time={timestamp}\n")
        block.append(f"#Sta/Elev= {len(section.station_elevation)} \n")
        block.extend(
            _format_fixed_width(
                sta_elev_values,
                column_width=8,
                values_per_line=10,
                precision=3,
            )
        )
        block.append("#Mann= 3 ,0,0\n")
        block.extend(
            _format_fixed_width(
                mann_values,
                column_width=8,
                values_per_line=9,
                precision=3,
            )
        )
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
    ) -> None:
        all_xy: list[tuple[float, float]] = []
        for section in sections:
            all_xy.extend([(x, y) for x, y in section.line.coords])
            all_xy.append((section.channel_point.x, section.channel_point.y))

        xmin = min(x for x, _ in all_xy)
        ymin = min(y for _, y in all_xy)
        xmax = max(x for x, _ in all_xy)
        ymax = max(y for _, y in all_xy)

        upstream_min_z = float(sections[0].station_elevation["Elevation"].min())
        downstream_min_z = float(sections[-1].station_elevation["Elevation"].min())

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
                f"{sections[0].channel_point.x:.3f},"
                f"{sections[0].channel_point.y:.3f},"
                f"{upstream_min_z:.3f},1\n"
            ),
            (
                "ENDPOINT: "
                f"{sections[-1].channel_point.x:.3f},"
                f"{sections[-1].channel_point.y:.3f},"
                f"{downstream_min_z:.3f},2\n"
            ),
            "REACH:\n",
            f"STREAM ID: {river}\n",
            f"REACH ID: {reach}\n",
            "FROM POINT: 1\n",
            "TO POINT: 2\n",
            "CENTERLINE:\n",
        ]

        for section in sections:
            lines.append(
                f"{section.channel_point.x:.3f},"
                f"{section.channel_point.y:.3f},"
                "NULL,"
                f"{self._sdf_station_value(section):.3f}\n"
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
