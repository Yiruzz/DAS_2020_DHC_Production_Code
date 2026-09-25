#!/bin/bash
# Fast answer to "is it running, and is it making progress?"
# Paths are derived from this script location, not assumed:
#   DEPLOY     <clone>/deploy1940   this directory
#   REPO_ROOT  <clone>              the working tree
#   BASE       <clone>/..           venv, JDK, data, out -- untracked
DEPLOY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$DEPLOY")"
BASE="$(dirname "$REPO_ROOT")"
LOG="$BASE/out/alaska.log"

PIDS=$(pgrep -u "$USER" -f run_1940.py | tr '\n' ' ')
if [ -z "$PIDS" ]; then
  echo "PROCESS : not running"
else
  echo "PROCESS : alive  (pids: $PIDS)"
  # %cpu of the JVM doing the work, not just the python wrapper
  ps -o pid=,pcpu=,etime=,rss=,comm= -p $(pgrep -u "$USER" -f 'run_1940.py|SparkSubmit' | tr '\n' ',' | sed 's/,$//') 2>/dev/null \
    | awk '{printf "          pid %-8s cpu %-6s elapsed %-10s rss %.1fGB  %s\n", $1, $2"%", $3, $4/1048576, $5}'
fi

[ -r "$LOG" ] || { echo "LOG     : missing"; exit 0; }

A=$(stat -c %s "$LOG"); sleep 4; B=$(stat -c %s "$LOG")
printf 'LOG     : %s   last write %s\n' "$(du -h "$LOG" | cut -f1)" "$(date -r "$LOG" '+%H:%M:%S')"
if [ "$B" -gt "$A" ]; then
  printf 'PROGRESS: growing (+%d bytes in 4s) -- working\n' "$((B-A))"
else
  printf 'PROGRESS: no growth in 4s -- either between stages, or stuck\n'
fi
printf 'LOAD    : %s\n' "$(cut -d' ' -f1-3 /proc/loadavg)"

echo
echo "LAST MEANINGFUL LINES:"
grep -vE "INFO:root:config\[|^INFO: |does not exist and no default" "$LOG" \
  | tail -8 | cut -c1-160 | sed 's/^/  /'
