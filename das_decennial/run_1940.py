#!/usr/bin/env python3
"""
Entry point for running the DAS on the 1940 IPUMS data outside the Census
Bureau's EMR environment.

WHY THIS FILE EXISTS
--------------------
The release has two entry points, and neither works for this case as published.

das2020_driver.py is the production one. It runs a dashboard heartbeat thread,
posts testpoints over HTTP, and typesets a LaTeX certificate -- all of which
assume Census infrastructure that does not exist on a standalone host.

das_framework/driver.py is the standalone one (it is what standalone/
run_standalone.sh invokes) and is a complete driver on its own. But its
__main__ block at driver.py:1242-1245 calls main_make_das() without a delegate,
leaving DAS.delegate as None (driver.py:635,647).

That is fine for every lifecycle hook, because those are all guarded:

    if hasattr(self.delegate, 'willRunReader'):      # driver.py:765
        self.delegate.willRunReader(...)

but log_testpoint is called unguarded from inside the modules -- 23 call sites,
including programs/reader/table_reader.py:574 and
programs/engine/topdown_engine.py:208,294 -- so a run dies partway through the
reader with:

    AttributeError: 'NoneType' object has no attribute 'log_testpoint'

das2020_driver.py:607 avoids this by constructing a DASDelegate and passing it
through main_make_das(args, config, delegate=delegate), which forwards it to
DAS via **kwargs (driver.py:1171).

This file does the same thing with the no-op delegate the release already ships
for exactly this purpose, das_framework/das_stub.py:13. Testpoints become
no-ops; nothing else changes. The driver, reader, engine, writer and validator
are the published ones, untouched.

Note that only StubDelegate is used, not DASStub: DASStub.__init__ raises when
MISSION_NAME is set in the environment, and that variable has to be defined on a
standalone host to satisfy other parts of the code.

USAGE
-----
    spark-submit [spark options] run_1940.py <config.ini> [driver options]

Identical to invoking das_framework/driver.py, which it delegates to.
"""

import os
import sys

# das_framework/ must be importable for `import dfxml_writer` and friends, which
# das_framework's own modules do as top-level imports.
_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(_HERE, "das_framework")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from das_framework.driver import main_setup, main_make_das, main_run_das
from das_framework.das_stub import StubDelegate


def main():
    args, config = main_setup()
    das = main_make_das(args, config, delegate=StubDelegate())
    main_run_das(das)


if __name__ == "__main__":
    main()
