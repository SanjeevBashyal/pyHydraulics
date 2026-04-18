import math
from pathlib import Path

import pandas as pd

from sheets import GoogleSheetsClient


SHEET_ID = "1eJ7uyJdZmJus-4TmeQ1CkdMLbETq5xn_BzznjDScr64"
WORKSHEET_NAME = "1D"

TITLE_ROW = 1
BOUNDARY_TABLE_ROW = 4
SUMMARY_HEADER_ROW = 27
SUMMARY_START_ROW = 28
SOLVER_HEADER_ROW = 36
SOLVER_START_ROW = 37
SOLVER_STEPS = 60

TOTAL_Q_REF = "$D$5"
N_REF = "$D$6"
SIDE_Z_REF = "$D$7"
KE_REF = "$D$8"
KC_REF = "$D$9"
KB_REF = "$D$10"
KJ_REF = "$D$11"
STAGE_RELAX_REF = "$D$12"
SPLIT_RELAX_REF = "$D$13"
MOMENTUM_WT_REF = "$D$14"
MIN_DEPTH_REF = "$D$15"
DEFAULT_SLOPE_REF = "$D$16"
PROFILE_RELAX_REF = "$D$17"
PROFILE_TOL_REF = "$D$18"
INIT_J1_REF = "$D$19"
INIT_J2_REF = "$D$20"
INIT_A1_REF = "$D$21"
INIT_A2_REF = "$D$22"
G_REF = "$I$5"

FINAL_ALPHA1_REF = "$I$11"
FINAL_ALPHA2_REF = "$I$12"
FINAL_J1_REF = "$I$13"
FINAL_J2_REF = "$I$14"

NETWORK_DIR = Path("MTGHP-core")


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


def fill_widths(df):
    series = pd.to_numeric(df["Bed width"], errors="coerce")
    series = series.interpolate(limit_direction="both").bfill().ffill()
    return series


def cumulative_distances(df):
    distances = [0.0]
    east = df["Easting"].astype(float).tolist()
    north = df["Northing"].astype(float).tolist()
    for i in range(1, len(df)):
        dx = east[i] - east[i - 1]
        dy = north[i] - north[i - 1]
        distances.append(distances[-1] + math.hypot(dx, dy))
    return distances


def deflection_angles(df):
    east = df["Easting"].astype(float).tolist()
    north = df["Northing"].astype(float).tolist()
    angles = [0.0] * len(df)
    for i in range(1, len(df) - 1):
        v1x = east[i] - east[i - 1]
        v1y = north[i] - north[i - 1]
        v2x = east[i + 1] - east[i]
        v2y = north[i + 1] - north[i]
        a1_deg = angle_deg(v1x, v1y)
        a2_deg = angle_deg(v2x, v2y)
        angles[i] = angle_diff(a1_deg, a2_deg)
    return angles


def load_network():
    master = pd.read_excel(NETWORK_DIR / "master.xlsx")
    segments = []

    for _, item in master.iterrows():
        name = str(item["Channel"])
        df = pd.read_csv(NETWORK_DIR / f"{name}.csv")
        widths = fill_widths(df)
        chainages = cumulative_distances(df)
        def_angles = deflection_angles(df)

        df = df.copy()
        df["FilledWidth"] = widths
        df["Chainage"] = chainages
        df["Deflection"] = def_angles

        east = df["Easting"].astype(float).tolist()
        north = df["Northing"].astype(float).tolist()
        start_angle = angle_deg(east[1] - east[0], north[1] - north[0]) if len(df) > 1 else 0.0
        end_angle = angle_deg(east[-1] - east[-2], north[-1] - north[-2]) if len(df) > 1 else 0.0

        segments.append(
            {
                "name": name,
                "from_node": str(item["From"]),
                "to_node": str(item["To"]),
                "df": df,
                "npts": len(df),
                "length": float(chainages[-1]) if chainages else 0.0,
                "start_width": float(widths.iloc[0]),
                "end_width": float(widths.iloc[-1]),
                "avg_width": float(widths.mean()),
                "start_angle": start_angle,
                "end_angle": end_angle,
                "junction_delta": 0.0,
            }
        )

    junctions = {}
    for seg in segments:
        junctions.setdefault(seg["to_node"], {"incoming": [], "outgoing": []})["incoming"].append(seg)
        junctions.setdefault(seg["from_node"], {"incoming": [], "outgoing": []})["outgoing"].append(seg)

    for node, links in junctions.items():
        if node in ("Inlet", "Outlet"):
            continue
        if not links["incoming"] or not links["outgoing"]:
            continue
        main = links["incoming"][0]
        for seg in links["outgoing"]:
            seg["junction_delta"] = angle_diff(main["end_angle"], seg["start_angle"])

    return segments


def trap_area(width_ref, depth_expr):
    return f"(({width_ref})+({SIDE_Z_REF})*({depth_expr}))*({depth_expr})"


def trap_perimeter(width_ref, depth_expr):
    return f"({width_ref})+2*({depth_expr})*SQRT(1+({SIDE_Z_REF})^2)"


