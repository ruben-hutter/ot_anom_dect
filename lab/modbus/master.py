#!/usr/bin/env python3
"""Minimal Modbus TCP master for the ot-anom hello-world lab.

Writes two holding registers (FC6), then polls back a window of holding
registers (FC3) and the read-only input registers (FC4). Usage:

    python3 master.py <server-host>
"""
import sys

from pymodbus.client import ModbusTcpClient


def main() -> None:
    host = sys.argv[1] if len(sys.argv) > 1 else "modbus-server"
    client = ModbusTcpClient(host, port=502)
    assert client.connect(), f"connect to {host}:502 failed"

    w1 = client.write_register(100, 4242, slave=1)
    print(f"write_single_register  hr[100]=4242   -> {'ok' if not w1.isError() else w1}")
    w2 = client.write_register(101, 7, slave=1)
    print(f"write_single_register  hr[101]=7      -> {'ok' if not w2.isError() else w2}")

    rr = client.read_holding_registers(100, count=4, slave=1)
    print(f"read_holding_registers hr[100..103]   -> {rr.registers}")

    ri = client.read_input_registers(0, count=2, slave=1)
    pv = ri.registers[0] / 10
    print(f"read_input_registers   ir[0..1]       -> {ri.registers}  (process value: {pv} C)")

    client.close()


if __name__ == "__main__":
    main()
