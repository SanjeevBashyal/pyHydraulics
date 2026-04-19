import argparse
import math
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.workbook.properties import CalcProperties


WORKSHEET_NAME = "1D"
NETWORK_DIR = Path("MTGHP-core")
DEFAULT_OUTPUT_PATH = Path("steady_1d.xlsx")

INPUT_START_ROW = 5
SUMMARY_HEADER_ROW = 30
SUMMARY_START_ROW = 31
SOLVER_HEADER_ROW = 42
SOLVER_START_ROW = 43
SOLVER_STEPS = 80
TABLE_START_ROW = 130
WRITE_CHUNK_ROWS = 160
BOUNDARY_OPTIONS = ["Known WSE", "Known Depth", "Normal Depth", "Critical Depth", "None", "Spill End", "Spill Zero"]

TOTAL_Q_REF = "$D$5"
N_REF = "$D$6"
SIDE_DEFAULT_REF = "$D$7"
KE_REF = "$D$8"
KC_REF = "$D$9"
KB_REF = "$D$10"
KJ_REF = "$D$11"
SPILL_COEFF_REF = "$D$12"
STAGE_RELAX_REF = "$D$13"
SPLIT_RELAX_REF = "$D$14"
MOMENTUM_WT_REF = "$D$15"
MIN_DEPTH_REF = "$D$16"
DEFAULT_SLOPE_REF = "$D$17"
PROFILE_RELAX_REF = "$D$18"
PROFILE_TOL_REF = "$D$19"
RESET_REF = "$D$20"
INIT_J1_REF = "$D$21"
INIT_J2_REF = "$D$22"
INIT_A1_REF = "$D$23"
INIT_A2_REF = "$D$24"

G_REF = "$I$5"
RHO_REF = "$I$6"
FINAL_ALPHA1_REF = "$I$11"
FINAL_ALPHA2_REF = "$I$12"
FINAL_J1_REF = "$I$13"
FINAL_J2_REF = "$I$14"
TOTAL_SPILL_REF = "$I$15"

SUM_SEG = 1
SUM_FROM = 2
SUM_TO = 3
SUM_PTS = 4
SUM_LEN = 5
SUM_B0 = 6
SUM_B1 = 7
SUM_Z0 = 8
SUM_Z1 = 9
SUM_BED0 = 10
SUM_BED1 = 11
SUM_WS0 = 12
SUM_WS1 = 13
SUM_QIN = 14
SUM_SPILL = 15
SUM_QOUT = 16
SUM_YAVG = 17
SUM_SFAVG = 18
SUM_HF = 19
SUM_HJ = 20
SUM_HTOT = 21
SUM_MODE = 22
SUM_ANG0 = 23
SUM_ANG1 = 24

TABLE_SN = 1
TABLE_DIST = 2
TABLE_E = 3
TABLE_N = 4
TABLE_BED = 5
TABLE_WIDTH = 6
TABLE_SIDE = 7
TABLE_TYPE = 8
TABLE_DEF = 9
TABLE_DX = 10
TABLE_Q = 11
TABLE_YF = 12
TABLE_AF = 13
TABLE_RF = 14
TABLE_VF = 15
TABLE_WSF = 16
TABLE_EGF = 17
TABLE_SFF = 18
TABLE_ERRF = 19
TABLE_DONEF = 20
TABLE_YB = 21
TABLE_AB = 22
TABLE_RB = 23
TABLE_VB = 24
TABLE_WSB = 25
TABLE_EGB = 26
TABLE_SFB = 27
TABLE_ERRB = 28
TABLE_DONEB = 29
TABLE_Y = 30
TABLE_WS = 31
TABLE_EG = 32
TABLE_CREST_L = 33
TABLE_CREST_R = 34
TABLE_SPILL_L = 35
TABLE_SPILL_R = 36
TABLE_SPILL = 37
TABLE_CUM_SPILL = 38
TABLE_FR = 39
TABLE_REGIME = 40
TABLE_SF = 41


def col_letter(col_idx):
    result = ""
    while col_idx:
        col_idx, rem = divmod(col_idx - 1, 26)
        result = chr(65 + rem) + result
    return result


def a1(row, col):
    return f"{col_letter(col)}{row}"


def set_cell(grid, row, col, value):
    grid[row - 1][col - 1] = value


def angle_deg(dx, dy):
    return math.degrees(math.atan2(dy, dx))


def angle_diff(a1_deg, a2_deg):
    diff = (a2_deg - a1_deg + 180.0) % 360.0 - 180.0
    return abs(diff)


def yes_no(value):
    if value is None:
        return False
    return str(value).strip().lower() in {"yes", "true", "1"}


def float_or_none(value):
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def canonical_bc_type(value):
    text = " ".join(str(value).strip().lower().split())
    if not text or text == "nan":
        return ""
    mapping = {
        "known ws": "Known WSE",
        "known wse": "Known WSE",
        "known depth": "Known Depth",
        "normal depth": "Normal Depth",
        "critical depth": "Critical Depth",
        "none": "None",
        "spill end": "Spill End",
        "spill zero": "Spill Zero",
    }
    return mapping.get(text, str(value).strip())


def fill_numeric(series, default):
    values = pd.to_numeric(series, errors="coerce")
    values = values.interpolate(limit_direction="both").bfill().ffill()
    return values.fillna(default)


def optional_series(df, column_name, default_value):
    if column_name in df.columns:
        return df[column_name]
    return pd.Series([default_value] * len(df), index=df.index)


def cumulative_distances(df):
    distances = [0.0]
    east = df["Easting"].astype(float).tolist()
    north = df["Northing"].astype(float).tolist()
    for idx in range(1, len(df)):
        dx = east[idx] - east[idx - 1]
        dy = north[idx] - north[idx - 1]
        distances.append(distances[-1] + math.hypot(dx, dy))
    return distances


def deflection_angles(df):
    east = df["Easting"].astype(float).tolist()
    north = df["Northing"].astype(float).tolist()
    angles = [0.0] * len(df)
    for idx in range(1, len(df) - 1):
        a0 = angle_deg(east[idx] - east[idx - 1], north[idx] - north[idx - 1])
        a1_deg = angle_deg(east[idx + 1] - east[idx], north[idx + 1] - north[idx])
        angles[idx] = angle_diff(a0, a1_deg)
    return angles


def clean_columns(df):
    cols = []
    seen = {}
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
    df = df.copy()
    df.columns = cols
    return df


