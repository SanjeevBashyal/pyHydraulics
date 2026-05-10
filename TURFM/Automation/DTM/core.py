"""Core raster-window setup, survey parsing, and legacy single-channel helpers."""

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


class CoreMixin:
    """Core raster-window setup, survey parsing, and legacy single-channel helpers."""

    def __init__(self):
        """
        Initializes the DTM Modifier.
        """
        self.dtm_path = None
        self.csv_path = None
        self.output_path = None
        self.target_res = 0.1
        self.buffer_m = 20.0

        self.out_dir = None

        self.dtm_data = None
        self.dtm_transform = None
        self.dtm_crs = None
        self.dtm_meta = None

    @staticmethod
    def _read_csv_auto(csv_path, required_columns=None):
        """
        Reads CSV-like files with common delimiters used in survey exports.

        Some delivered cross-section files are comma-separated and others are
        semicolon-separated while keeping the .csv extension. Pandas' default
        comma parser then hides X/Y/Z inside one combined header, so we auto
        detect the delimiter and normalize column names.
        """
        csv_path = Path(csv_path)
        try:
            df = pd.read_csv(csv_path, sep=None, engine="python", encoding="utf-8-sig")
        except UnicodeDecodeError:
            df = pd.read_csv(csv_path, sep=None, engine="python", encoding="latin1")

        df.columns = [str(column).strip().strip('"').strip("'") for column in df.columns]
        if required_columns:
            missing = [column for column in required_columns if column not in df.columns]
            if missing:
                raise ValueError(
                    f"{csv_path} is missing required column(s): {', '.join(missing)}. "
                    f"Detected columns: {', '.join(map(str, df.columns))}"
                )
        return df

    def _read_survey_and_get_bounds(self):
        print("Reading survey data and determining processing window...")
        self.raw_df = self._read_csv_auto(
            self.csv_path,
            required_columns=("Station", "X", "Y", "Z"),
        )
        duplicate_columns = [
            column
            for column in ("River", "Reach", "Station", "X", "Y", "Z")
            if column in self.raw_df.columns
        ]
        duplicate_profile_point_mask = self.raw_df.duplicated(
            subset=duplicate_columns,
            keep="first",
        )
        if duplicate_profile_point_mask.any():
            print(
                "Removed "
                f"{int(duplicate_profile_point_mask.sum())} exact duplicate "
                "cross-section row(s)."
            )
        self.df = self.raw_df.loc[~duplicate_profile_point_mask].copy()

        minx = self.df["X"].min() - self.buffer_m
        maxx = self.df["X"].max() + self.buffer_m
        miny = self.df["Y"].min() - self.buffer_m
        maxy = self.df["Y"].max() + self.buffer_m

        self.bounds = (minx, miny, maxx, maxy)

    def _resample_dtm_window(self):
        print(
            f"Extracting and resampling DTM window to {self.target_res}m resolution..."
        )
        with rasterio.open(self.dtm_path) as dataset:
            window = from_bounds(*self.bounds, transform=dataset.transform)
            window = window.intersection(
                rasterio.windows.Window(0, 0, dataset.width, dataset.height)
            )

            orig_res_x = dataset.transform[0]
            orig_res_y = -dataset.transform[4]
            phys_width = window.width * orig_res_x
            phys_height = window.height * orig_res_y

            new_width = int(phys_width / self.target_res)
            new_height = int(phys_height / self.target_res)

            self.dtm_data = dataset.read(
                1,
                window=window,
                out_shape=(new_height, new_width),
                resampling=Resampling.bilinear,
            )

            window_transform = rasterio.windows.transform(window, dataset.transform)
            self.dtm_transform = window_transform * Affine.scale(
                (window.width / new_width), (window.height / new_height)
            )

            self.dtm_crs = dataset.crs
            self.dtm_meta = dataset.meta.copy()
            self.dtm_meta.update(
                {
                    "height": new_height,
                    "width": new_width,
                    "transform": self.dtm_transform,
                    "dtype": "float32",
                    "nodata": dataset.nodata,
                }
            )

    def _process_survey_geometry(self):
        print("Processing channel boundary and cross-sections...")
        left_banks, right_banks = [], []
        self.xs_lines_data, self.xs_centers_data = [], []

        self.cs_coords_list = []  # Store raw coordinates for custom math interpolation
        cl_pts = []

        stations = sorted(self.raw_df["Station"].unique())
        for stat in stations:
            # Full 3D cross-section data for the mathematical interpolation
            stat_filtered = self.df[self.df["Station"] == stat].copy()
            self.cs_coords_list.append(stat_filtered[["X", "Y", "Z"]].values)

            # For shapefiles
            stat_raw = self.raw_df[self.raw_df["Station"] == stat]
            line_coords = stat_raw[["X", "Y", "Z"]].values
            if len(line_coords) >= 2:
                self.xs_lines_data.append(
                    {"Station": stat, "geometry": LineString(line_coords)}
                )

            # Find Center of Deepest Point(s)
            min_z = stat_raw["Z"].min()
            lowest_pts = stat_raw[stat_raw["Z"] == min_z]

            first_pt, last_pt = lowest_pts.iloc[0], lowest_pts.iloc[-1]
            mid_x, mid_y = (first_pt["X"] + last_pt["X"]) / 2.0, (
                first_pt["Y"] + last_pt["Y"]
            ) / 2.0

            self.xs_centers_data.append(
                {
                    "Station": stat,
                    "Z_min": min_z,
                    "geometry": Point(mid_x, mid_y, min_z),
                }
            )
            cl_pts.append([mid_x, mid_y])

            left_banks.append((stat_filtered.iloc[0]["X"], stat_filtered.iloc[0]["Y"]))
            right_banks.append(
                (stat_filtered.iloc[-1]["X"], stat_filtered.iloc[-1]["Y"])
            )

        self.cl_coords = np.array(cl_pts)

        centerline_coords = [
            (pt["geometry"].x, pt["geometry"].y, pt["geometry"].z)
            for pt in sorted(self.xs_centers_data, key=lambda x: x["Station"])
        ]
        self.centerline_data = [
            {"Name": "River Centerline", "geometry": LineString(centerline_coords)}
        ]

        left_line = LineString(left_banks)
        right_line = LineString(right_banks)
        self.banks_gdf = gpd.GeoDataFrame(
            {"Name": ["Left Bank", "Right Bank"]},
            geometry=[left_line, right_line],
            crs=self.dtm_crs,
        )

        poly_gdf = self.create_polygon_mask_from_banks(self.banks_gdf)
        self.channel_polygon = poly_gdf.geometry.iloc[0]

    @staticmethod
    def _project_points_to_cs(pts_xy, cs_coords):
        """Vectorized projection of N points onto a 3D cross-section polyline."""
        N = pts_xy.shape[0]
        M = cs_coords.shape[0]
        min_dist = np.full(N, np.inf)
        interp_z = np.zeros(N)

        for i in range(M - 1):
            A, B = cs_coords[i, :2], cs_coords[i + 1, :2]
            ZA, ZB = cs_coords[i, 2], cs_coords[i + 1, 2]

            AB = B - A
            L2 = np.dot(AB, AB)
            if L2 == 0:
                continue

            AP = pts_xy - A
            t = np.clip(np.dot(AP, AB) / L2, 0.0, 1.0)

            Proj_x = A[0] + t * AB[0]
            Proj_y = A[1] + t * AB[1]

            dist = np.hypot(pts_xy[:, 0] - Proj_x, pts_xy[:, 1] - Proj_y)

            mask = dist < min_dist
            min_dist[mask] = dist[mask]
            interp_z[mask] = ZA + t[mask] * (ZB - ZA)

        return min_dist, interp_z

    @staticmethod
    def _get_bracketing_cs(pts_xy, cl_coords):
        """Finds which two cross sections a pixel sits between by projecting to centerline."""
        N = pts_xy.shape[0]
        K = cl_coords.shape[0]
        min_dist = np.full(N, np.inf)
        best_j = np.zeros(N, dtype=int)
        best_t = np.zeros(N)

        for j in range(K - 1):
            A, B = cl_coords[j, :2], cl_coords[j + 1, :2]
            AB = B - A
            L2 = np.dot(AB, AB)
            if L2 == 0:
                continue

            AP = pts_xy - A
            t = np.clip(np.dot(AP, AB) / L2, 0.0, 1.0)

            Proj_x = A[0] + t * AB[0]
            Proj_y = A[1] + t * AB[1]

            dist = np.hypot(pts_xy[:, 0] - Proj_x, pts_xy[:, 1] - Proj_y)

            mask = dist < min_dist
            min_dist[mask] = dist[mask]
            best_j[mask] = j
            best_t[mask] = t[mask]

        return best_j, best_t

    def _export_dtm(self):
        print(f"Exporting modified DTM to {self.output_path}...")
        with rasterio.open(self.output_path, "w", **self.dtm_meta) as dest:
            dest.write(self.modified_dtm.astype("float32"), 1)

    def _export_shapefiles(self):
        print("Exporting cross-section vector shapefiles...")
        crs = self.dtm_crs if self.dtm_crs else None

        gpd.GeoDataFrame(self.xs_lines_data, crs=crs).to_file(
            os.path.join(self.out_dir, "crossSections.shp")
        )
        gpd.GeoDataFrame(self.xs_centers_data, crs=crs).to_file(
            os.path.join(self.out_dir, "crossSectionsCenter.shp")
        )
        gpd.GeoDataFrame(self.centerline_data, crs=crs).to_file(
            os.path.join(self.out_dir, "centerLine.shp")
        )
        print("Shapefiles exported successfully.")

    def process(self):
        """Executes the standard DTM modification workflow."""
        if self.output_path is not None:
            self.out_dir = os.path.dirname(os.path.abspath(self.output_path))
        else:
            self.out_dir = os.getcwd()

        self._read_survey_and_get_bounds()
        self._resample_dtm_window()
        self._process_survey_geometry()
        self._export_shapefiles()
        print("\nAll processing complete successfully!")

    def get_cell_centerline_metrics(self, x, y, banks=None, centerline=None):
        """
        For given terrain cells (x, y) which can be scalars or numpy arrays,
        determine their nearest centerline point (cx, cy) and the corresponding interpolated
        total bank width at that exact centerline location.
        """
        x_in = np.asarray(x)
        y_in = np.asarray(y)
        
        use_cache = (banks is None and centerline is None)
        
        if banks is None:
            if getattr(self, 'banks_gdf', None) is None:
                raise ValueError("Banks not provided and self.banks_gdf not found. Run _process_survey_geometry first.")
            banks = self.banks_gdf
            
        if centerline is None:
            if getattr(self, 'centerline_gdf', None) is None:
                self.centerline_gdf = self.generate_centerline_from_banks(banks)
            centerline = self.centerline_gdf.geometry.iloc[0]
        elif isinstance(centerline, gpd.GeoDataFrame):
            centerline = centerline.geometry.iloc[0]
            
        lines = []
        for geom in banks.geometry:
            if geom.geom_type == 'LineString': lines.append(geom)
            elif geom.geom_type == 'MultiLineString': lines.extend(geom.geoms)
        if len(lines) < 2:
            raise ValueError("Banks must contain at least two valid LineStrings.")
        left_bank, right_bank = lines[0], lines[1]
        
        cl_coords = np.array(centerline.coords)[:, :2]
        
        if use_cache and getattr(self, '_cl_widths_cache', None) is not None and getattr(self, '_cl_coords_cache', None) is not None and len(self._cl_coords_cache) == len(cl_coords):
            widths = self._cl_widths_cache
        else:
            widths = np.zeros(len(cl_coords))
            # Point is imported at the top of the file
            for i, pt in enumerate(cl_coords):
                p = Point(pt)
                widths[i] = left_bank.distance(p) + right_bank.distance(p)
            if use_cache:
                self._cl_widths_cache = widths
                self._cl_coords_cache = cl_coords
        
        import shapely
        has_shapely2 = hasattr(shapely, 'line_locate_point')

        x_in = np.asarray(x)
        y_in = np.asarray(y)
        x_flat = x_in.ravel()
        y_flat = y_in.ravel()
        
        pts_xy = np.column_stack((x_flat, y_flat))
        N = len(pts_xy)
        
        # cl_coords and widths are already defined from the cache logic above
        K = len(cl_coords)
        
        if has_shapely2 and K > 1 and hasattr(self, 'centerline_gdf'):
            # Massive C-native acceleration avoiding pure python N*K loops
            pts_shp = shapely.points(x_flat, y_flat)
            cl_line = self.centerline_gdf.geometry.iloc[0]
            
            # Find 1D distance exactly natively mapped within millisecond threshold
            d_cl = shapely.line_locate_point(cl_line, pts_shp)
            
            # Find exact intersection coordinates natively
            intersections = shapely.line_interpolate_point(cl_line, d_cl)
            
            cx = shapely.get_x(intersections)
            cy = shapely.get_y(intersections)
            
            # Interpolate widths dynamically linking length proportions
            cl_dist = np.zeros(K)
            for i in range(1, K):
                cl_dist[i] = cl_dist[i-1] + np.hypot(cl_coords[i,0]-cl_coords[i-1,0], cl_coords[i,1]-cl_coords[i-1,1])
                
            width_interp = np.interp(d_cl, cl_dist, widths)
            
        else:
            # Slower python mathematical fallback logic
            min_dist = np.full(N, np.inf)
            best_j = np.zeros(N, dtype=int)
            best_t = np.zeros(N)
            
            for j in range(K - 1):
                A, B = cl_coords[j], cl_coords[j + 1]
                AB = B - A
                L2 = np.dot(AB, AB)
                if L2 == 0:
                    continue
                    
                AP = pts_xy - A
                t = np.clip(np.dot(AP, AB) / L2, 0.0, 1.0)
                
                Proj_x = A[0] + t * AB[0]
                Proj_y = A[1] + t * AB[1]
                
                dist = np.hypot(pts_xy[:, 0] - Proj_x, pts_xy[:, 1] - Proj_y)
                
                mask = dist < min_dist
                min_dist[mask] = dist[mask]
                best_j[mask] = j
                best_t[mask] = t[mask]
                
            j = best_j
            t = best_t
            
            A = cl_coords[j]
            B = cl_coords[j+1]
            
            cx = A[:, 0] + t * (B[:, 0] - A[:, 0])
            cy = A[:, 1] + t * (B[:, 1] - A[:, 1])
            
            wA = widths[j]
            wB = widths[j+1]
            width_interp = wA + t * (wB - wA)
        
        if np.isscalar(x) and np.isscalar(y):
            return cx[0], cy[0], width_interp[0]
            
        return cx.reshape(x_in.shape), cy.reshape(y_in.shape), width_interp.reshape(x_in.shape)
