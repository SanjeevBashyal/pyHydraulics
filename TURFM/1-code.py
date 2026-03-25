import os
import numpy as np

PROJECT_FOLDER = r"C:\Users\Ripple\Downloads\Turkey Flood\9 HECRAS-Test"  # Base folder for the project

PROJECT_SHORT_NAME = "ATATURK-T"      # Name of the project

STEP_REACHED = 0

PROJECT_LONG_NAME = "BUR-BUR-MER-" + PROJECT_SHORT_NAME

PROJ_PATH = os.path.join(PROJECT_FOLDER, '0 Proj')
BUR_BUR_PATH = os.path.join(PROJECT_FOLDER, '1 Bur-Bur')
HYDRO_PATH = os.path.join(PROJECT_FOLDER, '2 Hydrology')
HEC_PATH = os.path.join(PROJECT_FOLDER, '3 Hecras')
GIS_PATH = os.path.join(PROJECT_FOLDER, '4 GIS')
OUTPUT_PATH = os.path.join(PROJECT_FOLDER,'5 Outputs')

DEM_PATH = os.path.join(PROJECT_FOLDER, 'SET4_27_DTM_070226_R1.tif')
CROSS_SECTION_PATH = os.path.join(BUR_BUR_PATH,'BUR-BUR-MER-'+PROJECT_SHORT_NAME,'KESIT_TESLIM')
CROSS_SECTION_FILE_PATH = os.path.join(CROSS_SECTION_PATH,'BUR-BUR-MER-'+PROJECT_SHORT_NAME+'_KESIT_TESLIM.csv')
BANK_LINE_PATH = os.path.join(BUR_BUR_PATH,'BUR-BUR-MER-'+PROJECT_SHORT_NAME,'SEV_USTU')
BANK_LINE_FILE_PATH = os.path.join(BANK_LINE_PATH, 'BUR-BUR-MER-'+PROJECT_SHORT_NAME+'_SEV_USTU.shp')

# ====== USER CONFIGURATION ======
# Determines how the synthetic channel smoothly mathematically fades back
# into the real terrain surface from the bank line outwardly mapping to the cross-section mask.
# Options: 'linear', 'exponential'
BLEND_TYPE = 'linear'
# ================================

def generate_blended_terrain_dtm():
    print(f"Running ultra-fast terrain mapping with '{BLEND_TYPE}' fading boundary logic...")

    # Bypass massive Python dict list generation (return_dicts=False) to execute strictly in C-native NumPy arrays
    _, modifier = DTMChannelModifier.process_dtm_cells(
        dtm_path=DEM_PATH,
        cross_section_csv=CROSS_SECTION_FILE_PATH,
        bank_shp_path=BANK_LINE_FILE_PATH,
        target_res=0.1,
        buffer_m=20.0,
        break_after_first=False,
        blend_type=BLEND_TYPE,
        return_dicts=False
    )

    if modifier is None:
        print("Failed to map matrices.")
        return

    # Extract master CRS exactly to prevent strict string mismatch errors during merge
    with rasterio.open(DEM_PATH, 'r') as src:
        master_crs = src.crs

    # 1. Save the strictly modified window chunk
    out_tif_path = os.path.join(OUTPUT_PATH, f"terrain_blended_window_{BLEND_TYPE}.tif")
    print(f"\nWriting natively interpolated window surface to GeoTIFF: {out_tif_path} ...")
    
    with rasterio.open(
        out_tif_path,
        'w',
        driver='GTiff',
        height=modifier.dtm_data.shape[0],
        width=modifier.dtm_data.shape[1],
        count=1,
        dtype=modifier.dtm_data.dtype,
        crs=master_crs,
        transform=modifier.dtm_transform,
        nodata=-9999
    ) as dest:
        dest.write(modifier.dtm_data, 1)

    print("Success! Process complete.")

if __name__ == "__main__":
    generate_blended_terrain_dtm()