from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

import pandas as pd

from configProject import ChannelGeometry, canonical_bc_type, load_project


EPS = 1e-9


@dataclass(frozen=True)
class HydraulicParams:
    manning_n: float = 0.015
    gravity: float = 9.81
    alpha: float = 1.0
    beta: float = 1.0
    expansion_coeff: float = 0.30
    contraction_coeff: float = 0.10
    bend_coeff: float = 0.15
    min_depth: float = 0.05
    boundary_slope: float = 0.001
    profile_tolerance: float = 1e-4
    root_samples: int = 350
    max_depth: float = 200.0


@dataclass(frozen=True)
class SectionHydraulics:
    depth: float
    area: float
    wetted_perimeter: float
    hydraulic_radius: float
    top_width: float
    velocity: float
    velocity_head: float
    water_surface: float
    energy_grade: float
    friction_slope: float
    froude: float
    specific_force: float


@dataclass(frozen=True)
class StepSolve:
    depth: float
    error: float
    local_loss: float
    converged: bool
    critical_default: bool


def trap_area(width: float, side_slope: float, depth: float) -> float:
    return max((width + side_slope * depth) * depth, EPS)


def trap_wetted_perimeter(width: float, side_slope: float, depth: float) -> float:
    return max(width + 2.0 * depth * math.sqrt(1.0 + side_slope**2), EPS)


def trap_top_width(width: float, side_slope: float, depth: float) -> float:
    return max(width + 2.0 * side_slope * depth, EPS)


def hydraulic_state(
    *,
    bed_elevation: float,
    width: float,
    side_slope: float,
    discharge: float,
    depth: float,
    params: HydraulicParams,
) -> SectionHydraulics:
    depth = max(params.min_depth, depth)
    area = trap_area(width, side_slope, depth)
    perimeter = trap_wetted_perimeter(width, side_slope, depth)
    radius = area / perimeter
    top_width = trap_top_width(width, side_slope, depth)
    velocity = abs(discharge) / area
    velocity_head = params.alpha * velocity**2 / (2.0 * params.gravity)
    water_surface = bed_elevation + depth
    energy_grade = water_surface + velocity_head
    friction_slope = ((abs(discharge) * params.manning_n) / (area * max(radius ** (2.0 / 3.0), EPS))) ** 2
    froude = velocity / math.sqrt(params.gravity * max(area / top_width, EPS))
    hydrostatic_momentum = (width * depth**2) / 2.0 + (side_slope * depth**3) / 3.0
    specific_force = params.beta * discharge**2 / (params.gravity * area) + hydrostatic_momentum
    return SectionHydraulics(
        depth=depth,
        area=area,
        wetted_perimeter=perimeter,
        hydraulic_radius=radius,
        top_width=top_width,
        velocity=velocity,
        velocity_head=velocity_head,
        water_surface=water_surface,
        energy_grade=energy_grade,
        friction_slope=friction_slope,
        froude=froude,
        specific_force=specific_force,
    )


def section_state(channel: ChannelGeometry, index: int, discharge: float, depth: float, params: HydraulicParams) -> SectionHydraulics:
    row = channel.dataframe.iloc[index]
    return hydraulic_state(
        bed_elevation=float(row["bed_elevation"]),
        width=float(row["bed_width"]),
        side_slope=float(row["side_slope"]),
        discharge=discharge,
        depth=depth,
        params=params,
    )


def critical_depth(discharge: float, width: float, side_slope: float, params: HydraulicParams) -> float:
    if abs(discharge) <= EPS:
        return params.min_depth

    def residual(depth: float) -> float:
        area = trap_area(width, side_slope, depth)
        top_width = trap_top_width(width, side_slope, depth)
        return discharge**2 * top_width / (params.gravity * max(area**3, EPS)) - 1.0

    return max(params.min_depth, _solve_positive_root(residual, params.min_depth, params.max_depth))


