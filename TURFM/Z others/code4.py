import os
import rasterio
import numpy as np

from Automation.DTM import DTMChannelModifier

PROJECT_FOLDER = r"C:\Users\Ripple\Downloads\Turkey Flood\9 HECRAS-Test"  # Base folder for the project

PROJECT_SHORT_NAME = "ATATURK-T"      # Name of the project

PROJECT_LONG_NAME = "BUR-BUR-MER-" + PROJECT_SHORT_NAME

# PROJ_PATH = os.path.join(PROJECT_FOLDER, '0 Proj')
# BUR_BUR_PATH = os.path.join(PROJECT_FOLDER, '1 Bur-Bur')
# HYDRO_PATH = os.path.join(PROJECT_FOLDER, '2 Hydrology')
# HEC_PATH = os.path.join(PROJECT_FOLDER, '3 Hecras')
# GIS_PATH = os.path.join(PROJECT_FOLDER, '4 GIS')
# OUTPUT_PATH = os.path.join(PROJECT_FOLDER,'5 Outputs')

DEM_PATH = os.path.join(PROJECT_FOLDER, 'SET4_27_DTM_070226_R1.tif')

# Setup Paths
PWD = os.getcwd()
CROSS_SECTION_FILE_PATH = os.path.join(PWD, "cross-section.csv")
BANK_LINE_FILE_PATH = os.path.join(PWD, r"0 HECRAS-Template\1 Bur-Bur\BUR-BUR-MER-ATATURK-T\SEV_USTU\BUR-BUR-MER-ATATURK-T_SEV_USTU_V1.shp")
# DEM_PATH = os.path.join(PWD, r"0 HECRAS-Template\SET4_27_DTM_070226_R1.tif")
OUTPUT_PATH = os.path.join(PWD, r"0 HECRAS-Template\5 Outputs")

if not os.path.exists(OUTPUT_PATH):
    os.makedirs(OUTPUT_PATH, exist_ok=True)


def generate_interpolated_dtm():
    print("Running extensive DTM cell interpolation over the entire masked channel...")

    # Process ALL cells inside the bank polygon (break_after_first=False)
    results, modifier = DTMChannelModifier.process_dtm_cells(
        dtm_path=DEM_PATH,
        cross_section_csv=CROSS_SECTION_FILE_PATH,
        bank_shp_path=BANK_LINE_FILE_PATH,
        target_res=0.1,
        buffer_m=20.0,
        break_after_first=False
    )

    if not results:
        print("No cells found inside the bank polygon to modify.")
        return

    print("\nReplacing existing raster elevations with new cross-section interpolations...")
    
    # Make a physical copy of the native windowed DTM to prevent accidental overwrites
    new_dtm_data = np.copy(modifier.dtm_data)

    successful_cells = 0
    for cell in results:
        new_z = cell.get('new_interpolated_z')
        if new_z is not None:
            r = cell['row']
            c = cell['col']
            
            # Sub in our dynamically computed Z-value
            new_dtm_data[r, c] = new_z
            successful_cells += 1

    print(f"Successfully burned in {successful_cells} new cell elevations over {len(results)} valid channel cells.")

    out_tif_path = os.path.join(OUTPUT_PATH, "interpolated_channel_dtm.tif")
    print(f"\nWriting new continuous surface to GeoTIFF: {out_tif_path} ...")
    
    with rasterio.open(
        out_tif_path,
        'w',
        driver='GTiff',
        height=new_dtm_data.shape[0],
        width=new_dtm_data.shape[1],
        count=1,
        dtype=new_dtm_data.dtype,
        crs=modifier.dtm_crs,
        transform=modifier.dtm_transform,
        nodata=-9999
    ) as dest:
        dest.write(new_dtm_data, 1)

    print("Success! Process complete.")


if __name__ == "__main__":
    generate_interpolated_dtm()
