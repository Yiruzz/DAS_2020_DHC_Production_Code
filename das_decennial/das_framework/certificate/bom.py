"""
Minimal stand-in for the `certificate` package, which is referenced by
das_framework/driver.py (lines 61 and 73) but is not included in the public
DAS 2020 DHC production code release.

driver.py uses get_bom() only in bom_files(), where the returned tuples are
appended to the bill of materials. Returning an empty sequence leaves the BOM
to be assembled from config.seen_files and the DAS_DIR filesystem walk, which
is what the rest of bom_files() already does.

Nothing in the DAS algorithm depends on this module.
"""


def get_bom(content=False):
    """Return an empty bill of materials.

    driver.py:927 iterates this as (name, path, ver, bytecount) 4-tuples.
    """
    return []
