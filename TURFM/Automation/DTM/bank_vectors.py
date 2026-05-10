"""Bank-line cleaning, centerline generation, masks, and vector convenience exports."""

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
from shapely.ops import linemerge, nearest_points, split, unary_union

# Bound by channel_modifier.py after the final DTMChannelModifier facade class is created.
# The original implementation references DTMChannelModifier inside many static methods;
# keeping that symbol here preserves the existing method bodies while allowing this file
# to stay focused on one part of the workflow.
DTMChannelModifier = None


class BankVectorMixin:
    """Bank-line cleaning, centerline generation, masks, and vector convenience exports."""

    @staticmethod
    def _line_strings(geometry_input):
        if isinstance(geometry_input, gpd.GeoDataFrame):
            geometries = geometry_input.geometry
        else:
            geometries = [geometry_input]

        lines = []
        for geom in geometries:
            if geom is None or geom.is_empty:
                continue
            if geom.geom_type == "LineString":
                lines.append(geom)
            elif geom.geom_type == "MultiLineString":
                lines.extend(list(geom.geoms))
        return sorted(lines, key=lambda item: item.length, reverse=True)

    @staticmethod
    def _safe_name(value):
        safe = "".join(char if char.isalnum() or char in "-_" else "_" for char in str(value))
        return safe.strip("_") or "channel"

    @staticmethod
    def clean_and_merge_banklines(banks_input, micro_tolerance=0.5, macro_tolerance=50.0, angle_tol=30.0, bridge_junctions=True):
        import geopandas as gpd
        from shapely.geometry import LineString, Point
        import numpy as np
        import math
        
        if isinstance(banks_input, (str, os.PathLike)):
            gdf = gpd.read_file(banks_input)
        else:
            gdf = banks_input.copy()
            
        lines = []
        for geom in gdf.geometry:
            if geom.geom_type == 'LineString':
                lines.append(geom)
            elif geom.geom_type == 'MultiLineString':
                lines.extend(list(geom.geoms))
                
        if len(lines) <= 2:
            return gdf

        def get_vec_at_end(coords, start_idx, end_idx):
            dx = coords[end_idx][0] - coords[start_idx][0]
            dy = coords[end_idx][1] - coords[start_idx][1]
            length = math.hypot(dx, dy)
            if length == 0: return (0, 0)
            return (dx/length, dy/length)

        def angle_between(v1, v2):
            dot = v1[0]*v2[0] + v1[1]*v2[1]
            dot = max(-1.0, min(1.0, dot))
            return math.degrees(math.acos(dot))
            
        def merge_pass(current_lines, tolerance, use_angles=False):
            while len(current_lines) > 2:
                min_dist = float('inf')
                best_pair = None
                best_mode = None
                
                for i in range(len(current_lines)):
                    for j in range(i+1, len(current_lines)):
                        c1 = list(current_lines[i].coords)
                        c2 = list(current_lines[j].coords)
                        
                        d_ss = Point(c1[0]).distance(Point(c2[0]))
                        d_se = Point(c1[0]).distance(Point(c2[-1]))
                        d_es = Point(c1[-1]).distance(Point(c2[0]))
                        d_ee = Point(c1[-1]).distance(Point(c2[-1]))
                        
                        dists = [(d_ss, 'ss'), (d_se, 'se'), (d_es, 'es'), (d_ee, 'ee')]
                        dists.sort(key=lambda x: x[0])
                        
                        if dists[0][0] < min_dist and dists[0][0] < tolerance:
                            dist, mode = dists[0]
                            valid = True
                            if use_angles and dist > 0:
                                idx1_s = 0; idx1_e = min(3, len(c1)-1)
                                idx1_s_rev = len(c1)-1; idx1_e_rev = max(0, len(c1)-4)
                                idx2_s = 0; idx2_e = min(3, len(c2)-1)
                                idx2_s_rev = len(c2)-1; idx2_e_rev = max(0, len(c2)-4)
                                
                                if mode == 'es':
                                    vec_in = get_vec_at_end(c1, max(0, len(c1)-10), len(c1)-1)
                                    vec_out = get_vec_at_end(c2, 0, min(9, len(c2)-1))
                                    a1 = angle_between(vec_in, vec_out)
                                    if a1 > angle_tol: valid = False
                                        
                                if mode == 'se':
                                    vec_in = get_vec_at_end(c2, max(0, len(c2)-10), len(c2)-1)
                                    vec_out = get_vec_at_end(c1, 0, min(9, len(c1)-1))
                                    a1 = angle_between(vec_in, vec_out)
                                    if a1 > angle_tol: valid = False
                                        
                                if mode == 'ss':
                                    vec1_out = get_vec_at_end(c1, min(9, len(c1)-1), 0)
                                    vec2_out = get_vec_at_end(c2, 0, min(9, len(c2)-1))
                                    if angle_between(vec1_out, vec2_out) > angle_tol: valid = False
                                        
                                if mode == 'ee':
                                    vec1_in = get_vec_at_end(c1, max(0, len(c1)-10), len(c1)-1)
                                    vec2_in = get_vec_at_end(c2, len(c2)-1, max(0, len(c2)-10))
                                    if angle_between(vec1_in, vec2_in) > angle_tol: valid = False
                                    
                            if valid:
                                min_dist = dist
                                best_pair = (i, j)
                                best_mode = mode

                if best_pair:
                    i, j = best_pair
                    c1 = list(current_lines[i].coords)
                    c2 = list(current_lines[j].coords)
                    
                    if best_mode == 'ss': new_coords = c1[::-1] + c2
                    elif best_mode == 'se': new_coords = c2 + c1
                    elif best_mode == 'es': new_coords = c1 + c2
                    elif best_mode == 'ee': new_coords = c1 + c2[::-1]
                        
                    merged_line = LineString(new_coords)
                    l1, l2 = current_lines[i], current_lines[j]
                    current_lines.remove(l1)
                    current_lines.remove(l2)
                    current_lines.append(merged_line)
                else:
                    break
            return current_lines
            
        lines = merge_pass(lines, micro_tolerance, use_angles=False)
        lines = merge_pass(lines, macro_tolerance, use_angles=True)

        if len(lines) > 2 and bridge_junctions:
            lines.sort(key=lambda l: l.length, reverse=True)
            main_bank = lines[0]
            fragments = lines[1:]
            
            def dist_to_main(pt):
                return Point(pt).distance(main_bank)
            
            while len(fragments) > 1:
                min_dist = float('inf')
                best_pair = None
                best_mode = None
                
                for i in range(len(fragments)):
                    for j in range(i+1, len(fragments)):
                        c1 = list(fragments[i].coords)
                        c2 = list(fragments[j].coords)
                        dists = [
                            (Point(c1[0]).distance(Point(c2[0])), 'ss'),
                            (Point(c1[0]).distance(Point(c2[-1])), 'se'),
                            (Point(c1[-1]).distance(Point(c2[0])), 'es'),
                            (Point(c1[-1]).distance(Point(c2[-1])), 'ee')
                        ]
                        dists.sort(key=lambda x: x[0])
                        if dists[0][0] < min_dist:
                            min_dist = dists[0][0]
                            best_pair = (i, j)
                            best_mode = dists[0][1]
                
                if best_pair:
                    i, j = best_pair
                    c1 = list(fragments[i].coords)
                    c2 = list(fragments[j].coords)
                    
                    sample_pts = c1[::max(1, len(c1)//20)] + c2[::max(1, len(c2)//20)]
                    med_width = np.median([dist_to_main(pt) for pt in sample_pts])
                    threshold = med_width * 1.15
                    
                    def trim_end(coords, from_end=True):
                        idx = len(coords)-1 if from_end else 0
                        step = -1 if from_end else 1
                        while 0 <= idx < len(coords) and dist_to_main(coords[idx]) > threshold:
                            idx += step
                        
                        if from_end:
                            return coords[:max(2, idx+1)]
                        else:
                            return coords[min(len(coords)-2, idx):]

                    if best_mode == 'es':
                        coords1 = trim_end(c1, True)
                        coords2 = trim_end(c2, False)
                        p1, p2 = coords1[-1], coords2[0]
                    elif best_mode == 'se':
                        coords1 = trim_end(c2, True)
                        coords2 = trim_end(c1, False)
                        p1, p2 = coords1[-1], coords2[0]
                    elif best_mode == 'ss':
                        coords1 = trim_end(c1, False)[::-1]
                        coords2 = trim_end(c2, False)
                        p1, p2 = coords1[-1], coords2[0]
                    elif best_mode == 'ee':
                        coords1 = trim_end(c1, True)
                        coords2 = trim_end(c2, True)[::-1]
                        p1, p2 = coords1[-1], coords2[0]
                        
                    def generate_bridge(pt1, pt2, n_points=15):
                        pd1 = main_bank.project(Point(pt1))
                        pd2 = main_bank.project(Point(pt2))
                        w1 = Point(pt1).distance(main_bank)
                        w2 = Point(pt2).distance(main_bank)
                        
                        if abs(pd2 - pd1) < 1e-3: return []
                        dists = np.linspace(pd1, pd2, n_points + 2)[1:-1]
                        
                        def get_normal(d):
                            P_next = main_bank.interpolate(min(d + 0.5, main_bank.length))
                            P_prev = main_bank.interpolate(max(d - 0.5, 0.0))
                            dx, dy = P_next.x - P_prev.x, P_next.y - P_prev.y
                            L = math.hypot(dx, dy)
                            if L == 0: return 0, 0
                            return -dy/L, dx/L
                        
                        pb1 = main_bank.interpolate(pd1)
                        nx1, ny1 = get_normal(pd1)
                        tp1 = Point(pb1.x + w1 * nx1, pb1.y + w1 * ny1)
                        tp2 = Point(pb1.x - w1 * nx1, pb1.y - w1 * ny1)
                        sign = 1 if tp1.distance(Point(pt1)) < tp2.distance(Point(pt1)) else -1
                        
                        bridge = []
                        for i, d in enumerate(dists):
                            w = w1 + (w2 - w1) * (i + 1) / (n_points + 1)
                            pb = main_bank.interpolate(d)
                            nx, ny = get_normal(d)
                            bridge.append((pb.x + sign * w * nx, pb.y + sign * w * ny))
                        return bridge
                        
                    new_coords = coords1 + generate_bridge(p1, p2) + coords2
                        
                    merged_frag = LineString(new_coords)
                    f1 = fragments[i]
                    f2 = fragments[j]
                    fragments.remove(f1)
                    fragments.remove(f2)
                    fragments.append(merged_frag)
                    
            lines = [main_bank, fragments[0]]
            
        elif len(lines) > 2 and not bridge_junctions:
            # Sort by length for predictable consistent ordering, but keep all fragments
            lines.sort(key=lambda l: l.length, reverse=True)

        out_gdf = gpd.GeoDataFrame(geometry=lines, crs=gdf.crs)
        return out_gdf

    @staticmethod
    def create_cross_section_mask(
        cross_section_csv: str,
        bank_shp_path: str,
        interval: float = 1.0,
        skewness_correction=True,
        centerline_normal_sample_distance_m=3.0,
    ):
        """
        Creates a custom polygon mask by walking the centerline at 'interval' meters and interpolating 
        the left and right surveyed cross-section widths. 
        """
        print(f"\nGenerating dynamic cross section bounds polygon along centerline at {interval}m intervals...")
        import pandas as pd
        import numpy as np
        import geopandas as gpd
        from shapely.geometry import LineString, Polygon
        from shapely.ops import nearest_points

        df = DTMChannelModifier._read_csv_auto(
            cross_section_csv,
            required_columns=("X", "Y", "Z"),
        )
        banks_gdf = DTMChannelModifier.clean_and_merge_banklines(bank_shp_path)
        bank_lines = DTMChannelModifier._line_strings(banks_gdf)
        centerline_gdf = DTMChannelModifier.generate_centerline_from_banks(banks_gdf)
        if centerline_gdf.empty:
            raise ValueError("Failed to generate centerline from banks.")
        centerline = centerline_gdf.geometry.iloc[0]

        group_cols = [col for col in ['River', 'Reach', 'Station'] if col in df.columns]
        if not group_cols: group_cols = ['Station']

        stations = []
        for name, group in df.groupby(group_cols):
            coords_3d = group[['X', 'Y', 'Z']].values
            if len(coords_3d) < 2: continue
            line = LineString(coords_3d)
            
            station_name = str(name if not isinstance(name, tuple) else name[-1])
            pt_C = DTMChannelModifier._cross_section_center_point_from_centerline(
                line,
                centerline,
                label=station_name,
                bank_lines=bank_lines,
            )
            d_xs = centerline.project(pt_C)
            section_profile = DTMChannelModifier._build_corrected_section_profile(
                line=line,
                centerline=centerline,
                centerline_distance=d_xs,
                center_point=pt_C,
                bank_lines=bank_lines,
                skewness_correction=skewness_correction,
                centerline_normal_sample_distance_m=centerline_normal_sample_distance_m,
            )
            left_width = section_profile['corrected_left_width']
            right_width = section_profile['corrected_right_width']
            
            stations.append({'d_xs': d_xs, 'lw': left_width, 'rw': right_width})
            
        stations.sort(key=lambda x: x['d_xs'])
        d_xs_arr = np.array([s['d_xs'] for s in stations])
        lw_arr = np.array([s['lw'] for s in stations])
        rw_arr = np.array([s['rw'] for s in stations])

        cl_length = centerline.length
        distances = np.arange(0, cl_length, interval)
        if len(distances) == 0 or distances[-1] != cl_length:
            distances = np.append(distances, cl_length)

        left_pts = []
        right_pts = []
        for d in distances:
            lw = np.interp(d, d_xs_arr, lw_arr)
            rw = np.interp(d, d_xs_arr, rw_arr)
            pt = centerline.interpolate(d)
            
            tangent = DTMChannelModifier._centerline_unit_tangent(
                centerline,
                d,
                sample_distance=centerline_normal_sample_distance_m,
            )
            nx = -tangent[1]
            ny = tangent[0]
                
            left_pts.append((pt.x + nx * lw, pt.y + ny * lw))
            right_pts.append((pt.x - nx * rw, pt.y - ny * rw))
            
        poly_pts = left_pts + right_pts[::-1] + [left_pts[0]]
        poly = Polygon(poly_pts)
        if not poly.is_valid:
            # Buffer by 0 resolves boundary self-intersections (bow-ties) cleanly
            poly = poly.buffer(0)
            
        # In sharp inside-bends, the dissolved bowtie creates a MultiPolygon.
        # We extract the primary continuous polygon (largest area) for pure boundary tracing.
        if poly.geom_type == 'MultiPolygon':
            poly = max(poly.geoms, key=lambda a: a.area)
            
        return poly

    @staticmethod
    def interpolate_cross_sections(cross_section_csv: str, bank_shp_path: str, step_m: float = 0.1, out_csv: str = None):
        """
        Reads cross-section points from a CSV, generates the bank centerline from the provided shapefile,
        intersects them, and interpolates X, Y, Z at exactly `step_m` intervals outwards from the center.
        """
        print(f"\nInterpolating cross sections every {step_m}m from centerline...")
        
        df = DTMChannelModifier._read_csv_auto(
            cross_section_csv,
            required_columns=("X", "Y", "Z"),
        )
        
        centerline_gdf = DTMChannelModifier.generate_centerline_from_banks(bank_shp_path)
        if centerline_gdf.empty:
            raise ValueError("Failed to generate centerline from banks.")
        centerline = centerline_gdf.geometry.iloc[0]
        
        results = []
        
        group_cols = [col for col in ['River', 'Reach', 'Station'] if col in df.columns]
        if not group_cols:
            group_cols = ['Station'] if 'Station' in df.columns else []

        grouped = df.groupby(group_cols) if group_cols else [(None, df)]
            
        for name, group in grouped:
            coords_3d = group[['X', 'Y', 'Z']].values
            
            if len(coords_3d) < 2:
                continue
            
            xs_line = LineString(coords_3d)
            intersection = xs_line.intersection(centerline)
            
            if intersection.is_empty:
                print(f"No intersection found for cross section {name}")
                continue
                
            if intersection.geom_type in ['MultiPoint', 'GeometryCollection']:
                pts = [geom for geom in getattr(intersection, 'geoms', [intersection]) if geom.geom_type == 'Point']
                if not pts:
                    print(f"No valid point intersection found for cross section {name}")
                    continue
                intersection = pts[0]
            elif intersection.geom_type != 'Point':
                print(f"Invalid intersection type {intersection.geom_type} for cross section {name}")
                continue
                
            center_dist = xs_line.project(intersection)
            
            dists_left = np.arange(center_dist, 0, -step_m)[1:]
            dists_right = np.arange(center_dist, xs_line.length + 1e-5, step_m)
            all_dists = np.concatenate((dists_left[::-1], dists_right))
            
            seg_lengths = [np.hypot(coords_3d[i+1][0] - coords_3d[i][0], coords_3d[i+1][1] - coords_3d[i][1]) for i in range(len(coords_3d)-1)]
            cum_dist = np.insert(np.cumsum(seg_lengths), 0, 0)
            
            for d in all_dists:
                if d <= 0:
                    pt = coords_3d[0]
                elif d >= cum_dist[-1]:
                    pt = coords_3d[-1]
                else:
                    idx = np.searchsorted(cum_dist, d) - 1
                    idx = max(0, min(idx, len(seg_lengths) - 1))
                    
                    if seg_lengths[idx] == 0:
                        seg_frac = 0
                    else:
                        seg_frac = (d - cum_dist[idx]) / seg_lengths[idx]
                    
                    p1 = coords_3d[idx]
                    p2 = coords_3d[idx+1]
                    pt = (
                        p1[0] + (p2[0] - p1[0]) * seg_frac,
                        p1[1] + (p2[1] - p1[1]) * seg_frac,
                        p1[2] + (p2[2] - p1[2]) * seg_frac
                    )
                
                row_dict = {}
                if group_cols:
                    name_tuple = name if isinstance(name, tuple) else (name,)
                    for idx_c, c in enumerate(group_cols):
                        row_dict[c] = name_tuple[idx_c]
                
                row_dict["Distance_from_Center"] = round(d - center_dist, 3)
                row_dict["X"] = round(pt[0], 3)
                row_dict["Y"] = round(pt[1], 3)
                row_dict["Z"] = round(pt[2], 3)
                results.append(row_dict)
                
        out_df = pd.DataFrame(results)
        
        if out_csv:
            out_df.to_csv(out_csv, index=False)
            print(f"Interpolated cross sections successfully saved to: {out_csv}")
            
        return out_df

    @staticmethod
    def export_centerline_shapefile(bank_shp_path: str, out_shp_path: str):
        """Generates the river centerline from bank shapefiles and exports it to a shapefile."""
        print(f"\nExporting centerline shapefile to: {out_shp_path}")
        gdf = DTMChannelModifier.generate_centerline_from_banks(bank_shp_path)
        
        bank_gdf = DTMChannelModifier.clean_and_merge_banklines(bank_shp_path)
        if hasattr(gdf, "crs") and gdf.crs is None:
            gdf.set_crs(bank_gdf.crs, inplace=True)
            
        gdf.to_file(out_shp_path)

    @staticmethod
    def export_offset_bank_shapefile(bank_shp_path: str, offset_m: float, out_shp_path: str):
        """
        Reads bank shapefile, identifies all separate banks, and offsets them outward
        by offset_m distance, exporting the modified lines as a new Shapefile.
        """
        import numpy as np
        import geopandas as gpd
        
        print(f"Exporting outward offset bank shapefile ({offset_m}m) to: {out_shp_path}")
        # Keep junction gaps and hooks for the final output shapefile
        bank_gdf = DTMChannelModifier.clean_and_merge_banklines(bank_shp_path, bridge_junctions=False)
        
        lines = []
        for geom in bank_gdf.geometry:
            if geom.geom_type == 'LineString': lines.append(geom)
            elif geom.geom_type == 'MultiLineString': lines.extend(geom.geoms)
            
        if len(lines) < 2:
            raise ValueError("Bank shapefile must contain at least two LineStrings.")
            
        # Get centerline to reliably check which offset direction is "outwards"
        centerline_gdf = DTMChannelModifier.generate_centerline_from_banks(bank_shp_path)
        centerline = centerline_gdf.geometry.iloc[0]
        
        final_lines = []
        for line in lines:
            try:
                o1 = line.offset_curve(offset_m)
                o2 = line.offset_curve(-offset_m)
            except AttributeError:
                o1 = line.parallel_offset(offset_m, 'left')
                o2 = line.parallel_offset(offset_m, 'right')
                
            pts1 = [o1.interpolate(frac, normalized=True) for frac in np.linspace(0, 1, 10)]
            pts2 = [o2.interpolate(frac, normalized=True) for frac in np.linspace(0, 1, 10)]
            
            d1 = np.mean([pt.distance(centerline) for pt in pts1])
            d2 = np.mean([pt.distance(centerline) for pt in pts2])
            
            final_lines.append(o1 if d1 > d2 else o2)
            
        out_gdf = gpd.GeoDataFrame(geometry=final_lines, crs=bank_gdf.crs)
        out_gdf.to_file(out_shp_path)

    @staticmethod
    def export_cross_section_shapefile(cross_section_csv: str, bank_shp_path: str, step_m: float, out_shp_path: str):
        """
        Runs cross section interpolation and exports the generated cross sections directly to a 3D Shapefile.
        """
        print(f"Exporting cross-section shapefile to: {out_shp_path}")
        df = DTMChannelModifier.interpolate_cross_sections(cross_section_csv, bank_shp_path, step_m)
        
        group_cols = [col for col in ['River', 'Reach', 'Station'] if col in df.columns]
        if not group_cols:
            group_cols = ['Station'] if 'Station' in df.columns else []

        grouped = df.groupby(group_cols) if group_cols else [(None, df)]
        
        lines = []
        names = []
        for name, group in grouped:
            coords = group[['X', 'Y', 'Z']].values
            if len(coords) < 2: continue
            lines.append(LineString(coords))
            names.append(str(name[-1]) if isinstance(name, tuple) else str(name))
            
        bank_gdf = DTMChannelModifier.clean_and_merge_banklines(bank_shp_path)
        out_gdf = gpd.GeoDataFrame({'Station': names}, geometry=lines, crs=bank_gdf.crs)
        out_gdf.to_file(out_shp_path)

    @staticmethod
    def calculate_bank_widths(cross_section_csv: str, bank_shp_path: str, out_csv: str = None):
        """
        For each cross section, calculates the bank-to-bank width: the length of the
        cross-section line segment that lies between the left and right bank lines.
        """
        print("\nCalculating cross-section widths between banks...")

        df = DTMChannelModifier._read_csv_auto(
            cross_section_csv,
            required_columns=("X", "Y", "Z"),
        )
        banks = DTMChannelModifier.clean_and_merge_banklines(bank_shp_path)

        lines = []
        for geom in banks.geometry:
            if geom.geom_type == 'LineString': lines.append(geom)
            elif geom.geom_type == 'MultiLineString': lines.extend(geom.geoms)
        if len(lines) < 2:
            raise ValueError("Bank shapefile must contain at least two LineStrings.")
        left_bank, right_bank = lines[0], lines[1]

        group_cols = [col for col in ['River', 'Reach', 'Station'] if col in df.columns]
        if not group_cols:
            group_cols = ['Station'] if 'Station' in df.columns else []

        grouped = df.sort_values(group_cols).groupby(group_cols) if group_cols else [(None, df)]
        results = []

        for name, group in grouped:
            coords_3d = group[['X', 'Y', 'Z']].values
            if len(coords_3d) < 2:
                continue

            xs_line = LineString(coords_3d)

            # Find the nearest point on each bank to the cross-section
            pt_L, _ = nearest_points(left_bank, xs_line)
            pt_R, _ = nearest_points(right_bank, xs_line)

            # Project those bank points onto the cross-section to get 1-D distances
            d_L = xs_line.project(pt_L)
            d_R = xs_line.project(pt_R)

            bank_width = abs(d_R - d_L)

            row_dict = {}
            if group_cols:
                name_tuple = name if isinstance(name, tuple) else (name,)
                for idx_c, c in enumerate(group_cols):
                    row_dict[c] = name_tuple[idx_c]
            else:
                row_dict["Station"] = "All"

            row_dict["Bank_Width"] = round(bank_width, 3)
            row_dict["Left_Bank_X"] = round(pt_L.x, 3)
            row_dict["Left_Bank_Y"] = round(pt_L.y, 3)
            row_dict["Right_Bank_X"] = round(pt_R.x, 3)
            row_dict["Right_Bank_Y"] = round(pt_R.y, 3)
            results.append(row_dict)

        out_df = pd.DataFrame(results)
        if out_csv:
            out_df.to_csv(out_csv, index=False)
            print(f"Bank widths saved to: {out_csv}")
        return out_df

    @staticmethod
    def calculate_reach_lengths(cross_section_csv: str, bank_shp_path: str, out_csv: str = None):
        """
        Calculates the downstream reach lengths for Left Bank, Center, and Right Bank 
        between successive cross sections based on path length along their shapefiles.
        """
        print("\nCalculating bank reach lengths between cross sections...")
        
        df = DTMChannelModifier._read_csv_auto(
            cross_section_csv,
            required_columns=("X", "Y"),
        )
        banks = DTMChannelModifier.clean_and_merge_banklines(bank_shp_path)
        
        centerline_gdf = DTMChannelModifier.generate_centerline_from_banks(banks)
        if centerline_gdf.empty:
            raise ValueError("Failed to generate centerline from banks.")
        centerline = centerline_gdf.geometry.iloc[0]
        
        lines = []
        for geom in banks.geometry:
            if geom.geom_type == 'LineString': lines.append(geom)
            elif geom.geom_type == 'MultiLineString': lines.extend(geom.geoms)
            
        if len(lines) < 2:
            raise ValueError("Bank shapefile must contain at least two valid LineStrings.")
            
        left_bank, right_bank = lines[0], lines[1]
        
        group_cols = [col for col in ['River', 'Reach', 'Station'] if col in df.columns]
        if not group_cols:
            group_cols = ['Station'] if 'Station' in df.columns else []

        grouped = df.sort_values(group_cols).groupby(group_cols) if group_cols else [(None, df)]
        
        results = []
        
        for name, group in grouped:
            coords_3d = group[['X', 'Y', 'Z']].values
            if len(coords_3d) < 2:
                continue
                
            xs_line = LineString(coords_3d)
            
            pt_L, _ = nearest_points(left_bank, xs_line)
            pt_C, _ = nearest_points(centerline, xs_line)
            pt_R, _ = nearest_points(right_bank, xs_line)
            
            dist_L = left_bank.project(pt_L)
            dist_C = centerline.project(pt_C)
            dist_R = right_bank.project(pt_R)
            
            row_dict = {}
            if group_cols:
                name_tuple = name if isinstance(name, tuple) else (name,)
                for idx_c, c in enumerate(group_cols):
                    row_dict[c] = name_tuple[idx_c]
            else:
                row_dict["Station"] = "All"
                
            row_dict["L_CurveDist"] = dist_L
            row_dict["C_CurveDist"] = dist_C
            row_dict["R_CurveDist"] = dist_R
            results.append(row_dict)
            
        if not results:
            return pd.DataFrame()
            
        res_df = pd.DataFrame(results)
        
        res_df["Left_Bank_Length"] = abs(res_df["L_CurveDist"].diff(-1))
        res_df["Center_Length"] = abs(res_df["C_CurveDist"].diff(-1))
        res_df["Right_Bank_Length"] = abs(res_df["R_CurveDist"].diff(-1))
        
        res_df.fillna(0, inplace=True)
        
        res_df.drop(columns=["L_CurveDist", "C_CurveDist", "R_CurveDist"], inplace=True)
        
        for col in ["Left_Bank_Length", "Center_Length", "Right_Bank_Length"]:
            res_df[col] = res_df[col].round(2)
            
        if out_csv:
            res_df.to_csv(out_csv, index=False)
            print(f"Reach lengths successfully saved to: {out_csv}")
            
        return res_df

    @staticmethod
    def generate_centerline_from_banks(
        banks_input, output_shp_path: str = None, step_m: float = 1.0
    ):
        if isinstance(banks_input, (str, os.PathLike)):
            print(
                f"\nGenerating mathematically equidistant centerline from: {banks_input}..."
            )
            banks_gdf = DTMChannelModifier.clean_and_merge_banklines(banks_input)
        else:
            print(
                "\nGenerating mathematically equidistant centerline from provided GeoDataFrame..."
            )
            banks_gdf = banks_input

        lines = [geom for geom in banks_gdf.geometry if geom.geom_type == "LineString"]
        for geom in banks_gdf.geometry:
            if geom.geom_type == "MultiLineString":
                lines.extend(geom.geoms)

        if len(lines) < 2:
            raise ValueError("The shapefile must contain at least two line geometries.")

        line1, line2 = lines[0], lines[1]

        proj_start, proj_end = line1.project(Point(line2.coords[0])), line1.project(
            Point(line2.coords[-1])
        )
        start_dist, end_dist = max(min(proj_start, proj_end), 0), min(
            max(proj_start, proj_end), line1.length
        )

        working_length = end_dist - start_dist
        if working_length <= 0:
            start_dist, end_dist, working_length = 0.0, line1.length, line1.length

        num_points = max(int(working_length / step_m), 2)
        center_coords = []

        for i in range(num_points + 1):
            p_a = line1.interpolate(start_dist + (i / num_points) * working_length)
            _, p_b = nearest_points(p_a, line2)

            t_low, t_high, t_mid = 0.0, 1.0, 0.5
            for _ in range(40):
                t_mid = (t_low + t_high) / 2.0
                p_mid = Point(
                    p_a.x + t_mid * (p_b.x - p_a.x), p_a.y + t_mid * (p_b.y - p_a.y)
                )

                diff = line1.distance(p_mid) - line2.distance(p_mid)
                if abs(diff) < 1e-4:
                    break
                if diff < 0:
                    t_low = t_mid
                else:
                    t_high = t_mid

            p_eq = Point(
                p_a.x + t_mid * (p_b.x - p_a.x), p_a.y + t_mid * (p_b.y - p_a.y)
            )

            if line1.has_z and line2.has_z:
                z_avg = (
                    line1.interpolate(line1.project(p_eq)).z
                    + line2.interpolate(line2.project(p_eq)).z
                ) / 2.0
                center_coords.append((p_eq.x, p_eq.y, z_avg))
            else:
                center_coords.append((p_eq.x, p_eq.y))

        filtered_coords = [center_coords[0]]
        for coord in center_coords[1:]:
            if coord != filtered_coords[-1]:
                filtered_coords.append(coord)

        center_gdf = gpd.GeoDataFrame(
            [
                {
                    "Name": "Equidistant Centerline",
                    "geometry": LineString(filtered_coords),
                }
            ],
            crs=banks_gdf.crs,
        )
        if output_shp_path:
            center_gdf.to_file(output_shp_path)
            print(f"Equidistant centerline successfully saved to: {output_shp_path}")
        return center_gdf

    @staticmethod
    def _get_outward_offset_line(target_line, reference_line, dist):
        """Helper to find the correct 'outward' offset line while preserving 3D."""
        off_left = DTMChannelModifier._single_offset_line(
            target_line.parallel_offset(dist, "left")
        )
        off_right = DTMChannelModifier._single_offset_line(
            target_line.parallel_offset(dist, "right")
        )

        if off_left is None and off_right is None:
            return target_line
        if off_left is None:
            outward_line = off_right
        elif off_right is None:
            outward_line = off_left
        else:
            outward_line = (
                off_left
                if off_left.distance(reference_line) > off_right.distance(reference_line)
                else off_right
            )

        if outward_line is None:
            return target_line

        outward_line = DTMChannelModifier._single_offset_line(outward_line) or target_line

        if target_line.has_z:

            def restore_z(geom):
                if geom.geom_type == "LineString":
                    coords = [
                        (
                            pt[0],
                            pt[1],
                            target_line.interpolate(
                                target_line.project(Point(pt[:2]))
                            ).z,
                        )
                        for pt in geom.coords
                    ]
                    return LineString(coords)
                return geom

            outward_line = restore_z(outward_line)

        return outward_line

    @staticmethod
    def _single_offset_line(geometry):
        """Converts offset output to one usable LineString."""
        lines = DTMChannelModifier._line_strings(geometry)
        if not lines:
            return None
        if len(lines) == 1:
            return lines[0]

        try:
            merged = linemerge(unary_union(lines))
            merged_lines = DTMChannelModifier._line_strings(merged)
            if len(merged_lines) == 1:
                return merged_lines[0]
            if merged_lines:
                lines = merged_lines
        except Exception:
            pass

        return max(lines, key=lambda line: line.length)

    @staticmethod
    def offset_bank_lines_outwards(
        banks_input, output_shp_path: str = None, offset_m: float = 0.2
    ):
        if isinstance(banks_input, (str, os.PathLike)):
            print(
                f"\nOffsetting bank lines outwards by {offset_m}m from: {banks_input}..."
            )
            banks_gdf = DTMChannelModifier.clean_and_merge_banklines(banks_input)
        else:
            print(
                f"\nOffsetting bank lines outwards by {offset_m}m from provided GeoDataFrame..."
            )
            banks_gdf = banks_input

        lines = []
        for geom in banks_gdf.geometry:
            if geom.geom_type == "LineString":
                lines.append(geom)
            elif geom.geom_type == "MultiLineString":
                lines.extend(geom.geoms)

        if len(lines) < 2:
            raise ValueError("The shapefile must contain at least two line geometries.")

        line1, line2 = lines[0], lines[1]
        
        centerline_gdf = DTMChannelModifier.generate_centerline_from_banks(banks_gdf)
        centerline = centerline_gdf.geometry.iloc[0]

        new_line1 = DTMChannelModifier._get_outward_offset_line(line1, centerline, offset_m)
        new_line2 = DTMChannelModifier._get_outward_offset_line(line2, centerline, offset_m)

        offset_gdf = gpd.GeoDataFrame(
            {"Name": ["Bank 1 Offset Outward", "Bank 2 Offset Outward"]},
            geometry=[new_line1, new_line2],
            crs=banks_gdf.crs,
        )
        if output_shp_path:
            offset_gdf.to_file(output_shp_path)
            print(f"Outward offset bank lines successfully saved to: {output_shp_path}")
        return offset_gdf

    @staticmethod
    def create_polygon_mask_from_banks(
        banks_input, output_shp_path: str = None, offset_m: float = 0.2
    ):
        """
        Creates a closed polygon mask spanning between the two bank lines.
        Automatically offsets the lines outwards by offset_m before creating the polygon.
        """
        if isinstance(banks_input, (str, os.PathLike)):
            print(
                f"\nCreating polygon mask from banks (offset by {offset_m}m) from: {banks_input}..."
            )
            banks_gdf = DTMChannelModifier.clean_and_merge_banklines(banks_input)
        else:
            print(
                f"\nCreating polygon mask from banks (offset by {offset_m}m) from provided GeoDataFrame..."
            )
            banks_gdf = banks_input

        lines = []
        for geom in banks_gdf.geometry:
            if geom.geom_type == "LineString":
                lines.append(geom)
            elif geom.geom_type == "MultiLineString":
                lines.extend(geom.geoms)

        if len(lines) < 2:
            raise ValueError("The shapefile must contain at least two line geometries.")

        line1, line2 = lines[0], lines[1]

        centerline_gdf = DTMChannelModifier.generate_centerline_from_banks(banks_gdf)
        centerline = centerline_gdf.geometry.iloc[0]

        # 1. Offset the lines outwards
        new_line1 = DTMChannelModifier._get_outward_offset_line(line1, centerline, offset_m)
        new_line2 = DTMChannelModifier._get_outward_offset_line(line2, centerline, offset_m)

        coords1 = list(new_line1.coords)
        coords2 = list(new_line2.coords)

        # 2. Check orientation to prevent a twisted "bowtie" polygon
        p1_end = Point(coords1[-1])
        p2_start = Point(coords2[0])
        p2_end = Point(coords2[-1])

        # If line 1 end is closer to line 2 end than line 2 start, we need to reverse line 2
        if p1_end.distance(p2_start) > p1_end.distance(p2_end):
            coords2 = coords2[::-1]

        # 3. Join the coordinates to form a loop
        poly_coords = coords1 + coords2

        # Ensure the polygon is closed (first and last coordinate must be identical)
        if poly_coords[0] != poly_coords[-1]:
            poly_coords.append(poly_coords[0])

        mask_poly = Polygon(poly_coords)

        poly_gdf = gpd.GeoDataFrame(
            [{"Name": "Bank Mask Polygon", "geometry": mask_poly}], crs=banks_gdf.crs
        )
        if output_shp_path:
            poly_gdf.to_file(output_shp_path)
            print(f"Mask polygon successfully saved to: {output_shp_path}")
        return poly_gdf

    @staticmethod
    def export_study_perimeter(
        bank_shp_path: str,
        output_shp_path: str,
        offset_m: float = 500.0,
        cross_section_csv: str = None,
    ):
        print(f"\nExporting study perimeter (buffered by {offset_m}m) to: {output_shp_path}...")
        
        banks_gdf = DTMChannelModifier.clean_and_merge_banklines(bank_shp_path)
        centerline_gdf = DTMChannelModifier.generate_centerline_from_banks(banks_gdf)
        centerline = centerline_gdf.geometry.iloc[0]

        if cross_section_csv:
            channel = {
                "index": 0,
                "name": Path(cross_section_csv).stem,
                "cross_section_csv": Path(cross_section_csv),
                "bank_shp_path": Path(bank_shp_path),
                "banks_gdf": banks_gdf,
                "centerline": centerline,
                "processing_banks_gdf": banks_gdf,
                "processing_centerline": centerline,
            }
            study_polygon = DTMChannelModifier._build_clipped_network_perimeter(
                channels=[channel],
                offset_m=offset_m,
                network={"channels": [channel], "junctions": []},
            )
        else:
            study_polygon = centerline.buffer(offset_m)
        
        perimeter_gdf = gpd.GeoDataFrame(
            [{"Name": f"Study Perimeter {offset_m}m", "geometry": study_polygon}], 
            crs=centerline_gdf.crs
        )
        if output_shp_path:
            perimeter_gdf.to_file(output_shp_path)
            
        return perimeter_gdf
