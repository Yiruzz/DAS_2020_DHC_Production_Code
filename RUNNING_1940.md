# Running the DAS on the 1940 IPUMS data

This document explains how to run the Census Bureau's 2020 DHC Disclosure
Avoidance System over the 1940 full-count census, on your own hardware.

It is a new file: the release's own `README.md` is left untouched, as is every
configuration file that shipped with it.

---

## 1. Why this document exists

The published release runs on the Census Edited File, which is Title 13
confidential and unavailable outside the Bureau. The only input an outside
researcher can actually obtain is the 1940 IPUMS extract, and the release ships
a complete 1940 code path for it: schema, attributes, constraints, invariants,
reader, and writer.

That path does not run as published. The 1940 configuration files predate the
budget/strategy refactor in this release, so the engine rejects them before any
data is read. `standalone/README.rst` describes a successful 1940 run, but it
is written about the 2018/2020 codebase with a 2018-era configuration file;
neither is present here.

Making the path runnable required adding the configuration surface the release
does not carry for 1940. **No algorithm code was modified.**

### What was added

| File | Status | Purpose |
|---|---|---|
| `das_framework/certificate/{__init__,bom}.py` | new | `driver.py:61,73` imports `get_bom` from a package absent from the release |
| `programs/strategies/strategies_1940.py` | new | `Strategy1940` and `Strategy1940RegularOrdering` |
| `programs/strategies/strategies.py` | **+3 lines** | one import, one entry in each selector registry |
| `configs/Census1940/DDP2010_Update/ipums_1940_local.ini` | new | `INCLUDE`s the shipped config and supplies the four options the engine now requires |

Across those four: 286 insertions, 0 deletions, one existing file touched.
This document is additive too, and the release's own `README.md` is unchanged.
`git log` on this branch explains the reasoning behind each piece.

### What is missing from the release

`dprng`, the noise library, is imported unconditionally at
`programs/engine/primitives.py:5` but is not included. It is used only by
`RationalDiscreteGaussianMechanism` (`primitives.py:240`). The 1940 path uses
`RationalGeometricMechanism` — pure DP with the geometric mechanism, the engine
defaults at `budget.py:32-33` — and never reaches it, so a stub that raises on
call is sufficient (see section 4).

This does mean the public release **cannot reproduce the discrete-Gaussian
production runs as published.** The 1940 path is unaffected.

---

## 2. Getting the data

The file the code expects is published by IPUMS, created specifically for the
Census Bureau's development of this system:

**<https://usa.ipums.org/usa/1940CensusDASTestData.shtml>**

- File: `EXT1940USCB.dat` — 6.9 GB compressed, 39.5 GB uncompressed
- Access: a **free IPUMS account**. This is *not* the restricted full-count
  data; the extract carries no names or string variables, and the 1940 census
  passed the 72-year rule in 2012.
- Any publication using it must cite the extract. See the IPUMS page.

The layout matches `programs/reader/ipums_1940/ipums_1940_classes.py`
byte for byte: hierarchical fixed width, household records of 159 characters
each followed by their person records of 273, record type in column 1.

### Make a single-state subset first

Do not start with 132 million people. The file is ordered by state and Alaska
comes first, so the leading block is one complete state with its geographic
hierarchy intact:

```bash
zcat EXT1940USCB.dat.gz | awk '
  substr($0,1,1)=="H" { st=substr($0,54,2); if (first=="") first=st; if (st!=first) exit }
  { print }' > EXT1940USCB_AK.dat
```

This streams from the compressed file, exits as soon as the state changes, and
never cuts a household in half. Result: **22.7 MB, 24,277 households, 72,665
persons, 229 enumeration districts**.

Keep the `.gz` compressed until you need the national run. Spark reads gzip
transparently, but gzip is not splittable, so a 39.5 GB gzip file is processed
by a single task — decompress it only when you actually scale up.

---

## 3. Path A — Prototype (no Spark, no Java, no Gurobi)

This validates the configuration layer and the entire parse/recode path against
real data. It runs on Windows or Linux and takes about two minutes.