def trap_radius(width_ref, depth_expr):
    area = trap_area(width_ref, depth_expr)
    perimeter = trap_perimeter(width_ref, depth_expr)
    return f"({area})/({perimeter})"


def top_width(width_ref, depth_expr):
    return f"({width_ref})+2*({SIDE_Z_REF})*({depth_expr})"


def velocity_expr(q_expr, area_expr):
    return f"ABS({q_expr})/MAX({area_expr},1E-6)"


def velocity_head_expr(velocity):
    return f"(({velocity})^2)/(2*{G_REF})"


def friction_slope_expr(q_expr, area_expr, radius_expr):
    return f"((ABS({q_expr})*{N_REF})/(MAX({area_expr},1E-6)*MAX(({radius_expr})^(2/3),1E-6)))^2"


def specific_force_expr(width_ref, depth_expr, q_expr, area_expr):
    return (
        f"(({width_ref})*({depth_expr})^2)/2"
        f"+({SIDE_Z_REF})*({depth_expr})^3/3"
        f"+(ABS({q_expr})^2)/({G_REF}*MAX({area_expr},1E-6))"
    )


def branch_loss_expr(delta_deg, branch_q_expr, main_q_expr, vhu_expr, vhd_expr):
    if delta_deg <= 0.0:
        return "0"
    sin_sq = math.sin(math.radians(delta_deg / 2.0)) ** 2
    return (
        f"({KJ_REF}*{sin_sq:.8f}*(ABS({branch_q_expr})/MAX(ABS({main_q_expr}),1E-6))^2"
        f"*MAX({vhu_expr},{vhd_expr}))"
    )


def segment_loss_terms(seg_row, q_expr, stage_up_expr, stage_dn_expr, branch_q_expr="0", main_q_expr="1"):
    start_bed = a1(seg_row, 9)
    end_bed = a1(seg_row, 10)
    start_width = a1(seg_row, 6)
    end_width = a1(seg_row, 7)
    length_ref = a1(seg_row, 5)
    start_angle = a1(seg_row, 20)
    end_angle = a1(seg_row, 21)

    d_up = f"MAX({MIN_DEPTH_REF},({stage_up_expr})-({start_bed}))"
    d_dn = f"MAX({MIN_DEPTH_REF},({stage_dn_expr})-({end_bed}))"
    a_up = trap_area(start_width, d_up)
    a_dn = trap_area(end_width, d_dn)
    r_up = trap_radius(start_width, d_up)
    r_dn = trap_radius(end_width, d_dn)
    v_up = velocity_expr(q_expr, a_up)
    v_dn = velocity_expr(q_expr, a_dn)
    vh_up = velocity_head_expr(v_up)
    vh_dn = velocity_head_expr(v_dn)
    sf_up = friction_slope_expr(q_expr, a_up, r_up)
    sf_dn = friction_slope_expr(q_expr, a_dn, r_dn)
    hf = f"({length_ref})*(({sf_up})+({sf_dn}))/2"
    hve = (
        f"IF(({v_dn})>({v_up}),"
        f"{KC_REF}*(({vh_dn})-({vh_up})),"
        f"{KE_REF}*(({vh_up})-({vh_dn})))"
    )
    hbend = f"{KB_REF}*(ABS(({end_angle})-({start_angle}))/90)*MAX({vh_up},{vh_dn})"
    hj = branch_loss_expr(0.0, branch_q_expr, main_q_expr, vh_up, vh_dn)
    return {
        "depth_up": d_up,
        "depth_dn": d_dn,
        "area_up": a_up,
        "area_dn": a_dn,
        "velocity_up": v_up,
        "velocity_dn": v_dn,
        "vh_up": vh_up,
        "vh_dn": vh_dn,
        "hf": hf,
        "hve": hve,
        "hbend": hbend,
        "hj": hj,
        "hl": f"({hf})+({hve})+({hbend})+({hj})",
    }


def segment_loss_terms_with_delta(seg_row, q_expr, stage_up_expr, stage_dn_expr, delta_deg, branch_q_expr="0", main_q_expr="1"):
    terms = segment_loss_terms(seg_row, q_expr, stage_up_expr, stage_dn_expr, branch_q_expr, main_q_expr)
    start_bed = a1(seg_row, 9)
    end_bed = a1(seg_row, 10)
    start_width = a1(seg_row, 6)
    end_width = a1(seg_row, 7)
    d_up = f"MAX({MIN_DEPTH_REF},({stage_up_expr})-({start_bed}))"
    d_dn = f"MAX({MIN_DEPTH_REF},({stage_dn_expr})-({end_bed}))"
    a_up = trap_area(start_width, d_up)
    a_dn = trap_area(end_width, d_dn)
    v_up = velocity_expr(q_expr, a_up)
    v_dn = velocity_expr(q_expr, a_dn)
    vh_up = velocity_head_expr(v_up)
    vh_dn = velocity_head_expr(v_dn)
    terms["hj"] = branch_loss_expr(delta_deg, branch_q_expr, main_q_expr, vh_up, vh_dn)
    terms["hl"] = f"({terms['hf']})+({terms['hve']})+({terms['hbend']})+({terms['hj']})"
    return terms