def load_network():
    master = clean_columns(pd.read_excel(NETWORK_DIR / "master.xlsx", keep_default_na=False))
    from_col = "Upstream" if "Upstream" in master.columns else "From"
    to_col = "Downstream" if "Downstream" in master.columns else "To"
    up_bc_col = "Upstream BC" if "Upstream BC" in master.columns else None
    up_bc_value_col = "Upstream BC Value" if "Upstream BC Value" in master.columns else None
    dn_bc_col = "Downstream BC" if "Downstream BC" in master.columns else None
    dn_bc_value_col = "Downstream BC Value" if "Downstream BC Value" in master.columns else None
    segments = []

    for _, record in master.iterrows():
        name = str(record["Channel"]).strip()
        df = clean_columns(pd.read_csv(NETWORK_DIR / f"{name}.csv"))

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

        segments.append({
            "name": name,
            "from_node": str(record[from_col]).strip(),
            "to_node": str(record[to_col]).strip(),
            "df": df,
            "npts": len(df),
            "length": float(df["Chainage"].iloc[-1]) if len(df) else 0.0,
            "start_width": float(df["WidthFilled"].iloc[0]),
            "end_width": float(df["WidthFilled"].iloc[-1]),
            "start_side": float(df["SideFilled"].iloc[0]),
            "end_side": float(df["SideFilled"].iloc[-1]),
            "start_bed": float(df["BedFilled"].iloc[0]),
            "end_bed": float(df["BedFilled"].iloc[-1]),
            "start_angle": start_ang,
            "end_angle": end_ang,
            "upstream_bc_type": canonical_bc_type(record[up_bc_col]) if up_bc_col else "",
            "upstream_bc_value": float_or_none(record[up_bc_value_col]) if up_bc_value_col else None,
            "downstream_bc_type": canonical_bc_type(record[dn_bc_col]) if dn_bc_col else "",
            "downstream_bc_value": float_or_none(record[dn_bc_value_col]) if dn_bc_value_col else None,
            "junction_delta": 0.0,
        })

    junctions = {}
    for seg in segments:
        junctions.setdefault(seg["from_node"], {"incoming": [], "outgoing": []})["outgoing"].append(seg)
        junctions.setdefault(seg["to_node"], {"incoming": [], "outgoing": []})["incoming"].append(seg)

    for node, data in junctions.items():
        if node in ("Inlet", "Outlet", "Spill"):
            continue
        if not data["incoming"]:
            continue
        main = data["incoming"][0]
        for seg in data["outgoing"]:
            seg["junction_delta"] = angle_diff(main["end_angle"], seg["start_angle"])

    return segments


def boundary_label(seg_name, end_name):
    return f'{"Inlet" if end_name == "start" else "Outlet"}-{seg_name}'


def default_boundary_type(seg, end_name):
    if end_name == "start":
        return canonical_bc_type(seg.get("upstream_bc_type")) or "Normal Depth"
    return canonical_bc_type(seg.get("downstream_bc_type")) or "Normal Depth"


def default_boundary_value(seg, end_name):
    if end_name == "start":
        return seg.get("upstream_bc_value")
    return seg.get("downstream_bc_value")


def external_boundary_specs(segments, summary_rows):
    specs = []
    for seg in segments:
        if seg["from_node"].lower() == "inlet":
            specs.append({
                "label": boundary_label(seg["name"], "start"),
                "segment": seg["name"],
                "end": "start",
                "bc_type": default_boundary_type(seg, "start"),
                "bc_value": default_boundary_value(seg, "start"),
                "q_expr": a1(summary_rows[seg["name"]], SUM_QIN),
                "remarks": "Master inlet boundary" if seg.get("upstream_bc_type") else "Default inlet boundary from missing BC",
            })
        if seg["to_node"].lower() == "outlet":
            specs.append({
                "label": boundary_label(seg["name"], "end"),
                "segment": seg["name"],
                "end": "end",
                "bc_type": default_boundary_type(seg, "end"),
                "bc_value": default_boundary_value(seg, "end"),
                "q_expr": a1(summary_rows[seg["name"]], SUM_QOUT),
                "remarks": "Master outlet boundary" if seg.get("downstream_bc_type") else "Default outlet boundary from missing BC",
            })
    return specs


def spill_mapping_expr(segments_by_name, segment_table_rows, donor_names, target_name, target_idx):
    if target_idx < 2:
        return "0"
    seq = target_idx - 2
    terms = []
    for donor_name in donor_names:
        donor = segments_by_name[donor_name]
        donor_rows = []
        donor_df = donor["df"].reset_index(drop=True)
        for donor_idx, rec in donor_df.iterrows():
            left_match = yes_no(rec.get("SpillLeftOn", False)) and str(rec.get("SpillLeftTo", "")).strip().lower() == target_name.lower()
            right_match = yes_no(rec.get("SpillRightOn", False)) and str(rec.get("SpillRightTo", "")).strip().lower() == target_name.lower()
            if left_match or right_match:
                donor_rows.append(donor_idx)
        if seq < len(donor_rows):
            donor_row = segment_table_rows[donor_name]["data_start"] + donor_rows[seq]
            terms.append(f"AK{donor_row}")
    return "0" if not terms else f"({' + '.join(terms)})"


def trap_area(width_ref, side_ref, depth_expr):
    return f"(({width_ref})+({side_ref})*({depth_expr}))*({depth_expr})"


def trap_perimeter(width_ref, side_ref, depth_expr):
    return f"({width_ref})+2*({depth_expr})*SQRT(1+({side_ref})^2)"


def trap_radius(width_ref, side_ref, depth_expr):
    area = trap_area(width_ref, side_ref, depth_expr)
    perimeter = trap_perimeter(width_ref, side_ref, depth_expr)
    return f"({area})/MAX({perimeter},1E-6)"


def trap_top_width(width_ref, side_ref, depth_expr):
    return f"({width_ref})+2*({side_ref})*({depth_expr})"


def velocity_expr(q_expr, area_expr):
    return f"ABS({q_expr})/MAX({area_expr},1E-6)"


def velocity_head_expr(v_expr):
    return f"(({v_expr})^2)/(2*{G_REF})"


def friction_slope_expr(q_expr, area_expr, radius_expr):
    return (
        f"((ABS({q_expr})*{N_REF})/"
        f"(MAX({area_expr},1E-6)*MAX(({radius_expr})^(2/3),1E-6)))^2"
    )


def froude_expr(q_expr, area_expr, top_width_expr):
    velocity = velocity_expr(q_expr, area_expr)
    return f"ABS({velocity})/SQRT({G_REF}*MAX(({area_expr})/MAX({top_width_expr},1E-6),1E-6))"


def specific_force_expr(width_ref, side_ref, depth_expr, q_expr, area_expr):
    return (
        f"(({width_ref})*({depth_expr})^2)/2"
        f"+({side_ref})*({depth_expr})^3/3"
        f"+(ABS({q_expr})^2)/({G_REF}*MAX({area_expr},1E-6))"
    )


def approx_critical_depth_expr(q_expr, width_ref):
    return f"MAX({MIN_DEPTH_REF},POWER((ABS({q_expr})^2)/({G_REF}*MAX({width_ref},0.1)^2),1/3))"


def branch_loss_expr(delta_deg, branch_q_expr, main_q_expr, vh_up_expr, vh_dn_expr):
    if delta_deg <= 0.0:
        return "0"
    sin_sq = math.sin(math.radians(delta_deg / 2.0)) ** 2
    return (
        f"({KJ_REF}*{sin_sq:.8f}"
        f"*(ABS({branch_q_expr})/MAX(ABS({main_q_expr}),1E-6))^2"
        f"*MAX({vh_up_expr},{vh_dn_expr}))"
    )


