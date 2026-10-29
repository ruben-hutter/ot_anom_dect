#!/usr/bin/env python3
"""Minimal Modbus TCP slave for the ot-anom hello-world lab.

Holds a small register map (coils / discrete inputs / holding / input
registers) and prints every request it serves, so `just hello` can show the
server-side view of the exchange. pymodbus serves one unit id (1).
"""
import logging

# pymodbus logs noisy errors on client disconnects; we only care about the
# request log below (plain print, so nothing can swallow it).
logging.disable(logging.CRITICAL)

from pymodbus.datastore import (  # noqa: E402
    ModbusSequentialDataBlock,
    ModbusServerContext,
    ModbusSlaveContext,
)
from pymodbus.server import StartTcpServer  # noqa: E402

FC_NAMES = {
    1: "read coils",
    2: "read discrete inputs",
    3: "read holding registers",
    4: "read input registers",
    5: "write single coil",
    6: "write single register",
    15: "write multiple coils",
    16: "write multiple registers",
}
REGISTERS = 1000


def out(msg: str) -> None:
    import time

    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class LoggingSlaveContext(ModbusSlaveContext):
    """Log every read/write against the datastore (addresses as on the wire)."""

    def getValues(self, fc_as_hex, address, count=1):
        vals = super().getValues(fc_as_hex, address, count)
        fc = FC_NAMES.get(fc_as_hex, f"fc=0x{fc_as_hex:x}")
        out(f"served {fc} addr={address} count={count} -> {vals}")
        return vals

    def setValues(self, fc_as_hex, address, values):
        super().setValues(fc_as_hex, address, values)
        fc = FC_NAMES.get(fc_as_hex, f"fc=0x{fc_as_hex:x}")
        out(f"applied {fc} addr={address} values={list(values)}")


def build_context() -> ModbusServerContext:
    store = LoggingSlaveContext(
        di=ModbusSequentialDataBlock(0, [0] * REGISTERS),
        co=ModbusSequentialDataBlock(0, [0] * REGISTERS),
        hr=ModbusSequentialDataBlock(0, [0] * REGISTERS),
        ir=ModbusSequentialDataBlock(0, [0] * REGISTERS),
    )
    # seed the read-only input registers with a fake "process value":
    # 213 tenths of a degree = 21.3 °C on a sensor, plus one more reading
    # (goes through the context so the wire-address offset is applied)
    store.setValues(4, 0, [213, 1057])
    return ModbusServerContext(slaves=store, single=True)


def main() -> None:
    out(f"Modbus TCP slave up on 0.0.0.0:502 (unit id 1), {REGISTERS} registers/space, all zeroed")
    context = build_context()
    out("input registers seeded: ir[0]=213 (21.3 C), ir[1]=1057")
    StartTcpServer(context=context, address=("0.0.0.0", 502))


if __name__ == "__main__":
    main()
