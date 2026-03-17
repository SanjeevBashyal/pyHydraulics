import numpy as np
import pandas as pd
import rasterio
from rasterio.enums import Resampling
from rasterio.windows import from_bounds
from rasterio.transform import Affine
from rasterio.features import geometry_mask
from scipy.interpolate import griddata
from shapely.geometry import Polygon


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

        self.dtm_data = None
        self.dtm_transform = None
        self.dtm_crs = None
        self.dtm_meta = None

    def _read_survey_and_get_bounds(self):
        """Reads the CSV, processes vertical walls, and determines the bounding box."""
        print("Reading survey data and determining processing window...")
        self.df = pd.read_csv(self.csv_path)

        # Handle vertical walls: Keep the lowest Z for identical X,Y
        self.df = self.df.sort_values(by=["Station", "Z"]).drop_duplicates(
            subset=["Station", "X", "Y"], keep="first"
        )

        # Get Extents with buffer
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
            # Create a window from our survey bounding box
            window = from_bounds(*self.bounds, transform=dataset.transform)

            # Ensure window doesn't exceed the actual DTM bounds
            window = window.intersection(
                rasterio.windows.Window(0, 0, dataset.width, dataset.height)
            )

            # Calculate physical size of the cropped window
            orig_res_x = dataset.transform[0]
            orig_res_y = -dataset.transform[4]
            phys_width = window.width * orig_res_x
            phys_height = window.height * orig_res_y

            # Calculate new dimensions for the targeted resolution
            new_width = int(phys_width / self.target_res)
            new_height = int(phys_height / self.target_res)

            # Read and resample ONLY the cropped window
            self.dtm_data = dataset.read(
                1,
                window=window,
                out_shape=(new_height, new_width),
                resampling=Resampling.bilinear,
            )

            # Update transform for the cropped and resampled area
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
            print(
                f"New DTM shape for processing: {self.dtm_data.shape} (RAM friendly!)"
            )

    def _process_survey_geometry(self):
        """Extracts Thalweg (deepest points) and calculates the boundary polygon of the channel."""
        print("Processing channel boundary and Thalweg interpolation...")
        stations = sorted(self.df["Station"].unique())

        left_banks = []
        right_banks = []
        thalweg_pts = []

        for stat in stations:
            stat_data = self.df[self.df["Station"] == stat].copy()

            left_banks.append((stat_data.iloc[0]["X"], stat_data.iloc[0]["Y"]))
            right_banks.append((stat_data.iloc[-1]["X"], stat_data.iloc[-1]["Y"]))

            min_z_row = stat_data.loc[stat_data["Z"].idxmin()]
            thalweg_pts.append((min_z_row["X"], min_z_row["Y"], min_z_row["Z"]))

        # Create the bounding polygon for the mask
        right_banks.reverse()
        self.channel_polygon = Polygon(left_banks + right_banks)

        # Densify Thalweg to strictly guide the interpolation
        densified_thalweg = self._densify_3d_line(thalweg_pts, step=self.target_res * 5)

        self.final_points = np.vstack(
            [self.df[["X", "Y", "Z"]].values, densified_thalweg]
        )

    def _densify_3d_line(self, points, step=0.5):
        """Interpolates points along a 3D line to act as a breakline for the Thalweg."""
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

        # Create grid indices
        cols, rows = np.meshgrid(np.arange(width), np.arange(height))

        # DIRECT AFFINE CONVERSION (Fixes the shape flattening bug)
        # Adding 0.5 shifts the coordinate to the center of the pixel
        xs, ys = self.dtm_transform * (cols + 0.5, rows + 0.5)

        points_xy = self.final_points[:, :2]
        points_z = self.final_points[:, 2]

        # Interpolate survey Z values
        interpolated_channel = griddata(points_xy, points_z, (xs, ys), method="linear")

        # Force the interpolated channel back into the correct 2D shape if griddata flattened it
        interpolated_channel = interpolated_channel.reshape((height, width))

        # Mask out everything outside the survey bounds
        mask = geometry_mask(
            [self.channel_polygon],
            transform=self.dtm_transform,
            invert=True,
            out_shape=(height, width),
        )

        # Apply mask and merge
        valid_interp = mask & ~np.isnan(interpolated_channel)

        # Apply the carved channel over the extracted DTM
        self.modified_dtm = np.where(valid_interp, interpolated_channel, self.dtm_data)

    def _export_dtm(self):
        """Writes the modified, cropped DTM to disk."""
        print(f"Exporting modified DTM to {self.output_path}...")
        with rasterio.open(self.output_path, "w", **self.dtm_meta) as dest:
            dest.write(self.modified_dtm.astype("float32"), 1)
        print("Done! Processing complete.")

    def process(self):
        """Executes the entire workflow chronologically."""
        self._read_survey_and_get_bounds()
        self._resample_dtm_window()
        self._process_survey_geometry()
        self._interpolate_and_merge()
        self._export_dtm()
