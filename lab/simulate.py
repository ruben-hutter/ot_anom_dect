#!/usr/bin/env python3
"""Offline Modbus/TCP traffic simulator for the ot-anom lab.

Generates `events.jsonl` directly — no docker, no networking, no pymodbus:
event *timings* mimic what the real lab produces (one HMI master polling FC4
ir[0..1] every --interval seconds with ±0.2 s jitter, an FC3 hr[0] check
every 10th cycle, occasional operator FC6 setpoint writes ~1 per 10 min) and
the register *values* come from the same 1 Hz heated-loop process simulation
as lab/modbus/server.py, so simulated payloads look like real captures.

Output is the pinned events contract — one request/response pair per line,
keys in this exact order:

    {"ts": 1765000000.5, "src": "172.27.100.2", "dst": "172.27.100.3",
     "unit_id": 1, "fc": 4, "addr": 0, "count": 2, "values": [213, 1],
     "exception": false}

Anomaly scenarios (--scenario, deterministic given --seed) each target one
window feature (see featurize.py):

    normal           baseline, as above
    poll-storm       HMI poll interval / 10             -> n_req, poll_gap_*
    write-flood      FC6 write every ~2 s               -> n_write_fc
    off-schedule     poll gaps uniform 0.2-6 s          -> poll_gap_cv
    rogue-master     second src IP with its own loop    -> n_masters
    exception-storm  ~1/3 of requests get exceptions    -> n_except

    python3 simulate.py --hours 1 --scenario normal --out data/sim-normal.jsonl
    python3 simulate.py --selftest
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import tempfile
from pathlib import Path

# --- "wire" identity (matches the lab mgmt subnet 172.27.100.0/24) ---
HMI_IP = "172.27.100.2"  # the one legitimate master (src of all requests)
PLC_IP = "172.27.100.3"  # the PLC (dst of all requests)
ROGUE_IP = "172.27.100.99"  # rogue master, rogue-master scenario only
UNIT_ID = 1

# --- process simulation constants (mirror lab/modbus/server.py exactly) ---
AMBIENT = 200  # tenths °C; PV starts here
SP_DEFAULT = 250  # tenths °C (hr[0] before an operator writes it)
HEAT_GAIN = 3
LOSS = 0.02
HYSTERESIS = 10
NOISE = 2

# --- traffic-shape constants ---
JITTER = 0.2  # ±s jitter on HMI poll gaps
FC3_EVERY = 10  # FC3 setpoint check every Nth HMI cycle
FC3_DELAY = (0.001, 0.01)  # FC3 fires just after the FC4 of the same cycle
WRITE_MEAN_S = 600.0  # operator writes ~1 per 10 min
WRITE_MEAN_S_FLOOD = 2.0  # write-flood: an FC6 every ~2 s
SP_MIN, SP_MAX = 180, 350  # plausible setpoint band, tenths °C
SP_STEP = 40  # max setpoint change per operator action, tenths °C
EXCEPT_P = 1 / 3  # exception-storm: probability a request gets an exception
ROGUE_INTERVAL = 1.0  # rogue-master poll period, s
OFF_SCHEDULE_RANGE = (0.2, 6.0)  # off-schedule poll gaps, uniform

SCENARIOS = [
    "normal",
    "poll-storm",
    "write-flood",
    "off-schedule",
    "rogue-master",
    "exception-storm",
]

EVENT_KEYS = ["ts", "src", "dst", "unit_id", "fc", "addr", "count", "values", "exception"]
DEFAULT_START_TS = 1765000000.0  # fixed default epoch so runs are reproducible


class Process:
    """1 Hz heated loop, same formula as lab/modbus/server.py run_process."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.pv = AMBIENT
        self.heater = False
        self.sp = SP_DEFAULT

    def tick(self) -> None:
        delta = LOSS * (AMBIENT - self.pv) + (HEAT_GAIN if self.heater else 0.0)
        delta += self.rng.randint(-NOISE, NOISE)
        self.pv = int(min(65535, max(0, self.pv + round(delta))))
        if self.heater:
            if self.pv >= self.sp + HYSTERESIS:
                self.heater = False
        elif self.pv < self.sp - HYSTERESIS:
            self.heater = True


def build_timeline(rng: random.Random, seconds: int, sp_writes: dict[int, int]) -> list[tuple[int, int, int]]:
    """Per-second (pv, heater, sp) covering [0, seconds); sp_writes maps
    second -> new setpoint (operator writes land on the tick boundary)."""
    proc = Process(rng)
    timeline = []
    for s in range(seconds):
        if s in sp_writes:
            proc.sp = sp_writes[s]
        proc.tick()
        timeline.append((proc.pv, int(proc.heater), proc.sp))
    return timeline


def event(ts: float, src: str, fc: int, addr: int | None, count: int | None,
          values: list[int] | None, exception: bool = False) -> dict:
    return {
        "ts": round(ts, 3),
        "src": src,
        "dst": PLC_IP,
        "unit_id": UNIT_ID,
        "fc": fc,
        "addr": addr,
        "count": count,
        "values": values,
        "exception": exception,
    }


