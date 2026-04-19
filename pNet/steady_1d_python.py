from __future__ import annotations

import argparse
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd


EPS = 1e-6
DEFAULT_NETWORK_DIR = Path("MTGHP-core")
DEFAULT_OUTPUT_DIR = Path("steady_1d_python_output")
DEFAULT_PROFILE_MODES = {
    "UsProject": "Subcritical",
    "waterway": "Subcritical",
    "UsExit": "Subcritical",
    "forebay": "Subcritical",
    "sidechannel": "Subcritical",
    "drain": "Subcritical",
}

BC_SYNONYMS = {
    "known ws": "Known WSE",
    "known wse": "Known WSE",
    "known depth": "Known Depth",
    "normal depth": "Normal Depth",
    "critical depth": "Critical Depth",
    "none": "None",
    "spill end": "Spill End",
    "spill zero": "Spill Zero",
}


@dataclass
class SolverParameters:
    total_inflow_q: float = 51.0
    manning_n: float = 0.015
    default_bank_slope: float = 0.0
    expansion_coeff: float = 0.30
    contraction_coeff: float = 0.10
    bend_coeff: float = 0.15
    junction_coeff: float = 0.75
    spill_coeff: float = 1.70
    stage_relaxation: float = 0.35
    split_relaxation: float = 0.08
    momentum_weight: float = 0.05
    min_depth: float = 0.05
    boundary_slope: float = 0.001
    profile_relaxation: float = 0.15
    profile_tolerance: float = 0.001
    reset: int = 1
    init_j1_wse: float = 634.30
    init_j2_wse: float = 631.90
    init_alpha1: float = 0.65
    init_alpha2: float = 0.65
    gravity: float = 9.81
    density: float = 998.2
    max_junction_iterations: int = 80
    max_profile_iterations: int = 250
    max_segment_iterations: int = 12
    max_outer_iterations: int = 12
    outer_tolerance: float = 1e-4


@dataclass
class BoundaryCondition:
    label: str
    segment: str
    end: str
    bc_type: str
    value: Optional[float]
    remarks: str


@dataclass
class Segment:
    name: str
    from_node: str
    to_node: str
    df: pd.DataFrame
    npts: int
    length: float
    start_width: float
    end_width: float
    start_side: float
    end_side: float
    start_bed: float
    end_bed: float
    start_angle: float
    end_angle: float
    upstream_bc_type: str = ""
    upstream_bc_value: Optional[float] = None
    downstream_bc_type: str = ""
    downstream_bc_value: Optional[float] = None
    junction_delta: float = 0.0


@dataclass
class SectionState:
    depth: float
    area: float
    perimeter: float
    radius: float
    top_width: float
    velocity: float
    velocity_head: float
    water_surface: float
    energy_grade: float
    friction_slope: float
    froude: float
    specific_force: float


@dataclass
class SegmentLossTerms:
    up: SectionState
    dn: SectionState
    hf: float
    hve: float
    hbend: float
    hj: float
    hl: float


def angle_deg(dx: float, dy: float) -> float:
    return math.degrees(math.atan2(dy, dx))


def angle_diff(a1_deg: float, a2_deg: float) -> float:
    diff = (a2_deg - a1_deg + 180.0) % 360.0 - 180.0
    return abs(diff)


def yes_no(value) -> bool:
    if value is None:
        return False
    text = str(value).strip().lower()
    return text in {"yes", "true", "1"}


def float_or_none(value) -> Optional[float]:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def canonical_bc_type(value) -> str:
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return ""
    key = " ".join(text.lower().split())
    return BC_SYNONYMS.get(key, text)


def sanitize_name(text: str) -> str:
    clean = "".join(ch.lower() if ch.isalnum() else "_" for ch in text)
    while "__" in clean:
        clean = clean.replace("__", "_")
    return clean.strip("_")


def fill_numeric(series: pd.Series, default: float) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce")
    values = values.interpolate(limit_direction="both").bfill().ffill()
    return values.fillna(default)


def optional_series(df: pd.DataFrame, column_name: str, default_value) -> pd.Series:
    if column_name in df.columns:
        return df[column_name]
    return pd.Series([default_value] * len(df), index=df.index)


def clean_columns(df: pd.DataFrame) -> pd.DataFrame:
    cols = []
    seen: Dict[str, int] = {}
    for idx, name in enumerate(df.columns):
        label = str(name).strip()
        if not label or label.lower().startswith("unnamed:"):
            label = f"blank_{idx}"
        if label in seen:
            seen[label] += 1
            label = f"{label}.{seen[label]}"
        else:
            seen[label] = 0
        cols.append(label)
    cleaned = df.copy()
    cleaned.columns = cols
    return cleaned


def cumulative_distances(df: pd.DataFrame) -> List[float]:
    distances = [0.0]
    east = df["Easting"].astype(float).tolist()
    north = df["Northing"].astype(float).tolist()
    for idx in range(1, len(df)):
        dx = east[idx] - east[idx - 1]
        dy = north[idx] - north[idx - 1]
        distances.append(distances[-1] + math.hypot(dx, dy))
    return distances


def deflection_angles(df: pd.DataFrame) -> List[float]:
    east = df["Easting"].astype(float).tolist()
    north = df["Northing"].astype(float).tolist()
    angles = [0.0] * len(df)
    for idx in range(1, len(df) - 1):
        a0 = angle_deg(east[idx] - east[idx - 1], north[idx] - north[idx - 1])
        a1_deg = angle_deg(east[idx + 1] - east[idx], north[idx + 1] - north[idx])
        angles[idx] = angle_diff(a0, a1_deg)
    return angles


def boundary_label(segment_name: str, end: str) -> str:
    return f"{'Inlet' if end == 'start' else 'Outlet'}-{segment_name}"


def detect_total_inflow(master: pd.DataFrame) -> Optional[float]:
    candidates = [
        "total inflow q",
        "total inflow",
        "inflow q",
        "total discharge",
        "discharge",
        "flow",
    ]
    for col in master.columns:
        key = " ".join(str(col).strip().lower().split())
        if "bc" in key or "value" in key:
            continue
        if not any(token in key for token in candidates):
            continue
        values = pd.to_numeric(master[col], errors="coerce").dropna()
        if not values.empty:
            return float(values.iloc[0])
    return None


def build_boundary_conditions(segments: List[Segment]) -> List[BoundaryCondition]:
    boundaries: List[BoundaryCondition] = []
    for segment in segments:
        if segment.from_node.lower() == "inlet":
            bc_type = segment.upstream_bc_type or "Normal Depth"
            remarks = "Master inlet boundary" if segment.upstream_bc_type else "Default inlet boundary from missing BC"
            boundaries.append(
                BoundaryCondition(
                    label=boundary_label(segment.name, "start"),
                    segment=segment.name,
                    end="start",
                    bc_type=bc_type,
                    value=segment.upstream_bc_value,
                    remarks=remarks,
                )
            )
        if segment.to_node.lower() == "outlet":
            bc_type = segment.downstream_bc_type or "Normal Depth"
            remarks = "Master outlet boundary" if segment.downstream_bc_type else "Default outlet boundary from missing BC"
            boundaries.append(
                BoundaryCondition(
                    label=boundary_label(segment.name, "end"),
                    segment=segment.name,
                    end="end",
                    bc_type=bc_type,
                    value=segment.downstream_bc_value,
                    remarks=remarks,
                )
            )
    return boundaries


