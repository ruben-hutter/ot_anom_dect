#!/usr/bin/env python3
"""Night-test integration analysis + plots (test/anom-night-2026-11-13).

Inputs: .night/artifacts/{anom-data,anom-kmeans,anom-methods} (read-only)
Outputs: plots/*.png, integration matrix to stdout + reports/artifacts/
"""
import json
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ART = "/data/ot_anom_dect/.night/artifacts"
OUT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "plots"))
RA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "artifacts")
os.makedirs(OUT, exist_ok=True)
os.makedirs(RA, exist_ok=True)

SCENARIOS = ["poll-storm", "write-flood", "off-schedule", "rogue-master", "exception-storm"]
DETECTORS = ["kmeans", "iforest", "ewma"]
DCOLOR = {"kmeans": "#1f77b4", "iforest": "#2ca02c", "ewma": "#d62728"}

win = {s: pd.read_csv(f"{ART}/anom-data/{s}/windows.csv") for s in ["normal"] + SCENARIOS}
N = len(win["normal"])  # 121
ATTACK_START = N  # combined files: first N rows are normal


def tail_head(df):
    """Split a combined alerts df into (normal head, scenario tail)."""
    return df[df.window_id < N], df[df.window_id >= N]


# ---------------------------------------------------------------- integration
alerts = {}  # (view, detector, scenario) -> df
# View 1 "standalone": detector run on the scenario file alone
for det, sub in [("iforest", "iforest"), ("ewma", "ewma")]:
    for s in SCENARIOS:
        alerts[("standalone", det, s)] = pd.read_csv(f"{ART}/anom-methods/{sub}/{s}.alerts.csv")
alerts[("standalone", "kmeans", "normal-only-run")] = pd.read_csv(f"{ART}/anom-kmeans/normal/alerts-kmeans.csv")

# View 2 "combined": normal head (121 rows) + scenario tail (121 rows)
for s in SCENARIOS:
    alerts[("combined", "kmeans", s)] = pd.read_csv(f"{ART}/anom-kmeans/combined/alerts-{s}-full.csv")
    alerts[("combined", "iforest", s)] = pd.read_csv(f"{ART}/anom-methods/combined-view/iforest-{s}.alerts.csv")
    alerts[("combined", "ewma", s)] = pd.read_csv(f"{ART}/anom-methods/combined-view/ewma-{s}.alerts.csv")

lines = []

def emit(s=""):
    print(s)
    lines.append(s)


emit("# Integration matrix — night test 2026-11-13")
emit(f"windows per file: {N} (combined files: {2*N}, attack starts at window {ATTACK_START})")
emit("NOTE ewma 'alert' column marks new-alarm episodes (onset), kmeans/iforest per-window flags.")
emit()
emit("## View 1 — standalone: detector run on scenario windows.csv alone")
emit("(kmeans cannot run this shape: it trains on the file head, which would be attack traffic; N/A)")
emit()
emit("| scenario | iforest n_alerts/rate | ewma n_alerts(rat. ep.)/rate | kmeans |")
emit("|---|---|---|---|")
for s in SCENARIOS:
    fi = alerts[("standalone", "iforest", s)]
    fe = alerts[("standalone", "ewma", s)]
    # iforest/ewma self-train on first 60 windows -> score the last 61
    fis = fi.iloc[60:]
    fes = fe.iloc[60:]
    emit(f"| {s} | {int(fis.alert.sum())}/{fis.alert.mean()*100:.1f}% | "
         f"{int(fes.alert.sum())}/{fes.alert.mean()*100:.1f}% | N/A (poisoned train) |")

emit()
emit("## View 2 — combined: normal head (121) + scenario tail (121); train split = normal head")
emit()
emit("| scenario | kmeans tail | iforest tail | ewma tail | kmeans head | iforest head | ewma head |")
emit("|---|---|---|---|---|---|---|")
for s in SCENARIOS:
    row = [s]
    for det in DETECTORS:
        h, t = tail_head(alerts[("combined", det, s)])
        row.append(f"{int(t.alert.sum())}/{t.alert.mean()*100:.0f}%")
    for det in DETECTORS:
        h, t = tail_head(alerts[("combined", det, s)])
        row.append(f"{int(h.alert.sum())}/{h.alert.mean()*100:.0f}%")
    emit("| " + " | ".join(row) + " |")

emit()
emit("Normal baselines: kmeans normal-only run "
     f"{int(alerts[('standalone','kmeans','normal-only-run')].alert.sum())}/{N} = "
     f"{alerts[('standalone','kmeans','normal-only-run')].alert.mean()*100:.2f}% alerts; "
     "iforest/ewma normal-only scored region 0/61 (protocol C report).")
