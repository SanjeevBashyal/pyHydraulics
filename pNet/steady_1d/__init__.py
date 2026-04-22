__all__ = [
    "HydraulicParams",
    "backward_pass",
    "forward_pass",
    "mixed_profile",
    "solve_channel",
]


def __getattr__(name):
    if name in __all__:
        from . import channel

        return getattr(channel, name)
    raise AttributeError(name)
