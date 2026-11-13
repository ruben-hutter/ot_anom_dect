# Night-test artifacts (provenance)

Everything under `reports/artifacts/` was produced by the 2026-11-13 overnight run
(branch `test/anom-night-2026-11-13`). Raw multi-MB event files, model binaries (.joblib),
run logs and scratch scripts were deliberately NOT committed; they remain under the
ephemeral `.night/artifacts/` tree on the test server.

Layout (all CSVs small; scenario ∈ {normal, poll-storm, write-flood, off-schedule,
rogue-master, exception-storm}):

- `anom-data/<scenario>/windows.csv` — featurizer output @ feat/anom-data 33ac629
  (`simulate.py --hours 2 --scenario <s> --seed 42` → `featurize.py --window 60`),
  121 rows each. `anom-data/sanity-analysis.txt` — divergence analysis.
- `anom-kmeans/<scenario>/alerts-kmeans.csv` — detect_kmeans @ 13fbcaf. Scenario rows are
  the tails of combined runs (see next); `normal/alerts-kmeans.csv` is a normal-only run.
- `anom-kmeans/combined/` — combined inputs (`windows-<s>.csv`: verbatim normal 121 rows +
  scenario 121 rows; only window_id/t_start/t_end shifted) and full-run outputs
  (`alerts-<s>-full.csv`, 242 rows; attack = window_id ≥ 121). `summary.csv` + `REPORT.md`
  from the protocol-B subagent.
- `anom-methods/{iforest,ewma}/<scenario>.alerts.csv` — standalone per-scenario-file runs @
  e54d96c (self-train on first 60 rows → attack-baseline problem; see recap).
- `anom-methods/combined-view/` — orchestrator runs of both detectors on the k-means
  combined inputs (clean normal-head train).
- `anom-methods/selftest/ewma-selftest-control-chart-poll_gap_cv.csv` + `-meta.json` —
  deterministic regeneration of the EWMA `--selftest` fixture (the selftest itself writes
  only to a tempdir); drift = window_id 2000–2099.
- `anom-methods/alert-rate-table.txt`, `anom-methods/REPORT.md` — protocol-C subagent report.
- `integration-matrix.txt` — the scenario × detector tables in the recap.

Conventions: alerts CSV schema is `window_id,score,alert` everywhere. k-means/iforest
`alert` is per-window; **EWMA `alert` marks new-alarm episodes** (SPC latch); its per-window
out-of-control signal is `score > 0`.
