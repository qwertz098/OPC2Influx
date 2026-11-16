"""
A small Modbus TCP server over an in-memory register bank.

Function codes 1–6, 15 and 16. Used by the tests and the demo; the base for
publishing values over Modbus later.
"""
from __future__ import annotations

import asyncio
import logging
import struct
from typing import Optional

logger = logging.getLogger(__name__)


class RegisterBank:
    """Coils, discrete inputs, input and holding registers of one unit (65536 each)."""

    def __init__(self) -> None:
        self.coil: dict[int, bool] = {}
        self.discrete: dict[int, bool] = {}
        self.input: dict[int, int] = {}
        self.holding: dict[int, int] = {}
        self.valid: Optional[set[tuple[str, int]]] = None   # None: every address exists

    def exists(self, area: str, start: int, count: int) -> bool:
        if start < 0 or start + count > 65536:
            return False
        return self.valid is None or all((area, a) in self.valid for a in range(start, start + count))

    def set_words(self, area: str, address: int, words: list[int]) -> None:
        bank = getattr(self, area)
        for i, w in enumerate(words):
            bank[address + i] = w & 0xFFFF


class ModbusTcpServer:
    def __init__(self, banks: dict[int, RegisterBank] | RegisterBank, host: str = "127.0.0.1", port: int = 5020,
                 any_unit: bool = True) -> None:
        self.banks = banks if isinstance(banks, dict) else {1: banks}
        self.host, self.port, self.any_unit = host, port, any_unit
        self._server: Optional[asyncio.base_events.Server] = None
        self.requests = 0
        self._writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._client, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            for w in list(self._writers):      # wait_closed waits for open connections
                w.close()
            await self._server.wait_closed()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        try:
            while True:
                head = await reader.readexactly(7)
                tid, proto, length, unit = struct.unpack(">HHHB", head)
                pdu = await reader.readexactly(length - 1)
                self.requests += 1
                answer = self.handle(unit, pdu)
                if answer is None:
                    continue
                writer.write(struct.pack(">HHHB", tid, 0, len(answer) + 1, unit) + answer)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self._writers.discard(writer)
            writer.close()

    def handle(self, unit: int, pdu: bytes) -> Optional[bytes]:
        fc = pdu[0]
        bank = self.banks.get(unit) or (next(iter(self.banks.values())) if self.any_unit else None)
        if bank is None:
            return bytes([fc | 0x80, 11])                      # gateway target failed to respond

        def err(code: int) -> bytes:
            return bytes([fc | 0x80, code])
        try:
            if fc in (1, 2, 3, 4):
                start, count = struct.unpack(">HH", pdu[1:5])
                area = {1: "coil", 2: "discrete", 3: "holding", 4: "input"}[fc]
                if count < 1 or count > (2000 if fc <= 2 else 125):
                    return err(3)
                if not bank.exists(area, start, count):
                    return err(2)
                data = getattr(bank, area)
                if fc <= 2:
                    out = bytearray((count + 7) // 8)
                    for i in range(count):
                        if data.get(start + i):
                            out[i // 8] |= 1 << (i % 8)
                    return bytes([fc, len(out)]) + bytes(out)
                words = [data.get(start + i, 0) for i in range(count)]
                return bytes([fc, 2 * count]) + struct.pack(f">{count}H", *words)
            if fc == 5:
                addr, val = struct.unpack(">HH", pdu[1:5])
                if not bank.exists("coil", addr, 1):
                    return err(2)
                bank.coil[addr] = val == 0xFF00
                return pdu[:5]
            if fc == 6:
                addr, val = struct.unpack(">HH", pdu[1:5])
                if not bank.exists("holding", addr, 1):
                    return err(2)
                bank.holding[addr] = val
                return pdu[:5]
            if fc == 15:
                start, count, n = struct.unpack(">HHB", pdu[1:6])
                if not bank.exists("coil", start, count):
                    return err(2)
                for i in range(count):
                    bank.coil[start + i] = bool(pdu[6 + i // 8] >> (i % 8) & 1)
                return pdu[:5]
            if fc == 16:
                start, count, n = struct.unpack(">HHB", pdu[1:6])
                if not bank.exists("holding", start, count):
                    return err(2)
                bank.set_words("holding", start, list(struct.unpack(f">{count}H", pdu[6:6 + 2 * count])))
                return pdu[:5]
            return err(1)
        except (struct.error, IndexError):
            return err(3)
