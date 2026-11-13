#!/usr/bin/env python3
"""Modbus TCP slave for the mini OT network lab: PLC + simulated process.

Emulates a small temperature-control PLC:

    register map (unit id 1)
    -------------------------------------------------------------
    ir[0]  process value PV, temperature in tenths of °C   read-only
    ir[1]  heater output state, 0/1                        read-only
    hr[0]  setpoint SP, temperature in tenths of °C        read/write
           (the HMI/operator writes this; default 250 = 25.0 °C)

A ~1 Hz simulation thread runs a heated loop with a hysteresis thermostat:

    PV += loss*(ambient - PV) + heat_gain*heater + noise
    heater ON  when PV <  SP - hysteresis
    heater OFF when PV >= SP + hysteresis

The thread updates the input registers DIRECTLY on the data block, bypassing
LoggingSlaveContext, so the server log stays wire-traffic-only: `served` /
`applied` lines are emitted exclusively for real Modbus requests.

pymodbus serves one unit id (1).
"""
import logging

# pymodbus logs noisy errors on client disconnects; we only care about the
# request log below (plain print, so nothing can swallow it).
logging.disable(logging.CRITICAL)

import random  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

from pymodbus.datastore import (  # noqa: E402
    ModbusSequentialDataBlock,
    ModbusServerContext,
    ModbusSlaveContext,
)
from pymodbus.server import StartTcpServer  # noqa: E402

# --- process simulation constants (temperatures in tenths of °C) ---
AMBIENT = 200      # ambient temperature 20.0 °C; PV starts here
SP_DEFAULT = 250   # default setpoint 25.0 °C (hr[0] before an operator writes it)
HEAT_GAIN = 3      # heater contribution per tick
LOSS = 0.02        # fraction of (ambient - PV) exchanged with ambient per tick
                   # (max attainable PV = ambient + HEAT_GAIN/LOSS = 35.0 °C)
HYSTERESIS = 10    # thermostat band: ±1.0 °C around the setpoint
NOISE = 2          # process/measurement noise: ±0.2 °C per tick
TICK = 1.0         # simulation tick, seconds (~1 Hz)

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
# pymodbus's ModbusSlaveContext shifts every context (wire) address by +1
# before touching the data blocks (one-based datastore). The simulation
# works directly on the blocks, so it must apply the same shift.
DATASTORE_ADDR = 1


def out(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class LoggingSlaveContext(ModbusSlaveContext):
    """Log every read/write against the datastore (addresses as on the wire).

    Only Modbus requests served over TCP pass through here — the simulation
    thread writes to the raw data blocks instead (see run_process).
    """

    def getValues(self, fc_as_hex, address, count=1):
        vals = super().getValues(fc_as_hex, address, count)
        fc = FC_NAMES.get(fc_as_hex, f"fc=0x{fc_as_hex:x}")
        out(f"served {fc} addr={address} count={count} -> {vals}")
        return vals

    def setValues(self, fc_as_hex, address, values):
        super().setValues(fc_as_hex, address, values)
        fc = FC_NAMES.get(fc_as_hex, f"fc=0x{fc_as_hex:x}")
        out(f"applied {fc} addr={address} values={list(values)}")


def build_context():
    """Build the datastore; return (context, ir_block, hr_block).

    The raw blocks are returned so the simulation thread can read/write them
    without producing log lines (the blocks are the same objects the logging
    context wraps, so wire reads see the sim's updates).
    """
    di_block = ModbusSequentialDataBlock(0, [0] * REGISTERS)
    co_block = ModbusSequentialDataBlock(0, [0] * REGISTERS)
    hr_block = ModbusSequentialDataBlock(0, [0] * REGISTERS)
    ir_block = ModbusSequentialDataBlock(0, [0] * REGISTERS)
    # seed directly on the raw blocks (wire ir[0]/hr[0] == block addr 1,
    # see DATASTORE_ADDR): PV at ambient, heater off, setpoint at default
    ir_block.setValues(DATASTORE_ADDR, [AMBIENT, 0])
    hr_block.setValues(DATASTORE_ADDR, [SP_DEFAULT])
    store = LoggingSlaveContext(di=di_block, co=co_block, hr=hr_block, ir=ir_block)
    return ModbusServerContext(slaves=store, single=True), ir_block, hr_block


def run_process(ir_block, hr_block) -> None:
    """~1 Hz heated-loop simulation: thermostat + first-order physics.

    Writes go straight to the data blocks (not the logging context) so the
    log only shows wire traffic. List slice-assignment under the GIL is
    atomic enough here: wire reads see either the previous or the new state,
    never a torn one that matters at 1 Hz.
    """
    pv = AMBIENT
    heater = False
    while True:
        sp = hr_block.getValues(DATASTORE_ADDR, 1)[0]
        delta = LOSS * (AMBIENT - pv) + (HEAT_GAIN if heater else 0.0)
        delta += random.randint(-NOISE, NOISE)
        # registers are 16-bit: clamp so a silly setpoint can't overflow
        pv = int(min(65535, max(0, pv + round(delta))))
        # thermostat with hysteresis around the operator-set setpoint
        if heater:
            if pv >= sp + HYSTERESIS:
                heater = False
        elif pv < sp - HYSTERESIS:
            heater = True
        ir_block.setValues(DATASTORE_ADDR, [pv, int(heater)])
        time.sleep(TICK)


def main() -> None:
    out(f"Modbus TCP slave up on 0.0.0.0:502 (unit id 1), {REGISTERS} registers/space")
    context, ir_block, hr_block = build_context()
    out(
        f"seeded: ir[0]=PV (tenths C, starts {AMBIENT / 10:.1f} C), "
        f"ir[1]=heater (0/1), hr[0]=setpoint ({SP_DEFAULT / 10:.1f} C)"
    )
    sim = threading.Thread(
        target=run_process, args=(ir_block, hr_block), name="process-sim", daemon=True
    )
    sim.start()
    out(
        f"sim: process simulation running ({TICK:.0f} Hz tick, "
        f"ambient {AMBIENT / 10:.1f} C, hysteresis ±{HYSTERESIS / 10:.1f} C)"
    )
    StartTcpServer(context=context, address=("0.0.0.0", 502))


if __name__ == "__main__":
    main()
