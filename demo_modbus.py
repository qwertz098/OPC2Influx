"""
Modbus TCP demo device (port 5020): changing values to try the Modbus source.

  python demo_modbus.py [port]

Holding registers (unit 1, 0-based):
   0  float32 ABCD  Power kW            4  uint16 ×0.1  Temperature °C
   2  float32 CDAB  Flow m³/h           5  uint16       Status word (bit 0 run, bit 1 fault, bit 2 door)
   6  uint32  ABCD  Energy counter kWh  8  int16        Setpoint offset
Input registers: 0 int16 ×0.01 Pressure bar, 1 uint16 cycle counter
Coils: 0 pump running, 1 valve open · Discrete inputs: 0 alarm, 1 maintenance due
"""
import asyncio
import math
import sys
import time

from app.modbus.codec import encode
from app.modbus.server import ModbusTcpServer, RegisterBank


async def main(port: int) -> None:
    bank = RegisterBank()
    server = ModbusTcpServer(bank, host="0.0.0.0", port=port)
    await server.start()
    print(f"Modbus demo device on port {server.port}", flush=True)
    energy, cycle, t0 = 1000.0, 0, time.time()
    while True:
        t = time.time() - t0
        power = 80 + 20 * math.sin(t / 30) + 3 * math.sin(t / 3)
        energy += power / 3600
        fault = int(t) % 120 > 110
        bank.set_words("holding", 0, encode(power, "float32", "ABCD"))
        bank.set_words("holding", 2, encode(12.5 + 2 * math.sin(t / 20), "float32", "CDAB"))
        bank.set_words("holding", 4, [int((65 + 5 * math.sin(t / 50)) * 10)])
        bank.set_words("holding", 5, [1 | (2 if fault else 0) | (4 if int(t) % 300 < 20 else 0)])
        bank.set_words("holding", 6, encode(int(energy), "uint32"))
        bank.set_words("holding", 8, encode(-5, "int16"))
        bank.set_words("input", 0, encode(int((3 + 0.4 * math.sin(t / 15)) * 100), "int16"))
        cycle = (cycle + 1) & 0xFFFF
        bank.set_words("input", 1, [cycle])
        bank.coil[0], bank.coil[1] = True, int(t) % 60 < 30
        bank.discrete[0], bank.discrete[1] = fault, False
        await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 5020))