def normal_depth(discharge: float, width: float, side_slope: float, slope: float, params: HydraulicParams) -> float:
    if abs(discharge) <= EPS:
        return params.min_depth
    slope = max(abs(slope), params.boundary_slope, EPS)

    def residual(depth: float) -> float:
        area = trap_area(width, side_slope, depth)
        perimeter = trap_wetted_perimeter(width, side_slope, depth)
        radius = area / perimeter
        conveyance = area * max(radius ** (2.0 / 3.0), EPS) * math.sqrt(slope) / max(params.manning_n, EPS)
        return conveyance - abs(discharge)

    return max(params.min_depth, _solve_positive_root(residual, params.min_depth, params.max_depth))


def channel_bed_slope(channel: ChannelGeometry, params: HydraulicParams) -> float:
    if channel.length <= EPS:
        return params.boundary_slope
    return max(abs((channel.end_bed - channel.start_bed) / channel.length), params.boundary_slope)


def resolve_stage(
    channel: ChannelGeometry,
    *,
    end: str,
    discharge: float,
    bc_type: Optional[str] = None,
    bc_value: Optional[float] = None,
    params: HydraulicParams,
) -> float:
    at_start = end.lower() in {"start", "upstream", "inlet"}
    if bc_type is None:
        bc_type = channel.upstream_bc_type if at_start else channel.downstream_bc_type
    if bc_value is None:
        bc_value = channel.upstream_bc_value if at_start else channel.downstream_bc_value

    idx = 0 if at_start else channel.n_sections - 1
    row = channel.dataframe.iloc[idx]
    bed = float(row["bed_elevation"])
    width = float(row["bed_width"])
    side = float(row["side_slope"])
    bc = canonical_bc_type(bc_type)

    if bc == "Known WSE":
        return float(bc_value if bc_value is not None else bed + params.min_depth)
    if bc == "Known Depth":
        return bed + float(bc_value if bc_value is not None else params.min_depth)
    if bc == "Critical Depth":
        return bed + critical_depth(discharge, width, side, params)
    if bc == "Normal Depth" or not bc:
        return bed + normal_depth(discharge, width, side, channel_bed_slope(channel, params), params)
    if bc in {"None", "Spill End", "Spill Zero"}:
        return bed + params.min_depth
    raise ValueError(f"Unsupported boundary condition type: {bc_type}")


def _transition_loss(up: SectionHydraulics, dn: SectionHydraulics, deflection_angle_deg: float, params: HydraulicParams) -> float:
    if dn.velocity > up.velocity:
        transition = params.contraction_coeff * (dn.velocity_head - up.velocity_head)
    else:
        transition = params.expansion_coeff * (up.velocity_head - dn.velocity_head)
    bend = params.bend_coeff * (abs(deflection_angle_deg) / 90.0) * max(up.velocity_head, dn.velocity_head)
    return transition + bend


def _solve_bisection(func, low: float, high: float, iterations: int = 90) -> float:
    f_low = func(low)
    f_high = func(high)
    if abs(f_low) < 1e-12:
        return low
    if abs(f_high) < 1e-12:
        return high
    for _ in range(iterations):
        mid = 0.5 * (low + high)
        f_mid = func(mid)
        if abs(f_mid) < 1e-10 or abs(high - low) < 1e-8:
            return mid
        if f_low * f_mid <= 0.0:
            high = mid
            f_high = f_mid
        else:
            low = mid
            f_low = f_mid
    return 0.5 * (low + high)


def _solve_positive_root(func, min_depth: float, max_depth: float, samples: int = 250) -> float:
    low = max(min_depth, 1e-4)
    high = max(max_depth, low * 2.0)
    best_x = low
    best_error = abs(func(low))
    prev_x = low
    prev_f = func(prev_x)
    for idx in range(1, samples + 1):
        x = low + (high - low) * idx / samples
        f_x = func(x)
        if abs(f_x) < best_error:
            best_x = x
            best_error = abs(f_x)
        if prev_f * f_x <= 0.0:
            return _solve_bisection(func, prev_x, x)
        prev_x = x
        prev_f = f_x
    return best_x