def summary_section_terms(summary_row, at_start, stage_expr, q_expr):
    width_ref = a1(summary_row, SUM_B0 if at_start else SUM_B1)
    side_ref = a1(summary_row, SUM_Z0 if at_start else SUM_Z1)
    bed_ref = a1(summary_row, SUM_BED0 if at_start else SUM_BED1)
    depth = f"MAX({MIN_DEPTH_REF},({stage_expr})-({bed_ref}))"
    area = trap_area(width_ref, side_ref, depth)
    radius = trap_radius(width_ref, side_ref, depth)
    velocity = velocity_expr(q_expr, area)
    vh = velocity_head_expr(velocity)
    sf = friction_slope_expr(q_expr, area, radius)
    force = specific_force_expr(width_ref, side_ref, depth, q_expr, area)
    return {
        "width": width_ref,
        "side": side_ref,
        "bed": bed_ref,
        "depth": depth,
        "area": area,
        "radius": radius,
        "velocity": velocity,
        "vh": vh,
        "sf": sf,
        "force": force,
    }


def summary_segment_terms(summary_row, q_expr, stage_up_expr, stage_dn_expr, junction_delta_deg=0.0, branch_q_expr="0", main_q_expr="1"):
    up = summary_section_terms(summary_row, True, stage_up_expr, q_expr)
    dn = summary_section_terms(summary_row, False, stage_dn_expr, q_expr)
    length_ref = a1(summary_row, SUM_LEN)
    a0_ref = a1(summary_row, SUM_ANG0)
    a1_ref = a1(summary_row, SUM_ANG1)
    hf = f"({length_ref})*(({up['sf']})+({dn['sf']}))/2"
    hve = (
        f"IF(({dn['velocity']})>({up['velocity']}),"
        f"{KC_REF}*(({dn['vh']})-({up['vh']})),"
        f"{KE_REF}*(({up['vh']})-({dn['vh']})))"
    )
    hbend = f"{KB_REF}*(ABS(({a1_ref})-({a0_ref}))/90)*MAX({up['vh']},{dn['vh']})"
    hj = branch_loss_expr(junction_delta_deg, branch_q_expr, main_q_expr, up["vh"], dn["vh"])
    hl = f"({hf})+({hve})+({hbend})+({hj})"
    return {"up": up, "dn": dn, "hf": hf, "hve": hve, "hbend": hbend, "hj": hj, "hl": hl}


def boundary_stage_formula(summary_row, end_label, type_ref, value_ref, q_ref):
    bed_ref = a1(summary_row, SUM_BED0 if end_label == "start" else SUM_BED1)
    width_ref = a1(summary_row, SUM_B0 if end_label == "start" else SUM_B1)
    z0_ref = a1(summary_row, SUM_BED0)
    z1_ref = a1(summary_row, SUM_BED1)
    length_ref = a1(summary_row, SUM_LEN)
    norm_depth = (
        f"MAX({MIN_DEPTH_REF},POWER("
        f"(ABS({q_ref})*{N_REF})/"
        f"(MAX({width_ref},0.1)*SQRT(MAX(ABS(({z1_ref}-{z0_ref})/MAX({length_ref},1E-6)),{DEFAULT_SLOPE_REF}))),"
        f"3/5))"
    )
    crit_depth = f"MAX({MIN_DEPTH_REF},POWER((ABS({q_ref})^2)/({G_REF}*MAX({width_ref},0.1)^2),1/3))"
    return (
        f'=IF(OR({type_ref}="Known WSE",{type_ref}="Known WS"),{value_ref},'
        f'IF({type_ref}="Known Depth",({bed_ref})+({value_ref}),'
        f'IF({type_ref}="Normal Depth",({bed_ref})+({norm_depth}),'
        f'IF({type_ref}="Critical Depth",({bed_ref})+({crit_depth}),'
        f'IF(OR({type_ref}="None",{type_ref}="Spill End",{type_ref}="Spill Zero"),({bed_ref})+{MIN_DEPTH_REF},'
        f'({bed_ref})+({norm_depth}))))))'
    )