### Environment

Python 3.11 is required, **not** 3.12+. The code calls `np.issubsctype`,
removed in NumPy 2.0, at `programs/nodes/nodes.py:275` and
`programs/queries/constraints_dpqueries.py:131` — both on the real execution
path. Running under NumPy 2 would mean patching the release. No NumPy 1.x wheel
exists for Python 3.13+, so the Python version is what decides whether the
release stays unmodified.

```bash
python3.11 -m venv venv
source venv/bin/activate          # Windows: venv/Scripts/activate
pip install "numpy<2" scipy pandas psutil mpmath boto3 matplotlib randomgen pytest pyspark
```

`pyspark` is needed here only for `pyspark.sql.Row`, which is pure Python — no
Java required until you start a `SparkSession`.

### Shims

Two modules the code imports do not exist in this environment. Put them on
`PYTHONPATH` **outside the repository tree**, so the release stays unmodified:

- `dprng.py` — absent from the release (see section 1). Have
  `discrete_gaussian()` raise `NotImplementedError`, and define
  `OverflowBoundError`, `PerDrawOverflowStrategy` and `TotalOverflowStrategy`
  so that `primitives.py:242`'s `except` clause stays valid.
- `pwd.py` — **Windows only.** `das_framework/ctools/env.py:22` imports this
  Unix module at module scope. A `namedtuple`-based stand-in with `getpwuid`,
  `getpwnam` and `getpwall` is enough; nothing calls it during import.

### Run

```bash
export PYTHONPATH=/path/to/shims:$REPO/das_decennial:$REPO/das_decennial/das_framework
export SPARK_HOME=/anything    # conftest.py only needs the variable to exist

cd $REPO/das_decennial
pytest programs/constraints/tests/ programs/invariants/tests/ programs/schema/ -q
```

Expected: **446 passed**.

You can also exercise the real parse and recode path over the Alaska subset
without Spark, by calling `ipums_1940_classes.{H,P}.parse_line()` and the
`person_recoder` / `unit_recoder` classes from
`programs/reader/ipums_1940/ipums_1940_reader.py` directly. Over Alaska this
yields 24,277 households and 72,665 persons with zero parse or recode failures,
all geocodes 13 characters long, and an `hhgq1940` distribution of
`{0: 68630, 1: 455, 3: 349, 4: 278, 6: 102, 7: 2851}` — note that values 2 and
5 are absent, matching the comment in `map_to_hhgq`.

This matters because the recoders assert on every row
(`ipums_1940_reader.py:46,50,56,60` and the `assert hhgq >= 0` in
`map_to_hhgq`): a single out-of-range value kills the whole job. Validating
here is much cheaper than discovering it inside Spark.

---

## 4. Path B — Full pipeline on one state

Requires Java, Spark and a Gurobi licence. Use Linux, or WSL2 on Windows.

### 4.1 WSL2 (Windows only)

In an **administrator** PowerShell:

```powershell
wsl --install -d Ubuntu-22.04
```

Reboot, create your Ubuntu user, and do everything below inside WSL. Running
Spark natively on Windows is possible but requires `winutils.exe` and
`HADOOP_HOME`, plus path and permission workarounds; WSL avoids all of it and
behaves identically to the Linux host you will eventually use.

### 4.2 System packages

```bash
sudo apt update
sudo apt install -y openjdk-17-jdk python3.11 python3.11-venv git
```

### 4.3 Repository and data

**Do not work out of `/mnt/c`.** Spark over the 9p bridge is extremely slow.

```bash
mkdir -p ~/das && cd ~/das
git clone /mnt/c/path/to/DAS_2020_DHC_Production_Code repo
cd repo && git checkout make-1940-path-runnable

mkdir -p ~/das/data ~/das/out ~/das/shim
cp /mnt/c/path/to/EXT1940USCB_AK.dat ~/das/data/
cp /path/to/dprng.py ~/das/shim/        # pwd.py is not needed on Linux
```

### 4.4 Python environment

