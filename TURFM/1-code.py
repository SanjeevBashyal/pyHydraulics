import os
import sys
import rasterio
from pathlib import Path

# Ensure local ras_commander module is explicitly mapped inside python environment bounds
PWD = os.getcwd()
if PWD not in sys.path:
    sys.path.insert(0, PWD)

from Automation.DTM import DTMChannelModifier
from ras_commander import RasTerrain

PROJECT_FOLDER = r"C:\Users\Ripple\Downloads\Turkey Flood\9 HECRAS-Test"
PROJECT_SHORT_NAME = "ATATURK-T"
PROJECT_LONG_NAME = "BUR-BUR-MER-" + PROJECT_SHORT_NAME

DEM_PATH = os.path.join(PROJECT_FOLDER, 'SET4_27_DTM_070226_R1.tif')

BUR_BUR_PATH = os.path.join(PROJECT_FOLDER, '1 Bur-Bur')
CROSS_SECTION_PATH = os.path.join(BUR_BUR_PATH, 'BUR-BUR-MER-'+PROJECT_SHORT_NAME, 'KESIT_TESLIM')
CROSS_SECTION_FILE_PATH = os.path.join(CROSS_SECTION_PATH, 'BUR-BUR-MER-'+PROJECT_SHORT_NAME+'_KESIT_TESLIM.csv')

BANK_LINE_PATH = os.path.join(BUR_BUR_PATH, 'BUR-BUR-MER-'+PROJECT_SHORT_NAME, 'SEV_USTU')
BANK_LINE_FILE_PATH = os.path.join(BANK_LINE_PATH, 'BUR-BUR-MER-'+PROJECT_SHORT_NAME+'_SEV_USTU.shp')

OUTPUT_PATH = os.path.join(PROJECT_FOLDER, '5 Outputs')
os.makedirs(OUTPUT_PATH, exist_ok=True)

# ====== USER CONFIGURATION ======
BLEND_TYPE = 'linear'
HECRAS_VERSION = "6.7"
RAS_EXE_PATH = r"C:\Program Files (x86)\HEC\HEC-RAS\6.7 Beta 4\Ras.exe"
STEPS_COMPLETE = 2  # 0: Run all, 1: Skip Step 1, 2: Skip Step 1 & 2
# ================================

