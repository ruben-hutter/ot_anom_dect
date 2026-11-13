#!/usr/bin/env python3
"""EWMA control-chart detector for Modbus lab traffic windows.

The classic OT/SPC alarm layer: per-key-feature exponentially weighted moving
average with control limits learned from the training split. Catches DRIFT
(gradually worsening poll regularity, creeping write counts) that per-window
outlier scores (k-means, iforest) can miss, and is trivially explainable to
an operator ("poll_gap_cv drifted 3 sigma above baseline at 14:32").

Method
------
1. Load ``windows.csv`` (pinned schema, 60 s windows), sort chronologically,
   fill missing cells with the TRAIN mean of the feature (poll_gap_* is
   undefined for windows with <2 reads).
2. Default features = poll_gap_cv, n_write_fc, n_masters, n_except:
     - poll_gap_cv  — heartbeat regularity (jitter/off-schedule/drift)
     - n_write_fc   — operator writes are rare in a healthy lab; floods stick out
     - n_masters    — master count is an invariant; one master is "normal"
     - n_except     — protocol health; a healthy PLC almost never errors
3. Per feature: winsorize the raw series to mean +/- 10 train-sigma (a single
   huge spike then moves the chart ~4.4 chart-sigma — enough to alarm at once,
   and the chart recovers within ~4 windows instead of ringing for half an
   hour), then EWMA over the whole series (pandas ewm(span=20, adjust=False),
   warm-started on train so scored windows have no warm-up artifact).
   Control limits = mean +/- sigma * std of the EWMA series on the TRAIN split
   (estimated after a 2*span burn-in — the EWMA warm-up transient would
   otherwise shrink the observed std and tighten the limits).
   Special case: a feature that is constant in train (e.g. n_masters==1) is a
   hard invariant — its EWMA is the constant, so we chart the RAW value: any
   deviation is an immediate breach (zero-variance prior).
4. Per window: breach = max over features of (|ewma - mean|/std - sigma);
   score = max(breach, 0); "out of control" = breach > 0.
   alert = a NEW alarm on some feature chart: like a real SPC alarm panel,
   one excursion = one alarm — after alarming, a chart only re-arms once it
   has been back inside its limits for two consecutive windows (standard
   alarm-management dead time; the recovering chart hovering just below the
   limit must not re-alarm on jitter). The score column carries the ongoing
   breach magnitude for per-window comparison.
5. Write ``window_id,score,alert`` for every window; print breaches per
   feature with example window ids.

Usage
-----
    python3 lab/detect_ewma.py --in lab/data/windows.csv --out lab/data/alerts-ewma.csv
    python3 lab/detect_ewma.py --selftest

Known limitations (see reports/detect-ewma.md): EWMA smoothing means a
single small deviation (a few sigma in ONE window) may not breach by itself —
point spikes must be large or persistent; the chart needs chronological
windows; and a feature with train variance already covering the anomaly
(e.g. occasional multi-master windows in train) dilutes the invariant.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

# Pinned data contract (produced by the anom-data featurizer).
SCHEMA = [
    "window_id", "t_start", "t_end", "n_req", "n_resp", "n_except",
    "n_read_fc", "n_write_fc", "n_other_fc", "n_masters",
    "poll_gap_mean_s", "poll_gap_cv", "n_unique_write_addr",
]
# Default charted features and why (see module docstring / report).
FEATURES_DEFAULT = ["poll_gap_cv", "n_write_fc", "n_masters", "n_except"]
ALERT_HEADER = "window_id,score,alert"


# ---------------------------------------------------------------- data I/O

def sort_chronological(df: pd.DataFrame) -> pd.DataFrame:
    """Control charts need window order; sort by t_start when parseable."""
    if "t_start" not in df.columns:
        return df
    t = pd.to_numeric(df["t_start"], errors="coerce")
    if t.notna().mean() > 0.9:
        return df.assign(_t=t).sort_values("_t", kind="stable").drop(columns="_t").reset_index(drop=True)
    print("warning: t_start not numeric; assuming input rows are chronological", file=sys.stderr)
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
    sigma: float,
    span: int,
    winsor_sigma: float = 10.0,
) -> tuple[pd.DataFrame, dict]:
    """Return (alerts_df with window_id/score/alert, info dict with internals)."""
    n_train = int(len(df) * train_fraction)
    if not 0 < train_fraction < 1 or n_train < 10:
        raise SystemExit(f"error: --train-fraction {train_fraction} leaves <10 train windows for {len(df)} rows")

    x = df[features].apply(pd.to_numeric, errors="coerce")
    x = x.fillna(x.iloc[:n_train].mean())  # undefined poll_gap_* => neutral

    per_feature: dict[str, dict] = {}
    breach = np.full(len(df), -np.inf)
    for f in features:
        raw_mu = float(x[f].iloc[:n_train].mean())
        raw_sd = float(x[f].iloc[:n_train].std())
        if raw_sd < 1e-9:
            # Constant in train: hard invariant. The EWMA of a constant IS the
            # constant, so chart the raw value — any deviation breaches now.
            charted = x[f]
            mode = "invariant"
        else:
            # Winsorize: clip spikes to +/- winsor_sigma train-sigma so the
            # smoothed chart alarms once, hard, and recovers quickly.
            clipped = x[f].clip(raw_mu - winsor_sigma * raw_sd, raw_mu + winsor_sigma * raw_sd)
            charted = clipped.ewm(span=span, adjust=False).mean()  # whole series
            mode = "ewma"
        # the EWMA warm-up transient (first ~2*span windows, converging from
        # its first value) shrinks the observed std and would tighten the
        # limits — estimate both after a burn-in instead
        burn = min(2 * span, n_train // 2)
        mu = float(charted.iloc[burn:n_train].mean())
        sd = float(charted.iloc[burn:n_train].std())
        eff_sd = sd if sd > 1e-9 else 1e-6
        z = (charted - mu).abs() / eff_sd
        breach = np.maximum(breach, z.values - sigma)
        per_feature[f] = {
            "mu": mu, "sd": sd, "mode": mode, "ew": charted, "z": z,
            "lcl": mu - sigma * eff_sd, "ucl": mu + sigma * eff_sd,
        }
    out_of_control = breach > 0.0  # any chart beyond its limits right now
    new_alarm = np.zeros(len(df), dtype=bool)
    for f, d in per_feature.items():
        ooc_f = (d["z"].values - sigma) > 0.0
        # latch with dead time: a chart alarms when it leaves its limits and
        # only re-arms after two consecutive windows back inside them
        edge = np.zeros(len(df), dtype=bool)
        armed, below = True, 0
        for i, o in enumerate(ooc_f):
            if armed and o:
                edge[i] = True
                armed = False
                below = 0
            elif not o:
                below += 1
                if below >= 2:
                    armed = True
            else:
                below = 0
        d["edge"] = edge
        new_alarm |= edge
    alerts = pd.DataFrame({
        "window_id": df["window_id"].values,
        "score": np.round(np.maximum(breach, 0.0), 6),
        "alert": new_alarm.astype(int),
    })
    info = {"n_train": n_train, "features": features, "per_feature": per_feature,
            "out_of_control": out_of_control, "span": span, "sigma": sigma}
    return alerts, info


def print_breaches(alerts: pd.DataFrame, info: dict) -> None:
    n_train = info["n_train"]
    print(f"\nper-feature breaches (scored region, {info['sigma']:.0f} sigma limits, span {info['span']}):")
    for f, d in info["per_feature"].items():
        z = d["z"].values[n_train:]
        ooc_ids = alerts["window_id"].values[n_train:][z > info["sigma"]]
        worst = float(np.max(z)) if len(z) else 0.0
        mode = "raw invariant" if d["mode"] == "invariant" else "EWMA"
        ids = ", ".join(str(i) for i in ooc_ids[:5])
        more = f" (+{len(ooc_ids) - 5} more)" if len(ooc_ids) > 5 else ""
        print(f"  {f:>14s} [{mode:>12s}]: {len(ooc_ids):3d} windows out of control, worst {worst:.1f} sigma: {ids}{more}")
    n_alarm = int(alerts["alert"].iloc[n_train:].sum())
    print(f"new alarms in scored region: {n_alarm}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--in", dest="infile", help="input windows.csv (pinned schema)")
    ap.add_argument("--out", dest="outfile", help="output alerts CSV (window_id,score,alert)")
    ap.add_argument("--train-fraction", type=float, default=0.5,
                    help="first fraction of windows used to fit limits (default 0.5)")
    ap.add_argument("--features", help=f"comma-separated charted features (default: {', '.join(FEATURES_DEFAULT)})")
    ap.add_argument("--sigma", type=float, default=3.0, help="control limits at mean +/- sigma*std (default 3)")
    ap.add_argument("--span", type=int, default=20, help="EWMA span in windows (default 20)")
    ap.add_argument("--winsor-sigma", dest="winsor_sigma", type=float, default=10.0,
                    help="clip chart inputs to +/- this many train sigma (default 10)")
    ap.add_argument("--model-out", dest="model_out", help="optional path to save the learned limits (JSON)")
    ap.add_argument("--selftest", action="store_true", help="run the built-in test suite (synthetic data)")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    if not args.infile or not args.outfile:
        ap.error("--in and --out are required (or use --selftest)")
    features = [f.strip() for f in args.features.split(",")] if args.features else FEATURES_DEFAULT
    df = load_windows(args.infile, features)

    alerts, info = detect(df, features, args.train_fraction, args.sigma, args.span, args.winsor_sigma)
    alerts.to_csv(args.outfile, index=False)
    with open(args.outfile) as fh:
        assert fh.readline().strip() == ALERT_HEADER

    if args.model_out:
        limits = {
            f: {"mode": d["mode"], "mean": round(d["mu"], 6),
                "lcl": round(d["lcl"], 6), "ucl": round(d["ucl"], 6),
                "ewma_std": round(d["sd"], 6)}
            for f, d in info["per_feature"].items()
        }
        Path(args.model_out).write_text(json.dumps(
            {"features": info["features"], "span": info["span"], "sigma": info["sigma"],
             "train_fraction": args.train_fraction, "limits": limits}, indent=2) + "\n")
        print(f"limits saved: {args.model_out}")

    n_alarm = int(alerts["alert"].sum())
    print(f"{len(alerts)} windows scored ({info['n_train']} train) on {len(info['features'])} features; "
          f"{n_alarm} new alarms -> {args.outfile}")
    print_breaches(alerts, info)
    return 0


# ------------------------------------------------------- synthetic windows
# Test fixture shared verbatim with detect_iforest.py so both selftests run
# on identical data. Models the pinned lab baseline (mini-ot-lab): one HMI
# master, FC4 poll every 2 s (+FC3 every 10th cycle) => ~33 reads/window, rare
# FC6 operator writes, almost no exceptions; very regular poll heartbeat.

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
    print(f"=== detect_ewma selftest (synthetic windows, seed {seed}) ===")
    df, labels = make_synthetic(seed)
    n_train, drift_ids = labels["n_train"], labels["drift_ids"]

    alerts, info = detect(df, FEATURES_DEFAULT, 0.5, 3.0, 20)
    is_point = alerts["window_id"].isin(labels["point_ids"]).values
    is_drift = alerts["window_id"].isin(drift_ids).values
    scored = np.arange(len(alerts)) >= n_train
    clean = scored & ~is_point & ~is_drift

    fp = int(alerts.loc[clean, "alert"].sum())
    normal_rate = fp / clean.sum()
    recall = alerts.loc[is_point, "alert"].mean()
    drift_ooc = info["out_of_control"][np.arange(len(alerts))][is_drift]
    drift_new = int(alerts.loc[is_drift, "alert"].sum())
    drift_frac = drift_ooc.mean()
    drift_tail = drift_ooc[-30:].mean()

    print(f"\nwindows: {len(df)} ({n_train} train / {len(df) - n_train} scored), "
          f"{len(labels['point_ids'])} point anomalies, {len(drift_ids)} drift windows")
    print("confusion (scored region only):")
    print(f"  normal windows : {int(clean.sum()):4d}  false alarms: {fp:3d}  ({normal_rate:.2%})")
    print(f"  anomalies      : {int(is_point.sum()):4d}  alarmed   : {int(alerts.loc[is_point, 'alert'].sum()):3d}  ({recall:.2%})")
    print("  per type:")
    for t in sorted(set(labels["type_by_id"].values())):
        ids = [i for i, tt in labels["type_by_id"].items() if tt == t]
        hit = int(alerts.loc[alerts["window_id"].isin(ids), "alert"].sum())
        print(f"    {t:>16s}: {hit}/{len(ids)}")
    print(f"  drift segment  : {drift_frac:.0%} of {len(drift_ids)} windows out of control, "
          f"{drift_new} new alarm(s), last 30 windows {drift_tail:.0%}")

    checks = [
        (normal_rate < 0.05, f"normal alarm rate {normal_rate:.2%} must be < 5%"),
        (recall > 0.80, f"point-anomaly recall {recall:.2%} must be > 80%"),
        (drift_frac >= 0.50, f"drift coverage {drift_frac:.0%} must be >= 50%"),
        (drift_tail >= 0.90, f"drift tail coverage {drift_tail:.0%} must be >= 90%"),
        (drift_new >= 1, "the drift segment must raise at least one new alarm"),
    ]
    for ok, msg in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {msg}")
        if not ok:
            return 1

    # end-to-end: schema-exact windows.csv in, alerts CSV out, limits saved
    with tempfile.TemporaryDirectory() as td:
        wcsv, acsv, mout = Path(td) / "windows.csv", Path(td) / "alerts.csv", Path(td) / "limits.json"
        df.to_csv(wcsv, index=False)
        assert main(["--in", str(wcsv), "--out", str(acsv), "--model-out", str(mout)]) == 0
        lines = acsv.read_text().splitlines()
        assert lines[0] == ALERT_HEADER, f"bad header: {lines[0]!r}"
        assert len(lines) == len(df) + 1, "row count mismatch"
        for line in lines[1:]:
            _, s, a = line.split(",")
            float(s)
            assert a in ("0", "1"), f"bad alert value {a!r}"
        saved = json.loads(mout.read_text())
        assert saved["limits"]["n_masters"]["mode"] == "invariant"
    print("[PASS] end-to-end CLI run: schema-exact alerts CSV + limits JSON")
    print("=== selftest OK ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