def load_network(network_dir: Path) -> Tuple[List[Segment], List[BoundaryCondition], Optional[float]]:
    # Preserve the literal string "None" from Excel. pandas treats it as NA by default.
    master = clean_columns(pd.read_excel(network_dir / "master.xlsx", keep_default_na=False))
    from_col = "Upstream" if "Upstream" in master.columns else "From"
    to_col = "Downstream" if "Downstream" in master.columns else "To"
    up_bc_col = "Upstream BC" if "Upstream BC" in master.columns else None
    up_bc_value_col = "Upstream BC Value" if "Upstream BC Value" in master.columns else None
    dn_bc_col = "Downstream BC" if "Downstream BC" in master.columns else None
    dn_bc_value_col = "Downstream BC Value" if "Downstream BC Value" in master.columns else None

    segments: List[Segment] = []
    for _, record in master.iterrows():
        name = str(record["Channel"]).strip()
        df = clean_columns(pd.read_csv(network_dir / f"{name}.csv"))

        df["WidthFilled"] = fill_numeric(optional_series(df, "Bed width", 1.0), 1.0)
        df["SideFilled"] = fill_numeric(optional_series(df, "Bank slope", 0.0), 0.0)
        df["BedFilled"] = fill_numeric(optional_series(df, "Bed elevation", 0.0), 0.0)
        df["Chainage"] = cumulative_distances(df)
        df["Deflection"] = deflection_angles(df)

        left_to = optional_series(df, "Spill to", "")
        right_to = optional_series(df, "Spill to.1", "")
        left_crest = fill_numeric(optional_series(df, "Spillway Left Elevation", float("nan")), float("nan"))
        right_crest = fill_numeric(optional_series(df, "Spill Right Elevation", float("nan")), float("nan"))

        df["SpillLeftOn"] = optional_series(df, "Spill left", "").map(yes_no)
        df["SpillRightOn"] = optional_series(df, "Spill Right", "").map(yes_no)
        df["SpillLeftTo"] = left_to.fillna("").astype(str).str.strip()
        df["SpillRightTo"] = right_to.fillna("").astype(str).str.strip()
        df["SpillLeftCrest"] = left_crest
        df["SpillRightCrest"] = right_crest

        east = df["Easting"].astype(float).tolist()
        north = df["Northing"].astype(float).tolist()
        start_ang = angle_deg(east[1] - east[0], north[1] - north[0]) if len(df) > 1 else 0.0
        end_ang = angle_deg(east[-1] - east[-2], north[-1] - north[-2]) if len(df) > 1 else 0.0

        segments.append(
            Segment(
                name=name,
                from_node=str(record[from_col]).strip(),
                to_node=str(record[to_col]).strip(),
                df=df,
                npts=len(df),
                length=float(df["Chainage"].iloc[-1]) if len(df) else 0.0,
                start_width=float(df["WidthFilled"].iloc[0]),
                end_width=float(df["WidthFilled"].iloc[-1]),
                start_side=float(df["SideFilled"].iloc[0]),
                end_side=float(df["SideFilled"].iloc[-1]),
                start_bed=float(df["BedFilled"].iloc[0]),
                end_bed=float(df["BedFilled"].iloc[-1]),
                start_angle=start_ang,
                end_angle=end_ang,
                upstream_bc_type=canonical_bc_type(record[up_bc_col]) if up_bc_col else "",
                upstream_bc_value=float_or_none(record[up_bc_value_col]) if up_bc_value_col else None,
                downstream_bc_type=canonical_bc_type(record[dn_bc_col]) if dn_bc_col else "",
                downstream_bc_value=float_or_none(record[dn_bc_value_col]) if dn_bc_value_col else None,
            )
        )

    junctions: Dict[str, Dict[str, List[Segment]]] = {}
    for segment in segments:
        junctions.setdefault(segment.from_node, {"incoming": [], "outgoing": []})["outgoing"].append(segment)
        junctions.setdefault(segment.to_node, {"incoming": [], "outgoing": []})["incoming"].append(segment)

    for node, data in junctions.items():
        if node in ("Inlet", "Outlet", "Spill") or not data["incoming"]:
            continue
        main = data["incoming"][0]
        for segment in data["outgoing"]:
            segment.junction_delta = angle_diff(main.end_angle, segment.start_angle)

    return segments, build_boundary_conditions(segments), detect_total_inflow(master)


def trap_area(width: float, side: float, depth: float) -> float:
    return (width + side * depth) * depth


def trap_perimeter(width: float, side: float, depth: float) -> float:
    return width + 2.0 * depth * math.sqrt(1.0 + side**2)


def trap_radius(width: float, side: float, depth: float) -> float:
    area = trap_area(width, side, depth)
    perimeter = trap_perimeter(width, side, depth)
    return area / max(perimeter, EPS)


def trap_top_width(width: float, side: float, depth: float) -> float:
    return width + 2.0 * side * depth


def velocity(q: float, area: float) -> float:
    return abs(q) / max(area, EPS)


def velocity_head(v: float, gravity: float) -> float:
    return (v * v) / (2.0 * gravity)


def friction_slope(q: float, area: float, radius: float, manning_n: float) -> float:
    denominator = max(area, EPS) * max(radius ** (2.0 / 3.0), EPS)
    return ((abs(q) * manning_n) / denominator) ** 2


def froude_number(q: float, area: float, top_width: float, gravity: float) -> float:
    hydraulic_depth = max(area / max(top_width, EPS), EPS)
    return velocity(q, area) / math.sqrt(gravity * hydraulic_depth)


def specific_force(width: float, side: float, depth: float, q: float, area: float, gravity: float) -> float:
    return (width * depth * depth) / 2.0 + (side * depth**3) / 3.0 + (abs(q) ** 2) / (gravity * max(area, EPS))


def section_state(bed: float, width: float, side: float, q: float, depth: float, params: SolverParameters) -> SectionState:
    area = trap_area(width, side, depth)
    perimeter = trap_perimeter(width, side, depth)
    radius = area / max(perimeter, EPS)
    top_width = trap_top_width(width, side, depth)
    v = velocity(q, area)
    vh = velocity_head(v, params.gravity)
    ws = bed + depth
    eg = ws + vh
    sf = friction_slope(q, area, radius, params.manning_n)
    fr = froude_number(q, area, top_width, params.gravity)
    force = specific_force(width, side, depth, q, area, params.gravity)
    return SectionState(
        depth=depth,
        area=area,
        perimeter=perimeter,
        radius=radius,
        top_width=top_width,
        velocity=v,
        velocity_head=vh,
        water_surface=ws,
        energy_grade=eg,
        friction_slope=sf,
        froude=fr,
        specific_force=force,
    )


def solve_bisection_root(func, x_low: float, x_high: float, iterations: int = 80) -> float:
    f_low = func(x_low)
    f_high = func(x_high)
    if f_low == 0.0:
        return x_low
    if f_high == 0.0:
        return x_high
    for _ in range(iterations):
        x_mid = 0.5 * (x_low + x_high)
        f_mid = func(x_mid)
        if abs(f_mid) < 1e-10 or abs(x_high - x_low) < 1e-8:
            return x_mid
        if f_low * f_mid <= 0.0:
            x_high = x_mid
            f_high = f_mid
        else:
            x_low = x_mid
            f_low = f_mid
    return 0.5 * (x_low + x_high)


def solve_positive_root(func, start: float, max_depth: float = 200.0) -> float:
    lower = max(start, 1e-4)
    upper = max(0.5, lower * 2.0)
    f_lower = func(lower)
    best_x = lower
    best_abs = abs(f_lower)

    while upper <= max_depth:
        f_upper = func(upper)
        if abs(f_upper) < best_abs:
            best_x = upper
            best_abs = abs(f_upper)
        if f_lower == 0.0:
            return lower
        if f_lower * f_upper <= 0.0:
            return solve_bisection_root(func, lower, upper)
        lower = upper
        f_lower = f_upper
        upper *= 2.0

    for idx in range(1, 200):
        probe = start + idx * (max_depth - start) / 199.0
        value = abs(func(probe))
        if value < best_abs:
            best_x = probe
            best_abs = value
    return best_x


def solve_normal_depth(q: float, width: float, side: float, slope: float, params: SolverParameters) -> float:
    if abs(q) <= EPS:
        return params.min_depth

    def residual(depth: float) -> float:
        area = trap_area(width, side, depth)
        radius = trap_radius(width, side, depth)
        conveyance = (1.0 / max(params.manning_n, EPS)) * area * max(radius ** (2.0 / 3.0), EPS) * math.sqrt(max(slope, EPS))
        return conveyance - abs(q)

    return max(params.min_depth, solve_positive_root(residual, params.min_depth))