def build_sheet():
    segments = load_network()
    segments_by_name = {seg["name"]: seg for seg in segments}
    segment_order = [seg["name"] for seg in segments]
    summary_rows = {name: SUMMARY_START_ROW + idx for idx, name in enumerate(segment_order)}
    boundary_specs = external_boundary_specs(segments, summary_rows)
    boundary_rows = {spec["label"]: 5 + idx for idx, spec in enumerate(boundary_specs)}

    segment_table_rows = {}
    current_row = TABLE_START_ROW
    for seg in segments:
        data_start = current_row + 3
        segment_table_rows[seg["name"]] = {
            "title": current_row,
            "header": current_row + 1,
            "units": current_row + 2,
            "data_start": data_start,
            "data_end": data_start + seg["npts"] - 1,
        }
        current_row = data_start + seg["npts"] + 2

    total_rows = current_row + 5
    total_cols = 45
    grid = [["" for _ in range(total_cols)] for _ in range(total_rows)]

    set_cell(grid, 1, 1, "1D Steady Open Channel Network Solver")
    set_cell(grid, 3, 1, "Imported bed elevations, bank slopes, junction internal BC formulas, and spill routing to drain.")

    set_cell(grid, 4, 2, "Parameter")
    set_cell(grid, 4, 3, "Unit")
    set_cell(grid, 4, 4, "Value")
    inputs = [
        ("Total inflow Q", "m3/s", 51),
        ("Manning n", "-", 0.015),
        ("Default bank slope", "H:V", 0.0),
        ("Expansion coeff Ke", "-", 0.30),
        ("Contraction coeff Kc", "-", 0.10),
        ("Bend coeff Kb", "-", 0.15),
        ("Junction coeff base", "-", 0.75),
        ("Spill coeff Cw", "-", 1.70),
        ("Stage relaxation", "-", 0.35),
        ("Split relaxation", "-", 0.08),
        ("Momentum weight", "-", 0.05),
        ("Minimum depth", "m", 0.05),
        ("Boundary slope", "-", 0.001),
        ("Profile relaxation", "-", 0.15),
        ("Profile tolerance", "m", 0.001),
        ("Reset (0: Hold, 1: Eval)", "-", 1),
        ("J1 initial WSE", "m", 634.30),
        ("J2 initial WSE", "m", 631.90),
        ("alpha J1 initial", "-", 0.65),
        ("alpha J2 initial", "-", 0.65),
    ]
    for row_idx, (label, unit, value) in enumerate(inputs, start=INPUT_START_ROW):
        set_cell(grid, row_idx, 2, label)
        set_cell(grid, row_idx, 3, unit)
        set_cell(grid, row_idx, 4, value)

    set_cell(grid, 4, 8, "Fundamental Inputs")
    set_cell(grid, 5, 7, "Acceleration g")
    set_cell(grid, 5, 8, "m/s2")
    set_cell(grid, 5, 9, 9.81)
    set_cell(grid, 6, 7, "Density rho")
    set_cell(grid, 6, 8, "kg/m3")
    set_cell(grid, 6, 9, 998.2)
    set_cell(grid, 7, 7, "Allowed BC types")
    set_cell(grid, 7, 8, "-")
    set_cell(grid, 7, 9, "Known WSE / Known Depth / Normal Depth / Critical Depth / None / Spill End / Spill Zero")
    set_cell(grid, 11, 7, "Final alpha J1")
    set_cell(grid, 11, 8, "-")
    set_cell(grid, 11, 9, f"=Y{SOLVER_START_ROW + SOLVER_STEPS - 1}")
    set_cell(grid, 12, 7, "Final alpha J2")
    set_cell(grid, 12, 8, "-")
    set_cell(grid, 12, 9, f"=Z{SOLVER_START_ROW + SOLVER_STEPS - 1}")
    set_cell(grid, 13, 7, "Final J1 WSE")
    set_cell(grid, 13, 8, "m")
    set_cell(grid, 13, 9, f"=AA{SOLVER_START_ROW + SOLVER_STEPS - 1}")
    set_cell(grid, 14, 7, "Final J2 WSE")
    set_cell(grid, 14, 8, "m")
    set_cell(grid, 14, 9, f"=AB{SOLVER_START_ROW + SOLVER_STEPS - 1}")
    set_cell(grid, 15, 7, "Total spill to drain")
    set_cell(grid, 15, 8, "m3/s")
    set_cell(grid, 15, 9, f"=N{summary_rows['drain']}")
    set_cell(grid, 16, 7, "J1 residual")
    set_cell(grid, 16, 8, "m")
    set_cell(grid, 16, 9, f"=W{SOLVER_START_ROW + SOLVER_STEPS - 1}")
    set_cell(grid, 17, 7, "J2 residual")
    set_cell(grid, 17, 8, "m")
    set_cell(grid, 17, 9, f"=X{SOLVER_START_ROW + SOLVER_STEPS - 1}")

    for col_idx, title in enumerate(["Boundary", "Segment", "End", "Type", "Value", "Resolved Stage", "Remarks"], start=11):
        set_cell(grid, 4, col_idx, title)

    for spec in boundary_specs:
        row_idx = boundary_rows[spec["label"]]
        bc_value = spec["bc_value"] if spec["bc_value"] is not None else ""
        set_cell(grid, row_idx, 11, spec["label"])
        set_cell(grid, row_idx, 12, spec["segment"])
        set_cell(grid, row_idx, 13, spec["end"])
        set_cell(grid, row_idx, 14, spec["bc_type"])
        set_cell(grid, row_idx, 15, bc_value)
        set_cell(
            grid,
            row_idx,
            16,
            boundary_stage_formula(summary_rows[spec["segment"]], spec["end"], a1(row_idx, 14), a1(row_idx, 15), spec["q_expr"]),
        )
        set_cell(grid, row_idx, 17, spec["remarks"])

    set_cell(grid, 26, 1, "Junction Internal Boundary Conditions")
    set_cell(grid, 27, 1, "Common junction water surface is enforced through branch energy equality; split ratios are nudged by specific-force imbalance.")

    summary_headers = [
        "Segment", "From", "To", "Pts", "Length",
        "b_start", "b_end", "z_start", "z_end",
        "bed_start", "bed_end", "WS_start", "WS_end",
        "Q_in", "Q_spill", "Q_out", "y_avg", "Sf_avg",
        "hf", "h_junc", "h_total", "Profile", "Start ang", "End ang",
    ]
    for col_idx, title in enumerate(summary_headers, start=1):
        set_cell(grid, SUMMARY_HEADER_ROW, col_idx, title)

    for seg in segments:
        row = summary_rows[seg["name"]]
        set_cell(grid, row, SUM_SEG, seg["name"])
        set_cell(grid, row, SUM_FROM, seg["from_node"])
        set_cell(grid, row, SUM_TO, seg["to_node"])
        set_cell(grid, row, SUM_PTS, seg["npts"])
        set_cell(grid, row, SUM_LEN, round(seg["length"], 3))
        set_cell(grid, row, SUM_B0, round(seg["start_width"], 3))
        set_cell(grid, row, SUM_B1, round(seg["end_width"], 3))
        set_cell(grid, row, SUM_Z0, round(seg["start_side"], 3))
        set_cell(grid, row, SUM_Z1, round(seg["end_side"], 3))
        set_cell(grid, row, SUM_BED0, round(seg["start_bed"], 4))
        set_cell(grid, row, SUM_BED1, round(seg["end_bed"], 4))
        set_cell(grid, row, SUM_MODE, "Subcritical")
        set_cell(grid, row, SUM_ANG0, round(seg["start_angle"], 3))
        set_cell(grid, row, SUM_ANG1, round(seg["end_angle"], 3))

    usproject_row = summary_rows["UsProject"]
    waterway_row = summary_rows["waterway"]
    usexit_row = summary_rows["UsExit"]
    forebay_row = summary_rows["forebay"]
    side_row = summary_rows["sidechannel"]
    drain_row = summary_rows["drain"]
    usexit_type_ref = a1(boundary_rows[boundary_label("UsExit", "end")], 14)
    forebay_type_ref = a1(boundary_rows[boundary_label("forebay", "end")], 14)
    side_type_ref = a1(boundary_rows[boundary_label("sidechannel", "end")], 14)

    qin_formulas = {
        "UsProject": f"={TOTAL_Q_REF}",
        "waterway": f'=IF({usexit_type_ref}="None",{TOTAL_Q_REF},{TOTAL_Q_REF}*{FINAL_ALPHA1_REF})',
        "UsExit": f'=IF({usexit_type_ref}="None",0,{TOTAL_Q_REF}-N{waterway_row})',
        "forebay": (
            f'=IF(AND({forebay_type_ref}="None",{side_type_ref}="None"),0,'
            f'IF({side_type_ref}="None",N{waterway_row},'
            f'IF({forebay_type_ref}="None",0,N{waterway_row}*{FINAL_ALPHA2_REF})))'
        ),
        "sidechannel": (
            f'=IF(AND({forebay_type_ref}="None",{side_type_ref}="None"),0,'
            f'IF({side_type_ref}="None",0,'
            f'IF({forebay_type_ref}="None",N{waterway_row},N{waterway_row}-N{forebay_row})))'
        ),
        "drain": f"=O{forebay_row}+O{side_row}",
    }
    for seg_name, formula in qin_formulas.items():
        set_cell(grid, summary_rows[seg_name], SUM_QIN, formula)

    solver_headers = [
        "Iter", "alpha1", "alpha2", "J1_ws", "J2_ws",
        "Q_UsProject", "Q_waterway", "Q_UsExit", "Q_forebay", "Q_sidechannel",
        "WS_inlet", "WS_UsExit", "WS_forebay", "WS_sidechannel",
        "J1 from UsProject", "J1 from waterway", "J1 from UsExit",
        "J2 from waterway", "J2 from forebay", "J2 from sidechannel",
        "Mom J1", "Mom J2", "Err J1", "Err J2",
        "alpha1 next", "alpha2 next", "J1 next", "J2 next",
    ]
    for col_idx, title in enumerate(solver_headers, start=1):
        set_cell(grid, SOLVER_HEADER_ROW, col_idx, title)

    inlet_stage_ref = a1(boundary_rows[boundary_label("UsProject", "start")], 16)
    usexit_stage_ref = a1(boundary_rows[boundary_label("UsExit", "end")], 16)
    forebay_stage_ref = a1(boundary_rows[boundary_label("forebay", "end")], 16)
    side_stage_ref = a1(boundary_rows[boundary_label("sidechannel", "end")], 16)
    drain_start_stage_ref = a1(boundary_rows[boundary_label("drain", "start")], 16)
    drain_end_stage_ref = a1(boundary_rows[boundary_label("drain", "end")], 16)

    for idx in range(SOLVER_STEPS):
        row = SOLVER_START_ROW + idx
        prev_row = row - 1
        set_cell(grid, row, 1, idx)
        if row == SOLVER_START_ROW:
            set_cell(grid, row, 2, f"={INIT_A1_REF}")
            set_cell(grid, row, 3, f"={INIT_A2_REF}")
            set_cell(grid, row, 4, f"={INIT_J1_REF}")
            set_cell(grid, row, 5, f"={INIT_J2_REF}")
        else:
            set_cell(grid, row, 2, f"=Y{prev_row}")
            set_cell(grid, row, 3, f"=Z{prev_row}")
            set_cell(grid, row, 4, f"=AA{prev_row}")
            set_cell(grid, row, 5, f"=AB{prev_row}")

        set_cell(grid, row, 6, f"={TOTAL_Q_REF}")
        set_cell(grid, row, 7, f'=IF({usexit_type_ref}="None",F{row},MAX(0,F{row}*B{row}))')
        set_cell(grid, row, 8, f'=IF({usexit_type_ref}="None",0,MAX(0,F{row}-G{row}))')
        set_cell(
            grid,
            row,
            9,
            f'=IF(AND({forebay_type_ref}="None",{side_type_ref}="None"),0,IF({side_type_ref}="None",G{row},IF({forebay_type_ref}="None",0,MAX(0,G{row}*C{row}))))',
        )
        set_cell(
            grid,
            row,
            10,
            f'=IF(AND({forebay_type_ref}="None",{side_type_ref}="None"),0,IF({side_type_ref}="None",0,IF({forebay_type_ref}="None",G{row},MAX(0,G{row}-I{row}))))',
        )
        set_cell(grid, row, 11, f"={inlet_stage_ref}")
        set_cell(grid, row, 12, f"={usexit_stage_ref}")
        set_cell(grid, row, 13, f"={forebay_stage_ref}")
        set_cell(grid, row, 14, f"={side_stage_ref}")

        up_terms = summary_segment_terms(usproject_row, f"F{row}", f"K{row}", f"D{row}")
        ww_terms = summary_segment_terms(
            waterway_row, f"G{row}", f"D{row}", f"E{row}",
            segments_by_name["waterway"]["junction_delta"], f"H{row}", f"F{row}"
        )
        ue_terms = summary_segment_terms(
            usexit_row, f"H{row}", f"D{row}", f"L{row}",
            segments_by_name["UsExit"]["junction_delta"], f"G{row}", f"F{row}"
        )
        fb_terms = summary_segment_terms(
            forebay_row, f"I{row}", f"E{row}", f"M{row}",
            segments_by_name["forebay"]["junction_delta"], f"J{row}", f"G{row}"
        )
        sc_terms = summary_segment_terms(
            side_row, f"J{row}", f"E{row}", f"N{row}",
            segments_by_name["sidechannel"]["junction_delta"], f"I{row}", f"G{row}"
        )

        set_cell(grid, row, 15, f"=K{row}+{up_terms['up']['vh']}-{up_terms['hl']}-{up_terms['dn']['vh']}")
        set_cell(grid, row, 16, f"=E{row}+{ww_terms['dn']['vh']}+{ww_terms['hl']}-{ww_terms['up']['vh']}")
        set_cell(grid, row, 17, f"=L{row}+{ue_terms['dn']['vh']}+{ue_terms['hl']}-{ue_terms['up']['vh']}")
        set_cell(grid, row, 18, f"=D{row}+{ww_terms['up']['vh']}-{ww_terms['hl']}-{ww_terms['dn']['vh']}")
        set_cell(grid, row, 19, f"=M{row}+{fb_terms['dn']['vh']}+{fb_terms['hl']}-{fb_terms['up']['vh']}")
        set_cell(grid, row, 20, f"=N{row}+{sc_terms['dn']['vh']}+{sc_terms['hl']}-{sc_terms['up']['vh']}")

        cos_ww = math.cos(math.radians(segments_by_name["waterway"]["junction_delta"]))
        cos_ue = math.cos(math.radians(segments_by_name["UsExit"]["junction_delta"]))
        cos_fb = math.cos(math.radians(segments_by_name["forebay"]["junction_delta"]))
        cos_sc = math.cos(math.radians(segments_by_name["sidechannel"]["junction_delta"]))
        set_cell(
            grid,
            row,
            21,
            f"=((({ww_terms['up']['force']})*{cos_ww:.8f})-(({ue_terms['up']['force']})*{cos_ue:.8f}))"
            f"/MAX({up_terms['dn']['force']},1E-6)",
        )
        set_cell(
            grid,
            row,
            22,
            f"=((({fb_terms['up']['force']})*{cos_fb:.8f})-(({sc_terms['up']['force']})*{cos_sc:.8f}))"
            f"/MAX({ww_terms['dn']['force']},1E-6)",
        )
        set_cell(
            grid,
            row,
            23,
            f'=IF({usexit_type_ref}="None",MAX(ABS(O{row}-D{row}),ABS(P{row}-D{row})),MAX(ABS(O{row}-D{row}),ABS(P{row}-D{row}),ABS(Q{row}-D{row})))',
        )
        set_cell(
            grid,
            row,
            24,
            f'=IF(AND({forebay_type_ref}="None",{side_type_ref}="None"),ABS(R{row}-E{row}),IF({side_type_ref}="None",MAX(ABS(R{row}-E{row}),ABS(S{row}-E{row})),IF({forebay_type_ref}="None",MAX(ABS(R{row}-E{row}),ABS(T{row}-E{row})),MAX(ABS(R{row}-E{row}),ABS(S{row}-E{row}),ABS(T{row}-E{row})))))',
        )
        set_cell(
            grid,
            row,
            25,
            f'=IF({usexit_type_ref}="None",1,MIN(0.98,MAX(0.02,B{row}+{SPLIT_RELAX_REF}*(((Q{row}-P{row})/MAX(ABS(D{row}),1))+({MOMENTUM_WT_REF}*U{row}))))))',
        )
        set_cell(
            grid,
            row,
            26,
            f'=IF(AND({forebay_type_ref}="None",{side_type_ref}="None"),C{row},IF({side_type_ref}="None",1,IF({forebay_type_ref}="None",0,MIN(0.98,MAX(0.02,C{row}+{SPLIT_RELAX_REF}*(((T{row}-S{row})/MAX(ABS(E{row}),1))+({MOMENTUM_WT_REF}*V{row}))))))))',
        )
        set_cell(grid, row, 27, f'=D{row}+{STAGE_RELAX_REF}*(IF({usexit_type_ref}="None",AVERAGE(O{row}:P{row}),AVERAGE(O{row}:Q{row}))-D{row})')
        set_cell(grid, row, 28, f'=E{row}+{STAGE_RELAX_REF}*(IF(AND({forebay_type_ref}="None",{side_type_ref}="None"),R{row},IF({side_type_ref}="None",AVERAGE(R{row}:S{row}),IF({forebay_type_ref}="None",AVERAGE(R{row},T{row}),AVERAGE(R{row}:T{row}))))-E{row})')

    stage_inputs = {
        "UsProject": {"start": inlet_stage_ref, "end": FINAL_J1_REF},
        "waterway": {"start": FINAL_J1_REF, "end": FINAL_J2_REF},
        "UsExit": {"start": FINAL_J1_REF, "end": usexit_stage_ref},
        "forebay": {"start": FINAL_J2_REF, "end": forebay_stage_ref},
        "sidechannel": {"start": FINAL_J2_REF, "end": side_stage_ref},
        "drain": {"start": drain_start_stage_ref, "end": drain_end_stage_ref},
    }

    table_headers = [
        "SN", "Dist", "Easting", "Northing", "Bed Z", "Width", "Side z", "Type", "Def Ang", "dx",
        "Q_used", "y_fwd", "A_fwd", "R_fwd", "v_fwd", "WS_fwd", "EG_fwd", "Sf_fwd", "Err_fwd", "Done_fwd",
        "y_bwd", "A_bwd", "R_bwd", "v_bwd", "WS_bwd", "EG_bwd", "Sf_bwd", "Err_bwd", "Done_bwd",
        "y_final", "WS_final", "EG_final", "Crest_L", "Crest_R", "Spill_L", "Spill_R", "Spill_Q", "Cum_spill",
        "Fr_final", "Regime", "Sf_final",
    ]
    table_units = [
        "-", "m", "m", "m", "m", "m", "-", "-", "deg", "m",
        "m3/s", "m", "m2", "m", "m/s", "m", "m", "-", "m", "-",
        "m", "m2", "m", "m/s", "m", "m", "-", "m", "-",
        "m", "m", "m", "m", "m", "m3/s", "m3/s", "m3/s", "m3/s", "-", "-", "-",
    ]

    for seg in segments:
        rows = segment_table_rows[seg["name"]]
        summary_row = summary_rows[seg["name"]]
        data_start = rows["data_start"]
        data_end = rows["data_end"]
        start_stage_expr = stage_inputs[seg["name"]]["start"]
        end_stage_expr = stage_inputs[seg["name"]]["end"]
        q_ref = a1(summary_row, SUM_QIN)
        mode_ref = a1(summary_row, SUM_MODE)
        end_bc_type = default_boundary_type(seg, "end")
        start_bc_type = default_boundary_type(seg, "start")
        is_spill_end_bc = end_bc_type == "Spill End"
        is_closed_bc = end_bc_type == "None" and seg["to_node"].lower() == "outlet"
        is_spill_zero_bc = start_bc_type == "Spill Zero" and seg["from_node"].lower() == "inlet"

        set_cell(grid, rows["title"], 1, f"Segment: {seg['name']}")
        set_cell(grid, rows["title"], 5, f"=E{summary_row}")
        set_cell(grid, rows["title"], 6, "m length")
        set_cell(grid, rows["title"], 8, f"={q_ref}")
        set_cell(grid, rows["title"], 9, "m3/s")
        set_cell(grid, rows["title"], 11, f"={mode_ref}")
        set_cell(grid, rows["title"], 12, "profile")
        for col_idx, title in enumerate(table_headers, start=1):
            set_cell(grid, rows["header"], col_idx, title)
        for col_idx, unit in enumerate(table_units, start=1):
            set_cell(grid, rows["units"], col_idx, unit)

        df = seg["df"]
        for idx, (_, rec) in enumerate(df.iterrows()):
            row = data_start + idx
            prev_row = row - 1
            next_row = row + 1
            is_first = row == data_start
            is_last = row == data_end

            set_cell(grid, row, TABLE_SN, int(rec["SN"]))
            set_cell(grid, row, TABLE_DIST, round(float(rec["Chainage"]), 3))
            set_cell(grid, row, TABLE_E, round(float(rec["Easting"]), 3))
            set_cell(grid, row, TABLE_N, round(float(rec["Northing"]), 3))
            set_cell(grid, row, TABLE_BED, round(float(rec["BedFilled"]), 4))
            set_cell(grid, row, TABLE_WIDTH, round(float(rec["WidthFilled"]), 3))
            set_cell(grid, row, TABLE_SIDE, round(float(rec["SideFilled"]), 3))
            set_cell(grid, row, TABLE_TYPE, str(rec.get("Type", "")))
            set_cell(grid, row, TABLE_DEF, round(float(rec["Deflection"]), 3))
            set_cell(grid, row, TABLE_DX, 0 if is_first else f"=B{row}-B{prev_row}")
            if is_spill_zero_bc and seg["name"] == "drain":
                if idx < 2:
                    set_cell(grid, row, TABLE_Q, 0)
                else:
                    mapped_expr = spill_mapping_expr(segments_by_name, segment_table_rows, [name for name in segments_by_name if name != "drain"], "drain", idx)
                    set_cell(grid, row, TABLE_Q, f"=K{prev_row}+{mapped_expr}")
            elif is_first:
                set_cell(grid, row, TABLE_Q, f"={q_ref}")
            else:
                set_cell(grid, row, TABLE_Q, f"=MAX(0,K{prev_row}-AK{prev_row})")

            area_fwd = trap_area(a1(row, TABLE_WIDTH), a1(row, TABLE_SIDE), a1(row, TABLE_YF))
            radius_fwd = trap_radius(a1(row, TABLE_WIDTH), a1(row, TABLE_SIDE), a1(row, TABLE_YF))
            top_fwd = trap_top_width(a1(row, TABLE_WIDTH), a1(row, TABLE_SIDE), a1(row, TABLE_YF))
            vel_fwd = velocity_expr(a1(row, TABLE_Q), area_fwd)
            fr_fwd = froude_expr(a1(row, TABLE_Q), area_fwd, top_fwd)
            ws_fwd = f"=E{row}+L{row}"
            eg_fwd = f"=P{row}+(({vel_fwd})^2)/(2*{G_REF})"
            sf_fwd = f"={friction_slope_expr(a1(row, TABLE_Q), area_fwd, radius_fwd)}"
            if is_first:
                set_cell(grid, row, TABLE_YF, f"=MAX({MIN_DEPTH_REF},({start_stage_expr})-E{row})")
                set_cell(grid, row, TABLE_ERRF, 0)
                set_cell(grid, row, TABLE_DONEF, 1)
            else:
                set_cell(
                    grid,
                    row,
                    TABLE_YF,
                    f"=IF({RESET_REF}=0,L{prev_row},"
                    f"IF(T{prev_row}=0,L{prev_row},"
                    f"IF(L{row}=0,L{prev_row},"
                    f"IF(ABS(S{row})>{PROFILE_TOL_REF},"
                    f"MAX({MIN_DEPTH_REF},L{row}+((S{row})*{PROFILE_RELAX_REF}*IF(({fr_fwd})<1,1,-1))),"
                    f"L{row}))))",
                )
                local_fwd = (
                    f"IF(O{prev_row}>O{row},{KE_REF}*((O{prev_row}^2-O{row}^2)/(2*{G_REF})),"
                    f"{KC_REF}*((O{row}^2-O{prev_row}^2)/(2*{G_REF})))"
                    f"+({KB_REF}*(ABS(I{row})/90)*(MAX(O{prev_row}^2,O{row}^2)/(2*{G_REF})))"
                )
                set_cell(
                    grid,
                    row,
                    TABLE_ERRF,
                    f"=Q{prev_row}-(((R{prev_row}+R{row})/2)*J{row})-({local_fwd})-Q{row}",
                )
                set_cell(grid, row, TABLE_DONEF, f'=IF(ABS(S{row})<{PROFILE_TOL_REF},1,0)')
            set_cell(grid, row, TABLE_AF, f"={area_fwd}")
            set_cell(grid, row, TABLE_RF, f"={radius_fwd}")
            set_cell(grid, row, TABLE_VF, f"={vel_fwd}")
            set_cell(grid, row, TABLE_WSF, ws_fwd)
            set_cell(grid, row, TABLE_EGF, eg_fwd)
            set_cell(grid, row, TABLE_SFF, sf_fwd)

            area_bwd = trap_area(a1(row, TABLE_WIDTH), a1(row, TABLE_SIDE), a1(row, TABLE_YB))
            radius_bwd = trap_radius(a1(row, TABLE_WIDTH), a1(row, TABLE_SIDE), a1(row, TABLE_YB))
            top_bwd = trap_top_width(a1(row, TABLE_WIDTH), a1(row, TABLE_SIDE), a1(row, TABLE_YB))
            vel_bwd = velocity_expr(a1(row, TABLE_Q), area_bwd)
            fr_bwd = froude_expr(a1(row, TABLE_Q), area_bwd, top_bwd)
            ws_bwd = f"=E{row}+U{row}"
            eg_bwd = f"=Y{row}+(({vel_bwd})^2)/(2*{G_REF})"
            sf_bwd = f"={friction_slope_expr(a1(row, TABLE_Q), area_bwd, radius_bwd)}"
            if is_last:
                set_cell(grid, row, TABLE_YB, f"=MAX({MIN_DEPTH_REF},({end_stage_expr})-E{row})")
                set_cell(grid, row, TABLE_ERRB, 0)
                set_cell(grid, row, TABLE_DONEB, 1)
            else:
                set_cell(
                    grid,
                    row,
                    TABLE_YB,
                    f"=IF({RESET_REF}=0,U{next_row},"
                    f"IF(AC{next_row}=0,U{next_row},"
                    f"IF(U{row}=0,U{next_row},"
                    f"IF(ABS(AB{row})>{PROFILE_TOL_REF},"
                    f"MAX({MIN_DEPTH_REF},U{row}+((AB{row})*{PROFILE_RELAX_REF}*IF(({fr_bwd})<1,1,-1))),"
                    f"U{row}))))",
                )
                local_bwd = (
                    f"IF(X{row}>X{next_row},{KE_REF}*((X{row}^2-X{next_row}^2)/(2*{G_REF})),"
                    f"{KC_REF}*((X{next_row}^2-X{row}^2)/(2*{G_REF})))"
                    f"+({KB_REF}*(ABS(I{next_row})/90)*(MAX(X{row}^2,X{next_row}^2)/(2*{G_REF})))"
                )
                set_cell(
                    grid,
                    row,
                    TABLE_ERRB,
                    f"=Z{next_row}+(((AA{row}+AA{next_row})/2)*J{next_row})+({local_bwd})-Z{row}",
                )
                set_cell(grid, row, TABLE_DONEB, f'=IF(ABS(AB{row})<{PROFILE_TOL_REF},1,0)')
            set_cell(grid, row, TABLE_AB, f"={area_bwd}")
            set_cell(grid, row, TABLE_RB, f"={radius_bwd}")
            set_cell(grid, row, TABLE_VB, f"={vel_bwd}")
            set_cell(grid, row, TABLE_WSB, ws_bwd)
            set_cell(grid, row, TABLE_EGB, eg_bwd)
            set_cell(grid, row, TABLE_SFB, sf_bwd)

            set_cell(grid, row, TABLE_Y, f'=IF({mode_ref}="Supercritical",L{row},U{row})')
            vel_final = velocity_expr(a1(row, TABLE_Q), trap_area(a1(row, TABLE_WIDTH), a1(row, TABLE_SIDE), a1(row, TABLE_Y)))
            top_final = trap_top_width(a1(row, TABLE_WIDTH), a1(row, TABLE_SIDE), a1(row, TABLE_Y))
            set_cell(grid, row, TABLE_WS, f"=E{row}+AD{row}")
            set_cell(grid, row, TABLE_EG, f"=AE{row}+(({vel_final})^2)/(2*{G_REF})")

            left_crest = ""
            if yes_no(rec.get("SpillLeftOn", False)) and str(rec.get("SpillLeftTo", "")).lower() == "drain" and not pd.isna(rec.get("SpillLeftCrest")):
                left_crest = round(float(rec["SpillLeftCrest"]), 3)
            right_crest = ""
            if yes_no(rec.get("SpillRightOn", False)) and str(rec.get("SpillRightTo", "")).lower() == "drain" and not pd.isna(rec.get("SpillRightCrest")):
                right_crest = round(float(rec["SpillRightCrest"]), 3)
            set_cell(grid, row, TABLE_CREST_L, left_crest)
            set_cell(grid, row, TABLE_CREST_R, right_crest)
            set_cell(
                grid,
                row,
                TABLE_SPILL_L,
                0 if left_crest == "" else f"={SPILL_COEFF_REF}*MAX(J{row},0.1)*POWER(MAX(0,AE{row}-AG{row}),1.5)",
            )
            set_cell(
                grid,
                row,
                TABLE_SPILL_R,
                0 if right_crest == "" else f"={SPILL_COEFF_REF}*MAX(J{row},0.1)*POWER(MAX(0,AE{row}-AH{row}),1.5)",
            )
            if is_spill_end_bc and is_last:
                set_cell(grid, row, TABLE_SPILL, f"=AI{row}+AJ{row}+MAX(0,K{row}-(AI{row}+AJ{row}))")
            else:
                set_cell(grid, row, TABLE_SPILL, f"=AI{row}+AJ{row}")
            set_cell(grid, row, TABLE_CUM_SPILL, f"=AK{row}" if is_first else f"=AL{prev_row}+AK{row}")
            set_cell(grid, row, TABLE_FR, f"=ABS({vel_final})/SQRT({G_REF}*MAX(({trap_area(a1(row, TABLE_WIDTH), a1(row, TABLE_SIDE), a1(row, TABLE_Y))})/MAX({top_final},1E-6),1E-6))")
            set_cell(grid, row, TABLE_REGIME, f'=IF(AM{row}<1,"Subcritical","Supercritical")')
            set_cell(grid, row, TABLE_SF, f'=IF({mode_ref}="Supercritical",R{row},AA{row})')

        set_cell(grid, summary_row, SUM_WS0, f"=AE{data_start}")
        set_cell(grid, summary_row, SUM_WS1, f"=AE{data_end}")
        set_cell(grid, summary_row, SUM_SPILL, "0" if seg["name"] == "drain" else f"=SUM(AK{data_start}:AK{data_end})")
        if is_closed_bc or is_spill_end_bc:
            set_cell(grid, summary_row, SUM_QOUT, 0)
        else:
            set_cell(grid, summary_row, SUM_QOUT, f"=MAX(0,K{data_end}-AK{data_end})")
        set_cell(grid, summary_row, SUM_YAVG, f"=AVERAGE(AD{data_start}:AD{data_end})")
        set_cell(grid, summary_row, SUM_SFAVG, f"=AVERAGE(AO{data_start}:AO{data_end})")
        set_cell(grid, summary_row, SUM_HF, f"=E{summary_row}*R{summary_row}")

    for seg in segments:
        row = summary_rows[seg["name"]]
        if seg["name"] == "waterway":
            terms = summary_segment_terms(row, a1(row, SUM_QIN), FINAL_J1_REF, FINAL_J2_REF, seg["junction_delta"], a1(usexit_row, SUM_QIN), TOTAL_Q_REF)
            set_cell(grid, row, SUM_HJ, f"={terms['hj']}")
        elif seg["name"] == "UsExit":
            terms = summary_segment_terms(row, a1(row, SUM_QIN), FINAL_J1_REF, usexit_stage_ref, seg["junction_delta"], a1(waterway_row, SUM_QIN), TOTAL_Q_REF)
            set_cell(grid, row, SUM_HJ, f"={terms['hj']}")
        elif seg["name"] == "forebay":
            terms = summary_segment_terms(row, a1(row, SUM_QIN), FINAL_J2_REF, forebay_stage_ref, seg["junction_delta"], a1(side_row, SUM_QIN), a1(waterway_row, SUM_QIN))
            set_cell(grid, row, SUM_HJ, f"={terms['hj']}")
        elif seg["name"] == "sidechannel":
            terms = summary_segment_terms(row, a1(row, SUM_QIN), FINAL_J2_REF, side_stage_ref, seg["junction_delta"], a1(forebay_row, SUM_QIN), a1(waterway_row, SUM_QIN))
            set_cell(grid, row, SUM_HJ, f"={terms['hj']}")
        else:
            set_cell(grid, row, SUM_HJ, 0)
        set_cell(grid, row, SUM_HTOT, f"=S{row}+T{row}")

    return grid, total_rows, total_cols


