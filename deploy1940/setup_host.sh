#!/bin/bash
# Set up the environment for the DAS 1940 path on an ordinary Linux host.
#
# SELF-CONTAINED: installs nothing system-wide, uses no sudo, and writes only
# under the parent of this clone. Remove everything with:  rm -rf ~/das1940
#
# It brings its own interpreter and JDK rather than trusting the ones present,
# because the pins are not preferences:
#   - NumPy must be <2   the code calls np.issubsctype, removed in 2.0, at
#                        nodes.py:275 and constraints_dpqueries.py:131
#   - hence CPython 3.11 no NumPy 1.x wheel exists for 3.13+, and current
#                        distributions ship only 3.13
#   - hence JDK 17       Spark 3.5 supports 8/11/17, not 21
#   - Spark itself       comes from pip, not from the host
#
# Spark scratch goes under $HOME rather than /tmp, which is usually the smaller
# of the two and is shared.
#
# Bootstrap on a fresh host -- one command, no scp and no bundle:
#
#   mkdir -p ~/das1940 && git clone -b make-1940-path-runnable https://github.com/Yiruzz/DAS_2020_DHC_Production_Code.git ~/das1940/repo && bash ~/das1940/repo/deploy1940/setup_host.sh
#
# Thereafter:  cd ~/das1940/repo && git pull && bash deploy1940/update_and_run.sh
#
# The only thing still placed by hand is the data, which belongs to IPUMS:
#   ~/das1940/data/EXT1940USCB_AK.dat

set -euo pipefail

# This script lives inside the repository it configures, so every path is
# derived from its own location rather than assumed:
#   DEPLOY     <clone>/deploy1940   this directory
#   REPO_ROOT  <clone>              the working tree
#   BASE       <clone>/..           venv, JDK, data, out -- untracked
DEPLOY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$DEPLOY")"
BASE="$(dirname "$REPO_ROOT")"

PYVER="3.11.16"; PYTAG="20260901"
PYURL="https://github.com/astral-sh/python-build-standalone/releases/download/${PYTAG}/cpython-${PYVER}+${PYTAG}-x86_64-unknown-linux-gnu-install_only.tar.gz"
JDKURL="https://api.adoptium.net/v3/binary/latest/17/ga/linux/x64/jdk/hotspot/normal/eclipse"

mkdir -p "$BASE"
cd "$BASE"

echo "== 1. Portable JDK 17  (no sudo; the system java 21 is left alone)"
if [ -x "$BASE/jdk17/bin/java" ]; then
  echo "   already present"
else
  curl -fsSL -o jdk17.tar.gz "$JDKURL"
  mkdir -p jdk17
  tar xzf jdk17.tar.gz -C jdk17 --strip-components=1
  rm -f jdk17.tar.gz
fi
"$BASE/jdk17/bin/java" -version 2>&1 | head -1 | sed 's/^/   /'

echo "== 2. Standalone CPython ${PYVER}"
if [ -x "$BASE/python311/bin/python3" ]; then
  echo "   already present"
else
  curl -fsSL -o py311.tar.gz "$PYURL"
  mkdir -p python311
  tar xzf py311.tar.gz -C python311 --strip-components=1
  rm -f py311.tar.gz
fi
"$BASE/python311/bin/python3" --version | sed 's/^/   /'

echo "== 3. Virtual environment and pinned dependencies"
[ -x "$BASE/venv/bin/python" ] || "$BASE/python311/bin/python3" -m venv "$BASE/venv"
"$BASE/venv/bin/python" -m pip install -q --upgrade pip
# numpy<2 is required: np.issubsctype was removed in NumPy 2.0 and is called at
# programs/nodes/nodes.py:275 and programs/queries/constraints_dpqueries.py:131.
# pyspark 3.5 because the DAS targets Spark 2.4/3.x.
"$BASE/venv/bin/python" -m pip install -q \
    "numpy<2" scipy pandas psutil mpmath boto3 matplotlib randomgen pytest \
    "pyspark==3.5.*" gurobipy
"$BASE/venv/bin/python" - <<'PY' | sed 's/^/   /'
import numpy, scipy, pyspark, gurobipy
print(f"numpy {numpy.__version__}  scipy {scipy.__version__}  "
      f"pyspark {pyspark.__version__}  gurobipy {gurobipy.__version__}")
PY

