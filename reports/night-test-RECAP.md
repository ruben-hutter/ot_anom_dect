# Night test recap — anomaly detection branches

**Date:** 2026-11-13 (overnight) · **Orchestrator branch:** `test/anom-night-2026-11-13`
**Scope:** integration test of the three parallel `feat/anom-*` branches against the pinned
windows.csv/alerts CSV contract; read-only on all `feat/*` branches. No feature code was
modified. All outputs of this run live on this branch only (`reports/`, `plots/`).

## Tested-at commits (all match the expected table)

| branch | expected | tested | match |
|---|---|---|---|
| `main` | `5359451` | `5359451c2fd6…` | ✅ |
| `feat/mini-ot-lab` | `6d90d5d` | `6d90d5d2a13d…` | ✅ (not directly exercised — docker down) |
| `feat/anom-data` | `33ac629` | `33ac629cb079…` | ✅ |
| `feat/anom-kmeans` | `13fbcaf` | `13fbcaffcfc6…` | ✅ |
| `feat/anom-methods` | `e54d96c` | `e54d96c4f360…` | ✅ |

## Environment

- **`nix develop`** (repo flake): Python 3.12.12, numpy 2.2.5, pandas 2.2.3, scikit-learn 1.6.1,
  pymodbus present. (System python3 would also have satisfied the deps; venv fallback not needed.)
- **Docker daemon broken on this server** (`docker ps` exits 1, confirmed once) → **real-lab
  validation (containerlab deploy, `just capture 60`, pcap featurize) SKIPPED** — not a FAIL.
  Everything else ran offline.
- Orchestration: protocol A ran first (B/C consume its artifacts), then B and C in parallel
  via subagents; combined-view iforest/ewma runs (below) done by the orchestrator.

---

## feat/anom-data @ 33ac629 — verdict: **GOOD**

| # | check | result |
|---|---|---|
| 1 | `simulate.py --selftest`, `featurize.py --selftest` | **PASS** (both exit 0) |
| 2 | simulate+featurize normal 2 h + 5 scenarios | **PASS** (6 × events.jsonl + windows.csv, 121 windows each, 7260 s span; CLI exactly as documented, `--hours 2` for all six for apples-to-apples) |
| 3 | sanity divergence analysis | **PASS** — all 5 scenarios diverge in the expected direction (table below) |
| 3b | windows.csv header vs pinned contract | **MATCH** on all 6 files, exact |
| 4 | real lab (docker+clab) | **SKIPPED** (docker daemon broken) |

Divergence table (window means, 2 h runs):

| scenario | target feature | normal | scenario | divergence |
|---|---|---|---|---|
| poll-storm | `poll_gap_mean_s` | 1.8004 | 0.1815 | ✅ ↓ **9.92×** (distributions fully separated: normal min 1.624 > storm max 0.204) |
| write-flood | `n_write_fc` | 0.116 | 30.20 | ✅ high (~261×) |
| off-schedule | `poll_gap_cv` | 0.332 | 0.647 | ✅ up, but only **1.95×** (weakest of the five) |
| rogue-master | `n_masters` | 1.0 | 2.0 | ✅ 2 in all 121 windows |
| exception-storm | `n_except` | 0.0 | 11.04 | ✅ high (3–19 per window) |

Issues (findings, not blockers):
- poll-storm lands at 9.92× instead of the nominal 10×: jitter scales with the base interval
  but the every-10th-cycle near-zero FC3 gap (fixed ~0.06 s) is pooled into the mean and
  doesn't scale. Cosmetic — separation is total.
- **off-schedule is a weak injection**: cv only 1.95× normal, individual-window tails overlap
  (off-sched min 0.075 vs normal max 0.479). Window *means* separate cleanly; a cv-threshold
  detector will miss individual windows.
- Feature cross-talk: poll-storm's cv (0.680) is *higher* than off-schedule's (0.647) — cv
  alone cannot discriminate these two scenarios.
- **Every scenario file is attack-from-window-0** — no normal prefix. Harmless for the
  featurizer, but it is the root cause of the detector integration gap below.

## feat/anom-kmeans @ 13fbcaf — verdict: **GOOD-WITH-ISSUES**

| # | check | result |
|---|---|---|
| 1 | `detect_kmeans.py --selftest` | **PASS** (exit 0; confusion: recall 98.0%, normal alert rate 1.45%) |
| 2 | run on protocol-A artifacts | **PASS** via a *documented adaptation*: detector trains on the head of its input (`--train-fraction 0.5`), so per-scenario files (attack throughout) would poison training. Built `combined/windows-<s>.csv` = verbatim normal 121 rows + scenario 121 rows (only identity cols `window_id`/`t_*` shifted; feature cols verbatim); train split lands exactly on the normal head |
| 3 | alerts schema; normal rate ≲5%; scenarios elevated | **PASS** — schema exact ×6; normal **0.83 %**; scenarios **99.17–100 %** |

