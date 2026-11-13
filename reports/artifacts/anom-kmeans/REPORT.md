# Protocol B — k-means detector run (anom-kmeans)

- tested-at commit: 13fbcaffcfc6e8176b363402462e147635850f13 (branch feat/anom-kmeans)
- environment: nix dev shell (`nix develop --command bash -lc`, Python 3.12.12,
  pandas 2.2.3, sklearn 1.6.1) from the worktree root
- worktree untouched (git status clean); all outputs under this directory

## Item 1 — selftest
`nix develop --command bash -lc 'python3 lab/detect_kmeans.py --selftest'` → exit 0.

```
confusion (rows = truth, cols = alert):
            alert  no-alert
  attack       98         2   recall       98.00%
  normal       29      1971   alert rate    1.45%
PASS: alert rate on normal 1.45% (<5%), recall on attacks 98.00% (>80%)
```
(chosen k=1, threshold 7.949)

## Item 2 — runs on protocol-A windows (deviation from "run each file as-is")

`detect_kmeans.py` trains on the HEAD of its input file (`--train-fraction`,
default 0.5) and the pinned contract requires the file to start with normal
traffic. Protocol-A scenario files are attack-throughout (poll-storm window 0
already has 111 reqs), so running them standalone would train the baseline on
attack windows ("poisoned training head", reports/detect-kmeans.md).

Adaptation (recorded as a deviation): per scenario, a combined input was built
in this artifacts dir —
`combined/windows-<scenario>.csv` = normal/windows.csv (head, 121 rows,
window_id/t_* verbatim) + scenario windows (tail, 121 rows; identity-only
columns adjusted: window_id += 121, t_start/t_end shifted +8040 s so the file
is time-sorted; feature columns copied verbatim — window_id/t_start/t_end are
never clustered). CLI runs:

```
# normal (standalone, defaults):
python3 lab/detect_kmeans.py \
  --in  <anom-data>/normal/windows.csv \
  --out anom-kmeans/normal/alerts-kmeans.csv \
  --model-out anom-kmeans/normal/kmeans-model.joblib

# each scenario (train head = exactly the 121 normal windows):
python3 lab/detect_kmeans.py \
  --in  anom-kmeans/combined/windows-<scenario>.csv \
  --out anom-kmeans/combined/alerts-<scenario>-full.csv \
  --model-out anom-kmeans/<scenario>/kmeans-model.joblib \
  --train-fraction 0.5
# then summarize.py splits the tail 121 rows into <scenario>/alerts-kmeans.csv
# (window_id restored to the protocol-A original ids) and keeps the normal
# section of the same run as <scenario>/alerts-normal-section.csv
```
All runs exit 0. On the real normal training head the silhouette rule picked
k=3 (train silhouette 0.826 >= 0.5; threshold 3.401) — the real capture's head
has separated regimes (e.g. window 0 with only 9 reqs), unlike the synthetic
selftest where k=1 won. The normal standalone run picked k=2 (silhouette
0.861, threshold 5.525).

## Item 3 — alert-rate table (summary.csv)

| scenario | n_windows | n_alerts | alert_rate | mean score |
|---|---|---|---|---|
| normal | 121 | 1 | 0.83% | 1.300 |
| poll-storm | 121 | 121 | 100.00% | 193.078 |
| write-flood | 121 | 121 | 100.00% | 85.743 |
| off-schedule | 121 | 121 | 100.00% | 21.694 |
| rogue-master | 121 | 121 | 100.00% | 38.193 |
| exception-storm | 121 | 120 | 99.17% | 11.101 |

- normal alert rate 0.83% <= 5%  -> PASS
- every scenario 99.17–100% vs 0.83% -> clearly elevated -> PASS
- In the combined runs the normal head section itself alerts 2/121 (1.65%)
  under the same threshold — consistent with the <=5% budget.
- exception-storm: the single non-alerted window is a low-exception window
  (n_except min = 3 per protocol-A sanity analysis) — borderline, not a failure.

## Item 3 — alerts-CSV schema check
All six `<scenario>/alerts-kmeans.csv`: header exactly `window_id,score,alert`,
121 rows, alert ∈ {0,1}, score float, window_id int -> PASS (verified by
summarize.py, output above).

## Verdict: PASS on all protocol items. No errors encountered.
