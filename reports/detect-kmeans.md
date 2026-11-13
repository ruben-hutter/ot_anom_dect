# k-means anomaly detector (`lab/detect_kmeans.py`)

One-class k-means detector over the shared 60 s windows schema
(`windows.csv` → `alerts-kmeans.csv`). Implements the approach of
`docs/primer-modbus-kmeans.md` §3–4: learn a baseline of *normal* poll
traffic, flag windows that are far from every centroid.

## Method

1. **Load** `windows.csv` (pinned 13-column header). `window_id`, `t_start`,
   `t_end` are identity/time and never clustered.
2. **Training split** = head of the file, first `--train-fraction` (default
   0.5) of the rows in file order. This relies on the pinned contract that
   windows.csv is time-sorted and starts with the normal capture.
3. **Standardize** every feature with the training mean/std (`StandardScaler`,
   fit on train only, stored and reused for all rows). NaN cells — the
   contract allows empty `poll_gap_*` when a window has < 2 read requests —
   are imputed with the *training-split median* of that column. Columns with
   no numeric data in the training split are dropped (with a note).
4. **Fit k-means** (k-means++ init, `n_init=10`, seeded) on the training
   split only.
5. **Score** every window: `score = distance to the nearest centroid`
   (Euclidean, standardized space).
6. **Threshold** = `--threshold-quantile` (default 0.99) quantile of the
   *training* scores; `alert = score > threshold`.
7. **Write** `window_id,score,alert` for all windows; print a summary with
   the top-10 offenders and each one's dominant features (per-feature
   z-scores vs training) — this is what makes an alert analyzable.
8. Optional `--model-out`: joblib with features, impute medians, scaler,
   kmeans, threshold (and the k-selection rule/table).

## Default feature list

All numeric columns of the pinned schema except `window_id/t_start/t_end`
(10 features); override with `--features col1,col2,...`:

| Feature | What it captures |
|---|---|
| `n_req` | total requests in the window (poll rate) |
| `n_resp` | responses (loss/timeouts shift it below `n_req`) |
| `n_except` | exception responses — probing/scan fingerprint |
| `n_read_fc` | read FCs 1–4 — the cyclic poll load |
| `n_write_fc` | write FCs 5/6/15/16 — rare in steady state |
| `n_other_fc` | non-read/write FCs — diagnostics/device-ID/recon |
| `n_masters` | distinct masters polling (rogue master) |
| `poll_gap_mean_s` | mean inter-poll gap per master (poll rate regularity) |
| `poll_gap_cv` | gap std/mean — off-schedule/irregular polling |
| `n_unique_write_addr` | distinct addresses written (write sweep breadth) |

## k selection and threshold rationale

k is chosen **on the training split only**: candidates k = 1..`--kmax`
(default 3), scored by mean silhouette; inertia is printed for each k so the
elbow is visible. The best k ≥ 2 is taken **only if its silhouette ≥ 0.5**
(genuinely separated regimes); otherwise the model falls back to **k = 1** —
the one-class case, where normal traffic is a single tight cluster. Forcing a
k is possible via `--k`.

Rationale: this lab's normal traffic has no distinct regimes (one HMI, one
poll period), so a homogeneous blob should not be split arbitrarily —
splitting a blob raises the threshold's job (per-cluster distances get
smaller) and can *hide* anomalies that sit between artificial half-clusters.
With silhouette < 0.5 the split is artificial; k = 1 keeps "normal" tight and
the distance threshold strict.

The threshold itself is the empirical `--threshold-quantile` of training
scores, i.e. it inherits the training distance distribution including
rare-but-normal windows (single operator write, single exception) instead of
a parametric χ² assumption.

## Selftest (`--selftest`, exits 0 on pass)

Generates ~2000 synthetic normal windows from the pinned schema (n_req≈30±5
on a 60 s grid, `poll_gap_mean_s ≈ 60/n_req`, n_read_fc = n_req−0..1, 5% of
windows one FC6 write, ~2% one lost response, ~2% one exception,
n_masters=1, poll_gap_cv≈0.05±0.03), plus 100 injected attack windows
(20 each):

