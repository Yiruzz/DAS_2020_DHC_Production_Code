#!/bin/bash
# Run several national DAS runs back to back, unattended, recording the utility,
# time and memory of each and reclaiming its disk before the next.
#
#   bash deploy1940/campaign.sh 2 5                # das_rep2 .. das_rep5
#   bash deploy1940/campaign.sh 2 5 --dry-run      # check everything, launch nothing
#
# Detached, so it outlives the SSH session:
#   mkdir -p ~/das1940/out/campaign
#   setsid nohup bash ~/das1940/repo/deploy1940/campaign.sh 2 5 \
#       > ~/das1940/out/campaign/console.log 2>&1 < /dev/null &
#
# Read ~/das1940/out/campaign/campaign.log. Not resumable by design: a lost
# campaign is relaunched with whatever indices are missing.
#
# CONFINEMENT
#   Every path this script writes, moves or deletes goes through guard(), which
#   resolves it and refuses unless it lies inside BASE (the das1940 tree) or
#   CENSUSDP. Both roots are validated at startup: each must be under $HOME and
#   must contain a marker proving it is the tree this script means. So no rm ever
#   runs on an unchecked path, and nothing outside those two trees is touched.
#
#   The single exception reaches no files: pkill on a run that overran MAX_HOURS.
#
# WHAT IT RECORDS PER RUN, in out/campaign/<name>.json
#   utility  a pointer to CensusDP's <name>_metrics.json (TVD by geolevel)
#   time     the DAS's own elapsed, this script's wall clock, and the per-stage
#            split from the DFXML (also as <name>_times.json)
#   memory   peak resident set size over the run
#
#   The memory figure is a SAMPLED maximum -- once per POLL seconds across the JVM
#   and its pyspark workers -- not an instrumented peak. The DAS cannot be measured
#   from inside the way our own runs are, so a spike between samples is invisible.
#   The record says so in a field, so it is never quoted as if it were exact.

set -uo pipefail          # deliberately not -e; failures are handled, not fatal

DEPLOY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$DEPLOY")"
BASE="$(dirname "$REPO_ROOT")"

# Under set -u an unset USER aborts, and a detached process does not always
# inherit one. id -un always works.
USER="${USER:-$(id -un)}"
CENSUSDP="${CENSUSDP_HOME:-$HOME/CensusDP}"

OUT="$BASE/out"
CAMP="$OUT/campaign"
LOG="$CAMP/campaign.log"
RELEASE="$OUT/person/LOCAL_1940"
NOISY="$OUT/person/noisy_measurements"
METRICS_DIR="$CENSUSDP/data/out/ipums_1940"

MIN_FREE_GB="${MIN_FREE_GB:-300}"
MAX_HOURS="${MAX_HOURS:-12}"
POLL="${POLL:-60}"
HEARTBEAT="${HEARTBEAT:-1800}"

FIRST="${1:-2}"
LAST="${2:-5}"
DRY=0
for a in "$@"; do [ "$a" = "--dry-run" ] && DRY=1; done

