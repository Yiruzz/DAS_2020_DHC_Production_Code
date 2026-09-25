#!/bin/bash
# Pull, reconfigure, verify, relaunch. No bundle and no scp: this script is
# itself part of the working tree it updates.
set -euo pipefail

# Paths are derived from this script location, not assumed:
#   DEPLOY     <clone>/deploy1940   this directory
#   REPO_ROOT  <clone>              the working tree
#   BASE       <clone>/..           venv, JDK, data, out -- untracked
DEPLOY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$DEPLOY")"
BASE="$(dirname "$REPO_ROOT")"

echo "== 1. pull"
# --ff-only: a fast-forward or nothing. A merge commit created here would be a
# local-only commit on a host nobody pushes from, i.e. silent divergence.
git -C "$REPO_ROOT" pull --ff-only -q
git -C "$REPO_ROOT" log --oneline -1 | sed 's/^/   HEAD /'
echo -n "   working tree: "
if [ -z "$(git -C "$REPO_ROOT" status --porcelain)" ]; then echo clean; else echo DIRTY; fi

echo "== 2. regenerate env.sh (setup is idempotent; downloads are skipped)"
bash "$DEPLOY/setup_host.sh" 2>&1 | grep -E "^==|already present|numpy |wrote|passed|MISSING" | sed 's/^/   /'

set -a; . "$BASE/env.sh"; set +a
cd "$DAS_REPO"

echo "== 3. pre-flight A: config \$variables"
python "$DEPLOY/preflight_config.py" || exit 1

echo "== 4. pre-flight B: env vars the code reads via census_getenv() with no fallback"
missing=""
for v in DAS_S3INPUTS DAS_S3ROOT DAS_S3LOGS DAS_S3MGMT DAS_S3MGMT_ACL DAS_PYTHONDIR JBID MISSION_NAME CLUSTERID MASTER_IP DAS_DASHBOARD_URL DAS_SQS_URL DAS_SQS_ENDPOINT DAS_LOG_ENDPOINT DAS_LOG_ENDPOINT_DEBUG LD_LIBRARY_PATH DAS_RUN_UUID TMP TMPDIR AWS_DEFAULT_REGION DAS_DASHBOARD_SINK_PORT; do
  if [ -z "${!v+x}" ]; then missing="$missing $v"; fi
done
# BCC_HTTPS_PROXY is deliberately empty: test for presence, not value
if [ -z "${BCC_HTTPS_PROXY+x}" ]; then missing="$missing BCC_HTTPS_PROXY"; fi
if [ -n "$missing" ]; then echo "   NOT SET:$missing"; exit 1; fi
echo "   all present"
echo "   DAS_RUN_UUID=$DAS_RUN_UUID"
echo "   TMPDIR=$TMPDIR"
echo "   DAS_PYTHONDIR=$DAS_PYTHONDIR (ctools/ subdir? $([ -d "$DAS_PYTHONDIR/ctools" ] && echo yes || echo 'no, skipped'))"

echo "== 5. pre-flight C: external CLIs das_utils shells out to"
h=$(command -v hadoop || true)
a=$(command -v aws || true)
[ -n "$h" ] || { echo "   hadoop NOT on PATH (das_utils.py:299)"; exit 1; }
[ -n "$a" ] || { echo "   aws NOT on PATH (engine_utils.py:314 uses check_call)"; exit 1; }
echo "   hadoop -> $h"
echo "   aws    -> $a"
printf 'selftest\n' > "$TMPDIR/__selftest_src"
aws s3 cp --quiet "$TMPDIR/__selftest_src" "$DAS_1940_OUTPUT/__selftest_dst" 2>&1 | sed 's/^/   /'
if [ -f "$DAS_1940_OUTPUT/__selftest_dst" ]; then echo "   aws shim copy: OK"; else echo "   aws shim copy: FAILED"; exit 1; fi
hadoop fs -rm -r "$DAS_1940_OUTPUT/__selftest_dst" 2>&1 | sed 's/^/   /'
if [ ! -e "$DAS_1940_OUTPUT/__selftest_dst" ]; then echo "   hadoop shim rm: OK"; else echo "   hadoop shim rm: FAILED"; exit 1; fi
rm -f "$TMPDIR/__selftest_src"

echo "== 5b. pre-flight D: the dashboard sink shim"
[ -r "$DEPLOY/bin/dashboard_sink.py" ] || { echo "   MISSING $DEPLOY/bin/dashboard_sink.py"; exit 1; }
python -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$DEPLOY/bin/dashboard_sink.py"   || { echo "   dashboard_sink.py does not parse"; exit 1; }
echo "   present, port $DAS_DASHBOARD_SINK_PORT, log $DAS_DASHBOARD_SINK_LOG"

echo "== 6. launching"
[ -r "$DEPLOY/run_alaska.sh" ] || { echo "   MISSING $DEPLOY/run_alaska.sh"; exit 1; }
exec bash "$DEPLOY/run_alaska.sh"
