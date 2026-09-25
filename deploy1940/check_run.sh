#!/bin/bash
# Summarise the Alaska run. Filters the two high-volume noise sources:
#   - "Can't find census env file"  ctools/env.py:102, one per Spark worker
#   - "config[...] does not exist"  getconfig calls whose NoOptionError is caught
# Paths are derived from this script location, not assumed:
#   DEPLOY     <clone>/deploy1940   this directory
#   REPO_ROOT  <clone>              the working tree
#   BASE       <clone>/..           venv, JDK, data, out -- untracked
DEPLOY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$DEPLOY")"
BASE="$(dirname "$REPO_ROOT")"
LOG="$BASE/out/alaska.log"
NOISE="Can't find census env file|does not exist and no default|not present; returning default"
[ -r "$LOG" ] || { echo "no log at $LOG"; exit 1; }

printf '== process\n'
if pgrep -u "$USER" -f run_1940.py >/dev/null 2>&1; then
  echo "  RUNNING  $(pgrep -u "$USER" -f run_1940.py | tr '\n' ' ')"
  ps -o pcpu=,etime=,rss= -p "$(pgrep -u "$USER" -f run_1940.py | head -1)" 2>/dev/null \
    | awk '{printf "           cpu %s%%  elapsed %s  rss %.1fGB\n", $1, $2, $3/1048576}'
else
  echo "  not running (finished or died)"
fi
printf '  log %s   last write %s   load %s\n' \
  "$(du -h "$LOG" | cut -f1)" "$(date -r "$LOG" '+%H:%M:%S')" "$(cut -d' ' -f1-3 /proc/loadavg)"

printf '\n== pipeline stage\n'
for m in "Creating and running DAS setup object" "Creating and running DAS reader" \
         "Saving joined CEF in histogram format" "Reloading joined CEF" \
         "Generating noisy answers" "Saving noisy answers" \
         "Creating and running DAS engine" "Creating and running DAS writer" \
         "Run completed"; do
  n=$(grep -cF "$m" "$LOG" 2>/dev/null)
  [ "$n" -gt 0 ] && printf '  [x] %s\n' "$m" || printf '  [ ] %s\n' "$m"
done

printf '\n== geolevel progress (real markers, not config echo)\n'
printf '  %-10s %-18s %-18s\n' "" "noisy measured" "optimized"
for g in National State County Supdist Enumdist; do
  a=$(grep -cE "Taking noisy measurements at $g\b" "$LOG" 2>/dev/null)
  b=$(grep -cE "Geolevel $g has been optimized" "$LOG" 2>/dev/null)
  printf '  %-10s %-18s %-18s\n' "$g" "$([ "$a" -gt 0 ] && echo yes || echo -)" "$([ "$b" -gt 0 ] && echo yes || echo -)"
done
printf '  rows per level:\n'
grep -oE "Geolevel [A-Za-z]+ RDD has [0-9]+ rows" "$LOG" 2>/dev/null | sed 's/^/    /' | tail -6

printf '\n== reader counts (expect 24277 households / 72665 persons for Alaska)\n'
grep -oiE "[0-9]+ (records|rows|persons|households|units)" "$LOG" 2>/dev/null | sort -u | head -8 | sed 's/^/  /'

printf '\n== hadoop shim activity\n'
grep -c "hadoop-shim:" "$LOG" 2>/dev/null | sed 's/^/  calls: /'
grep "hadoop-shim:" "$LOG" 2>/dev/null | tail -3 | cut -c1-150 | sed 's/^/  /'

printf '\n== dashboard sink (must be alive; see HANDOFF 5.9)\n'
SINK_PORT="${DAS_DASHBOARD_SINK_PORT:-8940}"
if pgrep -u "$USER" -f "dashboard_sink.py --port $SINK_PORT" >/dev/null 2>&1; then
  printf '  ALIVE on %s   messages captured: %s\n' "$SINK_PORT" \
    "$(grep -c '' "$BASE/out/dashboard.jsonl" 2>/dev/null || echo 0)"
else
  printf '  DEAD -- the optimisation stage dies on NoRegionError at the next\n'
  printf '         branching model (optimizer.py:797 -> dashboard.py:398)\n'
fi

printf '\n== real errors\n'
grep -nEi "Traceback|^[A-Za-z_.]*(Error|Exception):|GurobiError|NoRegionError|UnboundLocalError|infeasible|OutOfMemory|Killed|REFUSING" "$LOG" 2>/dev/null \
  | grep -viE "$NOISE" | cut -c1-200 | tail -12 | sed 's/^/  /' || echo "  none"

printf '\n== completion\n'
grep -nE "Run completed|Elapsed time" "$LOG" 2>/dev/null | tail -3 | sed 's/^/  /' || echo "  not yet"

printf '\n== output\n'
du -sh "$BASE/out" 2>/dev/null | sed 's/^/  /'
find "$BASE/out" -maxdepth 2 -type d 2>/dev/null | tail -5 | sed 's/^/  /'

printf '\n== last 12 meaningful lines\n'
grep -vE "$NOISE" "$LOG" | tail -12 | cut -c1-175 | sed 's/^/  /'
