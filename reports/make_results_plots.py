#!/usr/bin/env python3
"""Regenerate reports/plots/*.png from the offline pipeline outputs in lab/data.

Inputs (all gitignored; regenerate as shown in reports/results-kmeans.md):
    lab/data/sim-<scenario>-windows.csv      featurized scenarios (just sim-all 2)
    lab/data/results/alerts-<scenario>.csv   k-means outputs (detect_kmeans.py)
    lab/data/results/model-normal.joblib     trained model (threshold inside)
    lab/data/windows-real.csv                featurized real-lab capture

Outputs: reports/plots/*.png (~150 dpi, committed).
"""

from pathlib import Path

import joblib
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
LAB = ROOT / "lab" / "data"
OUT = ROOT / "reports" / "plots"
SCENARIOS = ["poll-storm", "write-flood", "off-schedule", "rogue-master",
             "exception-storm"]
FEATURES = ["n_req", "n_write_fc", "n_except", "n_masters",
            "poll_gap_mean_s", "poll_gap_cv"]
COLORS = plt.get_cmap("tab10").colors


def load(scenario: str) -> pd.DataFrame:
    return pd.read_csv(LAB / f"sim-{scenario}-windows.csv")


def alerts(scenario: str) -> pd.DataFrame:
    return pd.read_csv(LAB / "results" / f"alerts-{scenario}.csv")


def plot_features(normal: pd.DataFrame) -> None:
    """Scenario feature series vs the normal mean +/- 2 sigma band."""
    fig, axes = plt.subplots(2, 3, figsize=(13, 6.5), sharex=True)
    mu, sd = normal[FEATURES].mean(), normal[FEATURES].std()
    for ax, feat in zip(axes.flat, FEATURES):
        ax.fill_between(normal.index, mu[feat] - 2 * sd[feat],
                        mu[feat] + 2 * sd[feat], alpha=0.25, color="0.4",
                        label="normal ±2σ")
        for i, s in enumerate(SCENARIOS):
            df = load(s)
            ax.plot(df.index, df[feat], lw=1.0, color=COLORS[i + 1], label=s)
        ax.set_title(feat, fontsize=10)
    axes[0, 0].legend(fontsize=7, loc="upper right")
    fig.suptitle("60 s window features — scenarios vs normal baseline "
                 "(band = normal mean ±2σ)")
    fig.tight_layout()
    fig.savefig(OUT / "features-scenarios-vs-normal.png", dpi=150)
    plt.close(fig)


def plot_scores(normal: pd.DataFrame) -> None:
    """k-means anomaly score per window: normal file + each scenario file."""
    model = joblib.load(LAB / "results" / "model-normal.joblib")
    thr = float(model["threshold"])
    k = model["k"]
    panels = [("normal", normal)] + [(s, load(s)) for s in SCENARIOS]
    fig, axes = plt.subplots(2, 3, figsize=(13, 6.5), sharex=True, sharey=True)
    for ax, (name, _df) in zip(axes.flat, panels):
        a = alerts(name)
        ax.plot(a.index, a.score, lw=1.0, color=COLORS[0])
        al = a[a.alert == 1]
        ax.scatter(al.index, al.score, s=12, facecolors="none", edgecolors="r")
        ax.axhline(thr, ls="--", lw=0.9, color="0.35")
        rate = 100 * a.alert.mean()
        ax.set_title(f"{name} — {rate:.1f}% alerted", fontsize=10)
    axes[0, 0].set_ylabel("score")
    axes[1, 0].set_ylabel("score")
    for ax in axes[1]:
        ax.set_xlabel("window")
    fig.suptitle(f"k-means detector (k={k}, one model trained on normal; "
                 f"dashed = alert threshold) — score per 60 s window")
    fig.tight_layout()
    fig.savefig(OUT / "scores-kmeans.png", dpi=150)
    plt.close(fig)


def plot_alert_rates() -> None:
    fig, ax = plt.subplots(figsize=(8, 4))
    names = ["normal"] + SCENARIOS
    rates = [100 * alerts(s).alert.mean() for s in names]
    bars = ax.bar(names, rates,
                  color=["0.6"] + [COLORS[i + 1] for i in range(len(SCENARIOS))])
    ax.axhline(5, ls="--", lw=1, color="darkgoldenrod")
    ax.text(0.02, 6, "5% false-alarm budget", fontsize=8,
            color="darkgoldenrod", transform=ax.get_yaxis_transform())
    for b, r in zip(bars, rates):
        ax.text(b.get_x() + b.get_width() / 2, r + 1.5, f"{r:.1f}%",
                ha="center", fontsize=8)
    ax.set_ylabel("alert rate [% of windows]")
    ax.set_ylim(0, 112)
    ax.set_title("k-means alert rate per file (train: normal 2 h, "
                 "score: full file)")
    fig.tight_layout()
    fig.savefig(OUT / "alert-rates.png", dpi=150)
    plt.close(fig)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    normal = load("normal")
    plot_features(normal)
    plot_scores(normal)
    plot_alert_rates()
    for p in sorted(OUT.glob("*.png")):
        print(f"wrote {p.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
