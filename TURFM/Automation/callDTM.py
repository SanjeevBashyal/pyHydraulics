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
    active structure source and run a shared-window, junction-aware DTM build.
    """

    def __init__(self, config):
        self.config = config

    def discover_project_subprojects(self):
        return self.config.discover_project_subprojects()

    def get_project_channel_inputs(self, project_name, sub_project_names=None):
        if sub_project_names is None:
            sub_project_names = self.discover_project_subprojects().get(project_name, [])

        if not sub_project_names:
            raise ValueError(f"No sub-projects found for project: {project_name}")

        channel_inputs = []
        for sub_project_name in sub_project_names:
            paths = self.config.get_sub_project_paths(
                project_name,
                sub_project_name,
                resolve_dtm=True,
            )
            channel_inputs.append(
                {
                    "name": paths.sub_project_name,
                    "cross_section_csv": paths.cross_section_file_path,
                    "bank_shp_path": paths.bank_line_file_path,
                    "dtm_path": paths.dtm_path,
                }
            )

        return channel_inputs

    def get_network_csv_path(self):
        for filename in ("networks.csv", "network.csv"):
            preferred = Path(self.config.ESSENTIALS_PATH) / filename
            if preferred.exists():
                return preferred

        candidates = []
        for filename in ("networks.csv", "network.csv"):
            candidates.extend(Path(self.config.PROJECT_FOLDER).glob(f"0*Essentials*/{filename}"))
        candidates = sorted(candidates)
        if candidates:
            return candidates[0]
        return None

    def preflight_project_dtms(self, project_subprojects):
        missing = []
        for project_name, sub_project_names in project_subprojects.items():
            for sub_project_name in sub_project_names:
                try:
                    self.config.resolve_dtm_path(project_name, sub_project_name)
                except FileNotFoundError as exc:
                    missing.append(f"{project_name}/{sub_project_name}: {exc}")

        if missing:
            raise FileNotFoundError(
                "Could not resolve DTM raster(s) before running DTM interpolation:\n"
                + "\n".join(f"- {message}" for message in missing)
            )

    def process_project_channels(
        self,
        project_name,
        sub_project_names=None,
        target_res=0.1,
        buffer_m=20.0,
        blend_type=None,
        bank_offset_m=0.2,
        full_cross_section_weight_distance_m=1.5,
        transition_to_dtm_distance_m=5.0,
        junction_tolerance=50.0,
        perimeter_offset_m=500.0,
        write_intermediate=True,
        network_csv_path=None,
        centerline_gap_m=0.5,
        junction_bank_clip_buffer_m=5.0,
        junction_clip_cross_section_count=2,
        junction_half_section_interpolation=True,
        junction_bank_structure_protection_m=1.0,
        skewness_correction=True,
    ):
        channel_inputs = self.get_project_channel_inputs(project_name, sub_project_names)
        gis_output_dir = Path(self.config.get_gis_project_path(project_name)) / "DTM"
        temp_output_dir = Path(self.config.get_temp_project_path(project_name)) / "DTM"
        gis_output_dir.mkdir(parents=True, exist_ok=True)
        temp_output_dir.mkdir(parents=True, exist_ok=True)
        resolved_blend_type = blend_type or self.config.BLEND_TYPE
        resolved_network_csv_path = network_csv_path or self.get_network_csv_path()

        terrain_stem = (
            f"{project_name}_channel_terrain"
            if len(channel_inputs) == 1
            else f"{project_name}_junction_channel_terrain"
        )

        print(f"--- Processing DTM project {project_name}: {[item['name'] for item in channel_inputs]} ---")
        channel_groups: dict[str, list[dict]] = {}
        for channel in channel_inputs:
            dtm_path = str(Path(channel.get("dtm_path") or self.config.DEM_PATH))
            channel_groups.setdefault(dtm_path, []).append(channel)

        results = []
        multiple_dtms = len(channel_groups) > 1
        if multiple_dtms:
            print(
                f"Project {project_name} uses {len(channel_groups)} DTM rasters; "
                "processing one shared raster window per DTM group."
            )

        for dtm_path, grouped_channels in channel_groups.items():
            group_suffix = ""
            if multiple_dtms:
                group_suffix = f"_{DTMChannelModifier._safe_name(Path(dtm_path).stem)}"

            group_terrain_stem = (
                f"{project_name}_channel_terrain{group_suffix}"
                if len(grouped_channels) == 1
                else f"{terrain_stem}{group_suffix}"
            )

            result = DTMChannelModifier.process_channel_network_dtm(
                dtm_path=dtm_path,
                channel_inputs=grouped_channels,
                output_tif_path=gis_output_dir / f"{group_terrain_stem}.tif",
                target_res=target_res,
                buffer_m=buffer_m,
                blend_type=resolved_blend_type,
                bank_offset_m=bank_offset_m,
                full_cross_section_weight_distance_m=full_cross_section_weight_distance_m,
                transition_to_dtm_distance_m=transition_to_dtm_distance_m,
                junction_tolerance=junction_tolerance,
                write_intermediate=write_intermediate,
                centerline_output_path=gis_output_dir / f"{project_name}_Centerlines{group_suffix}.shp",
                merged_banks_output_path=gis_output_dir / f"{project_name}_Merged_Banks{group_suffix}.shp",
                perimeter_output_path=gis_output_dir / f"{project_name}_Study_Perimeter{group_suffix}.shp",
                perimeter_offset_m=perimeter_offset_m,
                intermediate_output_dir=temp_output_dir / f"intermediate_channel_tifs{group_suffix}",
                network_csv_path=resolved_network_csv_path,
                centerline_gap_m=centerline_gap_m,
                connected_banks_output_dir=Path(self.config.get_gis_project_path(project_name)),
                junction_bank_clip_buffer_m=junction_bank_clip_buffer_m,
                junction_clip_cross_section_count=junction_clip_cross_section_count,
                junction_half_section_interpolation=junction_half_section_interpolation,
                junction_bank_structure_protection_m=junction_bank_structure_protection_m,
                skewness_correction=skewness_correction,
            )
            result["dtm_path"] = str(dtm_path)
            results.append(result)

        if len(results) == 1:
            return results[0]
        return {
            "project": project_name,
            "dtm_group_count": len(results),
            "dtm_group_results": results,
        }

    def process_structure_projects(
        self,
        projects=None,
        target_res=0.1,
        buffer_m=20.0,
        blend_type=None,
        bank_offset_m=0.2,
        full_cross_section_weight_distance_m=1.5,
        transition_to_dtm_distance_m=5.0,
        junction_tolerance=50.0,
        perimeter_offset_m=500.0,
        write_intermediate=True,
        network_csv_path=None,
        centerline_gap_m=0.5,
        junction_bank_clip_buffer_m=5.0,
        junction_clip_cross_section_count=2,
        junction_half_section_interpolation=True,
        junction_bank_structure_protection_m=1.0,
        skewness_correction=True,
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
            raise ValueError(
                f"No projects were selected from the active structure source ({self.config.structure_source}). "
                f"In folder mode, check that {Path(self.config.BUR_BUR_PATH)} contains project folders with sub-project folders."
            )

        self.preflight_project_dtms(project_subprojects)

        results = []
        for project_name, sub_project_names in project_subprojects.items():
            results.append(
                self.process_project_channels(
                    project_name=project_name,
                    sub_project_names=sub_project_names,
                    target_res=target_res,
                    buffer_m=buffer_m,
                    blend_type=blend_type,
                    bank_offset_m=bank_offset_m,
                    full_cross_section_weight_distance_m=full_cross_section_weight_distance_m,
                    transition_to_dtm_distance_m=transition_to_dtm_distance_m,
                    junction_tolerance=junction_tolerance,
                    perimeter_offset_m=perimeter_offset_m,
                    write_intermediate=write_intermediate,
                    network_csv_path=network_csv_path,
                    centerline_gap_m=centerline_gap_m,
                    junction_bank_clip_buffer_m=junction_bank_clip_buffer_m,
                    junction_clip_cross_section_count=junction_clip_cross_section_count,
                    junction_half_section_interpolation=junction_half_section_interpolation,
                    junction_bank_structure_protection_m=junction_bank_structure_protection_m,
                    skewness_correction=skewness_correction,
                )
            )

        summary_path = Path(self.config.TEMP_PATH) / "implementationDTM_summary.json"
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"\nDTM summary written to: {summary_path}")
        return results

    def get_interpolated_tif(
        self,
        target_res=0.1,
        buffer_m=20.0,
        blend_type=None,
        bank_offset_m=0.2,
        full_cross_section_weight_distance_m=1.5,
        transition_to_dtm_distance_m=5.0,
        skewness_correction=True,
    ):
        """
        Generates the interpolated DTM channel terrain for the active sub-project.
        """
        resolved_blend_type = blend_type or self.config.BLEND_TYPE
        print(f"--- STEP 1: Generating Interpolated DTM Channel Terrain ('{resolved_blend_type}' fade) ---")

        output_dir = Path(getattr(self.config, "GIS_SUB_PROJECT_PATH", self.config.OUTPUT_PATH)) / "DTM"
        output_dir.mkdir(parents=True, exist_ok=True)
        blended_tif_path = output_dir / f"terrain_blended_window_{resolved_blend_type}.tif"

        _, modifier = DTMChannelModifier.process_dtm_cells(
            dtm_path=self.config.DEM_PATH,
            cross_section_csv=self.config.CROSS_SECTION_FILE_PATH,
            bank_shp_path=self.config.BANK_LINE_FILE_PATH,
            target_res=target_res,
            buffer_m=buffer_m,
            break_after_first=False,
            blend_type=resolved_blend_type,
            return_dicts=False,
            bank_offset_m=bank_offset_m,
            full_cross_section_weight_distance_m=full_cross_section_weight_distance_m,
            transition_to_dtm_distance_m=transition_to_dtm_distance_m,
            skewness_correction=skewness_correction,
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
        centerline_shp = Path(getattr(self.config, "GIS_SUB_PROJECT_PATH", self.config.OUTPUT_PATH)) / output_filename
        centerline_shp.parent.mkdir(parents=True, exist_ok=True)
        DTMChannelModifier.export_centerline_shapefile(self.config.BANK_LINE_FILE_PATH, str(centerline_shp))
        return str(centerline_shp)

    def get_bank_lines(self, output_filename="Banks_Offset_0_2m.shp", offset_m=0.2):
        """
        Generates active sub-project bank lines with outward offset.
        """
        print(f"--- STEP 3: Generating Bank Lines (Offset: {offset_m}m) ---")
        offset_bank_shp = Path(getattr(self.config, "GIS_SUB_PROJECT_PATH", self.config.OUTPUT_PATH)) / output_filename
        offset_bank_shp.parent.mkdir(parents=True, exist_ok=True)
        DTMChannelModifier.export_offset_bank_shapefile(self.config.BANK_LINE_FILE_PATH, offset_m, str(offset_bank_shp))
        return str(offset_bank_shp)

    def get_study_perimeter(self, output_filename="Study_Perimeter.shp", offset_m=500.0):
        """
        Generates the active sub-project study perimeter polygon.
        """
        print(f"--- STEP 4: Generating Study Perimeter (Offset: {offset_m}m) ---")
        perimeter_shp = Path(getattr(self.config, "GIS_SUB_PROJECT_PATH", self.config.OUTPUT_PATH)) / output_filename
        perimeter_shp.parent.mkdir(parents=True, exist_ok=True)
        DTMChannelModifier.export_study_perimeter(
            self.config.BANK_LINE_FILE_PATH,
            str(perimeter_shp),
            offset_m,
            cross_section_csv=getattr(self.config, "CROSS_SECTION_FILE_PATH", None),
        )
        return str(perimeter_shp)

    @staticmethod
    def _normalize_name(value):
        return re.sub(r"[^0-9A-Za-z]+", "", str(value)).upper()
