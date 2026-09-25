"""
Development stand-in for `dprng`, which is NOT included in the public
DAS 2020 DHC code release but is imported unconditionally by
programs/engine/primitives.py:5.

Used only by RationalDiscreteGaussianMechanism (primitives.py:240). The 1940
path uses RationalGeometricMechanism -- pure DP with the geometric mechanism,
the engine defaults at budget.py:32-33 -- and never reaches it.

discrete_gaussian() raises on purpose: if a config ever asks for the discrete
Gaussian mechanism we want a loud failure, not a silent divergence.
"""


class OverflowBoundError(Exception):
    """Keeps the `except` clause at primitives.py:242 valid."""


class _OverflowStrategy:
    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs


class PerDrawOverflowStrategy(_OverflowStrategy):
    pass


class TotalOverflowStrategy(_OverflowStrategy):
    pass


def discrete_gaussian(*args, **kwargs):
    raise NotImplementedError(
        "dprng is not in the public DAS release. The 1940 path uses the "
        "geometric mechanism and should never reach this."
    )


class DPRNGBitGenerator:
    def __init__(self, *args, **kwargs):
        raise NotImplementedError("dprng is not in the public DAS release.")


def random_bytes(*args, **kwargs):
    raise NotImplementedError("dprng is not in the public DAS release.")
