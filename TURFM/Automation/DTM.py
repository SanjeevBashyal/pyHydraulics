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


class DTMChannelModifier:
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
        self.df = self.raw_df.sort_values(by=["Station", "Z"]).drop_duplicates(
            subset=["Station", "X", "Y"], keep="first"
        )

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

    @staticmethod
    def _centerline_unit_tangent(centerline, centerline_distance, sample_distance=0.5):
        cl_length = centerline.length
        if cl_length <= 0:
            return np.array([1.0, 0.0], dtype=float)

        d1 = max(0.0, float(centerline_distance) - sample_distance)
        d2 = min(cl_length, float(centerline_distance) + sample_distance)
        if d2 <= d1:
            d1 = max(0.0, float(centerline_distance) - 1e-3)
            d2 = min(cl_length, float(centerline_distance) + 1e-3)

        p1 = centerline.interpolate(d1)
        p2 = centerline.interpolate(d2)
        tangent = np.array([p2.x - p1.x, p2.y - p1.y], dtype=float)
        tangent_norm = np.linalg.norm(tangent)
        if tangent_norm > 0:
            return tangent / tangent_norm

        coords = np.asarray(centerline.coords)[:, :2]
        for idx in range(len(coords) - 1):
            segment = coords[idx + 1] - coords[idx]
            segment_norm = np.linalg.norm(segment)
            if segment_norm > 0:
                return segment / segment_norm

        return np.array([1.0, 0.0], dtype=float)

    @staticmethod
    def _compute_cross_section_skewness(line, centerline, centerline_distance):
        coords = np.asarray(line.coords)[:, :2]
        if len(coords) < 2:
            return 0.0, 0.0, 1.0

        xs_vector = coords[-1] - coords[0]
        xs_norm = np.linalg.norm(xs_vector)
        if xs_norm == 0:
            return 0.0, 0.0, 1.0
        xs_unit = xs_vector / xs_norm

        tangent = DTMChannelModifier._centerline_unit_tangent(
            centerline,
            centerline_distance,
        )
        normal = np.array([-tangent[1], tangent[0]], dtype=float)
        normal_norm = np.linalg.norm(normal)
        if normal_norm == 0:
            return 0.0, 0.0, 1.0
        normal /= normal_norm

        cosine_raw = float(np.clip(abs(np.dot(xs_unit, normal)), 0.0, 1.0))
        angle_radians = float(np.arccos(cosine_raw))
        angle_degrees = float(np.degrees(angle_radians))
        cosine_safe = max(cosine_raw, 1e-6)
        return angle_radians, angle_degrees, cosine_safe

    @staticmethod
    def _build_corrected_section_profile(
        line,
        centerline,
        centerline_distance,
        center_point,
        bank_lines=None,
        skewness_correction=True,
    ):
        coords = np.asarray(line.coords)
        if coords.shape[0] < 2:
            raise ValueError("Each cross section must contain at least two coordinates.")

        distances = np.zeros(coords.shape[0], dtype=float)
        for idx in range(1, coords.shape[0]):
            distances[idx] = distances[idx - 1] + np.linalg.norm(coords[idx, :2] - coords[idx - 1, :2])

        if coords.shape[1] < 3:
            z_values = np.zeros(coords.shape[0], dtype=float)
        else:
            z_values = coords[:, 2].astype(float)

        raw_center_distance = float(line.project(center_point))
        angle_radians, angle_degrees, cosine_safe = DTMChannelModifier._compute_cross_section_skewness(
            line,
            centerline,
            centerline_distance,
        )
        distance_cosine = cosine_safe if skewness_correction else 1.0

        corrected_distances = distances * distance_cosine
        corrected_center_distance = raw_center_distance * distance_cosine
        raw_left_bank_distance, raw_right_bank_distance = DTMChannelModifier._cross_section_bank_distances(
            line=line,
            bank_lines=bank_lines,
            center_distance=raw_center_distance,
        )
        corrected_left_bank_distance = raw_left_bank_distance * distance_cosine
        corrected_right_bank_distance = raw_right_bank_distance * distance_cosine

        unique_distances, unique_indices = np.unique(corrected_distances, return_index=True)
        unique_z_values = z_values[unique_indices]
        max_distance = float(unique_distances[-1])

        if len(unique_distances) == 1:
            constant_z = float(unique_z_values[0])

            def z_func(distance):
                distance_arr = np.asarray(distance, dtype=float)
                return np.full_like(distance_arr, constant_z, dtype=float)
        else:
            def z_func(distance):
                distance_arr = np.asarray(distance, dtype=float)
                return np.interp(
                    np.clip(distance_arr, 0.0, max_distance),
                    unique_distances,
                    unique_z_values,
                )

        return {
            "raw_center_distance": raw_center_distance,
            "corrected_center_distance": corrected_center_distance,
            "corrected_total_length": max_distance,
            "corrected_left_width": corrected_center_distance,
            "corrected_right_width": max(0.0, max_distance - corrected_center_distance),
            "raw_left_bank_distance": raw_left_bank_distance,
            "raw_right_bank_distance": raw_right_bank_distance,
            "corrected_left_bank_distance": corrected_left_bank_distance,
            "corrected_right_bank_distance": corrected_right_bank_distance,
            "corrected_left_bank_width": max(0.0, corrected_center_distance - corrected_left_bank_distance),
            "corrected_right_bank_width": max(0.0, corrected_right_bank_distance - corrected_center_distance),
            "skewness_angle_radians": angle_radians,
            "skewness_angle_degrees": angle_degrees,
            "skewness_cosine": cosine_safe,
            "skewness_correction": bool(skewness_correction),
            "distance_correction_cosine": distance_cosine,
            "z_func": z_func,
        }

    @staticmethod
    def _cross_section_center_point_from_centerline(line, centerline, label=None, bank_lines=None):
        intersection = line.intersection(centerline)
        points = DTMChannelModifier._points_from_geometry(intersection)
        if not points:
            bank_midpoint = DTMChannelModifier._cross_section_bank_midpoint(line, bank_lines)
            if bank_midpoint is not None:
                return bank_midpoint
            name = f" {label}" if label is not None else ""
            raise ValueError(
                f"Cross section{name} does not intersect the generated bank centerline. "
                "The centerline/cross-section intersection is required as the pivot."
            )

        if len(points) == 1:
            point = points[0]
            return Point(float(point.x), float(point.y))

        midpoint = line.interpolate(0.5, normalized=True)
        point = min(points, key=lambda candidate: candidate.distance(midpoint))
        return Point(float(point.x), float(point.y))

    @staticmethod
    def _points_from_geometry(geometry):
        if geometry is None or geometry.is_empty:
            return []
        if geometry.geom_type == "Point":
            return [geometry]
        if geometry.geom_type == "MultiPoint":
            return list(geometry.geoms)
        if geometry.geom_type == "LineString":
            return [geometry.interpolate(0.5, normalized=True)]
        if geometry.geom_type == "MultiLineString":
            return [part.interpolate(0.5, normalized=True) for part in geometry.geoms if not part.is_empty]
        if hasattr(geometry, "geoms"):
            points = []
            for part in geometry.geoms:
                points.extend(DTMChannelModifier._points_from_geometry(part))
            return points
        return []

    @staticmethod
    def _cross_section_bank_midpoint(line, bank_lines=None):
        if not bank_lines or len(bank_lines) < 2:
            return None

        distances = []
        for bank_line in bank_lines[:2]:
            intersections = DTMChannelModifier._points_from_geometry(line.intersection(bank_line))
            if intersections:
                point = min(intersections, key=lambda candidate: line.project(candidate))
                distances.append(float(line.project(point)))
                continue

            try:
                point_on_xs, _ = nearest_points(line, bank_line)
                distances.append(float(line.project(point_on_xs)))
            except Exception:
                continue

        if len(distances) < 2:
            return None

        left, right = sorted(distances[:2])
        if abs(right - left) <= 1e-6:
            return None
        center_distance = 0.5 * (left + right)
        point = line.interpolate(center_distance)
        return Point(float(point.x), float(point.y))

    @staticmethod
    def _cross_section_bank_distances(line, bank_lines=None, center_distance=None):
        line_length = float(line.length)
        center_distance = float(center_distance if center_distance is not None else line_length / 2.0)
        if not bank_lines:
            return 0.0, line_length

        distances = []
        for bank_line in bank_lines[:2]:
            if bank_line is None or bank_line.is_empty:
                continue
            try:
                point_on_xs, _ = nearest_points(line, bank_line)
                distances.append(float(line.project(point_on_xs)))
            except Exception:
                continue

        if len(distances) < 2:
            return 0.0, line_length

        distances = sorted(float(np.clip(distance, 0.0, line_length)) for distance in distances)
        left_candidates = [distance for distance in distances if distance <= center_distance]
        right_candidates = [distance for distance in distances if distance >= center_distance]
        left_distance = max(left_candidates) if left_candidates else distances[0]
        right_distance = min(right_candidates) if right_candidates else distances[-1]

        if left_distance > right_distance:
            left_distance, right_distance = right_distance, left_distance
        if abs(right_distance - left_distance) <= 1e-6:
            return 0.0, line_length
        return left_distance, right_distance

    @staticmethod
    def _centerline_cumulative_distances(centerline):
        coords = np.asarray(centerline.coords)[:, :2]
        distances = np.zeros(len(coords), dtype=float)
        for index in range(1, len(coords)):
            distances[index] = distances[index - 1] + np.linalg.norm(coords[index] - coords[index - 1])
        return coords, distances

    @staticmethod
    def _cell_signed_offsets_and_bank_widths(centerline, bank_lines, xs, ys, cxs, cys, centerline_distances):
        if len(bank_lines) < 2:
            fallback = np.full_like(np.asarray(xs, dtype=float), 1.0, dtype=float)
            return np.asarray(xs, dtype=float) * 0.0, fallback

        cl_coords, cl_distances = DTMChannelModifier._centerline_cumulative_distances(centerline)
        if len(cl_coords) < 2:
            fallback = np.full_like(np.asarray(xs, dtype=float), 1.0, dtype=float)
            return np.asarray(xs, dtype=float) * 0.0, fallback

        positive_width_samples = np.zeros(len(cl_coords), dtype=float)
        negative_width_samples = np.zeros(len(cl_coords), dtype=float)
        for index, coord in enumerate(cl_coords):
            tangent = DTMChannelModifier._centerline_unit_tangent(centerline, cl_distances[index])
            normal = np.array([-tangent[1], tangent[0]], dtype=float)
            point = Point(float(coord[0]), float(coord[1]))
            signed_widths = []
            for bank_line in bank_lines[:2]:
                _, bank_point = nearest_points(point, bank_line)
                vector = np.array([bank_point.x - point.x, bank_point.y - point.y], dtype=float)
                signed_widths.append(float(np.dot(vector, normal)))

            positive_candidates = [width for width in signed_widths if width >= 0.0]
            negative_candidates = [width for width in signed_widths if width < 0.0]
            total_width = sum(abs(width) for width in signed_widths)
            positive_width_samples[index] = (
                min(positive_candidates, key=abs)
                if positive_candidates
                else max(total_width / 2.0, 1e-6)
            )
            negative_width_samples[index] = abs(
                max(negative_candidates, key=lambda value: value)
                if negative_candidates
                else -max(total_width / 2.0, 1e-6)
            )

        centerline_distances = np.asarray(centerline_distances, dtype=float)
        segment_index = np.searchsorted(cl_distances, centerline_distances, side="right") - 1
        segment_index = np.clip(segment_index, 0, len(cl_coords) - 2)
        segment_vectors = cl_coords[segment_index + 1] - cl_coords[segment_index]
        segment_lengths = np.linalg.norm(segment_vectors, axis=1)
        safe_lengths = np.maximum(segment_lengths, 1e-6)
        normals = np.column_stack(
            (
                -segment_vectors[:, 1] / safe_lengths,
                segment_vectors[:, 0] / safe_lengths,
            )
        )
        cell_vectors = np.column_stack((np.asarray(xs) - np.asarray(cxs), np.asarray(ys) - np.asarray(cys)))
        signed_offsets = np.sum(cell_vectors * normals, axis=1)
        positive_widths = np.interp(centerline_distances, cl_distances, positive_width_samples)
        negative_widths = np.interp(centerline_distances, cl_distances, negative_width_samples)
        side_widths = np.where(signed_offsets >= 0.0, positive_widths, negative_widths)
        return signed_offsets, np.maximum(side_widths, 1e-6)

    @staticmethod
    def _map_lateral_distance_to_section_offset(
        distance_from_center,
        local_bank_width,
        section_bank_width,
        section_center_distance,
        direction,
    ):
        distance_from_center = np.asarray(distance_from_center, dtype=float)
        local_bank_width = np.maximum(np.asarray(local_bank_width, dtype=float), 1e-6)
        section_bank_width = np.maximum(np.asarray(section_bank_width, dtype=float), 1e-6)
        direction = np.asarray(direction, dtype=float)

        in_bank_distance = np.minimum(distance_from_center, local_bank_width)
        outside_bank_distance = np.maximum(distance_from_center - local_bank_width, 0.0)
        mapped_in_bank = in_bank_distance * (section_bank_width / local_bank_width)
        mapped_distance = mapped_in_bank + outside_bank_distance
        return float(section_center_distance) + direction * mapped_distance

    @staticmethod
    def _protected_bank_mapped_distance(
        distance_from_bank,
        local_bank_to_center_distance,
        section_bank_to_center_distance,
        protected_width=1.0,
    ):
        distance_from_bank = max(float(distance_from_bank), 0.0)
        local_width = max(float(local_bank_to_center_distance), 1e-6)
        section_width = max(float(section_bank_to_center_distance), 1e-6)
        protected = max(float(protected_width), 0.0)

        local_protected = min(protected, local_width)
        section_protected = min(protected, section_width)
        if distance_from_bank <= local_protected:
            return min(distance_from_bank, section_width)

        local_bed_width = max(local_width - local_protected, 1e-6)
        section_bed_width = max(section_width - section_protected, 0.0)
        bed_fraction = np.clip((distance_from_bank - local_protected) / local_bed_width, 0.0, 1.0)
        return min(section_protected + bed_fraction * section_bed_width, section_width)

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
        transition_to_dtm_distance_m=5.0,
        skewness_correction=True,
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

        # Generate centerline from banks
        modifier.centerline_gdf = DTMChannelModifier.generate_centerline_from_banks(modifier.banks_gdf)

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
                skewness_correction=skewness_correction,
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

        idx_dn = np.searchsorted(d_xs_array, d_cells)
        idx_dn = np.clip(idx_dn, 1, len(d_xs_array) - 1)
        idx_up = idx_dn - 1

        new_zs = np.zeros(N)
        dist_up_array = np.zeros(N)
        dist_dn_array = np.zeros(N)
        
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
            
            if has_shapely2:
                pts_shp = shapely.points(x_m, y_m)
                dist_up_m = shapely.distance(pts_shp, st_up['line'])
                dist_dn_m = shapely.distance(pts_shp, st_dn['line'])
                d_cell_up_m = shapely.line_locate_point(st_up['line'], pts_shp)
                d_cell_dn_m = shapely.line_locate_point(st_dn['line'], pts_shp)
            else:
                dist_up_m = np.zeros(len(x_m))
                dist_dn_m = np.zeros(len(x_m))
                d_cell_up_m = np.zeros(len(x_m))
                d_cell_dn_m = np.zeros(len(x_m))
                for k in range(len(x_m)):
                    p = Point(x_m[k], y_m[k])
                    dist_up_m[k] = p.distance(st_up['line'])
                    dist_dn_m[k] = p.distance(st_dn['line'])
                    d_cell_up_m[k] = st_up['line'].project(p)
                    d_cell_dn_m[k] = st_dn['line'].project(p)
            
            dist_up_array[mask] = dist_up_m
            dist_dn_array[mask] = dist_dn_m

            # Up Z
            mapped_up = dist_cl_m * (st_up['bw_xs'] / bw_m)
            dir_up = np.where(d_cell_up_m >= st_up['d_C_xs'], 1, -1)
            offset_up = st_up['d_C_xs_corrected'] + dir_up * mapped_up
            z_up = st_up['z_func'](offset_up)

            # Dn Z
            mapped_dn = dist_cl_m * (st_dn['bw_xs'] / bw_m)
            dir_dn = np.where(d_cell_dn_m >= st_dn['d_C_xs'], 1, -1)
            offset_dn = st_dn['d_C_xs_corrected'] + dir_dn * mapped_dn
            z_dn = st_dn['z_func'](offset_dn)
            
            tot = dist_up_m + dist_dn_m
            tot_safe = np.maximum(tot, 1e-6)
            w1 = np.where(tot == 0, 1.0, dist_dn_m / tot_safe)
            w2 = np.where(tot == 0, 0.0, dist_up_m / tot_safe)
            
            new_zs[mask] = w1 * z_up + w2 * z_dn

        # Apply final continuous mathematical blending
        final_zs = w1_terrain * dtm_zs + w2_cs * new_zs

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
    def process_channel_network_dtm(
        dtm_path,
        channel_inputs,
        output_tif_path,
        target_res=0.1,
        buffer_m=20.0,
        blend_type="linear",
        bank_offset_m=0.2,
        full_cross_section_weight_distance_m=1.5,
        transition_to_dtm_distance_m=5.0,
        junction_tolerance=50.0,
        write_intermediate=True,
        centerline_output_path=None,
        merged_banks_output_path=None,
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
                )
            )
        final_modifier.dtm_data = final_data
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
            "merged_banks_shp": str(merged_banks_path) if merged_banks_path else None,
            "perimeter_shp": str(perimeter_path) if perimeter_path else None,
            "connected_bank_products": connected_bank_products,
            "junction_interpolation": junction_interpolation_summary,
            "intermediate_tifs": intermediate_tifs,
            "shared_bounds": [float(value) for value in shared_bounds],
            "blend_type": blend_type,
            "bank_offset_m": float(bank_offset_m),
            "full_cross_section_weight_distance_m": float(full_cross_section_weight_distance_m),
            "transition_to_dtm_distance_m": float(transition_to_dtm_distance_m),
            "junction_half_section_interpolation": bool(junction_half_section_interpolation),
            "junction_bank_structure_protection_m": float(junction_bank_structure_protection_m),
            "skewness_correction": bool(skewness_correction),
            "network_csv_path": str(network_csv_path) if network_csv_path else None,
            "junction_coordinates_csv": str(junction_coordinates_csv_path) if junction_coordinates_csv_path else None,
            "dtm_path": str(dtm_path),
            "channels": [
                {
                    "name": channel["name"],
                    "cross_section_csv": str(channel["cross_section_csv"]),
                    "bank_shp_path": str(channel["bank_shp_path"]),
                    "dtm_path": str(channel.get("dtm_path", dtm_path)),
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
        transition_to_dtm_distance_m=5.0,
        blend_type="cubic",
        bank_structure_protection_m=1.0,
        skewness_correction=True,
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
            }
            if len(profiles) < 2 or not bank_lines:
                summaries.append(summary)
                continue

            centerlines = [
                main.get("processing_centerline") or main["centerline"],
                tributary.get("processing_centerline") or tributary["centerline"],
            ]
            bank_lines = DTMChannelModifier._offset_junction_bank_lines_outward(
                bank_lines,
                centerlines=centerlines,
                offset_m=bank_offset_m,
            )
            if not bank_lines:
                summaries.append(summary)
                continue
            max_profile_width = max(
                max(profile["bank_to_center_distance"], profile["corrected_half_length"])
                for profile in profiles
            )
            max_influence = max_profile_width + hold_distance + transition_distance + float(bank_offset_m)
            max_influence = max(max_influence, hold_distance + transition_distance, 1.0)

            for bank_line in bank_lines:
                selected_profiles = DTMChannelModifier._profiles_for_junction_bank_line(
                    profiles=profiles,
                    bank_line=bank_line,
                )
                if len(selected_profiles) < 2:
                    continue

                influence_polygon = bank_line.buffer(max_influence)
                if influence_polygon.is_empty:
                    continue

                influence_mask = rasterize(
                    [influence_polygon],
                    out_shape=(height, width),
                    transform=transform,
                    fill=0,
                    default_value=1,
                    dtype="uint8",
                    all_touched=True,
                )
                rows, cols = np.where(influence_mask == 1)
                for row, col in zip(rows, cols):
                    terrain_z = float(original[row, col])
                    current_z = float(updated[row, col])
                    if not np.isfinite(terrain_z) or not np.isfinite(current_z):
                        continue
                    if nodata is not None and np.isclose(terrain_z, nodata):
                        continue

                    x, y = transform * (col + 0.5, row + 0.5)
                    cell_point = Point(float(x), float(y))
                    bank_measure = bank_line.project(cell_point)
                    bank_point = bank_line.interpolate(bank_measure)
                    center_point, bank_to_center = DTMChannelModifier._nearest_centerline_point_and_distance(
                        bank_point,
                        centerlines,
                    )
                    if bank_to_center <= 1e-6:
                        bank_to_center = max(
                            profile["bank_to_center_distance"] for profile in selected_profiles
                        )
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

                    if inside_channel_side:
                        if dist_from_bank > bank_to_center:
                            continue
                        half_fraction = min(dist_from_bank / bank_to_center, 1.0)
                        blend_distance = 0.0
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
                    weight_sum = 0.0
                    for profile in selected_profiles:
                        if half_fraction is None:
                            profile_z = profile["z_from_outside_bank_distance"](dist_from_bank)
                        else:
                            profile_z = profile["z_from_inside_bank_distance"](
                                distance_from_bank=dist_from_bank,
                                local_bank_to_center_distance=bank_to_center,
                            )
                        profile_distance = max(cell_point.distance(profile["half_line"]), 1e-6)
                        profile_weight = 1.0 / profile_distance
                        weighted_z_sum += profile_weight * profile_z
                        weight_sum += profile_weight

                    if weight_sum <= 0.0:
                        continue

                    cross_section_z = weighted_z_sum / weight_sum
                    blended_z = terrain_weight * terrain_z + (1.0 - terrain_weight) * cross_section_z
                    if not np.isfinite(blended_z):
                        continue

                    updated[row, col] = float(blended_z)
                    if not np.isclose(current_z, blended_z):
                        summary["cells_updated"] += 1

            summaries.append(summary)

        return updated, summaries

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
        transition_to_dtm_distance_m=5.0,
        bank_structure_protection_m=1.0,
        skewness_correction=True,
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
            bank_lines = DTMChannelModifier._offset_junction_bank_lines_outward(
                bank_lines,
                centerlines=centerlines,
                offset_m=bank_offset_m,
            )
            if not bank_lines:
                continue

            max_profile_width = max(
                max(profile["bank_to_center_distance"], profile["corrected_half_length"])
                for profile in profiles
            )
            max_influence = max_profile_width + hold_distance + transition_distance + float(bank_offset_m)
            max_influence = max(max_influence, hold_distance + transition_distance, 1.0)
            influence_geometry = unary_union([line.buffer(max_influence) for line in bank_lines])
            if influence_geometry.is_empty:
                continue
            mask |= rasterize(
                [influence_geometry],
                out_shape=(height, width),
                transform=base_modifier.dtm_transform,
                fill=0,
                default_value=1,
                dtype="uint8",
                all_touched=True,
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
    ):
        sections = DTMChannelModifier._junction_cross_sections_for_interpolation(
            tributary=tributary,
            main=main,
            junction=junction,
            junction_point=junction_point,
            bank_offset_m=bank_offset_m,
            skewness_correction=skewness_correction,
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
    ):
        main_bank_lines = DTMChannelModifier._offset_bank_lines_outward(
            DTMChannelModifier._line_strings(main["banks_gdf"]),
            centerline=main["centerline"],
            offset_m=bank_offset_m,
        )
        tributary_bank_lines = DTMChannelModifier._offset_bank_lines_outward(
            DTMChannelModifier._line_strings(tributary["banks_gdf"]),
            centerline=tributary["centerline"],
            offset_m=bank_offset_m,
        )
        main_sections = DTMChannelModifier._cross_sections_by_centerline_measure(
            cross_section_csv=main["cross_section_csv"],
            centerline=main["centerline"],
            bank_lines=main_bank_lines,
            skewness_correction=skewness_correction,
        )
        tributary_sections = DTMChannelModifier._cross_sections_by_centerline_measure(
            cross_section_csv=tributary["cross_section_csv"],
            centerline=tributary["centerline"],
            bank_lines=tributary_bank_lines,
            skewness_correction=skewness_correction,
        )
        if len(main_sections) < 2 or not tributary_sections:
            return []

        junction_measure = main["centerline"].project(junction_point)
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

        endpoint_measure = 0.0 if junction["tributary_endpoint"] == "start" else tributary["centerline"].length
        tributary_first = min(
            tributary_sections,
            key=lambda section: abs(section["centerline_measure"] - endpoint_measure),
        )

        return [
            ("main_upstream", "main_upstream", main, main_up),
            ("main_downstream", "main_downstream", main, main_down),
            ("tributary_downstream", "tributary", tributary, tributary_first),
        ]

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
        halves = [
            {
                "side": "left",
                "bank_point": left_bank,
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
                }
            )
        return profiles

    @staticmethod
    def _profiles_for_junction_bank_line(profiles, bank_line):
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

    @staticmethod
    def build_channel_network(
        channel_inputs,
        junction_tolerance=50.0,
        network_connections=None,
        centerline_gap_m=0.5,
    ):
        channels = []
        for index, channel_input in enumerate(channel_inputs):
            channel = dict(channel_input)
            name = channel.get("name") or Path(channel["cross_section_csv"]).stem
            banks_gdf = DTMChannelModifier.clean_and_merge_banklines(
                channel["bank_shp_path"],
                bridge_junctions=True,
            )
            centerline_gdf = DTMChannelModifier.generate_centerline_from_banks(banks_gdf)
            centerline = centerline_gdf.geometry.iloc[0]
            channels.append(
                {
                    "index": index,
                    "name": str(name),
                    "cross_section_csv": Path(channel["cross_section_csv"]),
                    "bank_shp_path": Path(channel["bank_shp_path"]),
                    "dtm_path": Path(channel["dtm_path"]) if channel.get("dtm_path") else None,
                    "banks_gdf": banks_gdf,
                    "centerline": centerline,
                    "processing_banks_gdf": banks_gdf.copy(),
                    "processing_centerline": centerline,
                }
            )

        junctions = DTMChannelModifier._detect_junctions(
            channels,
            junction_tolerance=junction_tolerance,
            network_connections=network_connections,
            centerline_gap_m=centerline_gap_m,
        )

        for junction in junctions:
            tributary = channels[junction["tributary_index"]]
            main = channels[junction["main_index"]]
            junction_point = Point(junction["x"], junction["y"])
            tributary["processing_banks_gdf"] = DTMChannelModifier._extend_tributary_banks_to_main(
                tributary["processing_banks_gdf"],
                main["processing_banks_gdf"],
                junction_point=junction_point,
            )
            tributary["processing_centerline"] = DTMChannelModifier.generate_centerline_from_banks(
                tributary["processing_banks_gdf"]
            ).geometry.iloc[0]
            if junction.get("extended_centerline") is not None:
                tributary["processing_centerline"] = junction["extended_centerline"]

        if junctions:
            merged_banks_gdf = DTMChannelModifier.build_connected_junction_banklines(channels)
        else:
            merged_banks_gdf = DTMChannelModifier.merge_junction_bank_polylines(channels)
        return {
            "channels": channels,
            "junctions": junctions,
            "merged_banks_gdf": merged_banks_gdf,
        }

    @staticmethod
    def merge_junction_bank_polylines(channels):
        rows = []
        crs = None
        for channel in channels:
            banks_gdf = channel["processing_banks_gdf"]
            if crs is None:
                crs = banks_gdf.crs
            for line_index, line in enumerate(DTMChannelModifier._line_strings(banks_gdf)):
                rows.append(
                    {
                        "Channel": channel["name"],
                        "BankId": line_index + 1,
                        "geometry": line,
                    }
                )

        combined = gpd.GeoDataFrame(rows, crs=crs)
        if combined.empty:
            return combined

        try:
            merged = DTMChannelModifier.clean_and_merge_banklines(
                combined,
                bridge_junctions=True,
            )
            merged["BankId"] = range(1, len(merged) + 1)
            return merged
        except Exception as exc:
            print(f"Warning: bank merge failed, exporting unmerged bank lines: {exc}")
            return combined

    @staticmethod
    def _detect_junctions(
        channels,
        junction_tolerance=50.0,
        network_connections=None,
        centerline_gap_m=0.5,
    ):
        if network_connections:
            return DTMChannelModifier._detect_network_csv_junctions(
                channels=channels,
                network_connections=network_connections,
                junction_tolerance=junction_tolerance,
                centerline_gap_m=centerline_gap_m,
            )

        junctions = []
        seen_pairs = set()

        for i in range(len(channels)):
            for j in range(i + 1, len(channels)):
                candidate_a = DTMChannelModifier._endpoint_to_centerline_candidate(
                    tributary=channels[i],
                    main=channels[j],
                )
                candidate_b = DTMChannelModifier._endpoint_to_centerline_candidate(
                    tributary=channels[j],
                    main=channels[i],
                )
                candidates = [
                    candidate
                    for candidate in [candidate_a, candidate_b]
                    if candidate is not None and candidate["is_mid_reach"]
                ]
                if not candidates:
                    continue

                best = min(candidates, key=lambda value: value["distance"])
                if best["distance"] > junction_tolerance:
                    continue

                pair_key = tuple(sorted([best["tributary_index"], best["main_index"]]))
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)

                junctions.append(
                    {
                        "main": best["main_name"],
                        "tributary": best["tributary_name"],
                        "main_index": best["main_index"],
                        "tributary_index": best["tributary_index"],
                        "tributary_endpoint": best["tributary_endpoint"],
                        "distance": round(float(best["distance"]), 3),
                        "x": float(best["junction_point"].x),
                        "y": float(best["junction_point"].y),
                        "main_fraction": round(float(best["main_fraction"]), 4),
                    }
                )

        return junctions

    @staticmethod
    def _detect_network_csv_junctions(
        channels,
        network_connections,
        junction_tolerance=50.0,
        centerline_gap_m=0.5,
    ):
        junctions = []
        seen_pairs = set()

        for connection in network_connections:
            tributary = DTMChannelModifier._find_channel_by_network_name(
                channels,
                connection["from"],
            )
            main = DTMChannelModifier._find_channel_by_network_name(
                channels,
                connection["to"],
            )
            if tributary is None or main is None:
                continue
            if tributary["index"] == main["index"]:
                continue

            pair_key = (tributary["index"], main["index"])
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)

            best = DTMChannelModifier._endpoint_to_centerline_candidate(
                tributary=tributary,
                main=main,
            )
            if best is None:
                continue

            if best["distance"] > junction_tolerance:
                print(
                    "Warning: network.csv junction "
                    f"{tributary['name']} -> {main['name']} is "
                    f"{best['distance']:.2f}m from the main centerline "
                    f"(tolerance {junction_tolerance:.2f}m). Extending by network rule."
                )

            extended_centerline = DTMChannelModifier._extend_line_endpoint_along_tangent_to_line(
                line=tributary["centerline"],
                endpoint_name=best["tributary_endpoint"],
                target_line=main["centerline"],
                gap_m=centerline_gap_m,
            )

            junctions.append(
                {
                    "main": main["name"],
                    "tributary": tributary["name"],
                    "main_index": main["index"],
                    "tributary_index": tributary["index"],
                    "tributary_endpoint": best["tributary_endpoint"],
                    "distance": round(float(best["distance"]), 3),
                    "x": float(best["junction_point"].x),
                    "y": float(best["junction_point"].y),
                    "main_fraction": round(float(best["main_fraction"]), 4),
                    "source": "network.csv",
                    "from": connection["from"],
                    "to": connection["to"],
                    "centerline_gap_m": float(centerline_gap_m),
                    "extended_centerline": extended_centerline,
                }
            )

        return junctions

    @staticmethod
    def read_network_connections(network_csv_path):
        if network_csv_path is None:
            return []

        network_csv_path = Path(network_csv_path)
        if not network_csv_path.exists():
            return []

        df = DTMChannelModifier._read_csv_auto(network_csv_path)
        if df.empty or len(df.columns) < 2:
            return []

        normalized_columns = {str(column).strip().casefold(): column for column in df.columns}
        from_column = normalized_columns.get("from") or df.columns[0]
        to_column = normalized_columns.get("to") or df.columns[1]

        connections = []
        for _, row in df.iterrows():
            from_name = str(row[from_column]).strip()
            to_name = str(row[to_column]).strip()
            if not from_name or not to_name or from_name.lower() == "nan" or to_name.lower() == "nan":
                continue
            connections.append({"from": from_name, "to": to_name})
        return connections

    @staticmethod
    def update_network_junction_coordinates(network_csv_path, junctions, dtm_path):
        """
        Writes detected junction coordinates back to network(s).csv.

        The x/y point is the same point used by the current junction detection
        logic. Elevation is sampled from the source DTM raster, not from the
        modified channel terrain.
        """
        if network_csv_path is None or not junctions:
            return None

        network_csv_path = Path(network_csv_path)
        if not network_csv_path.exists():
            return None

        df = DTMChannelModifier._read_csv_auto(network_csv_path)
        if df.empty or len(df.columns) < 2:
            return None

        normalized_columns = {str(column).strip().casefold(): column for column in df.columns}
        from_column = normalized_columns.get("from") or df.columns[0]
        to_column = normalized_columns.get("to") or df.columns[1]

        rename_columns = {}
        if from_column != "From":
            rename_columns[from_column] = "From"
        if to_column != "To":
            rename_columns[to_column] = "To"
        if rename_columns:
            df = df.rename(columns=rename_columns)

        for column in ("Easting", "Northing", "Elevation"):
            if column not in df.columns:
                df[column] = np.nan

        for junction in junctions:
            from_name = junction.get("from") or junction.get("tributary")
            to_name = junction.get("to") or junction.get("main")
            x = float(junction["x"])
            y = float(junction["y"])
            elevation = DTMChannelModifier._sample_raster_elevation(dtm_path, x, y)

            junction["easting"] = x
            junction["northing"] = y
            junction["elevation"] = elevation

            mask = df.apply(
                lambda row: (
                    DTMChannelModifier._network_names_match(row.get("From", ""), from_name)
                    and DTMChannelModifier._network_names_match(row.get("To", ""), to_name)
                ),
                axis=1,
            )
            if not mask.any():
                mask = df.apply(
                    lambda row: (
                        DTMChannelModifier._network_names_match(row.get("From", ""), junction.get("tributary", ""))
                        and DTMChannelModifier._network_names_match(row.get("To", ""), junction.get("main", ""))
                    ),
                    axis=1,
                )

            if not mask.any():
                continue

            df.loc[mask, "Easting"] = round(x, 3)
            df.loc[mask, "Northing"] = round(y, 3)
            df.loc[mask, "Elevation"] = round(elevation, 3) if np.isfinite(elevation) else np.nan

        df.to_csv(network_csv_path, index=False)
        return network_csv_path

    @staticmethod
    def _sample_raster_elevation(dtm_path, x, y):
        dtm_path = Path(dtm_path)
        if not dtm_path.exists():
            return float("nan")

        with rasterio.open(dtm_path) as dataset:
            left, bottom, right, top = dataset.bounds
            if not (left <= x <= right and bottom <= y <= top):
                return float("nan")

            value = float(next(dataset.sample([(float(x), float(y))]))[0])
            nodata = dataset.nodata
            if nodata is not None and np.isclose(value, nodata):
                return float("nan")
            return value

    @staticmethod
    def _find_channel_by_network_name(channels, name):
        for channel in channels:
            aliases = [
                channel.get("name", ""),
                Path(channel.get("cross_section_csv", "")).stem,
                Path(channel.get("bank_shp_path", "")).parent.name,
            ]
            if any(DTMChannelModifier._network_names_match(name, alias) for alias in aliases):
                return channel
        return None

    @staticmethod
    def _network_names_match(left, right):
        left_norm = re.sub(r"[^0-9A-Za-z]+", "", str(left)).upper()
        right_norm = re.sub(r"[^0-9A-Za-z]+", "", str(right)).upper()
        if not left_norm or not right_norm:
            return False
        return (
            left_norm == right_norm
            or left_norm.endswith(right_norm)
            or right_norm.endswith(left_norm)
        )

    @staticmethod
    def _extend_line_endpoint_along_tangent_to_line(line, endpoint_name, target_line, gap_m=0.5):
        coords = list(line.coords)
        if len(coords) < 2:
            return line

        endpoint_index = 0 if endpoint_name == "start" else -1
        endpoint_coord = coords[endpoint_index]
        endpoint = Point(endpoint_coord[:2])

        tangent = DTMChannelModifier._endpoint_outward_unit_vector(coords, endpoint_index)
        if tangent is None:
            return line

        endpoint_to_main = endpoint.distance(target_line)
        if endpoint_to_main <= max(float(gap_m), 0.0):
            return line

        ray_length = max(float(line.length), endpoint_to_main * 3.0, 100.0)
        ray_end = Point(
            endpoint.x + tangent[0] * ray_length,
            endpoint.y + tangent[1] * ray_length,
        )
        ray = LineString([(endpoint.x, endpoint.y), (ray_end.x, ray_end.y)])
        ray_point, _ = nearest_points(ray, target_line)
        travel_distance = ray.project(ray_point)

        # If the tangent ray intersects the main line, stop before touching it.
        if ray_point.distance(target_line) <= max(float(gap_m), 0.0):
            travel_distance = max(travel_distance - float(gap_m), 0.0)

        if travel_distance <= 1e-6:
            return line

        new_x = endpoint.x + tangent[0] * travel_distance
        new_y = endpoint.y + tangent[1] * travel_distance
        new_coord = DTMChannelModifier._coord_like_point(Point(new_x, new_y), endpoint_coord)

        if endpoint_index == 0:
            coords = [new_coord] + coords
        else:
            coords = coords + [new_coord]
        return LineString(coords)

    @staticmethod
    def _endpoint_outward_unit_vector(coords, endpoint_index):
        if len(coords) < 2:
            return None

        if endpoint_index == 0:
            vector = np.array(coords[0][:2], dtype=float) - np.array(coords[1][:2], dtype=float)
        else:
            vector = np.array(coords[-1][:2], dtype=float) - np.array(coords[-2][:2], dtype=float)

        norm = np.linalg.norm(vector)
        if norm <= 0:
            return None
        return vector / norm

    @staticmethod
    def _endpoint_to_centerline_candidate(tributary, main):
        tributary_line = tributary["centerline"]
        main_line = main["centerline"]
        if tributary_line.length <= 0 or main_line.length <= 0:
            return None

        best = None
        endpoints = [("start", tributary_line.coords[0]), ("end", tributary_line.coords[-1])]
        for endpoint_name, endpoint_coord in endpoints:
            endpoint = Point(endpoint_coord[:2])
            main_distance = main_line.project(endpoint)
            main_fraction = main_distance / main_line.length if main_line.length else 0.0
            junction_point = main_line.interpolate(main_distance)
            distance = endpoint.distance(junction_point)
            is_mid_reach = 0.05 <= main_fraction <= 0.95
            candidate = {
                "tributary_index": tributary["index"],
                "main_index": main["index"],
                "tributary_name": tributary["name"],
                "main_name": main["name"],
                "tributary_endpoint": endpoint_name,
                "junction_point": junction_point,
                "main_fraction": main_fraction,
                "distance": distance,
                "is_mid_reach": is_mid_reach,
            }
            if best is None or candidate["distance"] < best["distance"]:
                best = candidate

        return best

    @staticmethod
    def _extend_tributary_banks_to_main(tributary_banks_gdf, main_banks_gdf, junction_point):
        tributary_lines = DTMChannelModifier._line_strings(tributary_banks_gdf)[:2]
        main_lines = DTMChannelModifier._line_strings(main_banks_gdf)[:2]
        if len(tributary_lines) < 2 or len(main_lines) < 2:
            return tributary_banks_gdf

        tributary_endpoints = []
        for line in tributary_lines:
            coords = list(line.coords)
            start_distance = Point(coords[0][:2]).distance(junction_point)
            end_distance = Point(coords[-1][:2]).distance(junction_point)
            endpoint_index = 0 if start_distance < end_distance else -1
            tributary_endpoints.append((line, endpoint_index, Point(coords[endpoint_index][:2])))

        assignments = [(0, 1), (1, 0)]
        best_assignment = min(
            assignments,
            key=lambda assignment: sum(
                tributary_endpoints[idx][2].distance(
                    nearest_points(tributary_endpoints[idx][2], main_lines[assignment[idx]])[1]
                )
                for idx in range(2)
            ),
        )

        extended_lines = []
        for index, (line, endpoint_index, endpoint) in enumerate(tributary_endpoints):
            target_line = main_lines[best_assignment[index]]
            _, target_point = nearest_points(endpoint, target_line)
            extended_lines.append(
                DTMChannelModifier._line_extended_at_endpoint(
                    line,
                    endpoint_index=endpoint_index,
                    target_point=target_point,
                )
            )

        return gpd.GeoDataFrame(
            {
                "Name": ["Tributary Bank 1 Extended", "Tributary Bank 2 Extended"],
            },
            geometry=extended_lines,
            crs=tributary_banks_gdf.crs,
        )

    @staticmethod
    def _line_extended_at_endpoint(line, endpoint_index, target_point):
        coords = list(line.coords)
        sample = coords[endpoint_index]
        target_coord = DTMChannelModifier._coord_like_point(target_point, sample)
        if Point(sample[:2]).distance(Point(target_coord[:2])) <= 1e-6:
            return line

        if endpoint_index == 0:
            coords = [target_coord] + coords
        else:
            coords = coords + [target_coord]
        return LineString(coords)

    @staticmethod
    def _coord_like_point(point, sample_coord):
        if len(sample_coord) >= 3:
            return (float(point.x), float(point.y), float(sample_coord[2]))
        return (float(point.x), float(point.y))

    @staticmethod
    def _combined_channel_bounds(channels, buffer_m=20.0):
        minx_values, miny_values, maxx_values, maxy_values = [], [], [], []

        for channel in channels:
            df = DTMChannelModifier._read_csv_auto(
                channel["cross_section_csv"],
                required_columns=("X", "Y"),
            )
            minx_values.append(float(df["X"].min()))
            maxx_values.append(float(df["X"].max()))
            miny_values.append(float(df["Y"].min()))
            maxy_values.append(float(df["Y"].max()))

            bounds = channel["processing_banks_gdf"].total_bounds
            minx_values.append(float(bounds[0]))
            miny_values.append(float(bounds[1]))
            maxx_values.append(float(bounds[2]))
            maxy_values.append(float(bounds[3]))

        return (
            min(minx_values) - buffer_m,
            min(miny_values) - buffer_m,
            max(maxx_values) + buffer_m,
            max(maxy_values) + buffer_m,
        )

    @staticmethod
    def _minimum_raster_stack(modifiers):
        if not modifiers:
            raise ValueError("No channel rasters were produced.")

        stack = np.stack([modifier.dtm_data.astype("float32") for modifier in modifiers])
        nodata = modifiers[0].dtm_meta.get("nodata")
        if nodata is not None:
            stack = np.where(np.isclose(stack, nodata), np.nan, stack)

        with np.errstate(all="ignore"):
            final = np.nanmin(stack, axis=0)

        if nodata is not None:
            final = np.where(np.isnan(final), nodata, final)
        else:
            final = np.where(np.isnan(final), modifiers[0].dtm_data, final)

        return final.astype("float32")

    @staticmethod
    def _write_modifier_geotiff(modifier, output_path):
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        meta = modifier.dtm_meta.copy()
        meta.update(
            {
                "driver": "GTiff",
                "height": modifier.dtm_data.shape[0],
                "width": modifier.dtm_data.shape[1],
                "count": 1,
                "dtype": "float32",
                "crs": modifier.dtm_crs,
                "transform": modifier.dtm_transform,
            }
        )
        with rasterio.open(output_path, "w", **meta) as dest:
            dest.write(modifier.dtm_data.astype("float32"), 1)

    @staticmethod
    def _delete_vector_sidecars(path):
        path = Path(path)
        if not path.parent.exists():
            return
        for sidecar in path.parent.glob(f"{path.stem}.*"):
            try:
                sidecar.unlink()
            except OSError:
                pass

    @staticmethod
    def _export_network_centerlines(channels, output_path):
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {
                "Channel": channel["name"],
                "geometry": channel["processing_centerline"],
            }
            for channel in channels
        ]
        crs = channels[0]["processing_banks_gdf"].crs if channels else None
        gpd.GeoDataFrame(rows, crs=crs).to_file(output_path)

    @staticmethod
    def _export_network_perimeter(channels, output_path, offset_m=500.0, network=None):
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        perimeter = DTMChannelModifier._build_clipped_network_perimeter(
            channels=channels,
            offset_m=offset_m,
            network=network,
        )
        crs = channels[0]["processing_banks_gdf"].crs if channels else None
        gpd.GeoDataFrame(
            [{"Name": f"Network Study Perimeter {offset_m}m", "geometry": perimeter}],
            crs=crs,
        ).to_file(output_path)

    @staticmethod
    def _build_clipped_network_perimeter(channels, offset_m=500.0, network=None):
        if not channels:
            raise ValueError("At least one channel is required to export a study perimeter.")

        network = network or {"channels": channels, "junctions": []}
        junctions = network.get("junctions", [])
        main_indices = {int(junction["main_index"]) for junction in junctions}
        tributary_by_index = {
            int(junction["tributary_index"]): junction
            for junction in junctions
        }

        clipped_polygons = []
        for channel in channels:
            centerline = channel.get("processing_centerline") or channel["centerline"]
            if centerline is None or centerline.is_empty:
                continue

            channel_perimeter = centerline.buffer(offset_m)
            if channel_perimeter.is_empty:
                continue

            bank_lines = DTMChannelModifier._line_strings(channel["banks_gdf"])
            sections = DTMChannelModifier._cross_sections_in_file_order(
                cross_section_csv=channel["cross_section_csv"],
                centerline=channel["centerline"],
                bank_lines=bank_lines,
            )
            if not sections:
                clipped_polygons.append(channel_perimeter)
                continue

            channel_index = int(channel.get("index", len(clipped_polygons)))
            is_main = channel_index in main_indices
            tributary_junction = tributary_by_index.get(channel_index)

            if not junctions or is_main or tributary_junction is None:
                channel_perimeter = DTMChannelModifier._clip_perimeter_between_cross_sections(
                    polygon=channel_perimeter,
                    channel_centerline=channel["centerline"],
                    start_section=sections[0],
                    end_section=sections[-1],
                )
            else:
                junction_point = Point(
                    float(tributary_junction["x"]),
                    float(tributary_junction["y"]),
                )
                channel_perimeter = DTMChannelModifier._clip_perimeter_at_cross_section(
                    polygon=channel_perimeter,
                    cut_section=sections[-1],
                    keep_point=junction_point,
                )

            if not channel_perimeter.is_empty:
                clipped_polygons.append(channel_perimeter)

        if not clipped_polygons:
            centerlines = [channel["processing_centerline"] for channel in channels]
            return unary_union(centerlines).buffer(offset_m)

        return unary_union(clipped_polygons)

    @staticmethod
    def _cross_sections_in_file_order(cross_section_csv, centerline, bank_lines=None):
        df = DTMChannelModifier._read_csv_auto(
            cross_section_csv,
            required_columns=("X", "Y"),
        )
        group_cols = [column for column in ["River", "Reach", "Station"] if column in df.columns]
        if not group_cols:
            group_cols = ["Station"] if "Station" in df.columns else []

        grouped = df.groupby(group_cols, sort=False) if group_cols else [(None, df)]
        sections = []
        for name, group in grouped:
            if len(group) < 2:
                continue
            coord_columns = ["X", "Y", "Z"] if "Z" in group.columns else ["X", "Y"]
            line = LineString(group[coord_columns].to_numpy(dtype=float))
            if line.length <= 0:
                continue
            station_name = str(name if not isinstance(name, tuple) else name[-1])
            center_point = DTMChannelModifier._cross_section_center_point_from_centerline(
                line,
                centerline,
                label=station_name,
                bank_lines=bank_lines,
            )
            sections.append(
                {
                    "station": station_name,
                    "line": line,
                    "center_point": center_point,
                    "centerline_measure": float(centerline.project(center_point)),
                }
            )
        return sections

    @staticmethod
    def _clip_perimeter_between_cross_sections(
        polygon,
        channel_centerline,
        start_section,
        end_section,
    ):
        if polygon.is_empty:
            return polygon

        start_measure = float(start_section["centerline_measure"])
        end_measure = float(end_section["centerline_measure"])
        if abs(end_measure - start_measure) <= 1e-6:
            return polygon

        start_keep = DTMChannelModifier._centerline_point_toward_measure(
            channel_centerline,
            from_measure=start_measure,
            toward_measure=end_measure,
        )
        end_keep = DTMChannelModifier._centerline_point_toward_measure(
            channel_centerline,
            from_measure=end_measure,
            toward_measure=start_measure,
        )

        clipped = DTMChannelModifier._clip_perimeter_at_cross_section(
            polygon=polygon,
            cut_section=start_section,
            keep_point=start_keep,
        )
        clipped = DTMChannelModifier._clip_perimeter_at_cross_section(
            polygon=clipped,
            cut_section=end_section,
            keep_point=end_keep,
        )
        return clipped

    @staticmethod
    def _centerline_point_toward_measure(centerline, from_measure, toward_measure):
        length = max(float(centerline.length), 0.0)
        from_measure = float(np.clip(from_measure, 0.0, length))
        toward_measure = float(np.clip(toward_measure, 0.0, length))
        direction = 1.0 if toward_measure >= from_measure else -1.0
        step = min(max(length * 0.01, 0.5), 5.0)
        target_measure = from_measure + direction * step
        if direction > 0:
            target_measure = min(target_measure, toward_measure, length)
        else:
            target_measure = max(target_measure, toward_measure, 0.0)
        if abs(target_measure - from_measure) <= 1e-9:
            target_measure = toward_measure
        return centerline.interpolate(target_measure)

    @staticmethod
    def _clip_perimeter_at_cross_section(polygon, cut_section, keep_point):
        if polygon.is_empty:
            return polygon

        extended_line = DTMChannelModifier._extended_cross_section_line(
            cut_section["line"],
            polygon,
        )
        if extended_line is None or extended_line.is_empty:
            return polygon

        try:
            pieces = list(split(polygon, extended_line).geoms)
        except Exception:
            return polygon

        polygonal_pieces = [
            piece
            for piece in pieces
            if piece.geom_type in {"Polygon", "MultiPolygon"} and not piece.is_empty
        ]
        if len(polygonal_pieces) <= 1:
            return polygon

        keep_point = Point(float(keep_point.x), float(keep_point.y))
        tolerance = max(polygon.length * 1e-9, 1e-6)
        selected = [
            piece
            for piece in polygonal_pieces
            if piece.buffer(tolerance).contains(keep_point)
            or piece.buffer(tolerance).touches(keep_point)
        ]
        if not selected:
            nearest_piece = min(polygonal_pieces, key=lambda piece: piece.distance(keep_point))
            selected = [nearest_piece]

        return unary_union(selected)

    @staticmethod
    def _extended_cross_section_line(cross_section_line, polygon):
        coords = list(cross_section_line.coords)
        if len(coords) < 2:
            return None

        start = np.asarray(coords[0][:2], dtype=float)
        end = np.asarray(coords[-1][:2], dtype=float)
        vector = end - start
        norm = np.linalg.norm(vector)
        if norm <= 0:
            return None
        unit = vector / norm

        minx, miny, maxx, maxy = polygon.bounds
        diagonal = float(np.hypot(maxx - minx, maxy - miny))
        extension = max(diagonal * 3.0, float(cross_section_line.length) * 3.0, 100.0)
        extended_start = start - unit * extension
        extended_end = end + unit * extension
        return LineString(
            [
                (float(extended_start[0]), float(extended_start[1])),
                (float(extended_end[0]), float(extended_end[1])),
            ]
        )

    @staticmethod
    def _export_connected_bank_products(
        network,
        output_dir,
        clip_buffer_m=5.0,
        nearest_cross_section_count=2,
    ):
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        products = []

        for junction in network.get("junctions", []):
            main = network["channels"][junction["main_index"]]
            tributary = network["channels"][junction["tributary_index"]]
            safe_pair_name = (
                f"{DTMChannelModifier._safe_name(tributary['name'])}"
                f"__{DTMChannelModifier._safe_name(main['name'])}"
            )
            junction_point = Point(float(junction["x"]), float(junction["y"]))
            merged_banks = DTMChannelModifier.build_connected_junction_banklines(
                channels=[tributary, main],
            )

            merged_path = output_dir / f"{safe_pair_name}_SEV_USTU_combined.shp"
            merged_banks.to_file(merged_path)

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
            clipped_path = output_dir / f"{safe_pair_name}_SEV_USTU_junction_clipped.shp"
            clipped_banks.to_file(clipped_path)

            products.append(
                {
                    "main": main["name"],
                    "tributary": tributary["name"],
                    "merged_banks_shp": str(merged_path),
                    "junction_clipped_banks_shp": str(clipped_path),
                }
            )

        return products

    @staticmethod
    def build_connected_junction_banklines(channels, proximity_tolerance=1.0):
        rows = []
        crs = None
        all_lines = []

        for channel in channels:
            bank_gdf = gpd.read_file(channel["bank_shp_path"])
            if crs is None:
                crs = bank_gdf.crs
            for line in DTMChannelModifier._line_strings(bank_gdf):
                all_lines.append(line)

        merged_lines = DTMChannelModifier._join_lines_by_endpoint_proximity(
            all_lines,
            tolerance=proximity_tolerance,
        )
        for index, line in enumerate(merged_lines, start=1):
            rows.append(
                {
                    "LineId": index,
                    "Source": "raw_SEV_USTU",
                    "geometry": line,
                }
            )

        return gpd.GeoDataFrame(rows, geometry="geometry", crs=crs)

    @staticmethod
    def _join_gdf_line_features_by_proximity(gdf, tolerance=1.0):
        if gdf is None or gdf.empty:
            return gdf

        joined_lines = DTMChannelModifier._join_lines_by_endpoint_proximity(
            DTMChannelModifier._line_strings(gdf),
            tolerance=tolerance,
        )
        rows = [
            {
                "LineId": index,
                "Source": "junction_clip",
                "geometry": line,
            }
            for index, line in enumerate(joined_lines, start=1)
        ]
        return gpd.GeoDataFrame(rows, geometry="geometry", crs=gdf.crs)

    @staticmethod
    def _join_lines_by_endpoint_proximity(lines, tolerance=1.0):
        if not lines:
            return []

        snapped_lines = DTMChannelModifier._snap_line_endpoints(lines, tolerance=tolerance)
        try:
            merged = linemerge(unary_union(snapped_lines))
            merged_lines = DTMChannelModifier._line_strings(merged)
            return merged_lines if merged_lines else snapped_lines
        except Exception:
            return snapped_lines

    @staticmethod
    def _snap_line_endpoints(lines, tolerance=1.0):
        endpoints = []
        for line_index, line in enumerate(lines):
            coords = list(line.coords)
            if not coords:
                continue
            endpoints.append((line_index, 0, Point(coords[0][:2])))
            endpoints.append((line_index, -1, Point(coords[-1][:2])))

        replacements = {}
        visited = set()
        for index, endpoint in enumerate(endpoints):
            if index in visited:
                continue
            cluster = [endpoint]
            visited.add(index)
            for other_index in range(index + 1, len(endpoints)):
                if other_index in visited:
                    continue
                if endpoint[2].distance(endpoints[other_index][2]) <= float(tolerance):
                    cluster.append(endpoints[other_index])
                    visited.add(other_index)

            if len(cluster) == 1:
                continue
            mean_x = sum(item[2].x for item in cluster) / len(cluster)
            mean_y = sum(item[2].y for item in cluster) / len(cluster)
            for line_index, endpoint_index, _ in cluster:
                replacements[(line_index, endpoint_index)] = Point(mean_x, mean_y)

        snapped = []
        for line_index, line in enumerate(lines):
            coords = list(line.coords)
            if not coords:
                continue
            if (line_index, 0) in replacements:
                coords[0] = DTMChannelModifier._coord_like_point(replacements[(line_index, 0)], coords[0])
            if (line_index, -1) in replacements:
                coords[-1] = DTMChannelModifier._coord_like_point(replacements[(line_index, -1)], coords[-1])
            snapped.append(LineString(coords))
        return snapped

    @staticmethod
    def _junction_bank_lines_between_cross_sections(tributary, main, junction, junction_point):
        crs = main["banks_gdf"].crs or tributary["banks_gdf"].crs
        rows = []

        main_range = DTMChannelModifier._main_junction_cross_section_range(
            channel=main,
            junction_point=junction_point,
        )
        tributary_range = DTMChannelModifier._tributary_junction_cross_section_range(
            channel=tributary,
            endpoint_name=junction["tributary_endpoint"],
        )

        for role, channel, measure_range in (
            ("main", main, main_range),
            ("tributary", tributary, tributary_range),
        ):
            if measure_range is None:
                continue
            bank_gdf = gpd.read_file(channel["bank_shp_path"])
            for bank_index, bank_line in enumerate(DTMChannelModifier._line_strings(bank_gdf), start=1):
                selected_lines = DTMChannelModifier._line_parts_by_centerline_measure_range(
                    bank_line=bank_line,
                    centerline=channel["centerline"],
                    measure_range=measure_range,
                )
                for part_index, line in enumerate(selected_lines, start=1):
                    rows.append(
                        {
                            "Channel": channel["name"][:80],
                            "Role": role,
                            "BankId": bank_index,
                            "PartId": part_index,
                            "FromM": float(measure_range[0]),
                            "ToM": float(measure_range[1]),
                            "geometry": line,
                        }
                    )

        columns = ["Channel", "Role", "BankId", "PartId", "FromM", "ToM", "geometry"]
        return gpd.GeoDataFrame(rows, columns=columns, geometry="geometry", crs=crs)

    @staticmethod
    def _main_junction_cross_section_range(channel, junction_point):
        measures = DTMChannelModifier._cross_section_centerline_measures(
            cross_section_csv=channel["cross_section_csv"],
            centerline=channel["centerline"],
            bank_lines=DTMChannelModifier._line_strings(channel["banks_gdf"]),
        )
        if len(measures) < 2:
            return None
        junction_measure = channel["centerline"].project(junction_point)
        lower = [measure for measure in measures if measure < junction_measure]
        upper = [measure for measure in measures if measure > junction_measure]

        if lower and upper:
            return (max(lower), min(upper))
        nearest = sorted(measures, key=lambda measure: abs(measure - junction_measure))[:2]
        if len(nearest) < 2:
            return None
        return (min(nearest), max(nearest))

    @staticmethod
    def _tributary_junction_cross_section_range(channel, endpoint_name):
        measures = DTMChannelModifier._cross_section_centerline_measures(
            cross_section_csv=channel["cross_section_csv"],
            centerline=channel["centerline"],
            bank_lines=DTMChannelModifier._line_strings(channel["banks_gdf"]),
        )
        if not measures:
            return None

        endpoint_measure = 0.0 if endpoint_name == "start" else channel["centerline"].length
        if endpoint_name == "start":
            candidates = [measure for measure in measures if measure >= endpoint_measure]
            section_measure = min(candidates) if candidates else min(measures, key=lambda measure: abs(measure - endpoint_measure))
        else:
            candidates = [measure for measure in measures if measure <= endpoint_measure]
            section_measure = max(candidates) if candidates else min(measures, key=lambda measure: abs(measure - endpoint_measure))

        return (min(endpoint_measure, section_measure), max(endpoint_measure, section_measure))

    @staticmethod
    def _cross_section_centerline_measures(cross_section_csv, centerline, bank_lines=None):
        df = DTMChannelModifier._read_csv_auto(
            cross_section_csv,
            required_columns=("X", "Y", "Z"),
        )
        group_cols = [col for col in ["River", "Reach", "Station"] if col in df.columns]
        if not group_cols:
            group_cols = ["Station"] if "Station" in df.columns else []

        grouped = df.groupby(group_cols) if group_cols else [(None, df)]
        measures = []
        for _, group in grouped:
            if len(group) < 2 or not {"X", "Y"}.issubset(group.columns):
                continue
            coords = group[["X", "Y"]].to_numpy(dtype=float)
            line = LineString(coords)
            center_point = DTMChannelModifier._cross_section_center_point_from_centerline(
                line,
                centerline,
                bank_lines=bank_lines,
            )
            measures.append(float(centerline.project(center_point)))

        return sorted(set(round(measure, 6) for measure in measures))

    @staticmethod
    def _line_parts_by_centerline_measure_range(bank_line, centerline, measure_range):
        start_measure, end_measure = sorted([float(measure_range[0]), float(measure_range[1])])
        if abs(end_measure - start_measure) <= 1e-6:
            return []

        selected_segments = []
        coords = list(bank_line.coords)
        for index in range(len(coords) - 1):
            segment = LineString([coords[index], coords[index + 1]])
            if segment.length <= 0:
                continue
            midpoint = segment.interpolate(0.5, normalized=True)
            measure = centerline.project(midpoint)
            if start_measure <= measure <= end_measure:
                selected_segments.append(segment)

        if not selected_segments:
            return []

        try:
            merged = linemerge(unary_union(selected_segments))
            return DTMChannelModifier._line_strings(merged)
        except Exception:
            return selected_segments

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

    # =========================================================
    # STATIC METHOD TOOLS
    # =========================================================
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
                skewness_correction=skewness_correction,
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
            
            d1 = max(0, d - 0.1)
            d2 = min(cl_length, d + 0.1)
            if d1 == d2:
                left_pts.append((pt.x, pt.y))
                right_pts.append((pt.x, pt.y))
                continue
                
            p1 = centerline.interpolate(d1)
            p2 = centerline.interpolate(d2)
            
            dx = p2.x - p1.x
            dy = p2.y - p1.y
            length = np.hypot(dx, dy)
            if length == 0:
                nx = ny = 0
            else:
                nx = -dy / length
                ny = dx / length
                
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