def solve_critical_depth(q: float, width: float, side: float, params: SolverParameters) -> float:
    if abs(q) <= EPS:
        return params.min_depth

    def residual(depth: float) -> float:
        area = trap_area(width, side, depth)
        top_width = trap_top_width(width, side, depth)
        return (abs(q) ** 2) * top_width / (params.gravity * max(area**3, EPS)) - 1.0

    return max(params.min_depth, solve_positive_root(residual, params.min_depth))


def branch_loss(delta_deg: float, branch_q: float, main_q: float, vh_up: float, vh_dn: float, params: SolverParameters) -> float:
    if delta_deg <= 0.0 or abs(branch_q) <= EPS or abs(main_q) <= EPS:
        return 0.0
    sin_sq = math.sin(math.radians(delta_deg / 2.0)) ** 2
    return params.junction_coeff * sin_sq * (abs(branch_q) / max(abs(main_q), EPS)) ** 2 * max(vh_up, vh_dn)


def transition_and_bend_loss(v_up: float, v_dn: float, deflection_deg: float, params: SolverParameters) -> float:
    vh_up = velocity_head(v_up, params.gravity)
    vh_dn = velocity_head(v_dn, params.gravity)
    if v_dn > v_up:
        transition = params.contraction_coeff * (vh_dn - vh_up)
    else:
        transition = params.expansion_coeff * (vh_up - vh_dn)
    bend = params.bend_coeff * (abs(deflection_deg) / 90.0) * max(vh_up, vh_dn)
    return transition + bend


def segment_end_terms(
    segment: Segment,
    q: float,
    stage_up: float,
    stage_dn: float,
    params: SolverParameters,
    branch_q: float = 0.0,
    main_q: float = 1.0,
) -> SegmentLossTerms:
    depth_up = max(params.min_depth, stage_up - segment.start_bed)
    depth_dn = max(params.min_depth, stage_dn - segment.end_bed)
    up = section_state(segment.start_bed, segment.start_width, segment.start_side, q, depth_up, params)
    dn = section_state(segment.end_bed, segment.end_width, segment.end_side, q, depth_dn, params)
    hf = segment.length * 0.5 * (up.friction_slope + dn.friction_slope)
    if dn.velocity > up.velocity:
        hve = params.contraction_coeff * (dn.velocity_head - up.velocity_head)
    else:
        hve = params.expansion_coeff * (up.velocity_head - dn.velocity_head)
    hbend = params.bend_coeff * (abs(segment.end_angle - segment.start_angle) / 90.0) * max(up.velocity_head, dn.velocity_head)
    hj = branch_loss(segment.junction_delta, branch_q, main_q, up.velocity_head, dn.velocity_head, params)
    return SegmentLossTerms(up=up, dn=dn, hf=hf, hve=hve, hbend=hbend, hj=hj, hl=hf + hve + hbend + hj)


def boundary_slope(segment: Segment, params: SolverParameters) -> float:
    if segment.length <= EPS:
        return params.boundary_slope
    slope = abs((segment.end_bed - segment.start_bed) / segment.length)
    return max(slope, params.boundary_slope)


def is_closed_outlet(segment: Segment) -> bool:
    return segment.to_node.lower() == "outlet" and canonical_bc_type(segment.downstream_bc_type) == "None"


def is_spill_end(segment: Segment) -> bool:
    return segment.to_node.lower() == "outlet" and canonical_bc_type(segment.downstream_bc_type) == "Spill End"


def is_spill_zero(segment: Segment) -> bool:
    return segment.from_node.lower() == "inlet" and canonical_bc_type(segment.upstream_bc_type) == "Spill Zero"


def resolve_boundary_stage(boundary: BoundaryCondition, segment: Segment, q_reference: float, params: SolverParameters) -> float:
    if boundary.end == "start":
        bed = segment.start_bed
        width = segment.start_width
        side = segment.start_side
    else:
        bed = segment.end_bed
        width = segment.end_width
        side = segment.end_side

    bc_type = canonical_bc_type(boundary.bc_type)
    if bc_type == "Known WSE":
        return float(boundary.value if boundary.value is not None else bed + params.min_depth)
    if bc_type == "Known Depth":
        return bed + float(boundary.value if boundary.value is not None else params.min_depth)
    if bc_type == "Normal Depth" or not bc_type:
        return bed + solve_normal_depth(q_reference, width, side, boundary_slope(segment, params), params)
    if bc_type == "Critical Depth":
        return bed + solve_critical_depth(q_reference, width, side, params)
    if bc_type in {"None", "Spill End", "Spill Zero"}:
        return bed + params.min_depth
    raise ValueError(f"Unsupported boundary condition type: {boundary.bc_type}")


def split_branch_flow(total_q: float, alpha: float, branch_a_open: bool, branch_b_open: bool) -> Tuple[float, float]:
    total_q = max(0.0, total_q)
    if not branch_a_open and not branch_b_open:
        return 0.0, 0.0
    if not branch_a_open:
        return 0.0, total_q
    if not branch_b_open:
        return total_q, 0.0
    alpha = min(0.98, max(0.02, alpha))
    branch_a = total_q * alpha
    return branch_a, total_q - branch_a


def initial_qout_guess(params: SolverParameters, segments_by_name: Dict[str, Segment]) -> Dict[str, float]:
    q_waterway, q_usexit = split_branch_flow(
        params.total_inflow_q,
        params.init_alpha1,
        True,
        not is_closed_outlet(segments_by_name["UsExit"]),
    )
    q_forebay, q_side = split_branch_flow(
        q_waterway,
        params.init_alpha2,
        not is_closed_outlet(segments_by_name["forebay"]),
        not is_closed_outlet(segments_by_name["sidechannel"]),
    )
    return {
        "UsProject": params.total_inflow_q,
        "waterway": q_waterway,
        "UsExit": q_usexit,
        "forebay": q_forebay,
        "sidechannel": q_side,
        "drain": 0.0,
    }


def resolve_boundary_stages(
    boundaries: List[BoundaryCondition],
    segments_by_name: Dict[str, Segment],
    q_reference_map: Dict[str, float],
    params: SolverParameters,
) -> Tuple[Dict[str, float], List[Dict[str, float]]]:
    boundary_stage_map: Dict[str, float] = {}
    boundary_rows: List[Dict[str, float]] = []
    for boundary in boundaries:
        q_reference = q_reference_map.get(boundary.segment, params.total_inflow_q)
        stage = resolve_boundary_stage(boundary, segments_by_name[boundary.segment], q_reference, params)
        boundary_stage_map[boundary.label] = stage
        boundary_rows.append(
            {
                "label": boundary.label,
                "segment": boundary.segment,
                "end": boundary.end,
                "bc_type": canonical_bc_type(boundary.bc_type),
                "bc_value": boundary.value,
                "q_reference": q_reference,
                "resolved_stage": stage,
                "remarks": boundary.remarks,
            }
        )
    return boundary_stage_map, boundary_rows


