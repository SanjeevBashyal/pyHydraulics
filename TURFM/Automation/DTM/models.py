"""Small data containers shared by the modular DTM package."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TerrainHdfResult:
    """Result returned after preparing a HEC-RAS terrain HDF."""

    merged_tif_path: Path | None
    hdf_path: Path
    created: bool
    message: str
    exact_bank_tif_path: Path | None = None
    bank_channel_tif_path: Path | None = None
    bank_channel_mode: str | None = None
    base_terrain_tif_path: Path | None = None


@dataclass(frozen=True)
class RaisedTerrainResult:
    """Result returned after preparing the full-size building-raised DTM."""

    raised_tif_path: Path
    created: bool
    enabled: bool
    buildings_shp: str | None
    lift_m: float
    cells_lifted: int
    message: str