def boundary_stage_formula(seg_row, end_label, type_ref, value_ref, q_ref):
    bed_ref = a1(seg_row, 9 if end_label == "start" else 10)
    width_ref = a1(seg_row, 6 if end_label == "start" else 7)
    z1_ref = a1(seg_row, 9)
    z2_ref = a1(seg_row, 10)
    length_ref = a1(seg_row, 5)
    norm_depth = (
        f"MAX({MIN_DEPTH_REF},POWER("
        f"(ABS({q_ref})*{N_REF})/"
        f"(MAX({width_ref},0.1)*SQRT(MAX(ABS(({z2_ref}-{z1_ref})/MAX({length_ref},1E-6)),{DEFAULT_SLOPE_REF}))),"
        f"3/5))"
    )
    crit_depth = f"MAX({MIN_DEPTH_REF},POWER((ABS({q_ref})^2)/({G_REF}*MAX({width_ref},0.1)^2),1/3))"
    return (
        f'=IF({type_ref}="Known WSE",{value_ref},'
        f'IF({type_ref}="Known Depth",({bed_ref})+({value_ref}),'
        f'IF({type_ref}="Normal Depth",({bed_ref})+({norm_depth}),'
        f'IF({type_ref}="Critical Depth",({bed_ref})+({crit_depth}),{value_ref}))))'
    )