def solve_junction_network(
    segments_by_name: Dict[str, Segment],
    boundary_stage_map: Dict[str, float],
    params: SolverParameters,
    init_alpha1: float,
    init_alpha2: float,
    init_j1: float,
    init_j2: float,
) -> Tuple[Dict[str, float], pd.DataFrame]:
    alpha1 = init_alpha1
    alpha2 = init_alpha2
    j1 = init_j1
    j2 = init_j2
    records: List[Dict[str, float]] = []

    usexit_open = not is_closed_outlet(segments_by_name["UsExit"])
    forebay_open = not is_closed_outlet(segments_by_name["forebay"])
    sidechannel_open = not is_closed_outlet(segments_by_name["sidechannel"])

    for iteration in range(params.max_junction_iterations):
        q_total = params.total_inflow_q
        q_waterway, q_usexit = split_branch_flow(q_total, alpha1, True, usexit_open)
        q_forebay, q_side = split_branch_flow(q_waterway, alpha2, forebay_open, sidechannel_open)

        inlet_stage = boundary_stage_map[boundary_label("UsProject", "start")]
        usexit_stage = boundary_stage_map[boundary_label("UsExit", "end")]
        forebay_stage = boundary_stage_map[boundary_label("forebay", "end")]
        side_stage = boundary_stage_map[boundary_label("sidechannel", "end")]

        up_terms = segment_end_terms(segments_by_name["UsProject"], q_total, inlet_stage, j1, params)
        ww_terms = segment_end_terms(segments_by_name["waterway"], q_waterway, j1, j2, params, q_usexit, q_total)
        ue_terms = segment_end_terms(segments_by_name["UsExit"], q_usexit, j1, usexit_stage, params, q_waterway, q_total)
        fb_terms = segment_end_terms(segments_by_name["forebay"], q_forebay, j2, forebay_stage, params, q_side, max(q_waterway, EPS))
        sc_terms = segment_end_terms(segments_by_name["sidechannel"], q_side, j2, side_stage, params, q_forebay, max(q_waterway, EPS))

        j1_from_usproject = inlet_stage + up_terms.up.velocity_head - up_terms.hl - up_terms.dn.velocity_head
        j1_from_waterway = j2 + ww_terms.dn.velocity_head + ww_terms.hl - ww_terms.up.velocity_head
        j1_from_usexit = usexit_stage + ue_terms.dn.velocity_head + ue_terms.hl - ue_terms.up.velocity_head

        j2_from_waterway = j1 + ww_terms.up.velocity_head - ww_terms.hl - ww_terms.dn.velocity_head
        j2_from_forebay = forebay_stage + fb_terms.dn.velocity_head + fb_terms.hl - fb_terms.up.velocity_head
        j2_from_sidechannel = side_stage + sc_terms.dn.velocity_head + sc_terms.hl - sc_terms.up.velocity_head

        j1_equations = [j1_from_usproject, j1_from_waterway]
        if usexit_open:
            j1_equations.append(j1_from_usexit)
        j2_equations = [j2_from_waterway]
        if forebay_open:
            j2_equations.append(j2_from_forebay)
        if sidechannel_open:
            j2_equations.append(j2_from_sidechannel)

        err_j1 = max(abs(value - j1) for value in j1_equations)
        err_j2 = max(abs(value - j2) for value in j2_equations)

        cos_ww = math.cos(math.radians(segments_by_name["waterway"].junction_delta))
        cos_ue = math.cos(math.radians(segments_by_name["UsExit"].junction_delta))
        cos_fb = math.cos(math.radians(segments_by_name["forebay"].junction_delta))
        cos_sc = math.cos(math.radians(segments_by_name["sidechannel"].junction_delta))
        mom_j1 = ((ww_terms.up.specific_force * cos_ww) - (ue_terms.up.specific_force * cos_ue)) / max(up_terms.dn.specific_force, EPS)
        mom_j2 = ((fb_terms.up.specific_force * cos_fb) - (sc_terms.up.specific_force * cos_sc)) / max(ww_terms.dn.specific_force, EPS)

        if usexit_open:
            alpha1_next = min(
                0.98,
                max(
                    0.02,
                    alpha1
                    + params.split_relaxation
                    * (((j1_from_usexit - j1_from_waterway) / max(abs(j1), 1.0)) + (params.momentum_weight * mom_j1)),
                ),
            )
        else:
            alpha1_next = 1.0

        if forebay_open and sidechannel_open:
            alpha2_next = min(
                0.98,
                max(
                    0.02,
                    alpha2
                    + params.split_relaxation
                    * (((j2_from_sidechannel - j2_from_forebay) / max(abs(j2), 1.0)) + (params.momentum_weight * mom_j2)),
                ),
            )
        elif forebay_open:
            alpha2_next = 1.0
        elif sidechannel_open:
            alpha2_next = 0.0
        else:
            alpha2_next = alpha2

        j1_next = j1 + params.stage_relaxation * (sum(j1_equations) / len(j1_equations) - j1)
        j2_next = j2 + params.stage_relaxation * (sum(j2_equations) / len(j2_equations) - j2)

        records.append(
            {
                "iteration": iteration,
                "alpha1": alpha1,
                "alpha2": alpha2,
                "j1_wse": j1,
                "j2_wse": j2,
                "q_total": q_total,
                "q_waterway": q_waterway,
                "q_usexit": q_usexit,
                "q_forebay": q_forebay,
                "q_sidechannel": q_side,
                "ws_inlet": inlet_stage,
                "ws_usexit": usexit_stage,
                "ws_forebay": forebay_stage,
                "ws_sidechannel": side_stage,
                "j1_from_usproject": j1_from_usproject,
                "j1_from_waterway": j1_from_waterway,
                "j1_from_usexit": j1_from_usexit if usexit_open else float("nan"),
                "j2_from_waterway": j2_from_waterway,
                "j2_from_forebay": j2_from_forebay if forebay_open else float("nan"),
                "j2_from_sidechannel": j2_from_sidechannel if sidechannel_open else float("nan"),
                "momentum_j1": mom_j1,
                "momentum_j2": mom_j2,
                "err_j1": err_j1,
                "err_j2": err_j2,
                "alpha1_next": alpha1_next,
                "alpha2_next": alpha2_next,
                "j1_next": j1_next,
                "j2_next": j2_next,
            }
        )

        alpha_delta = max(abs(alpha1_next - alpha1), abs(alpha2_next - alpha2))
        stage_delta = max(abs(j1_next - j1), abs(j2_next - j2))
        alpha1, alpha2, j1, j2 = alpha1_next, alpha2_next, j1_next, j2_next
        if max(err_j1, err_j2, alpha_delta, stage_delta) < params.outer_tolerance:
            break

    final_q_waterway, final_q_usexit = split_branch_flow(params.total_inflow_q, alpha1, True, usexit_open)
    final_q_forebay, final_q_side = split_branch_flow(final_q_waterway, alpha2, forebay_open, sidechannel_open)
    return (
        {
            "alpha1": alpha1,
            "alpha2": alpha2,
            "j1_wse": j1,
            "j2_wse": j2,
            "q_usproject": params.total_inflow_q,
            "q_waterway": final_q_waterway,
            "q_usexit": final_q_usexit,
            "q_forebay": final_q_forebay,
            "q_sidechannel": final_q_side,
            "err_j1": records[-1]["err_j1"],
            "err_j2": records[-1]["err_j2"],
        },
        pd.DataFrame(records),
    )


def forward_step_error(prev_state: Dict[str, float], curr_state: SectionState, dx: float, deflection_deg: float, params: SolverParameters) -> Tuple[float, float]:
    local_loss = transition_and_bend_loss(prev_state["velocity"], curr_state.velocity, deflection_deg, params)
    hf = 0.5 * (prev_state["friction_slope"] + curr_state.friction_slope) * dx
    return prev_state["energy_grade"] - hf - local_loss - curr_state.energy_grade, local_loss


def backward_step_error(curr_state: SectionState, next_state: Dict[str, float], dx: float, deflection_deg: float, params: SolverParameters) -> Tuple[float, float]:
    local_loss = transition_and_bend_loss(curr_state.velocity, next_state["velocity"], deflection_deg, params)
    hf = 0.5 * (curr_state.friction_slope + next_state["friction_slope"]) * dx
    return next_state["energy_grade"] + hf + local_loss - curr_state.energy_grade, local_loss


def raw_state_row(state: SectionState) -> Dict[str, float]:
    return {
        "depth": state.depth,
        "area": state.area,
        "perimeter": state.perimeter,
        "radius": state.radius,
        "velocity": state.velocity,
        "water_surface": state.water_surface,
        "energy_grade": state.energy_grade,
        "friction_slope": state.friction_slope,
        "froude": state.froude,
        "specific_force": state.specific_force,
    }


