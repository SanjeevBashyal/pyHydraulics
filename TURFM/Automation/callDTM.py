import os
import rasterio
from Automation.DTM import DTMChannelModifier

class DTM:
    """
    Wrapper class to interface with DTMChannelModifier for specifically extracting
    the interpolated TIF, river centerline, and offset bank lines.
    """
    def __init__(self, config):
        self.config = config

    def get_interpolated_tif(self, target_res=0.1, buffer_m=20.0):
        """
        Generates the interpolated DTM channel terrain fading and returns its file path.
        """
        print(f"--- STEP 1: Generating Interpolated DTM Channel Terrain ('{self.config.BLEND_TYPE}' fade) ---")
        
        blended_tif_path = os.path.join(self.config.OUTPUT_PATH, f"terrain_blended_window_{self.config.BLEND_TYPE}.tif")
        
        _, modifier = DTMChannelModifier.process_dtm_cells(
            dtm_path=self.config.DEM_PATH,
            cross_section_csv=self.config.CROSS_SECTION_FILE_PATH,
            bank_shp_path=self.config.BANK_LINE_FILE_PATH,
            target_res=target_res,
            buffer_m=buffer_m,
            break_after_first=False,
            blend_type=self.config.BLEND_TYPE,
            return_dicts=False
        )

        if modifier is None:
            raise RuntimeError("Failed to map matrices for interpolated DTM.")

        # Extract master CRS exactly mapping the parent DEM to align target frames
        with rasterio.open(self.config.DEM_PATH, 'r') as src:
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
            
        print("Interpolated TIF generation complete.")
        return blended_tif_path

    def get_river_centerline(self, output_filename="Centerline.shp"):
        """
        Generates and exports the river centerline shapefile, returning its path.
        """
        print("--- STEP 2: Generating River Centerline ---")
        centerline_shp = os.path.join(self.config.OUTPUT_PATH, output_filename)
        DTMChannelModifier.export_centerline_shapefile(self.config.BANK_LINE_FILE_PATH, centerline_shp)
        return centerline_shp

    def get_bank_lines(self, output_filename="Banks_Offset_0_2m.shp", offset_m=0.2):
        """
        Generates and exports the bank lines with the specified offset (default 0.2m), returning its path.
        """
        print(f"--- STEP 3: Generating Bank Lines (Offset: {offset_m}m) ---")
        offset_bank_shp = os.path.join(self.config.OUTPUT_PATH, output_filename)
        DTMChannelModifier.export_offset_bank_shapefile(self.config.BANK_LINE_FILE_PATH, offset_m, offset_bank_shp)
        return offset_bank_shp

    def get_study_perimeter(self, output_filename="Study_Perimeter.shp", offset_m=500.0):
        """
        Generates and exports the study perimeter polygon (buffered on both sides by offset_m), returning its path.
        """
        print(f"--- STEP 4: Generating Study Perimeter (Offset: {offset_m}m) ---")
        perimeter_shp = os.path.join(self.config.OUTPUT_PATH, output_filename)
        DTMChannelModifier.export_study_perimeter(self.config.BANK_LINE_FILE_PATH, perimeter_shp, offset_m)
        return perimeter_shp