# ───────────────────────────────────────────────────────────── confinement
inside() {  # inside <root> <path>  -- after resolving symlinks and ..
  local root path
  root="$(readlink -m -- "$1")"; path="$(readlink -m -- "$2")"
  [ "$path" = "$root" ] && return 0
  case "$path/" in "$root"/*) return 0 ;; esac
  return 1
}

guard() {   # the single chokepoint: nothing is written or removed without this
  local p="$1"
  inside "$BASE" "$p" && return 0
  inside "$CENSUSDP" "$p" && return 0
  printf 'REFUSING to touch %s\n  it is outside %s and %s\n' "$p" "$BASE" "$CENSUSDP" >&2
  printf 'REFUSING to touch %s (outside %s and %s)\n' "$p" "$BASE" "$CENSUSDP" >>"$LOG" 2>/dev/null
  exit 1
}

safe_rm()   { local p; for p in "$@"; do guard "$p"; done; rm -rf -- "$@"; }
safe_rm_f() { local p; for p in "$@"; do guard "$p"; done; rm -f  -- "$@"; }
safe_mv()   { guard "$1"; guard "$2"; mv -- "$1" "$2"; }
safe_cp()   { guard "$2"; cp -- "$1" "$2"; }

validate_root() {  # <label> <path> <marker it must contain>
  if ! inside "$HOME" "$2"; then
    echo "$1 ($2) is not under \$HOME ($HOME). Refusing to run."; exit 1
  fi
  if [ ! -e "$2/$3" ]; then
    echo "$1 ($2) has no $3, so it is not the tree this script means."
    echo "Refusing to run rather than delete inside an unknown directory."; exit 1
  fi
}

validate_root "BASE"     "$BASE"     "env.sh"
validate_root "BASE"     "$BASE"     "repo/deploy1940"
validate_root "CENSUSDP" "$CENSUSDP" "benchmarks/metrics.py"

mkdir -p "$CAMP"

# run_alaska.sh sources this itself, but the campaign needs it too: it calls python
# directly, and a Debian host has no bare `python` without the venv on PATH. The
# scoring step activates CensusDP's venv in a subshell, overriding this only there.
[ -r "$BASE/env.sh" ] || { echo "missing $BASE/env.sh -- run setup_host.sh first"; exit 1; }
set -a; . "$BASE/env.sh" >/dev/null 2>&1; set +a

say() { printf '%s  %s\n' "$(date '+%F %T')" "$*" | tee -a "$LOG"; }

free_gb() { df -BG --output=avail "$BASE" 2>/dev/null | tail -1 | tr -dc '0-9'; }

running() { pgrep -u "$USER" -f '[r]un_1940\.py' >/dev/null 2>&1; }

# Resident set size in MB over the run's JVM and its Python workers. pgrep on
# run_1940.py finds only the SparkSubmit JVM; the pyspark workers carry their own
# command line, so both patterns are needed to see the real footprint.
rss_mb() {
  ps -u "$USER" -o rss=,args= 2>/dev/null \
    | grep -E 'run_1940\.py|pyspark' \
    | awk '{ s += $1 } END { printf "%d", s / 1024 }'
}

esc() { printf '%s' "${1:-}" | sed 's/\\/\\\\/g; s/"/\\"/g'; }

# ───────────────────────────────────────────────────────────── preflight
say "=== campaign das_rep$FIRST .. das_rep$LAST  (dry-run=$DRY)"
say "    BASE      $BASE"
say "    CENSUSDP  $CENSUSDP"
say "    confined to those two trees; every write and delete is checked"
for need in "$DEPLOY/run_alaska.sh" "$DEPLOY/stage_times.py" \
            "$CENSUSDP/benchmarks/ipums_1940/das_release.py" \
            "$CENSUSDP/venv/bin/activate" "$BASE/data/EXT1940USCB.dat"; do
  [ -e "$need" ] || { say "MISSING $need -- refusing to start"; exit 1; }
done
if running; then say "a DAS run is already in progress -- refusing to start"; exit 1; fi
say "    free disk $(free_gb)G, need ${MIN_FREE_GB}G per run"

consecutive_failures=0

for N in $(seq "$FIRST" "$LAST"); do
  NAME="das_rep$N"
  REC="$CAMP/$NAME.json"
  say ""
  say "───────────────────────────────────────────── $NAME"

  avail="$(free_gb)"
  if [ -z "$avail" ] || [ "$avail" -lt "$MIN_FREE_GB" ]; then
    say "only ${avail:-?}G free, under ${MIN_FREE_GB}G. Stopping so nothing fills the disk."
    break
  fi
  say "disk ${avail}G free"

  if [ "$DRY" = 1 ]; then
    say "[dry-run] would clear, launch, wait, score"
    continue
  fi

  guard "$NOISY"
  safe_rm "$RELEASE"
  rm -rf -- "$NOISY"/* 2>/dev/null
  say "cleared the previous MDF and noisy measurements"

  # ---- launch and wait, sampling memory as we go
  started=$(date +%s)
  peak_mb=0
  samples=0
  DAS_1940_DATAFILE=EXT1940USCB.dat \
  DAS_1940_READER_PARTITIONS="${DAS_1940_READER_PARTITIONS:-2000}" \
  DAS_DRIVER_MEMORY="${DAS_DRIVER_MEMORY:-64g}" \
  bash "$DEPLOY/run_alaska.sh" >>"$LOG" 2>&1
  launched=$?
  if [ "$launched" -ne 0 ]; then
    say "run_alaska.sh exited $launched; not waiting"
  else
    for _ in $(seq 1 30); do running && break; sleep 2; done
    deadline=$(( started + MAX_HOURS * 3600 ))
    next_beat=$(( started + HEARTBEAT ))
    while running; do
      now=$(date +%s)
      if [ "$now" -gt "$deadline" ]; then
        say "past ${MAX_HOURS}h; killing this run and moving on"
        pkill -u "$USER" -f '[r]un_1940\.py'
        sleep 20
        break
      fi
      mb="$(rss_mb)"
      if [ -n "$mb" ] && [ "$mb" -gt 0 ]; then
        samples=$(( samples + 1 ))
        [ "$mb" -gt "$peak_mb" ] && peak_mb="$mb"
      fi
      if [ "$now" -ge "$next_beat" ]; then
        phase="$(grep -oE "Taking noisy measurements at [A-Za-z]+|Geolevel [A-Za-z]+ has been optimized|Creating and running DAS (reader|engine|writer)" "$OUT/alaska.log" 2>/dev/null | tail -1)"
        say "  ...$(( (now - started) / 60 )) min: ${phase:-(no phase marker yet)}  peak rss $(( peak_mb / 1024 ))G  log $(du -h "$OUT/alaska.log" 2>/dev/null | cut -f1)"
        next_beat=$(( now + HEARTBEAT ))
      fi
      sleep "$POLL"
    done
  fi
  wall=$(( $(date +%s) - started ))
  say "process gone after $(( wall / 60 )) min; peak sampled rss $(( peak_mb / 1024 ))G over $samples samples"

  # ---- keep the evidence before the next launch truncates alaska.log
  safe_cp "$OUT/alaska.log" "$CAMP/${NAME}_alaska.log" 2>/dev/null
  newest_dfxml="$(ls -t "$REPO_ROOT/das_decennial/logs"/*.dfxml 2>/dev/null | head -1)"
  [ -n "$newest_dfxml" ] && safe_cp "$newest_dfxml" "$CAMP/${NAME}.dfxml"

  guard "$CAMP/${NAME}_times.json"
  python "$DEPLOY/stage_times.py" --json --records 132404766 --units 158374 \
      --parents 6365 > "$CAMP/${NAME}_times.json" 2>&1
  guard "$CAMP/${NAME}_times.txt"
  python "$DEPLOY/stage_times.py" --records 132404766 --units 158374 \
      --parents 6365 > "$CAMP/${NAME}_times.txt" 2>&1

  META="$RELEASE/person/0_metadata"
  das_seconds="$(grep -oE "Run completed in [0-9.,]+ seconds" "$CAMP/${NAME}_alaska.log" 2>/dev/null | tail -1 | tr -dc '0-9.')"
  run_uuid="$(sed -n 's/^# DAS RUNID: //p' "$META" 2>/dev/null | head -1)"
  commit="$(sed -n 's/^# Git Repo Info://p' "$META" 2>/dev/null | head -1)"
  records="$(sed -n 's/^# Records: //p' "$META" 2>/dev/null | tr -dc '0-9')"

  status=ok
  grep -q "Run completed" "$CAMP/${NAME}_alaska.log" 2>/dev/null || status=run_failed

  # ---- score it, in CensusDP's venv -- a different one from this environment's
  if [ "$status" = ok ]; then
    say "run completed; scoring"
    (
      set +u
      . "$CENSUSDP/venv/bin/activate"
      cd "$CENSUSDP" || exit 1
      python -m benchmarks.ipums_1940.das_release "$RELEASE/person" "$NAME" \
        && python -m benchmarks.metrics ipums_1940 "$NAME" --discard
    ) >>"$LOG" 2>&1
    [ $? -ne 0 ] && status=scoring_failed
  fi

  # ---- the run record: utility pointer, time, memory
  guard "$REC"
  {
    printf '{\n'
    printf ' "name": "%s",\n'                 "$(esc "$NAME")"
    printf ' "system": "das_2020_dhc",\n'
    printf ' "status": "%s",\n'               "$status"
    printf ' "records": %s,\n'                "${records:-null}"
    printf ' "run_uuid": "%s",\n'             "$(esc "$run_uuid")"
    printf ' "git_commit": "%s",\n'           "$(esc "$commit")"
    printf ' "wall_seconds_campaign": %s,\n'  "$wall"
    printf ' "das_reported_seconds": %s,\n'   "${das_seconds:-null}"
    printf ' "peak_rss_gb_sampled": %s,\n'    "$(awk -v m="$peak_mb" 'BEGIN{printf "%.2f", m/1024}')"
    printf ' "rss_samples": %s,\n'            "$samples"
    printf ' "rss_note": "maximum over samples taken every %ss across the JVM and its pyspark workers; not an instrumented peak",\n' "$POLL"
    printf ' "driver_memory": "%s",\n'        "${DAS_DRIVER_MEMORY:-64g}"
    printf ' "reader_partitions": %s,\n'      "${DAS_1940_READER_PARTITIONS:-2000}"
    printf ' "stage_times_json": "%s",\n'     "$(esc "$CAMP/${NAME}_times.json")"
    printf ' "metrics_json": "%s"\n'          "$(esc "$METRICS_DIR/${NAME}_metrics.json")"
    printf '}\n'
  } > "$REC"
  say "record -> $REC"

  # ---- reclaim, or set aside
  case "$status" in
    ok)
      say "scored -> $METRICS_DIR/${NAME}_metrics.json"
      safe_rm_f "$METRICS_DIR/${NAME}_evaluation.parquet"
      safe_rm "$RELEASE"
      say "MDF deleted; metrics, times and record kept"
      consecutive_failures=0
      ;;
    scoring_failed)
      say "SCORING FAILED. Keeping the MDF as ${NAME}_unscored to score by hand; continuing."
      safe_mv "$RELEASE" "$OUT/person/${NAME}_unscored"
      consecutive_failures=0
      ;;
    run_failed)
      consecutive_failures=$(( consecutive_failures + 1 ))
      say "NO 'Run completed' in the log -- run failed ($consecutive_failures in a row)"
      grep -oE "^[A-Za-z_.]+(Error|Exception):.*" "$CAMP/${NAME}_alaska.log" 2>/dev/null \
          | sort -u | head -3 | sed 's/^/    /' | tee -a "$LOG"
      [ -d "$RELEASE" ] && safe_mv "$RELEASE" "$OUT/person/${NAME}_incomplete"
      if [ "$consecutive_failures" -ge 2 ]; then
        say "two failures in a row; stopping the campaign."
        break
      fi
      ;;
  esac
done

say ""
say "=== campaign done"
say "one record per run, with time and memory:"
ls -la "$CAMP"/das_rep*.json 2>/dev/null | tee -a "$LOG"
say "utility:"
ls -la "$METRICS_DIR"/das_rep*_metrics.json 2>/dev/null | tee -a "$LOG"
say "disk $(free_gb)G free"
