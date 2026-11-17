# GridScale

[![CI](https://github.com/Ananya6722/gridscale/actions/workflows/ci.yml/badge.svg)](https://github.com/Ananya6722/gridscale/actions)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-linux%20%7C%20macos%20%7C%20windows-lightgrey)](https://github.com/Ananya6722/gridscale)

**Zero-code-change, carbon- and cost-aware compute scheduling.**

GridScale is an OS-level process wrapper. Put it in front of *any* command - Python, Node.js,
Bash, Docker - and it decides **when** and **where** that command runs, with no SDK, no imports and
no refactoring. Every run produces an ESG audit receipt (Scope 2 CO2e avoided and spot-instance
savings).

```bash
gridscale --multi-region -- python real_workload.py
```

## How it works

```
                +------------------------------ gridscale ------------------------------+
 your command   |  telemetry            decision                 execution               |
 ------------>  |  UK National Grid --> Temporal shifting  -->  subprocess.run(cmd,      |
 (after "--")   |  Electricity Maps     (wait for clean grid,     env + GRIDSCALE_       |
                |  calibrated fallback   SLA --max-delay wins)    TARGET_REGION)         |
                |                       Spatial routing                                  |
                |                       (--multi-region)    -->  esg_audit_log.json      |
                +------------------------------------------------------------------------+
```

1. **Temporal shifting** - the job is held until grid carbon intensity falls to `--threshold`
   gCO2/kWh. A hard SLA deadline (`--max-delay`, minutes) always forces execution, so a job can
   never be starved.
2. **Spatial routing** (`--multi-region`) - five regions are compared, shown in a colour-coded table
   (cleanest first) and the job is dispatched immediately to the cleanest one.
3. **Environment injection** - the child process gets a copy of your environment plus
   `GRIDSCALE_TARGET_REGION=<selected region>`. The wrapped program may read it or ignore it.
4. **Audit ledger** - each run appends a JSON receipt to `esg_audit_log.json`:
   `co2_saved_g = max(0, (initial - final) * kWh)`. The spot saving (`baseline_cost * 0.58`) is a
   *modelled* figure and is credited only when the job ran on a cleaner grid than at submission;
   if nothing improved the receipt shows $0.00. Command-line parameters containing secrets or tokens
   are automatically sanitized and redacted (`[REDACTED]`) before being recorded.

### Telemetry and fallback chain

| Provider | Single region | Multi-region | If live data is unavailable |
|---|---|---|---|
| `uk` (default) | `api.carbonintensity.org.uk/intensity` | `/regional` (5 mapped regions) | Offline UK profile - never crashes a demo |
| `emaps` | `api.electricitymap.org/v3/carbon-intensity/latest?zone=` | 5 calibrated zones | Calibrated profile; source is shown in the output |

Electricity Maps needs `ELECTRICITY_MAPS_TOKEN` in the environment. Without it (or if the request
fails) GridScale uses calibrated profiles: `SE-SE3` 28, `US-CAL-CISO` 135, `DE` 265, `IN-SO` 485,
`IN-WE` 610 gCO2/kWh. The data source is always printed and recorded in the receipt.

| UK region (API shortname) | Cloud zone label |
|---|---|
| South Scotland | `eu-north-1a (Glasgow/Scotland)` |
| North East England | `eu-west-2b (Newcastle)` |
| North West England | `eu-west-2d (Manchester)` |
| South Wales | `eu-west-2c (Cardiff)` |
| London | `eu-west-2a (London)` |

**Savings baseline.** For spatial routing the baseline ("initial") is the region the job would
otherwise run in: London for `uk`, the `--zone` value for `emaps`. For temporal shifting it is the
grid intensity at submission time.

**Why London?** The UK API has no concept of "where you would have run", so London (the largest
cloud hub and the dirtiest of the five mapped regions in the offline profile) is used as a fixed
reference point. The headline saving is therefore *relative to that assumption*; change the
baseline and the number changes. Failed jobs (non-zero exit code) are recorded but credited
with no savings.

**Illustrative fallback data.** The calibrated `emaps` fallback values (including the two Indian
zones) are round-number placeholders for offline demos, not measured annual averages.

## Installation

### Directly from GitHub (Any OS)

Install directly into your Python environment without cloning:

```bash
pip install git+https://github.com/Ananya6722/gridscale.git
```

### From source

Clone the repository and install in editable mode:

```bash
git clone https://github.com/Ananya6722/gridscale.git
cd gridscale
pip install -e .

# Or install with test dependencies:
pip install -e .[test]
```

## Options

| Flag | Default | Meaning |
|---|---|---|
| `--provider {uk,emaps}` | `uk` | Telemetry source |
| `--zone` | `IN-SO` | Electricity Maps zone (`IN-SO`, `IN-WE`, `US-CAL-CISO`, `US-TEX-ERCO`, `DE`, `SE-SE3`, ...) |
| `--max-delay` | `240` | SLA: max minutes to wait before forcing execution |
| `--threshold` | `150` | Target gCO2/kWh |
| `--kwh` | `18.5` | Estimated workload energy |
| `--cost` | `24.00` | Baseline on-demand cost (USD) |
| `--poll` | 300 (30 for `--watch`) | Seconds between live grid checks |
| `--watch` | off | Live grid monitor, no command needed (add `--multi-region` for all regions) |
| `--demo` | off | Simulated 12-hour grid cycle, 1.5 s per 2-hour step |
| `--multi-region` | off | Route to the cleanest of 5 regions immediately |
| `--report` | off | Print cumulative ESG summary (add `--json` for raw JSON) |
| `--reset` | off | Delete the audit ledger and exit |
| `--dry-run` | off | Evaluate grid and simulate scheduling decision without executing the command |
| `--json` | off | Output receipt, report, or dry-run summary as structured JSON |
| `--log-file PATH` | `esg_audit_log.json` | Custom path for the ESG audit ledger (or via `GRIDSCALE_LOG_FILE`) |
| `--no-log` | off | Disable writing execution receipts to the audit ledger |
| `-v, --version` | off | Print program version and exit |

Live mode (no `--demo`) re-checks the real grid every `--poll` seconds (default 300; or set `GRIDSCALE_POLL_SECONDS`). The wrapped
command's exit code is returned as GridScale's own exit code.

## Stage demo

```bash
gridscale --reset
python real_workload.py                       # unscheduled baseline: "local-default (Unscheduled)"

# Temporal shifting. The demo grid runs 285 -> 240 -> 195 -> 115 -> 78 gCO2/kWh.
# A 480-minute SLA lets the job wait for the 00:00 Wind Surge (115):
gridscale --demo --threshold 130 --max-delay 480 --kwh 25.0 --cost 40.00 -- python real_workload.py
# With the default 240-minute SLA the deadline fires first, at 22:00 (195):
gridscale --demo --threshold 130 --kwh 25.0 --cost 40.00 -- python real_workload.py

# Spatial routing, live UK data
gridscale --multi-region --kwh 25.0 --cost 40.00 -- python real_workload.py

# Spatial routing, global zones (calibrated profiles unless ELECTRICITY_MAPS_TOKEN is set)
gridscale --provider emaps --multi-region --kwh 25.0 --cost 40.00 -- python real_workload.py

gridscale --report
gridscale --reset
```

`real_workload.py` is a dependency-free workload (50,000 synthetic telemetry rows, 6 epochs of
batch gradient descent). It never imports GridScale; it only reads `GRIDSCALE_TARGET_REGION` and
records it in `trained_grid_model.json`, which is how the demo proves the injection reached the
child process.

## Live mode (real data, no simulation)

```bash
gridscale --watch                      # live ticker of the grid, Ctrl+C to stop
gridscale --watch --multi-region       # live regional comparison table
gridscale --threshold 100 --max-delay 2 --poll 20 -- python real_workload.py
```

The last command checks the real grid every 20 s. If it is at or below 100 gCO2/kWh the job runs
at once; otherwise it waits, and the 2-minute SLA forces execution if the grid never gets there.
Each reading shows a local timestamp and, for UK data, the grid operator's own period timestamp.

## Testing

```bash
pytest -v
```

The suite never touches the network (HTTP is stubbed) and covers carbon-index classification, the
demo forecast, SLA override, ledger persistence/reset/recovery, provider fallbacks, and environment
injection - including into a real child process.

## Notes

- Cost figures use a fixed, modelled 58% spot discount (GridScale does not launch Spot instances); carbon figures are estimates from the `--kwh` you give.
- The wrapped command runs on the machine where GridScale runs. `GRIDSCALE_TARGET_REGION` is the
  hand-off to your own dispatcher or deploy script that places the job in that region.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
