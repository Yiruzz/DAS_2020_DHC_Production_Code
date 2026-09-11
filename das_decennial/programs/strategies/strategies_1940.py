"""
Budget strategy and query ordering for the IPUMS 1940 schema.

WHY THIS FILE EXISTS
--------------------
The 1940 configuration files shipped in this release
(configs/Census1940/DDP2010_Update/ipums_1940.ini) declare their privacy
budget the way the DAS did before the strategy refactor: with `dpqueries`
and `queriesprop` lists inside the .ini. The engine in this release reads
those from code instead -- programs/engine/budget.py:402 requires
`[budget] strategy` to name a class registered in StrategySelector, and
programs/engine/optimizer_query_ordering.py:84 requires
`[budget] query_ordering` to name one registered in QueryOrderingSelector.
No 1940 entry exists in either registry, so the 1940 configs cannot run.

The values below are transcribed from the shipped ipums_1940.ini so that the
allocation stays the one the Census Bureau documented for the 1940
demonstration run. Two values could not be transcribed and were chosen here;
both are called out in RECONSTRUCTED below.

TRANSCRIBED from ipums_1940.ini:

    dpqueries   = hhgq1940,
                  age1940 * hispanic1940 * cenrace1940 * citizen1940,
                  age1940 * sex1940,
                  ageGroups4 * sex1940,
                  ageGroups16 * sex1940,
                  ageGroups64 * sex1940,
                  detailed
    queriesprop = .2, .5, .05, .05, .05, .05, .1

    Expressed here as exact Fractions over a denominator of 20
    (4, 10, 1, 1, 1, 1, 2), which sum to 1. The engine works in Fractions,
    so the decimals in the .ini are given exactly rather than as floats.

RECONSTRUCTED (not recoverable from the shipped config):

 1. geolevel budget proportions. ipums_1940.ini carries seven values
    (0.2, 0.2, 0.12 x5) against the five geolevels it declares in
    [geodict]. The seven values sum to 1 and match a seven-geolevel 2020
    spine, so that line was carried over from a DHCP config and never
    adjusted to the 1940 geography. A uniform split across the five 1940
    geolevels is used instead, following the precedent set by this
    repository's other 1940 config, configs/Census1940/topdown.ini, which
    allocates uniformly (0.25 across its four geolevels).
    The proportions themselves live in the config, not here.

 2. query ordering. The ordering options (L2/rounder passes) postdate the
    1940 configs entirely, so there is no original choice to preserve. A
    single-pass ordering is used, which is what the 1940 config's implicit
    defaults select: L2PlusRounder (not interleaved), SinglePassRegular and
    CellWiseRounder -- see programs/engine/optimizer_query_ordering.py:75-78.

Nothing in this module changes the DAS algorithm. It only supplies the
configuration surface the release does not carry for 1940.
"""

from fractions import Fraction as Fr
from collections import defaultdict

from das_constants import CC


# Geolevels of the 1940 spine, smallest to largest, matching [geodict]
# geolevel_names in the 1940 configs. Used only as a fallback: in a real DAS
# run the levels are read from the config and passed into make().
LEVELS_1940 = ("Enumdist", "Supdist", "County", "State", "National")


# The seven measured queries, in the order given by ipums_1940.ini.
DPQUERIES_1940 = (
    "hhgq1940",
    "age1940 * hispanic1940 * cenrace1940 * citizen1940",
    "age1940 * sex1940",
    "ageGroups4 * sex1940",
    "ageGroups16 * sex1940",
    "ageGroups64 * sex1940",
    "detailed",
)

# queriesprop from ipums_1940.ini as exact Fractions: .2 .5 .05 .05 .05 .05 .1
_DENOM = 20
QUERIESPROP_1940 = tuple(Fr(num, _DENOM) for num in (4, 10, 1, 1, 1, 1, 2))


class Strategy1940:
    """Per-geolevel DP query allocation for the 1940 schema.

    The same allocation is applied at every geolevel, which is what
    ipums_1940.ini specifies: it gives a single `queriesprop` line with no
    per-geolevel overrides.
    """

    schema = CC.SCHEMA_1940
    levels = LEVELS_1940

    def make(self, levels):
        levels2make = levels if levels else self.levels

        # defaultdict so the unit-histogram keys resolve to empty mappings:
        # the 1940 config measures no unit DP queries.
        strategy = defaultdict(lambda: defaultdict(dict))
        strategy.update({
            CC.GEODICT_GEOLEVELS: levels2make,
            CC.DPQUERIES + "default": DPQUERIES_1940,
            CC.QUERIESPROP + "default": QUERIESPROP_1940,
        })

        for level in strategy[CC.GEODICT_GEOLEVELS]:
            strategy[CC.DPQUERIES][level] = strategy[CC.DPQUERIES + "default"]
            strategy[CC.QUERIESPROP][level] = strategy[CC.QUERIESPROP + "default"]

        strategy[CC.GEOLEVELS] = levels2make
        return strategy


class Strategy1940RegularOrdering:
    """Single-pass L2 and rounder ordering for the 1940 schema.

    Every measured query must appear in the L2 ordering or
    optimizer_query_ordering.py:133 raises. All seven are targeted in one
    pass, with `detailed` last.

    The single level of nesting ({pass: (queries,)}) is the shape the
    non-interleaved L2PlusRounder path expects -- see the `if not outer_pass`
    branches at optimizer_query_ordering.py:93 and :115.
    """

    @staticmethod
    def make(levels):
        ordering = {
            CC.L2_QUERY_ORDERING: {
                0: DPQUERIES_1940,
            },
            CC.L2_CONSTRAIN_TO_QUERY_ORDERING: {
                0: DPQUERIES_1940,
            },
            CC.ROUNDER_QUERY_ORDERING: {
                0: DPQUERIES_1940,
            },
        }

        query_ordering = {}
        for geolevel in levels:
            query_ordering[geolevel] = {
                CC.L2_QUERY_ORDERING: ordering[CC.L2_QUERY_ORDERING],
                CC.L2_CONSTRAIN_TO_QUERY_ORDERING: ordering[CC.L2_CONSTRAIN_TO_QUERY_ORDERING],
                CC.ROUNDER_QUERY_ORDERING: ordering[CC.ROUNDER_QUERY_ORDERING],
            }
        return query_ordering