def fill_rgb(red, green, blue):
    return PatternFill(
        fill_type="solid",
        fgColor=f"{int(round(red * 255)):02X}{int(round(green * 255)):02X}{int(round(blue * 255)):02X}",
    )


def apply_fill(ws, start_row, end_row, start_col, end_col, fill, bold=False, font_size=None):
    font = Font(bold=bold, size=font_size) if bold or font_size else None
    for row in ws.iter_rows(min_row=start_row, max_row=end_row, min_col=start_col, max_col=end_col):
        for cell in row:
            cell.fill = fill
            if font is not None:
                cell.font = font


def write_grid_to_worksheet(ws, grid):
    for row_idx, row_values in enumerate(grid, start=1):
        for col_idx, value in enumerate(row_values, start=1):
            if value == "":
                continue
            ws.cell(row=row_idx, column=col_idx, value=value)


def apply_boundary_dropdowns_excel(ws):
    validation = DataValidation(
        type="list",
        formula1='"' + ",".join(BOUNDARY_OPTIONS) + '"',
        allow_blank=True,
    )
    validation.prompt = "Select a boundary condition type."
    validation.promptTitle = "Boundary Condition"
    validation.error = "Choose one of the allowed boundary-condition options."
    validation.errorTitle = "Invalid Boundary Condition"
    ws.add_data_validation(validation)
    validation.add(f"N5:N20")


