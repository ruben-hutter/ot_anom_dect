#!/usr/bin/env python3
"""Isolation Forest detector for Modbus lab traffic windows.

Interpretable tree-ensemble complement to the k-means detector: no spherical
cluster assumption, handles feature interactions, fully offline (sklearn only).

Method
------
1. Load ``windows.csv`` (pinned schema, 60 s windows), keep the numeric
   feature columns, fill missing cells with the TRAIN mean of the feature
   (poll_gap_* is undefined for windows with <2 reads).
2. Standardize features with a StandardScaler fitted on the training split
   only (first ``--train-fraction`` of the file, chronologically).
3. Fit sklearn IsolationForest on the standardized training split
   (n_estimators=200, contamination="auto", max_samples=full train).
   ``contamination`` is irrelevant for scoring; we set our own threshold.
   ``max_samples`` is deliberately the full training split (capped at 4096):
   sklearn's default 256-point subsamples starve the tails on small window
   counts and let ordinary edge-of-range windows score like true outliers.
4. score = -score_samples (higher = more anomalous). Alert threshold =
   ``--threshold-quantile`` quantile (default 0.99) of the TRAIN scores, so
   by construction only ~1% of normal train windows would alert.
5. Write ``window_id,score,alert`` for every window and print the top-10
   offenders with their dominant per-feature z-scores.

Output rows cover ALL windows (train rows are in-sample and optimistically
normal); evaluation should use the scored/test half — the --selftest does.

Usage
-----
    python3 lab/detect_iforest.py --in lab/data/windows.csv --out lab/data/alerts-iforest.csv
    python3 lab/detect_iforest.py --selftest

Known limitation (by design, see reports/detect-iforest.md): a feature that
is constant in train (e.g. n_masters==1) is never split by the fitted trees,
so IsolationForest is blind to it; an anomaly that shows in exactly ONE
feature with everything else typical also cannot clear a train-quantile
threshold; gradual drift escapes a point-outlier detector. All three are the
EWMA detector's job.
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

# Pinned data contract (produced by the anom-data featurizer).
SCHEMA = [
    "window_id", "t_start", "t_end", "n_req", "n_resp", "n_except",
    "n_read_fc", "n_write_fc", "n_other_fc", "n_masters",
    "poll_gap_mean_s", "poll_gap_cv", "n_unique_write_addr",
]
# All numeric features of the schema (everything except ids/timestamps).
FEATURES_ALL = [
    "n_req", "n_resp", "n_except", "n_read_fc", "n_write_fc", "n_other_fc",
    "n_masters", "poll_gap_mean_s", "poll_gap_cv", "n_unique_write_addr",
]
ALERT_HEADER = "window_id,score,alert"


# ---------------------------------------------------------------- data I/O

def sort_chronological(df: pd.DataFrame) -> pd.DataFrame:
    """EWMA-style detectors need window order; sort by t_start when parseable."""
    if "t_start" not in df.columns:
        return df
    t = pd.to_numeric(df["t_start"], errors="coerce")
    if t.notna().mean() > 0.9:
        return df.assign(_t=t).sort_values("_t", kind="stable").drop(columns="_t").reset_index(drop=True)
    return df


def load_windows(path: str, features: list[str]) -> pd.DataFrame:
    df = pd.read_csv(path)
    missing = [c for c in ["window_id", *features] if c not in df.columns]
    if missing:
        raise SystemExit(f"error: {path} is missing required columns: {', '.join(missing)}")
    return sort_chronological(df)


# ---------------------------------------------------------------- detection

def detect(
    df: pd.DataFrame,
    features: list[str],
    train_fraction: float,
    threshold_quantile: float,
    seed: int,
) -> tuple[pd.DataFrame, dict]:
    """Return (alerts_df with window_id/score/alert, info dict with internals)."""
    n_train = int(len(df) * train_fraction)
    if not 0 < train_fraction < 1 or n_train < 10:
        raise SystemExit(f"error: --train-fraction {train_fraction} leaves <10 train windows for {len(df)} rows")

    x = df[features].apply(pd.to_numeric, errors="coerce")
    train_x = x.iloc[:n_train]
    drop = [c for c in features if train_x[c].notna().sum() == 0]
    if drop:
        print(f"warning: features all-empty in train, dropped: {', '.join(drop)}", file=sys.stderr)
        features = [c for c in features if c not in drop]
        x = x[features]
        train_x = x.iloc[:n_train]
    x = x.fillna(train_x.mean())  # missing poll_gap_* => neutral z=0

    scaler = StandardScaler().fit(train_x)
    z = scaler.transform(x)  # z-scores from TRAIN stats

    forest = IsolationForest(
        n_estimators=200, contamination="auto",
        max_samples=min(n_train, 4096), random_state=seed,
    )
    forest.fit(z[:n_train])
    score = -forest.score_samples(z)  # higher = more anomalous
    threshold = float(np.quantile(score[:n_train], threshold_quantile))

    alerts = pd.DataFrame({
        "window_id": df["window_id"].values,
        "score": np.round(score, 6),
        "alert": (score > threshold).astype(int),
    })
    info = {
        "n_train": n_train, "features": features, "scaler": scaler,
        "model": forest, "threshold": threshold, "z": z,
    }
    return alerts, info


def print_top_offenders(alerts: pd.DataFrame, info: dict, top: int = 10) -> None:
    z, feats, n_train = info["z"], info["features"], info["n_train"]
    scored = alerts.iloc[n_train:]  # train rows are in-sample; skip them
    idx = scored["score"].nlargest(top).index
    print(f"\ntop-{top} offenders (score = -score_samples, dominant |z| from train stats):")
    for i in idx:
        row = z[i]
        order = np.argsort(-np.abs(row))[:3]
        dom = ", ".join(f"{feats[j]}={row[j]:+.1f}" for j in order)
        a = " ALERT" if alerts.loc[i, "alert"] else ""
        print(f"  window {alerts.loc[i, 'window_id']}: score {alerts.loc[i, 'score']:.4f}{a} | {dom}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--in", dest="infile", help="input windows.csv (pinned schema)")
    ap.add_argument("--out", dest="outfile", help="output alerts CSV (window_id,score,alert)")
    ap.add_argument("--train-fraction", type=float, default=0.5,
                    help="first fraction of windows used to fit (default 0.5)")
    ap.add_argument("--features", help=f"comma-separated feature columns (default: {', '.join(FEATURES_ALL)})")
    ap.add_argument("--threshold-quantile", type=float, default=0.99,
                    help="alert threshold = this quantile of TRAIN scores (default 0.99)")
    ap.add_argument("--model-out", dest="model_out", help="optional path to save the fitted model (joblib)")
    ap.add_argument("--seed", type=int, default=42, help="IsolationForest random_state (default 42)")
    ap.add_argument("--selftest", action="store_true", help="run the built-in test suite (synthetic data)")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    if not args.infile or not args.outfile:
        ap.error("--in and --out are required (or use --selftest)")
    features = [f.strip() for f in args.features.split(",")] if args.features else FEATURES_ALL
    df = load_windows(args.infile, features)

    alerts, info = detect(df, features, args.train_fraction, args.threshold_quantile, args.seed)
    alerts.to_csv(args.outfile, index=False)
    with open(args.outfile) as fh:
        assert fh.readline().strip() == ALERT_HEADER

    if args.model_out:
        joblib.dump({
            "model": info["model"], "scaler": info["scaler"],
            "features": info["features"], "threshold": info["threshold"],
            "train_fraction": args.train_fraction, "seed": args.seed,
        }, args.model_out)
        print(f"model saved: {args.model_out}")

    n_alert = int(alerts["alert"].sum())
    print(f"{len(alerts)} windows scored ({info['n_train']} train) on {len(info['features'])} features; "
          f"threshold {info['threshold']:.4f} (q{args.threshold_quantile} of train); {n_alert} alerts "
          f"-> {args.outfile}")
    print_top_offenders(alerts, info)
    return 0


# ------------------------------------------------------- synthetic windows
# Test fixture shared verbatim with detect_ewma.py so both selftests run on
# identical data. Models the pinned lab baseline (mini-ot-lab): one HMI master,
# FC4 poll every 2 s (+FC3 every 10th cycle) => ~33 reads/window, rare FC6
# operator writes, almost no exceptions; very regular poll heartbeat.

def make_synthetic(seed: int = 42, n_normal: int = 2000, n_drift: int = 100) -> tuple[pd.DataFrame, dict]:
    rng = np.random.default_rng(seed)
    n = n_normal

    # "normal" = the pinned lab heartbeat: ~33 reads/window (FC4 every 2 s +
    # FC3 every 10th cycle), a rare single FC6 operator write, the very
    # occasional exception / odd function code, one master, tight poll stats.
    n_read = np.clip(rng.normal(33, 1.2, n).round(), 30, 36).astype(int)
    n_write = (rng.random(n) < 0.03).astype(int)  # single FC6 write or none
    n_except = rng.poisson(0.02, n)               # healthy PLC: almost never
    n_other = rng.poisson(0.02, n)                # lab only ever uses FC3/4/6
    n_masters = np.ones(n, dtype=int)
    poll_mean = rng.normal(1.98, 0.04, n)
    poll_cv = np.clip(rng.normal(0.03, 0.01, n), 0.005, 0.08)
    n_uniq_wr = n_write.copy()  # operator writes always hit hr[0]

    # drift tail: normal windows except poll_gap_cv ramping 0.05 -> 0.4
    n_read = np.concatenate([n_read, np.clip(rng.normal(33, 1.2, n_drift).round(), 30, 36).astype(int)])
    n_write = np.concatenate([n_write, (rng.random(n_drift) < 0.03).astype(int)])
    n_except = np.concatenate([n_except, rng.poisson(0.02, n_drift)])
    n_other = np.concatenate([n_other, rng.poisson(0.02, n_drift)])
    n_masters = np.concatenate([n_masters, np.ones(n_drift, dtype=int)])
    poll_mean = np.concatenate([poll_mean, rng.normal(1.98, 0.04, n_drift)])
    poll_cv = np.concatenate([poll_cv, np.linspace(0.05, 0.4, n_drift)])
    n_uniq_wr = np.concatenate([n_uniq_wr, n_write[n:]])
    total = n_normal + n_drift

    n_req = n_read + n_write + n_other
    df = pd.DataFrame({
        "window_id": np.arange(total, dtype=int),
        "t_start": np.arange(total, dtype=int) * 60,
        "t_end": (np.arange(total, dtype=int) + 1) * 60,
        "n_req": n_req, "n_resp": np.maximum(n_req - n_except, 0), "n_except": n_except,
        "n_read_fc": n_read, "n_write_fc": n_write, "n_other_fc": n_other,
        "n_masters": n_masters, "poll_gap_mean_s": poll_mean.round(4),
        "poll_gap_cv": poll_cv.round(4), "n_unique_write_addr": n_uniq_wr,
    })

    n_train = int(total * 0.5)
    # inject 100 point anomalies (5 types x 20) into the SCORED region only,
    # >=8 windows apart (strided grid with random phase, then random subset)
    lo, hi = n_train + 50, n_normal - 40
    grid = np.arange(lo + int(rng.integers(0, 8)), hi, 8)
    chosen = sorted(rng.choice(grid, size=100, replace=False).tolist())
    assert len(chosen) == 100 and all(b - a >= 8 for a, b in zip(chosen, chosen[1:]))

    # scenario types round-robin over the sorted grid (with a random phase):
    # same-type anomalies land ~40 windows apart, like separate attack
    # episodes — clustered same-type spikes would just re-breach an already
    # out-of-control chart, which measures the fixture, not the detectors
    types_base = ["poll-storm", "write-flood", "rogue-master", "off-schedule", "exception-storm"]
    phase = int(rng.integers(0, 5))
    types = np.array([types_base[(k + phase) % 5] for k in range(100)])
    type_by_id = {}
    for i, t in zip(chosen, types):
        type_by_id[i] = t
        if t == "poll-storm":       # 10x faster polling, bursty not metronomic
            df.loc[i, "poll_gap_mean_s"] = round(df.loc[i, "poll_gap_mean_s"] / 10, 4)
            df.loc[i, "poll_gap_cv"] = round(float(rng.uniform(0.15, 0.35)), 4)
            df.loc[i, "n_read_fc"] = int(rng.integers(300, 341))
        elif t == "write-flood":    # attacker hammering writes, some rejected
            df.loc[i, "n_write_fc"] = int(rng.integers(20, 31))
            df.loc[i, "n_unique_write_addr"] = int(rng.integers(2, 6))
            df.loc[i, "n_except"] = int(rng.integers(3, 9))
        elif t == "rogue-master":   # second master joins + its own polls
            df.loc[i, "n_masters"] = 2
            df.loc[i, "n_read_fc"] = df.loc[i, "n_read_fc"] + int(rng.integers(10, 31))
            df.loc[i, "poll_gap_mean_s"] = round(df.loc[i, "poll_gap_mean_s"] * 0.85, 4)
            df.loc[i, "poll_gap_cv"] = round(df.loc[i, "poll_gap_cv"] * 2.5, 4)
        elif t == "off-schedule":   # jittery poller: gaps drift, polls missed
            df.loc[i, "poll_gap_cv"] = round(float(rng.uniform(0.5, 0.9)), 4)
            df.loc[i, "poll_gap_mean_s"] = round(df.loc[i, "poll_gap_mean_s"] * float(rng.uniform(1.15, 1.35)), 4)
            df.loc[i, "n_read_fc"] = max(df.loc[i, "n_read_fc"] - int(rng.integers(3, 9)), 20)
        elif t == "exception-storm":  # PLC erroring; HMI retries inflate reads
            exc = int(rng.integers(15, 41))
            df.loc[i, "n_except"] = exc
            df.loc[i, "n_read_fc"] = df.loc[i, "n_read_fc"] + exc // 2
        df.loc[i, "n_req"] = df.loc[i, "n_read_fc"] + df.loc[i, "n_write_fc"] + df.loc[i, "n_other_fc"]
        df.loc[i, "n_resp"] = max(df.loc[i, "n_req"] - df.loc[i, "n_except"], 0)

    labels = {
        "n_train": n_train,
        "point_ids": chosen,
        "type_by_id": type_by_id,
        "drift_ids": list(range(n_normal, total)),
    }
    return df, labels


# ---------------------------------------------------------------- selftest

def selftest(seed: int = 42) -> int:
    print(f"=== detect_iforest selftest (synthetic windows, seed {seed}) ===")
    df, labels = make_synthetic(seed)
    n_train, drift_ids = labels["n_train"], labels["drift_ids"]

    alerts, info = detect(df, FEATURES_ALL, 0.5, 0.99, seed)
    is_point = alerts["window_id"].isin(labels["point_ids"]).values
    is_drift = alerts["window_id"].isin(drift_ids).values
    scored = np.arange(len(alerts)) >= n_train
    clean = scored & ~is_point & ~is_drift

    fp = int(alerts.loc[clean, "alert"].sum())
    normal_rate = fp / clean.sum()
    recall = alerts.loc[is_point, "alert"].mean()
    drift_flagged = int(alerts.loc[is_drift, "alert"].sum())

    print(f"\nwindows: {len(df)} ({n_train} train / {len(df) - n_train} scored), "
          f"{len(labels['point_ids'])} point anomalies, {len(drift_ids)} drift windows")
    print("confusion (scored region only):")
    print(f"  normal windows : {int(clean.sum()):4d}  false alerts: {fp:3d}  ({normal_rate:.2%})")
    print(f"  anomalies      : {int(is_point.sum()):4d}  detected   : {int(alerts.loc[is_point, 'alert'].sum()):3d}  ({recall:.2%})")
    print("  per type:")
    for t in sorted(set(labels["type_by_id"].values())):
        ids = [i for i, tt in labels["type_by_id"].items() if tt == t]
        hit = int(alerts.loc[alerts["window_id"].isin(ids), "alert"].sum())
        print(f"    {t:>16s}: {hit}/{len(ids)}")
    print(f"  drift segment  : {drift_flagged}/{len(drift_ids)} flagged "
          f"(iforest is a point detector; the ramp tail only — EWMA's job is the drift)")

    checks = [
        (normal_rate < 0.05, f"normal alert rate {normal_rate:.2%} must be < 5%"),
        (recall > 0.80, f"point-anomaly recall {recall:.2%} must be > 80%"),
    ]
    for ok, msg in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {msg}")
        if not ok:
            return 1

    # end-to-end: schema-exact windows.csv in, alerts CSV out, model saved
    with tempfile.TemporaryDirectory() as td:
        wcsv, acsv, mout = Path(td) / "windows.csv", Path(td) / "alerts.csv", Path(td) / "model.joblib"
        df.to_csv(wcsv, index=False)
        assert main(["--in", str(wcsv), "--out", str(acsv), "--model-out", str(mout)]) == 0
        lines = acsv.read_text().splitlines()
        assert lines[0] == ALERT_HEADER, f"bad header: {lines[0]!r}"
        assert len(lines) == len(df) + 1, "row count mismatch"
        for line in lines[1:]:
            _, s, a = line.split(",")
            float(s)
            assert a in ("0", "1"), f"bad alert value {a!r}"
        assert mout.exists() and joblib.load(mout)["threshold"] > 0
    print("[PASS] end-to-end CLI run: schema-exact alerts CSV + model file")
    print("=== selftest OK ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
