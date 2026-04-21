from __future__ import annotations

import argparse
import csv
import math
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd

from steady_1d_python import (
    DEFAULT_NETWORK_DIR,
    DEFAULT_PROFILE_MODES,
    EPS,
    SolverParameters,
    boundary_label,
    canonical_bc_type,
    load_network,
    solve_network,
)


DEFAULT_OUTPUT_DIR = Path("hecras_mtgph_project")
DEFAULT_PROJECT_NAME = "MTGHP_Steady"
DEFAULT_PROGRAM_VERSION = "6.70"
DEFAULT_RIVER_NAME = "MTGHP"
DEFAULT_XS_INTERVAL = 5.0
DEFAULT_XS_MARGIN = 0.2
DEFAULT_XS_MAX_TOP_DEPTH = 10.0
DEFAULT_CENTERLINE_INTERVAL = 1.0
WINDOWS_NEWLINE = "\r\n"


@dataclass
class BoundaryMapping:
    segment: str
    end: str
    source_bc_type: str
    hecras_type_code: int
    hecras_value_label: str
    hecras_value: str
    notes: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a text-based HEC-RAS steady 1D project from MTGHP-core."
    )
    parser.add_argument("--network-dir", type=Path, default=DEFAULT_NETWORK_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--project-name", default=DEFAULT_PROJECT_NAME)
    parser.add_argument("--river-name", default=DEFAULT_RIVER_NAME)
    parser.add_argument("--program-version", default=DEFAULT_PROGRAM_VERSION)
    parser.add_argument("--total-q", type=float, default=None)
    parser.add_argument("--xs-interval", type=float, default=DEFAULT_XS_INTERVAL)
    parser.add_argument("--xs-margin", type=float, default=DEFAULT_XS_MARGIN)
    parser.add_argument("--centerline-interval", type=float, default=DEFAULT_CENTERLINE_INTERVAL)
    return parser.parse_args()


def pad_name(name: str, width: int = 16) -> str:
    return f"{str(name).strip():<{width}}"[:width]


def trim_name(name: str, width: int = 16) -> str:
    return str(name).strip()[:width]


def segment_river_name(segment, default_river_name: str = "") -> str:
    river_name = trim_name(default_river_name or DEFAULT_RIVER_NAME)
    if river_name:
        return river_name
    return trim_name(getattr(segment, "name", segment)) or trim_name(DEFAULT_RIVER_NAME)


def segment_reach_name(segment) -> str:
    segment_name = trim_name(getattr(segment, "name", segment))
    return segment_name or "Reach"


def format_rs(value: float) -> str:
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return text if text else "0"


def format_numeric(value: float, width: int, decimals: int) -> str:
    text = f"{float(value):.{decimals}f}"
    if len(text) > width:
        text = f"{float(value):.{max(0, decimals - 2)}f}"
    if len(text) > width:
        text = f"{float(value):.3E}"
    return f"{text:>{width}}"


def format_fixed_width(
    values: Sequence[float],
    width: int,
    per_line: int,
    decimals: int,
) -> List[str]:
    lines: List[str] = []
    chunk: List[str] = []
    for value in values:
        chunk.append(format_numeric(value, width=width, decimals=decimals))
        if len(chunk) == per_line:
            lines.append("".join(chunk) + "\n")
            chunk = []
    if chunk:
        lines.append("".join(chunk) + "\n")
    return lines


def iter_spill_rows(segment) -> Iterable[Dict[str, object]]:
    df = segment.df.reset_index(drop=True)
    for idx, rec in df.iterrows():
        left_on = str(rec.get("SpillLeftOn", "")).strip().lower() in {"true", "yes", "1"}
        right_on = str(rec.get("SpillRightOn", "")).strip().lower() in {"true", "yes", "1"}
        left_to = str(rec.get("SpillLeftTo", "")).strip()
        right_to = str(rec.get("SpillRightTo", "")).strip()
        if left_on and left_to:
            yield {
                "segment": segment.name,
                "side": "left",
                "sn": int(rec["SN"]),
                "chainage": float(rec["Chainage"]),
                "target": left_to,
                "crest": rec.get("SpillLeftCrest"),
            }
        if right_on and right_to:
            yield {
                "segment": segment.name,
                "side": "right",
                "sn": int(rec["SN"]),
                "chainage": float(rec["Chainage"]),
                "target": right_to,
                "crest": rec.get("SpillRightCrest"),
            }


def compute_view_rectangle(segments) -> Tuple[float, float, float, float]:
    eastings: List[float] = []
    northings: List[float] = []
    for segment in segments:
        eastings.extend(segment.df["Easting"].astype(float).tolist())
        northings.extend(segment.df["Northing"].astype(float).tolist())
    min_x = min(eastings)
    max_x = max(eastings)
    min_y = min(northings)
    max_y = max(northings)
    pad_x = max((max_x - min_x) * 0.05, 10.0)
    pad_y = max((max_y - min_y) * 0.05, 10.0)
    return min_x - pad_x, max_x + pad_x, max_y + pad_y, min_y - pad_y


def is_internal_node(node_name: str) -> bool:
    return str(node_name).strip().lower() not in {"inlet", "outlet", "spill"}


def tangent_vector(points: Sequence[Tuple[float, float]], index: int) -> Tuple[float, float]:
    if len(points) == 1:
        return 1.0, 0.0
    if index == 0:
        dx = points[1][0] - points[0][0]
        dy = points[1][1] - points[0][1]
    elif index == len(points) - 1:
        dx = points[-1][0] - points[-2][0]
        dy = points[-1][1] - points[-2][1]
    else:
        dx = points[index + 1][0] - points[index - 1][0]
        dy = points[index + 1][1] - points[index - 1][1]
    norm = math.hypot(dx, dy)
    if norm < EPS:
        return 1.0, 0.0
    return dx / norm, dy / norm


def interpolate_linear(x_values: Sequence[float], y_values: Sequence[float], target: float) -> float:
    if not x_values or not y_values:
        return 0.0
    if len(x_values) == 1:
        return float(y_values[0])
    if target <= x_values[0] + EPS:
        return float(y_values[0])
    if target >= x_values[-1] - EPS:
        return float(y_values[-1])

    right_index = bisect_right(x_values, target)
    left_index = max(right_index - 1, 0)
    right_index = min(right_index, len(x_values) - 1)
    x0 = float(x_values[left_index])
    x1 = float(x_values[right_index])
    y0 = float(y_values[left_index])
    y1 = float(y_values[right_index])
    if abs(x1 - x0) < EPS:
        return y0
    ratio = (target - x0) / (x1 - x0)
    return y0 + ratio * (y1 - y0)


def nearest_chainage_index(chainages: Sequence[float], target: float) -> int:
    if not chainages:
        return 0
    if len(chainages) == 1:
        return 0
    if target <= chainages[0] + EPS:
        return 0
    if target >= chainages[-1] - EPS:
        return len(chainages) - 1

    right_index = bisect_right(chainages, target)
    left_index = max(right_index - 1, 0)
    right_index = min(right_index, len(chainages) - 1)
    if abs(chainages[left_index] - target) <= abs(chainages[right_index] - target):
        return left_index
    return right_index


def interpolate_centerline_point(
    chainages: Sequence[float],
    points: Sequence[Tuple[float, float]],
    target: float,
) -> Tuple[float, float]:
    xs = [float(x) for x, _ in points]
    ys = [float(y) for _, y in points]
    return (
        interpolate_linear(chainages, xs, target),
        interpolate_linear(chainages, ys, target),
    )


def dedupe_sorted_values(values: Sequence[float], tolerance: float = 1e-6) -> List[float]:
    result: List[float] = []
    for value in sorted(float(item) for item in values):
        if result and abs(value - result[-1]) <= tolerance:
            continue
        result.append(value)
    return result


def build_reach_centerline_chainages(
    segment,
    section_chainages: Sequence[float],
    centerline_interval: float,
) -> List[float]:
    original_chainages = segment.df["Chainage"].astype(float).tolist()
    length = float(getattr(segment, "length", 0.0))
    if length <= EPS:
        return [0.0]

    spacing = max(float(centerline_interval), 0.25)
    chainages: List[float] = [0.0, length]
    chainages.extend(original_chainages)
    chainages.extend(float(value) for value in section_chainages)

    cursor = spacing
    while cursor < length - EPS:
        chainages.append(cursor)
        cursor += spacing

    return dedupe_sorted_values(chainages)


def build_reach_centerline_points(
    original_chainages: Sequence[float],
    original_points: Sequence[Tuple[float, float]],
    reach_chainages: Sequence[float],
) -> List[Tuple[float, float]]:
    points: List[Tuple[float, float]] = []
    for chainage in reach_chainages:
        point = interpolate_centerline_point(original_chainages, original_points, chainage)
        if points and math.hypot(point[0] - points[-1][0], point[1] - points[-1][1]) <= 1e-6:
            continue
        points.append(point)
    return points


def build_cut_line(
    points: Sequence[Tuple[float, float]],
    chainages: Sequence[float],
    target_chainage: float,
    half_length: float,
) -> List[Tuple[float, float]]:
    cx, cy = interpolate_centerline_point(chainages, points, target_chainage)
    nearest_idx = nearest_chainage_index(chainages, target_chainage)
    tx, ty = tangent_vector(points, nearest_idx)
    nx, ny = -ty, tx
    return [
        (cx + nx * half_length, cy + ny * half_length),
        (cx - nx * half_length, cy - ny * half_length),
    ]


def build_sta_elev_points(
    bed_width: float,
    bed_elev: float,
    top_depth: float,
    margin_offset: float = DEFAULT_XS_MARGIN,
) -> Tuple[List[Tuple[float, float]], float, float]:
    depth = min(max(top_depth, 1.0), DEFAULT_XS_MAX_TOP_DEPTH)
    bed_width_eff = max(bed_width, 0.5)
    offset = max(margin_offset, 0.05)
    left_bank = offset
    right_bank = left_bank + bed_width_eff
    total_width = right_bank + offset
    top_elev = bed_elev + depth
    points = [
        (0.0, top_elev),
        (left_bank, top_elev),
        (left_bank, bed_elev),
        (right_bank, bed_elev),
        (right_bank, top_elev),
        (total_width, top_elev),
    ]
    return points, left_bank, right_bank


def sample_section_chainages(segment, interval: float) -> List[float]:
    length = float(getattr(segment, "length", 0.0))
    if length <= EPS:
        return [0.0]

    spacing = max(float(interval), 0.1)
    start_offset = min(spacing, length / 2.0) if is_internal_node(segment.from_node) else 0.0
    end_offset = min(spacing, length / 2.0) if is_internal_node(segment.to_node) else 0.0
    start_limit = start_offset
    end_limit = max(start_limit, length - end_offset)

    chainages = [start_limit, end_limit]
    cursor = spacing
    while cursor < length - EPS:
        if start_limit + EPS < cursor < end_limit - EPS:
            chainages.append(cursor)
        cursor += spacing

    unique_chainages: List[float] = []
    for value in sorted(round(float(chainage), 6) for chainage in chainages):
        if unique_chainages and abs(value - unique_chainages[-1]) < 1e-6:
            continue
        unique_chainages.append(value)

    min_spacing = spacing * 0.75
    while len(unique_chainages) > 2 and unique_chainages[1] - unique_chainages[0] < min_spacing - EPS:
        unique_chainages.pop(1)
    while len(unique_chainages) > 2 and unique_chainages[-1] - unique_chainages[-2] < min_spacing - EPS:
        unique_chainages.pop(-2)

    return unique_chainages


def interpolate_profile_row(profile_df: pd.DataFrame, chainage: float) -> Dict[str, float]:
    chainages = profile_df["chainage"].astype(float).tolist()
    interpolated = {"chainage": float(chainage)}
    for column in ("bed_width", "side_slope", "bed_elevation", "y_final", "ws_final"):
        interpolated[column] = interpolate_linear(
            chainages,
            profile_df[column].astype(float).tolist(),
            chainage,
        )
    return interpolated


def get_boundary_lookup(boundary_df: pd.DataFrame) -> Dict[str, Dict[str, object]]:
    return {
        str(row["label"]): row.to_dict()
        for _, row in boundary_df.iterrows()
    }


def get_summary_lookup(summary_df: pd.DataFrame) -> Dict[str, Dict[str, object]]:
    return {
        str(row["segment"]): row.to_dict()
        for _, row in summary_df.iterrows()
    }


def boundary_mapping_for_segment(
    segment,
    summary_row: Dict[str, object],
    boundary_lookup: Dict[str, Dict[str, object]],
    params: SolverParameters,
) -> List[BoundaryMapping]:
    mappings: List[BoundaryMapping] = []

    def add_mapping(end: str, source_type: str, code: int, label: str, value: str, notes: str) -> None:
        mappings.append(
            BoundaryMapping(
                segment=segment.name,
                end=end,
                source_bc_type=source_type,
                hecras_type_code=code,
                hecras_value_label=label,
                hecras_value=value,
                notes=notes,
            )
        )

    for end in ("start", "end"):
        external = (end == "start" and segment.from_node.lower() == "inlet") or (
            end == "end" and segment.to_node.lower() == "outlet"
        )
        if not external:
            add_mapping(end, "Internal", 0, "", "", "Internal junction end, no external steady boundary.")
            continue

        label = boundary_label(segment.name, end)
        boundary_row = boundary_lookup[label]
        bc_type = canonical_bc_type(boundary_row.get("bc_type"))
        resolved_stage = float(boundary_row["resolved_stage"])
        bed = segment.start_bed if end == "start" else segment.end_bed
        slope = max(abs((segment.end_bed - segment.start_bed) / max(segment.length, EPS)), params.boundary_slope)

        if bc_type == "Known WSE":
            add_mapping(end, bc_type, 1, "Known WS", f"{float(boundary_row['bc_value']):.3f}", "Directly from master.xlsx.")
        elif bc_type == "Known Depth":
            depth = float(boundary_row["bc_value"])
            add_mapping(end, bc_type, 1, "Known WS", f"{bed + depth:.3f}", "Converted from known depth to known water surface elevation.")
        elif bc_type == "Normal Depth" or not bc_type:
            add_mapping(end, "Normal Depth", 3, "Slope", f"{slope:.6f}", "Normal-depth approximation using local bed slope.")
        elif bc_type == "Critical Depth":
            add_mapping(end, bc_type, 2, "", "", "Critical-depth steady boundary.")
        elif bc_type == "None":
            add_mapping(
                end,
                bc_type,
                1,
                "Known WS",
                f"{float(summary_row['ws_start'] if end == 'start' else summary_row['ws_end']):.3f}",
                "Closed outlet approximated with zero-flow branch and solved-equivalent stage placeholder. Add a true gate/inline structure in HEC-RAS if needed.",
            )
        elif bc_type == "Spill End":
            add_mapping(
                end,
                bc_type,
                1,
                "Known WS",
                f"{float(summary_row['ws_start'] if end == 'start' else summary_row['ws_end']):.3f}",
                "Spill-end branch approximated with solved-equivalent outlet stage; side spill itself is exported separately for manual lateral-structure refinement.",
            )
        elif bc_type == "Spill Zero":
            add_mapping(
                end,
                bc_type,
                1,
                "Known WS",
                f"{float(summary_row['ws_start'] if end == 'start' else summary_row['ws_end']):.3f}",
                "Spill-zero inlet approximated with solved-equivalent stage so the HEC-RAS run has a valid mixed-flow upstream boundary.",
            )
        else:
            add_mapping(end, bc_type or "Unknown", 3, "Slope", f"{slope:.6f}", "Fallback to normal-depth boundary.")

    return mappings


def build_project_file(
    project_name: str,
) -> str:
    return (
        f"Proj Title={project_name}\n"
        "Current Geom=g01\n"
        "Current Flow=f01\n"
        "Default Exp/Contr=0.3,0.1\n"
        "SI Units\n"
        "Geom File=g01\n"
        "Flow File=f01\n"
        "Plan File=p01\n"
        "Y Axis Title=Elevation\n"
        "X Axis Title(PF)=Main Channel Distance\n"
        "X Axis Title(XS)=Station\n"
        "BEGIN DESCRIPTION:\n"
        "Auto-generated from MTGHP-core and steady_1d_python.\n"
        "END DESCRIPTION:\n"
    )


def build_plan_file(
    project_name: str,
    program_version: str,
) -> str:
    short_id = project_name[:30]
    lines = [
        f"Plan Title={project_name}\n",
        f"Program Version={program_version}\n",
        f"Short Identifier={short_id:<24}\n",
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
        "Parabolic Critical Depth\n",
        "Global Vel Dist= 0 , 0 , 0 \n",
        "CheckData=True\n",
        "Computation Interval=1MIN\n",
        "Output Interval=1HOUR\n",
        "Instantaneous Interval=1HOUR\n",
        "Mapping Interval=1HOUR\n",
        "Run HTab= 0 \n",
        "Run UNet= 0 \n",
        "Run Sediment= 0 \n",
        "Run PostProcess= 0 \n",
        "Run WQNet= 0 \n",
    ]
    return "".join(lines)


def build_geometry_file(
    segments,
    solution: Dict[str, object],
    params: SolverParameters,
    river_name: str,
    geom_title: str,
    program_version: str,
    xs_interval: float,
    xs_margin: float,
    centerline_interval: float,
) -> str:
    profiles: Dict[str, pd.DataFrame] = solution["profiles"]  # type: ignore[assignment]
    min_x, max_x, max_y, min_y = compute_view_rectangle(segments)

    now_stamp = datetime.now().strftime("%b/%d/%Y %H:%M:%S")
    lines: List[str] = [
        f"Geom Title={geom_title}\n",
        f"Program Version={program_version}\n",
        f"Viewing Rectangle= {min_x:.6f} , {max_x:.6f} , {max_y:.6f} , {min_y:.6f} \n",
        "\n",
    ]

    junctions: Dict[str, Dict[str, List[object]]] = {}
    for segment in segments:
        if segment.from_node.lower() not in {"inlet", "outlet"}:
            junctions.setdefault(segment.from_node, {"up": [], "down": []})["down"].append(segment)
        if segment.to_node.lower() not in {"inlet", "outlet"}:
            junctions.setdefault(segment.to_node, {"up": [], "down": []})["up"].append(segment)

    for node_name, data in junctions.items():
        coords: List[Tuple[float, float]] = []
        for segment in data["up"]:
            coords.append((float(segment.df["Easting"].iloc[-1]), float(segment.df["Northing"].iloc[-1])))
        for segment in data["down"]:
            coords.append((float(segment.df["Easting"].iloc[0]), float(segment.df["Northing"].iloc[0])))
        jx = sum(pt[0] for pt in coords) / max(len(coords), 1)
        jy = sum(pt[1] for pt in coords) / max(len(coords), 1)
        lines.append(f"Junct Name={pad_name(node_name, 16)}\n")
        lines.append("Junct Desc=, 0 , 0 , 0 ,0\n")
        lines.append(f"Junct X Y & Text X Y={jx:.6f},{jy:.6f},{jx:.6f},{jy:.6f}\n")
        for segment in data["up"]:
            segment_river = segment_river_name(segment, river_name)
            segment_reach = segment_reach_name(segment)
            lines.append(f"Up River,Reach={pad_name(segment_river)},{pad_name(segment_reach)}\n")
        for segment in data["down"]:
            segment_river = segment_river_name(segment, river_name)
            segment_reach = segment_reach_name(segment)
            lines.append(f"Dn River,Reach={pad_name(segment_river)},{pad_name(segment_reach)}\n")
        for _ in range(max(len(data["up"]), len(data["down"]))):
            lines.append("Junc L&A=0,0\n")
        lines.append("\n")

    for segment in segments:
        segment_river = segment_river_name(segment, river_name)
        segment_reach = segment_reach_name(segment)
        profile = profiles[segment.name].copy().sort_values("chainage").reset_index(drop=True)
        source_centerline = list(
            zip(
                segment.df["Easting"].astype(float).tolist(),
                segment.df["Northing"].astype(float).tolist(),
            )
        )
        source_centerline_chainages = segment.df["Chainage"].astype(float).tolist()
        section_chainages = sample_section_chainages(segment, xs_interval)
        centerline_chainages = build_reach_centerline_chainages(segment, section_chainages, centerline_interval)
        centerline = build_reach_centerline_points(
            source_centerline_chainages,
            source_centerline,
            centerline_chainages,
        )
        text_x = sum(pt[0] for pt in centerline) / len(centerline)
        text_y = sum(pt[1] for pt in centerline) / len(centerline)
        lines.append(f"River Reach={pad_name(segment_river)},{pad_name(segment_reach)}\n")
        lines.append(f"Reach XY= {len(centerline)} \n")
        reach_xy_values: List[float] = []
        for x, y in centerline:
            reach_xy_values.extend([x, y])
        lines.extend(format_fixed_width(reach_xy_values, width=16, per_line=4, decimals=6))
        lines.append(f"Rch Text X Y={text_x:.6f},{text_y:.6f}\n")
        lines.append("Reverse River Text= 0 \n")
        lines.append("\n")

        downstream_reference_chainage: Optional[float] = None
        for chainage in sorted(section_chainages, reverse=True):
            row = interpolate_profile_row(profile, chainage)
            rs = format_rs(segment.length - chainage)
            if downstream_reference_chainage is None:
                length_part = ",,,"
            else:
                dx = max(downstream_reference_chainage - chainage, 0.1)
                length_part = f",{dx:.3f},{dx:.3f},{dx:.3f}"
            lines.append(f"Type RM Length L Ch R = 1 ,{rs:<8}{length_part}\n")

            bed_width = float(row["bed_width"])
            bed_elev = float(row["bed_elevation"])
            top_depth = max(float(row["y_final"]) + 2.0, float(row["ws_final"]) - bed_elev + 1.0)
            sta_elev_points, bank_left, bank_right = build_sta_elev_points(
                bed_width=bed_width,
                bed_elev=bed_elev,
                top_depth=top_depth,
                margin_offset=xs_margin,
            )
            half_length = max((max(bed_width, 0.5) / 2.0) + max(xs_margin, 0.05), 0.25)
            cut_line = build_cut_line(
                centerline,
                centerline_chainages,
                target_chainage=chainage,
                half_length=half_length,
            )

            lines.append(f"XS GIS Cut Line={len(cut_line)}\n")
            cut_values: List[float] = []
            for x, y in cut_line:
                cut_values.extend([x, y])
            lines.extend(format_fixed_width(cut_values, width=16, per_line=4, decimals=6))
            lines.append(f"Node Last Edited Time={now_stamp}\n")

            lines.append(f"#Sta/Elev= {len(sta_elev_points)} \n")
            sta_elev_values: List[float] = []
            for sta, elev in sta_elev_points:
                sta_elev_values.extend([sta, elev])
            lines.extend(format_fixed_width(sta_elev_values, width=8, per_line=10, decimals=3))

            lines.append("#Mann= 3 ,0,0\n")
            mann_values = [0.0, params.manning_n, 0.0, bank_left, params.manning_n, 0.0, bank_right, params.manning_n, 0.0]
            lines.extend(format_fixed_width(mann_values, width=8, per_line=10, decimals=3))
            lines.append(f"Bank Sta={bank_left:.3f},{bank_right:.3f}\n")
            lines.append("XS Rating Curve= 0 ,0\n")
            start_el = min(pt[1] for pt in sta_elev_points) + 0.15
            incr = max((max(pt[1] for pt in sta_elev_points) - min(pt[1] for pt in sta_elev_points)) / 20.0, 0.20)
            lines.append(f"XS HTab Starting El and Incr={start_el:.4f},{incr:.2f}, 20 \n")
            lines.append("XS HTab Horizontal Distribution= 5 , 5 , 5 \n")
            lines.append("Exp/Cntr=0.3,0.1\n")
            lines.append("\n")
            downstream_reference_chainage = chainage

    return "".join(lines)


def build_flow_file(
    segments,
    solution: Dict[str, object],
    params: SolverParameters,
    river_name: str,
    flow_title: str,
    program_version: str,
    xs_interval: float,
) -> Tuple[str, List[BoundaryMapping], List[Dict[str, object]]]:
    boundary_lookup = get_boundary_lookup(solution["boundaries"])  # type: ignore[arg-type]
    summary_lookup = get_summary_lookup(solution["segment_summary"])  # type: ignore[arg-type]
    prescribed_flows: List[Dict[str, object]] = []
    boundary_mappings: List[BoundaryMapping] = []

    lines = [
        f"Flow Title={flow_title}\n",
        f"Program Version={program_version}\n",
        "BEGIN FILE DESCRIPTION:\n",
        "Auto-generated from MTGHP-core and steady_1d_python.\n",
        "Flow splits and spill-fed drain inflow are prescribed from the offline network solution.\n",
        "END FILE DESCRIPTION:\n",
        "Number of Profiles= 1 \n",
        "Profile Names=Base\n",
    ]

    for segment in segments:
        segment_river = segment_river_name(segment, river_name)
        segment_reach = segment_reach_name(segment)
        summary_row = summary_lookup[segment.name]
        q_in = float(summary_row["q_in"])
        if segment.name == "drain" and q_in <= EPS:
            q_in = float(summary_row.get("q_lateral_in_total", 0.0))
        section_chainages = sample_section_chainages(segment, xs_interval)
        upstream_rs = format_rs(segment.length - min(section_chainages))
        lines.append(f"River Rch & RM={segment_river},{segment_reach},{upstream_rs:<8}\n")
        lines.extend(format_fixed_width([q_in], width=8, per_line=10, decimals=3))
        prescribed_flows.append(
            {
                "segment": segment.name,
                "river": segment_river,
                "reach": segment_reach,
                "river_station": upstream_rs,
                "prescribed_q": q_in,
            }
        )

    for segment in segments:
        segment_river = segment_river_name(segment, river_name)
        segment_reach = segment_reach_name(segment)
        mappings = boundary_mapping_for_segment(segment, summary_lookup[segment.name], boundary_lookup, params)
        boundary_mappings.extend(mappings)
        map_by_end = {mapping.end: mapping for mapping in mappings}
        lines.append(f"Boundary for River Rch & Prof#={segment_river},{segment_reach}, 1 \n")

        up_map = map_by_end["start"]
        lines.append(f"Up Type= {up_map.hecras_type_code} \n")
        if up_map.hecras_value_label == "Slope":
            lines.append(f"Up Slope={up_map.hecras_value}\n")
        elif up_map.hecras_value_label == "Known WS":
            lines.append(f"Up Known WS={up_map.hecras_value}\n")

        dn_map = map_by_end["end"]
        lines.append(f"Dn Type= {dn_map.hecras_type_code} \n")
        if dn_map.hecras_value_label == "Slope":
            lines.append(f"Dn Slope={dn_map.hecras_value}\n")
        elif dn_map.hecras_value_label == "Known WS":
            lines.append(f"Dn Known WS={dn_map.hecras_value}\n")

    lines.extend(
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

    return "".join(lines), boundary_mappings, prescribed_flows


def write_text(path: Path, content: str) -> None:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    with path.open("w", encoding="utf-8", newline="") as f:
        f.write(normalized.replace("\n", WINDOWS_NEWLINE))


def write_csv(path: Path, rows: List[Dict[str, object]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8", newline="")
        return
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=list(rows[0].keys()),
            lineterminator=WINDOWS_NEWLINE,
        )
        writer.writeheader()
        writer.writerows(rows)


def archive_stale_derived_geometry(output_dir: Path, project_name: str) -> List[Dict[str, str]]:
    backup_dir = output_dir / "Backup"
    timestamp = datetime.now().strftime("%Y-%b-%d_%H%M%S")
    archived: List[Dict[str, str]] = []
    for suffix in ("g01.hdf", "p01.hdf"):
        path = output_dir / f"{project_name}.{suffix}"
        if not path.exists():
            continue
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / f"{project_name}.{timestamp}.{suffix}"
        counter = 1
        while backup_path.exists():
            backup_path = backup_dir / f"{project_name}.{timestamp}({counter}).{suffix}"
            counter += 1
        path.replace(backup_path)
        archived.append({"source": str(path), "backup": str(backup_path)})
    return archived


def expected_cross_section_count(segments, xs_interval: float) -> int:
    return sum(len(sample_section_chainages(segment, xs_interval)) for segment in segments)


def validate_project(
    output_dir: Path,
    project_name: str,
    expected_reaches: int,
    expected_junctions: int,
    expected_cross_sections: int,
) -> Dict[str, object]:
    validation: Dict[str, object] = {
        "files_ok": False,
        "cross_section_count": 0,
        "reach_count": 0,
        "junction_count": 0,
    }
    prj_path = output_dir / f"{project_name}.prj"
    geom_path = output_dir / f"{project_name}.g01"
    flow_path = output_dir / f"{project_name}.f01"
    plan_path = output_dir / f"{project_name}.p01"
    if not all(path.exists() for path in (prj_path, geom_path, flow_path, plan_path)):
        validation["error"] = "One or more required HEC-RAS files were not written."
        return validation

    prj_text = prj_path.read_text(encoding="utf-8", errors="ignore")
    geom_text = geom_path.read_text(encoding="utf-8", errors="ignore")
    flow_text = flow_path.read_text(encoding="utf-8", errors="ignore")
    plan_text = plan_path.read_text(encoding="utf-8", errors="ignore")

    validation["files_ok"] = all(
        marker in prj_text
        for marker in (
            "Proj Title=",
            "Geom File=g01",
            "Flow File=f01",
            "Plan File=p01",
        )
    ) and all(
        marker in plan_text
        for marker in (
            "Plan Title=",
            "Geom File=g01",
            "Flow File=f01",
            "Mixed Flow",
        )
    ) and "Flow Title=" in flow_text

    validation["reach_count"] = geom_text.count("River Reach=")
    validation["junction_count"] = geom_text.count("Junct Name=")
    validation["cross_section_count"] = geom_text.count("Type RM Length L Ch R =")
    validation["expected_reach_count"] = expected_reaches
    validation["expected_junction_count"] = expected_junctions
    validation["expected_cross_section_count"] = expected_cross_sections
    validation["counts_match"] = (
        validation["reach_count"] == expected_reaches
        and validation["junction_count"] == expected_junctions
        and validation["cross_section_count"] == expected_cross_sections
    )
    return validation


def generate_project(
    network_dir: Path,
    output_dir: Path,
    project_name: str,
    river_name: str,
    program_version: str,
    total_q: Optional[float] = None,
    xs_interval: float = DEFAULT_XS_INTERVAL,
    xs_margin: float = DEFAULT_XS_MARGIN,
    centerline_interval: float = DEFAULT_CENTERLINE_INTERVAL,
) -> Dict[str, object]:
    params = SolverParameters()
    segments, boundaries, detected_total_q = load_network(network_dir)
    if total_q is not None:
        params.total_inflow_q = total_q
    elif detected_total_q is not None:
        params.total_inflow_q = detected_total_q

    solution = solve_network(segments, boundaries, params, DEFAULT_PROFILE_MODES)

    output_dir.mkdir(parents=True, exist_ok=True)
    archived_derived = archive_stale_derived_geometry(output_dir, project_name)
    prj_path = output_dir / f"{project_name}.prj"
    geom_path = output_dir / f"{project_name}.g01"
    flow_path = output_dir / f"{project_name}.f01"
    plan_path = output_dir / f"{project_name}.p01"

    write_text(prj_path, build_project_file(project_name))
    write_text(
        geom_path,
        build_geometry_file(
            segments=segments,
            solution=solution,
            params=params,
            river_name=river_name,
            geom_title=f"{project_name}_Geometry",
            program_version=program_version,
            xs_interval=xs_interval,
            xs_margin=xs_margin,
            centerline_interval=centerline_interval,
        ),
    )
    flow_text, boundary_mappings, prescribed_flows = build_flow_file(
        segments=segments,
        solution=solution,
        params=params,
        river_name=river_name,
        flow_title=f"{project_name}_Flow",
        program_version=program_version,
        xs_interval=xs_interval,
    )
    write_text(flow_path, flow_text)
    write_text(plan_path, build_plan_file(project_name, program_version))

    write_csv(output_dir / "boundary_mapping.csv", [mapping.__dict__ for mapping in boundary_mappings])
    write_csv(output_dir / "prescribed_flows.csv", prescribed_flows)
    spill_rows: List[Dict[str, object]] = []
    for segment in segments:
        spill_rows.extend(list(iter_spill_rows(segment)))
    write_csv(output_dir / "spill_inventory.csv", spill_rows)

    summary_df: pd.DataFrame = solution["segment_summary"]  # type: ignore[assignment]
    with (output_dir / "reference_segment_summary.csv").open("w", encoding="utf-8", newline="") as f:
        summary_df.to_csv(f, index=False, lineterminator=WINDOWS_NEWLINE)

    note_lines = [
        "This project is auto-generated from MTGHP-core and steady_1d_python.py.",
        "",
        "Important approximations:",
        "1. Split discharges and drain inflow are prescribed from the offline solver results.",
        "2. 'None', 'Spill End', and 'Spill Zero' master BCs are translated into solved-equivalent HEC-RAS placeholder stages so the steady model has runnable external boundaries.",
        f"3. Cross sections are exported on approximately {xs_interval:.2f} m spacing, with internal junction ends held back where needed to reduce crossing and bow-tie issues.",
        f"4. River centerlines are rebuilt from the MTGHP-core CSV coordinates plus every exported cross-section intersection at no more than {centerline_interval:.2f} m spacing, so bends remain curved in RAS Mapper.",
        f"5. GIS cut lines and station/elevation templates are limited to the bed width plus {xs_margin:.2f} m offset on each side.",
        "6. Spill locations are exported in spill_inventory.csv for manual refinement into true lateral structures if you want native HEC-RAS spill mechanics instead of prescribed-flow equivalence.",
    ]
    if archived_derived:
        note_lines.extend(
            [
                "",
                "Derived HEC-RAS geometry caches archived:",
                *[f"- {row['source']} -> {row['backup']}" for row in archived_derived],
                "Open the project in HEC-RAS and let it rebuild the geometry HDF so RAS Mapper uses the current curved centerlines.",
            ]
        )
    write_text(output_dir / "README.txt", "\n".join(note_lines) + "\n")

    internal_nodes = sorted(
        {
            segment.from_node
            for segment in segments
            if segment.from_node.lower() not in {"inlet", "outlet"}
        }
        | {
            segment.to_node
            for segment in segments
            if segment.to_node.lower() not in {"inlet", "outlet"}
        }
    )
    validation = validate_project(
        output_dir,
        project_name,
        expected_reaches=len(segments),
        expected_junctions=len(internal_nodes),
        expected_cross_sections=expected_cross_section_count(segments, xs_interval),
    )
    write_csv(output_dir / "validation_summary.csv", [validation])

    return {
        "project_dir": str(output_dir),
        "project_name": project_name,
        "project_file": str(prj_path),
        "geometry_file": str(geom_path),
        "flow_file": str(flow_path),
        "plan_file": str(plan_path),
        "archived_derived_geometry": archived_derived,
        "validation": validation,
    }


def main() -> None:
    args = parse_args()
    result = generate_project(
        network_dir=args.network_dir,
        output_dir=args.output_dir,
        project_name=args.project_name,
        river_name=args.river_name,
        program_version=args.program_version,
        total_q=args.total_q,
        xs_interval=args.xs_interval,
        xs_margin=args.xs_margin,
        centerline_interval=args.centerline_interval,
    )
    print(f"Generated HEC-RAS project in {result['project_dir']}")
    print(f"Project file: {result['project_file']}")
    print(f"Validation: {result['validation']}")


if __name__ == "__main__":
    main()
