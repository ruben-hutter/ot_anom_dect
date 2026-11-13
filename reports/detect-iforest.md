# Detector: Isolation Forest (`lab/detect_iforest.py`)

Tree-ensemble anomaly scorer over the 60 s traffic windows — the interpretable,
classical complement to the k-means detector. Same input (`windows.csv`, pinned
schema), same output (`window_id,score,alert`), so the three detectors
(k-means, iforest, ewma) compare apples-to-apples.

## Method in one paragraph

Features are z-scored with **train-split** statistics, then an
`sklearn.ensemble.IsolationForest` is fitted on the training half only
(first `--train-fraction` of the file, chronologically). The anomaly score of
every window is `-score_samples` (higher = more anomalous). The alert
threshold is the `--threshold-quantile` (default 0.99) quantile of the
**train** scores, so by construction only ~1 % of normal training windows
would alert — a calibrated false-alarm budget instead of sklearn's
`contamination` knob (left at `"auto"`, unused for scoring).

## Parameters and why

| Parameter | Value | Rationale |
|---|---|---|
| `n_estimators` | 200 | pinned by the task; more trees only smooth the score, 200 is well past the point of diminishing returns at this data size |
| `max_samples` | full train split (cap 4096) | **not** sklearn's default 256. 256-point subsamples starve the tails on small window counts: ordinary edge-of-range windows then score like true outliers, and the train-quantile threshold rises to meet them (measured: recall collapsed to 42 % with 256). Full-sample trees separate "corner" anomalies from "edge" normals cleanly |
| `contamination` | `"auto"` | irrelevant here — we never use `decision_function`; the threshold comes from the train-score quantile |
| features | all 10 numeric schema columns | counts, poll statistics, master count; override with `--features` |
| threshold | q99 of train scores | expected false-positive rate ≈ 1 % on normal data; observed 0.6–1.5 % across seeds |
| z-scoring | fitted on train | test anomalies never inflate/deflate the scale; a constant-in-train feature gets scale 1 (see limitations) |

## When it shines (vs k-means, vs EWMA)

- **No spherical-cluster assumption.** k-means distance-to-centroid fails when
  normal traffic is a non-convex or multi-modal set (e.g. two poll rhythms);
  random axis-aligned cuts carve arbitrary shapes.
- **Feature interactions for free.** "write flood *and* rising exceptions"
  needs no engineered feature; a tree isolates on whichever combination is
  rare.
- **No temporal assumptions.** Works on unordered windows; EWMA needs
  chronological order.
- Selftest: 96 % recall on point anomalies at 1.5 % false alerts (seed 42;
  84–99 % across seeds 1/7/123), catching all five scenario types.

## Where it does NOT shine (EWMA / k-means win)

Measured on the same synthetic fixture, honestly:

- **Drift is invisible.** The `poll_gap_cv` 0.05→0.4 ramp over 100 windows was
  flagged in **1/100** windows (only the extreme tail). Each single window is
  unremarkable; only the trend is anomalous. This is exactly what
  `detect_ewma.py` exists for (97 % coverage of the same segment).
- **Constant-in-train features are dead wood.** A feature that never varies in
  train (e.g. `n_masters==1` in a healthy lab) is never chosen for a split, so
  `n_masters=2` is invisible to the forest on its own — rogue-master windows
  were only caught via their extra reads/poll shifts (17/20, the weakest
  type). EWMA's invariant chart catches them 20/20, instantly.
- **Beyond-the-edge saturation.** A point outside the training range follows
  the same tree branches as the most extreme *training* point, so magnitude
  does not increase the score: a z=+250 poll storm scores about the same as a
  z=+10 one. Consequences: a single-feature anomaly with everything else
  typical (a pure off-schedule jitter window) ranks below ordinary multi-edge
  normal windows; and count features whose normal support is {0,1}
  (`n_except`) saturate — an exception storm "looks like" the rare window
  with one exception plus whatever else moves. Scenarios that move ≥2 live
  features are detected reliably.
- Train rows in the output CSV are in-sample (optimistically normal);
  evaluate on the scored half, as the selftest does.

## Selftest

`python3 lab/detect_iforest.py --selftest` (exit 0 = pass; this is the test
suite, there is no pytest). It generates a 2100-window synthetic lab (pinned
heartbeat: ~33 reads/window, rare single FC6 writes, one master, tight poll
stats), injects 100 point anomalies (5 scenario types × 20) into the scored
half, appends a 100-window `poll_gap_cv` drift ramp, asserts
normal-alert-rate < 5 % and point-anomaly recall > 80 %, prints the confusion
summary, then re-runs the full CLI end-to-end on a schema-exact
`windows.csv` (asserting the alerts CSV header/rows and the saved model).

Seed 42 results (per-type recall):

| scenario | caught |
|---|---|
| poll-storm | 20/20 |
| write-flood | 20/20 |
| exception-storm | 20/20 |
| off-schedule | 19/20 |
| rogue-master | 17/20 |
| **all point anomalies** | **96/100** |
| false alerts on normal windows | 13/850 (1.5 %) |
| drift segment (informational) | 1/100 |

## Running on real data

```sh
direnv allow                       # once, at the repo root
python3 lab/detect_iforest.py \
    --in  lab/data/windows.csv \
    --out lab/data/alerts-iforest.csv \
    --model-out lab/data/iforest.joblib     # optional
# knobs: --train-fraction 0.5 --threshold-quantile 0.99 --features a,b,c --seed 42
python3 lab/detect_iforest.py --selftest
```

Notes for the real `windows.csv` (produced by the `feat/anom-data`
featurizer): rows are sorted by `t_start` when numeric; empty cells (e.g.
`poll_gap_cv` when a window has <2 reads) are filled with the train mean
(z=0, neutral); all-empty-in-train features are dropped with a warning.
Expect the first ~50 % of windows to be the assumed-normal training period —
make sure the capture starts clean. Inspect the printed top-10 offenders
(dominant per-feature z-scores) before trusting the CSV.
