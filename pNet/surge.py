from sheets import GoogleSheetsClient


TITLE_ROW = 101
HEADER_ROW = 102
DESCRIPTION_ROW = 103
FORMULA_ROW = 104
UNITS_ROW = 105
DATA_START_ROW = 106
INPUT_RANGE = "H44:K54"

DT_REF = "$J$46"
BOUNDARY_HEAD_REF = "$J$47"
INLET_AREA_REF = "$J$48"
OUTLET_AREA_REF = "$J$49"
K_IN_REF = "$J$51"
K_OUT_REF = "$J$52"
FINAL_Q_REF = "$J$53"
INITIAL_HS_REF = "$J$54"


def _discharge_formula(row):
    if row == DATA_START_ROW:
        return "=$D$3"

    return (
        f'=IF(OR($D$15="",$D$15<=0),{FINAL_Q_REF},'
        f'IF($D$3>={FINAL_Q_REF},'
        f'MAX({FINAL_Q_REF},$D$3-($D$3-{FINAL_Q_REF})*(C{row}/$D$15)),'
        f'MIN({FINAL_Q_REF},$D$3+({FINAL_Q_REF}-$D$3)*(C{row}/$D$15))))'
    )


def _build_rk4_input_block():
    return [
        ["", "RK4 Inputs", "", ""],
        ["", "Parameter", "Value", "Remarks"],
        ["", "Time Step (dt)", 0.1, "Recommended RK4 step"],
        ["", "Boundary Head / NWL", "=$D$5", "Normal headwater level reference"],
        ["", "Inlet Area A_in", "=$E$30", "Connecting tunnel area"],
        ["", "Outlet Area A_out", "", "Enter the orifice outlet area"],
        ["", "Area Ratio beta", '=IF(OR(J48="",J49=""),"",J49/J48)', "beta = A_out / A_in"],
        ["", "K_in", '=IF(OR(J48="",J49=""),0,(J48/J49-1)^2)', "K_in = (A_in/A_out - 1)^2"],
        ["", "K_out", '=IF(OR(J48="",J49=""),0,(1-J49/J48)^2)', "K_out = (1 - A_out/A_in)^2"],
        ["", "Final Turbine Discharge", 0, "0 for full load rejection"],
        ["", "Initial Surge Tank Head", f"={BOUNDARY_HEAD_REF}-$D$52*($D$34^2)", "Steady-state start for off-line surge tank"],
    ]