def _find_standard_step_depth(
    residual,
    *,
    min_depth: float,
    max_depth: float,
    prefer: str,
    params: HydraulicParams,
) -> Tuple[float, float, bool]:
    low = max(min_depth, 1e-4)
    high = max(max_depth, low + 1.0)
    roots: List[Tuple[float, float]] = []
    best_depth = low
    best_error = abs(residual(low))
    prev_depth = low
    prev_error = residual(prev_depth)

    for idx in range(1, params.root_samples + 1):
        depth = low + (high - low) * idx / params.root_samples
        error = residual(depth)
        if abs(error) < best_error:
            best_depth = depth
            best_error = abs(error)
        if prev_error * error <= 0.0:
            roots.append((prev_depth, depth))
        prev_depth = depth
        prev_error = error

    if roots:
        bracket = roots[-1] if prefer == "highest" else roots[0]
        depth = _solve_bisection(residual, bracket[0], bracket[1])
        return depth, residual(depth), True
    return best_depth, residual(best_depth), False


def _q_at(q_profile: float | Sequence[float], index: int) -> float:
    if isinstance(q_profile, (int, float)):
        return float(q_profile)
    return float(q_profile[index])


def _q_array(q_profile: float | Sequence[float], count: int) -> List[float]:
    if isinstance(q_profile, (int, float)):
        return [float(q_profile)] * count
    if len(q_profile) != count:
        raise ValueError(f"q_profile length {len(q_profile)} does not match channel section count {count}")
    return [float(value) for value in q_profile]


def _row_from_state(
    channel: ChannelGeometry,
    index: int,
    discharge: float,
    state: SectionHydraulics,
    *,
    pass_name: str,
    error: float,
    local_loss: float,
    done: bool,
    critical_default: bool,
) -> dict:
    row = channel.dataframe.iloc[index]
    return {
        "index": index,
        "sn": int(row["sn"]),
        "chainage": float(row["chainage"]),
        "easting": float(row["easting"]),
        "northing": float(row["northing"]),
        "bed_elevation": float(row["bed_elevation"]),
        "bed_width": float(row["bed_width"]),
        "side_slope": float(row["side_slope"]),
        "section_type": str(row.get("section_type", "")),
        "deflection_angle_deg": float(row["deflection_angle"]),
        "q": discharge,
        "pass": pass_name,
        "depth": state.depth,
        "area": state.area,
        "wetted_perimeter": state.wetted_perimeter,
        "hydraulic_radius": state.hydraulic_radius,
        "top_width": state.top_width,
        "velocity": state.velocity,
        "velocity_head": state.velocity_head,
        "water_surface": state.water_surface,
        "energy_grade": state.energy_grade,
        "friction_slope": state.friction_slope,
        "froude": state.froude,
        "specific_force": state.specific_force,
        "error": error,
        "local_loss": local_loss,
        "done": done,
        "critical_default": critical_default,
        "regime": "Subcritical" if state.froude < 1.0 else "Supercritical",
    }


def _critical_row(channel: ChannelGeometry, index: int, discharge: float, params: HydraulicParams, *, pass_name: str, error: float = 0.0, local_loss: float = 0.0) -> dict:
    row = channel.dataframe.iloc[index]
    depth = critical_depth(discharge, float(row["bed_width"]), float(row["side_slope"]), params)
    state = section_state(channel, index, discharge, depth, params)
    return _row_from_state(
        channel,
        index,
        discharge,
        state,
        pass_name=pass_name,
        error=error,
        local_loss=local_loss,
        done=True,
        critical_default=True,
    )