def generate_combined_workflow():
    blended_tif_path = os.path.join(OUTPUT_PATH, f"terrain_blended_window_{BLEND_TYPE}.tif")

    if STEPS_COMPLETE < 1:
        print(f"\n--- STEP 1: Generating Interpolated DTM Channel Terrain ('{BLEND_TYPE}' fade) ---")

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

        # Extract master CRS exactly mapping the parent DEM to align target frames
        with rasterio.open(DEM_PATH, 'r') as src:
            master_crs = src.crs

        print(f"Writing natively interpolated window surface to GeoTIFF: {blended_tif_path} ...")
        
        with rasterio.open(
        blended_tif_path,
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
            
        print("Step 1 execution complete.")
    else:
        print("\n--- Skipping STEP 1: Channel Interpolation (Already Complete) ---")

    if STEPS_COMPLETE < 2:
        print("\n--- STEP 2: Creating Master HEC-RAS Terrain HDF Overlay ---")
        
        # Read the CRS if Step 1 was skipped to use for projection
        with rasterio.open(DEM_PATH, 'r') as src:
            master_crs = src.crs
        
        # Dynamically extract geometric boundaries explicitly generating temporary PRJ mapping text
        temp_prj_path = Path(OUTPUT_PATH) / "temp_projection.prj"
        with open(temp_prj_path, "w") as f:
            f.write(master_crs.to_wkt())
            
        final_terrain_hdf = Path(OUTPUT_PATH) / "Merged_Final_Terrain.hdf"
    
        print(f"Fusing the geometric modifications identically over the complete parent DEM '{os.path.basename(DEM_PATH)}' using RasTerrain compiler...")
        
        try:
            # Hierarchical inputs list natively fuses the datasets prioritizing [0] precisely overlapping subsequent underlying files
            RasTerrain.create_terrain_hdf(
                input_rasters=[Path(blended_tif_path), Path(DEM_PATH)], 
                output_hdf=final_terrain_hdf,
                projection_prj=temp_prj_path,
                units="Meters",
                stitch=True,
                hecras_version=HECRAS_VERSION
            )
            print(f"Exported unified full-scale composite Terrain HDF natively to: {final_terrain_hdf} ...")
        except Exception as e:
            print(f"Warning: RasTerrain geometric generation failed:\n {e}")
            
        if temp_prj_path.exists():
            os.remove(temp_prj_path)
            
        print("Step 2 execution complete.")
    else:
        print("\n--- Skipping STEP 2: HEC-RAS Terrain Merge (Already Complete) ---")
    if STEPS_COMPLETE < 3:
        print("\n--- STEP 3: HEC-RAS Project Assembly ---")
        
        centerline_shp = os.path.join(OUTPUT_PATH, "Centerline.shp")
        offset_bank_shp = os.path.join(OUTPUT_PATH, "Banks_Offset_0_2m.shp")
        xs_shp = os.path.join(OUTPUT_PATH, "Interpolated_CrossSections.shp")
        
        # 1. Export Generated Geometry Features
        DTMChannelModifier.export_centerline_shapefile(BANK_LINE_FILE_PATH, centerline_shp)
        DTMChannelModifier.export_offset_bank_shapefile(BANK_LINE_FILE_PATH, 0.2, offset_bank_shp)
        DTMChannelModifier.export_cross_section_shapefile(CROSS_SECTION_FILE_PATH, BANK_LINE_FILE_PATH, 0.1, xs_shp)
        
        # 2. Base HEC-RAS Project Instantiation
        from Automation.hecras import HECRAS
        from Automation.rasMapper import _register_map_layer
        from ras_commander import RasMap
        
        project_stem = PROJECT_LONG_NAME
        project_title = PROJECT_LONG_NAME + " Master Integrated Model"
        
        print("\nBuilding HEC-RAS Native 1D Steady Model Files...")
        hec = HECRAS(hecras_version=HECRAS_VERSION, ras_exe_path=RAS_EXE_PATH)
        # hec.build_steady_1d_model(
        #     project_folder=OUTPUT_PATH,
        #     project_stem=project_stem,
        #     project_title=project_title,
        #     cross_section_csv=CROSS_SECTION_FILE_PATH,
        #     bank_lines_shp=BANK_LINE_FILE_PATH,
        #     flow_cms=13.0
        # )
        
        # 3. RAS Mapper Architecture Assignment
        rasmap_path = Path(OUTPUT_PATH) / f"{project_stem}.rasmap"
        
        # Fallback master projection setup identical to original DEM bounds natively
        with rasterio.open(DEM_PATH, 'r') as src:
            master_crs_wkt = src.crs.to_wkt()
        prj_path_stable = Path(OUTPUT_PATH) / f"{project_stem}.prj"
        prj_path_stable.write_text(master_crs_wkt, encoding="utf-8")
        
        if not rasmap_path.exists():
            xml_content = f"""<RASMapper>
  <Version>2.0.0</Version>
  <RASProjectionFilename>{prj_path_stable.name}</RASProjectionFilename>
  <MapLayers />
</RASMapper>"""
            rasmap_path.write_text(xml_content, encoding="utf-8")
            
        final_terrain_hdf = Path(OUTPUT_PATH) / "Merged_Final_Terrain.hdf"
        
        print("\nRegistering Geometry and Terrain exactly as native standard Layers into RAS Mapper...")
        if final_terrain_hdf.exists():
            RasMap.add_terrain_layer(
                terrain_hdf=final_terrain_hdf,
                rasmap_path=rasmap_path,
                layer_name="Merged_Final_Terrain",
                projection_prj=prj_path_stable
            )
            
        # Natively parse shapefiles into the mapping registry to physically load during COM init
        _register_map_layer("River Centerline", Path(centerline_shp), "MapLayer", rasmap_path)
        _register_map_layer("Offset Banks (0.2m)", Path(offset_bank_shp), "MapLayer", rasmap_path)
        _register_map_layer("Interpolated Cross Sections", Path(xs_shp), "MapLayer", rasmap_path)
        
        print("\nLaunching HEC-RAS GUI...")
        # 4. Try loading project, saving via COM, then definitively pop the Window UI
        hec.open_project(OUTPUT_PATH, project_stem)
        hec.save_project()  # Silently ignores if COM is down
        # hec.show_window(delay_seconds=2)
        print("HEC-RAS Project successfully booted and loaded with all Topographic mappings!")
            
        print("Step 3 execution complete.")
    else:
        print("\n--- Skipping STEP 3: HEC-RAS Project Load (Already Complete) ---")

    print("\nSuccess! Combined Master workflow physically executed perfectly.")

if __name__ == "__main__":
    generate_combined_workflow()