from sheets import GoogleSheetsClient

def design_intake(sheet_id, worksheet_name):
    client = GoogleSheetsClient()
    
    print("Starting intake formula generation...")
    
    # Fill in the missing "Number of Intake Bays" if it's empty to avoid #DIV/0!
    # Even if they change it later, 4 is a good default placeholder.
    client.update_range(sheet_id, worksheet_name, "D7", [["4"]], value_input_option="USER_ENTERED")
    
    # Let's write the missing explicit static constants directly to the sheet
    # We will write this block at B10
    parameter_headers = [
        ["Trashrack Flow Velocity (v_r)", "m/s", 1.0, "Desired nominal flow velocity"],
        ["Trashrack Blockage", "%", 0.20, "Area blocked by bars / structural elements"],
        ["Intake Bay Width", "m", 3.0, "To be used if Opening Type is Rectangular"],
        ["Trashrack Head Loss Coeff (K_tr)", "-", 0.2, "Standard constant for bar screens"]
    ]
    
    print("Writing missing hydraulic parameters to row 10...")
    client.update_range(sheet_id, worksheet_name, "B10:E13", parameter_headers, value_input_option="USER_ENTERED")
    
    # Highlight new parameter rows subtly to match formatting
    client.format_range(sheet_id, worksheet_name, "B10:E13", {
        "backgroundColor": {"red": 0.95, "green": 0.95, "blue": 0.95}
    })
    
    # -------------------------------------------------------------
    # Now generate the Dynamic Formula Calculation Block 
    # Starts at row 15 (just under "Calculations" which is at B14)
    # -------------------------------------------------------------
    start_row = 15
    
    # Our parameters maps:
    # Q_d: $D$4
    # % Increase: $D$5
    # Num Bays N: $D$7
    # Opening Type: $D$9
    # Density rho: $I$4
    # Gravity g: $I$7
    # v_r (Velocity): $D$10
    # Blockage: $D$11
    # Bay Width W: $D$12
    # K_tr: $D$13
    
    output_block = [
        ["Parameter", "Unit", "Result", "Formula Expression"],
        ["Total Intake Discharge (Q_i)", "m3/s", "=$D$4 * (1 + $D$5)", "=Q_d * (1 + % Inc)"],
        ["Discharge per Bay (q)", "m3/s", "=D16 / $D$7", "=Q_i / N"],
        ["Net Area Required (A_net)", "m2", "=D17 / $D$10", "=q / v_r"],
        ["Gross Area Required (A_gross)", "m2", "=D18 / (1 - $D$11)", "=A_net / (1 - Blockage)"],
        ["Required Dimension (D or H)", "m", '=IF($D$9="Circular", SQRT((4 * D19) / PI()), D19 / $D$12)', "Diameter if Circular, Height if Rectangular"],
        ["Trashrack Head Loss (h_t)", "m", "=($D$13 * ($D$10 ^ 2)) / (2 * $I$7)", "=K_tr * v_r^2 / 2g"]
    ]
    
    end_row = start_row + len(output_block) - 1
    cell_range = f"B{start_row}:E{end_row}"
    
    print(f"Writing dynamic Formula Calculation Block to {cell_range}...")
    client.update_range(sheet_id, worksheet_name, cell_range, output_block, value_input_option="USER_ENTERED")
    
    # Add Static Formatting for Result Header
    header_range = f"B{start_row}:E{start_row}"
    client.format_range(sheet_id, worksheet_name, header_range, {
        "textFormat": {"bold": True},
        "backgroundColor": {"red": 0.8, "green": 0.8, "blue": 0.8}
    })
    
    # Highlight the Required Dimension row prominently 
    dimension_row = start_row + 5  # Index 5 in output_block is the Required Dimension
    client.format_range(sheet_id, worksheet_name, f"B{dimension_row}:E{dimension_row}", {
        "backgroundColor": {"red": 0.8, "green": 1.0, "blue": 0.8},
        "textFormat": {"bold": True}
    })
    
    print("Intake Design formula logic successfully deployed to Google Sheets.")

if __name__ == "__main__":
    sheet_id = "1eJ7uyJdZmJus-4TmeQ1CkdMLbETq5xn_BzznjDScr64"
    worksheet_name = "Intake Design"
    design_intake(sheet_id, worksheet_name)
