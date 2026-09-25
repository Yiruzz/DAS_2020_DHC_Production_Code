#!/usr/bin/env python3
"""How far along a DAS run is, and roughly how much is left.

Two sources, because neither is enough alone:

  * the DAS log, for the phase -- reader, noisy measurements, which geolevel is
    being optimised, writer. Coarse, but it is the only thing that knows what
    the run is conceptually doing.
  * Spark's REST API on the driver UI (127.0.0.1:4040), for tasks completed out
    of tasks submitted in the stages running right now. This is the only signal
    with any resolution inside a geolevel, and inside a geolevel is where a
    national run spends nearly all of its time.

Why the second matters so much: the topdown loop optimises one geolevel
transition at a time, and the work of each is proportional to the number of
CHILD units it produces. Nationally that is

    National->State        51 units     0.03%
    State->County       3,108 units      2.0%
    County->Supdist     3,205 units      2.0%
    Supdist->Enumdist 152,009 units     96.0%

so a progress bar over geolevels would show four ticks and then sit at 96% to go
for hours. The Spark task counter is what moves during that time.

Usage:
    python deploy1940/progress.py               # one snapshot
    python deploy1940/progress.py --watch       # refresh until the run ends
    python deploy1940/progress.py --watch 30    # ... every 30 s
"""

import argparse
import glob
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

SPARK_UI = "http://127.0.0.1:4040/api/v1"

# Child units produced by each geolevel transition, nationally. The engine's
# work tracks these, not the geolevel count.
NATIONAL_CHILD_UNITS = {
    "State": 51,
    "County": 3_108,
    "Supdist": 3_205,
    "Enumdist": 152_009,
}
LEVELS_TOP_DOWN = ["National", "State", "County", "Supdist", "Enumdist"]

PHASES = [
    ("setup",      re.compile(r"Creating and running DAS setup")),
    ("reader",     re.compile(r"Creating and running DAS reader")),
    ("cef_hist",   re.compile(r"Saving joined CEF in histogram format")),
    ("cef_nodes",  re.compile(r"Saving CEF in GeounitNode format")),
    ("engine",     re.compile(r"Creating and running DAS engine")),
    ("noisy",      re.compile(r"Taking noisy measurements at (\w+)")),
    ("saving",     re.compile(r"Saving noisy answers")),
    ("optimized",  re.compile(r"Geolevel (\w+) has been optimized")),
    ("writer",     re.compile(r"Creating and running DAS writer")),
    ("done",       re.compile(r"Run completed")),
]


def newest_log():
    here = os.path.dirname(os.path.abspath(__file__))
    pattern = os.path.join(os.path.dirname(here), "das_decennial", "logs", "*.log")
    files = sorted(glob.glob(pattern), key=os.path.getmtime)
    return files[-1] if files else None


def read_phases(path):
    """Last phase seen, plus which geolevels have been measured and optimised."""
    seen, measured, optimized = [], [], []
    try:
        with open(path, "r", errors="replace") as fh:
            for line in fh:
                for name, pat in PHASES:
                    m = pat.search(line)
                    if not m:
                        continue
                    seen.append(name)
                    if name == "noisy" and m.group(1) not in measured:
                        measured.append(m.group(1))
                    if name == "optimized" and m.group(1) not in optimized:
                        optimized.append(m.group(1))
    except OSError as e:
        return None, [], [], str(e)
    return (seen[-1] if seen else None), measured, optimized, None


def spark_get(path):
    try:
        with urllib.request.urlopen(SPARK_UI + path, timeout=3) as r:
            return json.loads(r.read().decode())
    except (urllib.error.URLError, OSError, ValueError):
        return None


def spark_active_stages():
    apps = spark_get("/applications")
    if not apps:
        return None, []
    app_id = apps[0]["id"]
    stages = spark_get(f"/applications/{app_id}/stages") or []
    active = [s for s in stages
              if s.get("status") in ("ACTIVE", "PENDING") and s.get("numTasks")]
    return app_id, active


def bar(done, total, width=34):
    if not total:
        return "[" + " " * width + "]"
    filled = int(width * done / total)
    return "[" + "#" * filled + "-" * (width - filled) + f"] {100*done/total:5.1f}%"


def engine_fraction(optimized):
    """Share of the engine's work already done, weighted by child units."""
    total = sum(NATIONAL_CHILD_UNITS.values())
    done = sum(NATIONAL_CHILD_UNITS.get(lv, 0) for lv in optimized)
    return done / total, total


def snapshot(logpath):
    phase, measured, optimized, err = read_phases(logpath)
    print(f"log        {logpath}")
    if err:
        print(f"           unreadable: {err}")
        return False
    age = time.time() - os.path.getmtime(logpath)
    print(f"phase      {phase or '(nothing matched yet)'}    "
          f"last write {age:,.0f}s ago")

    if phase == "done":
        print("\nRun completed.")
        return False

    print(f"noisy      {len(measured)}/5 geolevels   {' '.join(measured) or '-'}")

    frac, _ = engine_fraction(optimized)
    print(f"optimized  {' '.join(optimized) or '-'}")
    print(f"  engine   {bar(frac, 1.0)}   (weighted by child units, national)")
    nxt = next((lv for lv in LEVELS_TOP_DOWN[1:] if lv not in optimized), None)
    if nxt:
        print(f"  next     {nxt}  ({NATIONAL_CHILD_UNITS.get(nxt, 0):,} units, "
              f"{100*NATIONAL_CHILD_UNITS.get(nxt,0)/sum(NATIONAL_CHILD_UNITS.values()):.0f}% of engine work)")

    app_id, active = spark_active_stages()
    print()
    if app_id is None:
        print("spark      driver UI not reachable on 127.0.0.1:4040")
        print("           (the run may be between stages, or finished)")
    elif not active:
        print(f"spark      {app_id}: no active stage right now")
    else:
        print(f"spark      {app_id}")
        for s in active[:4]:
            done = s.get("numCompleteTasks", 0)
            total = s.get("numTasks", 0)
            name = (s.get("name") or "")[:46]
            print(f"  stage {s.get('stageId'):>4}  {bar(done, total)}  "
                  f"{done:,}/{total:,}")
            print(f"             {name}")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log", nargs="?", default=None)
    ap.add_argument("--watch", nargs="?", const=20, type=int, default=None,
                    help="refresh every N seconds (default 20)")
    args = ap.parse_args()

    logpath = args.log or newest_log()
    if not logpath:
        sys.exit("no log found under das_decennial/logs/")

    if args.watch is None:
        snapshot(logpath)
        return
    try:
        while True:
            os.system("clear" if os.name != "nt" else "cls")
            print(time.strftime("%H:%M:%S"))
            if not snapshot(logpath):
                return
            time.sleep(args.watch)
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