| scenario | n_alerts / 121 | rate | mean score |
|---|---|---|---|
| normal | 1 | 0.83 % | 1.30 |
| poll-storm | 121 | 100 % | 193.1 |
| write-flood | 121 | 100 % | 85.7 |
| off-schedule | 121 | 100 % | 21.7 |
| rogue-master | 121 | 100 % | 38.2 |
| exception-storm | 120 | 99.17 % | 11.1 |

Issues:
- **Implicit input contract: "input must start with normal traffic"** is documented in the
  detector's design but not enforced or flag-able — there is no `--train-file`/`--reference`
  option, so anom-data's scenario outputs cannot be consumed as-is. This cost the protocol an
  input-construction step tonight and will bite the first real deployment.
- On the real normal head the silhouette rule picks **k=3** (0.826) vs k=1 in the synthetic
  selftest — normal window 0 has only 9 reqs (startup transient), so the normal file itself
  contains a regime boundary. Consider a startup-burn-in.
- exception-storm's single miss is a genuinely mild window (n_except = 3).

## feat/anom-methods @ e54d96c — verdict: **GOOD-WITH-ISSUES**

| # | check | result |
|---|---|---|
| 1a | `detect_iforest.py --selftest` | **PASS** (exit 0; recall 96 %, normal 1.53 %) |
| 1b | `detect_ewma.py --selftest` | **PASS** (exit 0; recall 95 %, normal 2.82 %) |
| 1c | EWMA drift case verified | **PASS** — fixture really injects `np.linspace(0.05, 0.4, 100)` on `poll_gap_cv` over the last 100 windows; selftest asserts drift coverage ≥50 % (got 97 %), drift-tail ≥90 % (100 %), ≥1 drift alarm (1) and passes |
| 2 | run both detectors on protocol-A artifacts | runs **PASS** (12/12 exit 0) — but see 3 |
| 3 | normal ≲5 %; scenarios clearly elevated | **FAIL on the natural per-scenario files** — iforest **0 alerts on every scenario**, EWMA 0–3. Root cause (verified): both detectors self-train on the first half of their input file; scenario files are anomalous from window 0, so the attack becomes the baseline. Selftests pass only because their synthetic fixtures are clean in the train half |
| 3b | alerts schema | **PASS** 12/12 exact (`window_id,score,alert`) |

Given a clean train (orchestrator's combined-view runs on the same normal-head + scenario-tail
inputs k-means used — see integration section), EWMA lights up on everything while iforest
stays weak.

Issues:
- **No way to supply normal reference data** (same missing seam as k-means): neither detector
  accepts a training/reference file; `lab/README.md` on this branch covers the network lab
  only, CLI discovered via `--help`/source.
- **`alert` semantics drift**: EWMA's `alert` marks *new-alarm episodes* (SPC latch: alarm →
  re-arm after 2 clean windows), while k-means/iforest flag per-window. Schema-identical,
  semantically different — anything downstream summing `alert` will miscompare detectors.
- iforest, even with a clean train head, only catches **off-schedule (53 %)**; 4/5 scenarios
  ≤ 2 alerts. Its threshold = 0.99-quantile of train scores ≈ point-outlier detector; the
  branch's own docs acknowledge "gradual drift escapes a point-outlier detector".
- EWMA `n_masters`-invariant breach (rogue-master) works exactly as designed and saturates the
  score (~1e6 sentinel) — good, explainable, instant detection.
- EWMA selftest saves no artifact (tempdir only) — minor DX gap; a deterministic regeneration
  is committed under `reports/artifacts/anom-methods/selftest/`.

---

## Integration (the actual point of tonight)

### Contract adherence
- **windows.csv header: exact match on all 6 files.** **alerts CSV header: exact match on all
  20 outputs** (6 k-means, 6+6 standalone iforest/ewma, 10 combined-view). The pinned
  *syntactic* contract held across all three branches.
- **The implicit contract did NOT hold:** all three detectors train on the head of their input,
  but anom-data emits attack-throughout files with no normal prefix. Nobody in the stack emits
  the stream shape the detectors require. This is the one real integration bug-like finding —
  it sits *between* the branches, so no single branch fails its own selftest because of it.

### Scenario × detector matrix

View 1 — detector run on the scenario file alone (natural anom-data output shape):

| scenario | kmeans | iforest | ewma (episodes) |
|---|---|---|---|
| normal | 0.83 % | 0 % (scored region) | 0 % |
| poll-storm | N/A* | 0 % | 1.6 % |
| write-flood | N/A* | 0 % | 0 % |
| off-schedule | N/A* | 0 % | 0 % |
| rogue-master | N/A* | 0 % | 0 % |
| exception-storm | N/A* | 0 % | 4.9 % |