def generate_events(rng: random.Random, hours: float, interval: float, scenario: str,
                    start_ts: float) -> list[dict]:
    """Produce the scenario's events (already ts-sorted, deterministic in rng)."""
    dur = hours * 3600.0
    t0 = start_ts

    # -- 1. operator write schedule (drawn first: it drives the process sim) --
    write_mean = WRITE_MEAN_S_FLOOD if scenario == "write-flood" else WRITE_MEAN_S
    writes: list[tuple[float, int]] = []
    sp = SP_DEFAULT
    t = t0 + rng.expovariate(1 / write_mean)
    while t < t0 + dur:
        sp = max(SP_MIN, min(SP_MAX, sp + rng.randint(-SP_STEP, SP_STEP)))
        writes.append((t, sp))
        t += rng.expovariate(1 / write_mean)

    # -- 2. process timeline the poll payloads read from --
    timeline = build_timeline(
        rng, int(dur) + 2, {int(wt - t0): wsp for wt, wsp in writes}
    )

    def snap(ts: float) -> tuple[int, int, int]:
        s = max(0, min(int(ts - t0), len(timeline) - 1))
        return timeline[s]

    # -- 3. poll loop(s) --
    events: list[dict] = []
    jit = min(JITTER, 0.1 * interval)  # jitter scales with the period so gaps stay > 0

    def poll_loop(src: str, gap_fn, fc3: bool) -> None:
        t = t0 + gap_fn()
        cycle = 0
        while t < t0 + dur:
            cycle += 1
            pv, heater, _ = snap(t)
            events.append(event(t, src, 4, 0, 2, [pv, heater]))
            if fc3 and cycle % FC3_EVERY == 0:
                d = rng.uniform(*FC3_DELAY)
                _, _, sp_now = snap(t + d)
                events.append(event(t + d, src, 3, 0, 1, [sp_now]))
            t += gap_fn()

    base = interval / 10.0 if scenario == "poll-storm" else interval

    def fixed_gap() -> float:  # fixed period + small jitter (the HMI heartbeat)
        return base + rng.uniform(-jit, jit)

    def off_schedule_gap() -> float:  # heavy jitter replaces the period entirely
        return rng.uniform(*OFF_SCHEDULE_RANGE)

    def rogue_gap() -> float:
        return ROGUE_INTERVAL + rng.uniform(-0.05, 0.05)

    gap_fn = off_schedule_gap if scenario == "off-schedule" else fixed_gap
    poll_loop(HMI_IP, gap_fn, fc3=True)
    if scenario == "rogue-master":
        poll_loop(ROGUE_IP, rogue_gap, fc3=False)

    # -- 4. operator events (FC6 write; rare manual writes get an op.py-style
    #       read-back, the write-flood tool does not) --
    for wt, wsp in writes:
        events.append(event(wt, HMI_IP, 6, 0, None, [wsp]))
        if scenario != "write-flood":
            pv, heater, _ = snap(wt + 0.03)
            events.append(event(wt + 0.03, HMI_IP, 4, 0, 2, [pv, heater]))
            _, _, sp_now = snap(wt + 0.06)
            events.append(event(wt + 0.06, HMI_IP, 3, 0, 1, [sp_now]))

    # -- 5. merge, then mark exceptions (exception-storm) --
    events.sort(key=lambda e: e["ts"])
    if scenario == "exception-storm":
        for e in events:
            if rng.random() < EXCEPT_P:
                e["values"] = None
                e["exception"] = True
    return events


def write_events(events: list[dict], out: str) -> None:
    lines = [json.dumps(e) for e in events]
    if out == "-":
        sys.stdout.write("\n".join(lines) + "\n")
    else:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text("\n".join(lines) + "\n")


def load_events(path: str) -> list[dict]:
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


# ---------------------------------------------------------------- selftest


def _read_gaps(events: list[dict]) -> list[float]:
    """Gaps between consecutive FC3/FC4 read requests from the same master
    (the featurizer's poll-gap definition, reimplemented for assertions)."""
    by_master: dict[str, list[float]] = {}
    for e in events:
        if e["fc"] in (3, 4):
            by_master.setdefault(e["src"], []).append(e["ts"])
    gaps = []
    for ts in by_master.values():
        gaps.extend(b - a for a, b in zip(sorted(ts), sorted(ts)[1:]))
    return gaps


