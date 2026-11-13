#!/usr/bin/env python3
"""One-class k-means anomaly detector over 60 s windows of Modbus TCP traffic.

Method (docs/primer-modbus-kmeans.md §3-4):
  windows.csv -> z-scored features -> k-means fit on NORMAL windows only ->
  score = Euclidean distance to the nearest centroid ->
  alert when score > quantile of the TRAINING scores (one-class threshold).

Input is the pinned windows schema (produced by the anom-data pipeline):

    window_id,t_start,t_end,n_req,n_resp,n_except,n_read_fc,n_write_fc,
    n_other_fc,n_masters,poll_gap_mean_s,poll_gap_cv,n_unique_write_addr

  - t_* are identity/time columns and never enter clustering.
  - poll_gap_* may be empty (undefined when a window has < 2 read requests);
    such NaNs are imputed with the TRAINING-split median of that column.
  - The file is assumed sorted by time and to start with the normal capture,
    so the training split is simply the head of the file (first --train-fraction
    of the rows, in file order).

Output is the shared alerts schema all detectors emit:

    window_id,score,alert        (score = distance to nearest centroid, alert 0/1)

k selection (training split only): k = 1..kmax (default 3), scored by mean
silhouette on the training split; the best k>=2 wins only if its silhouette
reaches 0.5 (genuinely separated regimes), otherwise the model falls back to
k=1 -- one-class usage where normal traffic is a single tight cluster.
Inertia for each k is printed so the elbow is visible too.

Usage:
    python3 lab/detect_kmeans.py --in lab/data/windows.csv --out lab/data/alerts-kmeans.csv
    python3 lab/detect_kmeans.py --in ... --out ... --model-out lab/data/kmeans-model.joblib
    python3 lab/detect_kmeans.py --selftest        # synthetic-data test suite (exits 0 on pass)
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score
from sklearn.preprocessing import StandardScaler

# Identity/time columns of the pinned schema: excluded from clustering.
ID_COLS = ("window_id", "t_start", "t_end")

# Pinned windows.csv header (selftest data generation and sanity checks).
CONTRACT_COLUMNS = [
    "window_id", "t_start", "t_end", "n_req", "n_resp", "n_except",
    "n_read_fc", "n_write_fc", "n_other_fc", "n_masters",
    "poll_gap_mean_s", "poll_gap_cv", "n_unique_write_addr",
]
# Default feature list: every schema column except identity/time.
DEFAULT_FEATURES = [c for c in CONTRACT_COLUMNS if c not in ID_COLS]

KMAX_DEFAULT = 3     # "k small": try k = 1..kmax on the training split
SIL_MIN = 0.5        # k>1 must earn this mean silhouette on train, else k=1
MIN_TRAIN = 10       # refuse to fit a baseline on fewer windows
WINDOW_S = 60.0      # window length, for the selftest synthetic timeline


# ---------------------------------------------------------------- features

def pick_features(df: pd.DataFrame, override: str | None) -> list[str]:
    """Feature columns to cluster on.

    Default: all columns except window_id/t_start/t_end (coerced to numeric
    later; columns that are not numeric at all get dropped there).
    Override: comma-separated list from --features.
    """
    if override:
        feats = [c.strip() for c in override.split(",") if c.strip()]
        missing = [c for c in feats if c not in df.columns]
        if missing:
            raise SystemExit(
                f"--features: no such column(s) {missing}; "
                f"file has {list(df.columns)}")
        return feats
    return [c for c in df.columns if c not in ID_COLS]


# ------------------------------------------------------------------- model

def fit_baseline(Z_train: np.ndarray, kmax: int, forced_k: int | None,
                 seed: int):
    """Fit k-means candidates on the (already standardized) training split and
    pick k. Returns (model, k, rule_string, table_of_(k, inertia, silhouette)).
    """
    ks = [k for k in range(1, kmax + 1) if k <= Z_train.shape[0]]
    if not ks:
        raise SystemExit("training split is empty; cannot fit k-means")
    models, table = {}, []
    for k in ks:
        m = KMeans(n_clusters=k, n_init=10, random_state=seed).fit(Z_train)
        sil = float("nan")
        if k >= 2 and len(set(m.labels_.tolist())) >= 2:
            sil = float(silhouette_score(Z_train, m.labels_))
        table.append((k, float(m.inertia_), sil))
        models[k] = m

    if forced_k is not None:
        if forced_k not in models:
            raise SystemExit(f"--k {forced_k}: only k in {ks} is valid here")
        return models[forced_k], forced_k, "forced via --k", table
    candidates = [(sil, k) for (k, _in, sil) in table
                  if k >= 2 and np.isfinite(sil)]
    if candidates and max(candidates)[0] >= SIL_MIN:
        best_sil, k = max(candidates)
        rule = f"max train silhouette {best_sil:.3f} >= {SIL_MIN}"
    else:
        k = 1
        rule = (f"no k>=2 reached train silhouette {SIL_MIN} "
                "(normal traffic is one tight cluster) -> k=1")
    return models[k], k, rule, table


def detect(df: pd.DataFrame, *, features: str | None = None,
           train_fraction: float = 0.5, quantile: float = 0.99,
           kmax: int = KMAX_DEFAULT, forced_k: int | None = None,
           seed: int = 0):
    """Full pipeline on a windows DataFrame. Returns (alerts_df, meta)."""
    n = len(df)
    n_train = int(round(train_fraction * n))
    n_train = max(1, min(n_train, n))
    if n_train < MIN_TRAIN:
        raise SystemExit(
            f"training split has {n_train} window(s) (< {MIN_TRAIN}); "
            "need more windows or a larger --train-fraction")

    # -- features: numeric coercion, drop non-numeric, impute NaN from train
    feats = pick_features(df, features)
    X = df[feats].apply(pd.to_numeric, errors="coerce")
    med = X.iloc[:n_train].median()
    dropped = [c for c in X.columns if pd.isna(med[c])]  # all-NaN in train
    if dropped:
        print(f"note: dropping column(s) with no numeric data in the "
              f"training split: {dropped}")
        X = X.drop(columns=dropped)
        med = med.drop(index=dropped)
    n_imputed = int(X.isna().to_numpy().sum())
    X = X.fillna(med)
    if X.shape[1] == 0:
        raise SystemExit("no numeric feature columns found")

    # -- standardize with train statistics only (stored & reused for all rows)
    scaler = StandardScaler().fit(X.iloc[:n_train])
    Z = scaler.transform(X)

    # -- one-class k-means baseline + distance threshold from train scores
    model, k, rule, table = fit_baseline(Z[:n_train], kmax, forced_k, seed)
    train_scores = model.transform(Z[:n_train]).min(axis=1)
    scores = model.transform(Z).min(axis=1)
    threshold = float(np.quantile(train_scores, quantile))
    alerts = (scores > threshold).astype(int)

    out = pd.DataFrame({"window_id": df["window_id"].to_numpy(),
                        "score": scores, "alert": alerts})
    meta = {
        "n": n, "n_train": n_train, "features": list(X.columns),
        "n_imputed": n_imputed, "impute_medians": med,
        "k": k, "k_rule": rule, "k_table": table,
        "threshold": threshold, "quantile": quantile,
        "scaler": scaler, "model": model, "X": X, "Z": Z,
    }
    return out, meta


def print_summary(out: pd.DataFrame, meta: dict) -> None:
    n_alert = int(out["alert"].sum())
    print(f"windows: {meta['n']} total, {meta['n_train']} used for training")
    print(f"features ({len(meta['features'])}): {', '.join(meta['features'])}"
          + (f"  [{meta['n_imputed']} NaN cell(s) imputed with train median]"
             if meta["n_imputed"] else ""))
    print("k selection (training split):")
    for k, inertia, sil in meta["k_table"]:
        sil_s = f"{sil:8.3f}" if np.isfinite(sil) else "     n/a"
        print(f"  k={k}: inertia={inertia:12.1f}  silhouette={sil_s}")
    print(f"chosen k={meta['k']} ({meta['k_rule']})")
    print(f"threshold: {meta['threshold']:.3f} "
          f"(quantile {meta['quantile']} of training scores)")
    print(f"alerted: {n_alert}/{meta['n']} "
          f"({100.0 * n_alert / meta['n']:.2f}%)")

    top = out.sort_values("score", ascending=False).head(10)
    print("top offending windows (dominant features: per-feature z-score vs "
          "training):")
    for i in top.index:
        zrow, xrow = meta["Z"][i], meta["X"].iloc[i]
        doms = np.argsort(-np.abs(zrow))[:3]
        dom = ", ".join(
            f"{meta['features'][j]}={xrow.iloc[j]:.4g} (z={zrow[j]:+.1f})"
            for j in doms)
        print(f"  window {out.at[i, 'window_id']}: "
              f"score={out.at[i, 'score']:8.3f} "
              f"alert={int(out.at[i, 'alert'])}  {dom}")


# ---------------------------------------------------------------- selftest

def _normal_window(rng: np.random.Generator) -> dict:
    """A normal window: single HMI polling FC3/FC4 every ~2 s, rare FC6 write.

    Matches the pinned synthetic schema: n_req~30±5, poll_gap~60/n_req,
    n_read_fc=n_req-0..1, n_write_fc~0, poll_gap_cv~0.05±0.03, n_masters=1.
    """
    n_req = int(np.clip(rng.normal(30, 5), 20, 42))
    n_write = 1 if rng.random() < 0.05 else 0     # rare operator setpoint write
    n_read = n_req - n_write
    n_resp = n_req - (1 if rng.random() < 0.02 else 0)  # rare lost response
    return {
        "n_req": n_req, "n_resp": n_resp,
        "n_except": 1 if rng.random() < 0.02 else 0,
        "n_read_fc": n_read, "n_write_fc": n_write, "n_other_fc": 0,
        "n_masters": 1,
        "poll_gap_mean_s": float(rng.normal(WINDOW_S / n_req, 0.04)),
        "poll_gap_cv": float(np.clip(rng.normal(0.05, 0.03), 0.01, 0.20)),
        "n_unique_write_addr": n_write,
    }


def _attack_poll_storm(rng) -> dict:
    """Compromised/extra poller at 10x rate: gap/10, cv unchanged, reads only."""
    n_req = int(np.clip(rng.normal(300, 30), 250, 400))
    return _win(n_req=n_req, n_read=n_req,
                poll_gap_mean_s=rng.normal(WINDOW_S / n_req, 0.01),
                poll_gap_cv=np.clip(rng.normal(0.06, 0.03), 0.01, 0.20))


def _attack_write_flood(rng) -> dict:
    """Write flood: 20-30 FC6/FC16 writes on top of the normal poll load."""
    n_write = int(rng.integers(20, 31))
    n_read = int(np.clip(rng.normal(30, 5), 20, 42))
    return _win(n_req=n_read + n_write, n_read=n_read, n_write=n_write,
                n_unique_write_addr=int(
                    rng.integers(max(1, n_write // 2), n_write + 1)),
                poll_gap_mean_s=rng.normal(WINDOW_S / n_read, 0.04),
                poll_gap_cv=np.clip(rng.normal(0.06, 0.03), 0.01, 0.20))


def _attack_rogue_master(rng) -> dict:
    """Second master on the wire: per-master poll gap unchanged, counts ~2x."""
    n_req = int(np.clip(rng.normal(60, 8), 45, 95))
    return _win(n_req=n_req, n_read=n_req, n_masters=2,
                poll_gap_mean_s=rng.normal(2.0, 0.12),
                poll_gap_cv=np.clip(rng.normal(0.06, 0.03), 0.01, 0.20))


def _attack_off_schedule(rng) -> dict:
    """Off-schedule polling: normal counts but irregular inter-arrival (cv>0.5)."""
    n_req = int(np.clip(rng.normal(30, 5), 20, 42))
    return _win(n_req=n_req, n_read=n_req,
                poll_gap_mean_s=rng.normal(WINDOW_S / n_req, 0.04),
                poll_gap_cv=rng.uniform(0.5, 0.95))


def _attack_exception_storm(rng) -> dict:
    """Address/FC scanning: 20-50 probes of bad addresses -> exception storm."""
    probes = int(rng.integers(20, 51))
    polls = int(np.clip(rng.normal(30, 5), 20, 42))
    return _win(n_req=polls + probes, n_read=polls + probes, n_except=probes,
                poll_gap_mean_s=rng.normal(WINDOW_S / (polls + probes), 0.03),
                poll_gap_cv=np.clip(rng.normal(0.08, 0.03), 0.01, 0.20))


def _win(*, n_req=0, n_resp=None, n_except=0, n_read=0, n_write=0,
         n_other=0, n_masters=1, poll_gap_mean_s=2.0, poll_gap_cv=0.05,
         n_unique_write_addr=0) -> dict:
    return {
        "n_req": n_req, "n_resp": n_req if n_resp is None else n_resp,
        "n_except": n_except, "n_read_fc": n_read, "n_write_fc": n_write,
        "n_other_fc": n_other, "n_masters": n_masters,
        "poll_gap_mean_s": float(poll_gap_mean_s),
        "poll_gap_cv": float(poll_gap_cv),
        "n_unique_write_addr": n_unique_write_addr,
    }


ATTACKS = [
    ("poll-storm", _attack_poll_storm),
    ("write-flood", _attack_write_flood),
    ("rogue-master", _attack_rogue_master),
    ("off-schedule", _attack_off_schedule),
    ("exception-storm", _attack_exception_storm),
]


def build_selftest_frame(rng: np.random.Generator, n_normal: int = 2000,
                         n_per_attack: int = 20) -> tuple[pd.DataFrame, np.ndarray]:
    """Synthetic windows.csv: n_normal normal windows + 5 attack shapes x
    n_per_attack, on a 60 s-grid timeline. Attacks are scattered through the
    SECOND half only (the training head must stay normal, as the pinned
    contract guarantees for real data). Returns (df, is_attack mask).
    """
    rows, t, attack = [], [], []
    n_total = n_normal + n_per_attack * len(ATTACKS)
    n_train = int(round(0.5 * n_total))
    for _ in range(n_normal):
        rows.append(_normal_window(rng))
        t.append(len(t) * WINDOW_S)
        attack.append(False)
    for _name, gen in ATTACKS:
        for _ in range(n_per_attack):
            rows.append(gen(rng))
            # +30 s offset keeps attack timestamps strictly between grid points
            t.append(rng.uniform(n_train * WINDOW_S + 30,
                                 n_total * WINDOW_S + 30))
            attack.append(True)
    df = pd.DataFrame(rows)
    df["t_start"] = t
    df["_attack"] = attack
    df = df.sort_values("t_start").reset_index(drop=True)  # mask must follow
    is_attack = df.pop("_attack").to_numpy()
    df["t_end"] = df["t_start"] + WINDOW_S
    df.insert(0, "window_id", np.arange(len(df)))
    df = df[CONTRACT_COLUMNS]
    return df, is_attack


def selftest(seed: int) -> int:
    rng = np.random.default_rng(seed)
    df, is_attack = build_selftest_frame(rng)
    n_atk, n_ok = int(is_attack.sum()), int((~is_attack).sum())

    # Exercise the real CSV round-trip (header schema included).
    with tempfile.TemporaryDirectory() as tmp:
        win_path = Path(tmp) / "windows.csv"
        alert_path = Path(tmp) / "alerts.csv"
        model_path = Path(tmp) / "model.joblib"
        df.to_csv(win_path, index=False)
        out, meta = detect(pd.read_csv(win_path))
        assert list(out.columns) == ["window_id", "score", "alert"], \
            f"bad alerts schema: {list(out.columns)}"
        out.to_csv(alert_path, index=False)
        header = alert_path.read_text().splitlines()[0]
        assert header == "window_id,score,alert", f"bad CSV header: {header}"
        joblib.dump({"features": meta["features"], "scaler": meta["scaler"],
                     "kmeans": meta["model"], "threshold": meta["threshold"],
                     "threshold_quantile": meta["quantile"],
                     "impute_medians": meta["impute_medians"],
                     "k": meta["k"], "k_rule": meta["k_rule"]},
                    model_path)
        assert joblib.load(model_path)["threshold"] == meta["threshold"]

    alert = out["alert"].to_numpy().astype(bool)
    tp = int((alert & is_attack).sum())
    fn = int((~alert & is_attack).sum())
    fp = int((alert & ~is_attack).sum())
    tn = int((~alert & ~is_attack).sum())
    recall, far = tp / n_atk, fp / n_ok

    print(f"selftest: {n_ok} synthetic normal + {n_atk} injected attack "
          f"windows (5 shapes x {n_atk // len(ATTACKS)}), seed={seed}")
    print_summary(out, meta)
    print("\nconfusion (rows = truth, cols = alert):")
    print(f"            alert  no-alert")
    print(f"  attack   {tp:6d}  {fn:8d}   recall      {100 * recall:6.2f}%")
    print(f"  normal   {fp:6d}  {tn:8d}   alert rate  {100 * far:6.2f}%")
    assert far < 0.05, f"false alert rate {far:.3f} on normal windows >= 5%"
    assert recall > 0.80, f"recall {recall:.3f} on attacks <= 80%"
    print(f"\nPASS: alert rate on normal {100 * far:.2f}% (<5%), "
          f"recall on attacks {100 * recall:.2f}% (>80%)")
    return 0


# -------------------------------------------------------------------- main

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="One-class k-means detector over Modbus traffic windows.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  %(prog)s --in lab/data/windows.csv --out lab/data/alerts-kmeans.csv\n"
               "  %(prog)s --in lab/data/windows.csv --out a.csv --model-out m.joblib --k 1\n"
               "  %(prog)s --selftest")
    p.add_argument("--in", dest="infile", help="input windows.csv (pinned schema)")
    p.add_argument("--out", dest="outfile", help="output alerts CSV: window_id,score,alert")
    p.add_argument("--train-fraction", type=float, default=0.5,
                   help="head fraction of windows to train on (default 0.5; "
                        "input must start with normal traffic)")
    p.add_argument("--threshold-quantile", type=float, default=0.99,
                   help="alert threshold = this quantile of TRAINING "
                        "distances (default 0.99)")
    p.add_argument("--kmax", type=int, default=KMAX_DEFAULT,
                   help="largest k to try on the training split (default 3)")
    p.add_argument("--k", type=int, default=None,
                   help="force k instead of the silhouette rule")
    p.add_argument("--features", default=None,
                   help="comma-separated feature columns to override the "
                        "default (all numeric columns except window_id/t_start/t_end)")
    p.add_argument("--model-out", default=None,
                   help="persist scaler + kmeans + threshold to this joblib file")
    p.add_argument("--selftest", action="store_true",
                   help="generate synthetic windows, assert sane detection, exit 0")
    p.add_argument("--seed", type=int, default=0, help="RNG seed (default 0)")
    args = p.parse_args(argv)

    if args.selftest:
        return selftest(args.seed)
    if not args.infile or not args.outfile:
        p.error("--in and --out are required (or use --selftest)")

    df = pd.read_csv(args.infile)
    out, meta = detect(
        df, features=args.features, train_fraction=args.train_fraction,
        quantile=args.threshold_quantile, kmax=args.kmax, forced_k=args.k,
        seed=args.seed)
    print_summary(out, meta)
    out.to_csv(args.outfile, index=False)
    print(f"wrote {args.outfile} ({len(out)} rows, schema window_id,score,alert)")
    if args.model_out:
        joblib.dump({"features": meta["features"], "scaler": meta["scaler"],
                     "kmeans": meta["model"], "threshold": meta["threshold"],
                     "threshold_quantile": meta["quantile"],
                     "impute_medians": meta["impute_medians"],
                     "k": meta["k"], "k_rule": meta["k_rule"]},
                    args.model_out)
        print(f"wrote {args.model_out} (scaler + kmeans + threshold)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