def _forward_error(channel: ChannelGeometry, prev: dict, curr_state: SectionHydraulics, idx: int, params: HydraulicParams) -> Tuple[float, float]:
    dx = float(channel.dataframe.iloc[idx]["chainage"] - channel.dataframe.iloc[idx - 1]["chainage"])
    prev_state = SectionHydraulics(
        depth=float(prev["depth"]),
        area=float(prev["area"]),
        wetted_perimeter=float(prev["wetted_perimeter"]),
        hydraulic_radius=float(prev["hydraulic_radius"]),
        top_width=float(prev["top_width"]),
        velocity=float(prev["velocity"]),
        velocity_head=float(prev["velocity_head"]),
        water_surface=float(prev["water_surface"]),
        energy_grade=float(prev["energy_grade"]),
        friction_slope=float(prev["friction_slope"]),
        froude=float(prev["froude"]),
        specific_force=float(prev["specific_force"]),
    )
    local_loss = _transition_loss(prev_state, curr_state, float(channel.dataframe.iloc[idx]["deflection_angle"]), params)
    hf = 0.5 * (prev_state.friction_slope + curr_state.friction_slope) * dx
    return prev_state.energy_grade - hf - local_loss - curr_state.energy_grade, local_loss


def _backward_error(channel: ChannelGeometry, curr_state: SectionHydraulics, next_row: dict, idx: int, params: HydraulicParams) -> Tuple[float, float]:
    dx = float(channel.dataframe.iloc[idx + 1]["chainage"] - channel.dataframe.iloc[idx]["chainage"])
    next_state = SectionHydraulics(
        depth=float(next_row["depth"]),
        area=float(next_row["area"]),
        wetted_perimeter=float(next_row["wetted_perimeter"]),
        hydraulic_radius=float(next_row["hydraulic_radius"]),
        top_width=float(next_row["top_width"]),
        velocity=float(next_row["velocity"]),
        velocity_head=float(next_row["velocity_head"]),
        water_surface=float(next_row["water_surface"]),
        energy_grade=float(next_row["energy_grade"]),
        friction_slope=float(next_row["friction_slope"]),
        froude=float(next_row["froude"]),
        specific_force=float(next_row["specific_force"]),
    )
    local_loss = _transition_loss(curr_state, next_state, float(channel.dataframe.iloc[idx + 1]["deflection_angle"]), params)
    hf = 0.5 * (curr_state.friction_slope + next_state.friction_slope) * dx
    return next_state.energy_grade + hf + local_loss - curr_state.energy_grade, local_loss


def forward_pass(
    channel: ChannelGeometry,
    q_profile: float | Sequence[float],
    upstream_stage: float,
    params: Optional[HydraulicParams] = None,
) -> pd.DataFrame:
    params = params or HydraulicParams()
    q_values = _q_array(q_profile, channel.n_sections)
    records: List[dict] = []

    first_depth = max(params.min_depth, upstream_stage - float(channel.dataframe.iloc[0]["bed_elevation"]))
    first_state = section_state(channel, 0, q_values[0], first_depth, params)
    records.append(
        _row_from_state(
            channel,
            0,
            q_values[0],
            first_state,
            pass_name="forward",
            error=0.0,
            local_loss=0.0,
            done=True,
            critical_default=False,
        )
    )

    for idx in range(1, channel.n_sections):
        row = channel.dataframe.iloc[idx]
        bed = float(row["bed_elevation"])
        width = float(row["bed_width"])
        side = float(row["side_slope"])
        q_here = q_values[idx]
        y_crit = critical_depth(q_here, width, side, params)
        max_depth = max(params.max_depth, records[-1]["depth"] * 3.0 + 5.0, y_crit * 5.0 + 5.0)

        def residual(depth: float) -> float:
            state = hydraulic_state(
                bed_elevation=bed,
                width=width,
                side_slope=side,
                discharge=q_here,
                depth=max(params.min_depth, depth),
                params=params,
            )
            error, _ = _forward_error(channel, records[-1], state, idx, params)
            return error

        depth, error, has_root = _find_standard_step_depth(
            residual,
            min_depth=params.min_depth,
            max_depth=max_depth,
            prefer="lowest",
            params=params,
        )
        state = section_state(channel, idx, q_here, depth, params)
        error, local_loss = _forward_error(channel, records[-1], state, idx, params)
        valid_supercritical = state.froude > 1.0 or round(state.froude, 3) == 1.0
        if not has_root or not valid_supercritical:
            records.append(_critical_row(channel, idx, q_here, params, pass_name="forward", error=error, local_loss=local_loss))
        else:
            records.append(
                _row_from_state(
                    channel,
                    idx,
                    q_here,
                    state,
                    pass_name="forward",
                    error=error,
                    local_loss=local_loss,
                    done=abs(error) <= params.profile_tolerance,
                    critical_default=False,
                )
            )

    return pd.DataFrame(records)


