"""HEC-RAS terrain preparation helpers for DTM outputs.

This module deliberately owns the terrain-HDF workflow instead of borrowing
from the v01 RAS Mapper scripts.  `implementationDTM.py` can therefore raise
buildings on the full original DTM first, interpolate the channel from that
raised source, and produce the final terrain package in `3 DTM`.
"""

from __future__ import annotations

from pathlib import Path
import shutil

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
    units: str = "Meters",
    hecras_version: str = "7.0",
    resampling: str = "nearest",
) -> TerrainHdfResult:
    """Merge the channel terrain with the original DTM and create a HEC-RAS HDF.

    The interpolated raster is usually a cropped window around the channel.  To
    create a complete terrain for HEC-RAS, we copy the original DTM, overlay
    valid interpolated channel cells onto that copy, and pass the merged GeoTIFF
    to `ras_commander.RasTerrain.create_terrain_hdf`. The HEC-RAS terrain HDF is
    created from the high-resolution channel raster first and the source DTM
    second so RAS Mapper keeps the 0.1 m channel grid in the overlap.
    """

    original_dtm_path = Path(original_dtm_path)
    interpolated_tif_path = Path(interpolated_tif_path)
    projection_prj_path = Path(projection_prj_path)
    dtm_root = Path(dtm_root)
    safe_component = _safe_name(component_name)

    if not projection_prj_path.exists():
        raise FileNotFoundError(
            f"Projection file required for HEC-RAS terrain HDF was not found: "
            f"{projection_prj_path}"
        )

    component_root = dtm_root / safe_component
    merged_dir = component_root / "merged_terrain"
    terrain_dir = component_root / "Terrain"
    merged_dir.mkdir(parents=True, exist_ok=True)
    terrain_dir.mkdir(parents=True, exist_ok=True)

    merged_tif = merged_dir / f"{safe_component}_original_plus_channel.tif"
    hdf_path = terrain_dir / f"{merged_tif.stem}.hdf"

    newest_source_mtime = max(
        original_dtm_path.stat().st_mtime,
        interpolated_tif_path.stat().st_mtime,
    )
    if merged_tif.exists() and merged_tif.stat().st_mtime >= newest_source_mtime:
        merged_created = False
    else:
        _merge_interpolated_tif_over_original(
            original_dtm_path=original_dtm_path,
            interpolated_tif_path=interpolated_tif_path,
            output_tif_path=merged_tif,
            resampling=resampling,
        )
        merged_created = True

    hdf_dependency_mtime = max(
        interpolated_tif_path.stat().st_mtime,
        original_dtm_path.stat().st_mtime,
        projection_prj_path.stat().st_mtime,
    )
    if hdf_path.exists() and hdf_path.stat().st_mtime >= hdf_dependency_mtime:
        return TerrainHdfResult(
            merged_tif_path=merged_tif,
            hdf_path=hdf_path,
            created=False,
            message="Existing HEC-RAS terrain HDF is current.",
        )

    _create_hecras_terrain_hdf(
        input_tif_path=[interpolated_tif_path, original_dtm_path],
        output_hdf_path=hdf_path,
        projection_prj_path=projection_prj_path,
        units=units,
        hecras_version=hecras_version,
    )
    return TerrainHdfResult(
        merged_tif_path=merged_tif,
        hdf_path=hdf_path,
        created=True,
        message=(
            "Created HEC-RAS terrain HDF."
            if merged_created
            else "Created HEC-RAS terrain HDF from existing merged GeoTIFF."
        ),
    )


def _merge_interpolated_tif_over_original(
    *,
    original_dtm_path: Path,
    interpolated_tif_path: Path,
    output_tif_path: Path,
    resampling: str,
) -> None:
    """Overlay valid interpolated channel cells on a copy of the original DTM."""

    output_tif_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(original_dtm_path, output_tif_path)
    resampling_method = getattr(Resampling, resampling, Resampling.bilinear)

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

        destination[valid] = reprojected[valid]
        dst.write(destination.astype(dst.dtypes[0]), 1, window=window)


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