emit()
emit("## mean tail score (combined view) + EWMA out-of-control windows (score>0)")
emit("| scenario | kmeans | iforest | ewma | ewma tail OOC rate |")
emit("|---|---|---|---|---|")
for s in SCENARIOS:
    row = [s]
    for det in DETECTORS:
        _, t = tail_head(alerts[("combined", det, s)])
        row.append(f"{t.score.mean():.3f}")
    _, te = tail_head(alerts[("combined", "ewma", s)])
    row.append(f"{(te.score > 0).mean()*100:.0f}%")
    emit("| " + " | ".join(row) + " |")

with open(os.path.join(RA, "integration-matrix.txt"), "w") as f:
    f.write("\n".join(lines) + "\n")

# ---------------------------------------------------------------- plot 1: features grid
FEATURES = ["poll_gap_mean_s", "n_write_fc", "n_masters", "poll_gap_cv", "n_except"]
fig, axes = plt.subplots(len(FEATURES), len(SCENARIOS), figsize=(3.0 * len(SCENARIOS), 2.2 * len(FEATURES)),
                         sharex=True)
tmin = win["normal"].t_start.min()
for c, s in enumerate(SCENARIOS):
    for r, feat in enumerate(FEATURES):
        ax = axes[r, c]
        n = win["normal"]
        x = (n.t_start - tmin) / 60.0
        ax.fill_between(x, n[feat].mean() - 2 * n[feat].std(), n[feat].mean() + 2 * n[feat].std(),
                        color="#88c0d0", alpha=0.35, label="normal ±2σ")
        ax.axhline(n[feat].mean(), color="#5e81ac", lw=1.2, ls="--", label="normal mean")
        xs = (win[s].t_start - tmin) / 60.0
        ax.plot(xs, win[s][feat], color="#bf616a", lw=1.0, label=s)
        if r == 0:
            ax.set_title(s, fontsize=11)
        if c == 0:
            ax.set_ylabel(feat, fontsize=9)
        if r == len(FEATURES) - 1:
            ax.set_xlabel("minutes", fontsize=8)
        ax.tick_params(labelsize=7)
handles, labels = axes[0, 0].get_legend_handles_labels()
fig.legend(handles, labels, loc="upper center", ncol=3, fontsize=9)
fig.suptitle("Feature time series: scenario vs normal band (2 h simulation, 60 s windows)", fontsize=13)
fig.tight_layout(rect=[0, 0, 1, 0.95])
fig.savefig(f"{OUT}/features-normal-vs-scenarios.png", dpi=150)
plt.close(fig)

# ---------------------------------------------------------------- plot 2: score series per detector
rep = {}
for det in DETECTORS:
    best = max(SCENARIOS, key=lambda s: (int(tail_head(alerts[("combined", det, s)])[1].alert.sum()),
                                         tail_head(alerts[("combined", det, s)])[1].score.mean()))
    rep[det] = best
emit()
emit(f"Representative scenarios for score plots: {rep}")

for det in DETECTORS:
    s = rep[det]
    df = alerts[("combined", det, s)]
    fig, ax = plt.subplots(figsize=(9, 4.5))
    if det == "ewma":  # scores span 0..1e7 (invariant breaches saturate)
        ax.set_yscale("symlog", linthresh=1)
    h, t = tail_head(df)
    ax.plot(df.window_id, df.score, color=DCOLOR[det], lw=1.1, label=f"score ({s}, combined run)")
    mu, sd = h.score.mean(), max(h.score.std(), 1e-9)
    ax.axhspan(mu - 3 * sd, mu + 3 * sd, color="#88c0d0", alpha=0.25, label="normal-head ±3σ")
    ax.axhline(mu, color="#5e81ac", ls="--", lw=1.2, label="normal-head mean")
    ax.axvline(ATTACK_START - 0.5, color="#a3be8c", lw=1.5, ls=":")
    ax.axvspan(ATTACK_START - 0.5, 2 * N - 1, color="#d08770", alpha=0.10, label="attack segment")
    al = df[df.alert == 1]
    ax.scatter(al.window_id, al.score, s=26, facecolor="none", edgecolor="#d62728", lw=1.4,
               zorder=5, label=f"alerts (n={len(al)}, tail n={int(t.alert.sum())})")
    ax.set_xlabel("window_id (attack starts at 121)")
    ax.set_ylabel("anomaly score")
    ax.set_title(f"{det} — combined run: normal (0–120) + {s} (121–241)")
    ax.legend(fontsize=8, loc="best")
    fig.tight_layout()
    fig.savefig(f"{OUT}/scores-{det}.png", dpi=150)
    plt.close(fig)