def apply_excel_layout(ws, total_cols):
    ws.freeze_panes = "A30"
    ws.sheet_view.showGridLines = True
    ws.auto_filter.ref = f"A{SUMMARY_HEADER_ROW}:{col_letter(24)}{SUMMARY_START_ROW + 5}"

    for col_idx in range(1, total_cols + 1):
        letter = get_column_letter(col_idx)
        if col_idx in (1, 2, 3, 4):
            ws.column_dimensions[letter].width = 14
        elif col_idx <= 10:
            ws.column_dimensions[letter].width = 12
        elif col_idx <= 17:
            ws.column_dimensions[letter].width = 18
        else:
            ws.column_dimensions[letter].width = 13

    title_fill = fill_rgb(0.85, 0.91, 0.97)
    panel_fill = fill_rgb(0.96, 0.96, 0.96)
    boundary_fill = fill_rgb(0.95, 0.98, 0.92)
    summary_fill = fill_rgb(0.96, 0.94, 0.88)
    solver_fill = fill_rgb(0.93, 0.96, 0.99)

    apply_fill(ws, 1, 1, 1, 9, title_fill, bold=True, font_size=14)
    apply_fill(ws, 4, 24, 2, 4, panel_fill)
    apply_fill(ws, 4, 20, 11, 17, boundary_fill, bold=True)
    apply_fill(ws, SUMMARY_HEADER_ROW, SUMMARY_HEADER_ROW, 1, 24, summary_fill, bold=True)
    apply_fill(ws, SOLVER_HEADER_ROW, SOLVER_HEADER_ROW, 1, 28, solver_fill, bold=True)


def configure_excel_calculation(workbook):
    workbook.calculation = CalcProperties(
        calcMode="auto",
        fullCalcOnLoad=True,
        forceFullCalc=True,
        iterate=True,
        iterateCount=250,
        iterateDelta=0.001,
    )


def write_1d_excel(output_path=DEFAULT_OUTPUT_PATH, worksheet_name=WORKSHEET_NAME):
    grid, total_rows, total_cols = build_sheet()

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = worksheet_name

    write_grid_to_worksheet(worksheet, grid)
    apply_boundary_dropdowns_excel(worksheet)
    apply_excel_layout(worksheet, total_cols)
    configure_excel_calculation(workbook)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(output_path)
    print(f"Wrote Excel workbook to {output_path.resolve()}", flush=True)


def parse_args():
    parser = argparse.ArgumentParser(description="Build the 1D steady network workbook in Excel format.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH, help="Output Excel workbook path.")
    return parser.parse_args()


def main():
    args = parse_args()
    write_1d_excel(output_path=args.output)


if __name__ == "__main__":
    main()
