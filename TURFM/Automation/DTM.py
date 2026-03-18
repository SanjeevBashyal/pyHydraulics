import os
import numpy as np
import pandas as pd
import rasterio
import geopandas as gpd
from rasterio.enums import Resampling
from rasterio.windows import from_bounds
from rasterio.transform import Affine
from rasterio.features import geometry_mask
from scipy.interpolate import griddata
from shapely.geometry import Polygon, LineString, Point
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

        :param dtm_path: Path to the input DTM TIFF file.
        :param csv_path: Path to the input Cross Section CSV file.
        :param output_path: Path to save the modified cropped DTM.
        :param target_res: Target resolution for the output DTM (default 0.1m).
        :param buffer_m: Buffer in meters to add around the survey bounding box.
        """
        self.dtm_path = dtm_path
        self.csv_path = csv_path
        self.output_path = output_path
        self.target_res = target_res
        self.buffer_m = buffer_m

        # Directory to save shapefiles (same folder as output_path)
        self.out_dir = os.path.dirname(os.path.abspath(output_path))

        self.dtm_data = None
        self.dtm_transform = None
        self.dtm_crs = None
        self.dtm_meta = None

    def _read_survey_and_get_bounds(self):
        """Reads the CSV, processes vertical walls, and determines the bounding box."""
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
        print(f"Survey Corridor Bounds (with {self.buffer_m}m buffer): {self.bounds}")

    def _resample_dtm_window(self):
        """Extracts only the required window from the DTM and resamples it."""
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
        """Extracts boundary polygons, thalwegs, and prepares shapefile geometries."""
        print("Processing channel boundary and generating shapefile geometries...")
        left_banks = []
        right_banks = []
        thalweg_pts = []

        self.xs_lines_data = []
        self.xs_centers_data = []

        stations = self.raw_df["Station"].unique()
        for stat in stations:
            # 1. Generate Cross Section Lines
            stat_raw = self.raw_df[self.raw_df["Station"] == stat]
            line_coords = stat_raw[["X", "Y", "Z"]].values
            if len(line_coords) >= 2:
                self.xs_lines_data.append(
                    {"Station": stat, "geometry": LineString(line_coords)}
                )

            # 2. Find Center of Deepest Point(s)
            min_z = stat_raw["Z"].min()
            lowest_pts = stat_raw[stat_raw["Z"] == min_z]

            first_pt = lowest_pts.iloc[0]
            last_pt = lowest_pts.iloc[-1]
            mid_x = (first_pt["X"] + last_pt["X"]) / 2.0
            mid_y = (first_pt["Y"] + last_pt["Y"]) / 2.0

            self.xs_centers_data.append(
                {
                    "Station": stat,
                    "Z_min": min_z,
                    "geometry": Point(mid_x, mid_y, min_z),
                }
            )

            # 3. Setup Boundary Polygon for DTM Interpolation
            stat_filtered = self.df[self.df["Station"] == stat].copy()
            left_banks.append((stat_filtered.iloc[0]["X"], stat_filtered.iloc[0]["Y"]))
            right_banks.append(
                (stat_filtered.iloc[-1]["X"], stat_filtered.iloc[-1]["Y"])
            )

            thalweg_pts.append((mid_x, mid_y, min_z))

        sorted_centers = sorted(self.xs_centers_data, key=lambda x: x["Station"])
        centerline_coords = [
            (pt["geometry"].x, pt["geometry"].y, pt["geometry"].z)
            for pt in sorted_centers
        ]
        self.centerline_data = [
            {"Name": "River Centerline", "geometry": LineString(centerline_coords)}
        ]

        right_banks.reverse()
        self.channel_polygon = Polygon(left_banks + right_banks)

        # Call the static method
        densified_thalweg = DTMChannelModifier._densify_3d_line(
            thalweg_pts, step=self.target_res * 5
        )

        self.final_points = np.vstack(
            [self.df[["X", "Y", "Z"]].values, densified_thalweg]
        )

    @staticmethod
    def _densify_3d_line(points, step=0.5):
        """Interpolates points along a 3D line."""
        densified = []
        for i in range(len(points) - 1):
            p1 = np.array(points[i])
            p2 = np.array(points[i + 1])
            dist = np.linalg.norm(p2[:2] - p1[:2])

            if dist <= step:
                densified.append(p1)
                continue

            num_points = int(dist // step)
            for j in range(num_points):
                fraction = j / num_points
                interp_pt = p1 + (p2 - p1) * fraction
                densified.append(interp_pt)

        densified.append(points[-1])
        return np.array(densified)

    def _interpolate_and_merge(self):
        """Interpolates the survey data onto the cropped DTM grid and merges them."""
        print("Interpolating cross-sections and merging with DTM...")
        height, width = self.dtm_data.shape
        cols, rows = np.meshgrid(np.arange(width), np.arange(height))

        xs, ys = self.dtm_transform * (cols + 0.5, rows + 0.5)
        points_xy = self.final_points[:, :2]
        points_z = self.final_points[:, 2]

        interpolated_channel = griddata(points_xy, points_z, (xs, ys), method="linear")
        interpolated_channel = interpolated_channel.reshape((height, width))

        mask = geometry_mask(
            [self.channel_polygon],
            transform=self.dtm_transform,
            invert=True,
            out_shape=(height, width),
        )
        valid_interp = mask & ~np.isnan(interpolated_channel)
        self.modified_dtm = np.where(valid_interp, interpolated_channel, self.dtm_data)

    def _export_dtm(self):
        """Writes the modified, cropped DTM to disk."""
        print(f"Exporting modified DTM to {self.output_path}...")
        with rasterio.open(self.output_path, "w", **self.dtm_meta) as dest:
            dest.write(self.modified_dtm.astype("float32"), 1)

    def _export_shapefiles(self):
        """Exports the requested vector layers as ESRI Shapefiles."""
        print("Exporting cross-section vector shapefiles...")
        crs = self.dtm_crs if self.dtm_crs else None

        gdf_xs = gpd.GeoDataFrame(self.xs_lines_data, crs=crs)
        xs_path = os.path.join(self.out_dir, "crossSections.shp")
        gdf_xs.to_file(xs_path)

        gdf_centers = gpd.GeoDataFrame(self.xs_centers_data, crs=crs)
        centers_path = os.path.join(self.out_dir, "crossSectionsCenter.shp")
        gdf_centers.to_file(centers_path)

        gdf_centerline = gpd.GeoDataFrame(self.centerline_data, crs=crs)
        cline_path = os.path.join(self.out_dir, "centerLine.shp")
        gdf_centerline.to_file(cline_path)
        print("Shapefiles exported successfully.")

    @staticmethod
    def generate_centerline_from_banks(
        bank_shp_path: str, output_shp_path: str, step_m: float = 1.0
    ):
        """
        Generates a mathematical centerline between two bank lines such that every point
        on the resulting centerline is strictly equidistant from BOTH bank polylines.
        """
        print(
            f"\nGenerating mathematically equidistant centerline from: {bank_shp_path}..."
        )
        banks_gdf = gpd.read_file(bank_shp_path)

        # Extract the line geometries
        lines = []
        for geom in banks_gdf.geometry:
            if geom.geom_type == "LineString":
                lines.append(geom)
            elif geom.geom_type == "MultiLineString":
                for part in geom.geoms:
                    lines.append(part)

        if len(lines) < 2:
            raise ValueError("The shapefile must contain at least two line geometries.")

        line1, line2 = lines[0], lines[1]

        # Determine the overlapping extent to prevent wild behavior at the ends
        proj_start = line1.project(Point(line2.coords[0]))
        proj_end = line1.project(Point(line2.coords[-1]))

        start_dist = max(min(proj_start, proj_end), 0)
        end_dist = min(max(proj_start, proj_end), line1.length)

        working_length = end_dist - start_dist
        if working_length <= 0:  # Failsafe
            start_dist, end_dist, working_length = 0.0, line1.length, line1.length

        num_points = max(int(working_length / step_m), 2)
        center_coords = []

        # Sweep along the channel
        for i in range(num_points + 1):
            dist_along = start_dist + (i / num_points) * working_length

            # Point on Line 1
            p_a = line1.interpolate(dist_along)

            # Find the nearest opposite point on Line 2 to create a Cross-Section Segment
            _, p_b = nearest_points(p_a, line2)

            # -- BISECTION SEARCH FOR EXACT EQUIDISTANT LOCUS --
            # We seek the exact point on the line segment p_a -> p_b where distance to line1 == distance to line2
            t_low, t_high = 0.0, 1.0
            t_mid = 0.5

            for _ in range(
                40
            ):  # 40 iterations of binary search yields sub-millimeter precision
                t_mid = (t_low + t_high) / 2.0
                p_mid = Point(
                    p_a.x + t_mid * (p_b.x - p_a.x), p_a.y + t_mid * (p_b.y - p_a.y)
                )

                # Check absolute orthogonal distance to both full lines
                d1 = line1.distance(p_mid)
                d2 = line2.distance(p_mid)

                diff = d1 - d2

                if abs(diff) < 1e-4:
                    break

                if diff < 0:
                    t_low = t_mid  # Too close to line 1, push towards line 2
                else:
                    t_high = t_mid  # Too close to line 2, push towards line 1

            # Final Equidistant Point
            p_eq = Point(
                p_a.x + t_mid * (p_b.x - p_a.x), p_a.y + t_mid * (p_b.y - p_a.y)
            )

            # Safely carry over Z-elevations if both shapefile lines are 3D
            if line1.has_z and line2.has_z:
                d1_proj, d2_proj = line1.project(p_eq), line2.project(p_eq)
                z_avg = (
                    line1.interpolate(d1_proj).z + line2.interpolate(d2_proj).z
                ) / 2.0
                center_coords.append((p_eq.x, p_eq.y, z_avg))
            else:
                center_coords.append((p_eq.x, p_eq.y))

        # Remove consecutive identical points to clean up the geometry
        filtered_coords = []
        for coord in center_coords:
            if not filtered_coords or filtered_coords[-1] != coord:
                filtered_coords.append(coord)

        centerline = LineString(filtered_coords)

        # Export
        center_gdf = gpd.GeoDataFrame(
            [{"Name": "Equidistant Bank Centerline", "geometry": centerline}],
            crs=banks_gdf.crs,
        )
        center_gdf.to_file(output_shp_path)
        print(f"Equidistant centerline successfully saved to: {output_shp_path}")

    def process(self):
        """Executes the standard DTM modification workflow."""
        self._read_survey_and_get_bounds()
        self._resample_dtm_window()
        self._process_survey_geometry()
        self._interpolate_and_merge()
        self._export_dtm()
        self._export_shapefiles()
        print("\nAll processing complete successfully!")