| Attack shape | Synthetic definition |
|---|---|
| poll-storm | poll_gap/10 (n_req≈300), cv unchanged, reads only |
| write-flood | 20–30 writes on top of the normal poll load, many distinct addresses |
| rogue-master | n_masters=2, per-master gap unchanged → counts ≈2× |
| off-schedule | normal counts but poll_gap_cv ∈ [0.5, 0.95] |
| exception-storm | 20–50 bad-address probes → n_except spike |

Attacks are scattered through the *evaluation* half only (training head stays
normal, matching the real contract). The whole CSV round-trip, alert schema,
and model persistence are exercised. Results (seed 0):

```
k selection: k=1 inertia=8400.0 · k=2 silhouette=0.374 · k=3 silhouette=0.419 → k=1
threshold: 7.949 (quantile 0.99 of training scores)

            alert  no-alert
  attack       98         2   recall       98.00%
  normal       29      1971   alert rate    1.45%

PASS: alert rate on normal 1.45% (<5%), recall on attacks 98.00% (>80%)
```

The 2 misses are borderline rogue-master windows (score ≈ 7.4–7.9 vs
threshold 7.95): a second master polling at exactly the baseline rate only
triples the correlated count features to z≈6, which sits near the threshold
set by rare-but-normal write/exception training windows. Other seeds behave
the same (FAR 0.85–1.45%, recall 98%).

## Running on real data (when `feat/anom-data` lands)

```sh
direnv allow                       # once, at the worktree root
python3 lab/detect_kmeans.py --in lab/data/windows.csv --out lab/data/alerts-kmeans.csv
# optional: keep the fitted baseline for later scoring
python3 lab/detect_kmeans.py --in lab/data/windows.csv --out lab/data/alerts-kmeans.csv \
    --model-out lab/data/kmeans-model.joblib
```

Useful flags: `--train-fraction 0.5`, `--threshold-quantile 0.99`,
`--kmax 3`, `--k <int>` (force), `--features col1,col2`, `--seed`.
No pytest in this repo — `--selftest` **is** the test suite:

```sh
python3 lab/detect_kmeans.py --selftest     # must exit 0
```

Alerts output is the shared detector contract `window_id,score,alert`, so it
can be diffed against the Isolation-Forest/EWMA detectors from
`feat/anom-methods` on identical inputs.

## Known limitations

- **Distance threshold vs novel-but-normal regimes**: anything the training
  head didn't contain (a legitimate second HMI, a recipe download, a shift
  change in poll rate) is "anomalous" by construction — one-class k-means has
  no notion of *novel but benign*. Expect false alarms on operational change,
  and retrain the baseline after intentional changes.
- **PV drift invisible**: features are counts/timing only; a lying slave or a
  slow process-value drift (attack on the data, not the traffic) does not
  move any feature. Catching it needs payload-value features, out of scope
  for this contract.
- **Rare-but-normal events inflate the threshold**: a single operator write
  or exception in a training window produces huge z-scores (tiny train std),
  so the 99th-pctile threshold is set by those windows (~8 instead of ~4
  here), reducing sensitivity to subtle attacks (the missed rogue-master
  windows above).
- **Correlated features**: `n_req/n_resp/n_read_fc` are near-duplicates and
  each write is counted in two features (`n_write_fc`, `n_unique_write_addr`),
  so the score double-counts some signals; thresholds are empirical so this
  is calibrated away, but distances are not χ²-distributed.
- **Poisoned training head**: if attacks ever land inside the training
  fraction, the threshold inherits them. The contract (time-sorted, normal
  head) protects this; verify before trusting results on new data.
- **i.i.d. windows**: no temporal context (a slow ramp into an attack is one
  borderline window at a time); k-means assumes spherical, equal-variance
  clusters — see the primer §4.5 improvement menu (per-master baselines,
  GMM, Isolation Forest — the sibling worktree).
- **NaN imputation**: windows with < 2 reads get the training-median gap
  features, i.e. they look "average" on timing; they are not flagged by
  themselves.
