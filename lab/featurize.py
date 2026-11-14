#!/usr/bin/env python3
"""Window featurizer: events.jsonl and/or pcap capture -> windows.csv.

Turns the pinned events contract (one Modbus request/response PAIR per line,
see simulate.py) — or a raw pcap, parsed on the host via tshark — into fixed
time-window features for the anomaly detectors:

    window_id,t_start,t_end,n_req,n_resp,n_except,n_read_fc,n_write_fc,
    n_other_fc,n_masters,poll_gap_mean_s,poll_gap_cv,n_unique_write_addr

Definitions (pinned; detectors in sibling worktrees are built against them):
  - fc classes: read = 1,2,3,4 · write = 5,6,15,16 · other = everything else
  - n_masters: unique request src IPs in the window
  - poll_gap_*: gaps between consecutive FC3/FC4 read requests from the same
    master (the HMI heartbeat), pooled across masters; cv = std/mean
    (population std). Empty cell (not 0) when the window has no such gap.
  - n_unique_write_addr: distinct addresses written (FC5/6/15/16) in the
    window
  - one row per window that contains at least one event; windows are aligned
    to floor(first_ts / W) * W

pcap input: requests are the frames with tcp dst port 502 (responses come
FROM the PLC); request/response are paired by (tcp stream, MBAP transaction
id). Values are taken from the response registers when present, else null.

    python3 featurize.py --events data/sim-normal.jsonl --out data/windows.csv
    python3 featurize.py --pcap data/capture-*.pcap --out data/windows.csv
    python3 featurize.py --selftest
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

READ_FCS = {1, 2, 3, 4}
WRITE_FCS = {5, 6, 15, 16}
POLL_FCS = (3, 4)  # read fcs that count as HMI heartbeat polls

HEADER = [
    "window_id", "t_start", "t_end", "n_req", "n_resp", "n_except",
    "n_read_fc", "n_write_fc", "n_other_fc", "n_masters",
    "poll_gap_mean_s", "poll_gap_cv", "n_unique_write_addr",
]
WINDOW_S = 60.0

# tshark fields dissected from each mbtcp frame (-e, flattened into layers).
# NOTE: this tshark's modbus dissector has no exception_flag field — an
# exception response is visible via modbus.exception_code and/or the high bit
# set in modbus.func_code. Register VALUES are modbus.regval_uint16
# (modbus.regnum16 is the register's index within the response).
TSHARK_FIELDS = [
    "frame.time_epoch", "ip.src", "ip.dst", "tcp.srcport", "tcp.dstport",
    "tcp.stream", "mbtcp.trans_id", "mbtcp.unit_id", "modbus.func_code",
    "modbus.reference_num", "modbus.word_cnt", "modbus.byte_cnt",
    "modbus.exception_code", "modbus.regval_uint16", "modbus.bitval",
]


# ------------------------------------------------------------------ events


def flag_pairs(e: dict) -> dict:
    """Attach internal pairing flags to a contract event.

    jsonl events are pairs by construction; a response was seen when the
    event carries response values or is an exception.
    """
    e = dict(e)
    e["_req"] = True
    e["_resp"] = e["values"] is not None or e["exception"]
    return e


def load_events_jsonl(paths: list[str]) -> list[dict]:
    events = []
    for p in paths:
        with open(p) as fh:
            for line in fh:
                if line.strip():
                    events.append(flag_pairs(json.loads(line)))
    return events


def field_values(layers: dict, name: str) -> list[str]:
    """All values of a tshark -e field, in packet order. tshark -T json emits
    repeated occurrences either as a list or as suffixed keys (name, name:1,
    name:2, ...); handle both."""
    out = []
    for key, val in layers.items():
        if key == name or key.startswith(name + ":"):
            if isinstance(val, list):
                out.extend(val)
            else:
                out.append(val)
    return out


def to_int(s: str) -> int:
    try:
        return int(s, 0)  # handles both "4" and "0x04"
    except ValueError:
        return int(s)


def parse_pcap(path: str) -> list[dict]:
    """One record per mbtcp frame via tshark (field -> list of raw strings)."""
    cmd = ["tshark", "-r", path, "-Y", "mbtcp", "-T", "json"]
    for f in TSHARK_FIELDS:
        cmd += ["-e", f]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"featurize: tshark failed on {path}:\n{proc.stderr.strip()}")
    frames = []
    for pkt in json.loads(proc.stdout or "[]"):
        layers = pkt["_source"]["layers"]
        frames.append({f: field_values(layers, f) for f in TSHARK_FIELDS})
    return frames


def frame_role(f: dict) -> str:
    """Modbus TCP servers listen on 502: clients send requests TO it."""
    if f["tcp.dstport"] and f["tcp.dstport"][0] == "502":
        return "req"
    if f["tcp.srcport"] and f["tcp.srcport"][0] == "502":
        return "resp"
    return "?"


def reg_values(f: dict) -> list[int] | None:
    vals = [to_int(raw) for raw in f["modbus.regval_uint16"]]
    for raw in f["modbus.bitval"]:  # coil data: boolean bits
        vals.append(1 if raw == "True" else 0)
    return vals or None


def frames_to_events(frames: list[dict]) -> list[dict]:
    """Pair requests with responses by (tcp stream, MBAP transaction id) and
    collapse each pair into one contract event (ts = request time)."""
    pending: dict[tuple, dict] = {}
    events = []

    def make_event(req: dict | None, resp: dict | None) -> dict:
        assert req or resp
        base = req or resp
        ts = float(base["frame.time_epoch"][0])
        if req:
            src, dst = req["ip.src"][0], req["ip.dst"][0]
        else:  # orphan response: master is the destination of the response
            src, dst = resp["ip.dst"][0], resp["ip.src"][0]
        fc = None
        if base["modbus.func_code"]:
            fc = to_int(base["modbus.func_code"][0]) & 0x7F
        addr = None
        ref = (req or resp)["modbus.reference_num"]
        if ref:
            addr = to_int(ref[0])
        count = None
        if req and req["modbus.word_cnt"]:
            count = to_int(req["modbus.word_cnt"][0])
        exception = False
        values = None
        if resp is not None:
            exception = bool(resp["modbus.exception_code"])
            if not exception and resp["modbus.func_code"]:
                exception = to_int(resp["modbus.func_code"][0]) & 0x80 != 0
            if not exception:
                values = reg_values(resp)
        return {
            "ts": ts, "src": src, "dst": dst,
            "unit_id": to_int(base["mbtcp.unit_id"][0]) if base["mbtcp.unit_id"] else None,
            "fc": fc, "addr": addr, "count": count,
            "values": values, "exception": exception,
            "_req": req is not None, "_resp": resp is not None,
        }

    for f in frames:
        role = frame_role(f)
        key = (f["tcp.stream"][0] if f["tcp.stream"] else "?",
               f["mbtcp.trans_id"][0] if f["mbtcp.trans_id"] else "?")
        if role == "req":
            pending.setdefault(key, []).append(f)
        elif role == "resp":
            if pending.get(key):
                events.append(make_event(pending[key].pop(0), f))
            else:
                events.append(make_event(None, f))
        else:  # direction unknown (no port 502 seen) — don't guess
            continue
    for queue in pending.values():  # requests without a captured response
        events.extend(make_event(r, None) for r in queue)
    return events


def load_events_pcaps(paths: list[str]) -> list[dict]:
    events = []
    for p in paths:
        events.extend(frames_to_events(parse_pcap(p)))
    return events


# ----------------------------------------------------------------- windows


def window_row(idx: int, t_start: float, t_end: float, evs: list[dict]) -> dict:
    n_req = sum(1 for e in evs if e["_req"])
    n_resp = sum(1 for e in evs if e["_resp"])
    n_except = sum(1 for e in evs if e["exception"])
    n_read = n_write = n_other = 0
    for e in evs:
        if e["fc"] in READ_FCS:
            n_read += 1
        elif e["fc"] in WRITE_FCS:
            n_write += 1
        elif e["fc"] is not None:
            n_other += 1
    masters = {e["src"] for e in evs if e["_req"]}
    by_master: dict[str, list[float]] = defaultdict(list)
    for e in evs:
        if e["_req"] and e["fc"] in POLL_FCS:
            by_master[e["src"]].append(e["ts"])
    gaps = []
    for ts in by_master.values():
        ts.sort()
        gaps.extend(b - a for a, b in zip(ts, ts[1:]))
    gap_mean = gap_cv = None
    if gaps:
        gap_mean = sum(gaps) / len(gaps)
        var = sum((g - gap_mean) ** 2 for g in gaps) / len(gaps)  # population var
        gap_cv = math.sqrt(var) / gap_mean if gap_mean > 0 else None
    w_addrs = {e["addr"] for e in evs
               if e["_req"] and e["fc"] in WRITE_FCS and e["addr"] is not None}
    return {
        "window_id": idx, "t_start": t_start, "t_end": t_end,
        "n_req": n_req, "n_resp": n_resp, "n_except": n_except,
        "n_read_fc": n_read, "n_write_fc": n_write, "n_other_fc": n_other,
        "n_masters": len(masters),
        "poll_gap_mean_s": gap_mean, "poll_gap_cv": gap_cv,
        "n_unique_write_addr": len(w_addrs),
    }


def build_windows(events: list[dict], window_s: float) -> list[dict]:
    if not events:
        return []
    t0 = math.floor(min(e["ts"] for e in events) / window_s) * window_s
    buckets: dict[int, list[dict]] = defaultdict(list)
    for e in events:
        buckets[int((e["ts"] - t0) // window_s)].append(e)
    rows = []
    for idx in sorted(buckets):
        t_start = t0 + idx * window_s
        rows.append(window_row(idx, t_start, t_start + window_s, buckets[idx]))
    return rows


def write_csv(rows: list[dict], out: str) -> None:
    def cell(v: object) -> str:
        if v is None:
            return ""  # contract: empty cell, not 0
        if isinstance(v, float):
            return f"{v:.4f}"
        return str(v)

    lines = [",".join(HEADER)]
    for r in rows:
        lines.append(",".join(cell(r[k]) for k in HEADER))
    text = "\n".join(lines) + "\n"
    if out == "-":
        sys.stdout.write(text)
    else:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(text)


def summarize(rows: list[dict], out: str, n_events: int) -> None:
    if not rows:
        print("featurize: no events -> no windows", file=sys.stderr)
        return
    span = rows[-1]["t_end"] - rows[0]["t_start"]
    keys = ("n_req", "n_resp", "n_except", "n_write_fc", "n_masters", "poll_gap_mean_s")
    means = {k: (sum(r[k] or 0 for r in rows) / len(rows)) for k in keys}
    stats = " ".join(f"{k}={v:.2f}" for k, v in means.items())
    print(f"featurize: {n_events} events -> {len(rows)} windows "
          f"({span:.0f} s) -> {out if out != '-' else 'stdout'}\n"
          f"  window means: {stats}", file=sys.stderr)


# ---------------------------------------------------------------- selftest


def _ip_bytes(ip: str) -> bytes:
    return bytes(int(o) for o in ip.split("."))


def _tcp_frame(src_ip: str, dst_ip: str, sport: int, dport: int, payload: bytes,
               seq: int = 1) -> bytes:
    """Ethernet + IPv4 + TCP with zero checksums (tshark dissects fine).
    seq must increase per direction or tshark flags retransmissions."""
    eth = bytes.fromhex("020000000002") + bytes.fromhex("020000000001") + b"\x08\x00"
    tcp_len, ip_len = 20, 20 + 20 + len(payload)
    tcp = (sport.to_bytes(2, "big") + dport.to_bytes(2, "big") + seq.to_bytes(4, "big")
           + (0).to_bytes(4, "big") + (5 << 4).to_bytes(1, "big") + bytes([0x18])
           + (0x2000).to_bytes(2, "big") + b"\x00\x00" + b"\x00\x00")
    ip = (bytes([0x45, 0x00]) + ip_len.to_bytes(2, "big") + (0).to_bytes(4, "big")
          + bytes([0x40, 0x06]) + b"\x00\x00"
          + _ip_bytes(src_ip) + _ip_bytes(dst_ip))
    return eth + ip + tcp + payload


def _mbap_pdu(trans: int, unit: int, pdu: bytes) -> bytes:
    # MBAP length = bytes after the length field = unit id (1) + PDU
    return (trans.to_bytes(2, "big") + b"\x00\x00"
            + (len(pdu) + 1).to_bytes(2, "big") + bytes([unit]) + pdu)


def _build_pcap(packets: list[tuple[float, bytes]]) -> bytes:
    """Minimal little-endian pcap: ethernet linktype, packets with timestamps."""
    out = (bytes([0xD4, 0xC3, 0xB2, 0xA1]) + (2).to_bytes(2, "little")
           + (4).to_bytes(2, "little") + (0).to_bytes(4, "little")
           + (0).to_bytes(4, "little") + (65535).to_bytes(4, "little")
           + (1).to_bytes(4, "little"))
    for ts, data in packets:
        sec, usec = int(ts), round((ts - int(ts)) * 1e6)
        out += (sec.to_bytes(4, "little") + usec.to_bytes(4, "little")
                + len(data).to_bytes(4, "little") + len(data).to_bytes(4, "little")
                + data)
    return out


def selftest() -> None:
    import statistics

    t0 = 1765000020.0  # multiple of the 60 s window so windows align at t0
    with tempfile.TemporaryDirectory() as td:
        # ---- events path: hand-built events with exactly known features ----
        A, B, PLC = "10.0.0.1", "10.0.0.2", "10.0.0.10"

        def ev(ts, src, fc, addr, count, values, exception=False):
            return flag_pairs({"ts": t0 + ts, "src": src, "dst": PLC, "unit_id": 1,
                               "fc": fc, "addr": addr, "count": count,
                               "values": values, "exception": exception})

        events = [
            ev(10, A, 4, 0, 2, [200, 0]), ev(12, A, 4, 0, 2, [201, 0]),
            ev(14, A, 4, 0, 2, [202, 1]),
            ev(20, B, 4, 0, 2, [203, 0]), ev(25, B, 4, 0, 2, [204, 0]),
            ev(30, A, 6, 0, None, [260]),          # operator write, addr 0
            ev(40, A, 4, 0, 2, None, True),         # exception response
            ev(70, A, 4, 0, 2, [205, 0]),           # window 1: single read
        ]
        rows = build_windows(events, WINDOW_S)
        assert len(rows) == 2, rows
        r0, r1 = rows
        # gaps pooled across masters: A [2,2] + B [5] -> mean 3, cv sqrt(2)/3
        assert (r0["n_req"], r0["n_resp"], r0["n_except"]) == (7, 7, 1), r0
        assert (r0["n_read_fc"], r0["n_write_fc"], r0["n_other_fc"]) == (6, 1, 0), r0
        assert r0["n_masters"] == 2 and r0["n_unique_write_addr"] == 1, r0
        # gaps pooled across masters; the exception FC4 at t=40 is still an FC4
        # request, so A contributes [2, 2, 26] and B [5]
        gaps = [2, 2, 26, 5]
        assert abs(r0["poll_gap_mean_s"] - statistics.mean(gaps)) < 1e-9, r0
        assert abs(r0["poll_gap_cv"] - statistics.pstdev(gaps) / statistics.mean(gaps)) < 1e-9, r0
        assert r1["n_req"] == 1 and r1["n_masters"] == 1, r1
        assert r1["poll_gap_mean_s"] is None and r1["poll_gap_cv"] is None, r1
        # csv shape: header + rows, None -> empty cells
        path = str(Path(td) / "windows.csv")
        write_csv(rows, path)
        lines = Path(path).read_text().splitlines()
        assert lines[0] == ",".join(HEADER), lines[0]
        assert lines[1] == ("0,1765000020.0000,1765000080.0000,7,7,1,6,1,0,2,"
                            "8.7500,1.1468,1"), lines[1]
        assert lines[2] == ("1,1765000080.0000,1765000140.0000,1,1,0,1,0,0,1,"
                            ",,0"), lines[2]

        # ---- pcap path: synthetic capture through real tshark ----
        if subprocess.run(["which", "tshark"], capture_output=True).returncode != 0:
            sys.exit("featurize selftest: tshark not on PATH (run inside the nix dev shell)")
        master_ip, plc_ip = "172.27.100.2", "172.27.100.3"
        seqs: dict[tuple[str, int], int] = {}

        def tcp(ts: float, src_ip: str, dst_ip: str, sport: int, dport: int, payload: bytes) -> bytes:
            key = (src_ip, sport)
            seq = seqs.get(key, 1000)
            seqs[key] = seq + len(payload)
            return _tcp_frame(src_ip, dst_ip, sport, dport, payload, seq=seq)

        def pair(ts, trans, req_pdu, resp_pdu):
            return [
                (ts, tcp(ts, master_ip, plc_ip, 40000, 502, _mbap_pdu(trans, 1, req_pdu))),
                (ts + 0.005, tcp(ts, plc_ip, master_ip, 502, 40000, _mbap_pdu(trans, 1, resp_pdu))),
            ]

        packets = []
        # FC4 read ir[0..1] -> regs 213, 1
        packets += pair(100.0, 1, bytes([4]) + (0).to_bytes(2, "big") + (2).to_bytes(2, "big"),
                        bytes([4, 4]) + (213).to_bytes(2, "big") + (1).to_bytes(2, "big"))
        # FC3 read hr[0] -> reg 250
        packets += pair(100.01, 2, bytes([3]) + (0).to_bytes(2, "big") + (1).to_bytes(2, "big"),
                        bytes([3, 2]) + (250).to_bytes(2, "big"))
        # FC6 write hr[0]=260 (request value echoed in response)
        packets += pair(100.02, 3, bytes([6]) + (0).to_bytes(2, "big") + (260).to_bytes(2, "big"),
                        bytes([6]) + (0).to_bytes(2, "big") + (260).to_bytes(2, "big"))
        # FC3 read -> exception 2 (illegal data address)
        packets += pair(100.03, 4, bytes([3]) + (0).to_bytes(2, "big") + (1).to_bytes(2, "big"),
                        bytes([0x83, 2]))
        pcap = str(Path(td) / "cap.pcap")
        Path(pcap).write_bytes(_build_pcap(packets))

        parsed = frames_to_events(parse_pcap(pcap))
        assert [e["fc"] for e in parsed] == [4, 3, 6, 3], parsed
        assert [e["addr"] for e in parsed] == [0, 0, 0, 0], parsed
        assert [e["count"] for e in parsed] == [2, 1, None, 1], parsed
        assert [e["values"] for e in parsed] == [[213, 1], [250], [260], None], parsed
        assert [e["exception"] for e in parsed] == [False, False, False, True], parsed
        assert all(e["src"] == master_ip and e["dst"] == plc_ip for e in parsed)
        rows = build_windows(parsed, WINDOW_S)
        r = rows[0]
        assert (r["n_req"], r["n_resp"], r["n_except"]) == (4, 4, 1), r
        assert (r["n_read_fc"], r["n_write_fc"], r["n_masters"]) == (3, 1, 1), r
        assert abs(r["poll_gap_mean_s"] - 0.015) < 1e-6, r  # reads at 100.0, 100.01, 100.03
        assert abs(r["poll_gap_cv"] - 1 / 3) < 1e-6, r  # gaps [0.01, 0.02] -> cv 1/3

        # ---- integration: simulated normal traffic -> sane magnitudes ----
        sys.path.insert(0, str(Path(__file__).parent))
        import random as _random

        import simulate

        sim = simulate.generate_events(_random.Random(1), 10 / 60, 2.0, "normal",
                                       simulate.DEFAULT_START_TS)
        rows = build_windows([flag_pairs(e) for e in sim], WINDOW_S)
        assert 1.7 < rows[0]["poll_gap_mean_s"] < 2.05, rows[0]
        assert rows[0]["n_masters"] == 1 and rows[0]["n_except"] == 0
        assert rows[0]["n_write_fc"] in (0, 1, 2)

    print("featurize selftest OK")


# -------------------------------------------------------------------- cli


def main() -> None:
    ap = argparse.ArgumentParser(description="events.jsonl / pcap -> windows.csv")
    ap.add_argument("--events", nargs="+", default=[], help="events.jsonl path(s)")
    ap.add_argument("--pcap", nargs="+", default=[], help="pcap path(s) (parsed via tshark)")
    ap.add_argument("--out", default="-", help="output csv path (default: stdout)")
    ap.add_argument("--window", type=float, default=WINDOW_S,
                    help="window length in seconds (default 60)")
    ap.add_argument("--selftest", action="store_true", help="run built-in tests and exit")
    args = ap.parse_args()

    if args.selftest:
        selftest()
        return

    events = load_events_jsonl(args.events) + load_events_pcaps(args.pcap)
    if not events:
        sys.exit("featurize: no input (use --events and/or --pcap)")
    events.sort(key=lambda e: e["ts"])
    rows = build_windows(events, args.window)
    write_csv(rows, args.out)
    summarize(rows, args.out, len(events))


if __name__ == "__main__":
    main()
