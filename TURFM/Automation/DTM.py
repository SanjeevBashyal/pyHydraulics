import os
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
from shapely.ops import nearest_points


class DTMChannelModifier:
    def __init__(
        self,
        dtm_path: str,
        csv_path: str,
        output_path: str,
        target_res: float = 0.1,
        buffer_m: float = 20.0,
    ):
        """
        Initializes the DTM Modifier.
        """
        self.dtm_path = dtm_path
        self.csv_path = csv_path
        self.output_path = output_path
        self.target_res = target_res
        self.buffer_m = buffer_m  # Acts as the blend/feathering distance

        self.out_dir = os.path.dirname(os.path.abspath(output_path))

        self.dtm_data = None
        self.dtm_transform = None
        self.dtm_crs = None
        self.dtm_meta = None

    def _read_survey_and_get_bounds(self):
        print("Reading survey data and determining processing window...")
        self.raw_df = pd.read_csv(self.csv_path)
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

        right_banks.reverse()
        self.channel_polygon = Polygon(left_banks + right_banks)

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

        return best_j

    def _interpolate_and_merge(self):
        print("Applying high-performance mathematical interpolation & blending...")
        height, width = self.dtm_data.shape
        cols, rows = np.meshgrid(np.arange(width), np.arange(height))

        # Grid coordinates
        xs, ys = self.dtm_transform * (cols + 0.5, rows + 0.5)
        pts_xy = np.column_stack((xs.ravel(), ys.ravel()))

        # 1. Bank polygon distance transform logic
        bank_mask = rasterize(
            [self.channel_polygon],
            out_shape=(height, width),
            transform=self.dtm_transform,
            fill=0,
            default_value=1,
            dtype="uint8",
        )

        # distance_transform_edt computes pixel distance to closest True value
        outside_mask = bank_mask == 0
        dist_pixels = distance_transform_edt(outside_mask)
        dist_m = (dist_pixels * self.target_res).ravel()

        # 2. Assign Global W1 and W2 weights based on blend distance
        # W1 = 1 inside banks. Fades to 0 at self.buffer_m distance.
        W1 = np.clip(1.0 - (dist_m / self.buffer_m), 0.0, 1.0)
        W2 = 1.0 - W1

        # Optimize processing by only solving math for pixels where W1 > 0
        valid_mask = W1 > 0
        valid_pts = pts_xy[valid_mask]
        valid_W1 = W1[valid_mask]
        valid_W2 = W2[valid_mask]

        # Find exactly which longitudinal reach every valid pixel belongs to
        best_j = self._get_bracketing_cs(valid_pts, self.cl_coords)

        cs_elevs = np.zeros(len(valid_pts))

        # 3. Apply Cross Section Mathematical Formula
        for j in range(len(self.cs_coords_list) - 1):
            mask_j = best_j == j
            if not np.any(mask_j):
                continue

            pts_in_reach = valid_pts[mask_j]

            # Project to CS 1 (upstream)
            D1, E1 = self._project_points_to_cs(pts_in_reach, self.cs_coords_list[j])

            # Project to CS 2 (downstream)
            D2, E2 = self._project_points_to_cs(
                pts_in_reach, self.cs_coords_list[j + 1]
            )

            # Calculate local CS weights
            w11 = 1.0 / (
                D1 + 1e-6
            )  # 1e-6 prevents division by zero if pixel is exactly on the line
            w12 = 1.0 / (D2 + 1e-6)

            # User formula: (w11 * CS1_ELEV + w12 * CS2_ELEV) / (w11 + w12)
            reach_elev = (w11 * E1 + w12 * E2) / (w11 + w12)
            cs_elevs[mask_j] = reach_elev

        # 4. Final Blending Formula
        old_elevs = self.dtm_data.ravel()[valid_mask]

        # User formula: (W1 * CS_Elev + W2 * Old_Elev) / (W1 + W2) (Since W1+W2=1, denominator drops out)
        new_elevs = valid_W1 * cs_elevs + valid_W2 * old_elevs

        # Merge back into DTM grid
        modified_dtm_flat = self.dtm_data.ravel().copy()
        modified_dtm_flat[valid_mask] = new_elevs
        self.modified_dtm = modified_dtm_flat.reshape((height, width))

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
        self._read_survey_and_get_bounds()
        self._resample_dtm_window()
        self._process_survey_geometry()
        self._interpolate_and_merge()
        self._export_dtm()
        self._export_shapefiles()
        print("\nAll processing complete successfully!")

    # =========================================================
    # STATIC METHOD TOOLS
    # =========================================================
    @staticmethod
    def generate_centerline_from_banks(
        bank_shp_path: str, output_shp_path: str, step_m: float = 1.0
    ):
        print(
            f"\nGenerating mathematically equidistant centerline from: {bank_shp_path}..."
        )
        banks_gdf = gpd.read_file(bank_shp_path)

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
        center_gdf.to_file(output_shp_path)
        print(f"Equidistant centerline successfully saved to: {output_shp_path}")

    @staticmethod
    def _get_outward_offset_line(target_line, reference_line, dist):
        """Helper to find the correct 'outward' offset line while preserving 3D."""
        off_left = target_line.parallel_offset(dist, "left")
        off_right = target_line.parallel_offset(dist, "right")

        outward_line = (
            off_left
            if off_left.distance(reference_line) > off_right.distance(reference_line)
            else off_right
        )

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
                elif geom.geom_type == "MultiLineString":
                    return MultiLineString([restore_z(part) for part in geom.geoms])
                return geom

            outward_line = restore_z(outward_line)

        return outward_line

    @staticmethod
    def offset_bank_lines_outwards(
        bank_shp_path: str, output_shp_path: str, offset_m: float = 0.2
    ):
        print(
            f"\nOffsetting bank lines outwards by {offset_m}m from: {bank_shp_path}..."
        )
        banks_gdf = gpd.read_file(bank_shp_path)

        lines = []
        for geom in banks_gdf.geometry:
            if geom.geom_type == "LineString":
                lines.append(geom)
            elif geom.geom_type == "MultiLineString":
                lines.extend(geom.geoms)

        if len(lines) < 2:
            raise ValueError("The shapefile must contain at least two line geometries.")

        line1, line2 = lines[0], lines[1]

        new_line1 = DTMChannelModifier._get_outward_offset_line(line1, line2, offset_m)
        new_line2 = DTMChannelModifier._get_outward_offset_line(line2, line1, offset_m)

        offset_gdf = gpd.GeoDataFrame(
            {"Name": ["Bank 1 Offset Outward", "Bank 2 Offset Outward"]},
            geometry=[new_line1, new_line2],
            crs=banks_gdf.crs,
        )
        offset_gdf.to_file(output_shp_path)
        print(f"Outward offset bank lines successfully saved to: {output_shp_path}")

    @staticmethod
    def create_polygon_mask_from_banks(
        bank_shp_path: str, output_shp_path: str, offset_m: float = 0.2
    ):
        """
        Creates a closed polygon mask spanning between the two bank lines.
        Automatically offsets the lines outwards by offset_m before creating the polygon.
        """
        print(
            f"\nCreating polygon mask from banks (offset by {offset_m}m) from: {bank_shp_path}..."
        )
        banks_gdf = gpd.read_file(bank_shp_path)

        lines = []
        for geom in banks_gdf.geometry:
            if geom.geom_type == "LineString":
                lines.append(geom)
            elif geom.geom_type == "MultiLineString":
                lines.extend(geom.geoms)

        if len(lines) < 2:
            raise ValueError("The shapefile must contain at least two line geometries.")

        line1, line2 = lines[0], lines[1]

        # 1. Offset the lines outwards
        new_line1 = DTMChannelModifier._get_outward_offset_line(line1, line2, offset_m)
        new_line2 = DTMChannelModifier._get_outward_offset_line(line2, line1, offset_m)

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
        poly_gdf.to_file(output_shp_path)
        print(f"Mask polygon successfully saved to: {output_shp_path}")