def backward_pass(
    channel: ChannelGeometry,
    q_profile: float | Sequence[float],
    downstream_stage: float,
    params: Optional[HydraulicParams] = None,
) -> pd.DataFrame:
    params = params or HydraulicParams()
    q_values = _q_array(q_profile, channel.n_sections)
    records: List[Optional[dict]] = [None] * channel.n_sections

    last_idx = channel.n_sections - 1
    last_depth = max(params.min_depth, downstream_stage - float(channel.dataframe.iloc[last_idx]["bed_elevation"]))
    last_state = section_state(channel, last_idx, q_values[last_idx], last_depth, params)
    records[last_idx] = _row_from_state(
        channel,
        last_idx,
        q_values[last_idx],
        last_state,
        pass_name="backward",
        error=0.0,
        local_loss=0.0,
        done=True,
        critical_default=False,
    )

    for idx in range(last_idx - 1, -1, -1):
        assert records[idx + 1] is not None
        row = channel.dataframe.iloc[idx]
        bed = float(row["bed_elevation"])
        width = float(row["bed_width"])
        side = float(row["side_slope"])
        q_here = q_values[idx]
        y_crit = critical_depth(q_here, width, side, params)
        max_depth = max(params.max_depth, records[idx + 1]["depth"] * 3.0 + 5.0, y_crit * 5.0 + 5.0)

        def residual(depth: float) -> float:
            state = hydraulic_state(
                bed_elevation=bed,
                width=width,
                side_slope=side,
                discharge=q_here,
                depth=max(params.min_depth, depth),
                params=params,
            )
            error, _ = _backward_error(channel, state, records[idx + 1], idx, params)
            return error

        depth, error, has_root = _find_standard_step_depth(
            residual,
            min_depth=params.min_depth,
            max_depth=max_depth,
            prefer="highest",
            params=params,
        )
        state = section_state(channel, idx, q_here, depth, params)
        error, local_loss = _backward_error(channel, state, records[idx + 1], idx, params)
        valid_subcritical = state.froude < 1.0 or round(state.froude, 3) == 1.0
        if not has_root or not valid_subcritical:
            records[idx] = _critical_row(channel, idx, q_here, params, pass_name="backward", error=error, local_loss=local_loss)
        else:
            records[idx] = _row_from_state(
                channel,
                idx,
                q_here,
                state,
                pass_name="backward",
                error=error,
                local_loss=local_loss,
                done=abs(error) <= params.profile_tolerance,
                critical_default=False,
            )

    return pd.DataFrame([record for record in records if record is not None])