def pass_result_row(prefix: str, row: Dict[str, float]) -> Dict[str, float]:
    return {
        f"y_{prefix}": row["depth"],
        f"area_{prefix}": row["area"],
        f"perimeter_{prefix}": row["perimeter"],
        f"radius_{prefix}": row["radius"],
        f"velocity_{prefix}": row["velocity"],
        f"ws_{prefix}": row["water_surface"],
        f"eg_{prefix}": row["energy_grade"],
        f"sf_{prefix}": row["friction_slope"],
        f"froude_{prefix}": row["froude"],
        f"specific_force_{prefix}": row["specific_force"],
        f"error_{prefix}": row["error"],
        f"done_{prefix}": row["done"],
        f"iterations_{prefix}": row["iterations"],
        f"local_loss_{prefix}": row["local_loss"],
        f"fallback_{prefix}": row["fallback_used"],
    }


def solve_forward_profile(segment: Segment, q_array: List[float], start_stage: float, params: SolverParameters) -> List[Dict[str, float]]:
    rows = segment.df.reset_index(drop=True)
    results: List[Dict[str, float]] = []

    first_depth = max(params.min_depth, start_stage - float(rows.loc[0, "BedFilled"]))
    first_state = section_state(
        float(rows.loc[0, "BedFilled"]),
        float(rows.loc[0, "WidthFilled"]),
        float(rows.loc[0, "SideFilled"]),
        q_array[0],
        first_depth,
        params,
    )
    results.append({"error": 0.0, "done": True, "iterations": 0, "local_loss": 0.0, "fallback_used": False, **raw_state_row(first_state)})

    for idx in range(1, len(rows)):
        prev = results[idx - 1]
        dx = float(rows.loc[idx, "Chainage"] - rows.loc[idx - 1, "Chainage"])
        deflection = float(rows.loc[idx, "Deflection"])
        bed = float(rows.loc[idx, "BedFilled"])
        width = float(rows.loc[idx, "WidthFilled"])
        side = float(rows.loc[idx, "SideFilled"])
        q_here = q_array[idx]
        depth = max(params.min_depth, prev["depth"])
        done = False
        local_loss = 0.0
        error = 0.0
        fallback_used = False
        iteration = 0

        if params.reset == 0:
            done = False
        else:
            for iteration in range(1, params.max_profile_iterations + 1):
                state = section_state(bed, width, side, q_here, depth, params)
                error, local_loss = forward_step_error(prev, state, dx, deflection, params)
                if abs(error) < params.profile_tolerance:
                    done = True
                    break
                direction = 1.0 if state.froude < 1.0 else -1.0
                depth_new = max(params.min_depth, depth + error * params.profile_relaxation * direction)
                if abs(depth_new - depth) < 1e-10:
                    depth = depth_new
                    break
                depth = depth_new
            if not done:
                def residual(test_depth: float) -> float:
                    state = section_state(bed, width, side, q_here, max(params.min_depth, test_depth), params)
                    test_error, _ = forward_step_error(prev, state, dx, deflection, params)
                    return test_error

                depth = max(params.min_depth, solve_positive_root(residual, params.min_depth))
                fallback_used = True

        state = section_state(bed, width, side, q_here, depth, params)
        error, local_loss = forward_step_error(prev, state, dx, deflection, params)
        results.append(
            {
                "error": error,
                "done": abs(error) < params.profile_tolerance if params.reset == 1 else False,
                "iterations": iteration if params.reset == 1 else 0,
                "local_loss": local_loss,
                "fallback_used": fallback_used,
                **raw_state_row(state),
            }
        )

    return results


def solve_backward_profile(segment: Segment, q_array: List[float], end_stage: float, params: SolverParameters) -> List[Dict[str, float]]:
    rows = segment.df.reset_index(drop=True)
    results: List[Optional[Dict[str, float]]] = [None] * len(rows)
    last_idx = len(rows) - 1

    last_depth = max(params.min_depth, end_stage - float(rows.loc[last_idx, "BedFilled"]))
    last_state = section_state(
        float(rows.loc[last_idx, "BedFilled"]),
        float(rows.loc[last_idx, "WidthFilled"]),
        float(rows.loc[last_idx, "SideFilled"]),
        q_array[last_idx],
        last_depth,
        params,
    )
    results[last_idx] = {"error": 0.0, "done": True, "iterations": 0, "local_loss": 0.0, "fallback_used": False, **raw_state_row(last_state)}

    for idx in range(last_idx - 1, -1, -1):
        next_row = results[idx + 1]
        assert next_row is not None
        dx = float(rows.loc[idx + 1, "Chainage"] - rows.loc[idx, "Chainage"])
        deflection = float(rows.loc[idx + 1, "Deflection"])
        bed = float(rows.loc[idx, "BedFilled"])
        width = float(rows.loc[idx, "WidthFilled"])
        side = float(rows.loc[idx, "SideFilled"])
        q_here = q_array[idx]
        depth = max(params.min_depth, next_row["depth"])
        done = False
        local_loss = 0.0
        error = 0.0
        fallback_used = False
        iteration = 0

        if params.reset == 0:
            done = False
        else:
            for iteration in range(1, params.max_profile_iterations + 1):
                state = section_state(bed, width, side, q_here, depth, params)
                error, local_loss = backward_step_error(state, next_row, dx, deflection, params)
                if abs(error) < params.profile_tolerance:
                    done = True
                    break
                direction = 1.0 if state.froude < 1.0 else -1.0
                depth_new = max(params.min_depth, depth + error * params.profile_relaxation * direction)
                if abs(depth_new - depth) < 1e-10:
                    depth = depth_new
                    break
                depth = depth_new
            if not done:
                def residual(test_depth: float) -> float:
                    state = section_state(bed, width, side, q_here, max(params.min_depth, test_depth), params)
                    test_error, _ = backward_step_error(state, next_row, dx, deflection, params)
                    return test_error

                depth = max(params.min_depth, solve_positive_root(residual, params.min_depth))
                fallback_used = True

        state = section_state(bed, width, side, q_here, depth, params)
        error, local_loss = backward_step_error(state, next_row, dx, deflection, params)
        results[idx] = {
            "error": error,
            "done": abs(error) < params.profile_tolerance if params.reset == 1 else False,
            "iterations": iteration if params.reset == 1 else 0,
            "local_loss": local_loss,
            "fallback_used": fallback_used,
            **raw_state_row(state),
        }

    return [row for row in results if row is not None]


def heuristic_start_stage(segment: Segment, q_reference: float, params: SolverParameters) -> float:
    depth = solve_normal_depth(q_reference, segment.start_width, segment.start_side, boundary_slope(segment, params), params)
    return segment.start_bed + depth


def build_section_discharge_array(
    segment: Segment,
    q_in: float,
    row_spill_total: List[float],
    lateral_inflows: Optional[List[float]] = None,
) -> List[float]:
    inflows = lateral_inflows or [0.0] * segment.npts
    q_array: List[float] = []
    running = 0.0 if is_spill_zero(segment) else max(q_in, 0.0)

    for idx in range(segment.npts):
        running += inflows[idx]
        q_array.append(max(running, 0.0))
        running = max(running - row_spill_total[idx], 0.0)

    return q_array


def configured_spill_rows(segment: Segment, recipient: Optional[str] = None) -> List[int]:
    rows = segment.df.reset_index(drop=True)
    target = recipient.strip().lower() if recipient else None
    indices: List[int] = []
    for idx, rec in rows.iterrows():
        left_match = yes_no(rec.get("SpillLeftOn", False)) and str(rec.get("SpillLeftTo", "")).strip()
        right_match = yes_no(rec.get("SpillRightOn", False)) and str(rec.get("SpillRightTo", "")).strip()
        if target is not None:
            left_match = left_match and str(rec.get("SpillLeftTo", "")).strip().lower() == target
            right_match = right_match and str(rec.get("SpillRightTo", "")).strip().lower() == target
        if left_match or right_match:
            indices.append(idx)
    return indices


def segment_has_spill_targets(segment: Segment) -> bool:
    return bool(configured_spill_rows(segment))