def selftest() -> None:
    import statistics

    seed = 7
    hours = 1 / 6  # 600 s
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "sim.jsonl")

        # determinism: same seed -> identical file; different seed -> differs
        for expect_same in (True, False):
            s1, s2 = seed, (seed if expect_same else seed + 1)
            write_events(generate_events(random.Random(s1), hours, 2.0, "normal", DEFAULT_START_TS), path)
            first = Path(path).read_text()
            write_events(generate_events(random.Random(s2), hours, 2.0, "normal", DEFAULT_START_TS), path)
            same = Path(path).read_text() == first
            assert same == expect_same, f"determinism broken (same={expect_same})"

        ev = generate_events(random.Random(seed), hours, 2.0, "normal", DEFAULT_START_TS)
        # schema: exact keys, exact order, plausible types
        for e in ev:
            assert list(e.keys()) == EVENT_KEYS, e
            assert isinstance(e["ts"], float) and e["src"] == HMI_IP and e["dst"] == PLC_IP
            assert e["unit_id"] == UNIT_ID and e["fc"] in (3, 4, 6)
            assert e["exception"] is False
        fc4 = [e for e in ev if e["fc"] == 4]
        fc3 = [e for e in ev if e["fc"] == 3]
        fc6 = [e for e in ev if e["fc"] == 6]
        assert 280 <= len(fc4) <= 320, len(fc4)  # ~600 s / 2 s = 300 cycles
        assert len(fc4) // 10 - 1 <= len(fc3) <= len(fc4) // 10 + 3, (len(fc4), len(fc3))
        assert 0 <= len(fc6) <= 3, len(fc6)  # Poisson mean 1 per 600 s
        for e in fc4:
            assert e["addr"] == 0 and e["count"] == 2 and len(e["values"]) == 2
            assert e["values"][1] in (0, 1) and 150 <= e["values"][0] <= 400
        for e in fc6:
            assert e["addr"] == 0 and e["count"] is None and SP_MIN <= e["values"][0] <= SP_MAX
        gaps = _read_gaps(ev)
        assert 1.6 < statistics.mean(gaps) < 2.05, statistics.mean(gaps)

        ev = generate_events(random.Random(seed), hours, 2.0, "poll-storm", DEFAULT_START_TS)
        gaps = _read_gaps(ev)
        assert len(ev) > 10 * 280, len(ev)  # ~10x the request rate
        assert statistics.mean(gaps) < 0.25, statistics.mean(gaps)

        ev = generate_events(random.Random(seed), hours, 2.0, "write-flood", DEFAULT_START_TS)
        fc6 = [e for e in ev if e["fc"] == 6]
        assert 250 <= len(fc6) <= 350, len(fc6)  # one write every ~2 s
        assert all(e["addr"] == 0 for e in fc6)

        ev = generate_events(random.Random(seed), hours, 2.0, "off-schedule", DEFAULT_START_TS)
        fc4 = [e for e in ev if e["fc"] == 4]
        fc4_gaps = [b - a for a, b in zip([e["ts"] for e in fc4], [e["ts"] for e in fc4][1:])]
        assert all(0.2 - 1e-9 <= g <= 6.0 + 1e-9 for g in fc4_gaps), "gap outside uniform 0.2-6"
        assert statistics.pstdev(fc4_gaps) / statistics.mean(fc4_gaps) > 0.4, "cv not elevated"

        ev = generate_events(random.Random(seed), hours, 2.0, "rogue-master", DEFAULT_START_TS)
        srcs = {e["src"] for e in ev}
        assert srcs == {HMI_IP, ROGUE_IP}, srcs
        rogue = [e for e in ev if e["src"] == ROGUE_IP]
        assert 500 <= len(rogue) <= 700, len(rogue)  # ~1 s period over 600 s

        ev = generate_events(random.Random(seed), hours, 2.0, "exception-storm", DEFAULT_START_TS)
        frac = sum(e["exception"] for e in ev) / len(ev)
        assert 0.25 <= frac <= 0.42, frac
        assert all(e["values"] is None for e in ev if e["exception"])
        assert all(e["values"] is not None for e in ev if not e["exception"])

        # writer round-trip
        write_events(ev, path)
        rt = load_events(path)
        assert rt == ev, "jsonl round-trip mismatch"

    print("simulate selftest OK")


def main() -> None:
    ap = argparse.ArgumentParser(description="Offline lab-traffic simulator -> events.jsonl")
    ap.add_argument("--hours", type=float, default=1.0, help="simulated duration in hours (default 1)")
    ap.add_argument("--scenario", choices=SCENARIOS, default="normal")
    ap.add_argument("--interval", type=float, default=2.0, help="HMI poll period in seconds (default 2)")
    ap.add_argument("--seed", type=int, default=42, help="RNG seed (default 42, runs are reproducible)")
    ap.add_argument("--start-ts", type=float, default=DEFAULT_START_TS,
                    help=f"epoch seconds of the first event (default {DEFAULT_START_TS})")
    ap.add_argument("--out", default="-", help="output jsonl path (default: stdout)")
    ap.add_argument("--selftest", action="store_true", help="run built-in tests and exit")
    args = ap.parse_args()

    if args.selftest:
        selftest()
        return

    events = generate_events(random.Random(args.seed), args.hours, args.interval,
                             args.scenario, args.start_ts)
    write_events(events, args.out)
    out = args.out if args.out != "-" else "stdout"
    n_fc6 = sum(1 for e in events if e["fc"] == 6)
    print(
        f"scenario={args.scenario} seed={args.seed}: {len(events)} events "
        f"({args.hours:g} h, ~{n_fc6} operator writes) -> {out}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
