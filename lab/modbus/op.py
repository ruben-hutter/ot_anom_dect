#!/usr/bin/env python3
"""Operator console for the mini OT network lab: change the setpoint.

Writes hr[0] (setpoint, tenths of °C) with a single FC6 write, then reads
back the process value / heater state (FC4) and the new setpoint (FC3) as
confirmation. This is the rare "operator action" in otherwise read-only
poll traffic.

    python3 op.py [host] [setpoint-tenths]
"""
import sys
import time

from pymodbus.client import ModbusTcpClient


def main() -> None:
    host = sys.argv[1] if len(sys.argv) > 1 else "modbus-server"
    sp = int(sys.argv[2]) if len(sys.argv) > 2 else 250

    client = ModbusTcpClient(host, port=502)
    assert client.connect(), f"connect to {host}:502 failed"

    ts = time.strftime("%H:%M:%S")
    w = client.write_register(0, sp, slave=1)
    if w.isError():
        print(f"[{ts}] FC6 write hr[0]={sp} FAILED: {w}", flush=True)
    else:
        print(f"[{ts}] operator: setpoint hr[0]={sp} ({sp / 10:.1f} C) written", flush=True)

    ri = client.read_input_registers(0, count=2, slave=1)
    rr = client.read_holding_registers(0, count=1, slave=1)
    if not ri.isError() and not rr.isError():
        pv, heater = ri.registers[0] / 10, bool(ri.registers[1])
        print(
            f"[{ts}] now: PV={pv:.1f} C heater={'on' if heater else 'off'} "
            f"SP={rr.registers[0] / 10:.1f} C",
            flush=True,
        )

    client.close()


if __name__ == "__main__":
    main()