\* k-means cannot run this shape at all (head-train would poison on attack data).

View 2 — combined deployment shape: 121 normal windows then 121 attack windows
(train split = normal head; the shape all three detectors implicitly assume):

| scenario | kmeans tail | iforest tail | ewma tail (episodes / OOC windows) |
|---|---|---|---|
| normal head (all runs) | 1.7–2 % | 2 % | 1 % |
| poll-storm | **121 / 100 %** | 1 / 1 % | 1 ep / **100 %** |
| write-flood | **121 / 100 %** | 1 / 1 % | 2 ep / **100 %** |
| off-schedule | **121 / 100 %** | 64 / 53 % | 1 ep / **99 %** |
| rogue-master | **121 / 100 %** | 1 / 1 % | 2 ep / **100 %** (n_masters invariant, score ~1e6) |
| exception-storm | **120 / 99 %** | 2 / 2 % | 2 ep / **100 %** |

(OOC = windows with score>0, i.e. outside control limits — EWMA's per-window detection rate;
its `alert` column collapses those into latched episodes.)

### Read on the detectors
- **k-means is the workhorse**: near-perfect per-window flags on all five scenarios with ≤2 %
  false alarms. Merge candidate once the input-shape seam is addressed.
- **EWMA is the best alarm layer**: out-of-control on essentially every attack window, instant
  on the rogue-master invariant, and its episode semantics are exactly what a human SOC alarm
  panel wants — but it needs an `ooc`-per-window column (or documented semantics) before
  anyone computes "alert rates" from it.
- **iforest adds the least**: only off-schedule (53 %) even with a clean train; redundant
  behind k-means on these scenarios. Its 0.99-train-quantile threshold is a deliberate
  point-outlier design; if kept, position it as a complement, not coverage.
- Cross-check: the two scenarios with the *subtlest* feature shifts (off-schedule 1.95× cv,
  exception-storm's mild windows) are exactly where detectors disagreed most — consistent with
  protocol A's divergence magnitudes, i.e. the featurizer's signal explains the detector
  spread. No unexplained behavior anywhere.

## Plots (in `plots/`, ~150 dpi)

1. `features-normal-vs-scenarios.png` — 5 features × 5 scenarios grid, scenario series vs
   normal mean ±2σ band.
2. `scores-kmeans.png` / `scores-iforest.png` / `scores-ewma.png` — score per window on the
   combined normal→attack runs (representative scenario per detector: poll-storm / off-schedule
   / exception-storm), attack segment shaded, alerts marked, normal-head reference band
   (EWMA panel log-scale — invariant breaches saturate ~1e6).
3. `ewma-control-chart.png` — EWMA control chart of `poll_gap_cv` from the (regenerated)
   selftest fixture: center/LCL/UCL from train, slow drift over the last 100 windows, alarm.
4. `alert-rates.png` — alert rate bars, scenario × detector, View 1 vs View 2 side by side.

Raw per-branch details: `reports/artifacts/*/REPORT.md` (full subagent reports),
`reports/artifacts/integration-matrix.txt` (this matrix), CSVs per scenario/detector/view.

## For the user tomorrow

1. **Merge `feat/mini-ot-lab` and `feat/anom-data`** — both clean (anom-data verdict GOOD; the
   lab branch itself went unexercised only because docker was down). Re-run the real-lab
   capture/featurize comparison on a docker-capable host before trusting simulator magnitudes
   absolutely.
2. **Close the training-seam gap before merging any detector**: either anom-data gains a
   `--warmup-normal <n>` / normal-prefix mode, or all three detectors gain
   `--train-file <normal windows.csv>`. Tonight that gap forced hand-built combined inputs and
   caused the iforest/ewma standalone zero-detection result — it will bite any real deployment.
3. **k-means**: merge once (2) is decided; also document the "input must start normal" rule in
   its README and consider a startup burn-in (window 0 = 9 reqs pushes silhouette to k=3 on
   real data).
4. **EWMA**: keep — strongest, most explainable signal. Add an `ooc` per-window column (or
   document the episode semantics prominently) so downstream consumers don't misread `alert`;
   consider persisting the selftest control chart.
5. **iforest**: weakest link — catches only off-schedule (53 %) with a clean train. Decide
   whether to retune (lower threshold-quantile, per-feature attribution) or scope it explicitly
   as a point-outlier complement before merging.
6. **off-schedule injection is weak** (cv 1.95×, window tails overlap) — strengthen it in
   anom-data or accept that cv-based coverage on individual windows will be partial.

— End of recap.
