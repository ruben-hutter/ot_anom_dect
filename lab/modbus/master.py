#!/usr/bin/env python3
"""HMI/SCADA poller for the mini OT network lab.

One persistent Modbus/TCP session to the PLC, polling on a fixed period:
each cycle reads the process value + heater state (FC4), and every 10th
cycle also checks the setpoint (FC3). Never writes — operator changes go
through op.py (`just sp`). This fixed-period, single-master, read-mostly
pattern is the "normal" traffic baseline for anomaly detection.

    python3 master.py [host] [--interval SECONDS] [--duration SECONDS]

Ctrl-C ends the poll cleanly.
"""
import argparse
import time

from pymodbus.client import ModbusTcpClient


def main() -> None:
    ap = argparse.ArgumentParser(description="HMI/SCADA poller: fixed-period reads of PV/heater/setpoint")
    ap.add_argument("host", nargs="?", default="modbus-server", help="PLC host (default: modbus-server)")
    ap.add_argument("--interval", type=float, default=2.0, help="poll period in seconds (default: 2)")
    ap.add_argument("--duration", type=float, default=30.0, help="stop after this many seconds (default: 30)")
    args = ap.parse_args()

    client = ModbusTcpClient(args.host, port=502)
    assert client.connect(), f"connect to {args.host}:502 failed"
    print(
        f"polling {args.host}:502 every {args.interval:g}s for {args.duration:g}s (Ctrl-C to stop)",
        flush=True,
    )

    deadline = time.monotonic() + args.duration
    next_poll = time.monotonic()
    cycle = 0
    setpoint = None  # cached from the periodic FC3 check; shown once known
    try:
        while time.monotonic() < deadline:
            cycle += 1
            ts = time.strftime("%H:%M:%S")
            ri = client.read_input_registers(0, count=2, slave=1)
            if ri.isError():
                print(f"[{ts}] FC4 error: {ri}", flush=True)
            else:
                pv, heater = ri.registers[0] / 10, bool(ri.registers[1])
                line = f"[{ts}] PV={pv:.1f} C heater={'on' if heater else 'off'}"
                if setpoint is not None:
                    line += f" SP={setpoint / 10:.1f} C"
                print(line, flush=True)
            if cycle % 10 == 0:  # periodic setpoint check, cheap realistic variety
                rr = client.read_holding_registers(0, count=1, slave=1)
                if rr.isError():
                    print(f"[{ts}] FC3 error: {rr}", flush=True)
                else:
                    setpoint = rr.registers[0]
                    print(f"[{ts}] SP check: hr[0]={setpoint / 10:.1f} C", flush=True)
            # fixed-period pacing: sleep out the remainder of this cycle
            next_poll = max(next_poll + args.interval, time.monotonic())
            if next_poll > time.monotonic():
                time.sleep(next_poll - time.monotonic())
    except KeyboardInterrupt:
        print("--- poller stopped (Ctrl-C) ---", flush=True)
    finally:
        client.close()
        print(f"done: {cycle} cycles", flush=True)


if __name__ == "__main__":
    main()
