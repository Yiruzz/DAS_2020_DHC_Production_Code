# deploy1940 — running the 1940 path on a plain Linux host

Everything needed to execute the 1940 code path of this release outside the
Census Bureau's EMR environment. Nothing here touches `das_decennial/`; the
deviations from the published code are the added files listed in
`../RUNNING_1940.md`, and this directory is the scaffolding around them.

## Bootstrap (fresh host)

```bash
mkdir -p ~/das1940
git clone -b make-1940-path-runnable \
    https://github.com/Yiruzz/DAS_2020_DHC_Production_Code.git ~/das1940/repo
bash ~/das1940/repo/deploy1940/setup_host.sh
```

Then put the data in place — it belongs to IPUMS and is not in any repository:

```
~/das1940/data/EXT1940USCB_AK.dat
```

## Every run after that

```bash
ssh <host> 'cd ~/das1940/repo && git pull && bash deploy1940/update_and_run.sh'
```

`update_and_run.sh` pulls, reruns `setup_host.sh` (idempotent — downloads are
skipped), runs four pre-flight checks and launches. Monitor with:

```bash
ssh <host> 'bash ~/das1940/repo/deploy1940/is_running.sh'   # fast: alive? growing?
ssh <host> 'bash ~/das1940/repo/deploy1940/check_run.sh'    # full: stage, errors, output
```

## Layout

Scripts derive all three paths from their own location, so the clone can live
anywhere:

| | | tracked |
|---|---|---|
| `DEPLOY` | `<clone>/deploy1940` | yes — this directory |
| `REPO_ROOT` | `<clone>` | yes — the release plus our additions |
| `BASE` | `<clone>/..` | **no** — `venv/`, `jdk17/`, `python311/`, `data/`, `out/`, `env.sh` |

`BASE` holds everything heavy and machine-specific. Removing a host's
installation is `rm -rf ~/das1940`.

## What is in here

| | |
|---|---|
| `setup_host.sh` | builds `BASE`: portable JDK 17, standalone CPython 3.11, venv with NumPy pinned <2, `env.sh` |
| `update_and_run.sh` | pull → setup → pre-flight → launch |
| `run_alaska.sh` | launches the run detached, after starting and verifying the dashboard sink |
| `check_run.sh` / `is_running.sh` | monitoring |
| `preflight_config.py` | resolves the config and fails on any `$VAR` that will not expand — deliberately stricter than the release's own validator, which cannot see the `${VAR}` form at all |
| `shim/dprng.py` | stand-in for the noise library **absent from the public release**, imported unconditionally at `programs/engine/primitives.py:5`. Reached only by the discrete Gaussian mechanism; the 1940 path is pure DP / geometric. Its `discrete_gaussian()` raises on purpose. |
| `bin/hadoop` | `das_utils.clearPath` (`das_utils.py:299`) has no local-filesystem branch and shells out to `hadoop fs -rm -r` |
| `bin/aws` | `engine_utils.py:314` issues a local-to-local copy through `aws s3 cp` |
| `bin/dashboard_sink.py` | must answer **200** on `DAS_LOG_ENDPOINT` — see below |

### The shims are files, not heredocs, and that is deliberate

They used to be written by `setup_host.sh` as shell heredocs. A line continuation
inside one of them was once emitted as the two characters `\` + `n` instead of a
line break; bash read that as an escaped `n`, handed `dashboard_sink.py` a stray
argument, argparse rejected it, nothing bound to the port and the run aborted.
Real files are diffable, syntax-checkable, and cannot acquire that defect.

### Why the dashboard sink is not optional

`dashboard.send_obj` (`programs/dashboard.py:346`) tries REST first and, when
that fails, falls through **unconditionally** to
`SQS_Client().queue_message(...)` (`:398`) → `boto3.resource('sqs', ...)`
(`:194`), which raises `NoRegionError` on a host with no AWS region. No config
option skips the fallback, and `DAS_LOG_ENDPOINT=none` makes it worse — the REST
block is skipped and it goes straight to SQS.

The call site is `optimizer.py:797`, reached whenever `report_reason` is
non-empty, which `optimizer.py:760` sets as soon as `model.NodeCount > 1`.
That block is in the function the release annotates as *"the ONLY PLACE in the
entire DAS where optimize() is called"*, so every solve passes through it.

Answering 200 makes `send_url` return `True` (`:330`) and `send_obj` returns at
`:388`, so boto3 is never reached — no client, no thread, no `atexit` handler.
`run_alaska.sh` starts the sink, polls the port, and **refuses to launch**
without it. Everything the DAS would have sent to the Census dashboard lands in
`~/das1940/out/dashboard.jsonl` instead of being discarded.

## Full write-up

The findings this configuration works around — including two components the
release cannot run as published (`dprng`, and `Env.OtherEnv` which gurobipy 13
removed) — are in `../RUNNING_1940.md`.
