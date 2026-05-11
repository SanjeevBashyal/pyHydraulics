"""HEC-RAS terrain preparation helpers for DTM outputs.

This module deliberately owns the terrain-HDF workflow instead of borrowing
from the v01 RAS Mapper scripts.  `implementationDTM.py` can therefore raise
buildings on the full original DTM first, interpolate the channel from that
raised source, and produce the final terrain package in `3 DTM`.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import shutil
from collections.abc import Iterable

from affine import Affine
import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.errors import WindowError
from rasterio.features import rasterize
from rasterio.warp import reproject, transform_bounds
from rasterio.windows import Window, from_bounds

import geopandas as gpd

from .models import RaisedTerrainResult, TerrainHdfResult


def prepare_building_raised_original_dtm(
    *,
    original_dtm_path: str | Path,
    buildings_shp_path: str | Path | None,
    lift_m: float,
    dtm_root: str | Path,
) -> RaisedTerrainResult:
    """Create a full-size source DTM copy with building polygons raised.

    The original delivered DTM is not modified.  The raised copy becomes the
    input for clipping/interpolation and the merge base for the final terrain.
    """

    original_dtm_path = Path(original_dtm_path)
    dtm_root = Path(dtm_root)
    lift = float(lift_m or 0.0)
    enabled = bool(buildings_shp_path) and abs(lift) > 1e-9

    if not enabled:
        return RaisedTerrainResult(
            raised_tif_path=original_dtm_path,
            created=False,
            enabled=False,
            buildings_shp=str(buildings_shp_path) if buildings_shp_path else None,
            lift_m=lift,
            cells_lifted=0,
            message="Building lift disabled; using original DTM.",
        )

    buildings_path = Path(buildings_shp_path)
    if not buildings_path.exists():
        return RaisedTerrainResult(
            raised_tif_path=original_dtm_path,
            created=False,
            enabled=True,
            buildings_shp=str(buildings_path),
            lift_m=lift,
            cells_lifted=0,
            message=f"Building shapefile not found; using original DTM: {buildings_path}",
        )

    output_dir = dtm_root / "Raised_Originals"
    output_dir.mkdir(parents=True, exist_ok=True)
    lift_token = str(lift).replace("-", "minus_").replace(".", "_")
    output_path = output_dir / f"{_safe_name(original_dtm_path.stem)}_buildings_raised_{lift_token}m.tif"

    newest_source_mtime = max(original_dtm_path.stat().st_mtime, buildings_path.stat().st_mtime)
    if output_path.exists() and output_path.stat().st_mtime >= newest_source_mtime:
        cells_lifted = _count_building_cells(output_path, buildings_path)
        return RaisedTerrainResult(
            raised_tif_path=output_path,
            created=False,
            enabled=True,
            buildings_shp=str(buildings_path),
            lift_m=lift,
            cells_lifted=cells_lifted,
            message="Existing building-raised source DTM is current.",
        )

    shutil.copy2(original_dtm_path, output_path)
    cells_lifted = _apply_building_lift_to_raster(
        raster_path=output_path,
        buildings_shp_path=buildings_path,
        lift_m=lift,
    )
    return RaisedTerrainResult(
        raised_tif_path=output_path,
        created=True,
        enabled=True,
        buildings_shp=str(buildings_path),
        lift_m=lift,
        cells_lifted=cells_lifted,
        message=f"Created building-raised source DTM with {cells_lifted} lifted cells.",
    )


def prepare_component_terrain_hdf(
    *,
    original_dtm_path: str | Path,
    interpolated_tif_path: str | Path,
    dtm_root: str | Path,
    component_name: str,
    projection_prj_path: str | Path,
    exact_bank_polygon_path: str | Path | Iterable[str | Path] | None = None,
    units: str = "Meters",
    hecras_version: str = "7.0",
    resampling: str = "bilinear",
    exact_bank_buffer_m: float = 2.0,
    bilinear_bank_resolution_m: float = 0.1,
) -> TerrainHdfResult:
    """Merge the channel terrain with the original DTM and create a HEC-RAS HDF.

    The interpolated raster is usually a cropped window around the channel.  To
    create a complete terrain for HEC-RAS, we copy the original DTM and overlay
    valid interpolated channel cells using the requested resampling method.
    The HDF uses a high-resolution bank/near-bank overlay above the merged
    terrain. Nearest mode keeps the exact interpolated bank raster; bilinear
    mode writes a 0.1 m bank overlay resampled bilinearly onto a fresh grid so
    the bank region stays high-resolution without the coarse base-grid drift.
    """

    resampling = _normalize_hdf_bank_merge_type(resampling)

    original_dtm_path = Path(original_dtm_path)
    interpolated_tif_path = Path(interpolated_tif_path)
    projection_prj_path = Path(projection_prj_path)
    dtm_root = Path(dtm_root)
    safe_component = _safe_name(component_name)
    exact_bank_polygon_paths = _normalize_polygon_paths(exact_bank_polygon_path)
    use_exact_bank_raster = resampling == "nearest"
    use_bilinear_bank_raster = resampling == "bilinear"

    if not projection_prj_path.exists():
        raise FileNotFoundError(
            f"Projection file required for HEC-RAS terrain HDF was not found: "
            f"{projection_prj_path}"
        )

    component_root = dtm_root / safe_component
    merged_dir = component_root / "merged_terrain"
    terrain_dir = component_root / "Terrain"
    exact_dir = component_root / "exact_channel"
    bilinear_dir = component_root / "bilinear_channel"
    merged_dir.mkdir(parents=True, exist_ok=True)
    terrain_dir.mkdir(parents=True, exist_ok=True)
    exact_dir.mkdir(parents=True, exist_ok=True)
    bilinear_dir.mkdir(parents=True, exist_ok=True)

    merged_tif = merged_dir / f"{safe_component}_original_plus_channel.tif"
    exact_bank_tif = exact_dir / f"{safe_component}_bank_exact_channel.tif"
    bilinear_bank_tif = bilinear_dir / f"{safe_component}_bank_bilinear_channel.tif"
    hdf_path = terrain_dir / f"{merged_tif.stem}.hdf"

    newest_source_mtime = max(
        original_dtm_path.stat().st_mtime,
        interpolated_tif_path.stat().st_mtime,
    )
    if (
        merged_tif.exists()
        and merged_tif.stat().st_mtime >= newest_source_mtime
        and _merged_tif_matches_resampling(merged_tif, resampling)
        and _merged_tif_matches_bank_polygons(
            merged_tif,
            exact_bank_polygon_paths,
            buffer_m=exact_bank_buffer_m,
        )
    ):
        merged_created = False
    else:
        _merge_interpolated_tif_over_original(
            original_dtm_path=original_dtm_path,
            interpolated_tif_path=interpolated_tif_path,
            output_tif_path=merged_tif,
            resampling=resampling,
            exact_bank_polygon_paths=exact_bank_polygon_paths,
            exact_bank_buffer_m=exact_bank_buffer_m,
        )
        merged_created = True

    exact_bank_tif_path = None
    bank_channel_tif_path = None
    bank_channel_mode = None
    if use_exact_bank_raster:
        exact_bank_tif_path = _prepare_exact_bank_channel_tif(
            original_dtm_path=original_dtm_path,
            interpolated_tif_path=interpolated_tif_path,
            bank_polygon_paths=exact_bank_polygon_paths,
            output_tif_path=exact_bank_tif,
            buffer_m=exact_bank_buffer_m,
        )
        bank_channel_tif_path = exact_bank_tif_path
        bank_channel_mode = "nearest_exact" if exact_bank_tif_path else None
    elif use_bilinear_bank_raster:
        bank_channel_tif_path = _prepare_bilinear_bank_channel_tif(
            original_dtm_path=original_dtm_path,
            interpolated_tif_path=interpolated_tif_path,
            bank_polygon_paths=exact_bank_polygon_paths,
            output_tif_path=bilinear_bank_tif,
            buffer_m=exact_bank_buffer_m,
            target_res_m=bilinear_bank_resolution_m,
        )
        bank_channel_mode = "bilinear_0_1m" if bank_channel_tif_path else None

    hdf_input_paths = [bank_channel_tif_path, merged_tif] if bank_channel_tif_path else [merged_tif]
    hdf_inputs = hdf_input_paths if len(hdf_input_paths) > 1 else hdf_input_paths[0]
    hdf_marker_payload = _terrain_hdf_marker_payload(
        input_tif_paths=hdf_input_paths,
        resampling=resampling,
        exact_bank_buffer_m=exact_bank_buffer_m,
        bilinear_bank_resolution_m=bilinear_bank_resolution_m,
        exact_bank_polygon_paths=exact_bank_polygon_paths,
        bank_overlay_mode=bank_channel_mode,
    )

    hdf_dependency_mtime = max(
        merged_tif.stat().st_mtime,
        projection_prj_path.stat().st_mtime,
        bank_channel_tif_path.stat().st_mtime if bank_channel_tif_path else 0.0,
    )
    if (
        hdf_path.exists()
        and hdf_path.stat().st_mtime >= hdf_dependency_mtime
        and _terrain_hdf_marker_matches(hdf_path, hdf_marker_payload)
    ):
        return TerrainHdfResult(
            merged_tif_path=merged_tif,
            hdf_path=hdf_path,
            created=False,
            message="Existing HEC-RAS terrain HDF is current.",
            exact_bank_tif_path=exact_bank_tif_path,
            bank_channel_tif_path=bank_channel_tif_path,
            bank_channel_mode=bank_channel_mode,
        )

    _create_hecras_terrain_hdf(
        input_tif_path=hdf_inputs,
        output_hdf_path=hdf_path,
        projection_prj_path=projection_prj_path,
        units=units,
        hecras_version=hecras_version,
    )
    _write_terrain_hdf_marker(hdf_path, hdf_marker_payload)
    return TerrainHdfResult(
        merged_tif_path=merged_tif,
        hdf_path=hdf_path,
        created=True,
        message=(
            "Created HEC-RAS terrain HDF."
            if merged_created
            else "Created HEC-RAS terrain HDF from existing merged GeoTIFF."
        ),
        exact_bank_tif_path=exact_bank_tif_path,
        bank_channel_tif_path=bank_channel_tif_path,
        bank_channel_mode=bank_channel_mode,
    )


def _merge_interpolated_tif_over_original(
    *,
    original_dtm_path: Path,
    interpolated_tif_path: Path,
    output_tif_path: Path,
    resampling: str,
    exact_bank_polygon_paths: list[Path] | None = None,
    exact_bank_buffer_m: float = 0.0,
) -> None:
    """Overlay valid interpolated channel cells on a copy of the original DTM."""

    output_tif_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(original_dtm_path, output_tif_path)
    resampling = _normalize_hdf_bank_merge_type(resampling)
    resampling_method = getattr(Resampling, resampling)

    with rasterio.open(interpolated_tif_path) as src, rasterio.open(output_tif_path, "r+") as dst:
        src_bounds = src.bounds
        if src.crs and dst.crs and src.crs != dst.crs:
            src_bounds = transform_bounds(src.crs, dst.crs, *src.bounds, densify_pts=21)

        window = from_bounds(*src_bounds, transform=dst.transform)
        full_window = Window(0, 0, dst.width, dst.height)
        try:
            window = window.intersection(full_window)
        except WindowError as exc:
            raise ValueError(
                f"Interpolated terrain does not overlap original terrain: "
                f"{interpolated_tif_path} vs {original_dtm_path}"
            ) from exc

        window = window.round_offsets().round_lengths()
        height = int(window.height)
        width = int(window.width)
        if height <= 0 or width <= 0:
            raise ValueError(
                f"Interpolated terrain overlap is empty: {interpolated_tif_path}"
            )

        destination = dst.read(1, window=window).astype("float32")
        reprojected = np.full((height, width), np.nan, dtype="float32")
        dst_nodata = dst.nodata
        src_nodata = src.nodata

        reproject(
            source=rasterio.band(src, 1),
            destination=reprojected,
            src_transform=src.transform,
            src_crs=src.crs,
            src_nodata=src_nodata,
            dst_transform=dst.window_transform(window),
            dst_crs=dst.crs,
            dst_nodata=np.nan,
            resampling=resampling_method,
        )

        valid = np.isfinite(reprojected)
        if src_nodata is not None:
            valid &= ~np.isclose(reprojected, src_nodata)
        if dst_nodata is not None:
            valid &= ~np.isclose(reprojected, dst_nodata)

        if not np.any(valid):
            raise ValueError(f"Interpolated terrain has no valid cells: {interpolated_tif_path}")

        if exact_bank_polygon_paths:
            bank_mask = _rasterize_polygon_paths(
                polygon_paths=exact_bank_polygon_paths,
                crs=dst.crs,
                out_shape=destination.shape,
                transform=dst.window_transform(window),
                all_touched=True,
                buffer_m=exact_bank_buffer_m,
            )
            outside_bank = ~bank_mask
            lower_outside_bank = (
                valid
                & outside_bank
                & np.isfinite(destination)
                & (reprojected < destination)
            )
            valid[lower_outside_bank] = False

        destination[valid] = reprojected[valid]
        dst.write(destination.astype(dst.dtypes[0]), 1, window=window)
        dst.update_tags(
            TURFM_RESAMPLING=str(resampling).lower(),
            TURFM_BANK_POLYGONS=_polygon_path_signature(exact_bank_polygon_paths),
            TURFM_BANK_BUFFER_M=str(float(exact_bank_buffer_m)),
        )


def _merged_tif_matches_resampling(path: Path, resampling: str) -> bool:
    """Return True when a cached merged terrain was made with this resampling."""

    resampling = _normalize_hdf_bank_merge_type(resampling)
    try:
        with rasterio.open(path) as dataset:
            return dataset.tags().get("TURFM_RESAMPLING", "").lower() == resampling
    except Exception:
        return False


def _normalize_hdf_bank_merge_type(value: str) -> str:
    """Normalize the supported HDF bank-polygon merge resampling modes."""

    normalized = str(value or "bilinear").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "bilinear": "bilinear",
        "nearest": "nearest",
        "nearest_neighbor": "nearest",
        "nearest_neighbour": "nearest",
        "neighbour": "nearest",
        "neighbor": "nearest",
    }
    if normalized not in aliases:
        raise ValueError(
            "HDF bank polygon merge type must be 'bilinear' or 'nearest'. "
            f"Got: {value!r}"
        )
    return aliases[normalized]


def _terrain_hdf_marker_payload(
    *,
    input_tif_paths: list[Path],
    resampling: str,
    exact_bank_buffer_m: float,
    bilinear_bank_resolution_m: float,
    exact_bank_polygon_paths: list[Path],
    bank_overlay_mode: str | None,
) -> dict:
    """Build cache metadata for the terrain HDF input stack."""

    return {
        "input_rasters": [str(path) for path in input_tif_paths],
        "hdf_bank_polygon_merge_type": _normalize_hdf_bank_merge_type(resampling),
        "hdf_nearest_neighbour_buffer_distance_out_of_bank_polygon_m": float(exact_bank_buffer_m),
        "hdf_bilinear_bank_resolution_m": float(bilinear_bank_resolution_m),
        "exact_bank_polygon_signature": _polygon_path_signature(exact_bank_polygon_paths),
        "bank_overlay_mode": bank_overlay_mode,
    }


def _terrain_hdf_marker_path(hdf_path: Path) -> Path:
    """Return the sidecar path that records how this HDF was stitched."""

    return hdf_path.with_suffix(hdf_path.suffix + ".turfm.json")


def _terrain_hdf_marker_matches(hdf_path: Path, expected_payload: dict) -> bool:
    """Return True when the HDF sidecar matches the requested input stack."""

    marker_path = _terrain_hdf_marker_path(hdf_path)
    try:
        return json.loads(marker_path.read_text(encoding="utf-8")) == expected_payload
    except Exception:
        return False


def _write_terrain_hdf_marker(hdf_path: Path, payload: dict) -> None:
    """Write a small sidecar so cached HDFs do not reuse the wrong bank mode."""

    marker_path = _terrain_hdf_marker_path(hdf_path)
    marker_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _merged_tif_matches_bank_polygons(
    path: Path,
    polygon_paths: list[Path],
    buffer_m: float = 0.0,
) -> bool:
    """Return True when a cached merged terrain used this exact-bank mask."""

    try:
        with rasterio.open(path) as dataset:
            tags = dataset.tags()
            return (
                tags.get("TURFM_BANK_POLYGONS", "") == _polygon_path_signature(polygon_paths)
                and tags.get("TURFM_BANK_BUFFER_M", "0.0") == str(float(buffer_m))
            )
    except Exception:
        return False


def _bank_channel_tif_matches_source(
    path: Path,
    bank_polygon_paths: list[Path],
    buffer_m: float = 0.0,
    original_dtm_path: Path | None = None,
    role: str | None = None,
    target_res_m: float | None = None,
) -> bool:
    """Return True when a bank overlay raster matches its source inputs."""

    try:
        with rasterio.open(path) as dataset:
            tags = dataset.tags()
            role_matches = True if role is None else tags.get("TURFM_ROLE", "") == role
            resolution_matches = (
                True
                if target_res_m is None
                else tags.get("TURFM_BANK_CHANNEL_RESOLUTION_M", "") == str(float(target_res_m))
            )
            return (
                tags.get("TURFM_EXACT_BANK_POLYGON", "") == _polygon_path_signature(bank_polygon_paths)
                and tags.get("TURFM_EXACT_BANK_BUFFER_M", "0.0") == str(float(buffer_m))
                and tags.get("TURFM_EXACT_ORIGINAL_DTM", "") == (str(original_dtm_path) if original_dtm_path else "")
                and role_matches
                and resolution_matches
            )
    except Exception:
        return False


def _prepare_bilinear_bank_channel_tif(
    *,
    original_dtm_path: Path,
    interpolated_tif_path: Path,
    bank_polygon_paths: list[Path],
    output_tif_path: Path,
    buffer_m: float = 0.0,
    target_res_m: float = 0.1,
) -> Path | None:
    """Create a 0.1 m bilinear bank overlay for the final HDF stack."""

    bank_polygon_paths = [path for path in bank_polygon_paths if path.exists()]
    if not bank_polygon_paths:
        return None

    output_tif_path.parent.mkdir(parents=True, exist_ok=True)
    newest_source_mtime = max(
        original_dtm_path.stat().st_mtime,
        interpolated_tif_path.stat().st_mtime,
        max(path.stat().st_mtime for path in bank_polygon_paths),
    )
    if (
        output_tif_path.exists()
        and output_tif_path.stat().st_mtime >= newest_source_mtime
        and _bank_channel_tif_matches_source(
            output_tif_path,
            bank_polygon_paths,
            buffer_m=buffer_m,
            original_dtm_path=original_dtm_path,
            role="bilinear_bank_channel",
            target_res_m=target_res_m,
        )
    ):
        return output_tif_path

    with rasterio.open(interpolated_tif_path) as src, rasterio.open(original_dtm_path) as original:
        nodata = src.nodata if src.nodata is not None else -10000.0
        transform, width, height = _aligned_bank_overlay_grid(src, original, target_res_m=target_res_m)
        bilinear_values = np.full((height, width), np.nan, dtype="float32")

        reproject(
            source=rasterio.band(src, 1),
            destination=bilinear_values,
            src_transform=src.transform,
            src_crs=src.crs,
            src_nodata=src.nodata,
            dst_transform=transform,
            dst_crs=src.crs,
            dst_nodata=np.nan,
            resampling=Resampling.bilinear,
        )

        true_bank_mask = _rasterize_polygon_paths(
            polygon_paths=bank_polygon_paths,
            crs=src.crs,
            out_shape=bilinear_values.shape,
            transform=transform,
            all_touched=True,
            buffer_m=0.0,
        )
        patch_mask = _rasterize_polygon_paths(
            polygon_paths=bank_polygon_paths,
            crs=src.crs,
            out_shape=bilinear_values.shape,
            transform=transform,
            all_touched=True,
            buffer_m=buffer_m,
        )

        valid = patch_mask & np.isfinite(bilinear_values)
        if src.nodata is not None:
            valid &= ~np.isclose(bilinear_values, src.nodata)
        if not np.any(valid):
            return None

        original_grid = _read_original_on_grid_shape(
            original_dtm_path=original_dtm_path,
            out_shape=bilinear_values.shape,
            transform=transform,
            crs=src.crs,
        )
        outside_true_bank = valid & ~true_bank_mask & np.isfinite(original_grid)
        if original.nodata is not None:
            outside_true_bank &= ~np.isclose(original_grid, original.nodata)
        preserve_higher_terrain = outside_true_bank & (original_grid > bilinear_values)
        bilinear_values[preserve_higher_terrain] = original_grid[preserve_higher_terrain]

        overlay = np.full(bilinear_values.shape, float(nodata), dtype="float32")
        overlay[valid] = bilinear_values[valid]
        meta = src.meta.copy()
        meta.update(
            {
                "driver": "GTiff",
                "height": height,
                "width": width,
                "count": 1,
                "dtype": "float32",
                "transform": transform,
                "nodata": float(nodata),
            }
        )

        with rasterio.open(output_tif_path, "w", **meta) as dst:
            dst.write(overlay, 1)
            dst.update_tags(
                TURFM_EXACT_BANK_POLYGON=_polygon_path_signature(bank_polygon_paths),
                TURFM_EXACT_BANK_BUFFER_M=str(float(buffer_m)),
                TURFM_EXACT_ORIGINAL_DTM=str(original_dtm_path),
                TURFM_ROLE="bilinear_bank_channel",
                TURFM_BANK_CHANNEL_RESAMPLING="bilinear",
                TURFM_BANK_CHANNEL_RESOLUTION_M=str(float(target_res_m)),
            )

    return output_tif_path


def _aligned_bank_overlay_grid(src_dataset, original_dataset, target_res_m: float = 0.1) -> tuple[Affine, int, int]:
    """Return a high-resolution grid phased to the original DTM origin for bilinear overlay."""

    fallback_width = abs(float(src_dataset.transform.a))
    fallback_height = abs(float(src_dataset.transform.e))
    requested_res = float(target_res_m or 0.0)
    pixel_width = requested_res if requested_res > 0.0 else fallback_width
    pixel_height = requested_res if requested_res > 0.0 else fallback_height
    source_bounds = src_dataset.bounds
    origin_x = float(original_dataset.transform.c)
    origin_y = float(original_dataset.transform.f)

    col0 = math.floor((source_bounds.left - origin_x) / pixel_width)
    row0 = math.floor((origin_y - source_bounds.top) / pixel_height)
    left = origin_x + col0 * pixel_width
    top = origin_y - row0 * pixel_height
    width = max(1, int(math.ceil((source_bounds.right - left) / pixel_width)))
    height = max(1, int(math.ceil((top - source_bounds.bottom) / pixel_height)))
    transform = Affine(pixel_width, 0.0, left, 0.0, -pixel_height, top)
    return transform, width, height


def _prepare_exact_bank_channel_tif(
    *,
    original_dtm_path: Path,
    interpolated_tif_path: Path,
    bank_polygon_paths: list[Path],
    output_tif_path: Path,
    buffer_m: float = 0.0,
) -> Path | None:
    """Create a high-resolution channel raster masked to the bank polygon.

    Bilinear resampling is useful outside the channel because it removes blocky
    transitions on the original DTM grid. Inside the surveyed bank polygon,
    however, resampling can soften vertical walls and miss exact CSV-derived bed
    elevations. This raster preserves the interpolated channel grid only inside
    the banks and leaves nodata elsewhere, so HEC-RAS can stitch it above the
    smooth merged terrain.
    """

    bank_polygon_paths = [path for path in bank_polygon_paths if path.exists()]
    if not bank_polygon_paths:
        return None

    output_tif_path.parent.mkdir(parents=True, exist_ok=True)
    newest_source_mtime = max(
        original_dtm_path.stat().st_mtime,
        interpolated_tif_path.stat().st_mtime,
        max(path.stat().st_mtime for path in bank_polygon_paths),
    )
    if (
        output_tif_path.exists()
        and output_tif_path.stat().st_mtime >= newest_source_mtime
        and _exact_bank_tif_matches_source(
            output_tif_path,
            bank_polygon_paths,
            buffer_m=buffer_m,
            original_dtm_path=original_dtm_path,
        )
    ):
        return output_tif_path

    with rasterio.open(interpolated_tif_path) as src:
        nodata = src.nodata if src.nodata is not None else -10000.0
        data = src.read(1).astype("float32")
        true_bank_mask = _rasterize_polygon_paths(
            polygon_paths=bank_polygon_paths,
            crs=src.crs,
            out_shape=data.shape,
            transform=src.transform,
            all_touched=True,
            buffer_m=0.0,
        )
        patch_mask = _rasterize_polygon_paths(
            polygon_paths=bank_polygon_paths,
            crs=src.crs,
            out_shape=data.shape,
            transform=src.transform,
            all_touched=True,
            buffer_m=buffer_m,
        )

        valid = patch_mask & np.isfinite(data)
        if src.nodata is not None:
            valid &= ~np.isclose(data, src.nodata)
        if not np.any(valid):
            return None

        exact_values = data.copy()
        original_grid = _read_original_on_grid(
            original_dtm_path=original_dtm_path,
            like_dataset=src,
        )
        outside_true_bank = valid & ~true_bank_mask & np.isfinite(original_grid)
        if src.nodata is not None:
            outside_true_bank &= ~np.isclose(original_grid, src.nodata)
        preserve_higher_terrain = outside_true_bank & (original_grid > exact_values)
        exact_values[preserve_higher_terrain] = original_grid[preserve_higher_terrain]

        exact = np.full(data.shape, float(nodata), dtype="float32")
        exact[valid] = exact_values[valid]
        meta = src.meta.copy()
        meta.update(
            {
                "driver": "GTiff",
                "count": 1,
                "dtype": "float32",
                "nodata": float(nodata),
            }
        )

        with rasterio.open(output_tif_path, "w", **meta) as dst:
            dst.write(exact, 1)
            dst.update_tags(
                TURFM_EXACT_BANK_POLYGON=_polygon_path_signature(bank_polygon_paths),
                TURFM_EXACT_BANK_BUFFER_M=str(float(buffer_m)),
                TURFM_EXACT_ORIGINAL_DTM=str(original_dtm_path),
                TURFM_ROLE="exact_in_bank_channel",
            )

    return output_tif_path


def _exact_bank_tif_matches_source(
    path: Path,
    bank_polygon_paths: list[Path],
    buffer_m: float = 0.0,
    original_dtm_path: Path | None = None,
) -> bool:
    """Return True when the exact bank raster was created from this polygon."""

    try:
        with rasterio.open(path) as dataset:
            tags = dataset.tags()
            return (
                tags.get("TURFM_EXACT_BANK_POLYGON", "") == _polygon_path_signature(bank_polygon_paths)
                and tags.get("TURFM_EXACT_BANK_BUFFER_M", "0.0") == str(float(buffer_m))
                and tags.get("TURFM_EXACT_ORIGINAL_DTM", "") == (str(original_dtm_path) if original_dtm_path else "")
            )
    except Exception:
        return False


def _read_original_on_grid(*, original_dtm_path: Path, like_dataset) -> np.ndarray:
    """Read the original terrain reprojected to an interpolated/exact grid."""

    return _read_original_on_grid_shape(
        original_dtm_path=original_dtm_path,
        out_shape=(like_dataset.height, like_dataset.width),
        transform=like_dataset.transform,
        crs=like_dataset.crs,
    )


def _read_original_on_grid_shape(*, original_dtm_path: Path, out_shape, transform, crs) -> np.ndarray:
    """Read the original terrain onto an arbitrary target grid."""

    original_grid = np.full(
        out_shape,
        np.nan,
        dtype="float32",
    )
    with rasterio.open(original_dtm_path) as original:
        reproject(
            source=rasterio.band(original, 1),
            destination=original_grid,
            src_transform=original.transform,
            src_crs=original.crs,
            src_nodata=original.nodata,
            dst_transform=transform,
            dst_crs=crs,
            dst_nodata=np.nan,
            resampling=Resampling.bilinear,
        )
    return original_grid


def _normalize_polygon_paths(value: str | Path | Iterable[str | Path] | None) -> list[Path]:
    """Normalize optional polygon path input while preserving order."""

    if value is None:
        return []
    if isinstance(value, (str, Path)):
        values = [value]
    else:
        values = list(value)

    paths = []
    seen = set()
    for item in values:
        if not item:
            continue
        path = Path(item)
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        paths.append(path)
    return paths


def _polygon_path_signature(paths: list[Path] | None) -> str:
    """Stable tag value for polygon-mask provenance."""

    return "|".join(str(path) for path in paths or [])


def _rasterize_polygon_paths(
    *,
    polygon_paths: list[Path],
    crs,
    out_shape,
    transform,
    all_touched=True,
    buffer_m: float = 0.0,
) -> np.ndarray:
    """Rasterize all valid polygons from the supplied path list."""

    geometries = []
    for polygon_path in polygon_paths:
        if polygon_path is None or not polygon_path.exists():
            continue
        gdf = gpd.read_file(polygon_path)
        if gdf.empty:
            continue
        if crs is not None:
            if gdf.crs is None:
                gdf = gdf.set_crs(crs, allow_override=True)
            elif gdf.crs != crs:
                gdf = gdf.to_crs(crs)
        for geometry in gdf.geometry:
            if geometry is None or geometry.is_empty:
                continue
            if buffer_m and abs(float(buffer_m)) > 1e-9:
                geometry = geometry.buffer(float(buffer_m))
                if geometry is None or geometry.is_empty:
                    continue
            geometries.append(geometry)

    if not geometries:
        return np.zeros(out_shape, dtype=bool)

    return rasterize(
        geometries,
        out_shape=out_shape,
        transform=transform,
        fill=0,
        default_value=1,
        dtype="uint8",
        all_touched=all_touched,
    ).astype(bool)


def _apply_building_lift_to_raster(
    *,
    raster_path: Path,
    buildings_shp_path: Path,
    lift_m: float,
) -> int:
    """Raise cells inside building polygons in-place for a full-size raster."""

    buildings_gdf = gpd.read_file(buildings_shp_path)
    if buildings_gdf.empty:
        print(f"Warning: Building shapefile has no features: {buildings_shp_path}")
        return 0

    with rasterio.open(raster_path, "r+") as dataset:
        if dataset.crs is not None:
            if buildings_gdf.crs is None:
                buildings_gdf = buildings_gdf.set_crs(dataset.crs, allow_override=True)
            elif buildings_gdf.crs != dataset.crs:
                buildings_gdf = buildings_gdf.to_crs(dataset.crs)

        geometries = [
            geometry
            for geometry in buildings_gdf.geometry
            if geometry is not None and not geometry.is_empty
        ]
        if not geometries:
            print(f"Warning: Building shapefile has no valid polygon geometry: {buildings_shp_path}")
            return 0

        data = dataset.read(1).astype("float32")
        mask = rasterize(
            geometries,
            out_shape=data.shape,
            transform=dataset.transform,
            fill=0,
            default_value=1,
            dtype="uint8",
            all_touched=True,
        ).astype(bool)

        nodata = dataset.nodata
        if nodata is not None:
            mask &= ~np.isclose(data, nodata)

        cell_count = int(np.count_nonzero(mask))
        if cell_count:
            data[mask] = data[mask] + float(lift_m)
            dataset.write(data.astype(dataset.dtypes[0]), 1)

    print(
        f"Applied building lift of {float(lift_m):g} m to {cell_count} source DTM cells "
        f"using {buildings_shp_path}."
    )
    return cell_count


def _count_building_cells(raster_path: Path, buildings_shp_path: Path) -> int:
    """Count building-covered raster cells for a cached raised source DTM."""

    try:
        buildings_gdf = gpd.read_file(buildings_shp_path)
        if buildings_gdf.empty:
            return 0
        with rasterio.open(raster_path) as dataset:
            if dataset.crs is not None:
                if buildings_gdf.crs is None:
                    buildings_gdf = buildings_gdf.set_crs(dataset.crs, allow_override=True)
                elif buildings_gdf.crs != dataset.crs:
                    buildings_gdf = buildings_gdf.to_crs(dataset.crs)
            geometries = [
                geometry
                for geometry in buildings_gdf.geometry
                if geometry is not None and not geometry.is_empty
            ]
            if not geometries:
                return 0
            mask = rasterize(
                geometries,
                out_shape=(dataset.height, dataset.width),
                transform=dataset.transform,
                fill=0,
                default_value=1,
                dtype="uint8",
                all_touched=True,
            ).astype(bool)
            return int(np.count_nonzero(mask))
    except Exception:
        return 0


def _create_hecras_terrain_hdf(
    *,
    input_tif_path: Path | list[Path],
    output_hdf_path: Path,
    projection_prj_path: Path,
    units: str,
    hecras_version: str,
) -> None:
    """Create the HEC-RAS terrain HDF using ras_commander's terrain API."""

    try:
        from ras_commander import RasTerrain
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "HEC-RAS terrain HDF creation requires ras_commander. "
            "Install or expose the ras_commander package before running implementationDTM.py."
        ) from exc

    if isinstance(input_tif_path, (list, tuple)):
        input_rasters = [Path(path) for path in input_tif_path]
    else:
        input_rasters = [Path(input_tif_path)]

    output_hdf_path.parent.mkdir(parents=True, exist_ok=True)
    RasTerrain.create_terrain_hdf(
        input_rasters=input_rasters,
        output_hdf=output_hdf_path,
        projection_prj=projection_prj_path,
        units=units,
        stitch=True,
        hecras_version=hecras_version,
    )


def _safe_name(value: str) -> str:
    """Return a filesystem-safe component name for output folders."""

    safe = "".join(char if char.isalnum() or char in "-_" else "_" for char in str(value))
    return safe.strip("_") or "terrain_component"
