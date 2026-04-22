import json
from pathlib import Path
import re

import rasterio

from Automation.DTM import DTMChannelModifier


class DTM:
    """
    Wrapper class around DTMChannelModifier.

    Legacy methods still process the active config sub-project. The project-level
    helpers use configProject.Config to resolve every sub-project from the
    Structure sheet and run a shared-window, junction-aware DTM build.
    """

    def __init__(self, config):
        self.config = config

    def discover_project_subprojects(self):
        project_subprojects = {}
        for entry in self.config.FOLDER_ENTRIES:
            parts = entry.relative_parts
            if len(parts) == 2 and parts[0] == "1 Bur-Bur":
                project_subprojects.setdefault(parts[1], [])
            elif len(parts) == 3 and parts[0] == "1 Bur-Bur":
                project_subprojects.setdefault(parts[1], []).append(parts[2])

        return {
            project: sub_projects
            for project, sub_projects in project_subprojects.items()
            if sub_projects
        }

    def get_project_channel_inputs(self, project_name, sub_project_names=None):
        if sub_project_names is None:
            sub_project_names = self.discover_project_subprojects().get(project_name, [])

        if not sub_project_names:
            raise ValueError(f"No sub-projects found for project: {project_name}")

        channel_inputs = []
        for sub_project_name in sub_project_names:
            paths = self.config.get_sub_project_paths(project_name, sub_project_name)
            channel_inputs.append(
                {
                    "name": paths.sub_project_name,
                    "cross_section_csv": paths.cross_section_file_path,
                    "bank_shp_path": paths.bank_line_file_path,
                }
            )

        return channel_inputs

    def process_project_channels(
        self,
        project_name,
        sub_project_names=None,
        target_res=0.1,
        buffer_m=20.0,
        junction_tolerance=50.0,
        perimeter_offset_m=500.0,
        write_intermediate=True,
    ):
        channel_inputs = self.get_project_channel_inputs(project_name, sub_project_names)
        output_dir = Path(self.config.get_hecras_project_path(project_name)) / "DTM"
        output_dir.mkdir(parents=True, exist_ok=True)

        terrain_stem = (
            f"{project_name}_channel_terrain"
            if len(channel_inputs) == 1
            else f"{project_name}_junction_channel_terrain"
        )

        print(f"--- Processing DTM project {project_name}: {[item['name'] for item in channel_inputs]} ---")
        return DTMChannelModifier.process_channel_network_dtm(
            dtm_path=self.config.DEM_PATH,
            channel_inputs=channel_inputs,
            output_tif_path=output_dir / f"{terrain_stem}.tif",
            target_res=target_res,
            buffer_m=buffer_m,
            blend_type=self.config.BLEND_TYPE,
            junction_tolerance=junction_tolerance,
            write_intermediate=write_intermediate,
            centerline_output_path=output_dir / f"{project_name}_Centerlines.shp",
            merged_banks_output_path=output_dir / f"{project_name}_Merged_Banks.shp",
            perimeter_output_path=output_dir / f"{project_name}_Study_Perimeter.shp",
            perimeter_offset_m=perimeter_offset_m,
        )

    def process_structure_projects(
        self,
        projects=None,
        target_res=0.1,
        buffer_m=20.0,
        junction_tolerance=50.0,
        perimeter_offset_m=500.0,
        write_intermediate=True,
    ):
        project_subprojects = self.discover_project_subprojects()
        if isinstance(projects, str):
            projects = [projects]
        if projects is not None:
            selected = {self._normalize_name(project) for project in projects}
            project_subprojects = {
                project: sub_projects
                for project, sub_projects in project_subprojects.items()
                if self._normalize_name(project) in selected
            }

        if not project_subprojects:
            raise ValueError("No projects were selected from the Structure sheet.")

        results = []
        for project_name, sub_project_names in project_subprojects.items():
            results.append(
                self.process_project_channels(
                    project_name=project_name,
                    sub_project_names=sub_project_names,
                    target_res=target_res,
                    buffer_m=buffer_m,
                    junction_tolerance=junction_tolerance,
                    perimeter_offset_m=perimeter_offset_m,
                    write_intermediate=write_intermediate,
                )
            )

        summary_path = Path(self.config.HEC_PATH) / "implementationDTM_summary.json"
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"\nDTM summary written to: {summary_path}")
        return results

    def get_interpolated_tif(self, target_res=0.1, buffer_m=20.0):
        """
        Generates the interpolated DTM channel terrain for the active sub-project.
        """
        print(f"--- STEP 1: Generating Interpolated DTM Channel Terrain ('{self.config.BLEND_TYPE}' fade) ---")

        output_dir = Path(self.config.OUTPUT_PATH)
        output_dir.mkdir(parents=True, exist_ok=True)
        blended_tif_path = output_dir / f"terrain_blended_window_{self.config.BLEND_TYPE}.tif"

        _, modifier = DTMChannelModifier.process_dtm_cells(
            dtm_path=self.config.DEM_PATH,
            cross_section_csv=self.config.CROSS_SECTION_FILE_PATH,
            bank_shp_path=self.config.BANK_LINE_FILE_PATH,
            target_res=target_res,
            buffer_m=buffer_m,
            break_after_first=False,
            blend_type=self.config.BLEND_TYPE,
            return_dicts=False,
        )

        if modifier is None:
            raise RuntimeError("Failed to map matrices for interpolated DTM.")

        with rasterio.open(self.config.DEM_PATH, "r") as src:
            master_crs = src.crs

        print(f"Writing natively interpolated window surface to GeoTIFF: {blended_tif_path} ...")
        with rasterio.open(
            blended_tif_path,
            "w",
            driver="GTiff",
            height=modifier.dtm_data.shape[0],
            width=modifier.dtm_data.shape[1],
            count=1,
            dtype=modifier.dtm_data.dtype,
            crs=master_crs,
            transform=modifier.dtm_transform,
            nodata=-9999,
        ) as dest:
            dest.write(modifier.dtm_data, 1)

        print("Interpolated TIF generation complete.")
        return str(blended_tif_path)

    def get_river_centerline(self, output_filename="Centerline.shp"):
        """
        Generates and exports the river centerline shapefile for the active sub-project.
        """
        print("--- STEP 2: Generating River Centerline ---")
        centerline_shp = Path(self.config.OUTPUT_PATH) / output_filename
        centerline_shp.parent.mkdir(parents=True, exist_ok=True)
        DTMChannelModifier.export_centerline_shapefile(self.config.BANK_LINE_FILE_PATH, str(centerline_shp))
        return str(centerline_shp)

    def get_bank_lines(self, output_filename="Banks_Offset_0_2m.shp", offset_m=0.2):
        """
        Generates active sub-project bank lines with outward offset.
        """
        print(f"--- STEP 3: Generating Bank Lines (Offset: {offset_m}m) ---")
        offset_bank_shp = Path(self.config.OUTPUT_PATH) / output_filename
        offset_bank_shp.parent.mkdir(parents=True, exist_ok=True)
        DTMChannelModifier.export_offset_bank_shapefile(self.config.BANK_LINE_FILE_PATH, offset_m, str(offset_bank_shp))
        return str(offset_bank_shp)

    def get_study_perimeter(self, output_filename="Study_Perimeter.shp", offset_m=500.0):
        """
        Generates the active sub-project study perimeter polygon.
        """
        print(f"--- STEP 4: Generating Study Perimeter (Offset: {offset_m}m) ---")
        perimeter_shp = Path(self.config.OUTPUT_PATH) / output_filename
        perimeter_shp.parent.mkdir(parents=True, exist_ok=True)
        DTMChannelModifier.export_study_perimeter(self.config.BANK_LINE_FILE_PATH, str(perimeter_shp), offset_m)
        return str(perimeter_shp)

    @staticmethod
    def _normalize_name(value):
        return re.sub(r"[^0-9A-Za-z]+", "", str(value)).upper()
