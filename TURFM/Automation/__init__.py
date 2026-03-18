"""
pyHydraulics - A Python package for hydraulic engineering calculations and HEC-RAS automation.
"""

from .hecras import HECRAS
from .DTM import DTMChannelModifier

__version__ = "0.1.0"
__all__ = ["HECRAS", "DTMChannelModifier"]
