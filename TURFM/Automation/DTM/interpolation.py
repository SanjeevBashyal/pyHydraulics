"""Reach and junction terrain interpolation routines."""

from __future__ import annotations

import os
from pathlib import Path
import re
import numpy as np
import pandas as pd
import rasterio
import geopandas as gpd
from rasterio.enums import Resampling
from rasterio.windows import from_bounds
from rasterio.transform import Affine
from rasterio.features import geometry_mask, rasterize
from scipy.ndimage import distance_transform_edt
from shapely.geometry import Polygon, LineString, Point, MultiLineString
from shapely.ops import linemerge, nearest_points, polygonize, split, unary_union

# Bound by channel_modifier.py after the final DTMChannelModifier facade class is created.
# The original implementation references DTMChannelModifier inside many static methods;
# keeping that symbol here preserves the existing method bodies while allowing this file
# to stay focused on one part of the workflow.
DTMChannelModifier = None


class InterpolationMixin:
    """Reach and junction terrain interpolation routines."""

    @staticmethod
    def process_dtm_cells(
        dtm_path,
        cross_section_csv,
        bank_shp_path,
        target_res=0.1,
        buffer_m=20.0,
        break_after_first=False,
        blend_type='linear',
        return_dicts=True,
        bounds=None,
        bank_offset_m=0.2,
        full_cross_section_weight_distance_m=1.5,
        transition_to_dtm_distance_m=3.5,
        skewness_correction=True,
        centerline_normal_sample_distance_m=3.0,
    ):
        """
        Iterates through every cell in the DTM, checks if it lies inside the
        bank polygon mask, and if so determines the nearest centerline point
        and the corresponding bank width at that location.

        Args:
            dtm_path: Path to the DTM raster file.
            cross_section_csv: Path to the cross-section CSV.
            bank_shp_path: Path to the bank lines shapefile, or a GeoDataFrame
                           containing the bank lines to use for this pass.
            target_res: Target resolution for resampling (m).
            buffer_m: Buffer around the survey extent (m).
            break_after_first: If True, stops after finding the first cell
                               inside the polygon (for testing).
            bounds: Optional shared raster bounds `(minx, miny, maxx, maxy)`.
                    Supplying this lets several channels be interpolated onto
                    exactly the same cropped terrain grid before merging.
            bank_offset_m: Outward bank offset used to define the in-channel
                    polygon where the interpolated bed fully applies.
            full_cross_section_weight_distance_m: Distance outside the bank
                    polygon that still uses full cross-section elevation.
            transition_to_dtm_distance_m: Additional distance over which the
                    terrain eases from cross-section elevation back to the DTM.

        Returns:
            A list of dicts with keys: row, col, x, y, dtm_z, cx, cy, bank_width
        """
        print("\\nProcessing DTM cells for centerline metrics...")

        # Setup the modifier to get all the geometry
        modifier = DTMChannelModifier()
        modifier.dtm_path = dtm_path
        modifier.csv_path = cross_section_csv
        modifier.target_res = target_res
        modifier.buffer_m = buffer_m

        modifier._read_survey_and_get_bounds()
        if bounds is not None:
            modifier.bounds = tuple(bounds)
        modifier._resample_dtm_window()
        modifier.original_dtm_data = modifier.dtm_data.copy()

        # Load banks and generate polygon mask explicitly from sequence of shapefile lines
        import geopandas as gpd
        modifier.banks_gdf = DTMChannelModifier.clean_and_merge_banklines(bank_shp_path)
        bank_lines = DTMChannelModifier._line_strings(modifier.banks_gdf)
        poly_gdf = DTMChannelModifier.create_polygon_mask_from_banks(
            modifier.banks_gdf,
            offset_m=bank_offset_m,
        )
        modifier.channel_polygon = poly_gdf.geometry.iloc[0]

        # Generate the interpolation centerline from bank lines, not from cross-section points.
        modifier.centerline_gdf = DTMChannelModifier.generate_centerline_from_banks(modifier.banks_gdf)
        modifier.centerline_source = "bank_lines"

        # Pre-process cross sections for rapid bracketing & interpolation
        import pandas as pd
        import numpy as np
        from shapely.geometry import LineString, Point
        from shapely.ops import nearest_points
        import shapely

        df = DTMChannelModifier._read_csv_auto(
            cross_section_csv,
            required_columns=("X", "Y", "Z"),
        )
        centerline = modifier.centerline_gdf.geometry.iloc[0]
        
        group_cols = [col for col in ['River', 'Reach', 'Station'] if col in df.columns]
        if not group_cols: group_cols = ['Station']

        stations_list = []
        for name, group in df.groupby(group_cols):
            coords_3d = group[['X', 'Y', 'Z']].values
            if len(coords_3d) < 2: continue
            line = LineString(coords_3d)
            stat_name = str(name if not isinstance(name, tuple) else name[-1])
            pt_C = DTMChannelModifier._cross_section_center_point_from_centerline(
                line,
                centerline,
                label=stat_name,
                bank_lines=bank_lines,
            )
            d_xs = centerline.project(pt_C)
            _, _, bw_xs = modifier.get_cell_centerline_metrics(pt_C.x, pt_C.y)
            section_profile = DTMChannelModifier._build_corrected_section_profile(
                line=line,
                centerline=centerline,
                centerline_distance=d_xs,
                center_point=pt_C,
                bank_lines=bank_lines,
                skewness_correction=skewness_correction,
                centerline_normal_sample_distance_m=centerline_normal_sample_distance_m,
            )
            positive_side_direction = DTMChannelModifier._cross_section_positive_side_direction(
                line=line,
                centerline=centerline,
                centerline_distance=d_xs,
                center_point=pt_C,
                centerline_normal_sample_distance_m=centerline_normal_sample_distance_m,
            )
            
            stations_list.append({
                "Station": stat_name, 
                "d_xs": d_xs, 
                "line": line,
                "bw_xs": bw_xs,
                "d_C_xs": section_profile["raw_center_distance"],
                "d_C_xs_corrected": section_profile["corrected_center_distance"],
                "skewness_angle_degrees": section_profile["skewness_angle_degrees"],
                "skewness_cosine": section_profile["skewness_cosine"],
                "distance_correction_cosine": section_profile["distance_correction_cosine"],
                "corrected_total_length": section_profile["corrected_total_length"],
                "positive_side_direction": positive_side_direction,
                "z_func": section_profile["z_func"],
            })
            
        stations_list.sort(key=lambda s: s["d_xs"])
        if len(stations_list) < 2:
            raise ValueError(
                f"At least two cross sections are required for DTM interpolation: {cross_section_csv}"
            )
        d_xs_array = np.array([s["d_xs"] for s in stations_list])

        # Create the polygon mask raster
        height, width = modifier.dtm_data.shape
        bank_mask = rasterize(
            [modifier.channel_polygon],
            out_shape=(height, width),
            transform=modifier.dtm_transform,
            fill=0,
            default_value=1,
            dtype="uint8",
        )

        xs_poly = DTMChannelModifier.create_cross_section_mask(
            cross_section_csv,
            modifier.banks_gdf,
            interval=1.0,
            skewness_correction=skewness_correction,
            centerline_normal_sample_distance_m=centerline_normal_sample_distance_m,
        )
        xs_mask = rasterize(
            [xs_poly],
            out_shape=(height, width),
            transform=modifier.dtm_transform,
            fill=0,
            default_value=1,
            dtype="uint8",
        )

        print(f"Iterating through {height} x {width} = {height * width} cells using vectorized arrays with {blend_type} blending...")
        valid_rows, valid_cols = np.where(xs_mask == 1)
        if len(valid_rows) == 0:
            return [], modifier

        d_bank_grid = distance_transform_edt(bank_mask == 0) * modifier.target_res
        d_bank_arr = d_bank_grid[valid_rows, valid_cols]

        hold_distance = max(float(full_cross_section_weight_distance_m), 0.0)
        transition_distance = max(float(transition_to_dtm_distance_m), 0.0)
        transition_end = hold_distance + transition_distance

        w1_terrain = np.zeros_like(d_bank_arr, dtype=float)
        w2_cs = np.ones_like(d_bank_arr, dtype=float)

        if transition_distance == 0.0:
            outside_hold_mask = d_bank_arr > hold_distance
            w1_terrain[outside_hold_mask] = 1.0
            w2_cs[outside_hold_mask] = 0.0
        else:
            transition_mask = (d_bank_arr > hold_distance) & (d_bank_arr < transition_end)
            if np.any(transition_mask):
                x = (d_bank_arr[transition_mask] - hold_distance) / transition_distance
                if blend_type == 'linear':
                    terrain_weight = x
                elif blend_type == 'exponential':
                    terrain_weight = (np.exp(x) - 1.0) / (np.e - 1.0)
                else:
                    terrain_weight = x ** 3

                w1_terrain[transition_mask] = terrain_weight
                w2_cs[transition_mask] = 1.0 - terrain_weight

            dtm_mask = d_bank_arr >= transition_end
            w1_terrain[dtm_mask] = 1.0
            w2_cs[dtm_mask] = 0.0

        xs, ys = modifier.dtm_transform * (valid_cols + 0.5, valid_rows + 0.5)
        dtm_zs = modifier.dtm_data[valid_rows, valid_cols].astype(float)
        
        cxs, cys, bws = modifier.get_cell_centerline_metrics(xs, ys)
        dists_cl = np.hypot(xs - cxs, ys - cys)
        
        cl_coords = np.array(centerline.coords)[:, :2]
        K = len(cl_coords)
        pts_c = np.column_stack((cxs, cys))
        N = len(xs)
        
        has_shapely2 = hasattr(shapely, 'line_locate_point')
        
        if has_shapely2:
            pts_shp = shapely.points(cxs, cys)
            d_cells = shapely.line_locate_point(centerline, pts_shp)
        else:
            cl_cum_dist = np.zeros(K)
            for i in range(1, K):
                cl_cum_dist[i] = cl_cum_dist[i-1] + np.hypot(cl_coords[i,0]-cl_coords[i-1,0], cl_coords[i,1]-cl_coords[i-1,1])
                
            min_dist = np.full(N, np.inf)
            best_j = np.zeros(N, dtype=int)
            best_t = np.zeros(N)
            
            for j in range(K - 1):
                A, B = cl_coords[j], cl_coords[j + 1]
                AB = B - A
                L2 = np.dot(AB, AB)
                if L2 == 0: continue
                AP = pts_c - A
                t = np.clip(np.dot(AP, AB) / L2, 0.0, 1.0)
                Proj_x = A[0] + t * AB[0]
                Proj_y = A[1] + t * AB[1]
                dist = np.hypot(pts_c[:, 0] - Proj_x, pts_c[:, 1] - Proj_y)
                mask = dist < min_dist
                min_dist[mask] = dist[mask]
                best_j[mask] = j
                best_t[mask] = t[mask]
                
            d_cells = cl_cum_dist[best_j] + best_t * np.hypot(cl_coords[best_j+1, 0] - cl_coords[best_j, 0], cl_coords[best_j+1, 1] - cl_coords[best_j, 1])

        signed_offsets, _ = DTMChannelModifier._cell_signed_offsets_and_bank_widths(
            centerline=centerline,
            bank_lines=bank_lines,
            xs=xs,
            ys=ys,
            cxs=cxs,
            cys=cys,
            centerline_distances=d_cells,
            centerline_normal_sample_distance_m=centerline_normal_sample_distance_m,
        )
        dists_cl = np.abs(signed_offsets)

        idx_dn = np.searchsorted(d_xs_array, d_cells)
        idx_dn = np.clip(idx_dn, 1, len(d_xs_array) - 1)
        idx_up = idx_dn - 1

        new_zs = np.zeros(N)
        dist_up_array = np.zeros(N)
        dist_dn_array = np.zeros(N)
        exact_cross_section_tolerance = max(float(modifier.target_res), 1e-6)
        
        has_shapely2 = hasattr(shapely, 'line_locate_point')

        for i in range(len(stations_list) - 1):
            mask = (idx_up == i)
            if not np.any(mask): continue
            
            st_up = stations_list[i]
            st_dn = stations_list[i+1]
            
            x_m = xs[mask]
            y_m = ys[mask]
            bw_m = np.maximum(bws[mask], 1e-6)
            dist_cl_m = dists_cl[mask]
            signed_offset_m = signed_offsets[mask]
            d_cell_m = d_cells[mask]
            
            if has_shapely2:
                pts_shp = shapely.points(x_m, y_m)
                dist_up_m = shapely.distance(pts_shp, st_up['line'])
                dist_dn_m = shapely.distance(pts_shp, st_dn['line'])
            else:
                dist_up_m = np.zeros(len(x_m))
                dist_dn_m = np.zeros(len(x_m))
                for k in range(len(x_m)):
                    p = Point(x_m[k], y_m[k])
                    dist_up_m[k] = p.distance(st_up['line'])
                    dist_dn_m[k] = p.distance(st_dn['line'])
            
            dist_up_array[mask] = dist_up_m
            dist_dn_array[mask] = dist_dn_m

            # Up Z
            mapped_up = dist_cl_m * (st_up['bw_xs'] / bw_m)
            dir_up = np.where(
                signed_offset_m >= 0.0,
                st_up["positive_side_direction"],
                -st_up["positive_side_direction"],
            )
            offset_up = st_up['d_C_xs_corrected'] + dir_up * mapped_up
            z_up = st_up['z_func'](offset_up)

            # Dn Z
            mapped_dn = dist_cl_m * (st_dn['bw_xs'] / bw_m)
            dir_dn = np.where(
                signed_offset_m >= 0.0,
                st_dn["positive_side_direction"],
                -st_dn["positive_side_direction"],
            )
            offset_dn = st_dn['d_C_xs_corrected'] + dir_dn * mapped_dn
            z_dn = st_dn['z_func'](offset_dn)
            
            reach_length = max(float(st_dn["d_xs"] - st_up["d_xs"]), 1e-6)
            w2 = np.clip((d_cell_m - st_up["d_xs"]) / reach_length, 0.0, 1.0)
            w1 = 1.0 - w2
            interpolated_z = w1 * z_up + w2 * z_dn

            exact_up_mask = dist_up_m <= exact_cross_section_tolerance
            exact_dn_mask = dist_dn_m <= exact_cross_section_tolerance
            if np.any(exact_up_mask) or np.any(exact_dn_mask):
                if has_shapely2:
                    raw_offset_up = shapely.line_locate_point(st_up["line"], pts_shp)
                    raw_offset_dn = shapely.line_locate_point(st_dn["line"], pts_shp)
                else:
                    raw_offset_up = np.zeros(len(x_m), dtype=float)
                    raw_offset_dn = np.zeros(len(x_m), dtype=float)
                    for k in range(len(x_m)):
                        p = Point(x_m[k], y_m[k])
                        raw_offset_up[k] = st_up["line"].project(p)
                        raw_offset_dn[k] = st_dn["line"].project(p)

                z_exact_up = st_up["z_func"](
                    raw_offset_up * st_up["distance_correction_cosine"]
                )
                z_exact_dn = st_dn["z_func"](
                    raw_offset_dn * st_dn["distance_correction_cosine"]
                )
                use_up_exact = exact_up_mask & (
                    ~exact_dn_mask | (dist_up_m <= dist_dn_m)
                )
                use_dn_exact = exact_dn_mask & (
                    ~exact_up_mask | (dist_dn_m < dist_up_m)
                )
                interpolated_z[use_up_exact] = z_exact_up[use_up_exact]
                interpolated_z[use_dn_exact] = z_exact_dn[use_dn_exact]

            new_zs[mask] = interpolated_z

        # Apply final continuous mathematical blending
        final_zs = w1_terrain * dtm_zs + w2_cs * new_zs
        outside_bank_polygon_mask = bank_mask[valid_rows, valid_cols] == 0
        terrain_preserve_mask = (
            outside_bank_polygon_mask
            & np.isfinite(dtm_zs)
            & np.isfinite(final_zs)
            & (dtm_zs > final_zs)
        )
        final_zs[terrain_preserve_mask] = dtm_zs[terrain_preserve_mask]

        if break_after_first:
            return [{
                "row": int(valid_rows[0]), "col": int(valid_cols[0]),
                "x": round(xs[0], 3), "y": round(ys[0], 3), "dtm_z": round(dtm_zs[0], 3),
                "cx": round(cxs[0], 3), "cy": round(cys[0], 3),
                "dist_to_centerline": round(dists_cl[0], 3), "bank_width": round(bws[0], 3),
                "up_station": stations_list[idx_up[0]]["Station"],
                "up_skewness_angle_deg": round(stations_list[idx_up[0]]["skewness_angle_degrees"], 3),
                "min_dist_up": round(dist_up_array[0], 3),
                "down_station": stations_list[idx_dn[0]]["Station"],
                "down_skewness_angle_deg": round(stations_list[idx_dn[0]]["skewness_angle_degrees"], 3),
                "min_dist_down": round(dist_dn_array[0], 3),
                "new_interpolated_z": round(new_zs[0], 3),
                "final_blended_z": round(final_zs[0], 3)
            }], modifier
            
        print(f"Vectorized processing completed successfully for {N} mapped cross-section grid cells.")
        
        # Natively map it into the modifier framework for ultra-fast TIF export saving seconds of dict-reading
        mod_dtm = modifier.dtm_data.copy()
        mod_dtm[valid_rows, valid_cols] = final_zs
        modifier.dtm_data = mod_dtm
        interpolation_mask = np.zeros_like(bank_mask, dtype=bool)
        active_mask = w2_cs > 0.0
        interpolation_mask[valid_rows[active_mask], valid_cols[active_mask]] = True
        modifier.interpolation_mask = interpolation_mask
        
        if not return_dicts:
            return None, modifier
            
        results = []
        for i in range(N):
            results.append({
                "row": int(valid_rows[i]), "col": int(valid_cols[i]),
                "x": round(xs[i], 3), "y": round(ys[i], 3), "dtm_z": round(dtm_zs[i], 3),
                "cx": round(cxs[i], 3), "cy": round(cys[i], 3),
                "dist_to_centerline": round(dists_cl[i], 3), "bank_width": round(bws[i], 3),
                "up_station": stations_list[idx_up[i]]["Station"],
                "up_skewness_angle_deg": round(stations_list[idx_up[i]]["skewness_angle_degrees"], 3),
                "min_dist_up": round(dist_up_array[i], 3),
                "down_station": stations_list[idx_dn[i]]["Station"],
                "down_skewness_angle_deg": round(stations_list[idx_dn[i]]["skewness_angle_degrees"], 3),
                "min_dist_down": round(dist_dn_array[i], 3),
                "new_interpolated_z": round(new_zs[i], 3),
                "final_blended_z": round(final_zs[i], 3)
            })

        return results, modifier

    @staticmethod
    def _profile_lower_envelope_z(
        z_func,
        corrected_distance,
        corrected_total_length,
        search_radius,
        sample_count=9,
    ):
        """Return the lowest surveyed profile elevation within one raster footprint.

        Cross-section CSVs can contain near-vertical wall breaks. A raster cell
        center that falls on the wall side can block the bed even when the cell
        footprint overlaps the surveyed bed. Using the lower envelope only for
        exact control-section cells keeps reach/junction connections hydraulic
        while preserving the original profile interpolation everywhere else.
        """
        scalar_input = np.isscalar(corrected_distance)
        distance_arr = np.asarray(corrected_distance, dtype=float)
        radius = max(float(search_radius), 0.0)
        total_length = max(float(corrected_total_length), 0.0)

        if radius <= 0.0 or sample_count <= 1:
            result = z_func(np.clip(distance_arr, 0.0, total_length))
            return float(result) if scalar_input else result

        offsets = np.linspace(-radius, radius, int(sample_count), dtype=float)
        sample_distances = np.clip(
            np.expand_dims(distance_arr, axis=-1) + offsets,
            0.0,
            total_length,
        )
        sample_z = np.asarray(z_func(sample_distances), dtype=float)
        with np.errstate(all="ignore"):
            result = np.nanmin(sample_z, axis=-1)

        if scalar_input:
            return float(np.asarray(result).reshape(-1)[0])
        return result

    @staticmethod
    def process_channel_network_dtm(
        dtm_path,
        channel_inputs,
        output_tif_path,
        target_res=0.1,
        buffer_m=20.0,
        blend_type="linear",
        bank_offset_m=0.2,
        full_cross_section_weight_distance_m=1.5,
        transition_to_dtm_distance_m=3.5,
        junction_tolerance=50.0,
        write_intermediate=True,
        centerline_output_path=None,
        merged_banks_output_path=None,
        bank_polygon_output_path=None,
        perimeter_output_path=None,
        perimeter_offset_m=500.0,
        intermediate_output_dir=None,
        network_csv_path=None,
        centerline_gap_m=0.5,
        connected_banks_output_dir=None,
        junction_bank_clip_buffer_m=5.0,
        junction_clip_cross_section_count=2,
        junction_half_section_interpolation=True,
        junction_bank_structure_protection_m=1.0,
        skewness_correction=True,
        centerline_normal_sample_distance_m=3.0,
        buildings_shp_path=None,
        building_lift_m=0.0,
    ):
        """
        Builds a junction-aware channel terrain for one river system.

        Each sub-project is interpolated onto the same cropped DTM window. The
        final raster overlays active reach cells on top of the original terrain
        instead of taking a minimum stack. Connected channels reserve the
        clipped-bank junction zone for a half cross-section interpolation pass.
        """
        if not channel_inputs:
            raise ValueError("At least one channel input is required.")

        output_tif_path = Path(output_tif_path)
        output_tif_path.parent.mkdir(parents=True, exist_ok=True)

        network = DTMChannelModifier.build_channel_network(
            channel_inputs=channel_inputs,
            junction_tolerance=junction_tolerance,
            network_connections=DTMChannelModifier.read_network_connections(network_csv_path),
            centerline_gap_m=centerline_gap_m,
        )
        junction_coordinates_csv_path = DTMChannelModifier.update_network_junction_coordinates(
            network_csv_path=network_csv_path,
            junctions=network["junctions"],
            dtm_path=dtm_path,
        )
        shared_bounds = DTMChannelModifier._combined_channel_bounds(
            network["channels"],
            buffer_m=buffer_m,
        )

        intermediate_dir = (
            Path(intermediate_output_dir)
            if intermediate_output_dir is not None
            else output_tif_path.parent / "intermediate_channel_tifs"
        )
        if write_intermediate:
            intermediate_dir.mkdir(parents=True, exist_ok=True)

        has_junctions = bool(network["junctions"])
        modifiers = []
        intermediate_tifs = []
        for channel in network["channels"]:
            if has_junctions:
                print(f"\nProcessing reach component outside junction zone: {channel['name']}")
            else:
                print(f"\nProcessing channel on shared DTM window: {channel['name']}")
            _, modifier = DTMChannelModifier.process_dtm_cells(
                dtm_path=dtm_path,
                cross_section_csv=channel["cross_section_csv"],
                bank_shp_path=channel["processing_banks_gdf"],
                target_res=target_res,
                buffer_m=buffer_m,
                break_after_first=False,
                blend_type=blend_type,
                return_dicts=False,
                bounds=shared_bounds,
                bank_offset_m=bank_offset_m,
                full_cross_section_weight_distance_m=full_cross_section_weight_distance_m,
                transition_to_dtm_distance_m=transition_to_dtm_distance_m,
                skewness_correction=skewness_correction,
                centerline_normal_sample_distance_m=centerline_normal_sample_distance_m,
            )
            modifiers.append(modifier)

            if write_intermediate and not has_junctions:
                channel_tif = intermediate_dir / f"{DTMChannelModifier._safe_name(channel['name'])}_channel.tif"
                DTMChannelModifier._write_modifier_geotiff(modifier, channel_tif)
                intermediate_tifs.append(str(channel_tif))

        final_modifier = modifiers[0]
        original_dtm_data = getattr(final_modifier, "original_dtm_data", final_modifier.dtm_data)
        junction_exclusion_mask = None
        if has_junctions and junction_half_section_interpolation:
            junction_exclusion_mask = DTMChannelModifier._junction_influence_mask(
                base_modifier=final_modifier,
                network=network,
                bank_offset_m=bank_offset_m,
                full_cross_section_weight_distance_m=full_cross_section_weight_distance_m,
                transition_to_dtm_distance_m=transition_to_dtm_distance_m,
                skewness_correction=skewness_correction,
                centerline_normal_sample_distance_m=centerline_normal_sample_distance_m,
            )
        final_data = DTMChannelModifier._overlay_channel_rasters(
            modifiers=modifiers,
            base_data=original_dtm_data,
            exclusion_mask=junction_exclusion_mask,
        )
        junction_interpolation_summary = []
        if has_junctions and junction_half_section_interpolation:
            final_data, junction_interpolation_summary = (
                DTMChannelModifier._apply_junction_half_section_interpolation(
                    base_modifier=final_modifier,
                    base_data=final_data,
                    original_dtm_data=original_dtm_data,
                    network=network,
                    bank_offset_m=bank_offset_m,
                    full_cross_section_weight_distance_m=full_cross_section_weight_distance_m,
                    transition_to_dtm_distance_m=transition_to_dtm_distance_m,
                    blend_type=blend_type,
                    bank_structure_protection_m=junction_bank_structure_protection_m,
                    skewness_correction=skewness_correction,
                    centerline_normal_sample_distance_m=centerline_normal_sample_distance_m,
                )
            )
        final_data, outside_bank_preserved_count = (
            DTMChannelModifier._preserve_higher_terrain_outside_network_footprint(
                data=final_data,
                original=original_dtm_data,
                network=network,
                transform=final_modifier.dtm_transform,
                bank_offset_m=bank_offset_m,
                nodata=final_modifier.dtm_meta.get("nodata") if final_modifier.dtm_meta else None,
            )
        )
        final_modifier.dtm_data = final_data
        building_lift_summary = DTMChannelModifier._apply_building_lift_to_modifier(
            modifier=final_modifier,
            buildings_shp_path=buildings_shp_path,
            lift_m=building_lift_m,
        )
        DTMChannelModifier._write_modifier_geotiff(final_modifier, output_tif_path)

        centerline_output_path = (
            Path(centerline_output_path)
            if centerline_output_path is not None
            else output_tif_path.with_name(f"{output_tif_path.stem}_centerlines.shp")
        )
        DTMChannelModifier._export_network_centerlines(
            network["channels"],
            centerline_output_path,
        )

        bank_polygon_path = None
        if bank_polygon_output_path is not None:
            bank_polygon_path = Path(bank_polygon_output_path)
            DTMChannelModifier._export_network_bank_polygons(
                network["channels"],
                bank_polygon_path,
                offset_m=bank_offset_m,
            )

        merged_banks_path = None
        if merged_banks_output_path is not None and not has_junctions:
            merged_banks_path = Path(merged_banks_output_path)
            merged_banks_path.parent.mkdir(parents=True, exist_ok=True)
            network["merged_banks_gdf"].to_file(merged_banks_path)
        elif merged_banks_output_path is not None and has_junctions:
            DTMChannelModifier._delete_vector_sidecars(merged_banks_output_path)

        perimeter_path = None
        if perimeter_output_path is not None:
            perimeter_path = Path(perimeter_output_path)
            DTMChannelModifier._export_network_perimeter(
                network["channels"],
                perimeter_path,
                offset_m=perimeter_offset_m,
                network=network,
            )

        connected_bank_products = []
        connected_banks_dir = (
            Path(connected_banks_output_dir)
            if connected_banks_output_dir is not None
            else output_tif_path.parent
        )
        if has_junctions:
            connected_bank_products = DTMChannelModifier._export_connected_bank_products(
                network=network,
                output_dir=connected_banks_dir,
                clip_buffer_m=junction_bank_clip_buffer_m,
                nearest_cross_section_count=junction_clip_cross_section_count,
            )

        return {
            "output_tif": str(output_tif_path),
            "centerline_shp": str(centerline_output_path),
            "bank_polygon_shp": str(bank_polygon_path) if bank_polygon_path else None,
            "merged_banks_shp": str(merged_banks_path) if merged_banks_path else None,
            "perimeter_shp": str(perimeter_path) if perimeter_path else None,
            "connected_bank_products": connected_bank_products,
            "junction_interpolation": junction_interpolation_summary,
            "intermediate_tifs": intermediate_tifs,
            "shared_bounds": [float(value) for value in shared_bounds],
            "blend_type": blend_type,
            "centerline_source": "bank_lines",
            "bank_offset_m": float(bank_offset_m),
            "full_cross_section_weight_distance_m": float(full_cross_section_weight_distance_m),
            "transition_to_dtm_distance_m": float(transition_to_dtm_distance_m),
            "junction_half_section_interpolation": bool(junction_half_section_interpolation),
            "junction_bank_structure_protection_m": float(junction_bank_structure_protection_m),
            "outside_bank_terrain_preserved_cells": int(outside_bank_preserved_count),
            "skewness_correction": bool(skewness_correction),
            "centerline_normal_sample_distance_m": float(centerline_normal_sample_distance_m),
            "building_lift": building_lift_summary,
            "network_csv_path": str(network_csv_path) if network_csv_path else None,
            "junction_coordinates_csv": str(junction_coordinates_csv_path) if junction_coordinates_csv_path else None,
            "dtm_path": str(dtm_path),
            "channels": [
                {
                    "name": channel["name"],
                    "cross_section_csv": str(channel["cross_section_csv"]),
                    "bank_shp_path": str(channel["bank_shp_path"]),
                    "dtm_path": str(channel.get("dtm_path", dtm_path)),
                    "centerline_source": channel.get("centerline_source", "bank_lines"),
                    "processing_centerline_source": channel.get("processing_centerline_source", "bank_lines"),
                }
                for channel in network["channels"]
            ],
            "junctions": [
                {
                    key: value
                    for key, value in junction.items()
                    if key != "extended_centerline"
                }
                for junction in network["junctions"]
            ],
        }

    @staticmethod
    def _apply_junction_half_section_interpolation(
        base_modifier,
        base_data,
        original_dtm_data,
        network,
        bank_offset_m=0.2,
        full_cross_section_weight_distance_m=1.5,
        transition_to_dtm_distance_m=3.5,
        blend_type="cubic",
        bank_structure_protection_m=1.0,
        junction_inner_bed_offset_m=0.2,
        skewness_correction=True,
        centerline_normal_sample_distance_m=3.0,
    ):
        updated = np.array(base_data, copy=True)
        original = np.asarray(original_dtm_data if original_dtm_data is not None else base_data)
        height, width = updated.shape
        transform = base_modifier.dtm_transform
        nodata = base_modifier.dtm_meta.get("nodata") if base_modifier.dtm_meta else None
        hold_distance = max(float(full_cross_section_weight_distance_m), 0.0)
        transition_distance = max(float(transition_to_dtm_distance_m), 0.0)
        summaries = []

        for junction in network.get("junctions", []):
            main = network["channels"][junction["main_index"]]
            tributary = network["channels"][junction["tributary_index"]]
            junction_point = Point(float(junction["x"]), float(junction["y"]))
            profiles = DTMChannelModifier._junction_half_cross_section_profiles(
                tributary=tributary,
                main=main,
                junction=junction,
                junction_point=junction_point,
                bank_offset_m=bank_offset_m,
                bank_structure_protection_m=bank_structure_protection_m,
                skewness_correction=skewness_correction,
                centerline_normal_sample_distance_m=centerline_normal_sample_distance_m,
            )
            clipped_banks = DTMChannelModifier._junction_bank_lines_between_cross_sections(
                tributary=tributary,
                main=main,
                junction=junction,
                junction_point=junction_point,
            )
            clipped_banks = DTMChannelModifier._join_gdf_line_features_by_proximity(
                clipped_banks,
                tolerance=1.0,
            )
            bank_lines = DTMChannelModifier._line_strings(clipped_banks)

            summary = {
                "main": main["name"],
                "tributary": tributary["name"],
                "half_profiles": len(profiles),
                "bank_lines": len(bank_lines),
                "cells_updated": 0,
                "inner_bed_profiles": 0,
                "inner_bed_offset_m": float(junction_inner_bed_offset_m),
                "inner_bed_cells_updated": 0,
                "bank_strip_cells_updated": 0,
            }
            if len(profiles) < 2 or not bank_lines:
                summaries.append(summary)
                continue

            centerlines = [
                main.get("processing_centerline") or main["centerline"],
                tributary.get("processing_centerline") or tributary["centerline"],
            ]
            channel_footprint = DTMChannelModifier._junction_channel_footprint(
                main=main,
                tributary=tributary,
                bank_offset_m=bank_offset_m,
            )
            junction_bank_polygon = DTMChannelModifier._junction_bank_polygon_from_clipped_banks(
                clipped_banks,
                bank_lines=bank_lines,
                junction_point=junction_point,
            )
            if junction_bank_polygon is None or junction_bank_polygon.is_empty:
                summaries.append(summary)
                continue
            inner_bed_offset = max(float(junction_inner_bed_offset_m), 0.0)
            inner_bed_polygon = DTMChannelModifier._junction_inner_bed_polygon(
                junction_bank_polygon=junction_bank_polygon,
                bank_lines=bank_lines,
                offset_m=inner_bed_offset,
            )
            if inner_bed_polygon is not None and not inner_bed_polygon.is_empty:
                inner_bed_polygon = inner_bed_polygon.intersection(junction_bank_polygon)
                if not inner_bed_polygon.is_valid:
                    inner_bed_polygon = inner_bed_polygon.buffer(0)
                if inner_bed_polygon.is_empty:
                    inner_bed_polygon = None

            bed_profiles = DTMChannelModifier._junction_bed_cross_section_profiles(
                profiles=profiles,
                inner_offset_m=inner_bed_offset,
            )
            summary["inner_bed_profiles"] = len(bed_profiles)
            outside_blend_extent = max(
                hold_distance + transition_distance + float(bank_offset_m),
                float(bank_offset_m),
                0.25,
            )
            outside_transition_zone = DTMChannelModifier._junction_outside_transition_zone(
                junction_bank_polygon=junction_bank_polygon,
                bank_lines=bank_lines,
                outside_blend_extent=outside_blend_extent,
                exclude_geometry=channel_footprint,
            )
            junction_zone = junction_bank_polygon
            if outside_transition_zone is not None and not outside_transition_zone.is_empty:
                junction_zone = unary_union([junction_bank_polygon, outside_transition_zone])
                if not junction_zone.is_valid:
                    junction_zone = junction_zone.buffer(0)
            if junction_zone is None or junction_zone.is_empty:
                summaries.append(summary)
                continue

            influence_mask = rasterize(
                [junction_zone],
                out_shape=(height, width),
                transform=transform,
                fill=0,
                default_value=1,
                dtype="uint8",
                all_touched=False,
            )
            rows, cols = np.where(influence_mask == 1)
            cell_size = max(abs(transform.a), abs(transform.e), 1e-6)
            for row, col in zip(rows, cols):
                terrain_z = float(original[row, col])
                current_z = float(updated[row, col])
                if not np.isfinite(terrain_z) or not np.isfinite(current_z):
                    continue
                if nodata is not None and np.isclose(terrain_z, nodata):
                    continue

                x, y = transform * (col + 0.5, row + 0.5)
                cell_point = Point(float(x), float(y))
                inside_junction_bank_polygon = bool(junction_bank_polygon.covers(cell_point))
                inside_inner_bed_polygon = (
                    inner_bed_polygon is not None
                    and bool(inner_bed_polygon.covers(cell_point))
                )

                blended_z = None
                if inside_inner_bed_polygon and bed_profiles:
                    blended_z = DTMChannelModifier._junction_inner_bed_elevation(
                        cell_point=cell_point,
                        bed_profiles=bed_profiles,
                        cell_size=cell_size,
                    )
                    if blended_z is not None:
                        summary["inner_bed_cells_updated"] += 1

                if blended_z is None:
                    candidates = DTMChannelModifier._junction_cell_elevation_candidates(
                        cell_point=cell_point,
                        bank_lines=bank_lines,
                        profiles=profiles,
                        centerlines=centerlines,
                        terrain_z=terrain_z,
                        hold_distance=hold_distance,
                        transition_distance=transition_distance,
                        blend_type=blend_type,
                        cell_size=cell_size,
                        inner_bed_offset_m=inner_bed_offset,
                        allow_inside=inside_junction_bank_polygon,
                        allow_outside=not inside_junction_bank_polygon,
                    )
                    if inside_junction_bank_polygon and not candidates:
                        # Concave junctions can make the bank-side test ambiguous near
                        # the meeting point.  Retry with all candidates so the junction
                        # center is filled instead of leaving a terrain island.
                        candidates = DTMChannelModifier._junction_cell_elevation_candidates(
                            cell_point=cell_point,
                            bank_lines=bank_lines,
                            profiles=profiles,
                            centerlines=centerlines,
                            terrain_z=terrain_z,
                            hold_distance=hold_distance,
                            transition_distance=transition_distance,
                            blend_type=blend_type,
                            cell_size=cell_size,
                            inner_bed_offset_m=inner_bed_offset,
                            allow_inside=True,
                            allow_outside=True,
                        )
                    if not candidates:
                        continue

                    inside_candidates = [candidate for candidate in candidates if candidate["inside"]]
                    if inside_junction_bank_polygon:
                        active_candidates = inside_candidates if inside_candidates else candidates
                    else:
                        active_candidates = [candidate for candidate in candidates if not candidate["inside"]]
                    if not active_candidates:
                        continue
                    weight_sum = sum(candidate["weight"] for candidate in active_candidates)
                    if weight_sum <= 0.0:
                        continue

                    blended_z = sum(
                        candidate["weight"] * candidate["z"]
                        for candidate in active_candidates
                    ) / weight_sum
                    summary["bank_strip_cells_updated"] += 1

                # Outside the clipped junction bank polygon, only blend the
                # surveyed outside-bank half sections and preserve higher terrain.
                if not inside_junction_bank_polygon and blended_z < terrain_z:
                    blended_z = terrain_z

                if not np.isfinite(blended_z):
                    continue

                updated[row, col] = float(blended_z)
                if not np.isclose(current_z, blended_z):
                    summary["cells_updated"] += 1

            control_sections = DTMChannelModifier._junction_cross_sections_for_interpolation(
                tributary=tributary,
                main=main,
                junction=junction,
                junction_point=junction_point,
                bank_offset_m=bank_offset_m,
                skewness_correction=skewness_correction,
                centerline_normal_sample_distance_m=centerline_normal_sample_distance_m,
            )
            summary["control_sections"] = [
                {
                    "role": role,
                    "channel": channel["name"],
                    "station": section.get("station"),
                }
                for _, role, channel, section in control_sections
            ]
            updated, enforced_count = DTMChannelModifier._enforce_control_cross_sections_on_raster(
                data=updated,
                sections=control_sections,
                transform=transform,
                nodata=nodata,
                cell_size=cell_size,
            )
            summary["control_section_cells_enforced"] = enforced_count

            summaries.append(summary)

        return updated, summaries

    @staticmethod
    def _enforce_control_cross_sections_on_raster(
        data,
        sections,
        transform,
        nodata=None,
        cell_size=0.1,
        enforcement_width_m=None,
        use_lower_envelope=False,
    ):
        """Write CSV-derived cell-footprint elevations along control sections.

        The reach interpolation already honors exact cross sections, but the
        later junction overlay can repaint those same cells.  Re-applying the
        controlling cross sections at the end keeps reach/junction connections
        tied to the CSV survey values while avoiding sub-cell wall blockage.
        """
        if not sections:
            return data, 0

        updated = np.asarray(data).copy()
        height, width = updated.shape
        tolerance = (
            max(float(enforcement_width_m), 1e-6)
            if enforcement_width_m is not None
            else max(float(cell_size) * 1.75, 0.15, 1e-6)
        )
        enforced = 0

        for _, _, _, section in sections:
            line = section.get("line")
            profile = section.get("profile")
            if line is None or line.is_empty or profile is None:
                continue

            left = float(profile["raw_left_bank_distance"])
            right = float(profile["raw_right_bank_distance"])
            if right < left:
                left, right = right, left

            minx, miny, maxx, maxy = line.bounds
            window = from_bounds(
                minx - tolerance,
                miny - tolerance,
                maxx + tolerance,
                maxy + tolerance,
                transform,
            )
            row_start = max(int(np.floor(window.row_off)), 0)
            row_stop = min(int(np.ceil(window.row_off + window.height)), height)
            col_start = max(int(np.floor(window.col_off)), 0)
            col_stop = min(int(np.ceil(window.col_off + window.width)), width)
            if row_start >= row_stop or col_start >= col_stop:
                continue

            for row in range(row_start, row_stop):
                for col in range(col_start, col_stop):
                    x, y = transform * (col + 0.5, row + 0.5)
                    cell_point = Point(float(x), float(y))
                    if cell_point.distance(line) > tolerance:
                        continue

                    raw_distance = float(line.project(cell_point))
                    if raw_distance < left - tolerance or raw_distance > right + tolerance:
                        continue

                    raw_distance = float(np.clip(raw_distance, left, right))
                    corrected_distance = raw_distance * float(profile["distance_correction_cosine"])
                    if use_lower_envelope:
                        exact_z = DTMChannelModifier._profile_lower_envelope_z(
                            z_func=profile["z_func"],
                            corrected_distance=corrected_distance,
                            corrected_total_length=profile["corrected_total_length"],
                            search_radius=tolerance * float(profile["distance_correction_cosine"]),
                        )
                    else:
                        exact_z = profile["z_func"](corrected_distance)
                    exact_z = float(np.asarray(exact_z).reshape(-1)[0])
                    if not np.isfinite(exact_z):
                        continue
                    if nodata is not None and np.isclose(exact_z, nodata):
                        continue

                    if not np.isclose(updated[row, col], exact_z):
                        enforced += 1
                    updated[row, col] = exact_z

        return updated, enforced

    @staticmethod
    def _junction_cell_elevation_candidates(
        cell_point,
        bank_lines,
        profiles,
        centerlines,
        terrain_z,
        hold_distance,
        transition_distance,
        blend_type="cubic",
        cell_size=0.5,
        inner_bed_offset_m=0.2,
        allow_inside=True,
        allow_outside=True,
    ):
        """Return smooth candidate elevations from clipped junction bank lines.

        The old junction mapper hard-selected the nearest bank line, which could
        create seams where bank/centerline direction changed.  This helper
        computes candidates for all usable clipped-bank lines and lets the caller
        smoothly blend the candidates.
        """

        candidates = []
        for bank_line in bank_lines:
            selected_profiles = DTMChannelModifier._profiles_for_junction_bank_line(
                profiles=profiles,
                bank_line=bank_line,
            )
            if len(selected_profiles) < 2:
                continue

            bank_measure = bank_line.project(cell_point)
            bank_point = bank_line.interpolate(bank_measure)

            profile_entries = []
            for profile in selected_profiles:
                # Use distance along the clipped bank line as the interpolation
                # coordinate.  This keeps tributary-side cells tied to the
                # tributary half-sections instead of accidentally pivoting to
                # the nearest main-channel centerline.
                profile_measure = bank_line.project(profile["bank_point"])
                profile_distance = abs(float(bank_measure) - float(profile_measure))
                profile_weight = 1.0 / max(profile_distance, float(cell_size), 1e-6)
                profile_entries.append((profile, profile_weight))

            profile_weight_sum = sum(weight for _, weight in profile_entries)
            if profile_weight_sum <= 0.0:
                continue

            center_x = sum(
                weight * profile["center_point"].x
                for profile, weight in profile_entries
            ) / profile_weight_sum
            center_y = sum(
                weight * profile["center_point"].y
                for profile, weight in profile_entries
            ) / profile_weight_sum
            center_point = Point(float(center_x), float(center_y))
            bank_to_center = bank_point.distance(center_point)

            if bank_to_center <= 1e-6:
                center_point, bank_to_center = DTMChannelModifier._nearest_centerline_point_and_distance(
                    bank_point,
                    centerlines,
                )
                if bank_to_center <= 1e-6:
                    bank_to_center = sum(
                        weight * profile["bank_to_center_distance"]
                        for profile, weight in profile_entries
                    ) / profile_weight_sum
            if bank_to_center <= 1e-6:
                continue

            vector_to_center = np.array(
                [center_point.x - bank_point.x, center_point.y - bank_point.y],
                dtype=float,
            )
            vector_to_cell = np.array(
                [cell_point.x - bank_point.x, cell_point.y - bank_point.y],
                dtype=float,
            )
            dist_from_bank = float(np.linalg.norm(vector_to_cell))
            inside_channel_side = float(np.dot(vector_to_center, vector_to_cell)) >= -1e-9
            if inside_channel_side and not allow_inside:
                continue
            if not inside_channel_side and not allow_outside:
                continue

            if inside_channel_side:
                half_fraction = min(dist_from_bank / bank_to_center, 1.0)
                blend_distance = 0.0
                if dist_from_bank > max(float(inner_bed_offset_m), 0.0):
                    continue
            else:
                half_fraction = None
                blend_distance = dist_from_bank

            terrain_weight = DTMChannelModifier._terrain_transition_weight(
                distance_from_bank=blend_distance,
                hold_distance=hold_distance,
                transition_distance=transition_distance,
                blend_type=blend_type,
            )
            if terrain_weight >= 1.0:
                continue

            weighted_z_sum = 0.0
            profile_weight_sum = 0.0
            for profile, profile_weight in profile_entries:
                if half_fraction is None:
                    profile_z = profile["z_from_outside_bank_distance"](dist_from_bank)
                else:
                    profile_z = profile["z_from_inside_bank_distance"](
                        distance_from_bank=dist_from_bank,
                        local_bank_to_center_distance=bank_to_center,
                    )
                weighted_z_sum += profile_weight * profile_z
                profile_weight_sum += profile_weight

            if profile_weight_sum <= 0.0:
                continue

            cross_section_z = weighted_z_sum / profile_weight_sum
            blended_z = terrain_weight * terrain_z + (1.0 - terrain_weight) * cross_section_z
            if not inside_channel_side and blended_z < terrain_z:
                blended_z = terrain_z
            if not np.isfinite(blended_z):
                continue

            bank_distance = max(cell_point.distance(bank_line), float(cell_size))
            candidates.append(
                {
                    "z": float(blended_z),
                    "weight": 1.0 / (bank_distance * bank_distance),
                    "inside": bool(inside_channel_side),
                }
            )

        return candidates

    @staticmethod
    def _offset_junction_bank_lines_inward(bank_lines, profiles, offset_m=0.2):
        """Offset clipped junction bank lines toward their paired half-section centers."""

        offset = max(float(offset_m), 0.0)
        if offset <= 1e-9:
            return bank_lines

        inward_lines = []
        for bank_line in bank_lines:
            if bank_line is None or bank_line.is_empty:
                continue

            selected_profiles = DTMChannelModifier._profiles_for_junction_bank_line(
                profiles=profiles,
                bank_line=bank_line,
            )
            target_points = [
                profile["center_point"]
                for profile in selected_profiles
                if profile.get("center_point") is not None
            ]
            if not target_points:
                inward_lines.append(bank_line)
                continue

            candidates = DTMChannelModifier._line_offset_candidates(bank_line, offset)
            if not candidates:
                inward_lines.append(bank_line)
                continue

            def mean_distance_to_targets(candidate_line):
                samples = [
                    candidate_line.interpolate(frac, normalized=True)
                    for frac in np.linspace(0.0, 1.0, 9)
                ]
                distances = [
                    min(sample.distance(target) for target in target_points)
                    for sample in samples
                ]
                return float(np.mean(distances))

            inward_lines.append(min(candidates, key=mean_distance_to_targets))

        return inward_lines if inward_lines else bank_lines

    @staticmethod
    def _line_offset_candidates(line, offset_m):
        """Return usable left/right offset line candidates for a LineString."""

        try:
            raw_candidates = [line.offset_curve(float(offset_m)), line.offset_curve(-float(offset_m))]
        except AttributeError:
            raw_candidates = [
                line.parallel_offset(float(offset_m), "left"),
                line.parallel_offset(float(offset_m), "right"),
            ]

        candidates = []
        for candidate in raw_candidates:
            candidates.extend(
                item
                for item in DTMChannelModifier._line_strings(candidate)
                if item is not None and not item.is_empty and item.length > 1e-9
            )
        return candidates

    @staticmethod
    def _junction_bed_cross_section_profiles(profiles, inner_offset_m=0.2):
        """Build inner-bed cross sections from points just inside each bank."""

        grouped = {}
        for profile in profiles:
            grouped.setdefault(profile.get("section_key"), {})[profile.get("side")] = profile

        bed_profiles = []
        for section_key, side_profiles in grouped.items():
            left = side_profiles.get("left")
            right = side_profiles.get("right")
            if left is None or right is None:
                continue

            left_distance = min(
                max(float(inner_offset_m), 0.0),
                max(float(left["bank_to_center_distance"]) * 0.95, 1e-6),
            )
            right_distance = min(
                max(float(inner_offset_m), 0.0),
                max(float(right["bank_to_center_distance"]) * 0.95, 1e-6),
            )
            left_point = left["point_from_inside_distance"](left_distance)
            right_point = right["point_from_inside_distance"](right_distance)
            if left_point.distance(right_point) <= 1e-6:
                continue

            left_offset = left["corrected_offset_from_inside_distance"](left_distance)
            right_offset = right["corrected_offset_from_inside_distance"](right_distance)
            bed_line = LineString(
                [
                    (left_point.x, left_point.y),
                    (right_point.x, right_point.y),
                ]
            )
            section_z_func = left["section_z_func"]

            def z_from_fraction(
                fraction,
                z_func=section_z_func,
                start_offset=left_offset,
                end_offset=right_offset,
            ):
                fraction = float(np.clip(fraction, 0.0, 1.0))
                value = z_func(start_offset + fraction * (end_offset - start_offset))
                return float(np.asarray(value).reshape(-1)[0])

            bed_profiles.append(
                {
                    "section_key": section_key,
                    "channel": left.get("channel"),
                    "station": left.get("station"),
                    "line": bed_line,
                    "z_from_fraction": z_from_fraction,
                }
            )

        return bed_profiles

    @staticmethod
    def _junction_inner_bed_polygon(junction_bank_polygon, bank_lines, offset_m=0.2):
        """Return the central junction bed area inside the inward bank offsets.

        The clipped junction polygon is the full hydraulic junction footprint.
        The inner bed is that footprint after removing only the narrow strips
        within `offset_m` of the clipped bank lines.  This follows the requested
        "0.2 m inward offset from each bank line" logic while keeping all three
        branch beds connected through one continuous central zone.
        """

        if junction_bank_polygon is None or junction_bank_polygon.is_empty:
            return None
        offset = max(float(offset_m), 0.0)
        if offset <= 1e-9 or not bank_lines:
            return junction_bank_polygon

        bank_strips = [
            line.buffer(offset, cap_style=2, join_style=2)
            for line in bank_lines
            if line is not None and not line.is_empty
        ]
        if not bank_strips:
            return junction_bank_polygon

        inner_polygon = junction_bank_polygon.difference(unary_union(bank_strips))
        if inner_polygon is None or inner_polygon.is_empty:
            inner_polygon = junction_bank_polygon.buffer(-offset, join_style=2)
        if inner_polygon is None or inner_polygon.is_empty:
            return None
        if not inner_polygon.is_valid:
            inner_polygon = inner_polygon.buffer(0)
        return inner_polygon if inner_polygon is not None and not inner_polygon.is_empty else None

    @staticmethod
    def _junction_inner_bed_elevation(cell_point, bed_profiles, cell_size=0.1):
        """Mutually interpolate the inner junction bed from all bed cross sections."""

        weighted_z = 0.0
        weight_sum = 0.0
        for profile in bed_profiles:
            line = profile["line"]
            if line is None or line.is_empty or line.length <= 1e-9:
                continue
            measure = line.project(cell_point)
            fraction = measure / line.length
            z_value = profile["z_from_fraction"](fraction)
            distance = max(cell_point.distance(line), float(cell_size), 1e-6)
            weight = 1.0 / (distance * distance)
            weighted_z += weight * z_value
            weight_sum += weight

        if weight_sum <= 0.0:
            return None
        return float(weighted_z / weight_sum)

    @staticmethod
    def _junction_bank_polygon_from_clipped_banks(
        clipped_banks,
        bank_lines=None,
        junction_point=None,
    ):
        """Create the bounded junction bed polygon from clipped bank lines.

        The clipped junction bank shapefile contains the bank segments between
        the controlling cross sections.  We close only the clipped line
        endpoints, then polygonize that closed bank network.  This keeps the
        junction bed boundary governed by the smooth clipped banklines rather
        than by cross-section or reach-footprint fallback geometry.
        """
        lines = list(bank_lines or DTMChannelModifier._line_strings(clipped_banks))
        if not lines:
            return None

        boundary_lines = [line for line in lines if line is not None and not line.is_empty]
        boundary_lines.extend(
            DTMChannelModifier._junction_clipped_bank_endpoint_connectors(boundary_lines)
        )

        polygons = []
        if boundary_lines:
            try:
                snapped = DTMChannelModifier._snap_line_endpoints(boundary_lines, tolerance=1.0)
                polygons = list(polygonize(unary_union(snapped)))
            except Exception:
                polygons = []

        valid_polygons = [item for item in polygons if item.area > 1e-9]
        if valid_polygons:
            polygon = unary_union(valid_polygons)
        else:
            polygon = DTMChannelModifier._fallback_junction_bank_polygon(lines)

        if polygon is None or polygon.is_empty:
            return None
        polygon = DTMChannelModifier._fill_polygon_holes(polygon)
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        return polygon if polygon is not None and not polygon.is_empty else None

    @staticmethod
    def _junction_clipped_bank_endpoint_connectors(lines):
        endpoints = []
        for line_index, line in enumerate(lines or []):
            if line is None or line.is_empty:
                continue
            coords = list(line.coords)
            if len(coords) < 2:
                continue
            endpoints.append((line_index, 0, Point(coords[0][:2]), coords[0]))
            endpoints.append((line_index, -1, Point(coords[-1][:2]), coords[-1]))

        connectors = []
        used = set()
        while len(used) < len(endpoints):
            best = None
            for first_index, first in enumerate(endpoints):
                if first_index in used:
                    continue
                for second_index in range(first_index + 1, len(endpoints)):
                    if second_index in used:
                        continue
                    second = endpoints[second_index]
                    if first[0] == second[0]:
                        continue
                    distance = first[2].distance(second[2])
                    if best is None or distance < best[0]:
                        best = (distance, first_index, second_index, first, second)
            if best is None:
                break

            _, first_index, second_index, first, second = best
            used.update([first_index, second_index])
            if first[2].distance(second[2]) <= 1e-6:
                continue
            connectors.append(
                LineString(
                    [
                        DTMChannelModifier._coord_like_point(first[2], first[3]),
                        DTMChannelModifier._coord_like_point(second[2], second[3]),
                    ]
                )
            )
        return connectors

    @staticmethod
    def _fallback_junction_bank_polygon(lines):
        if len(lines) < 2:
            return unary_union(lines).convex_hull.buffer(0.25)

        line1, line2 = sorted(lines, key=lambda line: line.length, reverse=True)[:2]
        coords1 = [(float(x), float(y)) for x, y, *_ in line1.coords]
        coords2 = [(float(x), float(y)) for x, y, *_ in line2.coords]
        if len(coords1) < 2 or len(coords2) < 2:
            return None

        same_orientation = (
            Point(coords1[0]).distance(Point(coords2[0]))
            + Point(coords1[-1]).distance(Point(coords2[-1]))
        )
        opposite_orientation = (
            Point(coords1[0]).distance(Point(coords2[-1]))
            + Point(coords1[-1]).distance(Point(coords2[0]))
        )
        if same_orientation > opposite_orientation:
            coords2 = coords2[::-1]

        polygon = Polygon(coords1 + coords2[::-1] + [coords1[0]])
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        return polygon

    @staticmethod
    def _fill_polygon_holes(geometry):
        if geometry is None or geometry.is_empty:
            return geometry
        if geometry.geom_type == "Polygon":
            return Polygon(geometry.exterior)
        if geometry.geom_type == "MultiPolygon":
            return unary_union([Polygon(part.exterior) for part in geometry.geoms])
        return geometry

    @staticmethod
    def _junction_outside_transition_zone(
        junction_bank_polygon,
        bank_lines,
        outside_blend_extent,
        exclude_geometry=None,
    ):
        if junction_bank_polygon is None or junction_bank_polygon.is_empty:
            return None
        extent = max(float(outside_blend_extent), 0.0)
        if extent <= 0.0 or not bank_lines:
            return None

        strip_geometries = [
            line.buffer(extent, cap_style=2, join_style=2)
            for line in bank_lines
            if line is not None and not line.is_empty
        ]
        if not strip_geometries:
            return None

        outside_zone = unary_union(strip_geometries)
        if not outside_zone.is_valid:
            outside_zone = outside_zone.buffer(0)
        outside_zone = outside_zone.difference(junction_bank_polygon)
        if exclude_geometry is not None and not exclude_geometry.is_empty:
            outside_zone = outside_zone.difference(exclude_geometry)
        # Keep the transition tied to the clipped junction polygon.  Flat line
        # caps prevent rounded bed-elevation buffers at reach connection ends.
        outside_zone = outside_zone.intersection(junction_bank_polygon.buffer(extent, cap_style=2, join_style=2))
        if not outside_zone.is_valid:
            outside_zone = outside_zone.buffer(0)
        return outside_zone if not outside_zone.is_empty else None

    @staticmethod
    def _junction_interpolation_zone_geometry(
        profiles,
        bank_lines,
        junction_point,
        pad_m=0.25,
        clip_geometry=None,
    ):
        """
        Builds a bounded junction overlay zone from clipped junction banks and
        the controlling half cross-sections. This prevents bank-buffer strips
        from painting beyond the junction while also filling the middle.
        """
        geometries = []
        for line in bank_lines or []:
            if line is not None and not line.is_empty:
                geometries.append(line)

        for profile in profiles or []:
            half_line = profile.get("half_line")
            if half_line is not None and not half_line.is_empty:
                geometries.append(half_line)
            bank_point = profile.get("bank_point")
            center_point = profile.get("center_point")
            if bank_point is not None and not bank_point.is_empty:
                geometries.append(bank_point)
            if center_point is not None and not center_point.is_empty:
                geometries.append(center_point)

        if junction_point is not None and not junction_point.is_empty:
            geometries.append(junction_point)

        if not geometries:
            return None

        zone = unary_union(geometries).convex_hull
        if zone.is_empty:
            return None
        if zone.geom_type in {"Point", "LineString", "MultiLineString"}:
            zone = zone.buffer(max(float(pad_m), 0.25))
        else:
            zone = zone.buffer(max(float(pad_m), 0.0))

        if not zone.is_valid:
            zone = zone.buffer(0)
        if clip_geometry is not None and not clip_geometry.is_empty:
            zone = zone.intersection(clip_geometry)
            if not zone.is_valid:
                zone = zone.buffer(0)
        return zone

    @staticmethod
    def _junction_channel_footprint(main, tributary, bank_offset_m=0.2):
        polygons = []
        for channel in (main, tributary):
            banks_gdf = channel.get("processing_banks_gdf")
            if banks_gdf is None:
                banks_gdf = channel.get("banks_gdf")
            if banks_gdf is None or banks_gdf.empty:
                continue
            try:
                polygon_gdf = DTMChannelModifier.create_polygon_mask_from_banks(
                    banks_gdf,
                    offset_m=bank_offset_m,
                )
            except Exception:
                polygon_gdf = DTMChannelModifier.create_polygon_mask_from_banks(
                    banks_gdf,
                    offset_m=0.0,
                )
            polygons.extend(
                geom
                for geom in polygon_gdf.geometry
                if geom is not None and not geom.is_empty
            )
        if not polygons:
            return None
        footprint = unary_union(polygons)
        if not footprint.is_valid:
            footprint = footprint.buffer(0)
        return footprint

    @staticmethod
    def _network_channel_footprint(network, bank_offset_m=0.2):
        """Build the true in-bank footprint for all reaches and junctions."""

        polygons = []
        for channel in network.get("channels", []):
            banks_gdf = channel.get("processing_banks_gdf")
            if banks_gdf is None:
                banks_gdf = channel.get("banks_gdf")
            if banks_gdf is None or banks_gdf.empty:
                continue
            try:
                polygon_gdf = DTMChannelModifier.create_polygon_mask_from_banks(
                    banks_gdf,
                    offset_m=bank_offset_m,
                )
            except Exception:
                polygon_gdf = DTMChannelModifier.create_polygon_mask_from_banks(
                    banks_gdf,
                    offset_m=0.0,
                )
            polygons.extend(
                geom
                for geom in polygon_gdf.geometry
                if geom is not None and not geom.is_empty
            )

        for junction in network.get("junctions", []):
            main = network["channels"][junction["main_index"]]
            tributary = network["channels"][junction["tributary_index"]]
            junction_point = Point(float(junction["x"]), float(junction["y"]))
            clipped_banks = DTMChannelModifier._junction_bank_lines_between_cross_sections(
                tributary=tributary,
                main=main,
                junction=junction,
                junction_point=junction_point,
            )
            clipped_banks = DTMChannelModifier._join_gdf_line_features_by_proximity(
                clipped_banks,
                tolerance=1.0,
            )
            bank_lines = DTMChannelModifier._line_strings(clipped_banks)
            junction_polygon = DTMChannelModifier._junction_bank_polygon_from_clipped_banks(
                clipped_banks,
                bank_lines=bank_lines,
                junction_point=junction_point,
            )
            if junction_polygon is not None and not junction_polygon.is_empty:
                polygons.append(junction_polygon)

        if not polygons:
            return None
        footprint = unary_union(polygons)
        if not footprint.is_valid:
            footprint = footprint.buffer(0)
        return footprint if footprint is not None and not footprint.is_empty else None

    @staticmethod
    def _preserve_higher_terrain_outside_network_footprint(
        data,
        original,
        network,
        transform,
        bank_offset_m=0.2,
        nodata=None,
    ):
        """Prevent any channel/junction interpolation from lowering outside banks."""

        footprint = DTMChannelModifier._network_channel_footprint(
            network,
            bank_offset_m=bank_offset_m,
        )
        if footprint is None or footprint.is_empty:
            return data, 0

        updated = np.asarray(data).copy()
        original_arr = np.asarray(original)
        in_bank_mask = rasterize(
            [footprint],
            out_shape=updated.shape,
            transform=transform,
            fill=0,
            default_value=1,
            dtype="uint8",
            all_touched=True,
        ).astype(bool)
        preserve_mask = (
            ~in_bank_mask
            & np.isfinite(updated)
            & np.isfinite(original_arr)
            & (original_arr > updated)
        )
        if nodata is not None:
            preserve_mask &= ~np.isclose(updated, nodata)
            preserve_mask &= ~np.isclose(original_arr, nodata)
        count = int(np.count_nonzero(preserve_mask))
        if count:
            updated[preserve_mask] = original_arr[preserve_mask]
        return updated, count

    @staticmethod
    def _overlay_channel_rasters(modifiers, base_data, exclusion_mask=None):
        if not modifiers:
            raise ValueError("No channel rasters were produced.")

        final = np.array(base_data, copy=True).astype("float32")
        for modifier in modifiers:
            mask = getattr(modifier, "interpolation_mask", None)
            if mask is None:
                mask = ~np.isclose(modifier.dtm_data, getattr(modifier, "original_dtm_data", base_data))
            else:
                mask = np.array(mask, dtype=bool, copy=True)

            if exclusion_mask is not None:
                mask &= ~exclusion_mask
            if not np.any(mask):
                continue
            final[mask] = modifier.dtm_data[mask].astype("float32")

        return final

    @staticmethod
    def _junction_influence_mask(
        base_modifier,
        network,
        bank_offset_m=0.2,
        full_cross_section_weight_distance_m=1.5,
        transition_to_dtm_distance_m=3.5,
        bank_structure_protection_m=1.0,
        skewness_correction=True,
        centerline_normal_sample_distance_m=3.0,
    ):
        height, width = base_modifier.dtm_data.shape
        mask = np.zeros((height, width), dtype=bool)
        hold_distance = max(float(full_cross_section_weight_distance_m), 0.0)
        transition_distance = max(float(transition_to_dtm_distance_m), 0.0)

        for junction in network.get("junctions", []):
            main = network["channels"][junction["main_index"]]
            tributary = network["channels"][junction["tributary_index"]]
            junction_point = Point(float(junction["x"]), float(junction["y"]))
            profiles = DTMChannelModifier._junction_half_cross_section_profiles(
                tributary=tributary,
                main=main,
                junction=junction,
                junction_point=junction_point,
                bank_offset_m=bank_offset_m,
                bank_structure_protection_m=bank_structure_protection_m,
                skewness_correction=skewness_correction,
                centerline_normal_sample_distance_m=centerline_normal_sample_distance_m,
            )
            clipped_banks = DTMChannelModifier._junction_bank_lines_between_cross_sections(
                tributary=tributary,
                main=main,
                junction=junction,
                junction_point=junction_point,
            )
            clipped_banks = DTMChannelModifier._join_gdf_line_features_by_proximity(
                clipped_banks,
                tolerance=1.0,
            )
            bank_lines = DTMChannelModifier._line_strings(clipped_banks)
            if not profiles or not bank_lines:
                continue
            centerlines = [
                main.get("processing_centerline") or main["centerline"],
                tributary.get("processing_centerline") or tributary["centerline"],
            ]
            channel_footprint = DTMChannelModifier._junction_channel_footprint(
                main=main,
                tributary=tributary,
                bank_offset_m=bank_offset_m,
            )
            junction_bank_polygon = DTMChannelModifier._junction_bank_polygon_from_clipped_banks(
                clipped_banks,
                bank_lines=bank_lines,
                junction_point=junction_point,
            )
            if junction_bank_polygon is None or junction_bank_polygon.is_empty:
                continue

            outside_blend_extent = max(
                hold_distance + transition_distance + float(bank_offset_m),
                float(bank_offset_m),
                0.25,
            )
            outside_transition_zone = DTMChannelModifier._junction_outside_transition_zone(
                junction_bank_polygon=junction_bank_polygon,
                bank_lines=bank_lines,
                outside_blend_extent=outside_blend_extent,
                exclude_geometry=channel_footprint,
            )
            junction_zone = junction_bank_polygon
            if outside_transition_zone is not None and not outside_transition_zone.is_empty:
                junction_zone = unary_union([junction_bank_polygon, outside_transition_zone])
                if not junction_zone.is_valid:
                    junction_zone = junction_zone.buffer(0)
            if junction_zone is None or junction_zone.is_empty:
                continue
            # Exclude reach rasters only where the junction pass is allowed to
            # paint: inside the clipped bank polygon plus the flat-ended outside
            # transition strips. This avoids removing reach interpolation in a
            # broad convex buffer around the tie-in.
            influence_geometry = junction_zone
            if influence_geometry.is_empty:
                continue
            mask |= rasterize(
                [influence_geometry],
                out_shape=(height, width),
                transform=base_modifier.dtm_transform,
                fill=0,
                default_value=1,
                dtype="uint8",
                all_touched=False,
            ).astype(bool)

        return mask

    @staticmethod
    def _terrain_transition_weight(distance_from_bank, hold_distance, transition_distance, blend_type="cubic"):
        if distance_from_bank <= hold_distance:
            return 0.0
        if transition_distance <= 0.0:
            return 1.0

        x = (float(distance_from_bank) - float(hold_distance)) / float(transition_distance)
        x = float(np.clip(x, 0.0, 1.0))
        if x >= 1.0:
            return 1.0
        if blend_type == "linear":
            return x
        if blend_type == "exponential":
            return float((np.exp(x) - 1.0) / (np.e - 1.0))
        return x ** 3

    @staticmethod
    def _junction_half_cross_section_profiles(
        tributary,
        main,
        junction,
        junction_point,
        bank_offset_m=0.2,
        bank_structure_protection_m=1.0,
        skewness_correction=True,
        centerline_normal_sample_distance_m=3.0,
    ):
        sections = DTMChannelModifier._junction_cross_sections_for_interpolation(
            tributary=tributary,
            main=main,
            junction=junction,
            junction_point=junction_point,
            bank_offset_m=bank_offset_m,
            skewness_correction=skewness_correction,
            centerline_normal_sample_distance_m=centerline_normal_sample_distance_m,
        )
        profiles = []
        for section_key, role, channel, section in sections:
            profiles.extend(
                DTMChannelModifier._split_cross_section_into_half_profiles(
                    section_key=section_key,
                    role=role,
                    channel=channel,
                    section=section,
                    bank_structure_protection_m=bank_structure_protection_m,
                )
            )
        return profiles

    @staticmethod
    def _junction_cross_sections_for_interpolation(
        tributary,
        main,
        junction,
        junction_point,
        bank_offset_m=0.2,
        skewness_correction=True,
        centerline_normal_sample_distance_m=3.0,
    ):
        main_centerline = main.get("processing_centerline") or main["centerline"]
        tributary_centerline = tributary.get("processing_centerline") or tributary["centerline"]
        main_banks = main.get("processing_banks_gdf", main["banks_gdf"])
        tributary_banks = tributary.get("processing_banks_gdf", tributary["banks_gdf"])

        main_bank_lines = DTMChannelModifier._offset_bank_lines_outward(
            DTMChannelModifier._line_strings(main_banks),
            centerline=main_centerline,
            offset_m=bank_offset_m,
        )
        tributary_bank_lines = DTMChannelModifier._offset_bank_lines_outward(
            DTMChannelModifier._line_strings(tributary_banks),
            centerline=tributary_centerline,
            offset_m=bank_offset_m,
        )
        main_sections = DTMChannelModifier._cross_sections_by_centerline_measure(
            cross_section_csv=main["cross_section_csv"],
            centerline=main_centerline,
            bank_lines=main_bank_lines,
            skewness_correction=skewness_correction,
            centerline_normal_sample_distance_m=centerline_normal_sample_distance_m,
        )
        tributary_sections = DTMChannelModifier._cross_sections_by_centerline_measure(
            cross_section_csv=tributary["cross_section_csv"],
            centerline=tributary_centerline,
            bank_lines=tributary_bank_lines,
            skewness_correction=skewness_correction,
            centerline_normal_sample_distance_m=centerline_normal_sample_distance_m,
        )
        if len(main_sections) < 2 or not tributary_sections:
            return []

        junction_measure = main_centerline.project(junction_point)
        upstream = [
            section for section in main_sections
            if section["centerline_measure"] <= junction_measure
        ]
        downstream = [
            section for section in main_sections
            if section["centerline_measure"] >= junction_measure
        ]
        main_up = max(upstream, key=lambda section: section["centerline_measure"], default=None)
        main_down = min(downstream, key=lambda section: section["centerline_measure"], default=None)

        if main_up is None or main_down is None or main_up is main_down:
            nearest = sorted(
                main_sections,
                key=lambda section: abs(section["centerline_measure"] - junction_measure),
            )[:2]
            if len(nearest) < 2:
                return []
            nearest.sort(key=lambda section: section["centerline_measure"])
            main_up, main_down = nearest[0], nearest[1]

        tributary_first = DTMChannelModifier._select_tributary_junction_section(
            tributary_sections,
            centerline=tributary_centerline,
            endpoint_name=junction["tributary_endpoint"],
        )

        return [
            ("main_upstream", "main_upstream", main, main_up),
            ("main_downstream", "main_downstream", main, main_down),
            ("tributary_downstream", "tributary", tributary, tributary_first),
        ]

    @staticmethod
    def _select_tributary_junction_section(sections, centerline, endpoint_name):
        """Pick the tributary cross section at the downstream junction end.

        The tributary junction reference must be directional.  If the tributary
        connects at its end point, the downstream control section is the greatest
        centerline measure; if it connects at its start point, it is the
        smallest.  This avoids selecting an upstream near-duplicate when several
        survey sections lie close to the junction.
        """

        if not sections:
            return None
        if endpoint_name == "start":
            return min(sections, key=lambda section: section["centerline_measure"])
        return max(sections, key=lambda section: section["centerline_measure"])

    @staticmethod
    def _offset_bank_lines_outward(bank_lines, centerline, offset_m=0.2):
        if not bank_lines or abs(float(offset_m)) <= 1e-9:
            return bank_lines

        offset_lines = []
        for line in bank_lines[:2]:
            if line is None or line.is_empty:
                continue
            try:
                candidates = [line.offset_curve(float(offset_m)), line.offset_curve(-float(offset_m))]
            except AttributeError:
                candidates = [
                    line.parallel_offset(float(offset_m), "left"),
                    line.parallel_offset(float(offset_m), "right"),
                ]

            candidate_lines = []
            for candidate in candidates:
                candidate_lines.extend(DTMChannelModifier._line_strings(candidate))
            if not candidate_lines:
                offset_lines.append(line)
                continue

            def mean_distance_to_centerline(candidate_line):
                samples = [
                    candidate_line.interpolate(frac, normalized=True)
                    for frac in np.linspace(0.0, 1.0, 10)
                ]
                return float(np.mean([sample.distance(centerline) for sample in samples]))

            offset_lines.append(max(candidate_lines, key=mean_distance_to_centerline))

        return offset_lines if offset_lines else bank_lines

    @staticmethod
    def _offset_junction_bank_lines_outward(bank_lines, centerlines, offset_m=0.2):
        if not bank_lines or abs(float(offset_m)) <= 1e-9:
            return bank_lines

        offset_lines = []
        for line in bank_lines:
            if line is None or line.is_empty:
                continue
            try:
                candidates = [line.offset_curve(float(offset_m)), line.offset_curve(-float(offset_m))]
            except AttributeError:
                candidates = [
                    line.parallel_offset(float(offset_m), "left"),
                    line.parallel_offset(float(offset_m), "right"),
                ]

            candidate_lines = []
            for candidate in candidates:
                candidate_lines.extend(DTMChannelModifier._line_strings(candidate))
            if not candidate_lines:
                offset_lines.append(line)
                continue

            def mean_distance_to_network(candidate_line):
                samples = [
                    candidate_line.interpolate(frac, normalized=True)
                    for frac in np.linspace(0.0, 1.0, 10)
                ]
                distances = []
                for sample in samples:
                    distances.append(min(sample.distance(centerline) for centerline in centerlines))
                return float(np.mean(distances))

            offset_lines.append(max(candidate_lines, key=mean_distance_to_network))

        return offset_lines

    @staticmethod
    def _split_cross_section_into_half_profiles(
        section_key,
        role,
        channel,
        section,
        bank_structure_protection_m=1.0,
    ):
        line = section["line"]
        coords = list(line.coords)
        if len(coords) < 2:
            return []

        profile = section["profile"]
        center_distance_raw = float(profile["raw_center_distance"])
        raw_center_point = line.interpolate(center_distance_raw)
        center_point_on_section = Point(float(raw_center_point.x), float(raw_center_point.y))
        left_bank_raw = float(profile["raw_left_bank_distance"])
        right_bank_raw = float(profile["raw_right_bank_distance"])
        raw_left_bank = line.interpolate(left_bank_raw)
        raw_right_bank = line.interpolate(right_bank_raw)
        left_bank = Point(float(raw_left_bank.x), float(raw_left_bank.y))
        right_bank = Point(float(raw_right_bank.x), float(raw_right_bank.y))
        center_corrected = float(profile["corrected_center_distance"])
        left_bank_corrected = float(profile["corrected_left_bank_distance"])
        right_bank_corrected = float(profile["corrected_right_bank_distance"])
        left_half_width = max(center_corrected - left_bank_corrected, 1e-6)
        right_half_width = max(right_bank_corrected - center_corrected, 1e-6)
        protected_width = max(float(bank_structure_protection_m), 0.0)
        # Junction half-sections preserve the near-bank/embankment shape and
        # only scale the inner bed portion toward the centerline.  This avoids
        # stretching the bank feature when local junction width differs from
        # the surveyed bank-to-center distance.
        halves = [
            {
                "side": "left",
                "bank_point": left_bank,
                "corrected_bank_distance": left_bank_corrected,
                "inside_direction": 1.0,
                "corrected_half_length": left_half_width,
                "offset_from_fraction": lambda fraction, bank=left_bank_corrected: (
                    bank + float(np.clip(fraction, 0.0, 1.0)) * left_half_width
                ),
                "outside_offset_from_distance": lambda distance, bank=left_bank_corrected: (
                    bank - max(float(distance), 0.0)
                ),
                "inside_offset_from_distance": lambda distance, local_width, bank=left_bank_corrected, half_width=left_half_width, protected=protected_width: (
                    bank + DTMChannelModifier._protected_bank_mapped_distance(
                        distance_from_bank=distance,
                        local_bank_to_center_distance=local_width,
                        section_bank_to_center_distance=half_width,
                        protected_width=protected,
                    )
                ),
            },
            {
                "side": "right",
                "bank_point": right_bank,
                "corrected_bank_distance": right_bank_corrected,
                "inside_direction": -1.0,
                "corrected_half_length": right_half_width,
                "offset_from_fraction": lambda fraction, bank=right_bank_corrected: (
                    bank - float(np.clip(fraction, 0.0, 1.0)) * right_half_width
                ),
                "outside_offset_from_distance": lambda distance, bank=right_bank_corrected: (
                    bank + max(float(distance), 0.0)
                ),
                "inside_offset_from_distance": lambda distance, local_width, bank=right_bank_corrected, half_width=right_half_width, protected=protected_width: (
                    bank - DTMChannelModifier._protected_bank_mapped_distance(
                        distance_from_bank=distance,
                        local_bank_to_center_distance=local_width,
                        section_bank_to_center_distance=half_width,
                        protected_width=protected,
                    )
                ),
            },
        ]

        profiles = []
        for half in halves:
            if half["bank_point"].distance(center_point_on_section) <= 1e-6:
                continue

            def z_from_bank_fraction(
                fraction,
                z_func=profile["z_func"],
                offset_from_fraction=half["offset_from_fraction"],
            ):
                value = z_func(offset_from_fraction(fraction))
                return float(np.asarray(value).reshape(-1)[0])

            def z_from_outside_bank_distance(
                distance,
                z_func=profile["z_func"],
                outside_offset_from_distance=half["outside_offset_from_distance"],
            ):
                value = z_func(outside_offset_from_distance(distance))
                return float(np.asarray(value).reshape(-1)[0])

            def z_from_inside_bank_distance(
                distance_from_bank,
                local_bank_to_center_distance,
                z_func=profile["z_func"],
                inside_offset_from_distance=half["inside_offset_from_distance"],
            ):
                value = z_func(
                    inside_offset_from_distance(
                        distance_from_bank,
                        local_bank_to_center_distance,
                    )
                )
                return float(np.asarray(value).reshape(-1)[0])

            def point_from_inside_distance(
                distance,
                bank_point=half["bank_point"],
                center_point=center_point_on_section,
            ):
                width = bank_point.distance(center_point)
                if width <= 1e-9:
                    return bank_point
                use_distance = float(np.clip(distance, 0.0, width * 0.95))
                fraction = use_distance / width
                return Point(
                    float(bank_point.x + (center_point.x - bank_point.x) * fraction),
                    float(bank_point.y + (center_point.y - bank_point.y) * fraction),
                )

            def corrected_offset_from_inside_distance(
                distance,
                bank=half["corrected_bank_distance"],
                direction=half["inside_direction"],
                half_width=half["corrected_half_length"],
            ):
                use_distance = float(np.clip(distance, 0.0, half_width * 0.95))
                return bank + direction * use_distance

            profiles.append(
                {
                    "section_key": section_key,
                    "role": role,
                    "channel": channel["name"],
                    "station": section["station"],
                    "side": half["side"],
                    "bank_point": half["bank_point"],
                    "center_point": center_point_on_section,
                    "half_line": LineString(
                        [
                            (half["bank_point"].x, half["bank_point"].y),
                            (center_point_on_section.x, center_point_on_section.y),
                        ]
                    ),
                    "bank_to_center_distance": half["bank_point"].distance(center_point_on_section),
                    "corrected_half_length": half["corrected_half_length"],
                    "z_from_bank_fraction": z_from_bank_fraction,
                    "z_from_inside_bank_distance": z_from_inside_bank_distance,
                    "z_from_outside_bank_distance": z_from_outside_bank_distance,
                    "bank_structure_protection_m": protected_width,
                    "section_z_func": profile["z_func"],
                    "point_from_inside_distance": point_from_inside_distance,
                    "corrected_offset_from_inside_distance": corrected_offset_from_inside_distance,
                }
            )
        return profiles

    @staticmethod
    def _profiles_for_junction_bank_line(profiles, bank_line):
        distance_cutoff = max(3.5, 0.15 * float(bank_line.length))
        near_by_section = {}
        for profile in profiles:
            if profile.get("bank_point") is None:
                continue
            distance = profile["bank_point"].distance(bank_line)
            if distance > distance_cutoff:
                continue
            section_key = profile.get("section_key")
            current = near_by_section.get(section_key)
            if current is None or distance < current[0]:
                near_by_section[section_key] = (distance, profile)
        if len(near_by_section) >= 2:
            return [
                item[1]
                for item in sorted(near_by_section.values(), key=lambda value: value[0])
            ]

        best_by_section = {}
        for profile in profiles:
            distance = profile["bank_point"].distance(bank_line)
            current = best_by_section.get(profile["section_key"])
            if current is None or distance < current[0]:
                best_by_section[profile["section_key"]] = (distance, profile)

        selected = [item[1] for item in best_by_section.values()]
        if len(selected) >= 2:
            return selected

        nearest = sorted(
            profiles,
            key=lambda profile: profile["bank_point"].distance(bank_line),
        )
        return nearest[: min(3, len(nearest))]

    @staticmethod
    def _nearest_centerline_point_and_distance(point, centerlines):
        best_point = None
        best_distance = np.inf
        for centerline in centerlines:
            projected = centerline.interpolate(centerline.project(point))
            distance = point.distance(projected)
            if distance < best_distance:
                best_distance = distance
                best_point = projected
        if best_point is None:
            return point, 0.0
        return best_point, float(best_distance)

    @staticmethod
    def _cross_sections_by_centerline_measure(
        cross_section_csv,
        centerline,
        bank_lines=None,
        skewness_correction=True,
        centerline_normal_sample_distance_m=3.0,
    ):
        df = DTMChannelModifier._read_csv_auto(
            cross_section_csv,
            required_columns=("X", "Y", "Z"),
        )
        group_cols = [column for column in ["River", "Reach", "Station"] if column in df.columns]
        if not group_cols:
            group_cols = ["Station"]

        sections = []
        for name, group in df.groupby(group_cols):
            coords = group[["X", "Y", "Z"]].values
            if len(coords) < 2:
                continue
            line = LineString(coords)
            station_name = str(name if not isinstance(name, tuple) else name[-1])
            center_point = DTMChannelModifier._cross_section_center_point_from_centerline(
                line,
                centerline,
                label=station_name,
                bank_lines=bank_lines,
            )
            centerline_measure = centerline.project(center_point)
            profile = DTMChannelModifier._build_corrected_section_profile(
                line=line,
                centerline=centerline,
                centerline_distance=centerline_measure,
                center_point=center_point,
                bank_lines=bank_lines,
                skewness_correction=skewness_correction,
                centerline_normal_sample_distance_m=centerline_normal_sample_distance_m,
            )
            sections.append(
                {
                    "station": station_name,
                    "line": line,
                    "centerline_measure": float(centerline_measure),
                    "profile": profile,
                }
            )

        sections.sort(key=lambda section: section["centerline_measure"])
        return sections