def choose_spill_end_target(segment: Segment) -> Tuple[int, Optional[str], Optional[str]]:
    rows = segment.df.reset_index(drop=True)
    for idx in range(len(rows) - 1, -1, -1):
        rec = rows.loc[idx]
        left_target = str(rec.get("SpillLeftTo", "")).strip()
        if yes_no(rec.get("SpillLeftOn", False)) and left_target:
            return idx, left_target, "left"
        right_target = str(rec.get("SpillRightTo", "")).strip()
        if yes_no(rec.get("SpillRightOn", False)) and right_target:
            return idx, right_target, "right"
    return len(rows) - 1, None, None


def assemble_profile_dataframe(
    segment: Segment,
    q_array: List[float],
    mode: str,
    forward_rows: List[Dict[str, float]],
    backward_rows: List[Dict[str, float]],
    lateral_inflows: List[float],
    params: SolverParameters,
) -> pd.DataFrame:
    base = segment.df.reset_index(drop=True)
    records: List[Dict[str, float]] = []
    is_supercritical = str(mode).strip().lower() == "supercritical"

    for idx, rec in base.iterrows():
        dx = 0.0 if idx == 0 else float(base.loc[idx, "Chainage"] - base.loc[idx - 1, "Chainage"])
        forward = forward_rows[idx]
        backward = backward_rows[idx]
        final_depth = forward["depth"] if is_supercritical else backward["depth"]
        final_state = section_state(
            float(rec["BedFilled"]),
            float(rec["WidthFilled"]),
            float(rec["SideFilled"]),
            q_array[idx],
            final_depth,
            params,
        )
        records.append(
            {
                "segment": segment.name,
                "sn": int(rec["SN"]),
                "chainage": float(rec["Chainage"]),
                "easting": float(rec["Easting"]),
                "northing": float(rec["Northing"]),
                "bed_elevation": float(rec["BedFilled"]),
                "bed_width": float(rec["WidthFilled"]),
                "side_slope": float(rec["SideFilled"]),
                "section_type": str(rec.get("Type", "")),
                "deflection_angle_deg": float(rec["Deflection"]),
                "dx": dx,
                "q_used": q_array[idx],
                "lateral_inflow": lateral_inflows[idx],
                **pass_result_row("forward", forward),
                **pass_result_row("backward", backward),
                "y_final": final_depth,
                "ws_final": final_state.water_surface,
                "eg_final": final_state.energy_grade,
                "sf_final": final_state.friction_slope,
                "froude_final": final_state.froude,
                "specific_force_final": final_state.specific_force,
                "regime": "Subcritical" if final_state.froude < 1.0 else "Supercritical",
            }
        )
    return pd.DataFrame(records)


def evaluate_spills(
    segment: Segment,
    profile_df: pd.DataFrame,
    q_in: float,
    lateral_inflows: List[float],
    params: SolverParameters,
) -> Dict[str, object]:
    rows = segment.df.reset_index(drop=True)
    recipient_names = set()
    for rec in rows.itertuples(index=False):
        left_target = str(getattr(rec, "SpillLeftTo", "")).strip()
        right_target = str(getattr(rec, "SpillRightTo", "")).strip()
        if left_target:
            recipient_names.add(left_target)
        if right_target:
            recipient_names.add(right_target)

    left_values = [0.0] * segment.npts
    right_values = [0.0] * segment.npts
    extra_values = [0.0] * segment.npts
    total_values = [0.0] * segment.npts
    recipient_spills = {name: [0.0] * segment.npts for name in recipient_names}

    for idx, rec in rows.iterrows():
        dx = max(float(profile_df.loc[idx, "dx"]), 0.1)
        ws = float(profile_df.loc[idx, "ws_final"])
        available = max(float(profile_df.loc[idx, "q_used"]), 0.0)

        left_target = str(rec.get("SpillLeftTo", "")).strip()
        left_potential = 0.0
        if yes_no(rec.get("SpillLeftOn", False)) and left_target and not pd.isna(rec.get("SpillLeftCrest")):
            left_potential = params.spill_coeff * dx * max(0.0, ws - float(rec["SpillLeftCrest"])) ** 1.5

        right_target = str(rec.get("SpillRightTo", "")).strip()
        right_potential = 0.0
        if yes_no(rec.get("SpillRightOn", False)) and right_target and not pd.isna(rec.get("SpillRightCrest")):
            right_potential = params.spill_coeff * dx * max(0.0, ws - float(rec["SpillRightCrest"])) ** 1.5

        potential_total = left_potential + right_potential
        scale = min(1.0, available / max(potential_total, EPS)) if potential_total > 0.0 else 0.0
        left_actual = left_potential * scale
        right_actual = right_potential * scale

        left_values[idx] = left_actual
        right_values[idx] = right_actual
        total_values[idx] = left_actual + right_actual
        if left_target:
            recipient_spills.setdefault(left_target, [0.0] * segment.npts)[idx] += left_actual
        if right_target:
            recipient_spills.setdefault(right_target, [0.0] * segment.npts)[idx] += right_actual

    if is_spill_end(segment):
        residual = max(0.0, q_in + sum(lateral_inflows) - sum(total_values))
        if residual > EPS:
            target_idx, target_name, target_side = choose_spill_end_target(segment)
            total_values[target_idx] += residual
            extra_values[target_idx] += residual
            if target_name:
                recipient_spills.setdefault(target_name, [0.0] * segment.npts)[target_idx] += residual
            if target_side == "left":
                left_values[target_idx] += residual
            elif target_side == "right":
                right_values[target_idx] += residual

    return {
        "spill_left": left_values,
        "spill_right": right_values,
        "spill_extra": extra_values,
        "spill_total": total_values,
        "recipient_spills": recipient_spills,
    }


def finalize_profile_dataframe(segment: Segment, profile_df: pd.DataFrame, spill_eval: Dict[str, object]) -> pd.DataFrame:
    rows = segment.df.reset_index(drop=True)
    result = profile_df.copy()
    spill_left = list(spill_eval["spill_left"])
    spill_right = list(spill_eval["spill_right"])
    spill_extra = list(spill_eval["spill_extra"])
    spill_total = list(spill_eval["spill_total"])

    result["spill_crest_left"] = [
        float(rec["SpillLeftCrest"]) if yes_no(rec.get("SpillLeftOn", False)) and not pd.isna(rec.get("SpillLeftCrest")) else float("nan")
        for _, rec in rows.iterrows()
    ]
    result["spill_crest_right"] = [
        float(rec["SpillRightCrest"]) if yes_no(rec.get("SpillRightOn", False)) and not pd.isna(rec.get("SpillRightCrest")) else float("nan")
        for _, rec in rows.iterrows()
    ]
    result["spill_left"] = spill_left
    result["spill_right"] = spill_right
    result["spill_extra"] = spill_extra
    result["spill_total"] = spill_total
    result["cumulative_spill"] = pd.Series(spill_total).cumsum()
    result["cumulative_lateral_inflow"] = result["lateral_inflow"].cumsum()
    result["q_after_row"] = (result["q_used"] - result["spill_total"]).clip(lower=0.0)

    recipient_spills: Dict[str, List[float]] = spill_eval["recipient_spills"]  # type: ignore[assignment]
    for recipient, values in recipient_spills.items():
        result[f"spill_to_{sanitize_name(recipient)}"] = values

    return result