def build_sheet():
    segments = load_network()
    segments_by_name = {seg["name"]: seg for seg in segments}
    segment_order = [seg["name"] for seg in segments]
    summary_rows = {name: SUMMARY_START_ROW + idx for idx, name in enumerate(segment_order)}

    boundary_rows = {
        "Inlet": 5,
        "UsExit": 6,
        "forebay": 7,
        "sidechannel": 8,
    }

    table_start_row = 105
    segment_table_rows = {}
    current_row = table_start_row
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
    total_cols = 48
    grid = [["" for _ in range(total_cols)] for _ in range(total_rows)]

    set_cell(grid, 1, 1, "1D Steady Open Channel Network Solver")
    set_cell(grid, 3, 1, "Network imported from MTGHP-core with junction-aware steady routing.")
    set_cell(grid, 4, 2, "Parameter")
    set_cell(grid, 4, 3, "Unit")
    set_cell(grid, 4, 4, "Value")

    inputs = [
        ("Total Inflow Q", "m3/s", 12.5),
        ("Manning n", "-", 0.015),
        ("Side slope z", "H:V", 0),
        ("Expansion coeff Ke", "-", 0.30),
        ("Contraction coeff Kc", "-", 0.10),
        ("Bend coeff Kb", "-", 0.15),
        ("Junction coeff base", "-", 0.75),
        ("Stage relaxation", "-", 0.35),
        ("Split relaxation", "-", 0.08),
        ("Momentum weight", "-", 0.05),
        ("Minimum depth", "m", 0.05),
        ("Boundary slope", "-", 0.001),
        ("Profile relaxation", "-", 0.15),
        ("Profile tolerance", "m", 0.001),
        ("J1 initial WSE", "m", "=AVERAGE($O$5:$O$8)"),
        ("J2 initial WSE", "m", "=AVERAGE($O$6:$O$8)"),
        ("alpha J1 initial", "-", 0.65),
        ("alpha J2 initial", "-", 0.65),
    ]
    for idx, (label, unit, value) in enumerate(inputs, start=5):
        set_cell(grid, idx, 2, label)
        set_cell(grid, idx, 3, unit)
        set_cell(grid, idx, 4, value)

    set_cell(grid, 4, 8, "Fundamental Inputs")
    set_cell(grid, 5, 7, "Acceleration g")
    set_cell(grid, 5, 8, "m/s2")
    set_cell(grid, 5, 9, 9.81)
    set_cell(grid, 6, 7, "Density rho")
    set_cell(grid, 6, 8, "kg/m3")
    set_cell(grid, 6, 9, 998.2)
    set_cell(grid, 7, 7, "Allowed BC types")
    set_cell(grid, 7, 8, "-")
    set_cell(grid, 7, 9, "Known WSE / Known Depth / Normal Depth / Critical Depth")

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
    set_cell(grid, 15, 7, "J1 residual")
    set_cell(grid, 15, 8, "m")
    set_cell(grid, 15, 9, f"=W{SOLVER_START_ROW + SOLVER_STEPS - 1}")
    set_cell(grid, 16, 7, "J2 residual")
    set_cell(grid, 16, 8, "m")
    set_cell(grid, 16, 9, f"=X{SOLVER_START_ROW + SOLVER_STEPS - 1}")

    boundary_header = ["Boundary", "Segment", "End", "Type", "Value", "Resolved Stage", "Remarks"]
    for c, item in enumerate(boundary_header, start=11):
        set_cell(grid, 4, c, item)

    boundary_specs = [
        ("Inlet", "UsProject", "start", "Known WSE", f"={a1(summary_rows['UsProject'], 9)}+1.50", TOTAL_Q_REF, "External inflow boundary"),
        ("Outlet-UsExit", "UsExit", "end", "Known WSE", f"={a1(summary_rows['UsExit'], 10)}+1.00", a1(summary_rows["UsExit"], 13), "Branch outlet boundary"),
        ("Outlet-forebay", "forebay", "end", "Known WSE", f"={a1(summary_rows['forebay'], 10)}+1.00", a1(summary_rows["forebay"], 13), "Branch outlet boundary"),
        ("Outlet-sidechannel", "sidechannel", "end", "Known WSE", f"={a1(summary_rows['sidechannel'], 10)}+1.00", a1(summary_rows["sidechannel"], 13), "Branch outlet boundary"),
    ]
    for idx, (label, seg_name, end_name, bc_type, bc_value, q_ref, remark) in enumerate(boundary_specs, start=5):
        set_cell(grid, idx, 11, label)
        set_cell(grid, idx, 12, seg_name)
        set_cell(grid, idx, 13, end_name)
        set_cell(grid, idx, 14, bc_type)
        set_cell(grid, idx, 15, bc_value)
        set_cell(grid, idx, 16, boundary_stage_formula(summary_rows[seg_name], end_name, a1(idx, 14), a1(idx, 15), q_ref))
        set_cell(grid, idx, 17, remark)

    set_cell(grid, 24, 1, "Bed invert endpoints are editable defaults. Longitudinal beds are linearly interpolated between each segment start/end invert.")

    summary_header = [
        "Segment",
        "From",
        "To",
        "Pts",
        "Length",
        "b_start",
        "b_end",
        "b_avg",
        "z_start",
        "z_end",
        "WS_start",
        "WS_end",
        "Q_final",
        "y_avg",
        "Sf_avg",
        "hf",
        "h_junc",
        "h_total",
        "Profile",
        "Start ang",
        "End ang",
    ]
    for c, label in enumerate(summary_header, start=1):
        set_cell(grid, SUMMARY_HEADER_ROW, c, label)

    usproject_row = summary_rows["UsProject"]
    waterway_row = summary_rows["waterway"]
    usexit_row = summary_rows["UsExit"]
    forebay_row = summary_rows["forebay"]
    side_row = summary_rows["sidechannel"]

    for seg in segments:
        row = summary_rows[seg["name"]]
        set_cell(grid, row, 1, seg["name"])
        set_cell(grid, row, 2, seg["from_node"])
        set_cell(grid, row, 3, seg["to_node"])
        set_cell(grid, row, 4, seg["npts"])
        set_cell(grid, row, 5, round(seg["length"], 3))
        set_cell(grid, row, 6, round(seg["start_width"], 3))
        set_cell(grid, row, 7, round(seg["end_width"], 3))
        set_cell(grid, row, 8, round(seg["avg_width"], 3))
        set_cell(grid, row, 19, "Subcritical")
        set_cell(grid, row, 20, round(seg["start_angle"], 3))
        set_cell(grid, row, 21, round(seg["end_angle"], 3))

    set_cell(grid, usproject_row, 9, 100.0)
    set_cell(grid, usproject_row, 10, f"=I{usproject_row}-0.005*E{usproject_row}")
    set_cell(grid, waterway_row, 9, f"=J{usproject_row}")
    set_cell(grid, waterway_row, 10, f"=I{waterway_row}-0.001*E{waterway_row}")
    set_cell(grid, usexit_row, 9, f"=J{usproject_row}")
    set_cell(grid, usexit_row, 10, f"=I{usexit_row}-0.002*E{usexit_row}")
    set_cell(grid, forebay_row, 9, f"=J{waterway_row}")
    set_cell(grid, forebay_row, 10, f"=I{forebay_row}-0.001*E{forebay_row}")
    set_cell(grid, side_row, 9, f"=J{waterway_row}")
    set_cell(grid, side_row, 10, f"=I{side_row}-0.0012*E{side_row}")

    final_q_formulas = {
        "UsProject": f"={TOTAL_Q_REF}",
        "waterway": f"={TOTAL_Q_REF}*{FINAL_ALPHA1_REF}",
        "UsExit": f"={TOTAL_Q_REF}-M{waterway_row}",
        "forebay": f"=M{waterway_row}*{FINAL_ALPHA2_REF}",
        "sidechannel": f"=M{waterway_row}-M{forebay_row}",
    }
    start_stage_formulas = {
        "UsProject": "=P5",
        "waterway": f"={FINAL_J1_REF}",
        "UsExit": f"={FINAL_J1_REF}",
        "forebay": f"={FINAL_J2_REF}",
        "sidechannel": f"={FINAL_J2_REF}",
    }
    end_stage_formulas = {
        "UsProject": f"={FINAL_J1_REF}",
        "waterway": f"={FINAL_J2_REF}",
        "UsExit": "=P6",
        "forebay": "=P7",
        "sidechannel": "=P8",
    }

    for seg in segments:
        row = summary_rows[seg["name"]]
        set_cell(grid, row, 11, start_stage_formulas[seg["name"]])
        set_cell(grid, row, 12, end_stage_formulas[seg["name"]])
        set_cell(grid, row, 13, final_q_formulas[seg["name"]])

        y_up = f"MAX({MIN_DEPTH_REF},K{row}-I{row})"
        y_dn = f"MAX({MIN_DEPTH_REF},L{row}-J{row})"
        a_up = trap_area(a1(row, 6), y_up)
        a_dn = trap_area(a1(row, 7), y_dn)
        r_up = trap_radius(a1(row, 6), y_up)
        r_dn = trap_radius(a1(row, 7), y_dn)
        sf_up = friction_slope_expr(a1(row, 13), a_up, r_up)
        sf_dn = friction_slope_expr(a1(row, 13), a_dn, r_dn)
        v_up = velocity_expr(a1(row, 13), a_up)
        v_dn = velocity_expr(a1(row, 13), a_dn)
        vh_up = velocity_head_expr(v_up)
        vh_dn = velocity_head_expr(v_dn)
        hj = "0"
        if seg["name"] == "waterway":
            hj = branch_loss_expr(seg["junction_delta"], a1(usexit_row, 13), a1(usproject_row, 13), vh_up, vh_dn)
        elif seg["name"] == "UsExit":
            hj = branch_loss_expr(seg["junction_delta"], a1(waterway_row, 13), a1(usproject_row, 13), vh_up, vh_dn)
        elif seg["name"] == "forebay":
            hj = branch_loss_expr(seg["junction_delta"], a1(side_row, 13), a1(waterway_row, 13), vh_up, vh_dn)
        elif seg["name"] == "sidechannel":
            hj = branch_loss_expr(seg["junction_delta"], a1(forebay_row, 13), a1(waterway_row, 13), vh_up, vh_dn)
        set_cell(grid, row, 14, f"=(({y_up})+({y_dn}))/2")
        set_cell(grid, row, 15, f"=(({sf_up})+({sf_dn}))/2")
        set_cell(grid, row, 16, f"=E{row}*O{row}")
        set_cell(grid, row, 17, f"={hj}")
        set_cell(grid, row, 18, f"=P{row}+Q{row}")

    solver_headers = [
        "Iter",
        "alpha1",
        "alpha2",
        "J1_ws",
        "J2_ws",
        "Q_UsProject",
        "Q_waterway",
        "Q_UsExit",
        "Q_forebay",
        "Q_sidechannel",
        "WS_inlet",
        "WS_UsExit",
        "WS_forebay",
        "WS_sidechannel",
        "J1 from up",
        "J1 from waterway",
        "J1 from UsExit",
        "J2 from waterway",
        "J2 from forebay",
        "J2 from sidechannel",
        "Mom J1",
        "Mom J2",
        "Err J1",
        "Err J2",
        "alpha1 next",
        "alpha2 next",
        "J1 next",
        "J2 next",
    ]
    for c, label in enumerate(solver_headers, start=1):
        set_cell(grid, SOLVER_HEADER_ROW, c, label)

    def boundary_stage_runtime(boundary_row, q_expr):
        seg_name = grid[boundary_row - 1][11]
        end_name = grid[boundary_row - 1][12]
        return boundary_stage_formula(summary_rows[seg_name], end_name, a1(boundary_row, 14), a1(boundary_row, 15), q_expr)

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
        set_cell(grid, row, 7, f"=MAX(0.02,F{row}*B{row})")
        set_cell(grid, row, 8, f"=MAX(0.02,F{row}-G{row})")
        set_cell(grid, row, 9, f"=MAX(0.02,G{row}*C{row})")
        set_cell(grid, row, 10, f"=MAX(0.02,G{row}-I{row})")

        set_cell(grid, row, 11, boundary_stage_runtime(boundary_rows["Inlet"], f"F{row}"))
        set_cell(grid, row, 12, boundary_stage_runtime(boundary_rows["UsExit"], f"H{row}"))
        set_cell(grid, row, 13, boundary_stage_runtime(boundary_rows["forebay"], f"I{row}"))
        set_cell(grid, row, 14, boundary_stage_runtime(boundary_rows["sidechannel"], f"J{row}"))

        up_terms = segment_loss_terms_with_delta(usproject_row, f"F{row}", f"K{row}", f"D{row}", 0.0)
        ww_terms = segment_loss_terms_with_delta(waterway_row, f"G{row}", f"D{row}", f"E{row}", segments_by_name["waterway"]["junction_delta"], f"H{row}", f"F{row}")
        ue_terms = segment_loss_terms_with_delta(usexit_row, f"H{row}", f"D{row}", f"L{row}", segments_by_name["UsExit"]["junction_delta"], f"G{row}", f"F{row}")
        fb_terms = segment_loss_terms_with_delta(forebay_row, f"I{row}", f"E{row}", f"M{row}", segments_by_name["forebay"]["junction_delta"], f"J{row}", f"G{row}")
        sc_terms = segment_loss_terms_with_delta(side_row, f"J{row}", f"E{row}", f"N{row}", segments_by_name["sidechannel"]["junction_delta"], f"I{row}", f"G{row}")

        set_cell(grid, row, 15, f"=K{row}+{up_terms['vh_up']}-{up_terms['vh_dn']}-{up_terms['hl']}")
        set_cell(grid, row, 16, f"=E{row}+{ww_terms['vh_dn']}-{ww_terms['vh_up']}+{ww_terms['hl']}")
        set_cell(grid, row, 17, f"=L{row}+{ue_terms['vh_dn']}-{ue_terms['vh_up']}+{ue_terms['hl']}")
        set_cell(grid, row, 18, f"=D{row}+{ww_terms['vh_up']}-{ww_terms['vh_dn']}-{ww_terms['hl']}")
        set_cell(grid, row, 19, f"=M{row}+{fb_terms['vh_dn']}-{fb_terms['vh_up']}+{fb_terms['hl']}")
        set_cell(grid, row, 20, f"=N{row}+{sc_terms['vh_dn']}-{sc_terms['vh_up']}+{sc_terms['hl']}")

        mw_up = specific_force_expr(a1(usproject_row, 7), up_terms["depth_dn"], f"F{row}", up_terms["area_dn"])
        mw_ww = specific_force_expr(a1(waterway_row, 6), ww_terms["depth_up"], f"G{row}", ww_terms["area_up"])
        mw_ue = specific_force_expr(a1(usexit_row, 6), ue_terms["depth_up"], f"H{row}", ue_terms["area_up"])
        cos_ww = math.cos(math.radians(segments_by_name["waterway"]["junction_delta"]))
        cos_ue = math.cos(math.radians(segments_by_name["UsExit"]["junction_delta"]))
        set_cell(
            grid,
            row,
            21,
            f"=((({mw_ww})*{cos_ww:.8f})-(({mw_ue})*{cos_ue:.8f}))/MAX(({mw_up}),1E-6)",
        )

        mw_wd = specific_force_expr(a1(waterway_row, 7), ww_terms["depth_dn"], f"G{row}", ww_terms["area_dn"])
        mw_fb = specific_force_expr(a1(forebay_row, 6), fb_terms["depth_up"], f"I{row}", fb_terms["area_up"])
        mw_sc = specific_force_expr(a1(side_row, 6), sc_terms["depth_up"], f"J{row}", sc_terms["area_up"])
        cos_fb = math.cos(math.radians(segments_by_name["forebay"]["junction_delta"]))
        cos_sc = math.cos(math.radians(segments_by_name["sidechannel"]["junction_delta"]))
        set_cell(
            grid,
            row,
            22,
            f"=((({mw_fb})*{cos_fb:.8f})-(({mw_sc})*{cos_sc:.8f}))/MAX(({mw_wd}),1E-6)",
        )

        set_cell(grid, row, 23, f"=MAX(ABS(O{row}-D{row}),ABS(P{row}-D{row}),ABS(Q{row}-D{row}))")
        set_cell(grid, row, 24, f"=MAX(ABS(R{row}-E{row}),ABS(S{row}-E{row}),ABS(T{row}-E{row}))")
        set_cell(
            grid,
            row,
            25,
            f"=MIN(0.98,MAX(0.02,B{row}+{SPLIT_RELAX_REF}*(((Q{row}-P{row})/MAX(ABS(D{row}),1))+({MOMENTUM_WT_REF}*U{row}))))",
        )
        set_cell(
            grid,
            row,
            26,
            f"=MIN(0.98,MAX(0.02,C{row}+{SPLIT_RELAX_REF}*(((T{row}-S{row})/MAX(ABS(E{row}),1))+({MOMENTUM_WT_REF}*V{row}))))",
        )
        set_cell(grid, row, 27, f"=D{row}+{STAGE_RELAX_REF}*(AVERAGE(O{row}:Q{row})-D{row})")
        set_cell(grid, row, 28, f"=E{row}+{STAGE_RELAX_REF}*(AVERAGE(R{row}:T{row})-E{row})")

    for seg in segments:
        rows = segment_table_rows[seg["name"]]
        title_row = rows["title"]
        header_row = rows["header"]
        units_row = rows["units"]
        data_start = rows["data_start"]

        summary_row = summary_rows[seg["name"]]
        start_stage_ref = f"K{summary_row}"
        end_stage_ref = f"L{summary_row}"
        q_ref = f"M{summary_row}"
        mode_ref = f"S{summary_row}"

        set_cell(grid, title_row, 1, f"Segment: {seg['name']}")
        set_cell(grid, title_row, 5, f"=E{summary_row}")
        set_cell(grid, title_row, 6, "m length")
        set_cell(grid, title_row, 8, f"={q_ref}")
        set_cell(grid, title_row, 9, "m3/s")
        set_cell(grid, title_row, 11, f"={start_stage_ref}")
        set_cell(grid, title_row, 12, "m start WS")
        set_cell(grid, title_row, 14, f"={end_stage_ref}")
        set_cell(grid, title_row, 15, "m end WS")
        set_cell(grid, title_row, 17, f"={mode_ref}")
        set_cell(grid, title_row, 18, "final profile")

        headers = [
            "Sec",
            "Dist",
            "Easting",
            "Northing",
            "Bed Z",
            "Width",
            "Side z",
            "Def Ang",
            "dx",
            "y_fwd",
            "A_fwd",
            "R_fwd",
            "v_fwd",
            "WS_fwd",
            "Sf_fwd",
            "Err_fwd",
            "Done_fwd",
            "y_bwd",
            "A_bwd",
            "R_bwd",
            "v_bwd",
            "WS_bwd",
            "Sf_bwd",
            "Err_bwd",
            "Done_bwd",
            "y_final",
            "WS_final",
            "Fr_final",
            "Regime",
        ]
        units = [
            "-",
            "m",
            "m",
            "m",
            "m",
            "m",
            "-",
            "deg",
            "m",
            "m",
            "m2",
            "m",
            "m/s",
            "m",
            "-",
            "m",
            "-",
            "m",
            "m2",
            "m",
            "m/s",
            "m",
            "-",
            "m",
            "-",
            "m",
            "m",
            "-",
            "-",
        ]
        for c, item in enumerate(headers, start=1):
            set_cell(grid, header_row, c, item)
        for c, item in enumerate(units, start=1):
            set_cell(grid, units_row, c, item)

        df = seg["df"]
        for idx, (_, rec) in enumerate(df.iterrows()):
            row = data_start + idx
            is_first = idx == 0
            is_last = idx == len(df) - 1
            prev_row = row - 1
            next_row = row + 1

            set_cell(grid, row, 1, int(rec["SN"]))
            set_cell(grid, row, 2, round(float(rec["Chainage"]), 3))
            set_cell(grid, row, 3, round(float(rec["Easting"]), 3))
            set_cell(grid, row, 4, round(float(rec["Northing"]), 3))
            set_cell(grid, row, 5, f"=I{summary_row}+(J{summary_row}-I{summary_row})*(B{row}/MAX(E{summary_row},1E-6))")
            set_cell(grid, row, 6, round(float(rec["FilledWidth"]), 3))
            set_cell(grid, row, 7, f"={SIDE_Z_REF}")
            set_cell(grid, row, 8, round(float(rec["Deflection"]), 3))
            set_cell(grid, row, 9, 0 if is_first else f"=B{row}-B{prev_row}")

            area_fwd = trap_area(a1(row, 6), a1(row, 10))
            rad_fwd = trap_radius(a1(row, 6), a1(row, 10))
            vel_fwd = velocity_expr(q_ref, area_fwd)
            ws_fwd = f"=E{row}+J{row}+(({vel_fwd})^2)/(2*{G_REF})"
            sf_fwd = f"={friction_slope_expr(q_ref, area_fwd, rad_fwd)}"
            if is_first:
                set_cell(grid, row, 10, f"=MAX({MIN_DEPTH_REF},{start_stage_ref}-E{row})")
            else:
                set_cell(
                    grid,
                    row,
                    10,
                    f"=MAX({MIN_DEPTH_REF},J{prev_row}+({PROFILE_RELAX_REF}*P{row}))",
                )
            set_cell(grid, row, 11, f"={area_fwd}")
            set_cell(grid, row, 12, f"={rad_fwd}")
            set_cell(grid, row, 13, f"={vel_fwd}")
            set_cell(grid, row, 14, ws_fwd)
            set_cell(grid, row, 15, sf_fwd)
            if is_first:
                set_cell(grid, row, 16, 0)
                set_cell(grid, row, 17, 1)
            else:
                local_fwd = (
                    f"IF(M{prev_row}>M{row},{KE_REF}*((M{prev_row}^2-M{row}^2)/(2*{G_REF})),"
                    f"{KC_REF}*((M{row}^2-M{prev_row}^2)/(2*{G_REF})))"
                    f"+({KB_REF}*(ABS(H{row})/90)*(MAX(M{prev_row}^2,M{row}^2)/(2*{G_REF})))"
                )
                set_cell(
                    grid,
                    row,
                    16,
                    f"=N{prev_row}-(((O{prev_row}+O{row})/2)*I{row})-({local_fwd})-N{row}",
                )
                set_cell(grid, row, 17, f'=IF(ABS(P{row})<{PROFILE_TOL_REF},1,0)')

            area_bwd = trap_area(a1(row, 6), a1(row, 18))
            rad_bwd = trap_radius(a1(row, 6), a1(row, 18))
            vel_bwd = velocity_expr(q_ref, area_bwd)
            ws_bwd = f"=E{row}+R{row}+(({vel_bwd})^2)/(2*{G_REF})"
            sf_bwd = f"={friction_slope_expr(q_ref, area_bwd, rad_bwd)}"
            if is_last:
                set_cell(grid, row, 18, f"=MAX({MIN_DEPTH_REF},{end_stage_ref}-E{row})")
            else:
                set_cell(
                    grid,
                    row,
                    18,
                    f"=MAX({MIN_DEPTH_REF},R{next_row}+({PROFILE_RELAX_REF}*X{row}))",
                )
            set_cell(grid, row, 19, f"={area_bwd}")
            set_cell(grid, row, 20, f"={rad_bwd}")
            set_cell(grid, row, 21, f"={vel_bwd}")
            set_cell(grid, row, 22, ws_bwd)
            set_cell(grid, row, 23, sf_bwd)
            if is_last:
                set_cell(grid, row, 24, 0)
                set_cell(grid, row, 25, 1)
            else:
                local_bwd = (
                    f"IF(U{row}>U{next_row},{KE_REF}*((U{row}^2-U{next_row}^2)/(2*{G_REF})),"
                    f"{KC_REF}*((U{next_row}^2-U{row}^2)/(2*{G_REF})))"
                    f"+({KB_REF}*(ABS(H{next_row})/90)*(MAX(U{row}^2,U{next_row}^2)/(2*{G_REF})))"
                )
                set_cell(
                    grid,
                    row,
                    24,
                    f"=V{next_row}+(((W{row}+W{next_row})/2)*I{next_row})+({local_bwd})-V{row}",
                )
                set_cell(grid, row, 25, f'=IF(ABS(X{row})<{PROFILE_TOL_REF},1,0)')

            set_cell(grid, row, 26, f'=IF({mode_ref}="Supercritical",J{row},R{row})')
            area_fin = trap_area(a1(row, 6), a1(row, 26))
            top_fin = top_width(a1(row, 6), a1(row, 26))
            vel_fin = velocity_expr(q_ref, area_fin)
            set_cell(grid, row, 27, f"=E{row}+Z{row}+(({vel_fin})^2)/(2*{G_REF})")
            set_cell(grid, row, 28, f"=ABS({q_ref})/(MAX({area_fin},1E-6)*SQRT({G_REF}*MAX(({area_fin})/MAX({top_fin},1E-6),1E-6)))")
            set_cell(grid, row, 29, '=IF(AB{0}<1,"Subcritical","Supercritical")'.format(row))

    return segments, grid, total_rows, total_cols