def _build_rk4_rows(end_row):
    rows = [[10, "Solution of Differential Equations (Transient Analysis using RK4 Method)"]]

    headers = [
        "",
        "",
        "t",
        "dt",
        "Ho(t)",
        "Ho(mid)",
        "Q(t)",
        "Q(mid)",
        "Vn",
        "Hs(t)",
        "Vc",
        "K_orifice",
        "h_branch",
        "kV1",
        "kZ1",
        "kV2",
        "kZ2",
        "kV3",
        "kZ3",
        "kV4",
        "kZ4",
        "Vn+1",
        "Hs(t+dt)",
        "Power",
        "Surge Height",
    ]
    description = [
        "",
        "",
        "time",
        "step",
        "reservoir head",
        "mid-step head",
        "turbine discharge",
        "mid-step discharge",
        "current tunnel velocity",
        "current surge tank head",
        "(At*V-Q)/Ac",
        "directional orifice K",
        "(beta_c+K/2g)Vc|Vc|",
        "dt*fV1",
        "dt*fZ1",
        "dt*fV2",
        "dt*fZ2",
        "dt*fV3",
        "dt*fZ3",
        "dt*fV4",
        "dt*fZ4",
        "next velocity",
        "next surge head",
        "power output",
        "Hs-NWL",
    ]
    latex_formula = [
        "",
        "",
        "$t_n$",
        "$\\Delta t$",
        "$H_{0,n}$",
        "$\\frac{H_{0,n}+H_{0,n+1}}{2}$",
        "$Q_n$",
        "$\\frac{Q_n+Q_{n+1}}{2}$",
        "$V_n$",
        "$H_{s,n}$",
        "$V_{c,n}=\\frac{A_tV_n-Q_n}{A_c}$",
        "$K_n=\\mathrm{IF}(V_{c,n}\\ge0,K_{in},K_{out})$",
        "$h_{br,n}=(\\beta_c+\\frac{K_n}{2g})V_{c,n}|V_{c,n}|$",
        "$k_{V1}=\\Delta t\\,f_V(V_n,H_{s,n},Q_n)$",
        "$k_{Z1}=\\Delta t\\,f_Z(V_n,Q_n)$",
        "$k_{V2}=\\Delta t\\,f_V(V_n+\\frac{k_{V1}}{2},H_{s,n}+\\frac{k_{Z1}}{2},Q_{mid})$",
        "$k_{Z2}=\\Delta t\\,f_Z(V_n+\\frac{k_{V1}}{2},Q_{mid})$",
        "$k_{V3}=\\Delta t\\,f_V(V_n+\\frac{k_{V2}}{2},H_{s,n}+\\frac{k_{Z2}}{2},Q_{mid})$",
        "$k_{Z3}=\\Delta t\\,f_Z(V_n+\\frac{k_{V2}}{2},Q_{mid})$",
        "$k_{V4}=\\Delta t\\,f_V(V_n+k_{V3},H_{s,n}+k_{Z3},Q_{n+1})$",
        "$k_{Z4}=\\Delta t\\,f_Z(V_n+k_{V3},Q_{n+1})$",
        "$V_{n+1}=V_n+\\frac{k_{V1}+2k_{V2}+2k_{V3}+k_{V4}}{6}$",
        "$H_{s,n+1}=H_{s,n}+\\frac{k_{Z1}+2k_{Z2}+2k_{Z3}+k_{Z4}}{6}$",
        "$P_n=\\eta_t\\eta_g\\rho gQ_nH_n$",
        "$y_n=H_{s,n}-\\mathrm{NWL}$",
    ]
    units = [
        "",
        "",
        "s",
        "s",
        "m",
        "m",
        "m3/s",
        "m3/s",
        "m/s",
        "m",
        "m/s",
        "-",
        "m",
        "m/s",
        "m",
        "m/s",
        "m",
        "m/s",
        "m",
        "m/s",
        "m",
        "m/s",
        "m",
        "MW",
        "m",
    ]

    rows.extend([headers, description, latex_formula, units])

    for row in range(DATA_START_ROW, end_row + 1):
        is_last = row == end_row
        prev_row = row - 1
        next_row = row + 1

        time_formula = "0" if row == DATA_START_ROW else f"=C{prev_row}+{DT_REF}"
        dt_formula = f"={DT_REF}"
        ho_formula = f"={BOUNDARY_HEAD_REF}"
        ho_mid_formula = "" if is_last else f"=(E{row}+E{next_row})/2"
        q_formula = _discharge_formula(row)
        q_mid_formula = "" if is_last else f"=(G{row}+G{next_row})/2"
        v_formula = "=$D$34" if row == DATA_START_ROW else f"=V{prev_row}"
        hs_formula = f"={INITIAL_HS_REF}" if row == DATA_START_ROW else f"=W{prev_row}"
        vc_formula = f"=($D$30*I{row}-G{row})/$E$30"
        k_orifice_formula = f"=IF(K{row}>=0,{K_IN_REF},{K_OUT_REF})"
        h_branch_formula = f"=($E$52+(L{row}/(2*$K$6)))*K{row}*ABS(K{row})"

        if is_last:
            kv1_formula = ""
            kz1_formula = ""
            kv2_formula = ""
            kz2_formula = ""
            kv3_formula = ""
            kz3_formula = ""
            kv4_formula = ""
            kz4_formula = ""
            v_next_formula = ""
            hs_next_formula = ""
        else:
            vc2 = f"(($D$30*(I{row}+N{row}/2)-H{row})/$E$30)"
            vc3 = f"(($D$30*(I{row}+P{row}/2)-H{row})/$E$30)"
            vc4 = f"(($D$30*(I{row}+R{row})-G{next_row})/$E$30)"
            h_branch2 = f"(($E$52+(IF({vc2}>=0,{K_IN_REF},{K_OUT_REF})/(2*$K$6)))*{vc2}*ABS({vc2}))"
            h_branch3 = f"(($E$52+(IF({vc3}>=0,{K_IN_REF},{K_OUT_REF})/(2*$K$6)))*{vc3}*ABS({vc3}))"
            h_branch4 = f"(($E$52+(IF({vc4}>=0,{K_IN_REF},{K_OUT_REF})/(2*$K$6)))*{vc4}*ABS({vc4}))"

            kv1_formula = f"={DT_REF}*($K$6/$D$22)*(E{row}-(J{row}+M{row})-$D$52*I{row}*ABS(I{row}))"
            kz1_formula = f"={DT_REF}*(($D$30*I{row}-G{row})/$D$65)"
            kv2_formula = f"={DT_REF}*($K$6/$D$22)*(F{row}-(J{row}+O{row}/2)-{h_branch2}-$D$52*(I{row}+N{row}/2)*ABS(I{row}+N{row}/2))"
            kz2_formula = f"={DT_REF}*(($D$30*(I{row}+N{row}/2)-H{row})/$D$65)"
            kv3_formula = f"={DT_REF}*($K$6/$D$22)*(F{row}-(J{row}+Q{row}/2)-{h_branch3}-$D$52*(I{row}+P{row}/2)*ABS(I{row}+P{row}/2))"
            kz3_formula = f"={DT_REF}*(($D$30*(I{row}+P{row}/2)-H{row})/$D$65)"
            kv4_formula = f"={DT_REF}*($K$6/$D$22)*(E{next_row}-(J{row}+S{row})-{h_branch4}-$D$52*(I{row}+R{row})*ABS(I{row}+R{row}))"
            kz4_formula = f"={DT_REF}*(($D$30*(I{row}+R{row})-G{next_row})/$D$65)"
            v_next_formula = f"=I{row}+(N{row}+2*P{row}+2*R{row}+T{row})/6"
            hs_next_formula = f"=J{row}+(O{row}+2*Q{row}+2*S{row}+U{row})/6"

        power_formula = (
            f"=($D$10*$D$11*$K$3*$K$6*G{row}*"
            f"($D$8+(J{row}-E{row})+(((G{row}/$F$30)^2)/(2*$K$6))-$F$52*(G{row}/$F$30)^2))"
            f"/1000000"
        )
        surge_formula = f"=J{row}-{BOUNDARY_HEAD_REF}"

        rows.append([
            "",
            "",
            time_formula,
            dt_formula,
            ho_formula,
            ho_mid_formula,
            q_formula,
            q_mid_formula,
            v_formula,
            hs_formula,
            vc_formula,
            k_orifice_formula,
            h_branch_formula,
            kv1_formula,
            kz1_formula,
            kv2_formula,
            kz2_formula,
            kv3_formula,
            kz3_formula,
            kv4_formula,
            kz4_formula,
            v_next_formula,
            hs_next_formula,
            power_formula,
            surge_formula,
        ])

    return rows