def solve_segment_profile(
    segment: Segment,
    q_in: float,
    start_stage: Optional[float],
    end_stage: float,
    mode: str,
    params: SolverParameters,
    lateral_inflows: Optional[List[float]] = None,
) -> pd.DataFrame:
    inflows = lateral_inflows or [0.0] * segment.npts
    row_spills = [0.0] * segment.npts
    start_stage_value = start_stage

    iterations = 1
    if segment_has_spill_targets(segment) or is_spill_end(segment) or any(abs(value) > EPS for value in inflows):
        iterations = params.max_segment_iterations

    for _ in range(iterations):
        q_array = build_section_discharge_array(segment, q_in, row_spills, inflows)
        if start_stage_value is None:
            start_stage_value = heuristic_start_stage(segment, q_array[0], params)
        forward_rows = solve_forward_profile(segment, q_array, start_stage_value, params)
        backward_rows = solve_backward_profile(segment, q_array, end_stage, params)
        profile = assemble_profile_dataframe(segment, q_array, mode, forward_rows, backward_rows, inflows, params)
        spill_eval = evaluate_spills(segment, profile, q_in, inflows, params)
        next_row_spills = list(spill_eval["spill_total"])
        if max(abs(a - b) for a, b in zip(next_row_spills, row_spills)) < params.outer_tolerance:
            row_spills = next_row_spills
            break
        row_spills = next_row_spills

    q_array = build_section_discharge_array(segment, q_in, row_spills, inflows)
    if start_stage_value is None:
        start_stage_value = heuristic_start_stage(segment, q_array[0], params)
    forward_rows = solve_forward_profile(segment, q_array, start_stage_value, params)
    backward_rows = solve_backward_profile(segment, q_array, end_stage, params)
    profile = assemble_profile_dataframe(segment, q_array, mode, forward_rows, backward_rows, inflows, params)
    spill_eval = evaluate_spills(segment, profile, q_in, inflows, params)
    return finalize_profile_dataframe(segment, profile, spill_eval)


def map_spills_to_recipient(
    recipient: Segment,
    donor_profiles: Dict[str, pd.DataFrame],
    segments_by_name: Dict[str, Segment],
) -> Tuple[List[float], pd.DataFrame]:
    inflows = [0.0] * recipient.npts
    mapping_rows: List[Dict[str, object]] = []
    recipient_col = f"spill_to_{sanitize_name(recipient.name)}"

    for donor_name, donor_profile in donor_profiles.items():
        if recipient_col not in donor_profile.columns:
            continue
        donor_segment = segments_by_name[donor_name]
        donor_indices = configured_spill_rows(donor_segment, recipient.name)
        for seq, donor_idx in enumerate(donor_indices):
            target_idx = 2 + seq
            if target_idx >= recipient.npts:
                break
            q_value = float(donor_profile.loc[donor_idx, recipient_col])
            inflows[target_idx] += q_value
            mapping_rows.append(
                {
                    "donor_segment": donor_name,
                    "recipient_segment": recipient.name,
                    "donor_sn": int(donor_profile.loc[donor_idx, "sn"]),
                    "donor_chainage": float(donor_profile.loc[donor_idx, "chainage"]),
                    "recipient_sn": int(recipient.df.reset_index(drop=True).loc[target_idx, "SN"]),
                    "recipient_chainage": float(recipient.df.reset_index(drop=True).loc[target_idx, "Chainage"]),
                    "mapped_sequence": seq + 1,
                    "mapped_q": q_value,
                }
            )

    return inflows, pd.DataFrame(mapping_rows)


def profile_summary(segment: Segment, profile_df: pd.DataFrame, q_in: float, mode: str, h_junc: float) -> Dict[str, float]:
    q_spill = float(profile_df["spill_total"].sum())
    q_lateral = float(profile_df["lateral_inflow"].sum())
    q_out = float(profile_df["q_after_row"].iloc[-1])
    if is_closed_outlet(segment) or is_spill_end(segment):
        q_out = 0.0
    return {
        "segment": segment.name,
        "from_node": segment.from_node,
        "to_node": segment.to_node,
        "n_points": segment.npts,
        "length": segment.length,
        "start_width": segment.start_width,
        "end_width": segment.end_width,
        "start_side": segment.start_side,
        "end_side": segment.end_side,
        "start_bed": segment.start_bed,
        "end_bed": segment.end_bed,
        "upstream_bc_type": canonical_bc_type(segment.upstream_bc_type),
        "upstream_bc_value": segment.upstream_bc_value,
        "downstream_bc_type": canonical_bc_type(segment.downstream_bc_type),
        "downstream_bc_value": segment.downstream_bc_value,
        "ws_start": float(profile_df["ws_final"].iloc[0]),
        "ws_end": float(profile_df["ws_final"].iloc[-1]),
        "q_in": q_in,
        "q_lateral_in_total": q_lateral,
        "q_spill": q_spill,
        "q_out": q_out,
        "y_avg": float(profile_df["y_final"].mean()),
        "sf_avg": float(profile_df["sf_final"].mean()),
        "hf": segment.length * float(profile_df["sf_final"].mean()),
        "h_junc": h_junc,
        "h_total": segment.length * float(profile_df["sf_final"].mean()) + h_junc,
        "profile_mode": mode,
        "start_angle_deg": segment.start_angle,
        "end_angle_deg": segment.end_angle,
    }


