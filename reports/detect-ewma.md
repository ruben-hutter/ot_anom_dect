# Detector: EWMA control chart (`lab/detect_ewma.py`)

The classic OT/SPC alarm layer over the 60 s traffic windows: an exponentially
weighted moving average per key feature, with control limits learned from the
training split. Catches **drift** — gradually worsening poll regularity,
creeping write counts — that per-window outlier scores (k-means, iforest)
structurally miss, and explains itself to an operator in one line:
*"poll_gap_cv drifted 3σ above baseline at 14:32"*.

Same input (`windows.csv`, pinned schema) and output
(`window_id,score,alert`) as the other detectors.

## Method in one paragraph

Windows are sorted by `t_start`; per feature the raw series is winsorized to
±10 train-σ, smoothed with `ewm(span=20, adjust=False)` over the *whole*
series (warm-started on train, so scored windows carry no warm-up artifact),
and limits are set at train-mean ± 3·std of the train portion of the EWMA
series (estimated after a 2·span burn-in — the EWMA warm-up transient would
otherwise shrink the observed std and tighten the limits). A feature that is
**constant in train** (e.g. `n_masters==1`) is a hard invariant: its EWMA is
the constant, so the *raw* value is charted and any deviation is an immediate
breach (zero-variance prior). Per window, `score = max(|ewma−mean|/std − 3, 0)`
over features; `alert` marks a **new alarm** — a chart freshly leaving its
limits, re-armed only after two consecutive windows back inside them.

## Default features and why exactly these four

| feature | what it encodes | catches |
|---|---|---|
| `poll_gap_cv` | heartbeat regularity | off-schedule jitter, bursty poll storms, **slow drift** |
| `n_write_fc` | operator writes are rare in a healthy lab | write floods, creeping write rates |
| `n_masters` | master count is an invariant | a second master joining, instantly |
| `n_except` | protocol health — a healthy PLC almost never errors | exception storms, scan probes |

They are the four "process variables" of the OT heartbeat (regularity, write
rarity, topology, protocol health). `poll_gap_mean_s` is deliberately *not*
charted by default: a uniform rate change (HMI reconfigured to 1 s polling) is
a configuration change, not drift — `n_req`/`n_read_fc` and the iforest/k-means
detectors cover intentional-looking step changes.

## Parameters and why

| Parameter | Value | Rationale |
|---|---|---|
| `span` | 20 windows (20 min) | pinned by the task; α=2/21≈0.095 → one-window spikes move the chart by ~10 % of their size, sustained shifts dominate. Steady-state chart σ ≈ √(α/(2−α)) ≈ 0.22× raw σ |
| `sigma` | 3 | classic Shewhart-style 3σ limits: ≈0.27 % false alarms per feature per window under normality; observed 2.7–3.7 % new-alarm rate on clean windows in the selftest (charts are autocorrelated and 4 features are charted) |
| winsorization | clip inputs to ±10 train-σ | a raw 40σ spike would leave the chart above 3σ for ~25 windows ("alarm ringing"). Clipped, a huge spike moves the chart ~4.4 chart-σ — alarms at once, recovers in ≤4 windows. Drift is unaffected (sustained shifts reach the same chart level, just a few windows later) |
| alarm latch + dead time | new alarm on leaving the limits; re-arm after 2 consecutive in-control windows | standard alarm-management practice: one excursion = one alarm; a recovering chart hovering at 2.5–3σ must not re-alarm on jitter (without it, ~30 % of anomalies produced a spurious follow-up alarm on the next clean window) |

## When it shines (vs k-means, vs iforest)

- **Drift.** The `poll_gap_cv` 0.05→0.4 ramp over 100 windows: **97 %** of the
  segment out of control, first new alarm ~6 windows in, 100 % of the last 30
  windows. iforest flagged 1/100 on the identical fixture; per-window
  detectors cannot see a trend made of unremarkable windows.
- **Invariants.** `n_masters=2` is caught 20/20 the moment it appears (raw
  invariant chart) — iforest gets 17/20 and only via side effects, because
  trees never split a constant-in-train feature.
- **Explainability.** Every alert is "feature X left mean±3σ of its baseline
  at window N"; the saved limits JSON is the whole model. An operator can
  audit it by eye; nobody can audit 200 trees.
- **Magnitude-aware scoring.** Unlike iforest (whose score saturates beyond
  the train range), the breach grows with how far the smoothed feature sits
  outside its limits.

## Where it does NOT shine (iforest / k-means win)

- **Point outliers in uncharted features.** Only the four defaults (or the
  `--features` override) are watched. A rogue master whose extra polls
  perfectly mimic the HMI cadence would still trip `n_masters`, but a
  read-storm with unchanged poll regularity is invisible — add features
  (`--features poll_gap_cv,n_write_fc,n_masters,n_except,n_read_fc`) or use
  iforest, which sees all 10 columns.
- **Smoothing lag.** A small (few-σ) single-window deviation does not breach
  by itself; it must be large (≈10 raw σ, via winsorization ≈4.4 chart-σ) or
  persistent. Very subtle one-shot anomalies are iforest's job.
- **Needs chronological, stationary-baseline data.** Train must be a clean
  "normal" period; a regime change mid-file breaks the limits (refit, or use
  per-shift baselines).
- Alert semantics are *events*, not per-window flags: a sustained excursion
  logs one alert, then `score>0` carries the ongoing state. When comparing
  detectors window-by-window, use `score>0` for EWMA.

## Selftest

`python3 lab/detect_ewma.py --selftest` (exit 0 = pass; this is the test
suite, there is no pytest). Same synthetic fixture as the iforest selftest
(byte-identical generator, seed 42): 2100 windows, 100 spaced point anomalies
(5 scenario types × 20, round-robin across the region) plus the 100-window
`poll_gap_cv` drift ramp. Asserts normal-alarm-rate < 5 %, point recall
> 80 %, drift coverage ≥ 50 %, drift-tail coverage ≥ 90 %, ≥1 new alarm
inside the drift segment; prints the confusion summary and per-feature
breaches; then re-runs the full CLI end-to-end on a schema-exact
`windows.csv` (alerts CSV header/rows, limits JSON with the invariant chart).

Seed 42 results (per-type alarms):

| scenario | alarmed |
|---|---|
| rogue-master | 20/20 |
| write-flood | 20/20 |
| exception-storm | 19/20 |
| off-schedule | 18/20 |
| poll-storm | 18/20 |
| **all point anomalies** | **95/100** |
| false alarms on normal windows | 24/850 (2.8 %) |
| drift segment | 97 % out of control, 1 new alarm, last 30 windows 100 % |

(Seeds 1/7/123: recall 94–95 %, drift 97–98 %, false alarms 2.7–3.7 % — all
assertions pass.)

## Running on real data

```sh
direnv allow                       # once, at the repo root
python3 lab/detect_ewma.py \
    --in  lab/data/windows.csv \
    --out lab/data/alerts-ewma.csv \
    --model-out lab/data/ewma-limits.json     # optional, human-readable
# knobs: --train-fraction 0.5 --sigma 3 --span 20 --winsor-sigma 10
#        --features poll_gap_cv,n_write_fc,n_masters,n_except
python3 lab/detect_ewma.py --selftest
```

Notes for the real `windows.csv` (produced by the `feat/anom-data`
featurizer): rows are sorted by `t_start`; empty cells (`poll_gap_*` when a
window has <2 reads) are filled with the train mean (neutral). The first
`--train-fraction` of the capture must be a clean baseline period. If the lab
ever legitimately runs with two masters, `n_masters` stops being an invariant
— drop it from `--features` or the chart will nag.
