"""Fresh clipped-bank junction interpolation routines."""

from __future__ import annotations

import heapq
import numpy as np
from rasterio.features import rasterize
from rasterio.transform import from_origin
from shapely.geometry import LineString, Point
from shapely.ops import nearest_points, unary_union

# Bound by channel_modifier.py after the final facade class is created.
DTMChannelModifier = None


class JunctionInterpolationMixin:
    """Junction overlay built from clipped bank lines and inner bed sections."""

    JUNCTION_INNER_BED_OFFSET_M = 0.3

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
        junction_inner_bed_offset_m=None,
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
        inner_offset = DTMChannelModifier._junction_inner_offset_value(
            junction_inner_bed_offset_m
        )
        summaries = []

        for junction in network.get("junctions", []):
            context = DTMChannelModifier._fresh_junction_context(
                network=network,
                junction=junction,
                bank_offset_m=bank_offset_m,
                bank_structure_protection_m=bank_structure_protection_m,
                inner_offset_m=inner_offset,
                hold_distance=hold_distance,
                transition_distance=transition_distance,
                skewness_correction=skewness_correction,
                centerline_normal_sample_distance_m=centerline_normal_sample_distance_m,
            )
            summary = context["summary"]
            if not context["usable"]:
                summaries.append(summary)
                continue

            junction_zone = context["junction_zone"]
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
                inside_bank_polygon = bool(context["junction_bank_polygon"].covers(cell_point))
                inside_inner_bed = (
                    context["inner_bed_polygon"] is not None
                    and bool(context["inner_bed_polygon"].covers(cell_point))
                )

                blended_z = None
                if inside_inner_bed and context["bed_profiles"]:
                    blended_z = DTMChannelModifier._fresh_junction_inner_bed_elevation(
                        cell_point=cell_point,
                        bed_profiles=context["bed_profiles"],
                        cell_size=cell_size,
                        junction_blend=context["junction_elevation_blend"],
                    )
                    if blended_z is not None:
                        summary["inner_bed_cells_updated"] += 1

                if blended_z is None:
                    candidate_bank_lines = context["bank_lines"]
                    if candidate_bank_lines:
                        candidate_bank_lines = [
                            min(candidate_bank_lines, key=lambda line: cell_point.distance(line))
                        ]
                    candidates = DTMChannelModifier._fresh_junction_bank_candidates(
                        cell_point=cell_point,
                        bank_lines=candidate_bank_lines,
                        profiles=context["profiles"],
                        centerlines=context["centerlines"],
                        terrain_z=terrain_z,
                        hold_distance=hold_distance,
                        transition_distance=transition_distance,
                        blend_type=blend_type,
                        cell_size=cell_size,
                        inner_offset_m=inner_offset,
                        allow_inside=True,
                        allow_outside=True,
                        force_outside_bank=True if not inside_bank_polygon else None,
                        junction_bank_polygon=context["junction_bank_polygon"],
                        inner_bed_polygon=context["inner_bed_polygon"],
                    )
                    if inside_bank_polygon and not candidates:
                        candidates = DTMChannelModifier._fresh_junction_bank_candidates(
                            cell_point=cell_point,
                            bank_lines=context["bank_lines"],
                            profiles=context["profiles"],
                            centerlines=context["centerlines"],
                            terrain_z=terrain_z,
                            hold_distance=hold_distance,
                            transition_distance=transition_distance,
                            blend_type=blend_type,
                            cell_size=cell_size,
                            inner_offset_m=inner_offset,
                            allow_inside=True,
                            allow_outside=True,
                            force_outside_bank=None,
                            junction_bank_polygon=context["junction_bank_polygon"],
                            inner_bed_polygon=context["inner_bed_polygon"],
                        )
                    if candidates:
                        inside_candidates = [
                            candidate for candidate in candidates
                            if not candidate["outside_bank"]
                        ]
                        outside_candidates = [
                            candidate for candidate in candidates
                            if candidate["outside_bank"]
                        ]
                        active_candidates = (
                            inside_candidates if inside_bank_polygon and inside_candidates
                            else outside_candidates if not inside_bank_polygon and outside_candidates
                            else candidates
                        )
                        weight_sum = sum(candidate["weight"] for candidate in active_candidates)
                        if weight_sum > 0.0:
                            blended_z = sum(
                                candidate["weight"] * candidate["z"]
                                for candidate in active_candidates
                            ) / weight_sum
                            summary["bank_strip_cells_updated"] += 1

                if (
                    blended_z is None
                    and inside_bank_polygon
                    and context["bed_profiles"]
                ):
                    blended_z = DTMChannelModifier._fresh_junction_inner_bed_elevation(
                        cell_point=cell_point,
                        bed_profiles=context["bed_profiles"],
                        cell_size=cell_size,
                        junction_blend=context["junction_elevation_blend"],
                    )
                    if blended_z is not None:
                        summary["inner_bed_fallback_cells_updated"] += 1

                if blended_z is None or not np.isfinite(blended_z):
                    continue
                if (
                    inside_bank_polygon
                    and not inside_inner_bed
                    and context["tributary_bed_ramp"] is not None
                ):
                    ramped_z, ramp_weight = DTMChannelModifier._fresh_apply_tributary_bed_ramp(
                        z_value=blended_z,
                        cell_point=cell_point,
                        ramp=context["tributary_bed_ramp"],
                    )
                    if ramp_weight > 0.0:
                        blended_z = ramped_z
                        summary["tributary_bed_ramp_cells_updated"] += 1
                if not inside_bank_polygon and blended_z < terrain_z:
                    blended_z = terrain_z

                updated[row, col] = float(blended_z)
                if not np.isclose(current_z, blended_z):
                    summary["cells_updated"] += 1

            summary["control_sections"] = [
                {
                    "role": role,
                    "channel": channel["name"],
                    "station": section.get("station"),
                }
                for _, role, channel, section in context["control_sections"]
            ]
            updated, enforced_count = DTMChannelModifier._enforce_control_cross_sections_on_raster(
                data=updated,
                sections=context["control_sections"],
                transform=transform,
                nodata=nodata,
                cell_size=cell_size,
            )
            summary["control_section_cells_enforced"] = enforced_count
            summaries.append(summary)

        return updated, summaries

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
        inner_offset = DTMChannelModifier._junction_inner_offset_value(None)

        for junction in network.get("junctions", []):
            context = DTMChannelModifier._fresh_junction_context(
                network=network,
                junction=junction,
                bank_offset_m=bank_offset_m,
                bank_structure_protection_m=bank_structure_protection_m,
                inner_offset_m=inner_offset,
                hold_distance=hold_distance,
                transition_distance=transition_distance,
                skewness_correction=skewness_correction,
                centerline_normal_sample_distance_m=centerline_normal_sample_distance_m,
            )
            if not context["usable"]:
                continue
            mask |= rasterize(
                [context["junction_zone"]],
                out_shape=(height, width),
                transform=base_modifier.dtm_transform,
                fill=0,
                default_value=1,
                dtype="uint8",
                all_touched=False,
            ).astype(bool)

        return mask

    @staticmethod
    def _junction_inner_offset_value(value):
        if value is None:
            return float(JunctionInterpolationMixin.JUNCTION_INNER_BED_OFFSET_M)
        return max(float(value), 0.0)

    @staticmethod
    def _fresh_junction_context(
        network,
        junction,
        bank_offset_m,
        bank_structure_protection_m,
        inner_offset_m,
        hold_distance,
        transition_distance,
        skewness_correction,
        centerline_normal_sample_distance_m,
    ):
        main = network["channels"][junction["main_index"]]
        tributary = network["channels"][junction["tributary_index"]]
        junction_point = Point(float(junction["x"]), float(junction["y"]))
        control_sections = DTMChannelModifier._fresh_junction_cross_sections_for_interpolation(
            tributary=tributary,
            main=main,
            junction=junction,
            junction_point=junction_point,
            skewness_correction=skewness_correction,
            centerline_normal_sample_distance_m=centerline_normal_sample_distance_m,
        )
        profiles = DTMChannelModifier._fresh_junction_half_cross_section_profiles(
            sections=control_sections,
            bank_structure_protection_m=bank_structure_protection_m,
        )
        raw_clipped_banks = DTMChannelModifier._junction_bank_lines_between_cross_sections(
            tributary=tributary,
            main=main,
            junction=junction,
            junction_point=junction_point,
        )
        clipped_banks = DTMChannelModifier._join_gdf_line_features_by_proximity(
            raw_clipped_banks,
            tolerance=1.0,
        )
        bank_lines = DTMChannelModifier._line_strings(clipped_banks)
        centerlines = [
            main.get("processing_centerline") or main["centerline"],
            tributary.get("processing_centerline") or tributary["centerline"],
        ]
        summary = {
            "main": main["name"],
            "tributary": tributary["name"],
            "half_profiles": len(profiles),
            "bank_lines": len(bank_lines),
            "clipped_bank_features": int(len(clipped_banks)) if clipped_banks is not None else 0,
            "raw_clipped_bank_features": int(len(raw_clipped_banks)) if raw_clipped_banks is not None else 0,
            "cells_updated": 0,
            "inner_bed_profiles": 0,
            "inner_bed_weighting": "reach_cross_section_proximity_idw",
            "junction_elevation": None,
            "junction_elevation_blend_mode": None,
            "junction_elevation_blend_radius_m": None,
            "junction_elevation_min_weight": None,
            "junction_elevation_section_count": 0,
            "inner_bed_polygon_area_m2": None,
            "inner_bed_offset_m": float(inner_offset_m),
            "inner_bed_cells_updated": 0,
            "inner_bed_fallback_cells_updated": 0,
            "bank_strip_cells_updated": 0,
            "tributary_bed_ramp_cells_updated": 0,
            "tributary_bed_ramp_target_z": None,
            "tributary_bed_ramp_length_m": None,
            "tributary_connector_centerline_length_m": None,
            "outside_bank_hold_distance_m": float(hold_distance),
            "outside_bank_transition_distance_m": float(transition_distance),
        }

        context = {
            "usable": False,
            "summary": summary,
            "main": main,
            "tributary": tributary,
            "junction_point": junction_point,
            "control_sections": control_sections,
            "profiles": profiles,
            "bank_lines": bank_lines,
            "centerlines": centerlines,
            "bed_profiles": [],
            "junction_bank_polygon": None,
            "inner_bed_polygon": None,
            "junction_zone": None,
            "tributary_bed_ramp": None,
            "junction_elevation_blend": None,
        }
        if len(profiles) < 2 or not bank_lines:
            return context

        junction_bank_polygon = DTMChannelModifier._junction_bank_polygon_from_clipped_banks(
            clipped_banks,
            bank_lines=bank_lines,
            junction_point=junction_point,
        )
        if junction_bank_polygon is None or junction_bank_polygon.is_empty:
            return context

        inner_bed_polygon = DTMChannelModifier._fresh_junction_inner_bed_polygon(
            junction_bank_polygon=junction_bank_polygon,
            bank_lines=bank_lines,
            offset_m=inner_offset_m,
        )
        if inner_bed_polygon is not None and not inner_bed_polygon.is_empty:
            summary["inner_bed_polygon_area_m2"] = round(
                float(inner_bed_polygon.area),
                4,
            )
        bed_profiles = DTMChannelModifier._fresh_junction_bed_cross_section_profiles(
            profiles=profiles,
            inner_offset_m=inner_offset_m,
        )
        junction_elevation_blend = DTMChannelModifier._fresh_junction_elevation_blend(
            junction=junction,
            junction_point=junction_point,
            control_sections=control_sections,
            bed_profiles=bed_profiles,
            inner_bed_polygon=inner_bed_polygon,
            cell_size=0.1,
        )
        if junction_elevation_blend is not None:
            summary["junction_elevation"] = round(
                float(junction_elevation_blend["elevation"]),
                4,
            )
            summary["junction_elevation_blend_mode"] = junction_elevation_blend["mode"]
            summary["junction_elevation_blend_radius_m"] = round(
                float(junction_elevation_blend["blend_radius"]),
                4,
            )
            summary["junction_elevation_min_weight"] = round(
                float(junction_elevation_blend["min_weight"]),
                4,
            )
            summary["junction_elevation_section_count"] = len(
                junction_elevation_blend["sections"]
            )
        summary["inner_bed_profiles"] = len(bed_profiles)
        tributary_bed_ramp = DTMChannelModifier._fresh_junction_tributary_bed_ramp(
            junction=junction,
            junction_point=junction_point,
            main=main,
            tributary=tributary,
            control_sections=control_sections,
            profiles=profiles,
            bed_profiles=bed_profiles,
            junction_bank_polygon=junction_bank_polygon,
            bank_lines=bank_lines,
            inner_offset_m=inner_offset_m,
        )
        if tributary_bed_ramp is not None:
            centerlines = centerlines + [tributary_bed_ramp["centerline"]]
            summary["tributary_bed_ramp_target_z"] = round(
                float(tributary_bed_ramp["target_z"]),
                4,
            )
            summary["tributary_bed_ramp_length_m"] = round(
                float(tributary_bed_ramp["ramp_length"]),
                4,
            )
            summary["tributary_connector_centerline_length_m"] = round(
                float(tributary_bed_ramp["centerline"].length),
                4,
            )

        channel_footprint = DTMChannelModifier._junction_channel_footprint(
            main=main,
            tributary=tributary,
            bank_offset_m=bank_offset_m,
        )
        outside_zone = DTMChannelModifier._fresh_junction_outside_transition_zone(
            junction_bank_polygon=junction_bank_polygon,
            bank_lines=bank_lines,
            outside_blend_extent=max(
                float(hold_distance) + float(transition_distance),
                float(inner_offset_m),
                0.25,
            ),
            exclude_geometry=channel_footprint,
        )

        junction_zone = junction_bank_polygon
        if outside_zone is not None and not outside_zone.is_empty:
            junction_zone = unary_union([junction_bank_polygon, outside_zone])
            if not junction_zone.is_valid:
                junction_zone = junction_zone.buffer(0)
        if junction_zone is None or junction_zone.is_empty:
            return context

        context.update(
            {
                "usable": True,
                "bed_profiles": bed_profiles,
                "junction_bank_polygon": junction_bank_polygon,
                "inner_bed_polygon": inner_bed_polygon,
                "junction_zone": junction_zone,
                "tributary_bed_ramp": tributary_bed_ramp,
                "junction_elevation_blend": junction_elevation_blend,
            }
        )
        return context

    @staticmethod
    def _fresh_junction_cross_sections_for_interpolation(
        tributary,
        main,
        junction,
        junction_point,
        skewness_correction=True,
        centerline_normal_sample_distance_m=3.0,
    ):
        main_centerline = main.get("processing_centerline") or main["centerline"]
        tributary_centerline = tributary.get("processing_centerline") or tributary["centerline"]
        main_bank_lines = DTMChannelModifier._line_strings(main.get("banks_gdf"))
        tributary_bank_lines = DTMChannelModifier._line_strings(tributary.get("banks_gdf"))
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
        if tributary_first is None:
            return []

        return [
            ("main_upstream", "main_upstream", main, main_up),
            ("main_downstream", "main_downstream", main, main_down),
            ("tributary_downstream", "tributary", tributary, tributary_first),
        ]

    @staticmethod
    def _fresh_junction_half_cross_section_profiles(
        sections,
        bank_structure_protection_m=1.0,
    ):
        profiles = []
        for section_key, role, channel, section in sections:
            profiles.extend(
                DTMChannelModifier._fresh_split_cross_section_into_half_profiles(
                    section_key=section_key,
                    role=role,
                    channel=channel,
                    section=section,
                    bank_structure_protection_m=bank_structure_protection_m,
                )
            )
        return profiles

    @staticmethod
    def _fresh_split_cross_section_into_half_profiles(
        section_key,
        role,
        channel,
        section,
        bank_structure_protection_m=1.0,
    ):
        line = section["line"]
        if line is None or line.is_empty or len(line.coords) < 2:
            return []

        profile = section["profile"]
        center_raw = float(profile["raw_center_distance"])
        center_point = line.interpolate(center_raw)
        center_point = Point(float(center_point.x), float(center_point.y))
        left_raw = float(profile["raw_left_bank_distance"])
        right_raw = float(profile["raw_right_bank_distance"])
        left_bank = line.interpolate(left_raw)
        right_bank = line.interpolate(right_raw)
        left_bank = Point(float(left_bank.x), float(left_bank.y))
        right_bank = Point(float(right_bank.x), float(right_bank.y))

        center_corrected = float(profile["corrected_center_distance"])
        left_corrected = float(profile["corrected_left_bank_distance"])
        right_corrected = float(profile["corrected_right_bank_distance"])
        left_width = max(center_corrected - left_corrected, 1e-6)
        right_width = max(right_corrected - center_corrected, 1e-6)
        protected_width = max(float(bank_structure_protection_m), 0.0)
        halves = [
            {
                "side": "left",
                "bank_point": left_bank,
                "corrected_bank_distance": left_corrected,
                "inside_direction": 1.0,
                "corrected_half_length": left_width,
            },
            {
                "side": "right",
                "bank_point": right_bank,
                "corrected_bank_distance": right_corrected,
                "inside_direction": -1.0,
                "corrected_half_length": right_width,
            },
        ]

        split_profiles = []
        for half in halves:
            if half["bank_point"].distance(center_point) <= 1e-6:
                continue

            def protected_inside_distance(
                distance,
                local_width,
                half_width=half["corrected_half_length"],
                protected=protected_width,
            ):
                return DTMChannelModifier._protected_bank_mapped_distance(
                    distance_from_bank=distance,
                    local_bank_to_center_distance=local_width,
                    section_bank_to_center_distance=half_width,
                    protected_width=protected,
                )

            def corrected_offset_from_inside_distance(
                distance,
                bank=half["corrected_bank_distance"],
                direction=half["inside_direction"],
                bank_point=half["bank_point"],
                center=center_point,
            ):
                local_width = max(bank_point.distance(center), 1e-6)
                mapped = protected_inside_distance(distance, local_width)
                return bank + direction * mapped

            def z_from_inner_outward_distance(
                outward_distance,
                inner_distance,
                z_func=profile["z_func"],
                direction=half["inside_direction"],
                offset_from_inside=corrected_offset_from_inside_distance,
            ):
                inner_corrected = offset_from_inside(max(float(inner_distance), 0.0))
                corrected_offset = inner_corrected - direction * max(float(outward_distance), 0.0)
                value = z_func(corrected_offset)
                return float(np.asarray(value).reshape(-1)[0])

            def point_from_inside_distance(
                distance,
                bank_point=half["bank_point"],
                center=center_point,
            ):
                width = bank_point.distance(center)
                if width <= 1e-9:
                    return bank_point
                use_distance = float(np.clip(distance, 0.0, width * 0.95))
                fraction = use_distance / width
                return Point(
                    float(bank_point.x + (center.x - bank_point.x) * fraction),
                    float(bank_point.y + (center.y - bank_point.y) * fraction),
                )

            split_profiles.append(
                {
                    "section_key": section_key,
                    "role": role,
                    "channel": channel["name"],
                    "station": section["station"],
                    "side": half["side"],
                    "bank_point": half["bank_point"],
                    "center_point": center_point,
                    "half_line": LineString(
                        [
                            (half["bank_point"].x, half["bank_point"].y),
                            (center_point.x, center_point.y),
                        ]
                    ),
                    "bank_to_center_distance": half["bank_point"].distance(center_point),
                    "corrected_bank_distance": half["corrected_bank_distance"],
                    "corrected_half_length": half["corrected_half_length"],
                    "inside_direction": half["inside_direction"],
                    "section_z_func": profile["z_func"],
                    "z_from_inner_outward_distance": z_from_inner_outward_distance,
                    "point_from_inside_distance": point_from_inside_distance,
                    "corrected_offset_from_inside_distance": corrected_offset_from_inside_distance,
                    "bank_structure_protection_m": protected_width,
                }
            )
        return split_profiles

    @staticmethod
    def _fresh_junction_bed_cross_section_profiles(profiles, inner_offset_m=0.3):
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
                    "role": left.get("role"),
                    "channel": left.get("channel"),
                    "station": left.get("station"),
                    "line": LineString(
                        [
                            (left_point.x, left_point.y),
                            (right_point.x, right_point.y),
                        ]
                    ),
                    "left_inner_distance": float(left_distance),
                    "right_inner_distance": float(right_distance),
                    "z_from_fraction": z_from_fraction,
                }
            )
        return bed_profiles

    @staticmethod
    def _fresh_junction_elevation_blend(
        junction,
        junction_point,
        control_sections,
        bed_profiles,
        inner_bed_polygon=None,
        cell_size=0.1,
        sample_count=81,
    ):
        section_lookup = {
            section_key: (role, channel, section)
            for section_key, role, channel, section in control_sections
        }
        entries = []
        tolerance = max(float(cell_size), 1e-6)

        for profile in bed_profiles:
            section_key = profile.get("section_key")
            if section_key not in section_lookup:
                continue
            line = profile.get("line")
            if line is None or line.is_empty or line.length <= 1e-9:
                continue

            fractions = np.linspace(0.0, 1.0, max(int(sample_count), 3))
            elevations = np.asarray(
                [profile["z_from_fraction"](fraction) for fraction in fractions],
                dtype=float,
            )
            elevations = elevations[np.isfinite(elevations)]
            if elevations.size == 0:
                continue

            role, channel, section = section_lookup[section_key]
            distance = DTMChannelModifier._fresh_control_section_distance_to_junction(
                junction=junction,
                junction_point=junction_point,
                role=role,
                channel=channel,
                section=section,
            )
            if distance is None or not np.isfinite(distance):
                distance = float(line.distance(junction_point))

            weight_distance = max(float(distance), tolerance)
            weight = 1.0 / (weight_distance * weight_distance)
            entries.append(
                {
                    "section_key": section_key,
                    "role": role,
                    "channel": channel.get("name"),
                    "distance": float(distance),
                    "min_z": float(np.nanmin(elevations)),
                    "weight": float(weight),
                }
            )

        weight_sum = sum(entry["weight"] for entry in entries)
        if weight_sum <= 0.0:
            return None

        junction_elevation = sum(
            entry["weight"] * entry["min_z"] for entry in entries
        ) / weight_sum
        blend_radius = DTMChannelModifier._fresh_polygon_blend_radius(
            point=junction_point,
            polygon=inner_bed_polygon,
            padding=tolerance,
        )
        mode = "inner_bed_polygon_extent"
        if blend_radius is None or not np.isfinite(blend_radius):
            positive_distances = [
                entry["distance"]
                for entry in entries
                if np.isfinite(entry["distance"]) and entry["distance"] > tolerance
            ]
            if positive_distances:
                blend_radius = max(positive_distances)
            else:
                blend_radius = max(
                    [entry["distance"] for entry in entries if np.isfinite(entry["distance"])]
                    or [tolerance]
                )
            mode = "control_section_extent_fallback"
        blend_radius = max(float(blend_radius), tolerance)
        min_weight = 0.05

        return {
            "point": junction_point,
            "elevation": float(junction_elevation),
            "blend_radius": float(blend_radius),
            "min_weight": float(min_weight),
            "mode": mode,
            "sections": entries,
        }

    @staticmethod
    def _fresh_polygon_blend_radius(point, polygon, padding=0.1):
        if point is None or polygon is None or polygon.is_empty:
            return None
        geometries = getattr(polygon, "geoms", [polygon])
        max_distance = 0.0
        found_coordinate = False

        for geometry in geometries:
            exterior = getattr(geometry, "exterior", None)
            if exterior is not None:
                for x, y in exterior.coords:
                    max_distance = max(max_distance, point.distance(Point(float(x), float(y))))
                    found_coordinate = True
            for interior in getattr(geometry, "interiors", []):
                for x, y in interior.coords:
                    max_distance = max(max_distance, point.distance(Point(float(x), float(y))))
                    found_coordinate = True

        if not found_coordinate:
            try:
                minx, miny, maxx, maxy = polygon.bounds
                for x, y in ((minx, miny), (minx, maxy), (maxx, miny), (maxx, maxy)):
                    max_distance = max(max_distance, point.distance(Point(float(x), float(y))))
                    found_coordinate = True
            except Exception:
                return None

        if not found_coordinate or max_distance <= 0.0:
            return None
        return float(max_distance + max(float(padding), 0.0))

    @staticmethod
    def _fresh_control_section_distance_to_junction(
        junction,
        junction_point,
        role,
        channel,
        section,
    ):
        centerline = channel.get("processing_centerline") or channel["centerline"]
        section_measure = section.get("centerline_measure")
        if section_measure is None or not np.isfinite(section_measure):
            section_measure = centerline.project(section["line"].centroid)
        section_measure = float(section_measure)

        if role == "tributary":
            endpoint = junction.get("tributary_endpoint")
            if endpoint == "start":
                junction_measure = 0.0
            elif endpoint == "end":
                junction_measure = float(centerline.length)
            else:
                junction_measure = float(centerline.project(junction_point))
        else:
            junction_measure = float(centerline.project(junction_point))

        return abs(section_measure - junction_measure)

    @staticmethod
    def _fresh_junction_inner_bed_polygon(junction_bank_polygon, bank_lines, offset_m=0.3):
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
    def _fresh_junction_inner_bed_elevation(
        cell_point,
        bed_profiles,
        cell_size=0.1,
        junction_blend=None,
    ):
        candidates = DTMChannelModifier._fresh_junction_inner_bed_candidates(
            cell_point=cell_point,
            bed_profiles=bed_profiles,
            cell_size=cell_size,
        )
        if not candidates:
            return None

        weight_sum = sum(candidate["weight"] for candidate in candidates)
        if weight_sum <= 0.0:
            return None
        blended_z = float(
            sum(candidate["weight"] * candidate["z"] for candidate in candidates)
            / weight_sum
        )
        if junction_blend is None:
            return blended_z
        return DTMChannelModifier._fresh_apply_junction_elevation_blend(
            z_value=blended_z,
            cell_point=cell_point,
            junction_blend=junction_blend,
        )

    @staticmethod
    def _fresh_apply_junction_elevation_blend(z_value, cell_point, junction_blend):
        junction_z = float(junction_blend["elevation"])
        if not np.isfinite(junction_z):
            return float(z_value)

        blend_radius = max(float(junction_blend["blend_radius"]), 1e-6)
        distance = float(cell_point.distance(junction_blend["point"]))
        if distance >= blend_radius:
            return float(z_value)

        junction_weight = 1.0 - DTMChannelModifier._smoothstep(
            np.clip(distance / blend_radius, 0.0, 1.0)
        )
        min_weight = float(np.clip(junction_blend.get("min_weight", 0.0), 0.0, 1.0))
        junction_weight = max(junction_weight, min_weight)
        junction_weight = float(np.clip(junction_weight, 0.0, 1.0))
        if junction_weight <= 0.0:
            return float(z_value)
        return float(
            (1.0 - junction_weight) * float(z_value)
            + junction_weight * junction_z
        )

    @staticmethod
    def _fresh_junction_inner_bed_candidates(cell_point, bed_profiles, cell_size=0.1):
        candidates = []
        tolerance = max(float(cell_size), 1e-6)
        for profile in bed_profiles:
            line = profile["line"]
            if line is None or line.is_empty or line.length <= 1e-9:
                continue

            start = Point(line.coords[0])
            end = Point(line.coords[-1])
            dx = float(end.x - start.x)
            dy = float(end.y - start.y)
            length = float(line.length)
            if length <= 1e-9:
                continue

            raw_measure = (
                (float(cell_point.x) - float(start.x)) * dx
                + (float(cell_point.y) - float(start.y)) * dy
            ) / length
            fraction = float(np.clip(raw_measure / length, 0.0, 1.0))
            z_value = float(profile["z_from_fraction"](fraction))
            if not np.isfinite(z_value):
                continue

            distance = max(float(cell_point.distance(line)), tolerance)
            weight = 1.0 / (distance * distance)
            candidates.append(
                {
                    "profile": profile,
                    "section_key": profile.get("section_key"),
                    "channel": profile.get("channel"),
                    "fraction": fraction,
                    "z": z_value,
                    "distance": distance,
                    "weight": float(weight),
                }
            )
        return candidates

    @staticmethod
    def _fresh_junction_tributary_bed_ramp(
        junction,
        junction_point,
        main,
        tributary,
        control_sections,
        profiles,
        bed_profiles,
        junction_bank_polygon,
        bank_lines,
        inner_offset_m=0.3,
    ):
        main_bed_profiles = [
            profile for profile in bed_profiles
            if str(profile.get("section_key", "")).startswith("main_")
        ]
        main_centerline = main.get("processing_centerline") or main["centerline"]
        main_target_point = main_centerline.interpolate(
            main_centerline.project(junction_point)
        )
        if not junction_bank_polygon.covers(main_target_point):
            if junction_bank_polygon.covers(junction_point):
                main_target_point = junction_point
            else:
                _, main_target_point = nearest_points(junction_bank_polygon, main_target_point)

        target_z = DTMChannelModifier._fresh_junction_inner_bed_elevation(
            main_target_point,
            main_bed_profiles,
            cell_size=0.1,
        )
        if target_z is None or not np.isfinite(target_z):
            return None

        tributary_section = None
        for _, role, _, section in control_sections:
            if role == "tributary":
                tributary_section = section
                break
        if tributary_section is None:
            return None

        centerline = tributary.get("processing_centerline") or tributary["centerline"]
        profile = tributary_section["profile"]
        section_center = tributary_section["line"].interpolate(
            float(profile["raw_center_distance"])
        )
        section_center = Point(float(section_center.x), float(section_center.y))
        connector_centerline = DTMChannelModifier._fresh_junction_connector_centerline(
            junction_bank_polygon=junction_bank_polygon,
            bank_lines=bank_lines,
            start_point=section_center,
            end_point=main_target_point,
        )
        ramp_length = float(connector_centerline.length)
        if ramp_length <= 1e-6:
            return None

        tributary_profiles = [
            profile for profile in profiles
            if profile.get("role") == "tributary"
        ]
        if tributary_profiles:
            total_width = sum(
                float(profile.get("bank_to_center_distance", 0.0))
                for profile in tributary_profiles
            )
        else:
            total_width = 0.0
        tributary_bed_profiles = [
            profile for profile in bed_profiles
            if profile.get("section_key") == "tributary_downstream"
        ]
        inner_bed_width = max(
            [float(profile["line"].length) for profile in tributary_bed_profiles]
            or [max(total_width - 2.0 * float(inner_offset_m), 0.0)]
        )
        full_lateral = max(inner_bed_width * 0.5, 0.25)
        influence_lateral = max(total_width * 0.5 + 0.5, full_lateral + 0.25, 1.0)

        return {
            "centerline": connector_centerline,
            "start_measure": 0.0,
            "end_measure": float(connector_centerline.length),
            "ramp_length": float(ramp_length),
            "target_z": float(target_z),
            "full_lateral": float(full_lateral),
            "influence_lateral": float(influence_lateral),
        }

    @staticmethod
    def _fresh_junction_connector_centerline(
        junction_bank_polygon,
        bank_lines,
        start_point,
        end_point,
        resolution_m=0.2,
    ):
        if (
            junction_bank_polygon is None
            or junction_bank_polygon.is_empty
            or start_point is None
            or end_point is None
            or start_point.distance(end_point) <= 1e-9
        ):
            return LineString([start_point, end_point])

        polygon = junction_bank_polygon
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        if polygon is None or polygon.is_empty:
            return LineString([start_point, end_point])

        start = start_point if polygon.covers(start_point) else nearest_points(polygon, start_point)[0]
        end = end_point if polygon.covers(end_point) else nearest_points(polygon, end_point)[0]
        if start.distance(end) <= 1e-9:
            return LineString([start_point, end_point])

        resolution = max(float(resolution_m), 0.05)
        minx, miny, maxx, maxy = unary_union([polygon, start, end]).bounds
        pad = max(1.0, resolution * 4.0)
        minx -= pad
        miny -= pad
        maxx += pad
        maxy += pad
        width = max(int(np.ceil((maxx - minx) / resolution)), 3)
        height = max(int(np.ceil((maxy - miny) / resolution)), 3)
        if width * height > 250000:
            scale = float(np.sqrt((width * height) / 250000.0))
            resolution *= scale
            width = max(int(np.ceil((maxx - minx) / resolution)), 3)
            height = max(int(np.ceil((maxy - miny) / resolution)), 3)

        transform = from_origin(minx, maxy, resolution, resolution)
        mask = rasterize(
            [polygon],
            out_shape=(height, width),
            transform=transform,
            fill=0,
            default_value=1,
            dtype="uint8",
            all_touched=True,
        ).astype(bool)
        if not np.any(mask):
            return LineString([start, end])

        def point_to_cell(point):
            col = int(np.floor((point.x - minx) / resolution))
            row = int(np.floor((maxy - point.y) / resolution))
            row = int(np.clip(row, 0, height - 1))
            col = int(np.clip(col, 0, width - 1))
            return row, col

        nearest_inside = None

        def snap_cell(row, col):
            nonlocal nearest_inside
            if mask[row, col]:
                return row, col
            if nearest_inside is None:
                _, nearest_inside = distance_transform_edt(
                    ~mask,
                    return_indices=True,
                )
            return int(nearest_inside[0, row, col]), int(nearest_inside[1, row, col])

        try:
            from scipy.ndimage import distance_transform_edt

            start_cell = snap_cell(*point_to_cell(start))
            end_cell = snap_cell(*point_to_cell(end))
            path_cells = DTMChannelModifier._fresh_grid_centerline_path(
                mask=mask,
                start_cell=start_cell,
                end_cell=end_cell,
                resolution=resolution,
            )
        except Exception:
            path_cells = None

        if not path_cells:
            return LineString([start, end])

        coords = [(float(start.x), float(start.y))]
        for row, col in path_cells[1:-1]:
            x = minx + (float(col) + 0.5) * resolution
            y = maxy - (float(row) + 0.5) * resolution
            coords.append((x, y))
        coords.append((float(end.x), float(end.y)))

        deduped = []
        for coord in coords:
            if not deduped or Point(coord).distance(Point(deduped[-1])) > resolution * 0.25:
                deduped.append(coord)
        if len(deduped) < 2:
            return LineString([start, end])

        connector = LineString(deduped)
        if connector.length > resolution:
            connector = connector.simplify(resolution * 0.5, preserve_topology=False)
        if connector.is_empty or connector.length <= 1e-9:
            return LineString([start, end])
        return connector

    @staticmethod
    def _fresh_grid_centerline_path(mask, start_cell, end_cell, resolution):
        try:
            from scipy.ndimage import distance_transform_edt
        except Exception:
            return None

        height, width = mask.shape
        clearance = distance_transform_edt(mask).astype(float) * float(resolution)
        cell_cost = 1.0 + 4.0 / np.maximum(clearance, float(resolution))
        dist = np.full((height, width), np.inf, dtype=float)
        prev_row = np.full((height, width), -1, dtype=np.int32)
        prev_col = np.full((height, width), -1, dtype=np.int32)
        start_row, start_col = start_cell
        end_row, end_col = end_cell
        dist[start_row, start_col] = 0.0
        heap = [(0.0, start_row, start_col)]
        neighbors = [
            (-1, 0, 1.0), (1, 0, 1.0), (0, -1, 1.0), (0, 1, 1.0),
            (-1, -1, np.sqrt(2.0)), (-1, 1, np.sqrt(2.0)),
            (1, -1, np.sqrt(2.0)), (1, 1, np.sqrt(2.0)),
        ]

        while heap:
            current_dist, row, col = heapq.heappop(heap)
            if current_dist > dist[row, col]:
                continue
            if row == end_row and col == end_col:
                break
            for dr, dc, step_factor in neighbors:
                nr = row + dr
                nc = col + dc
                if nr < 0 or nr >= height or nc < 0 or nc >= width:
                    continue
                if not mask[nr, nc]:
                    continue
                step = float(resolution) * float(step_factor)
                step_cost = step * 0.5 * (cell_cost[row, col] + cell_cost[nr, nc])
                new_dist = current_dist + step_cost
                if new_dist < dist[nr, nc]:
                    dist[nr, nc] = new_dist
                    prev_row[nr, nc] = row
                    prev_col[nr, nc] = col
                    heapq.heappush(heap, (new_dist, nr, nc))

        if not np.isfinite(dist[end_row, end_col]):
            return None

        path = []
        row, col = end_row, end_col
        while row >= 0 and col >= 0:
            path.append((int(row), int(col)))
            if row == start_row and col == start_col:
                break
            next_row = int(prev_row[row, col])
            next_col = int(prev_col[row, col])
            row, col = next_row, next_col
        if not path or path[-1] != (start_row, start_col):
            return None
        path.reverse()
        return path

    @staticmethod
    def _fresh_apply_tributary_bed_ramp(z_value, cell_point, ramp):
        centerline = ramp["centerline"]
        start_measure = float(ramp["start_measure"])
        end_measure = float(ramp["end_measure"])
        measure_span = end_measure - start_measure
        if abs(measure_span) <= 1e-9:
            return float(z_value), 0.0

        measure = float(centerline.project(cell_point))
        low_measure = min(start_measure, end_measure)
        high_measure = max(start_measure, end_measure)
        if measure < low_measure or measure > high_measure:
            return float(z_value), 0.0

        projected = centerline.interpolate(measure)
        lateral_distance = float(cell_point.distance(projected))
        influence_lateral = max(float(ramp["influence_lateral"]), 1e-6)
        if lateral_distance > influence_lateral:
            return float(z_value), 0.0

        along_fraction = np.clip((measure - start_measure) / measure_span, 0.0, 1.0)
        along_weight = DTMChannelModifier._smoothstep(along_fraction)

        full_lateral = max(float(ramp["full_lateral"]), 0.0)
        if lateral_distance <= full_lateral:
            lateral_weight = 1.0
        else:
            lateral_fraction = (lateral_distance - full_lateral) / max(
                influence_lateral - full_lateral,
                1e-6,
            )
            lateral_weight = 1.0 - DTMChannelModifier._smoothstep(
                np.clip(lateral_fraction, 0.0, 1.0)
            )

        ramp_weight = float(np.clip(along_weight * lateral_weight, 0.0, 1.0))
        if ramp_weight <= 0.0:
            return float(z_value), 0.0
        target_z = float(ramp["target_z"])
        ramped_z = (1.0 - ramp_weight) * float(z_value) + ramp_weight * target_z
        return float(ramped_z), ramp_weight

    @staticmethod
    def _smoothstep(value):
        x = float(np.clip(value, 0.0, 1.0))
        return x * x * (3.0 - 2.0 * x)

    @staticmethod
    def _fresh_junction_outside_transition_zone(
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

        strips = [
            line.buffer(extent, cap_style=2, join_style=2)
            for line in bank_lines
            if line is not None and not line.is_empty
        ]
        if not strips:
            return None

        outside_zone = unary_union(strips)
        if not outside_zone.is_valid:
            outside_zone = outside_zone.buffer(0)
        outside_zone = outside_zone.difference(junction_bank_polygon)
        if exclude_geometry is not None and not exclude_geometry.is_empty:
            outside_zone = outside_zone.difference(exclude_geometry)
        outside_zone = outside_zone.intersection(
            junction_bank_polygon.buffer(extent, cap_style=2, join_style=2)
        )
        if not outside_zone.is_valid:
            outside_zone = outside_zone.buffer(0)
        return outside_zone if outside_zone is not None and not outside_zone.is_empty else None

    @staticmethod
    def _fresh_junction_bank_candidates(
        cell_point,
        bank_lines,
        profiles,
        centerlines,
        terrain_z,
        hold_distance,
        transition_distance,
        blend_type="cubic",
        cell_size=0.1,
        inner_offset_m=0.3,
        allow_inside=True,
        allow_outside=True,
        force_outside_bank=None,
        junction_bank_polygon=None,
        inner_bed_polygon=None,
    ):
        candidates = []
        inner_offset = max(float(inner_offset_m), 0.0)
        tolerance = max(float(cell_size), 1e-6)

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
                profile_measure = bank_line.project(profile["bank_point"])
                profile_distance = abs(float(bank_measure) - float(profile_measure))
                profile_weight = 1.0 / max(profile_distance, tolerance, 1e-6)
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
            inward = np.array(
                [center_point.x - bank_point.x, center_point.y - bank_point.y],
                dtype=float,
            )
            inward_norm = float(np.linalg.norm(inward))
            if inward_norm <= 1e-9:
                center_point, inward_norm = DTMChannelModifier._nearest_centerline_point_and_distance(
                    bank_point,
                    centerlines,
                )
                inward = np.array(
                    [center_point.x - bank_point.x, center_point.y - bank_point.y],
                    dtype=float,
                )
                inward_norm = float(np.linalg.norm(inward))
            if inward_norm <= 1e-9:
                continue
            inward_unit = DTMChannelModifier._fresh_bank_line_inward_unit(
                bank_line=bank_line,
                bank_measure=bank_measure,
                bank_point=bank_point,
                fallback_center_point=center_point,
                junction_bank_polygon=junction_bank_polygon,
                inner_bed_polygon=inner_bed_polygon,
                probe_distance=max(inner_offset, float(cell_size) * 2.0, 0.2),
            )
            if inward_unit is None:
                inward_unit = inward / inward_norm
            cell_vector = np.array(
                [cell_point.x - bank_point.x, cell_point.y - bank_point.y],
                dtype=float,
            )
            signed_inside_distance = float(np.dot(cell_vector, inward_unit))
            if force_outside_bank is True:
                outside_bank = True
            elif force_outside_bank is False:
                outside_bank = False
            else:
                outside_bank = signed_inside_distance < -0.5 * tolerance
            if outside_bank and not allow_outside:
                continue
            if not outside_bank and not allow_inside:
                continue

            distance_to_bank_line = float(cell_point.distance(bank_line))
            if outside_bank:
                distance_outside_bank = max(distance_to_bank_line, max(-signed_inside_distance, 0.0))
                signed_inside_for_profile = -distance_outside_bank
            else:
                signed_inside_for_profile = signed_inside_distance
                distance_outside_bank = 0.0

            if signed_inside_for_profile > inner_offset + tolerance:
                continue

            outward_from_inner = max(inner_offset - signed_inside_for_profile, 0.0)
            terrain_weight = DTMChannelModifier._terrain_transition_weight(
                distance_from_bank=distance_outside_bank,
                hold_distance=hold_distance,
                transition_distance=transition_distance,
                blend_type=blend_type,
            )
            if terrain_weight >= 1.0:
                continue

            weighted_z = 0.0
            weight_sum = 0.0
            for profile, profile_weight in profile_entries:
                inner_distance = min(
                    inner_offset,
                    max(float(profile["bank_to_center_distance"]) * 0.95, 1e-6),
                )
                z_value = profile["z_from_inner_outward_distance"](
                    outward_distance=outward_from_inner,
                    inner_distance=inner_distance,
                )
                weighted_z += profile_weight * z_value
                weight_sum += profile_weight

            if weight_sum <= 0.0:
                continue
            cross_section_z = weighted_z / weight_sum
            blended_z = terrain_weight * float(terrain_z) + (1.0 - terrain_weight) * cross_section_z
            if outside_bank and blended_z < terrain_z:
                blended_z = terrain_z
            if not np.isfinite(blended_z):
                continue

            bank_distance = max(distance_to_bank_line, tolerance, 1e-6)
            inner_distance_weight = max(outward_from_inner + tolerance, tolerance)
            candidates.append(
                {
                    "z": float(blended_z),
                    "weight": 1.0 / (bank_distance * inner_distance_weight),
                    "outside_bank": bool(outside_bank),
                }
            )

        return candidates

    @staticmethod
    def _fresh_bank_line_inward_unit(
        bank_line,
        bank_measure,
        bank_point,
        fallback_center_point=None,
        junction_bank_polygon=None,
        inner_bed_polygon=None,
        probe_distance=0.3,
    ):
        if bank_line is None or bank_line.is_empty or bank_line.length <= 1e-9:
            return None

        delta = min(max(float(probe_distance), 0.2), max(float(bank_line.length) * 0.25, 0.2))
        start_measure = max(0.0, float(bank_measure) - delta)
        end_measure = min(float(bank_line.length), float(bank_measure) + delta)
        if end_measure <= start_measure:
            start_measure = max(0.0, float(bank_measure) - 1e-3)
            end_measure = min(float(bank_line.length), float(bank_measure) + 1e-3)
        if end_measure <= start_measure:
            return None

        p1 = bank_line.interpolate(start_measure)
        p2 = bank_line.interpolate(end_measure)
        tangent = np.array([p2.x - p1.x, p2.y - p1.y], dtype=float)
        tangent_norm = float(np.linalg.norm(tangent))
        if tangent_norm <= 1e-12:
            return None
        tangent /= tangent_norm
        normals = [
            np.array([-tangent[1], tangent[0]], dtype=float),
            np.array([tangent[1], -tangent[0]], dtype=float),
        ]
        probe = max(float(probe_distance), 0.05)
        bank_xy = np.array([bank_point.x, bank_point.y], dtype=float)

        scored = []
        for normal in normals:
            sample = Point(
                float(bank_xy[0] + normal[0] * probe),
                float(bank_xy[1] + normal[1] * probe),
            )
            score = 0.0
            if junction_bank_polygon is not None and not junction_bank_polygon.is_empty:
                score += 1000.0 if junction_bank_polygon.covers(sample) else -1000.0
            if inner_bed_polygon is not None and not inner_bed_polygon.is_empty:
                bank_inner_distance = bank_point.distance(inner_bed_polygon)
                sample_inner_distance = sample.distance(inner_bed_polygon)
                score += bank_inner_distance - sample_inner_distance
                if inner_bed_polygon.covers(sample):
                    score += 100.0
            if fallback_center_point is not None and not fallback_center_point.is_empty:
                score -= 0.05 * sample.distance(fallback_center_point)
            scored.append((score, normal))

        scored.sort(key=lambda item: item[0], reverse=True)
        best = scored[0][1]
        best_norm = float(np.linalg.norm(best))
        if best_norm <= 1e-12:
            return None
        return best / best_norm
