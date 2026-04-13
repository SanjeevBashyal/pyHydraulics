from sheets import GoogleSheetsClient

def apply_dynamic_capacity(sheet_id, worksheet_name):
    client = GoogleSheetsClient()
    
    print("Connecting to 'Energy' sheet to update Installed Capacity formula...")
    
    # The formula targets cell D18.
    # $D$11 = Design Flow Percentile
    # E25:E36 = Usable Flow values
    # $D$14 = Net Head
    # $D$16 = Plant Efficiency
    # $I$4 = Density
    # $I$7 = Gravity
    
    # Formula calculates exceedance flow via PERCENTILE, then transforms it to MW 
    formula = "=(PERCENTILE(E25:E36, 1 - $D$11) * $D$14 * $D$16 * $I$4 * $I$7) / 1000000"
    
    client.update_range(
        sheet_id, 
        worksheet_name, 
        "D18:D18", 
        [[formula]], 
        value_input_option="USER_ENTERED"
    )
    
    # We will subtly format the cell to visually show users it is auto-computed
    client.format_range(
        sheet_id, 
        worksheet_name, 
        "D18:D18", 
        {
            "textFormat": {"bold": True, "foregroundColor": {"red": 0.0, "green": 0.3, "blue": 0.0}},
            "backgroundColor": {"red": 0.85, "green": 1.0, "blue": 0.85}
        }
    )
    
    print("Successfully mapped Installed Capacity to dynamic Percentile.")

if __name__ == "__main__":
    sheet_id = "1eJ7uyJdZmJus-4TmeQ1CkdMLbETq5xn_BzznjDScr64"
    worksheet_name = "Energy"
    apply_dynamic_capacity(sheet_id, worksheet_name)