def solve_network(
    segments: List[Segment],
    boundary_conditions: List[BoundaryCondition],
    params: SolverParameters,
    profile_modes: Dict[str, str],
) -> Dict[str, object]:
    segments_by_name = {segment.name: segment for segment in segments}
    qout_guess = initial_qout_guess(params, segments_by_name)
    alpha1_guess = params.init_alpha1
    alpha2_guess = params.init_alpha2
    j1_guess = params.init_j1_wse
    j2_guess = params.init_j2_wse

    outer_records: List[Dict[str, float]] = []
    final_boundary_rows: List[Dict[str, float]] = []
    final_profiles: Dict[str, pd.DataFrame] = {}
    final_junction_df = pd.DataFrame()
    final_junction_state: Dict[str, float] = {}
    final_spill_mapping = pd.DataFrame()

    for outer_iter in range(params.max_outer_iterations):
        boundary_stage_map, boundary_rows = resolve_boundary_stages(boundary_conditions, segments_by_name, qout_guess, params)

        junction_state, junction_df = solve_junction_network(
            segments_by_name,
            boundary_stage_map,
            params,
            init_alpha1=alpha1_guess,
            init_alpha2=alpha2_guess,
            init_j1=j1_guess,
            init_j2=j2_guess,
        )

        q_in_map = {
            "UsProject": params.total_inflow_q,
            "waterway": junction_state["q_waterway"],
            "UsExit": junction_state["q_usexit"],
            "forebay": junction_state["q_forebay"],
            "sidechannel": junction_state["q_sidechannel"],
            "drain": 0.0,
        }

        profiles: Dict[str, pd.DataFrame] = {}
        profiles["UsProject"] = solve_segment_profile(
            segments_by_name["UsProject"],
            q_in=q_in_map["UsProject"],
            start_stage=boundary_stage_map[boundary_label("UsProject", "start")],
            end_stage=junction_state["j1_wse"],
            mode=profile_modes["UsProject"],
            params=params,
        )
        profiles["waterway"] = solve_segment_profile(
            segments_by_name["waterway"],
            q_in=q_in_map["waterway"],
            start_stage=junction_state["j1_wse"],
            end_stage=junction_state["j2_wse"],
            mode=profile_modes["waterway"],
            params=params,
        )
        profiles["UsExit"] = solve_segment_profile(
            segments_by_name["UsExit"],
            q_in=q_in_map["UsExit"],
            start_stage=junction_state["j1_wse"],
            end_stage=boundary_stage_map[boundary_label("UsExit", "end")],
            mode=profile_modes["UsExit"],
            params=params,
        )
        profiles["forebay"] = solve_segment_profile(
            segments_by_name["forebay"],
            q_in=q_in_map["forebay"],
            start_stage=junction_state["j2_wse"],
            end_stage=boundary_stage_map[boundary_label("forebay", "end")],
            mode=profile_modes["forebay"],
            params=params,
        )
        profiles["sidechannel"] = solve_segment_profile(
            segments_by_name["sidechannel"],
            q_in=q_in_map["sidechannel"],
            start_stage=junction_state["j2_wse"],
            end_stage=boundary_stage_map[boundary_label("sidechannel", "end")],
            mode=profile_modes["sidechannel"],
            params=params,
        )

        drain_inflows, spill_mapping_df = map_spills_to_recipient(
            segments_by_name["drain"],
            {name: profiles[name] for name in ("UsProject", "waterway", "UsExit", "forebay", "sidechannel") if name in profiles},
            segments_by_name,
        )
        q_in_map["drain"] = 0.0 if is_spill_zero(segments_by_name["drain"]) else sum(drain_inflows)
        boundary_stage_map[boundary_label("drain", "end")] = resolve_boundary_stage(
            next(boundary for boundary in boundary_conditions if boundary.label == boundary_label("drain", "end")),
            segments_by_name["drain"],
            max(sum(drain_inflows), qout_guess.get("drain", 0.0)),
            params,
        )
        for row in boundary_rows:
            if row["label"] == boundary_label("drain", "end"):
                row["q_reference"] = max(sum(drain_inflows), qout_guess.get("drain", 0.0))
                row["resolved_stage"] = boundary_stage_map[boundary_label("drain", "end")]
                break

        profiles["drain"] = solve_segment_profile(
            segments_by_name["drain"],
            q_in=q_in_map["drain"],
            start_stage=boundary_stage_map[boundary_label("drain", "start")],
            end_stage=boundary_stage_map[boundary_label("drain", "end")],
            mode=profile_modes["drain"],
            params=params,
            lateral_inflows=drain_inflows,
        )

        qout_new = {name: float(profile["q_after_row"].iloc[-1]) for name, profile in profiles.items()}
        if is_closed_outlet(segments_by_name["UsExit"]):
            qout_new["UsExit"] = 0.0
        if is_closed_outlet(segments_by_name["forebay"]):
            qout_new["forebay"] = 0.0
        if is_closed_outlet(segments_by_name["sidechannel"]) or is_spill_end(segments_by_name["sidechannel"]):
            qout_new["sidechannel"] = 0.0

        outer_records.append(
            {
                "outer_iteration": outer_iter,
                "alpha1": junction_state["alpha1"],
                "alpha2": junction_state["alpha2"],
                "j1_wse": junction_state["j1_wse"],
                "j2_wse": junction_state["j2_wse"],
                "err_j1": junction_state["err_j1"],
                "err_j2": junction_state["err_j2"],
                "q_waterway": q_in_map["waterway"],
                "q_usexit": q_in_map["UsExit"],
                "q_forebay": q_in_map["forebay"],
                "q_sidechannel": q_in_map["sidechannel"],
                "spill_forebay_to_drain": float(profiles["forebay"].get("spill_to_drain", pd.Series(dtype=float)).sum()),
                "spill_sidechannel_to_drain": float(profiles["sidechannel"].get("spill_to_drain", pd.Series(dtype=float)).sum()),
                "spill_to_drain_total": float(sum(drain_inflows)),
                "q_drain_out": float(profiles["drain"]["q_after_row"].iloc[-1]),
                "max_qout_change": max(abs(qout_new[name] - qout_guess.get(name, 0.0)) for name in qout_new),
            }
        )

        alpha1_guess = junction_state["alpha1"]
        alpha2_guess = junction_state["alpha2"]
        j1_guess = junction_state["j1_wse"]
        j2_guess = junction_state["j2_wse"]

        final_boundary_rows = boundary_rows
        final_profiles = profiles
        final_junction_df = junction_df
        final_junction_state = junction_state
        final_spill_mapping = spill_mapping_df

        qout_delta = max(abs(qout_new[name] - qout_guess.get(name, 0.0)) for name in qout_new)
        qout_guess = qout_new
        if qout_delta < params.outer_tolerance:
            break

    summary_rows: List[Dict[str, float]] = []
    for segment in segments:
        h_junc = 0.0
        if segment.name == "waterway":
            h_junc = segment_end_terms(
                segment,
                final_junction_state["q_waterway"],
                final_junction_state["j1_wse"],
                final_junction_state["j2_wse"],
                params,
                final_junction_state["q_usexit"],
                params.total_inflow_q,
            ).hj
        elif segment.name == "UsExit":
            h_junc = segment_end_terms(
                segment,
                final_junction_state["q_usexit"],
                final_junction_state["j1_wse"],
                final_boundary_rows[[row["label"] for row in final_boundary_rows].index(boundary_label("UsExit", "end"))]["resolved_stage"],
                params,
                final_junction_state["q_waterway"],
                params.total_inflow_q,
            ).hj
        elif segment.name == "forebay":
            h_junc = segment_end_terms(
                segment,
                final_junction_state["q_forebay"],
                final_junction_state["j2_wse"],
                final_boundary_rows[[row["label"] for row in final_boundary_rows].index(boundary_label("forebay", "end"))]["resolved_stage"],
                params,
                final_junction_state["q_sidechannel"],
                max(final_junction_state["q_waterway"], EPS),
            ).hj
        elif segment.name == "sidechannel":
            h_junc = segment_end_terms(
                segment,
                final_junction_state["q_sidechannel"],
                final_junction_state["j2_wse"],
                final_boundary_rows[[row["label"] for row in final_boundary_rows].index(boundary_label("sidechannel", "end"))]["resolved_stage"],
                params,
                final_junction_state["q_forebay"],
                max(final_junction_state["q_waterway"], EPS),
            ).hj

        q_in = (
            params.total_inflow_q
            if segment.name == "UsProject"
            else final_junction_state["q_waterway"]
            if segment.name == "waterway"
            else final_junction_state["q_usexit"]
            if segment.name == "UsExit"
            else final_junction_state["q_forebay"]
            if segment.name == "forebay"
            else final_junction_state["q_sidechannel"]
            if segment.name == "sidechannel"
            else 0.0
        )
        summary_rows.append(profile_summary(segment, final_profiles[segment.name], q_in=q_in, mode=profile_modes[segment.name], h_junc=h_junc))

    return {
        "boundaries": pd.DataFrame(final_boundary_rows),
        "outer_iterations": pd.DataFrame(outer_records),
        "junction_iterations": final_junction_df,
        "segment_summary": pd.DataFrame(summary_rows),
        "profiles": final_profiles,
        "spill_mapping": final_spill_mapping,
    }


def write_results(output_dir: Path, params: SolverParameters, solution: Dict[str, object]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([asdict(params)]).to_csv(output_dir / "run_parameters.csv", index=False)
    solution["boundaries"].to_csv(output_dir / "boundary_conditions.csv", index=False)
    solution["outer_iterations"].to_csv(output_dir / "outer_iterations.csv", index=False)
    solution["junction_iterations"].to_csv(output_dir / "junction_iterations.csv", index=False)
    solution["segment_summary"].to_csv(output_dir / "segment_summary.csv", index=False)
    solution["spill_mapping"].to_csv(output_dir / "spill_mapping.csv", index=False)

    profiles: Dict[str, pd.DataFrame] = solution["profiles"]  # type: ignore[assignment]
    for segment_name, profile_df in profiles.items():
        profile_df.to_csv(output_dir / f"profile_{segment_name}.csv", index=False)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Offline steady 1D open-channel network solver.")
    parser.add_argument("--network-dir", type=Path, default=DEFAULT_NETWORK_DIR, help="Directory containing master.xlsx and reach CSVs.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Directory where CSV outputs will be written.")
    parser.add_argument("--total-q", type=float, default=None, help="Optional override for total inflow discharge.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    params = SolverParameters()

    segments, boundaries, detected_total_q = load_network(args.network_dir)
    if args.total_q is not None:
        params.total_inflow_q = args.total_q
    elif detected_total_q is not None:
        params.total_inflow_q = detected_total_q

    solution = solve_network(segments, boundaries, params, DEFAULT_PROFILE_MODES)
    write_results(args.output_dir, params, solution)

    summary_df: pd.DataFrame = solution["segment_summary"]  # type: ignore[assignment]
    print(f"Wrote steady 1D results to {args.output_dir.resolve()}")
    print(summary_df[["segment", "q_in", "q_lateral_in_total", "q_spill", "q_out", "ws_start", "ws_end"]].to_string(index=False))


if __name__ == "__main__":
    main()