echo "== 4. Shims and stubs (versioned in this repo, used in place)"
#
# These used to be written here as shell heredocs. They are ordinary files now:
#   shim/dprng.py          noise library absent from the public DAS release,
#                          imported unconditionally at engine/primitives.py:5
#   bin/hadoop             das_utils.clearPath (das_utils.py:299) shells out to it
#   bin/aws                engine_utils.py:314 does a local-to-local copy through it
#   bin/dashboard_sink.py  must answer 200 on DAS_LOG_ENDPOINT or the optimisation
#                          stage dies on NoRegionError -- see the note further down
#
# Keeping them as files rather than heredocs is not cosmetic. A line continuation
# inside one of those heredocs was once emitted as the two characters \+n rather
# than a line break; bash read it as an escaped n, the sink got a stray argument,
# argparse rejected it, nothing bound to the port and the run aborted. Real files
# are diffable, syntax-checkable, and cannot acquire that class of defect.
for f in "$DEPLOY/bin/hadoop" "$DEPLOY/bin/aws" "$DEPLOY/bin/dashboard_sink.py" "$DEPLOY/shim/dprng.py"; do
  [ -r "$f" ] || { echo "   MISSING $f -- is the working tree complete?"; exit 1; }
done
chmod +x "$DEPLOY/bin/hadoop" "$DEPLOY/bin/aws" "$DEPLOY/bin/dashboard_sink.py"
sh -n "$DEPLOY/bin/hadoop" || { echo "   bin/hadoop does not parse"; exit 1; }
sh -n "$DEPLOY/bin/aws"    || { echo "   bin/aws does not parse"; exit 1; }
python -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$DEPLOY/bin/dashboard_sink.py" || { echo "   bin/dashboard_sink.py does not parse"; exit 1; }
echo "   hadoop, aws, dashboard_sink.py, dprng.py -- present and parse"

echo "== 5. Repository"
# Nothing to clone: this script lives inside the working tree it configures.
git -C "$REPO_ROOT" log --oneline -1 | sed 's/^/   HEAD /'
if [ -n "$(git -C "$REPO_ROOT" status --porcelain)" ]; then
  echo "   working tree: DIRTY"
else
  echo "   working tree: clean"
fi

echo "== 6. Directories and env file"
mkdir -p "$BASE/data" "$BASE/out" "$BASE/spark-tmp"
cat > "$BASE/env.sh" <<EOF
# source this before running. Everything it points at lives under $BASE.
source $BASE/venv/bin/activate

export JAVA_HOME=$BASE/jdk17
# $DEPLOY/bin first: it holds the hadoop CLI stand-in that das_utils.clearPath
# (das_utils.py:299) shells out to. PySpark ships Hadoop jars but not the CLI.
export PATH=$DEPLOY/bin:\$JAVA_HOME/bin:\$PATH

export DAS_REPO=$REPO_ROOT/das_decennial
export PYTHONPATH=$DEPLOY/shim:\$DAS_REPO:\$DAS_REPO/das_framework
export SPARK_HOME=\$(python -c "import pyspark,os;print(os.path.dirname(pyspark.__file__))")
export PATH=\$SPARK_HOME/bin:\$PATH

export DAS_1940_INPUT=$BASE/data
export DAS_1940_OUTPUT=$BASE/out

# /tmp is typically small and shared; keep Spark scratch under \$HOME.
export SPARK_LOCAL_DIRS=$BASE/spark-tmp

# s3cat.get_tmp() (s3cat.py:155) returns the hardcoded constant TMP_DIR
# ('/usr/tmp', s3cat.py:65) unless TMP, TEMP or TMP_DIR is set in the
# environment, in which case it returns None and tempfile picks its own
# directory. /usr/tmp exists on Amazon Linux but not on Debian, so without this
# every tempfile.NamedTemporaryFile(dir=get_tmp()) raises FileNotFoundError --
# engine_utils.py:282,307 among them. Setting TMP makes get_tmp() return None;
# TMPDIR is what Python's tempfile actually honours.
export TMP=$BASE/tmp
export TMPDIR=$BASE/tmp
mkdir -p \$TMP

export GRB_LICENSE_FILE=\${GRB_LICENSE_FILE:-\$HOME/gurobi.lic}

