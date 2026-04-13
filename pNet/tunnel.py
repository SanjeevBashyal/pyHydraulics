import math
from sheets import GoogleSheetsClient

def optimize_tunnel(sheet_id, worksheet_name):
    client = GoogleSheetsClient()
    
    print("Starting tunnel formula generation...")
    
    # Let's write the missing explicit constants directly to the sheet first
    parameter_headers = [
        ["Parameter", "Unit", "Value", "Remarks"],
        ["Tunnel Length (L)", "m", 5000, "Assumed Length"],
        ["Roughness (ε)", "m", 0.003, "Concrete lining"],
        ["Efficiency (η)", "-", 0.90, "Plant Efficiency"],
        ["Tariff per kWh", "$/kWh", 0.07, "Energy Price"],
        ["Excavation Rate", "$/m3", 150, "Unit const. cost"],
        ["hours per year * PF", "h", 5256, "365 * 24 * 0.6"],
        ["PV Factor", "-", 9.4269, "30 years @ 10%"],
    ]
    
    print("Writing missing parameters to row 11...")
    # Writing to B11:E18
    client.update_range(sheet_id, worksheet_name, "B11:E18", parameter_headers)
    
    # We also need to style the new header at B11:E11
    client.format_range(sheet_id, worksheet_name, "B11:E11", {
        "textFormat": {"bold": True},
        "backgroundColor": {"red": 0.8, "green": 0.8, "blue": 0.8}
    })
    
    # Now generate the Dynamic Formula Table
    # Start the table at row 21 to give some breathing room
    start_row = 21
    
    headers = [
        "Diameter D (m)", "Velocity V (m/s)", "Reynolds No. Re", "Friction Factor f", 
        "Head Loss hf (m)", "Const. Cost ($)", "PV Energy Lost ($)", "Total Cost ($)"
    ]
    
    diameters = [d * 0.5 for d in range(6, 31)]  # 3.0 to 15.0 m
    
    output_values = [headers]
    
    # Map of fixed references
    # Q_d: $D$4
    # rho: $I$4
    # mu: $I$5
    # g: $I$7
    
    # New constant references
    # Tunnel Length L: $D$12
    # Roughness epsilon: $D$13
    # Efficiency: $D$14
    # Tariff: $D$15
    # Exc Rate: $D$16
    # Hours: $D$17
    # PV Factor: $D$18
    
    for i, D in enumerate(diameters):
        current_row = start_row + 1 + i
        
        # Diameter Column B
        col_B = f"{D:.1f}"
        
        # Velocity Column C: Q/(pi * D^2 / 4)
        col_C = f"=($D$4)/(PI()*($B{current_row}^2)/4)"
        
        # Reynolds Column D: rho * V * D / mu
        col_D = f"=($I$4*C{current_row}*$B{current_row})/$I$5"
        
        # Friction Factor Column E: Swamee-Jain
        col_E = f'=IF(D{current_row}<2000, 64/D{current_row}, 0.25/(LOG10(($D$13/(3.7*$B{current_row})) + (5.74/(D{current_row}^0.9))))^2)'
        
        # Head loss Column F: f * (L/D) * (V^2 / 2g)
        col_F = f"=E{current_row}*($D$12/$B{current_row})*(C{current_row}^2)/(2*$I$7)"
        
        # Construction Cost G: Area * Length * UnitRate
        col_G = f"=(PI()*($B{current_row}^2)/4) * $D$12 * $D$16"
        
        # PV Energy Lost H: Power * Hrs * Tariff * PVFactor
        # Power = rho * g * Q * hf / 1000 * efficiency
        col_H = f"=(($I$4*$I$7*$D$4*F{current_row}*$D$14)/1000) * $D$17 * $D$15 * $D$18"
        
        # Total Cost I: Const Cost + PV Energy Lost
        col_I = f"=G{current_row}+H{current_row}"
        
        output_values.append([col_B, col_C, col_D, col_E, col_F, col_G, col_H, col_I])
        
    end_row = start_row + len(output_values) - 1
    cell_range = f"B{start_row}:I{end_row}"
    
    print(f"Writing dynamic formula table to {cell_range}...")
    client.update_range(sheet_id, worksheet_name, cell_range, output_values, value_input_option="USER_ENTERED")
    
    # Add Static Formatting for Header
    header_range = f"B{start_row}:I{start_row}"
    client.format_range(sheet_id, worksheet_name, header_range, {
        "textFormat": {"bold": True},
        "backgroundColor": {"red": 0.8, "green": 0.8, "blue": 0.8}
    })
    
    # Number Formatting for columns C to I
    # We can format the table so numbers aren't wildly long
    # (Optional, but looks nice in Google Sheets)
    
    print("Applying Dynamic Conditional Formatting...")
    # Add Conditional Formatting to highlight minimum row in Column I
    # We must use batch_update since `format()` doesn't support conditional format rules
    
    # We need the sheetIdx of the "3 Tunnel Optimisation" worksheet
    worksheet = client.get_worksheet(sheet_id, worksheet_name)
    sheet_idx = worksheet.id
    
    # Delete existing conditional formatting rules first (optional, but good if script runs multiple times)
    # We can't easily clear rules cleanly without getting them first. Instead we just push a new rule.
    # Be aware multiple runs will stack rules, but for now we push one.
    
    requests = [
        {
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [{
                        "sheetId": sheet_idx,
                        "startRowIndex": start_row,      # 0-indexed, so row 22 = index 21
                        "endRowIndex": end_row,          # end of our table
                        "startColumnIndex": 1,           # Col B (index 1)
                        "endColumnIndex": 9              # Col J (index 9, exclusive)
                    }],
                    "booleanRule": {
                        "condition": {
                            "type": "CUSTOM_FORMULA",
                            "values": [{"userEnteredValue": f"=$I{start_row+1}=MIN($I${start_row+1}:$I${end_row})"}]
                        },
                        "format": {
                            "backgroundColor": {"red": 0.8, "green": 1.0, "blue": 0.8},
                            "textFormat": {"bold": True}
                        }
                    }
                },
                "index": 0
            }
        }
    ]
    
    client.batch_update(sheet_id, requests)
    
    print("Optimization formula logic successfully deployed to Google Sheets.")

if __name__ == "__main__":
    sheet_id = "1eJ7uyJdZmJus-4TmeQ1CkdMLbETq5xn_BzznjDScr64"
    worksheet_name = "3 Tunnel Optimisation"
    optimize_tunnel(sheet_id, worksheet_name)