def resize_worksheet(client, sheet_id, worksheet_name, row_count, col_count):
    worksheet = client.get_worksheet(sheet_id, worksheet_name)
    requests = [
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": worksheet.id,
                    "gridProperties": {"rowCount": row_count, "columnCount": col_count},
                },
                "fields": "gridProperties(rowCount,columnCount)",
            }
        }
    ]
    client.batch_update(sheet_id, requests)


def format_sheet(client, sheet_id, worksheet_name, total_rows):
    client.format_range(sheet_id, worksheet_name, "A1:I1", {
        "textFormat": {"bold": True, "fontSize": 14},
        "backgroundColor": {"red": 0.85, "green": 0.91, "blue": 0.97},
    })
    client.format_range(sheet_id, worksheet_name, "B4:D22", {
        "backgroundColor": {"red": 0.96, "green": 0.96, "blue": 0.96},
    })
    client.format_range(sheet_id, worksheet_name, "K4:Q8", {
        "backgroundColor": {"red": 0.95, "green": 0.98, "blue": 0.92},
    })
    client.format_range(sheet_id, worksheet_name, f"A{SUMMARY_HEADER_ROW}:U{SUMMARY_START_ROW+4}", {
        "backgroundColor": {"red": 0.96, "green": 0.94, "blue": 0.88},
    })
    client.format_range(sheet_id, worksheet_name, f"A{SOLVER_HEADER_ROW}:AB{SOLVER_START_ROW+SOLVER_STEPS-1}", {
        "backgroundColor": {"red": 0.93, "green": 0.96, "blue": 0.99},
    })
    client.format_range(sheet_id, worksheet_name, f"A{table_start_row_placeholder()}:AC{total_rows}", {
        "numberFormat": {"type": "NUMBER", "pattern": "0.000"},
    })


