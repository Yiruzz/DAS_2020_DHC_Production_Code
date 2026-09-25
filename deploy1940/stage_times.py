#!/usr/bin/env python3
"""Per-stage timings of a DAS run, and what they project to at national scale.

The DFXML file is the structured source: driver.py calls self.timestamp() at
every stage boundary (:760 reader, :775 engine, :789 error_metrics, :803 writer,
:817 validator, :846 takedown) and for every annotation (:755), and
dfxml_writer.timestamp() (:174) records each as

    <timestamp name="..." delta="<seconds since previous>" total="<since start>"/>

so no log parsing heuristics are needed. The stdout log (out/alaska.log) carries
no timestamps at all, which is why it is the wrong file to measure from.

Usage:
    python deploy1940/stage_times.py [file.dfxml] [--units N] [--records N]

With no file it takes the newest under <clone>/das_decennial/logs/.
--units and --records describe the run that produced the file, so the national
projection knows what to scale from; they default to the Alaska subset.
"""

import argparse
import glob
import os
import sys
import xml.etree.ElementTree as ET

# Measured over the whole 1940 file; see RUNNING_1940.md.
NATIONAL_UNITS = 158_374          # geographic units including National
NATIONAL_RECORDS = 132_404_766    # persons
NATIONAL_PARENTS = 6_365          # 1 + 51 states + 3,108 counties + 3,205 supdists

ALASKA_UNITS = 239
ALASKA_RECORDS = 72_665
ALASKA_PARENTS = 10               # 1 + 1 + 4 + 4

# Which stage scales with what. The engine solves one optimisation per parent
# node group and each model is (children x 44,544 cells), and the average
# children per parent barely moves between one state and the nation (~24 vs
# ~25) -- so the engine tracks parent count, not population. The reader and the
# writer move one record at a time and track population.
BY_RECORDS = ("reader", "writer")
BY_PARENTS = ("engine",)

STAGES = [
    ("setup",         "Creating and running DAS setup"),
    ("reader",        "runReader:"),
    ("engine",        "runEngine:"),
    ("error_metrics", "runErrorMetrics:"),
    ("writer",        "runWriter:"),
    ("validator",     "runValidator:"),
    ("takedown",      "runTakedown:"),
]


def newest_dfxml():
    here = os.path.dirname(os.path.abspath(__file__))
    pattern = os.path.join(os.path.dirname(here), "das_decennial", "logs", "*.dfxml")
    files = sorted(glob.glob(pattern), key=os.path.getmtime)
    if not files:
        sys.exit(f"no .dfxml under {pattern} -- has a run completed?")
    return files[-1]


def read_timestamps(path):
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as e:
        sys.exit(f"{path} is not parseable ({e}). A run that died before its "
                 f"atexit handler leaves it truncated.")
    out = []
    for el in root.iter("timestamp"):
        try:
            out.append((el.get("name", ""), float(el.get("delta", 0)),
                        float(el.get("total", 0))))
        except ValueError:
            continue
    if not out:
        sys.exit(f"{path} carries no <timestamp> elements.")
    return out


def stage_durations(stamps):
    """Wall time from each stage marker to the next one, in file order."""
    marks = []
    for i, (name, _delta, total) in enumerate(stamps):
        for key, needle in STAGES:
            if needle in name:
                marks.append((key, total, i))
                break
    end = stamps[-1][2]
    durations = []
    for j, (key, total, _i) in enumerate(marks):
        stop = marks[j + 1][1] if j + 1 < len(marks) else end
        durations.append((key, stop - total))
    return durations, end


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dfxml", nargs="?", default=None)
    ap.add_argument("--units", type=int, default=ALASKA_UNITS)
    ap.add_argument("--records", type=int, default=ALASKA_RECORDS)
    ap.add_argument("--parents", type=int, default=ALASKA_PARENTS)
    args = ap.parse_args()

    path = args.dfxml or newest_dfxml()
    stamps = read_timestamps(path)
    durations, total = stage_durations(stamps)

    print(f"file      {path}")
    print(f"measured  {args.units:,} units / {args.parents:,} parent groups / "
          f"{args.records:,} records")
    print(f"total     {total:,.1f} s")
    print()
    print(f"  {'stage':<15}{'seconds':>10}{'share':>9}   scales with")
    print(f"  {'-' * 15}{'-' * 10:>10}{'-' * 9:>9}   {'-' * 12}")
    by_record_s = by_parent_s = flat_s = 0.0
    for key, secs in durations:
        if key in BY_RECORDS:
            basis, by_record_s = "records", by_record_s + secs
        elif key in BY_PARENTS:
            basis, by_parent_s = "parent groups", by_parent_s + secs
        else:
            basis, flat_s = "(flat)", flat_s + secs
        share = 100 * secs / total if total else 0
        print(f"  {key:<15}{secs:>10.1f}{share:>8.1f}%   {basis}")

    r = NATIONAL_RECORDS / args.records
    p = NATIONAL_PARENTS / args.parents
    print()
    print(f"scale factors: records x{r:,.0f}   parent groups x{p:,.0f}")
    print()
    projected = by_record_s * r + by_parent_s * p + flat_s
    print(f"  national projection, same parallelism   "
          f"{projected / 3600:>8.1f} h   ({projected / 86400:.1f} days)")
    print(f"    of which reader+writer                {by_record_s * r / 3600:>8.1f} h")
    print(f"    of which engine                       {by_parent_s * p / 3600:>8.1f} h")
    print()
    print("Two corrections this does NOT apply, pulling opposite ways:")
    print("  - parallelism improves with scale. This run's lowest level had")
    print(f"    {args.parents - 6 if args.parents > 6 else args.parents} bottom groups over 20 workers; the nation has 3,205,")
    print("    so the engine term is an overestimate, perhaps several-fold.")
    print("  - I/O grows with the checkpoints. Alaska wrote ~306 MB of noisy")
    print("    measurements; the nation writes ~200 GB, so the flat and")
    print("    reader/writer terms are underestimates.")
    print()
    print("Measure a second, larger state before trusting either: two points")
    print("beat one extrapolation across three orders of magnitude.")


if __name__ == "__main__":
    main()
