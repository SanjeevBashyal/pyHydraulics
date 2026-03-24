import os
import rasterio
import numpy as np

from Automation.DTM import DTMChannelModifier

PROJECT_FOLDER = r"C:\Users\Ripple\Downloads\Turkey Flood\9 HECRAS-Test"
PROJECT_SHORT_NAME = "ATATURK-T"
PROJECT_LONG_NAME = "BUR-BUR-MER-" + PROJECT_SHORT_NAME

DEM_PATH = os.path.join(PROJECT_FOLDER, 'SET4_27_DTM_070226_R1.tif')

# Setup Paths
PWD = os.getcwd()
CROSS_SECTION_FILE_PATH = os.path.join(PWD, "cross-section.csv")
BANK_LINE_FILE_PATH = os.path.join(PWD, r"0 HECRAS-Template\1 Bur-Bur\BUR-BUR-MER-ATATURK-T\SEV_USTU\BUR-BUR-MER-ATATURK-T_SEV_USTU_V1.shp")
OUTPUT_PATH = os.path.join(PWD, r"0 HECRAS-Template\5 Outputs")

if not os.path.exists(OUTPUT_PATH):
    os.makedirs(OUTPUT_PATH, exist_ok=True)

# ====== USER CONFIGURATION ======
# Determines how the synthetic channel smoothly mathematically fades back
# into the real terrain surface from the bank line outwardly mapping to the cross-section mask.
# Options: 'linear', 'exponential'
BLEND_TYPE = 'linear'
# ================================

def generate_blended_terrain_dtm():
    print(f"Running terrain mapping with '{BLEND_TYPE}' fading boundary logic...")

    # Process all mathematically mapped cells natively
    results, modifier = DTMChannelModifier.process_dtm_cells(
        dtm_path=DEM_PATH,
        cross_section_csv=CROSS_SECTION_FILE_PATH,
        bank_shp_path=BANK_LINE_FILE_PATH,
        target_res=0.1,
        buffer_m=20.0,
        break_after_first=False,
        blend_type=BLEND_TYPE
    )

    if not results:
        print("No cells found inside the target geometries.")
        return

    out_tif_path = os.path.join(OUTPUT_PATH, f"terrain_blended_dtm_{BLEND_TYPE}.tif")
    print(f"\nWriting new continuous blended surface to natively integrated GeoTIFF: {out_tif_path} ...")
    
    with rasterio.open(
        out_tif_path,
        'w',
        driver='GTiff',
        height=modifier.dtm_data.shape[0],
        width=modifier.dtm_data.shape[1],
        count=1,
        dtype=modifier.dtm_data.dtype,
        crs=modifier.dtm_crs,
        transform=modifier.dtm_transform,
        nodata=-9999
    ) as dest:
        dest.write(modifier.dtm_data, 1)

    print("Success! Process complete.")


if __name__ == "__main__":
    generate_blended_terrain_dtm()