def table_start_row_placeholder():
    return 105


def update_1d_sheet(sheet_id=SHEET_ID, worksheet_name=WORKSHEET_NAME):
    _, grid, total_rows, total_cols = build_sheet()
    client = GoogleSheetsClient()
    resize_worksheet(client, sheet_id, worksheet_name, total_rows, total_cols)
    end_cell = f"{col_letter(total_cols)}{total_rows}"
    client.update_range(sheet_id, worksheet_name, f"A1:{end_cell}", grid, value_input_option="USER_ENTERED")

    format_jobs = [
        ("A1:I1", {
            "textFormat": {"bold": True, "fontSize": 14},
            "backgroundColor": {"red": 0.85, "green": 0.91, "blue": 0.97},
        }),
        ("B4:D22", {
            "backgroundColor": {"red": 0.96, "green": 0.96, "blue": 0.96},
        }),
        ("K4:Q8", {
            "backgroundColor": {"red": 0.95, "green": 0.98, "blue": 0.92},
            "textFormat": {"bold": True},
        }),
        (f"A{SUMMARY_HEADER_ROW}:U{SUMMARY_HEADER_ROW}", {
            "backgroundColor": {"red": 0.96, "green": 0.94, "blue": 0.88},
            "textFormat": {"bold": True},
        }),
        (f"A{SOLVER_HEADER_ROW}:AB{SOLVER_HEADER_ROW}", {
            "backgroundColor": {"red": 0.93, "green": 0.96, "blue": 0.99},
            "textFormat": {"bold": True},
        }),
    ]
    for cell_range, fmt in format_jobs:
        try:
            client.format_range(sheet_id, worksheet_name, cell_range, fmt)
        except Exception:
            # Formula deployment is the priority; formatting retries can be done later.
            pass


if __name__ == "__main__":
    update_1d_sheet()
