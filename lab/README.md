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

## Traffic profile = "normal" baseline

The point of this lab is traffic with learnable structure for anomaly
detection: fixed-period FC4 polls from a single master (2 s default), an FC3
setpoint check every 10th cycle, rare operator FC6 writes, and a slowly
moving process value in the poll payloads. Capture with `just capture`, then
on the host: `tshark -r lab/data/capture-<ts>.pcap -Y mbtcp`.

`data/` is gitignored — captures never get committed.
