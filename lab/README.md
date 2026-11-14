# Mini OT network lab

A small containerlab network that behaves like a minimal real installation:

```
   operator (you)                      HMI/SCADA
   `just sp 300` ──┐              every 2 s: FC4 poll
                   ▼                  │  FC3 check every 10th cycle
              ┌────────────────────────────────┐
              │       modbus-server  :502      │
              │  PLC: register map + simulated │
              │  process (1 Hz, thermostat)    │
              └────────────────────────────────┘
```

- **`modbus-server` — PLC** (`modbus/server.py`): pymodbus Modbus/TCP slave
  (unit id 1) plus a ~1 Hz simulation thread: heated loop with hysteresis
  thermostat, PV starts at ambient and approaches the setpoint over tens of
  seconds, then oscillates around it. The simulation writes the registers
  directly on the datastore, so the server log (`/tmp/modbus-server.log` in
  the container) shows only real wire traffic (`served`/`applied` lines).
- **`modbus-master` — HMI/SCADA** (`modbus/master.py`): one persistent
  session, fixed-period polls (FC4 on `ir[0..1]`, FC3 on `hr[0]` every 10th
  cycle), never writes.
- **operator** (`modbus/op.py`, via `just sp`): rare single FC6 write of the
  setpoint.

## Register map (unit id 1)

| Register | Meaning                                   | Access    |
|---|---|---|
| `ir[0]`  | process value PV, tenths of °C            | read-only |
| `ir[1]`  | heater output state, 0/1                  | read-only |
| `hr[0]`  | setpoint SP, tenths of °C (default 250)   | read/write |

Simulation constants (ambient 20.0 °C, heat gain, hysteresis ±1.0 °C, ±0.2 °C
noise) live at the top of `server.py`.

## Recipes (`just`, run from this directory)

| Recipe | What it does |
|---|---|
| `just up` | deploy the 2 containers, install pymodbus + tcpdump, start the PLC (first run ~1 min) |
| `just hello` | smoke test: operator writes setpoint 240, reads everything back, tails the server log |
| `just run [secs=30]` | HMI poller for `secs`, then tail the server log — the default demo |
| `just sp [tenths=250]` | operator writes the setpoint (e.g. `just sp 300` → 30.0 °C) |
| `just capture [secs=30]` | tcpdump port 502 in the server node → `data/capture-<ts>.pcap` |
| `just down` | tear down |
| `just simulate [hours=1] [scenario=normal]` | **offline**: simulate traffic → `data/sim-<scenario>.jsonl` (no lab needed) |
| `just featurize <events…> [out]` | **offline**: events.jsonl → `windows.csv` |
| `just featurize-pcap <pcap…> [out]` | pcap(s) → `windows.csv` (tshark runs on the host; glob ok) |
| `just sim-all [hours=0.5]` | **offline**: all 6 scenarios + per-scenario `windows.csv` + feature comparison table |

## Traffic profile = "normal" baseline

The point of this lab is traffic with learnable structure for anomaly
detection: fixed-period FC4 polls from a single master (2 s default), an FC3
setpoint check every 10th cycle, rare operator FC6 writes, and a slowly
moving process value in the poll payloads. Capture with `just capture`, then
on the host: `tshark -r lab/data/capture-<ts>.pcap -Y mbtcp`.

`data/` is gitignored — captures and generated datasets never get committed.

## Offline data pipeline: simulator, scenarios, featurizer

`simulate.py` and `featurize.py` run on the host (no lab, no docker).
They produce/consume the **pinned data contract** the anomaly detectors in
the sibling worktrees are built against:

**`events.jsonl`** — one Modbus request/response pair per line (ts = request
time, src = master; `values` from the response, `null` if absent/exception):

```json
{"ts": 1765000000.5, "src": "172.27.100.2", "dst": "172.27.100.3", "unit_id": 1,
 "fc": 4, "addr": 0, "count": 2, "values": [213, 1], "exception": false}
```

**`windows.csv`** — one row per 60 s window (only windows with ≥1 event),
header exactly:

```
window_id,t_start,t_end,n_req,n_resp,n_except,n_read_fc,n_write_fc,n_other_fc,n_masters,poll_gap_mean_s,poll_gap_cv,n_unique_write_addr
```

fc classes: read = 1,2,3,4 · write = 5,6,15,16 · other = rest. `n_masters` =
unique request src IPs. `poll_gap_*` = gaps between consecutive FC3/FC4 read
requests of the same master, pooled across masters (cv = std/mean, population
std); **empty cell** when a window has no such gap. `n_unique_write_addr` =
distinct addresses written. Simulated and captured traffic land in the same
magnitudes: normal ≈ n_req 32/window, n_masters 1, n_write_fc ≈ 0,
poll_gap_mean ≈ 1.8 s, poll_gap_cv ≈ 0.33.

(`poll_gap_mean` sits below the 2 s poll period because the every-10th-cycle
FC3 check is a read request too — its near-zero gap to the FC4 of the same
cycle is pooled in, per the contract. Real captures show the same effect.)

### `simulate.py` — offline traffic simulator

Mimics the live lab's traffic: one HMI master polling FC4 `ir[0..1]` every
`--interval` (2 s ±0.2 s jitter) + FC3 `hr[0]` every 10th cycle, operator FC6
setpoint writes ~1 per 10 min; register values come from the same 1 Hz
heated-loop process sim as `modbus/server.py`. Deterministic per `--seed`;
`--hours 1` runs in milliseconds. IPs are illustrative (master
172.27.100.2 → PLC 172.27.100.3).

`--scenario` emits labeled traffic targeting one window feature each
(featurize + compare: `just sim-all`):

| Scenario | Traffic | Target feature |
|---|---|---|
| `normal` | baseline (above) | — |
| `poll-storm` | poll interval / 10 | `n_req` ↑, `poll_gap_mean_s` ↓ |
| `write-flood` | FC6 write every ~2 s | `n_write_fc` ↑ |
| `off-schedule` | poll gaps uniform 0.2–6 s | `poll_gap_cv` ↑ |
| `rogue-master` | 2nd src IP polling at 1 s | `n_masters` = 2 |
| `exception-storm` | ~1/3 of requests excepted | `n_except` ↑ |

### `featurize.py` — events/pcap → windows.csv

`--events x.jsonl` (one or more) reads the contract directly. `--pcap y.pcap`
(one or more, glob ok) is parsed on the host via
`tshark -r … -Y mbtcp -T json`: requests are the frames with tcp dst port
502 (Modbus servers listen there), request/response paired by (tcp stream,
MBAP transaction id), response register values become `values`. `--window`
overrides the 60 s default.

Validated against the real lab: `just up && just capture 60`, then
`just featurize-pcap data/capture-*.pcap data/windows-real.csv`, then
`just down` — features match the simulator's magnitudes (30 polls/window,
n_masters 1, n_write_fc 0, poll_gap_mean ≈ 1.8 s, cv ≈ 0.34).

Both scripts self-test: `python3 lab/simulate.py --selftest`,
`python3 lab/featurize.py --selftest` (featurize's exercises tshark on a
hand-crafted capture — run inside the dev shell).