```bash
cd ~/das
python3.11 -m venv venv && source venv/bin/activate
pip install --upgrade pip
pip install "numpy<2" scipy pandas psutil mpmath boto3 matplotlib randomgen \
            "pyspark==3.5.*" gurobipy
```

Pin **PySpark 3.5**, the last 3.x release. The code targets Spark 2.4/3.x;
Spark 4 is a larger jump than this port needs to absorb.

### 4.5 Gurobi

The histogram is 8 x 2 x 116 x 2 x 6 x 2 = **44,544 cells per geographic
unit**, so each optimisation has tens of thousands of variables. The
size-limited licence bundled with the `gurobipy` wheel caps at 2,000 and
**will not work**. You need a real licence.

`optimizer.py:224` reads `GRB_LICENSE_FILE` from the environment in preference
to everything else, so pointing it at your licence file is all that is needed.

| Licence type | What to do |
|---|---|
| WLS / Web License Service | Copy the `gurobi.lic` containing `WLSACCESSID`/`WLSSECRET`. Works in WSL and containers as is. |
| Named-user academic | Machine-locked. WSL counts as a different host, so re-run `grbgetkey <key>` inside WSL, on the university network or VPN. |
| Token server / floating | Ensure the host can reach the server; set `TOKENSERVER` and `PORT` in the `.lic`. |

Verify:

```bash
python -c "import gurobipy; gurobipy.Model(); print('gurobi OK')"
```

### 4.6 Environment

```bash
cat > ~/das/env.sh <<'EOF'
source ~/das/venv/bin/activate
export DAS_REPO=~/das/repo/das_decennial
export PYTHONPATH=~/das/shim:$DAS_REPO:$DAS_REPO/das_framework
export SPARK_HOME=$(python -c "import pyspark,os;print(os.path.dirname(pyspark.__file__))")
export PATH=$SPARK_HOME/bin:$PATH

# Read without a fallback by the code; absent values raise
export MASTER_IP=127.0.0.1                      # das_utils.py:129, syslog target (UDP, discarded)
export DAS_DASHBOARD_URL=http://localhost:9999  # dashboard.py:159; post failures are caught
export LD_LIBRARY_PATH=${LD_LIBRARY_PATH:-/usr/lib}  # optimizer.py:109 requires it to be set

export DAS_1940_INPUT=~/das/data
export DAS_1940_OUTPUT=~/das/out
export GRB_LICENSE_FILE=~/gurobi.lic
EOF
source ~/das/env.sh
```

### 4.7 Run

```bash
cd $DAS_REPO
spark-submit \
  --driver-memory 8g --master 'local[*]' \
  --conf spark.driver.maxResultSize=0 \
  das_framework/driver.py \
  configs/Census1940/DDP2010_Update/ipums_1940_local.ini \
  --loglevel INFO 2>&1 | tee ~/das/run_alaska.log
```

Use `das_framework/driver.py`, not `das2020_driver.py`. The latter runs a
dashboard heartbeat thread, posts testpoints over HTTP, and typesets a LaTeX
certificate. `driver.py` is a complete entry point on its own
(`driver.py:1242`) and is what `run_standalone.sh` uses.

### 4.8 Checking the run

In order, the log should show:

1. `VINTAGE OF INPUT DATA: 1940` — the config resolved
2. `Levels: ('Enumdist', 'Supdist', 'County', 'State', 'National')` and
   `Global scale: 1/4`
3. `Detected Enumdist l2_dp_query_ordering:` listing all seven queries
4. Reader counts matching section 2: 24,277 households, 72,665 persons
5. Gurobi solves climbing the geolevels: 229 Enumdist, 4 Supdist, 4 County,
   1 State, 1 National — **239 optimisation problems**
6. `Run completed in ... seconds`, with output under `$DAS_1940_OUTPUT/person/`

---

## 5. Scaling to the national file

Everything above stays the same except the input path and the resources.

```ini
[reader]
PersonData.path: $DAS_1940_INPUT/EXT1940USCB.dat
UnitData.path: $DAS_1940_INPUT/EXT1940USCB.dat
```