def mixed_profile(
    channel: ChannelGeometry,
    q_profile: float | Sequence[float],
    upstream_stage: float,
    downstream_stage: float,
    params: Optional[HydraulicParams] = None,
) -> pd.DataFrame:
    params = params or HydraulicParams()
    forward = forward_pass(channel, q_profile, upstream_stage, params)
    backward = backward_pass(channel, q_profile, downstream_stage, params)
    rows = channel.dataframe.reset_index(drop=True)
    q_values = _q_array(q_profile, channel.n_sections)
    records: List[dict] = []
    previous_source: Optional[str] = None

    for idx in range(channel.n_sections):
        fwd = forward.iloc[idx]
        bwd = backward.iloc[idx]
        if float(fwd["specific_force"]) >= float(bwd["specific_force"]):
            selected = fwd
            source = "supercritical"
        else:
            selected = bwd
            source = "subcritical"

        jump_flag = previous_source == "supercritical" and source == "subcritical"
        previous_source = source
        row = rows.iloc[idx]
        records.append(
            {
                "channel": channel.name,
                "index": idx,
                "sn": int(row["sn"]),
                "chainage": float(row["chainage"]),
                "easting": float(row["easting"]),
                "northing": float(row["northing"]),
                "bed_elevation": float(row["bed_elevation"]),
                "bed_width": float(row["bed_width"]),
                "side_slope": float(row["side_slope"]),
                "q": q_values[idx],
                "y_super": float(fwd["depth"]),
                "ws_super": float(fwd["water_surface"]),
                "fr_super": float(fwd["froude"]),
                "m_super": float(fwd["specific_force"]),
                "critical_default_super": bool(fwd["critical_default"]),
                "y_sub": float(bwd["depth"]),
                "ws_sub": float(bwd["water_surface"]),
                "fr_sub": float(bwd["froude"]),
                "m_sub": float(bwd["specific_force"]),
                "critical_default_sub": bool(bwd["critical_default"]),
                "y_final": float(selected["depth"]),
                "ws_final": float(selected["water_surface"]),
                "eg_final": float(selected["energy_grade"]),
                "fr_final": float(selected["froude"]),
                "m_final": float(selected["specific_force"]),
                "selected_profile": source,
                "hydraulic_jump_between_previous_section": jump_flag,
            }
        )

    return pd.DataFrame(records)


def solve_channel(
    channel: ChannelGeometry,
    q_profile: float | Sequence[float],
    upstream_stage: Optional[float] = None,
    downstream_stage: Optional[float] = None,
    mode: str = "Mixed",
    params: Optional[HydraulicParams] = None,
) -> pd.DataFrame:
    params = params or HydraulicParams()
    q0 = _q_at(q_profile, 0)
    qn = _q_at(q_profile, channel.n_sections - 1)
    if upstream_stage is None:
        upstream_stage = resolve_stage(channel, end="start", discharge=q0, params=params)
    if downstream_stage is None:
        downstream_stage = resolve_stage(channel, end="end", discharge=qn, params=params)

    key = mode.strip().lower()
    if key == "supercritical":
        return forward_pass(channel, q_profile, upstream_stage, params)
    if key == "subcritical":
        return backward_pass(channel, q_profile, downstream_stage, params)
    if key == "mixed":
        return mixed_profile(channel, q_profile, upstream_stage, downstream_stage, params)
    raise ValueError("mode must be one of: Mixed, Subcritical, Supercritical")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a two-pass steady 1D solve for one channel.")
    parser.add_argument("--network-dir", type=Path, default=Path("MTGHP-core"), help="Folder containing master.xlsx and channel CSVs.")
    parser.add_argument("--channel", default="waterway", help="Channel name from master.xlsx.")
    parser.add_argument("--q", type=float, default=51.0, help="Discharge for the channel pass.")
    parser.add_argument("--upstream-stage", type=float, default=None, help="Optional upstream stage override.")
    parser.add_argument("--downstream-stage", type=float, default=None, help="Optional downstream stage override.")
    parser.add_argument("--mode", default="Mixed", choices=["Mixed", "Subcritical", "Supercritical"], help="Profile mode.")
    parser.add_argument("--output", type=Path, default=None, help="Optional CSV output path.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    project = load_project(args.network_dir)
    if args.channel not in project.channels:
        raise KeyError(f"Unknown channel '{args.channel}'. Available: {', '.join(project.channel_names())}")
    result = solve_channel(
        project.channels[args.channel],
        args.q,
        upstream_stage=args.upstream_stage,
        downstream_stage=args.downstream_stage,
        mode=args.mode,
    )
    print(result.tail(10).to_string(index=False))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        result.to_csv(args.output, index=False)
        print(f"Wrote {args.output.resolve()}")


if __name__ == "__main__":
    main()
