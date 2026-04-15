from sheets import GoogleSheetsClient

def execute_head_loss(sheet_id, worksheet_name):
    client = GoogleSheetsClient()
    
    print("Writing Dual-Pass Mixed Regime solver to Head Loss model...")
    
    # 1. Global Boundary Parameters Block 
    param_block = [
        ["Channel Properties", "", "Value", "", "", "Boundary Conditions", "", "Value"],
        ["", "Manning's n", "0.015", "", "", "Upstream Depth y_up", "m", "0.50"],
        ["", "Contraction Coeff (Kc)", "0.1", "", "", "Downstream Depth y_dn", "m", "2.00"],
        ["", "Expansion Coeff (Ke)", "0.3", "", "", "", "", ""],        
        ["", "Bend Coeff (Kb)", "0.15", "", "", "", "", ""],
        ["", "Reset (0: Hold, 1: Eval)", "1", "", "", "", "", ""],
        ["", "Convergence Limit", "0.001", "", "", "", "", ""],
        ["", "Relaxation Factor", "0.15", "", "", "", "", ""]
    ]
    client.update_range(sheet_id, worksheet_name, "B8:I15", param_block, value_input_option="USER_ENTERED")
    client.format_range(sheet_id, worksheet_name, "B8:I15", {
        "backgroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}
    })
    client.format_range(sheet_id, worksheet_name, "B8:I8", {
        "textFormat": {"bold": True}, "backgroundColor": {"red": 0.85, "green": 0.85, "blue": 0.85}
    })
    
    start_row = 18
    num_rows = 10
    
    # Two header rows to organize columns cleanly
    header_1 = [
        ["Inputs", "", "", "", "", "", "Forward Pass (Supercritical)", "", "", "", "", "", "", "", "", "", "", "",
         "Backward Pass (Subcritical)", "", "", "", "", "", "", "", "", "", "", "",
         "Final Profile", ""]
    ]
    header_2 = [
        ["Sec", "Dist", "Bed Z", "Bed Width b", "Side z", "Def Ang",
         "Length L", "y_sup", "A_sup", "P_sup", "R_sup", "v_sup", "H_sup", "Sf_sup", "Error", "Status", "Fr_sup", "M_sup",
         "Length L", "y_sub", "A_sub", "P_sub", "R_sub", "v_sub", "H_sub", "Sf_sub", "Error", "Status", "Fr_sub", "M_sub",
         "y_final", "Regime"]
    ]
    
    output_block = []
    output_block.extend(header_1)
    output_block.extend(header_2)
    
    for i in range(num_rows):
        cur = start_row + 2 + i
        prv = cur - 1
        nxt = cur + 1
        
        # Cross Section Variables
        col_sec = str(i)
        dist = i * 50
        col_dist = str(dist)
        col_z = str(100 - dist * 0.0001) if i == 0 else f"=C{prv} - (0.005 * (B{cur}-B{prv}))"
        col_b = "3.0"
        col_z_side = "0.0"
        col_ang = "0" if i==0 else "30" if i==5 else "0"
        
        row_arr = [col_sec, col_dist, col_z, col_b, col_z_side, col_ang]
        
        # --- FORWARD PASS (Supercritical) ---
        if i == 0:
            row_arr.extend(["0", "=$I$9"])
        else:
            len_fwd = f"=B{cur} - B{prv}"
            iter_y = f"=IF($D$13=0, H{prv}, IF(P{prv}=0, H{prv}, IF(H{cur}=0, H{prv}, IF(ABS(O{cur}) > $D$14, MAX(0.01, H{cur} + (O{cur} * $D$15 * IF(Q{cur} < 1, 1, -1))), H{cur}))))"
            row_arr.extend([len_fwd, iter_y])
            
        col_a_sup = f"=(D{cur} + E{cur}*H{cur})*H{cur}"
        col_p_sup = f"=D{cur} + 2*H{cur}*SQRT(1+E{cur}^2)"
        col_r_sup = f"=I{cur} / J{cur}"
        col_v_sup = f"=$D$3 / I{cur}"
        col_h_sup = f"=C{cur} + H{cur} + (L{cur}^2) / (2*$H$6)"
        col_sf_sup= f"=(L{cur} * $D$9 / (K{cur}^(2/3)))^2"
        
        if i == 0:
            row_arr.extend([col_a_sup, col_p_sup, col_r_sup, col_v_sup, col_h_sup, col_sf_sup, "0", f"=IF(ABS(O{cur})<$D$14, 1, 0)"])
        else:
            hf = f"( (N{prv}+N{cur})/2 ) * G{cur}"
            he = f"IF(L{prv}>L{cur}, $D$11*( (L{prv}^2)/(2*$H$6) - (L{cur}^2)/(2*$H$6) ), $D$10*( (L{cur}^2)/(2*$H$6) - (L{prv}^2)/(2*$H$6) )) + ($D$12 * (F{cur}/90) * (L{cur}^2)/(2*$H$6))"
            err= f"=M{prv} - ({hf}) - ({he}) - M{cur}"
            stat= f"=IF(ABS(O{cur})<$D$14, 1, 0)"
            row_arr.extend([col_a_sup, col_p_sup, col_r_sup, col_v_sup, col_h_sup, col_sf_sup, err, stat])
            
        fr_sup = f"=L{cur} / SQRT($H$6 * (I{cur}/(D{cur} + 2*E{cur}*H{cur})))"
        m_sup  = f"=(D{cur}*H{cur}^2)/2 + (E{cur}*H{cur}^3)/3 + ($D$3^2)/($H$6*I{cur})"
        row_arr.extend([fr_sup, m_sup])
        
        # --- BACKWARD PASS (Subcritical) ---
        if i == num_rows - 1:
            row_arr.extend(["0", "=$I$10"])
        else:
            len_bwd = f"=B{nxt} - B{cur}"
            iter_y_sub = f"=IF($D$13=0, T{nxt}, IF(AB{nxt}=0, T{nxt}, IF(T{cur}=0, T{nxt}, IF(ABS(AA{cur}) > $D$14, MAX(0.01, T{cur} + (AA{cur} * $D$15 * IF(AC{cur} < 1, 1, -1))), T{cur}))))"
            row_arr.extend([len_bwd, iter_y_sub])
            
        col_a_sub = f"=(D{cur} + E{cur}*T{cur})*T{cur}"
        col_p_sub = f"=D{cur} + 2*T{cur}*SQRT(1+E{cur}^2)"
        col_r_sub = f"=U{cur} / V{cur}"
        col_v_sub = f"=$D$3 / U{cur}"
        col_h_sub = f"=C{cur} + T{cur} + (X{cur}^2) / (2*$H$6)"
        col_sf_sub= f"=(X{cur} * $D$9 / (W{cur}^(2/3)))^2"
        
        if i == num_rows - 1:
            row_arr.extend([col_a_sub, col_p_sub, col_r_sub, col_v_sub, col_h_sub, col_sf_sub, "0", f"=IF(ABS(AA{cur})<$D$14, 1, 0)"])
        else:
            hf_b = f"( (Z{cur}+Z{nxt})/2 ) * S{cur}"
            he_b = f"IF(X{cur}>X{nxt}, $D$11*( (X{cur}^2)/(2*$H$6) - (X{nxt}^2)/(2*$H$6) ), $D$10*( (X{nxt}^2)/(2*$H$6) - (X{cur}^2)/(2*$H$6) )) + ($D$12 * (F{cur}/90) * (X{cur}^2)/(2*$H$6))"
            err_b= f"=Y{nxt} + ({hf_b}) + ({he_b}) - Y{cur}"
            stat_b= f"=IF(ABS(AA{cur})<$D$14, 1, 0)"
            row_arr.extend([col_a_sub, col_p_sub, col_r_sub, col_v_sub, col_h_sub, col_sf_sub, err_b, stat_b])
            
        fr_sub = f"=X{cur} / SQRT($H$6 * (U{cur}/(D{cur} + 2*E{cur}*T{cur})))"
        m_sub  = f"=(D{cur}*T{cur}^2)/2 + (E{cur}*T{cur}^3)/3 + ($D$3^2)/($H$6*U{cur})"
        row_arr.extend([fr_sub, m_sub])
        
        # --- FINAL ENVELOPE ---
        y_fin = f"=IF(AD{cur} > R{cur}, T{cur}, H{cur})"
        reg_fin = f'=IF(AD{cur} > R{cur}, "Subcritical", "Supercritical")'
        row_arr.extend([y_fin, reg_fin])
        
        output_block.append(row_arr)

    end_row = start_row + len(output_block) - 1
    cell_range = f"A{start_row}:AF{end_row}"
    
    print("Writing Dual-Pass mixed regime execution matrix...")
    client.update_range(sheet_id, worksheet_name, cell_range, output_block, value_input_option="USER_ENTERED")
    
    # -------------------------------------------------------------
    # Formatting
    # -------------------------------------------------------------
    client.format_range(sheet_id, worksheet_name, f"A{start_row}:AF{start_row}", {
        "textFormat": {"bold": True, "fontSize": 12},
        "horizontalAlignment": "CENTER"
    })
    client.format_range(sheet_id, worksheet_name, f"A{start_row+1}:AF{start_row+1}", {
        "textFormat": {"bold": True, "fontSize": 10}, "horizontalAlignment": "CENTER", "backgroundColor": {"red": 0.9, "green": 0.9, "blue": 0.9}
    })
    
    # Shade Trial depths dynamically
    client.format_range(sheet_id, worksheet_name, f"H{start_row+2}:H{end_row}", {
        "backgroundColor": {"red": 1.0, "green": 0.9, "blue": 0.8}
    })
    client.format_range(sheet_id, worksheet_name, f"T{start_row+2}:T{end_row}", {
        "backgroundColor": {"red": 0.85, "green": 1.0, "blue": 0.85}
    })
    client.format_range(sheet_id, worksheet_name, f"AE{start_row+2}:AF{end_row}", {
        "textFormat": {"bold": True}, "backgroundColor": {"red": 0.9, "green": 0.8, "blue": 1.0}
    })
    print("Dual regime mapping successfully pushed.")

if __name__ == "__main__":
    sheet_id = "1eJ7uyJdZmJus-4TmeQ1CkdMLbETq5xn_BzznjDScr64"
    worksheet_name = "Head Loss"
    execute_head_loss(sheet_id, worksheet_name)
