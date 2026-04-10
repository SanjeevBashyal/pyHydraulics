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
    def process_dtm_cells(dtm_path, cross_section_csv, bank_shp_path, target_res=0.1, buffer_m=20.0, break_after_first=False, blend_type='linear', return_dicts=True):
        """
        Iterates through every cell in the DTM, checks if it lies inside the
        bank polygon mask, and if so determines the nearest centerline point
        and the corresponding bank width at that location.

        Args:
            dtm_path: Path to the DTM raster file.
            cross_section_csv: Path to the cross-section CSV.
            bank_shp_path: Path to the bank lines shapefile.
            target_res: Target resolution for resampling (m).
            buffer_m: Buffer around the survey extent (m).
            break_after_first: If True, stops after finding the first cell
                               inside the polygon (for testing).

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
        modifier._resample_dtm_window()

        # Load banks and generate polygon mask explicitly from sequence of shapefile lines
        import geopandas as gpd
        modifier.banks_gdf = DTMChannelModifier.clean_and_merge_banklines(bank_shp_path)
        poly_gdf = DTMChannelModifier.create_polygon_mask_from_banks(modifier.banks_gdf)
        modifier.channel_polygon = poly_gdf.geometry.iloc[0]

        # Generate centerline from banks
        modifier.centerline_gdf = DTMChannelModifier.generate_centerline_from_banks(modifier.banks_gdf)

        # Pre-process cross sections for rapid bracketing & interpolation
        import pandas as pd
        import numpy as np
        from shapely.geometry import LineString, Point
        from shapely.ops import nearest_points
        import shapely

        df = pd.read_csv(cross_section_csv)
        centerline = modifier.centerline_gdf.geometry.iloc[0]
        
        group_cols = [col for col in ['River', 'Reach', 'Station'] if col in df.columns]
        if not group_cols: group_cols = ['Station']

        def make_z_interp(line):
            coords = np.array(line.coords)
            if coords.shape[1] < 3: return lambda d: np.zeros_like(d)
            dists = [0.0]
            for i in range(1, len(coords)):
                d = np.linalg.norm(coords[i, :2] - coords[i-1, :2])
                dists.append(dists[-1] + d)
            dists_np = np.array(dists)
            z_np = coords[:, 2]
            return lambda d: np.interp(np.clip(d, 0, dists_np[-1]), dists_np, z_np)

        stations_list = []
        for name, group in df.groupby(group_cols):
            coords_3d = group[['X', 'Y', 'Z']].values
            if len(coords_3d) < 2: continue
            line = LineString(coords_3d)
            stat_name = str(name if not isinstance(name, tuple) else name[-1])
            pt_C, _ = nearest_points(centerline, line)
            d_xs = centerline.project(pt_C)
            _, _, bw_xs = modifier.get_cell_centerline_metrics(pt_C.x, pt_C.y)
            d_C_xs = line.project(pt_C)
            
            stations_list.append({
                "Station": stat_name, 
                "d_xs": d_xs, 
                "line": line,
                "bw_xs": bw_xs,
                "d_C_xs": d_C_xs,
                "z_func": make_z_interp(line)
            })
            
        stations_list.sort(key=lambda s: s["d_xs"])
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

        xs_poly = DTMChannelModifier.create_cross_section_mask(cross_section_csv, bank_shp_path, interval=1.0)
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

        from scipy.ndimage import distance_transform_edt
        d_bank_grid = distance_transform_edt(bank_mask == 0) * modifier.target_res
        d_bound_grid = distance_transform_edt(xs_mask == 1) * modifier.target_res
        
        d_bank_arr = d_bank_grid[valid_rows, valid_cols]
        d_bound_arr = d_bound_grid[valid_rows, valid_cols]

        tot_d = d_bank_arr + d_bound_arr
        tot_d_safe = np.maximum(tot_d, 1e-6)
        
        w1_terrain = d_bank_arr / tot_d_safe
        if blend_type == 'exponential':
            w1_terrain = (np.exp(w1_terrain) - 1.0) / (np.e - 1.0)
            
        w2_cs = 1.0 - w1_terrain

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
            offset_up = st_up['d_C_xs'] + dir_up * mapped_up
            z_up = st_up['z_func'](offset_up)

            # Dn Z
            mapped_dn = dist_cl_m * (st_dn['bw_xs'] / bw_m)
            dir_dn = np.where(d_cell_dn_m >= st_dn['d_C_xs'], 1, -1)
            offset_dn = st_dn['d_C_xs'] + dir_dn * mapped_dn
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
                "min_dist_up": round(dist_up_array[0], 3),
                "down_station": stations_list[idx_dn[0]]["Station"],
                "min_dist_down": round(dist_dn_array[0], 3),
                "new_interpolated_z": round(new_zs[0], 3),
                "final_blended_z": round(final_zs[0], 3)
            }], modifier
            
        print(f"Vectorized processing completed successfully for {N} mapped cross-section grid cells.")
        
        # Natively map it into the modifier framework for ultra-fast TIF export saving seconds of dict-reading
        mod_dtm = modifier.dtm_data.copy()
        mod_dtm[valid_rows, valid_cols] = final_zs
        modifier.dtm_data = mod_dtm
        
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
                "min_dist_up": round(dist_up_array[i], 3),
                "down_station": stations_list[idx_dn[i]]["Station"],
                "min_dist_down": round(dist_dn_array[i], 3),
                "new_interpolated_z": round(new_zs[i], 3),
                "final_blended_z": round(final_zs[i], 3)
            })

        return results, modifier

    # =========================================================
    # STATIC METHOD TOOLS
    # =========================================================
    @staticmethod
    def clean_and_merge_banklines(banks_input, micro_tolerance=0.5, macro_tolerance=50.0, angle_tol=30.0, bridge_junctions=True):
        import geopandas as gpd
        from shapely.geometry import LineString, Point
        import numpy as np
        import math
        
        if isinstance(banks_input, str):
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
    def create_cross_section_mask(cross_section_csv: str, bank_shp_path: str, interval: float = 1.0):
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

        df = pd.read_csv(cross_section_csv)
        banks_gdf = DTMChannelModifier.clean_and_merge_banklines(bank_shp_path)
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
            
            pt_C, _ = nearest_points(centerline, line)
            d_xs = centerline.project(pt_C)
            
            d_C_xs = line.project(pt_C)
            left_width = d_C_xs
            right_width = line.length - d_C_xs
            
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
        
        df = pd.read_csv(cross_section_csv)
        
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

        df = pd.read_csv(cross_section_csv)
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
        
        df = pd.read_csv(cross_section_csv)
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
        if isinstance(banks_input, str):
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
        banks_input, output_shp_path: str = None, offset_m: float = 0.2
    ):
        if isinstance(banks_input, str):
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

        new_line1 = DTMChannelModifier._get_outward_offset_line(line1, line2, offset_m)
        new_line2 = DTMChannelModifier._get_outward_offset_line(line2, line1, offset_m)

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
        if isinstance(banks_input, str):
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
        if output_shp_path:
            poly_gdf.to_file(output_shp_path)
            print(f"Mask polygon successfully saved to: {output_shp_path}")
        return poly_gdf
