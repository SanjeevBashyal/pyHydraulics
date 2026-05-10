"""Modular DTM automation package.

Public imports:
- `DTM` orchestrates project/sub-project DTM processing.
- `DTMChannelModifier` contains the interpolation engine assembled from
  focused mixin modules.
- `prepare_component_terrain_hdf` merges the interpolated raster with the
  original DTM and writes a HEC-RAS terrain HDF under `3 DTM`.
"""

from .channel_modifier import DTMChannelModifier
from .controller import DTM
from .models import RaisedTerrainResult, TerrainHdfResult
from .terrain_hdf import prepare_building_raised_original_dtm, prepare_component_terrain_hdf

__all__ = [
    "DTM",
    "DTMChannelModifier",
    "RaisedTerrainResult",
    "TerrainHdfResult",
    "prepare_building_raised_original_dtm",
    "prepare_component_terrain_hdf",
]
