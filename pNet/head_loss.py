from sheets import GoogleSheetsClient

def execute_head_loss(sheet_id, worksheet_name):
    client = GoogleSheetsClient()
    
    print("Wiring HEC-RAS Newton-Raphson global properties to the Head Loss model...")
    # -------------------------------------------------------------
    # 1. Global Parameters Block 
    # -------------------------------------------------------------
    param_block = [
        ["Channel Properties", "", "Value", "", "", "Geometric Constraints", "", "Value"],
        ["", "Manning's n", "0.015", "", "", "Bed Width (b)", "m", "3.00"],
        ["", "Contraction Coeff (Kc)", "0.1", "", "", "Side Slope (z) [1:z]", "-", "0.0"],
        ["", "Expansion Coeff (Ke)", "0.3", "", "", "Bend Coeff (Kb)", "-", "0.15"]
    ]
    client.update_range(sheet_id, worksheet_name, "B8:I11", param_block, value_input_option="USER_ENTERED")
    client.format_range(sheet_id, worksheet_name, "B8:I8", {
        "textFormat": {"bold": True}, "backgroundColor": {"red": 0.85, "green": 0.85, "blue": 0.85}
    })
    
    # -------------------------------------------------------------
    # 2. Main Step Method Table
    # -------------------------------------------------------------
    start_row = 14
    table_headers = [
        [
            "Sec", "Dist (m)", "Def Ang (deg)", "Length L (m)", "Bed Elev Z (m)", 
            "Trial Depth y (m)", "Top Width T (m)", "Area A (m2)", "Hyd Depth Dh (m)", "Froude Fr", 
            "Regime", "W_Perim P (m)", "Hyd Rad R (m)", "Vel v (m/s)", "Vel Head (m)", 
            "Total Head H (m)", "Fric Slope Sf", "Avg Sf", "Fric Loss hf (m)", "Minor Loss he (m)", 
            "Error Delta"
        ]
    ]
    
    # Static anchors:
    # Q = $D$3
    # g = $H$6
    # Manning n = $D$9
    # Kc = $D$10
    # Ke = $D$11
    # Bed Width = $I$9
    # Side slope z = $I$10
    # Bend Coeff Kb = $I$11
    
    output_block = []
    output_block.extend(table_headers)
    
    for i in range(10):
        current_row = start_row + 1 + i
        prev_row = current_row - 1
        
        # Generic user columns A, B, C
        col_sec = str(i)
        col_dist = str(i * 50)
        col_def_ang = "0" if i == 0 else "15" if i == 5 else "0"
        
        if i == 0:
            col_l = "0"      # D
            col_z = "100"    # E
            col_y = "1.5"    # F (Explicity fixed depth for BC)
        else:
            col_l = f"=B{current_row} - B{prev_row}"
            col_z = f"=E{prev_row} - (0.001 * D{current_row})"
            
            # The HEC-RAS Newton Raphson Loop:
            # Prevents #DIV/0 natively during Froude check
            fr2_delta = f"IF(ABS(1 - J{current_row}^2) < 0.0001, 0.0001, 1 - J{current_row}^2)"
            newton_step = f"MAX(-0.5, MIN(0.5, U{current_row} / {fr2_delta}))"
            
            col_y = f'=IF(F{current_row}=0, F{prev_row}, IF(ABS(U{current_row}) > 0.001, MAX(0.01, F{current_row} + {newton_step}), F{current_row}))'
            
        # Geometric Columns (G:I)
        col_T = f"=$I$9 + 2*$I$10*F{current_row}"
        col_A = f"=($I$9 + $I$10*F{current_row})*F{current_row}"
        col_Dh = f"=H{current_row} / G{current_row}"
        
        # Froude Evaluators (J:K)
        col_Fr = f"=N{current_row} / SQRT($H$6 * I{current_row})"
        col_Regime = f'=IF(J{current_row}<1, "Sub-critical", IF(J{current_row}>1, "Super-critical", "Critical"))'
        
        # Remaining Hydraulics (L:Q)
        col_P = f"=$I$9 + 2*F{current_row}*SQRT(1+$I$10^2)"
        col_R = f"=H{current_row} / L{current_row}"
        col_v = f"=$D$3 / H{current_row}"
        col_vh = f"=(N{current_row}^2) / (2*$H$6)"
        col_H = f"=E{current_row} + F{current_row} + O{current_row}"
        col_Sf = f"=(N{current_row} * $D$9 / (M{current_row}^(2/3)))^2"
        
        if i == 0:
            col_AvgSf = "0"  # R
            col_hf = "0"     # S
            col_he = "0"     # T
            col_err = "0"    # U
        else:
            col_AvgSf = f"=(Q{prev_row} + Q{current_row})/2"
            col_hf = f"=R{current_row} * D{current_row}"
            
            loss_ec = f"IF(O{current_row} > O{prev_row}, $D$10 * ABS(O{current_row}-O{prev_row}), $D$11 * ABS(O{current_row}-O{prev_row}))"
            loss_bend = f"($I$11 * (C{current_row}/90) * O{current_row})"
            col_he = f"={loss_ec} + {loss_bend}"
            
            col_err = f"= P{prev_row} - S{current_row} - T{current_row} - P{current_row}"
        
        output_block.append([
            col_sec, col_dist, col_def_ang, col_l, col_z, col_y, col_T, col_A, col_Dh, 
            col_Fr, col_Regime, col_P, col_R, col_v, col_vh, col_H, col_Sf, 
            col_AvgSf, col_hf, col_he, col_err
        ])
    
    end_row = start_row + len(output_block) - 1
    cell_range = f"A{start_row}:U{end_row}"
    
    print("Deploying strictly aligned HEC-RAS Newton-Raphson Iterative Formulation...")
    client.update_range(sheet_id, worksheet_name, cell_range, output_block, value_input_option="USER_ENTERED")
    
    # Header format
    client.format_range(sheet_id, worksheet_name, f"A{start_row}:U{start_row}", {
        "textFormat": {"bold": True}, "backgroundColor": {"red": 0.8, "green": 0.9, "blue": 1.0}
    })
    
    worksheet = client.get_worksheet(sheet_id, worksheet_name)
    sheet_idx = worksheet.id
    
    # Conditional formatting for Regime
    requests = [
        {
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [{"sheetId": sheet_idx, "startRowIndex": start_row, "endRowIndex": end_row, "startColumnIndex": 10, "endColumnIndex": 11}],
                    "booleanRule": {
                        "condition": {"type": "TEXT_CONTAINS", "values": [{"userEnteredValue": "Super-critical"}]},
                        "format": {"backgroundColor": {"red": 1.0, "green": 0.8, "blue": 0.8}, "textFormat": {"bold": True}}
                    }
                }, "index": 0
            }
        },
        {
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [{"sheetId": sheet_idx, "startRowIndex": start_row, "endRowIndex": end_row, "startColumnIndex": 20, "endColumnIndex": 21}],
                    "booleanRule": {
                        "condition": {"type": "NUMBER_BETWEEN", "values": [{"userEnteredValue": "-0.01"}, {"userEnteredValue": "0.01"}]},
                        "format": {"backgroundColor": {"red": 0.8, "green": 1.0, "blue": 0.8}, "textFormat": {"bold": True}}
                    }
                }, "index": 0
            }
        }
    ]
    client.batch_update(sheet_id, requests)
    
    # Format Trial depth y
    client.format_range(sheet_id, worksheet_name, f"F{start_row+2}:F{end_row}", {
        "backgroundColor": {"red": 0.95, "green": 0.85, "blue": 1.0}
    })
    
    print("HEC-RAS alignment successfully executed.")

if __name__ == "__main__":
    sheet_id = "1eJ7uyJdZmJus-4TmeQ1CkdMLbETq5xn_BzznjDScr64"
    worksheet_name = "Head Loss"
    execute_head_loss(sheet_id, worksheet_name)