Decompress first — a gzip file is not splittable and would run on one task:

```bash
gunzip -k EXT1940USCB.dat.gz     # 39.5 GB
```

### What changes

- **Disk.** 39.5 GB of input, plus the reader's `CEFhistograms.pickle`
  checkpoint (`table_reader.py:617`), plus output. Allow 150 GB.
- **Optimisation count.** One Gurobi solve per geographic unit at every level,
  each over 44,544 variables. Alaska needs 239; the national file is three
  orders of magnitude larger. See the note below.
- **Memory.** The National level aggregates the entire country into one node.
  Raise `--driver-memory` substantially, and consider
  `spark.driver.maxResultSize=0`.
- **Parallelism.** `local[*]` on one machine serialises the work. This is what
  the cluster settings in `run_cluster.sh:240-245` exist for
  (`EXECUTOR_CORES=4`, `EXECUTORS_PER_NODE=4`, `EXECUTOR_MEMORY=16g`,
  `EXECUTOR_MEMORY_OVERHEAD=20g`), against the EMR nodes described in
  `wiki/DAS-EMR-Configuration.md`: `r5.24xlarge`, 96 vCPU, 768 GB RAM.

A full national run is a cluster job, not a laptop job. If a single machine is
all you have, the practical approach is to run state by state: set
`geolevel_names` to `Enumdist,Supdist,County,State`, drop `National`, adjust
`geolevel_budget_prop` to four values, and feed one state's records at a time.
Note that this changes the privacy accounting — the top-level geounit becomes
the state — and must be stated as such in any write-up.

---

## 6. Reconstruction notes

Two values in the configuration could not be recovered from the release and
were chosen here. Both are documented in the docstring of
`programs/strategies/strategies_1940.py` and in the commit history, and both
should be disclosed in any publication.

1. **Geolevel budget proportions.** `ipums_1940.ini` carries seven values
   against the five geolevels it declares. The seven sum to 1 and match a
   seven-geolevel 2020 spine, so the line was carried over from a DHCP config
   and never adjusted. A uniform split across the five 1940 geolevels is used
   instead, following the precedent of `configs/Census1940/topdown.ini`, the
   repository's other 1940 config, which allocates uniformly.

2. **Query ordering.** The L2/rounder pass options postdate the 1940 configs
   entirely, so there is no original choice to preserve. A single-pass ordering
   is used, matching the config's implicit defaults: `L2PlusRounder`,
   `SinglePassRegular`, `CellWiseRounder`.

Everything else is transcribed from `ipums_1940.ini`. The seven queries and
their proportions are verbatim; `global_scale = 1/4` reproduces that file's
`epsilon_budget_total = 4.0`, since under pure DP the engine derives total
epsilon as `1 / global_scale` (`budget.py:224`).

---

## 7. Troubleshooting

| Symptom | Cause |
|---|---|
| `ModuleNotFoundError: certificate` | branch not checked out; the stub is part of it |
| `ModuleNotFoundError: dprng` | shim directory not on `PYTHONPATH` |
| `ModuleNotFoundError: pwd` | Windows without the `pwd` shim |
| `AttributeError: np.issubsctype` | NumPy 2 is installed; pin `numpy<2` |
| `DASConfigError: strategy` / `query_ordering` | running the shipped `ipums_1940.ini` instead of `ipums_1940_local.ini` |
| `KeyError: 'MASTER_IP'` or `'DAS_DASHBOARD_URL'` | environment not sourced |
| `RuntimeError: LD_LIBRARY_PATH not set` | `optimizer.py:109`; export it to anything |
| `Cannot import gurobipy` | `gurobi_path` is not empty in the config |
| Gurobi model size error | size-limited licence; a real one is required |
| Assertion inside `person_recoder` | input layout does not match `SPEC_DICT`; verify record widths are 159 and 273 |
| First Spark stage hangs | working out of `/mnt/c` under WSL |
| OOM at the National level | raise `--driver-memory` |
