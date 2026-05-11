"""Public DTM channel modifier facade.

The original DTMChannelModifier class grew into a very large single file.  The
public class remains here for backwards compatibility, but its methods now live
in focused mixin modules so interpolation, network handling, and vector exports
can be read independently.
"""

from __future__ import annotations

from . import bank_vectors as _bank_vectors
from . import core as _core
from . import exports as _exports
from . import geometry as _geometry
from . import interpolation as _interpolation
from . import junction as _junction
from . import network as _network
from .bank_vectors import BankVectorMixin
from .core import CoreMixin
from .exports import ExportMixin
from .geometry import GeometryMixin
from .interpolation import InterpolationMixin
from .junction import JunctionInterpolationMixin
from .network import NetworkMixin


class DTMChannelModifier(
    CoreMixin,
    GeometryMixin,
    JunctionInterpolationMixin,
    InterpolationMixin,
    NetworkMixin,
    ExportMixin,
    BankVectorMixin,
):
    """Backwards-compatible facade for channel DTM modification.

    Use the public static methods exactly as before, for example
    ``DTMChannelModifier.process_channel_network_dtm(...)``.  The class is now
    assembled from mixins purely to keep the source code navigable.
    """


for _module in (_core, _geometry, _junction, _interpolation, _network, _exports, _bank_vectors):
    _module.DTMChannelModifier = DTMChannelModifier


__all__ = ["DTMChannelModifier"]
