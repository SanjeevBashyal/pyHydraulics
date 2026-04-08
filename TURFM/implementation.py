import sys
import os

# Ensure local module directory is mapped to the python environment
PWD = os.path.dirname(os.path.abspath(__file__))
if PWD not in sys.path:
    sys.path.insert(0, PWD)

from Automation.configProject import Config
from Automation.callDTM import DTM

print("===========================================")
print("   TURFM DTM Processing Implementation")
print("===========================================\n")

# 1. Initialize Configuration
print("Loading Configuration and ensuring directories exist...")
app_config = Config()

# Example: Set project context dynamically (Users can modify this path for their own usage)
user_project_path = r"C:\Users\Ripple\Downloads\Turkey Flood\9 HECRAS-Test"
app_config.set_project_folder(user_project_path, short_name="ATATURK-T")

app_config.setup_directories()

# 2. Instantiate DTM Processing logic
dtm_processor = DTM(app_config)
print("\nStarting DTM extractions:\n")

# 3. Implement the three methods cleanly
try:
    # A. Get Interpolated TIF File
    tif_file = dtm_processor.get_interpolated_tif(target_res=0.1, buffer_m=20.0)
    print(f"-> Successfully generated Interpolated TIF: {tif_file}\n")
    
    # B. Get River Centerline Shapefile
    centerline_file = dtm_processor.get_river_centerline()
    print(f"-> Successfully generated River Centerline: {centerline_file}\n")
    
    # C. Get Bank Lines Shapefile (offset 0.2m)
    bank_lines_file = dtm_processor.get_bank_lines(offset_m=0.2)
    print(f"-> Successfully generated Offset Bank Lines: {bank_lines_file}\n")
    
except Exception as e:
    print(f"\n[ERROR] Processing failed: {e}")
    sys.exit(1)

print("===========================================")
print("   Processing Completed Successfully!")
print("===========================================")
