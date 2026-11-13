# Protocol C test report — anom-methods

- **Tested-at commit**: `e54d96c4f360ed4224bb20fd1fb7ffc6f6dc8155` (branch `feat/anom-methods`, matches expected)
- **Environment path used**: `nix develop --command bash -lc '<cmd>'` from worktree root (Python 3.12.12, pandas 2.2.3, sklearn 1.6.1, numpy 2.2.5). No fallback needed.
- **Worktree**: left clean (no commits, no tracked-file modifications; the `lab/__pycache__/` created by my read-only module import was deleted; `git status --short --ignored` clean at end).

## Item verdicts

| # | Item | Verdict |
|---|---|---|
| 1a | `detect_iforest.py --selftest` exit 0 | **PASS** (exit 0; normal rate 1.53% < 5%, point recall 96.00% > 80%, end-to-end CLI check PASS) |
| 1b | `detect_ewma.py --selftest` exit 0 | **PASS** (exit 0; normal 2.82% < 5%, recall 95.00% > 80%, drift coverage 97% ≥ 50%, drift tail 100% ≥ 90%, ≥1 drift alarm — all PASS) |
| 1c | EWMA selftest covers DRIFT case (poll_gap_cv slowly rising) and passes it | **PASS** — verified in source: `make_synthetic` appends 100 drift windows with `poll_cv = np.linspace(0.05, 0.4, n_drift)` (all other features normal); selftest asserts drift coverage/tail/new-alarm on exactly those windows and they pass |
| 1d | EWMA selftest saves a control-chart artifact to copy out | **N/A / finding** — `--selftest` writes only into an auto-deleted `tempfile.TemporaryDirectory`; no artifact exists to copy. Regenerated deterministically instead: `selftest/ewma-selftest-control-chart.csv` (+ meta JSON) from the identical fixture/detect call |
| 2 | Both detectors run on normal + 5 scenario windows.csv, outputs under artifacts | **PASS** (12/12 runs exit 0) |
| 3a | alerts CSV matches contract schema exactly (per detector, per scenario) | **PASS** — header exactly `window_id,score,alert`, 121 rows, window_ids match input, score float, alert ∈ {0,1} (12/12 files) |
| 3b | alert rate on normal ≲ 5% | **PASS** — scored region: iforest 0/61 (0.0%), ewma 0/61 (0.0%); full file: 1/121 (0.8%) each |
| 3c | each scenario clearly elevated vs normal | **FAIL (both detectors)** — see table |

## Alert-rate table

Scored region = rows after the in-file train split (`n_train = int(121*0.5) = 60` → 61 windows), the evaluation region the detectors themselves prescribe (train rows are in-sample). EWMA `alert` = latched "new alarm" per excursion episode (dead-time semantics), so counts are episodes, not raw out-of-control windows.

### detect_iforest (defaults: `--train-fraction 0.5 --threshold-quantile 0.99 --seed 42`)

| scenario | n_windows | n_alerts (scored) | alert_rate | mean score (scored) | n_alerts (full file) |
|---|---|---|---|---|---|
| normal | 61 | 0 | 0.0% | 0.4102 | 1 (window 0) |
| poll-storm | 61 | 0 | 0.0% | 0.4365 | 1 (window 0) |
| write-flood | 61 | 0 | 0.0% | 0.4352 | 1 (window 0) |
| off-schedule | 61 | 0 | 0.0% | 0.4557 | 1 (window 0) |
| rogue-master | 61 | 0 | 0.0% | 0.4156 | 1 (window 0) |
| exception-storm | 61 | 0 | 0.0% | 0.4090 | 1 (window 0) |

### detect_ewma (defaults: `--train-fraction 0.5 --sigma 3 --span 20 --winsor-sigma 10`)

| scenario | n_windows | n_alerts (scored) | alert_rate | mean score (scored) | n_alerts (full file) |
|---|---|---|---|---|---|
| normal | 61 | 0 | 0.0% | 0.0000 | 1 (window 0) |
| poll-storm | 61 | 1 | 1.6% | 0.0067 | 1 (window 94) |
| write-flood | 61 | 0 | 0.0% | 0.0000 | 2 (windows 0, 15) |
| off-schedule | 61 | 0 | 0.0% | 0.0000 | 1 (window 0) |
| rogue-master | 61 | 0 | 0.0% | 0.0000 | 1 (window 0) |
| exception-storm | 61 | 3 | 4.9% | 0.2331 | 4 (windows 0, 71, 87, 100) |

**Verdict 3c**: iforest — 0 scored-region alerts everywhere (scenarios indistinguishable from normal). EWMA — only poll-storm (1) and exception-storm (3) above normal's 0, and not "clearly elevated"; write-flood/off-schedule/rogue-master equal normal (0).

## Root cause (finding, not fixed)

Each scenario's `windows.csv` is anomalous from window 0 (protocol A sanity: rogue-master has `n_masters==2` in ALL windows; write-flood `n_write_fc` mean ≈ 30 across all windows; etc.). Both detectors fit their baseline on the **first half of the same input file** (`--train-fraction 0.5`; there is no flag to supply external normal-only reference data), so the attack is absorbed into the train baseline and the scored half looks "normal" relative to it. The detectors' selftests pass because their synthetic fixture is clean for the first 1050 windows with anomalies only in the scored half. Additionally: window 0 alarms in EWMA are a warm-up artifact (limits estimated after burn-in, EWMA starts at its first value); iforest's documented blindness to train-constant features (e.g. `n_masters`) also applies to rogue-master here.

## CLI flags used + deviations

- iforest: `--in <anom-data>/<scenario>/windows.csv --out <artifacts>/iforest/<scenario>.alerts.csv --model-out <artifacts>/iforest/<scenario>.model.joblib` (all other flags default)
- ewma: `--in <anom-data>/<scenario>/windows.csv --out <artifacts>/ewma/<scenario>.alerts.csv --model-out <artifacts>/ewma/<scenario>.limits.json` (all other flags default)
- **Deviation recorded**: no separate training step on normal windows exists or was performed — neither detector accepts external reference data; each file self-trains on its own first 60 windows. `lab/README.md` documents the network lab only, not detector CLIs; flags taken from `--help`/source. Model/limits files saved beyond the required alerts CSVs (extra evidence, harmless).
- Selftest artifacts: none produced by the scripts themselves (tempdirs); logs teed, EWMA control chart regenerated as substitute.

## Errors verbatim

None. All 14 invocations (2 selftests + 12 runs) exited 0; no detector error output. Only protocol-level check 3c fails, as analyzed above. (Context: protocol A's own `sanity-analysis.txt` ended `SANITY RESULT: FAIL` for divergence ratios — poll-storm 9.92x vs wanted ≥10, off-schedule 1.95x vs wanted ≥3 — which compounds, but does not cause, the 3c failure here.)

## Artifacts (absolute paths)

- Selftests: `selftest/iforest-selftest.log`, `selftest/ewma-selftest.log`, `selftest/ewma-selftest-control-chart.csv` (+ `-meta.json`, `regen_ewma_chart.py`)
- iforest: `iforest/{normal,poll-storm,write-flood,off-schedule,rogue-master,exception-storm}.alerts.csv` (+ `.model.joblib`, `.run.log` each)
- ewma: `ewma/{normal,poll-storm,write-flood,off-schedule,rogue-master,exception-storm}.alerts.csv` (+ `.limits.json`, `.run.log` each)
- Tables: `alert-rate-table.txt`, `alert-positions.txt`; scripts `analyze_protocol_c.py`, `analyze_supplement.py`
- All under `/data/ot_anom_dect/.night/artifacts/anom-methods/`
