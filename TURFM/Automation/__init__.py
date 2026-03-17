"""
pyHydraulics - A Python package for hydraulic engineering calculations and HEC-RAS automation.
"""

from .solver import *
from .hecras import HECRAS

__version__ = "0.1.0"
__all__ = ["HECRAS"]