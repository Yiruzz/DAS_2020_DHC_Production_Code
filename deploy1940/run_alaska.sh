#!/bin/bash
# Launch the Alaska DAS run detached, so it survives an SSH disconnect.
# Log stays on the server at ~/das1940/out/alaska.log
set -euo pipefail

# Paths are derived from this script location, not assumed:
#   DEPLOY     <clone>/deploy1940   this directory
#   REPO_ROOT  <clone>              the working tree
#   BASE       <clone>/..           venv, JDK, data, out -- untracked
DEPLOY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$DEPLOY")"
BASE="$(dirname "$REPO_ROOT")"

LOG="$BASE/out/alaska.log"

[ -r "$BASE/env.sh" ] || { echo "missing $BASE/env.sh -- run setup first"; exit 1; }
# shellcheck disable=SC1090
set -a; . "$BASE/env.sh"; set +a

[ -r "$DAS_1940_INPUT/EXT1940USCB_AK.dat" ] || { echo "missing input data"; exit 1; }

if pgrep -u "$USER" -f "run_1940.py" >/dev/null 2>&1; then
  echo "a DAS run is already in progress:"
  pgrep -u "$USER" -af "run_1940.py" | head -3
  exit 1
fi

mkdir -p "$BASE/out"
: > "$LOG"

# The dashboard sink. Without a 200 on DAS_LOG_ENDPOINT, dashboard.send_obj
# falls through to SQS_Client() -> boto3.resource('sqs', ...), which raises
# NoRegionError inside the Spark task; optimizer.py:797 reaches it on every
# solve with model.NodeCount > 1. See the note in setup_host.sh.
SINK_PORT="${DAS_DASHBOARD_SINK_PORT:-8940}"
SINK_LOG="${DAS_DASHBOARD_SINK_LOG:-$BASE/out/dashboard.jsonl}"
SINK_ERR="$BASE/out/dashboard_sink.err"

# Deliberately one line. A backslash continuation here was once emitted as
# the two characters \+n instead of a line break; bash read that as an
# escaped n and handed the sink a stray argument, argparse rejected it, and
# nothing ever bound to the port.
if pgrep -u "$USER" -f "dashboard_sink.py --port $SINK_PORT" >/dev/null 2>&1; then
  echo "dashboard sink already listening on $SINK_PORT"
else
  : > "$SINK_ERR"
  setsid nohup python "$DEPLOY/bin/dashboard_sink.py" --port "$SINK_PORT" --log "$SINK_LOG" >> "$SINK_ERR" 2>&1 < /dev/null &
fi

# Poll instead of sleeping a fixed second: on a loaded host the interpreter
# can take longer to bind, and a fixed sleep turns a slow start into a
# spurious abort.
sink_up() {
  python - "$SINK_PORT" <<'PYCHK'
import socket, sys
s = socket.socket(); s.settimeout(2)
sys.exit(s.connect_ex(("127.0.0.1", int(sys.argv[1]))))
PYCHK
}
SINK_OK=0
for _ in $(seq 1 20); do
  if sink_up; then SINK_OK=1; break; fi
  sleep 0.5
done

if [ "$SINK_OK" = 1 ]; then
  echo "dashboard sink: listening on 127.0.0.1:$SINK_PORT -> $SINK_LOG"
else
  echo "dashboard sink FAILED to start after 10s. Aborting -- the optimisation"
  echo "stage would die on NoRegionError (HANDOFF 5.9)."
  echo
  echo "--- $SINK_ERR ---"
  cat "$SINK_ERR" 2>/dev/null || echo "  (empty or missing)"
  echo "--- anything already on port $SINK_PORT? ---"
  { ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null; } | grep ":$SINK_PORT " || echo "  nothing listening"
  echo "--- interpreter ---"
  command -v python || echo "  python NOT on PATH"
  python -c "import http.server, json, argparse; print('  stdlib imports OK')" 2>&1 | tail -3
  echo "--- script ---"
  ls -la "$DEPLOY/bin/dashboard_sink.py" 2>&1
  python -c "import ast,sys; ast.parse(open(sys.argv[1]).read()); print('  parses OK')" "$DEPLOY/bin/dashboard_sink.py" 2>&1 | tail -3
  exit 1
fi

cd "$DAS_REPO"

# Sizing. Defaults suit a 24-core host and leave a few cores for the OS and
# other users; override per host without editing this file, e.g.
#   DAS_SPARK_WORKERS=8 DAS_DRIVER_MEMORY=8g bash deploy1940/run_alaska.sh
WORKERS="${DAS_SPARK_WORKERS:-$(n=$(nproc 2>/dev/null || echo 4); [ "$n" -gt 4 ] && echo $((n - 4)) || echo "$n")}"
DRIVER_MEM="${DAS_DRIVER_MEMORY:-16g}"

# Stamped into the MDF metadata header. Both go through --set (driver.py:1066),
# which is applied BEFORE config_apply_environment (:1165) -- and that function
# reads config[ENVIRONMENT][var] raw, without do_expandvars, so a $VAR written
# in the config is exported to the environment LITERALLY and clobbers the real
# value. That is why the first complete run recorded
#     # DAS RUNID: $DAS_RUN_UUID
# instead of the id. Passing a resolved literal here sidesteps it. Note that
# set_parameter (driver.py:1008) splits on ':' and requires exactly two, so
# neither value may contain a colon.
GIT_COMMIT="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
echo "run uuid $DAS_RUN_UUID   commit $GIT_COMMIT"
echo "spark: local[$WORKERS], driver memory $DRIVER_MEM"

setsid nohup spark-submit \
  --driver-memory "$DRIVER_MEM" \
  --master "local[$WORKERS]" \
  --conf spark.driver.maxResultSize=0 \
  --conf spark.ui.showConsoleProgress=false \
  run_1940.py \
  configs/Census1940/DDP2010_Update/ipums_1940_local.ini \
  --loglevel INFO \
  --set "environment:DAS_RUN_UUID:$DAS_RUN_UUID" \
  --set "reader:git_commit:$GIT_COMMIT" \
  >> "$LOG" 2>&1 < /dev/null &

PID=$!
sleep 3
echo "launched pid $PID"
echo "log: $LOG"
if kill -0 "$PID" 2>/dev/null; then
  echo "still alive after 3s -- good"
else
  echo "DIED IMMEDIATELY."
  echo "--- errors ---"
  grep -nEi "Traceback|^[A-Za-z_.]*(Error|Exception):|ERROR:|DASConfigError|GurobiError|licen[cs]e" "$LOG"     | grep -viE "census env file" | cut -c1-200 | tail -15 | sed 's/^/  /'
  echo "--- last 30 lines ---"
  tail -30 "$LOG" | cut -c1-200 | sed 's/^/  /'
fi
