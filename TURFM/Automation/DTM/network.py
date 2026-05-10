"""River-network detection, network.csv handling, and tributary extension helpers."""

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


class NetworkMixin:
    """River-network detection, network.csv handling, and tributary extension helpers."""

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
                    "centerline_source": "bank_lines",
                    "processing_banks_gdf": banks_gdf.copy(),
                    "processing_centerline": centerline,
                    "processing_centerline_source": "bank_lines",
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
            tributary["processing_centerline_source"] = "extended_bank_lines"
            if junction.get("extended_centerline") is not None:
                tributary["processing_centerline"] = junction["extended_centerline"]
                tributary["processing_centerline_source"] = "bank_line_centerline_extended_to_junction"

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

        try:
            df.to_csv(network_csv_path, index=False)
            return network_csv_path
        except PermissionError:
            fallback_path = network_csv_path.with_name(f"{network_csv_path.stem}_junction_coordinates.csv")
            df.to_csv(fallback_path, index=False)
            print(
                f"Warning: {network_csv_path} is locked by another process; "
                f"wrote junction coordinates to {fallback_path} instead."
            )
            return fallback_path

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
