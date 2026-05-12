"""Project-level DTM workflow controller.

The controller is intentionally thin: it resolves project paths from
`configProject.Config`, groups connected/disconnected river components, calls
`DTMChannelModifier` for interpolation, and writes HEC-RAS terrain products into
the configured `3 DTM` folder.
"""

import json
from pathlib import Path
import re

import rasterio

from .channel_modifier import DTMChannelModifier
from .terrain_hdf import prepare_building_raised_original_dtm, prepare_component_terrain_hdf


class DTM:
    """Project-level orchestration wrapper around DTMChannelModifier.

    This controller resolves configProject paths, groups connected sub-projects,
    calls the raster interpolation engine, and now prepares the HEC-RAS terrain
    HDF in the configured ``3 DTM`` folder for each processed component.

    Legacy methods still process the active config sub-project. The project-level
    helpers use configProject.Config to resolve every sub-project from the
    active structure source and run a shared-window, junction-aware DTM build.
    """

    def __init__(self, config):
        self.config = config

    def discover_project_subprojects(self):
        """Return the project/sub-project mapping discovered from `1 Bur-Bur`."""

        return self.config.discover_project_subprojects()

    def get_project_channel_inputs(self, project_name, sub_project_names=None):
        """Resolve cross-section, bank-line, and DTM inputs for one project."""

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

    def get_buildings_shp_path(self):
        preferred = Path(self.config.ESSENTIALS_PATH) / "Building" / "BuildingsAll.shp"
        if preferred.exists():
            return preferred

        candidates = sorted(Path(self.config.PROJECT_FOLDER).glob("0*Essentials*/Building/BuildingsAll.shp"))
        if candidates:
            return candidates[0]
        return preferred

    def get_projection_prj_path(self):
        """Resolve the project projection file used by HEC-RAS terrain HDFs."""

        essentials_path = Path(self.config.ESSENTIALS_PATH)
        preferred = essentials_path / "TUREF_CM30_projection.prj"
        if preferred.exists():
            return preferred

        candidates = sorted(essentials_path.glob("*projection*.prj"))
        if candidates:
            return candidates[0]

        candidates = sorted(essentials_path.glob("*.prj"))
        if candidates:
            return candidates[0]

        return preferred

    def preflight_project_dtms(self, project_subprojects):
        """Validate that every selected sub-project has a resolvable DTM."""

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

    def group_connected_channel_inputs(self, channel_inputs, network_connections):
        """Group river channels into connected components using network.csv."""

        if not channel_inputs:
            return []

        parent = list(range(len(channel_inputs)))

        def find(index):
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(left, right):
            root_left = find(left)
            root_right = find(right)
            if root_left != root_right:
                parent[root_right] = root_left

        for connection in network_connections or []:
            from_index = self._find_channel_input_index(channel_inputs, connection.get("from"))
            to_index = self._find_channel_input_index(channel_inputs, connection.get("to"))
            if from_index is None or to_index is None or from_index == to_index:
                continue
            union(from_index, to_index)

        groups_by_root = {}
        for index, channel in enumerate(channel_inputs):
            groups_by_root.setdefault(find(index), []).append(channel)

        groups = list(groups_by_root.values())
        return sorted(
            groups,
            key=lambda group: (
                0 if len(group) > 1 else 1,
                self._normalize_name(group[0].get("name", "")),
            ),
        )

    @staticmethod
    def _find_channel_input_index(channel_inputs, network_name):
        if not network_name:
            return None
        for index, channel in enumerate(channel_inputs):
            aliases = [
                channel.get("name", ""),
                Path(channel.get("cross_section_csv", "")).stem,
                Path(channel.get("bank_shp_path", "")).parent.name,
            ]
            if any(DTMChannelModifier._network_names_match(network_name, alias) for alias in aliases):
                return index
        return None

    def process_project_channels(
        self,
        project_name,
        sub_project_names=None,
        target_res=0.1,
        buffer_m=20.0,
        blend_type=None,
        bank_offset_m=0.2,
        full_cross_section_weight_distance_m=1.5,
        transition_to_dtm_distance_m=3.5,
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
        centerline_normal_sample_distance_m=3.0,
        buildings_shp_path=None,
        building_lift_m=0.0,
        split_disconnected_components=True,
    ):
        """Run interpolation and terrain-HDF preparation for one project."""

        channel_inputs = self.get_project_channel_inputs(project_name, sub_project_names)
        gis_project_dir = Path(self.config.get_gis_project_path(project_name))
        temp_project_dir = Path(self.config.get_temp_project_path(project_name))
        gis_project_dir.mkdir(parents=True, exist_ok=True)
        temp_project_dir.mkdir(parents=True, exist_ok=True)
        resolved_blend_type = blend_type or self.config.BLEND_TYPE
        resolved_network_csv_path = network_csv_path or self.get_network_csv_path()
        resolved_buildings_shp_path = buildings_shp_path or self.get_buildings_shp_path()
        network_connections = DTMChannelModifier.read_network_connections(resolved_network_csv_path)
        connected_groups = (
            self.group_connected_channel_inputs(channel_inputs, network_connections)
            if split_disconnected_components
            else [channel_inputs]
        )

        print(f"--- Processing DTM project {project_name}: {[item['name'] for item in channel_inputs]} ---")
        if len(connected_groups) > 1:
            print(
                f"Project {project_name} has {len(connected_groups)} disconnected river component(s); "
                "isolated components will be written under their sub-project GIS folders."
            )

        channel_groups: dict[str, list[dict]] = {}
        for connected_group in connected_groups:
            for channel in connected_group:
                dtm_path = str(Path(channel.get("dtm_path") or self.config.DEM_PATH))
                component_key = self._component_output_key(project_name, connected_group, len(connected_groups))
                channel_groups.setdefault(f"{dtm_path}||{component_key}", []).append(channel)

        dtm_paths = {key.split("||", 1)[0] for key in channel_groups}
        multiple_dtms = len(dtm_paths) > 1
        results = []
        if multiple_dtms:
            print(
                f"Project {project_name} uses {len(dtm_paths)} DTM rasters; "
                "processing one shared raster window per DTM group."
            )

        for group_key, grouped_channels in channel_groups.items():
            dtm_path, component_key = group_key.split("||", 1)
            output_context = self._component_output_context(
                project_name=project_name,
                grouped_channels=grouped_channels,
                component_key=component_key,
                has_multiple_groups=len(channel_groups) > 1,
            )
            gis_output_dir = output_context["gis_output_dir"]
            temp_output_dir = output_context["temp_output_dir"]
            gis_output_dir.mkdir(parents=True, exist_ok=True)
            temp_output_dir.mkdir(parents=True, exist_ok=True)

            group_suffix = ""
            if multiple_dtms and not output_context["is_isolated_subproject"]:
                group_suffix = f"_{DTMChannelModifier._safe_name(Path(dtm_path).stem)}"

            group_terrain_stem = (
                f"{output_context['stem']}_channel_terrain{group_suffix}"
                if len(grouped_channels) == 1
                else f"{output_context['stem']}_junction_channel_terrain{group_suffix}"
            )

            print(
                f"--- Processing DTM component {output_context['stem']}: "
                f"{[item['name'] for item in grouped_channels]} -> {gis_output_dir} ---"
            )
            raised_terrain = prepare_building_raised_original_dtm(
                original_dtm_path=dtm_path,
                buildings_shp_path=resolved_buildings_shp_path,
                lift_m=building_lift_m,
                dtm_root=Path(self.config.DTM_OUTPUT_PATH),
            )
            processing_dtm_path = raised_terrain.raised_tif_path
            if raised_terrain.enabled:
                print(
                    f"Using building-raised DTM for clipping/interpolation: "
                    f"{processing_dtm_path}"
                )
            result = DTMChannelModifier.process_channel_network_dtm(
                dtm_path=processing_dtm_path,
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
                centerline_output_path=gis_output_dir / f"{output_context['stem']}_Centerlines{group_suffix}.shp",
                merged_banks_output_path=gis_output_dir / f"{output_context['stem']}_Merged_Banks{group_suffix}.shp",
                bank_polygon_output_path=gis_output_dir / f"{output_context['stem']}_Bank_Polygon{group_suffix}.shp",
                perimeter_output_path=gis_output_dir / f"{output_context['stem']}_Study_Perimeter{group_suffix}.shp",
                perimeter_offset_m=perimeter_offset_m,
                intermediate_output_dir=temp_output_dir / f"intermediate_channel_tifs{group_suffix}",
                network_csv_path=resolved_network_csv_path,
                centerline_gap_m=centerline_gap_m,
                connected_banks_output_dir=gis_output_dir.parent if output_context["is_isolated_subproject"] else gis_project_dir,
                junction_bank_clip_buffer_m=junction_bank_clip_buffer_m,
                junction_clip_cross_section_count=junction_clip_cross_section_count,
                junction_half_section_interpolation=junction_half_section_interpolation,
                junction_bank_structure_protection_m=junction_bank_structure_protection_m,
                skewness_correction=skewness_correction,
                centerline_normal_sample_distance_m=centerline_normal_sample_distance_m,
                buildings_shp_path=None,
                building_lift_m=0.0,
            )
            result["source_dtm_path"] = str(dtm_path)
            result["raised_dtm_path"] = str(processing_dtm_path)
            result["dtm_path"] = str(processing_dtm_path)
            result["component"] = output_context["stem"]
            result["gis_output_dir"] = str(gis_output_dir)
            result["building_lift"] = {
                "enabled": raised_terrain.enabled,
                "buildings_shp": raised_terrain.buildings_shp,
                "lift_m": raised_terrain.lift_m,
                "cells_lifted": raised_terrain.cells_lifted,
                "raised_tif": str(raised_terrain.raised_tif_path),
                "created": raised_terrain.created,
                "message": raised_terrain.message,
            }

            # Build the HEC-RAS terrain package immediately after the channel
            # raster is prepared. HEC-RAS stitches the interpolated GeoTIFF
            # above the building-raised source DTM.
            terrain_result = prepare_component_terrain_hdf(
                original_dtm_path=processing_dtm_path,
                interpolated_tif_path=result["output_tif"],
                dtm_root=Path(self.config.DTM_OUTPUT_PATH),
                component_name=output_context["stem"],
                projection_prj_path=self.get_projection_prj_path(),
                units="Meters",
                hecras_version=self.config.HECRAS_VERSION,
            )
            result["merged_terrain_tif"] = (
                str(terrain_result.merged_tif_path)
                if terrain_result.merged_tif_path
                else None
            )
            result["hdf_base_terrain_tif"] = (
                str(terrain_result.base_terrain_tif_path)
                if terrain_result.base_terrain_tif_path
                else None
            )
            result["exact_bank_channel_tif"] = (
                str(terrain_result.exact_bank_tif_path)
                if terrain_result.exact_bank_tif_path
                else None
            )
            result["hdf_bank_channel_tif"] = (
                str(terrain_result.bank_channel_tif_path)
                if terrain_result.bank_channel_tif_path
                else None
            )
            result["hdf_interpolated_terrain_tif"] = (
                str(terrain_result.bank_channel_tif_path)
                if terrain_result.bank_channel_tif_path
                else None
            )
            result["hdf_bank_channel_mode"] = terrain_result.bank_channel_mode
            result["terrain_hdf"] = str(terrain_result.hdf_path)
            result["terrain_hdf_created"] = terrain_result.created
            result["terrain_hdf_message"] = terrain_result.message
            results.append(result)

        if len(results) == 1:
            return results[0]
        return {
            "project": project_name,
            "dtm_group_count": len(results),
            "dtm_group_results": results,
        }

    def _component_output_key(self, project_name, connected_group, connected_group_count):
        if len(connected_group) == 1 and connected_group_count > 1:
            return f"subproject:{connected_group[0]['name']}"
        if len(connected_group) == 1:
            return f"single:{connected_group[0]['name']}"
        return f"project:{project_name}"

    def _component_output_context(self, project_name, grouped_channels, component_key, has_multiple_groups):
        if component_key.startswith("subproject:"):
            sub_project_name = component_key.split(":", 1)[1]
            return {
                "stem": sub_project_name,
                "gis_output_dir": Path(self.config.get_gis_sub_project_path(project_name, sub_project_name)) / "DTM",
                "temp_output_dir": Path(self.config.get_temp_sub_project_path(project_name, sub_project_name)) / "DTM",
                "is_isolated_subproject": True,
            }

        if component_key.startswith("single:"):
            sub_project_name = component_key.split(":", 1)[1]
            stem = sub_project_name if has_multiple_groups else project_name
            gis_dir = (
                Path(self.config.get_gis_sub_project_path(project_name, sub_project_name)) / "DTM"
                if has_multiple_groups
                else Path(self.config.get_gis_project_path(project_name)) / "DTM"
            )
            temp_dir = (
                Path(self.config.get_temp_sub_project_path(project_name, sub_project_name)) / "DTM"
                if has_multiple_groups
                else Path(self.config.get_temp_project_path(project_name)) / "DTM"
            )
            return {
                "stem": stem,
                "gis_output_dir": gis_dir,
                "temp_output_dir": temp_dir,
                "is_isolated_subproject": has_multiple_groups,
            }

        return {
            "stem": project_name,
            "gis_output_dir": Path(self.config.get_gis_project_path(project_name)) / "DTM",
            "temp_output_dir": Path(self.config.get_temp_project_path(project_name)) / "DTM",
            "is_isolated_subproject": False,
        }

    def process_structure_projects(
        self,
        projects=None,
        target_res=0.1,
        buffer_m=20.0,
        blend_type=None,
        bank_offset_m=0.2,
        full_cross_section_weight_distance_m=1.5,
        transition_to_dtm_distance_m=3.5,
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
        centerline_normal_sample_distance_m=3.0,
        buildings_shp_path=None,
        building_lift_m=0.0,
        split_disconnected_components=True,
    ):
        """Run DTM processing for all selected projects in the folder structure."""

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
                    centerline_normal_sample_distance_m=centerline_normal_sample_distance_m,
                    buildings_shp_path=buildings_shp_path,
                    building_lift_m=building_lift_m,
                    split_disconnected_components=split_disconnected_components,
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
        transition_to_dtm_distance_m=3.5,
        skewness_correction=True,
        centerline_normal_sample_distance_m=3.0,
    ):
        """Legacy single-channel helper that writes one interpolated GeoTIFF."""

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
            centerline_normal_sample_distance_m=centerline_normal_sample_distance_m,
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
