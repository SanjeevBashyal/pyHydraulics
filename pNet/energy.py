from sheets import GoogleSheetsClient

def design_energy_sheet(sheet_id, worksheet_name):
    client = GoogleSheetsClient()
    
    print("Starting Energy sheet architecture and formula generation...")
    
    # -------------------------------------------------------------
    # 1. Write the Title Header
    # -------------------------------------------------------------
    title_block = [["Hydropower Energy Generation Model"]]
    client.update_range(sheet_id, worksheet_name, "B2:B2", title_block, value_input_option="USER_ENTERED")
    client.format_range(sheet_id, worksheet_name, "B2:D2", {
        "textFormat": {"bold": True, "fontSize": 14},
        "backgroundColor": {"red": 0.2, "green": 0.6, "blue": 0.86} # A nice blue
    })
    
    # -------------------------------------------------------------
    # 2. Write the Global Parameters Block
    # -------------------------------------------------------------
    # We will write this block starting at B4
    parameter_headers = [
        ["Fundamental Layout Parameters", "", "", ""],
        ["Parameter", "Unit", "Value", "Remarks"],
        ["Density of Water (ρ)", "kg/m3", 998.2, "Standard at 20°C"],
        ["Gravity (g)", "m/s2", 9.81, "Acceleration due to gravity"],
        ["Net Head (H_net)", "m", 100.0, "Effective head available"],
        ["Plant Efficiency (η)", "-", 0.85, "Combined turbine/generator efficiency"],
        ["Installed Capacity", "MW", 50.0, "Max plant output"],
        ["Environmental Release", "m3/s", 2.0, "Mandatory downstream flow"]
    ]
    
    client.update_range(sheet_id, worksheet_name, "B4:E11", parameter_headers, value_input_option="USER_ENTERED")
    
    # Style the Parameters Header
    client.format_range(sheet_id, worksheet_name, "B4:E5", {
        "textFormat": {"bold": True},
        "backgroundColor": {"red": 0.85, "green": 0.85, "blue": 0.85}
    })
    
    # -------------------------------------------------------------
    # 3. Write Sector Headers for the Table
    # -------------------------------------------------------------
    # Starts at row 14
    start_row = 14
    table_headers = [
        [
            "Month", "Days", "Avg River Flow (m3/s)", "Usable Flow (m3/s)", 
            "Available Power (MW)", "Generated Power (MW)", "Monthly Energy (GWh)"
        ]
    ]
    
    # Constants references explicitly mapped
    # rho = $D$6
    # g = $D$7
    # H_net = $D$8
    # efficiency = $D$9
    # capacity = $D$10
    # env_flow = $D$11
    
    months = [
        ("Jan", 31), ("Feb", 28), ("Mar", 31), ("Apr", 30), 
        ("May", 31), ("Jun", 30), ("Jul", 31), ("Aug", 31), 
        ("Sep", 30), ("Oct", 31), ("Nov", 30), ("Dec", 31)
    ]
    
    # Placeholder hypothetical hydrograph flows for demonstration
    flows = [12.5, 10.2, 8.5, 15.6, 25.4, 60.5, 120.4, 155.2, 95.5, 45.3, 22.1, 15.0]
    
    output_block = []
    output_block.extend(table_headers)
    
    for i, (m, d) in enumerate(months):
        current_row = start_row + 1 + i
        flow = flows[i]
        
        # B: Month
        col_m = m
        # C: Days
        col_d = d
        # D: River Flow
        col_river = flow
        # E: Usable Flow = MAX(0, River Flow - Env Flow)
        col_usable = f"=MAX(0, D{current_row} - $D$11)"
        
        # F: Available Power (MW) = (rho * g * Q * H * eff) / 1,000,000
        col_avail_pwr = f"=(($D$6 * $D$7 * E{current_row} * $D$8 * $D$9) / 1000000)"
        
        # G: Generated Power (MW) = MIN(Available, Capacity)
        col_gen_pwr = f"=MIN(F{current_row}, $D$10)"
        
        # H: Monthly Energy (GWh) = Generated Power * Days * 24 / 1000
        col_energy = f"=(G{current_row} * C{current_row} * 24) / 1000"
        
        output_block.append([col_m, col_d, col_river, col_usable, col_avail_pwr, col_gen_pwr, col_energy])
        
    end_row = start_row + len(output_block) - 1
    cell_range = f"B{start_row}:H{end_row}"
    
    client.update_range(sheet_id, worksheet_name, cell_range, output_block, value_input_option="USER_ENTERED")
    
    # Format the Table Header
    client.format_range(sheet_id, worksheet_name, f"B{start_row}:H{start_row}", {
        "textFormat": {"bold": True},
        "backgroundColor": {"red": 0.8, "green": 0.8, "blue": 0.8}
    })
    
    # -------------------------------------------------------------
    # 4. Summary Block
    # -------------------------------------------------------------
    summary_start = end_row + 2
    
    # Calculate total energy natively
    summary_block = [
        ["Total Annual Energy (GWh)", "", f"=SUM(H{start_row+1}:H{end_row})"],
        ["Plant Capacity Factor (%)", "", f"=(D{summary_start} * 1000) / ($D$10 * 365 * 24) * 100"] 
        # Total Energy (GWh) is D{summary_start}. 
        # Actually it's written in Column D because Column B is label.
        # Wait, if "Total Annual Energy" is in B, "" is C, formula is D. 
    ]
    
    client.update_range(sheet_id, worksheet_name, f"B{summary_start}:D{summary_start+1}", summary_block, value_input_option="USER_ENTERED")
    
    # Format Summary
    client.format_range(sheet_id, worksheet_name, f"B{summary_start}:D{summary_start+1}", {
        "textFormat": {"bold": True},
        "backgroundColor": {"red": 0.8, "green": 1.0, "blue": 0.8}
    })
    
    print("Energy sheet architecture and formula logic successfully deployed.")


if __name__ == "__main__":
    sheet_id = "1eJ7uyJdZmJus-4TmeQ1CkdMLbETq5xn_BzznjDScr64"
    worksheet_name = "Energy"
    design_energy_sheet(sheet_id, worksheet_name)
