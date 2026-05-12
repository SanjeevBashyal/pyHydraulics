"""HEC-RAS terrain preparation helpers for DTM outputs.

This module deliberately owns the terrain-HDF workflow instead of borrowing
from the v01 RAS Mapper scripts.  `implementationDTM.py` can therefore raise
buildings on the full original DTM first, interpolate the channel from that
raised source, and produce the final terrain package in `3 DTM`.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
from collections.abc import Iterable

import numpy as np
import rasterio
from rasterio.features import rasterize

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
    channel_terrain_resolution_m: float = 0.1,
    channel_terrain_outward_buffer_m: float = 20.0,
) -> TerrainHdfResult:
    """Create a HEC-RAS HDF from the interpolated terrain over the base DTM."""

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
    if not interpolated_tif_path.exists():
        raise FileNotFoundError(f"Interpolated channel terrain GeoTIFF was not found: {interpolated_tif_path}")
    if not original_dtm_path.exists():
        raise FileNotFoundError(f"Base DTM GeoTIFF was not found: {original_dtm_path}")

    component_root = dtm_root / safe_component
    terrain_dir = component_root / "Terrain"
    terrain_dir.mkdir(parents=True, exist_ok=True)

    hdf_path = terrain_dir / f"{safe_component}_original_plus_channel.hdf"

    base_terrain_tif_path = original_dtm_path
    channel_terrain_tif_path = interpolated_tif_path
    hdf_input_paths = [channel_terrain_tif_path, base_terrain_tif_path]
    hdf_marker_payload = _terrain_hdf_marker_payload(
        input_tif_paths=hdf_input_paths,
        merge_mode="direct_interpolated_over_base",
        builder_version=2,
    )

    hdf_dependency_mtime = max(
        base_terrain_tif_path.stat().st_mtime,
        channel_terrain_tif_path.stat().st_mtime,
        projection_prj_path.stat().st_mtime,
    )
    if (
        hdf_path.exists()
        and hdf_path.stat().st_mtime >= hdf_dependency_mtime
        and _terrain_hdf_marker_matches(hdf_path, hdf_marker_payload)
    ):
        return TerrainHdfResult(
            merged_tif_path=None,
            hdf_path=hdf_path,
            created=False,
            message="Existing HEC-RAS terrain HDF is current.",
            bank_channel_tif_path=channel_terrain_tif_path,
            bank_channel_mode="direct_interpolated_terrain",
            base_terrain_tif_path=base_terrain_tif_path,
        )

    _create_hecras_terrain_hdf(
        input_tif_path=hdf_input_paths,
        output_hdf_path=hdf_path,
        projection_prj_path=projection_prj_path,
        units=units,
        hecras_version=hecras_version,
    )
    _write_terrain_hdf_marker(hdf_path, hdf_marker_payload)
    return TerrainHdfResult(
        merged_tif_path=None,
        hdf_path=hdf_path,
        created=True,
        message="Created HEC-RAS terrain HDF from interpolated terrain and base DTM.",
        bank_channel_tif_path=channel_terrain_tif_path,
        bank_channel_mode="direct_interpolated_terrain",
        base_terrain_tif_path=base_terrain_tif_path,
    )


def _terrain_hdf_marker_payload(
    *,
    input_tif_paths: list[Path],
    merge_mode: str,
    builder_version: int,
) -> dict:
    """Build cache metadata for the terrain HDF input stack."""

    return {
        "input_rasters": [str(path) for path in input_tif_paths],
        "merge_mode": merge_mode,
        "builder_version": int(builder_version),
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

    _print_hecras_terrain_sources(
        input_rasters=input_rasters,
        output_hdf_path=output_hdf_path,
        projection_prj_path=projection_prj_path,
        units=units,
        hecras_version=hecras_version,
    )
    _clean_hecras_terrain_sidecars(output_hdf_path)

    output_hdf_path.parent.mkdir(parents=True, exist_ok=True)
    RasTerrain.create_terrain_hdf(
        input_rasters=input_rasters,
        output_hdf=output_hdf_path,
        projection_prj=projection_prj_path,
        units=units,
        stitch=True,
        hecras_version=hecras_version,
    )


def _print_hecras_terrain_sources(
    *,
    input_rasters: list[Path],
    output_hdf_path: Path,
    projection_prj_path: Path,
    units: str,
    hecras_version: str,
) -> None:
    """Print the exact HEC-RAS terrain source stack used for debugging."""

    print("Preparing HEC-RAS terrain HDF from source TIFFs:")
    print(f"  Output HDF: {output_hdf_path}")
    print(f"  Projection: {projection_prj_path}")
    print(f"  Units: {units}")
    print(f"  HEC-RAS version: {hecras_version}")
    for index, raster_path in enumerate(input_rasters, start=1):
        priority = "highest priority" if index == 1 else "lower priority"
        details = ""
        try:
            with rasterio.open(raster_path) as dataset:
                details = (
                    f" | size={dataset.width}x{dataset.height}"
                    f" | res={dataset.res}"
                    f" | nodata={dataset.nodata}"
                )
        except Exception as exc:
            details = f" | metadata unavailable: {exc}"
        print(f"  Input {index} ({priority}): {raster_path}{details}")


def _clean_hecras_terrain_sidecars(output_hdf_path: Path) -> None:
    """Remove stale HEC-RAS terrain sidecar rasters for this output basename."""

    output_dir = output_hdf_path.parent
    if not output_dir.exists():
        return

    patterns = [
        f"{output_hdf_path.stem}.*.tif",
        f"{output_hdf_path.stem}.*.tif.aux.xml",
        f"{output_hdf_path.stem}.*.vrt",
        f"{output_hdf_path.stem}.*.ovr",
    ]
    removed = []
    for pattern in patterns:
        for path in output_dir.glob(pattern):
            if not path.is_file():
                continue
            path.unlink()
            removed.append(path)
    if removed:
        print("Removed stale HEC-RAS terrain sidecar raster(s):")
        for path in removed:
            print(f"  {path}")


def _safe_name(value: str) -> str:
    """Return a filesystem-safe component name for output folders."""

    safe = "".join(char if char.isalnum() or char in "-_" else "_" for char in str(value))
    return safe.strip("_") or "terrain_component"
