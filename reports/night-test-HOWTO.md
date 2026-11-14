# Night test 2026-11-13 — state + by-hand howto

Companion to `night-test-RECAP.md` (the morning deliverable). This file documents
(1) the exact state of the repo after the run and (2) the per-branch test recipes and
evaluation methodology, so anyone can reproduce the whole night by hand.

## 1. State after the run

- **Pushed:** `test/anom-night-2026-11-13` (local == origin). Contains only test outputs:
  `reports/` (recap, this howto, artifacts, plotting script) and `plots/*.png` (6).
- **Untouched (read-only, verified):** `main` `5359451…`, `feat/mini-ot-lab` `6d90d5d…`,
  `feat/anom-data` `33ac629…`, `feat/anom-kmeans` `13fbcaf…`, `feat/anom-methods` `e54d96c…`.
  No commits, rebases, or force-pushes to any of them.
- **Cleaned up:** the three `.night/*` worktrees were removed (`git worktree remove`);
  the only remaining worktree is `main`/ (checkout on the test branch, working tree clean).
- **Not in git:** `/data/ot_anom_dect/.night/artifacts/` (~22 MB on server disk only:
  raw `events.jsonl`, `.joblib` models, run logs). Safe to delete; the distilled CSVs
  under `reports/artifacts/` are the committed record.
- **Real-lab leg:** SKIPPED — docker daemon broken on the test server, so **no real pcap
  traffic was captured**; all evaluation ran on the offline simulator.

## 2. Per-branch recipes (what a human would type)

Environment throughout: `nix develop` (repo flake → Python 3.12, numpy/pandas/scikit-learn,
pymodbus). System python3 has the same deps. All work happens in read-only worktrees
(`git worktree add .night/<branch> <branch>`); outputs go to a scratch dir, never into the
feature branches.

### feat/anom-data — generate data, check the signal exists

```bash
python3 lab/simulate.py --selftest && python3 lab/featurize.py --selftest   # both exit 0
for s in normal poll-storm write-flood off-schedule rogue-master exception-storm; do
  python3 lab/simulate.py  --hours 2 --scenario $s --out $s/events.jsonl   # seed 42, window 60s defaults
  python3 lab/featurize.py --events $s/events.jsonl --out $s/windows.csv
done
```

Evaluation (pandas): per scenario, compare the **target feature** against the normal run —

| scenario | feature | normal → scenario |
|---|---|---|
| poll-storm | `poll_gap_mean_s` | 1.80 → 0.18 (9.92×) |
| write-flood | `n_write_fc` | 0.12 → 30.2 per window |
| off-schedule | `poll_gap_cv` | 0.33 → 0.65 (only 1.95×) |
| rogue-master | `n_masters` | 1 → 2 in all 121 windows |
| exception-storm | `n_except` | 0 → 11.0 per window |

Plus: exact string-match of the windows.csv header against the pinned contract (passed on
all 6 files), and distribution-separation checks, not just means (e.g. poll-storm cleanly
separated: normal min 1.62 > storm max 0.20; off-schedule tails overlap).

### feat/anom-kmeans — detect, after fixing the input shape

```bash
python3 lab/detect_kmeans.py --selftest                                          # 98% recall, 1.45% FP
python3 lab/detect_kmeans.py --in normal/windows.csv --out ... --model-out ...   # 0.83% alerts on normal
```

Scenario files **cannot** be fed directly: the detector trains on the head of its input
(`--train-fraction 0.5`) and scenario files are attack from window 0 → it would train on
the attack. Hand-fix: build combined files — 121 verbatim normal rows + 121 scenario rows
(only `window_id`/`t_start`/`t_end` shifted; feature columns untouched) — then run with
`--train-fraction 0.5` so the train split is exactly the normal head, and count alerts in
the tail 121 rows. Result: 99–100 % alert rate on every scenario, ≤2 % on normal heads.

### feat/anom-methods — same story, twice

```bash
python3 lab/detect_iforest.py --selftest   # 96% recall
python3 lab/detect_ewma.py    --selftest   # 95% recall; source check: the fixture really
                                           # drifts poll_gap_cv (linspace 0.05→0.4 over the
                                           # last 100 windows) and asserts drift coverage
```

Two views, both run:

1. **Standalone** (the shape anom-data emits): `--in <scenario>/windows.csv` — both
   detectors self-train on the attack head and detect ~nothing (iforest 0 alerts on every
   scenario; EWMA 0–3 episodes).
2. **Combined** (the shape the detectors assume): the same combined inputs k-means got.
   EWMA: out-of-control (`score > 0`) on 99–100 % of attack windows. iforest: only
   off-schedule (53 %).

## 3. How the results were evaluated

No captured traffic exists (docker down → lab leg SKIPPED), so everything below was
evaluated on simulator output (`events.jsonl` → `windows.csv`).

1. **Contract check.** Exact header match on every `windows.csv` (6) and alerts CSV (20):
   `window_id,score,alert`. The syntactic contract held across all three branches.
2. **Signal quality first.** Before trusting any detector, verify the featurized signal:
   means **and** distribution separation per scenario vs normal (see table above). The two
   weakest injections (off-schedule 1.95× cv; exception-storm's mild windows) predicted
   exactly where detectors later disagreed.
3. **False-alarm budget vs detection, in both views.** Normal files: ≤5 % alerts required
   (measured 0.83–2 % everywhere). Attack files scored standalone vs combined — the gap
   between the two views *is* the integration finding.
4. **Semantics-aware counting.** Sources were read before judging numbers:
   - EWMA `alert` = **new-alarm episodes** (SPC latch, re-arm after 2 clean windows), not
     per-window flags → summing it undercounts; the fair per-window rate is `score > 0`
     (99–100 % on attacks).
   - iforest threshold = 0.99-quantile of train scores → point-outlier design by intent;
     explains why gradual/off-schedule-style shifts are its only catch.
   - EWMA rogue-master score ~1e6 = documented "constant-in-train feature = hard invariant"
     sentinel (`n_masters` 1→2 breaches instantly). Working as designed.
5. **Root cause over verdict.** The zero-detection standalone result was traced to a
   mechanism (self-train on input head + attack-from-window-0 files), not shrugged off.
   It's a **contract gap between branches** — no branch fails its own selftest because of
   it — hence recap recommendation #1: add a normal-prefix mode to anom-data or a
   `--train-file` flag to the detectors before merging any of them.

Full numbers and per-branch verdicts: `night-test-RECAP.md`. Matrices:
`artifacts/integration-matrix.txt`. Raw subagent protocols/reports: `artifacts/*/REPORT.md`.
