"""Cross-section geometry, skewness correction, and cell-to-centerline metrics."""

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


class GeometryMixin:
    """Cross-section geometry, skewness correction, and cell-to-centerline metrics."""

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
    def _centerline_unit_tangents(centerline, centerline_distances, sample_distance=3.0):
        """Vectorized bend-smoothed tangents sampled around each centerline chainage."""
        distances = np.asarray(centerline_distances, dtype=float)
        tangents = np.zeros((distances.size, 2), dtype=float)
        cl_length = float(centerline.length)
        if cl_length <= 0.0 or distances.size == 0:
            tangents[:, 0] = 1.0
            return tangents

        sample = max(float(sample_distance), 0.05)
        d1 = np.maximum(0.0, distances - sample)
        d2 = np.minimum(cl_length, distances + sample)
        collapsed = d2 <= d1
        if np.any(collapsed):
            d1[collapsed] = np.maximum(0.0, distances[collapsed] - 1e-3)
            d2[collapsed] = np.minimum(cl_length, distances[collapsed] + 1e-3)

        try:
            import shapely

            p1 = shapely.line_interpolate_point(centerline, d1)
            p2 = shapely.line_interpolate_point(centerline, d2)
            tangents[:, 0] = shapely.get_x(p2) - shapely.get_x(p1)
            tangents[:, 1] = shapely.get_y(p2) - shapely.get_y(p1)
        except Exception:
            for index, (start_d, end_d) in enumerate(zip(d1, d2)):
                p1 = centerline.interpolate(float(start_d))
                p2 = centerline.interpolate(float(end_d))
                tangents[index] = (p2.x - p1.x, p2.y - p1.y)

        norms = np.linalg.norm(tangents, axis=1)
        valid = norms > 0.0
        tangents[valid] = tangents[valid] / norms[valid, None]
        tangents[~valid] = np.array([1.0, 0.0], dtype=float)
        return tangents

    @staticmethod
    def _compute_cross_section_skewness(line, centerline, centerline_distance, centerline_normal_sample_distance_m=3.0):
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
            sample_distance=centerline_normal_sample_distance_m,
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
    def _cross_section_positive_side_direction(
        line,
        centerline,
        centerline_distance,
        center_point,
        centerline_normal_sample_distance_m=3.0,
    ):
        """Returns the profile direction (+1 end, -1 start) for centerline-positive side."""
        coords = np.asarray(line.coords)[:, :2]
        if len(coords) < 2:
            return 1.0

        tangent = DTMChannelModifier._centerline_unit_tangent(
            centerline,
            centerline_distance,
            sample_distance=centerline_normal_sample_distance_m,
        )
        normal = np.array([-tangent[1], tangent[0]], dtype=float)
        normal_norm = np.linalg.norm(normal)
        if normal_norm <= 0:
            return 1.0
        normal /= normal_norm

        center_xy = np.array([center_point.x, center_point.y], dtype=float)
        start_dot = float(np.dot(coords[0] - center_xy, normal))
        end_dot = float(np.dot(coords[-1] - center_xy, normal))
        return -1.0 if start_dot > end_dot else 1.0

    @staticmethod
    def _build_corrected_section_profile(
        line,
        centerline,
        centerline_distance,
        center_point,
        bank_lines=None,
        skewness_correction=True,
        centerline_normal_sample_distance_m=3.0,
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
            centerline_normal_sample_distance_m=centerline_normal_sample_distance_m,
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

        profile_distances = DTMChannelModifier._strictly_increasing_profile_distances(
            corrected_distances
        )
        max_distance = float(corrected_distances[-1])

        if len(profile_distances) == 1:
            constant_z = float(z_values[0])

            def z_func(distance):
                distance_arr = np.asarray(distance, dtype=float)
                return np.full_like(distance_arr, constant_z, dtype=float)
        else:
            def z_func(distance):
                scalar_input = np.isscalar(distance)
                distance_arr = np.asarray(distance, dtype=float)
                result = np.interp(
                    np.clip(distance_arr, 0.0, max_distance),
                    profile_distances,
                    z_values,
                )
                if scalar_input:
                    return float(result)
                return result

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
    def _strictly_increasing_profile_distances(distances, minimum_step=1e-6):
        """Nudge repeated profile stations so vertical walls survive interpolation."""
        adjusted = np.asarray(distances, dtype=float).copy()
        if adjusted.size == 0:
            return adjusted

        profile_length = float(np.nanmax(adjusted) - np.nanmin(adjusted))
        step = max(float(minimum_step), profile_length * 1e-9)
        for idx in range(1, adjusted.size):
            if adjusted[idx] <= adjusted[idx - 1]:
                adjusted[idx] = adjusted[idx - 1] + step
        return adjusted

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
    def _cell_signed_offsets_and_bank_widths(
        centerline,
        bank_lines,
        xs,
        ys,
        cxs,
        cys,
        centerline_distances,
        centerline_normal_sample_distance_m=3.0,
    ):
        if len(bank_lines) < 2:
            fallback = np.full_like(np.asarray(xs, dtype=float), 1.0, dtype=float)
            return np.asarray(xs, dtype=float) * 0.0, fallback

        cl_coords, cl_distances = DTMChannelModifier._centerline_cumulative_distances(centerline)
        if len(cl_coords) < 2:
            fallback = np.full_like(np.asarray(xs, dtype=float), 1.0, dtype=float)
            return np.asarray(xs, dtype=float) * 0.0, fallback

        positive_width_samples = np.zeros(len(cl_coords), dtype=float)
        negative_width_samples = np.zeros(len(cl_coords), dtype=float)
        width_tangents = DTMChannelModifier._centerline_unit_tangents(
            centerline,
            cl_distances,
            sample_distance=centerline_normal_sample_distance_m,
        )
        for index, coord in enumerate(cl_coords):
            tangent = width_tangents[index]
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
        tangent_vectors = DTMChannelModifier._centerline_unit_tangents(
            centerline,
            centerline_distances,
            sample_distance=centerline_normal_sample_distance_m,
        )
        normals = np.column_stack(
            (
                -tangent_vectors[:, 1],
                tangent_vectors[:, 0],
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