# ---------------------------------------------------------------- plot 3: EWMA control chart
cc = pd.read_csv(f"{ART}/anom-methods/selftest/ewma-selftest-control-chart.csv")
meta = json.load(open(f"{ART}/anom-methods/selftest/ewma-selftest-control-chart-meta.json"))
drift = meta["drift_window_ids"]
cc = cc[cc.feature == "poll_gap_cv"].reset_index(drop=True)
fig, ax = plt.subplots(figsize=(10, 4.5))
ax.plot(cc.window_id, cc.raw, color="#b48ead", lw=0.7, alpha=0.6, label="raw poll_gap_cv")
ax.plot(cc.window_id, cc.ewma, color="#5e81ac", lw=1.5, label="EWMA (span 20)")
ax.plot(cc.window_id, cc.center_mean, color="#4c566a", ls="--", lw=1.0, label="center (train mean)")
ax.plot(cc.window_id, cc.lcl, color="#bf616a", ls=":", lw=1.2, label="LCL")
ax.plot(cc.window_id, cc.ucl, color="#bf616a", ls=":", lw=1.2, label="UCL")
d0, d1 = min(drift), max(drift)
ax.axvspan(d0, d1, color="#d08770", alpha=0.12, label=f"drift segment (windows {d0}–{d1})")
al = cc[cc.new_alarm == 1]
ax.scatter(al.window_id, al.loc[al.new_alarm == 1, "ewma"], s=60, facecolor="none",
           edgecolor="#d62728", lw=1.8, zorder=5, label=f"new alarms (n={len(al)})")
ax.set_xlabel("window_id")
ax.set_ylabel("poll_gap_cv")
ax.set_title("EWMA control chart — selftest fixture: slow poll_gap_cv drift over last 100 windows")
ax.legend(fontsize=8, loc="upper left")
fig.tight_layout()
fig.savefig(f"{OUT}/ewma-control-chart.png", dpi=150)
plt.close(fig)

# ---------------------------------------------------------------- plot 4: alert-rate bars
fig, axes = plt.subplots(1, 2, figsize=(13, 4.6), sharey=True)
x = np.arange(len(SCENARIOS) + 1)
labels = ["normal"] + SCENARIOS
w = 0.26
for ax, view, title in [(axes[0], "standalone", "View 1: detector on scenario file alone (attack from window 0)"),
                        (axes[1], "combined", "View 2: combined normal head + scenario tail")]:
    for i, det in enumerate(DETECTORS):
        rates = []
        if det == "kmeans":
            if view == "standalone":
                nrm = alerts[("standalone", "kmeans", "normal-only-run")].alert.mean() * 100
                rates = [nrm] + [np.nan] * len(SCENARIOS)
            else:
                for s in SCENARIOS:
                    _, t = tail_head(alerts[("combined", det, s)])
                    rates.append(t.alert.mean() * 100)
                # normal head rates for reference bar
                hrate = np.mean([tail_head(alerts[("combined", det, s)])[0].alert.mean() * 100 for s in SCENARIOS])
                rates = [hrate] + rates
        else:
            for s in SCENARIOS:
                if view == "standalone":
                    rates.append(alerts[("standalone", det, s)].iloc[60:].alert.mean() * 100)
                else:
                    _, t = tail_head(alerts[("combined", det, s)])
                    rates.append(t.alert.mean() * 100)
            if view == "standalone":
                rates = [0.0] + rates  # scored-region 0/61 per protocol C
            else:
                hrate = np.mean([tail_head(alerts[("combined", det, s)])[0].alert.mean() * 100 for s in SCENARIOS])
                rates = [hrate] + rates
        ax.bar(x + (i - 1) * w, rates, w, color=DCOLOR[det], label=det)
    ax.axhline(5, color="#a3be8c", ls="--", lw=1.0)
    ax.text(0.02, 6.5, "5% false-alarm budget", fontsize=8, color="#a3be8c")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=20, ha="right", fontsize=8)
    ax.set_title(title, fontsize=10)
    ax.set_ylim(0, 105)
axes[0].set_ylabel("alert rate [%]")
axes[0].legend(fontsize=9)
fig.suptitle("Alert rate by scenario × detector (night test 2026-11-13)", fontsize=12)
fig.tight_layout(rect=[0, 0, 1, 0.94])
fig.savefig(f"{OUT}/alert-rates.png", dpi=150)
plt.close(fig)

emit()
emit(f"Wrote plots to {OUT}: " + ", ".join(sorted(os.listdir(OUT))))
print("\n".join(lines[int(np.argmax([l.startswith("## View 1") for l in lines])):] if False else []))