# ---------------------------------------------------------------------------
# EMR-era environment variables.
#
# ctools/env.py:105 census_getenv() falls through to os.environ[name] when no
# fallback is given, so every one of these raises KeyError on a host that is not
# an EMR node. They cannot be overridden from the config file because the calls
# are hardcoded in Python. Each is set to something local and obviously invalid,
# so that if any code path genuinely reads one, the failure is legible instead
# of looking like a misconfigured AWS account.
#
# Where each is read:
#   DAS_S3INPUTS       das_setup.py:122 (grfc default; also overridden in config)
#   DAS_PYTHONDIR      das_utils.py:626, reached from das_setup.py:212
#                      ship_files2spark globs \$DAS_PYTHONDIR/ctools; pointing it
#                      at a directory without a ctools/ subdir skips that step,
#                      which is what we want in local mode -- ctools already
#                      reaches the workers via PYTHONPATH
#   MASTER_IP          das_utils.py:133, syslog target; UDP to a closed port
#   DAS_DASHBOARD_URL  dashboard.py:159
#   DAS_SQS_URL        dashboard.py:142, :188
#   DAS_SQS_ENDPOINT   dashboard.py:189
#   BCC_HTTPS_PROXY    dashboard.py:145, :190
#   DAS_S3MGMT_ACL     dashboard.py:156
#   DAS_LOG_ENDPOINT*  dashboard.py:367, :369
# dashboard is imported by optimizer.py:33 and topdown_engine.py:26 and called
# at optimizer.py:174,283,307,797 and topdown_engine.py:285.
#
# An unreachable endpoint does NOT degrade quietly, which is what this block
# used to assume. send_url's failures are caught (dashboard.py:331-344) and it
# returns None -- and that is exactly what routes send_obj (:388) into its
# fallback, which is not caught:
#
#     r = send_url(surl)
#     if r: return
#     SQS_Client().queue_message(...)        # dashboard.py:398
#
# SQS_Client.__init__ -> sqs_queue() (:187) -> boto3.resource('sqs', ...) (:194),
# which raises botocore.exceptions.NoRegionError on a host with no AWS region.
# No config option skips the fallback. The call site that matters is
# optimizer.py:797, reached whenever report_reason is non-empty, which
# optimizer.py:760 sets as soon as model.NodeCount > 1 -- routine for the
# rounder MIP. So the optimisation stage dies at the first branching model.
#
# Answering 200 instead makes send_url return True (:330) and send_obj returns
# before boto3 is ever imported into the path. bin/dashboard_sink.py does that
# and records every message, so the dashboard traffic becomes evidence.
# AWS_DEFAULT_REGION is belt and braces for any path that still builds the
# client; the endpoint is a refused loopback port, so nothing leaves the host.
# ---------------------------------------------------------------------------
export DAS_UNUSED=$BASE/unused-aws-paths
mkdir -p \$DAS_UNUSED

export DAS_S3INPUTS=\$DAS_UNUSED/s3inputs
export DAS_S3ROOT=\$DAS_UNUSED/s3root
export DAS_S3LOGS=\$DAS_UNUSED/s3logs
export DAS_S3MGMT=\$DAS_UNUSED/s3mgmt
export DAS_S3MGMT_ACL=none
export DAS_PYTHONDIR=$BASE

export JBID=\$USER
export MISSION_NAME=LOCAL_1940
export CLUSTERID=local
export APPLICATIONID=local

# Stamped into the output header by writer.py:171 and read from the config by
# optimizer.py:216,419. A UTC timestamp so each launch is distinguishable.
export DAS_RUN_UUID=\${DAS_RUN_UUID:-1940-\$(date -u +%Y%m%dT%H%M%SZ)}

export MASTER_IP=127.0.0.1
export DAS_DASHBOARD_URL=http://127.0.0.1:9
export DAS_SQS_URL=http://127.0.0.1:9
export DAS_SQS_ENDPOINT=http://127.0.0.1:9

# Port run_alaska.sh starts bin/dashboard_sink.py on (see the note above).
# Defined before the endpoints, which interpolate it.
export DAS_DASHBOARD_SINK_PORT=\${DAS_DASHBOARD_SINK_PORT:-8940}
export DAS_DASHBOARD_SINK_LOG=$BASE/out/dashboard.jsonl

export DAS_LOG_ENDPOINT=http://127.0.0.1:\$DAS_DASHBOARD_SINK_PORT
export DAS_LOG_ENDPOINT_DEBUG=http://127.0.0.1:\$DAS_DASHBOARD_SINK_PORT
export BCC_HTTPS_PROXY=

# boto3 needs a region even to construct a client against an explicit
# endpoint_url. Never used: the endpoint is a refused loopback port.
export AWS_DEFAULT_REGION=\${AWS_DEFAULT_REGION:-us-east-1}

export LD_LIBRARY_PATH=\${LD_LIBRARY_PATH:-/usr/lib}  # optimizer.py:109 needs it set
EOF
echo "   wrote $BASE/env.sh"

echo "== 7. Smoke test: the 1940 unit tests (no Spark, no Gurobi)"
set +e
( set -a; . "$BASE/env.sh"; set +a
  cd "$DAS_REPO" 2>/dev/null || exit 0
  python -m pytest programs/invariants/tests/test_1940_invariants_creator.py -q 2>&1 | tail -2 | sed 's/^/   /'
)
set -e

echo
echo "Ready. Everything is under $BASE -- remove it all with: rm -rf $BASE"
echo "Next:  source $BASE/env.sh"