def update_surge_sheet(sheet_id, worksheet_name):
    client = GoogleSheetsClient()
    worksheet = client.get_worksheet(sheet_id, worksheet_name)
    end_row = worksheet.row_count

    client.update_range(
        sheet_id,
        worksheet_name,
        INPUT_RANGE,
        _build_rk4_input_block(),
        value_input_option="USER_ENTERED",
    )
    client.format_range(sheet_id, worksheet_name, "H44:K45", {
        "textFormat": {"bold": True},
        "backgroundColor": {"red": 0.8, "green": 0.8, "blue": 0.8},
    })

    client.update_range(
        sheet_id,
        worksheet_name,
        f"A{TITLE_ROW}:Y{end_row}",
        _build_rk4_rows(end_row),
        value_input_option="USER_ENTERED",
    )
    client.format_range(sheet_id, worksheet_name, f"A{TITLE_ROW}:B{TITLE_ROW}", {
        "textFormat": {"bold": True},
        "backgroundColor": {"red": 0.8, "green": 0.8, "blue": 0.8},
    })
    client.format_range(sheet_id, worksheet_name, f"C{HEADER_ROW}:Y{UNITS_ROW}", {
        "textFormat": {"bold": True},
        "backgroundColor": {"red": 0.8, "green": 0.8, "blue": 0.8},
    })

    client.update_range(
        sheet_id,
        worksheet_name,
        "D76:D76",
        [[f"=MAX($Y${DATA_START_ROW}:$Y${end_row - 1})"]],
        value_input_option="USER_ENTERED",
    )
    client.update_range(
        sheet_id,
        worksheet_name,
        "D84:D84",
        [[f"=MIN($Y${DATA_START_ROW}:$Y${end_row - 1})"]],
        value_input_option="USER_ENTERED",
    )


if __name__ == "__main__":
    SHEET_ID = "1eJ7uyJdZmJus-4TmeQ1CkdMLbETq5xn_BzznjDScr64"
    WORKSHEET_NAME = "Surge"
    update_surge_sheet(SHEET_ID, WORKSHEET_NAME)
