"""
A small Modbus TCP server over an in-memory register bank.

Function codes 1–6, 15 and 16. Used by the tests and the demo; the base for
publishing values over Modbus later.
"""
from __future__ import annotations

import asyncio
import logging
import struct
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class RegisterBank:
    """Coils, discrete inputs, input and holding registers of one unit (65536 each)."""

    def __init__(self) -> None:
        self.coil: dict[int, bool] = {}
        self.discrete: dict[int, bool] = {}
        self.input: dict[int, int] = {}
        self.holding: dict[int, int] = {}
        self.valid: Optional[set[tuple[str, int]]] = None   # None: every address exists
        # Writes: check(area, start, count) → None to allow, else a Modbus exception code;
        # on_write(area, start, values) after a write was stored
        self.write_check: Optional[Callable[[str, int, int], Optional[int]]] = None
        self.on_write: Optional[Callable[[str, int, list], None]] = None

    def exists(self, area: str, start: int, count: int) -> bool:
        if start < 0 or start + count > 65536:
            return False
        return self.valid is None or all((area, a) in self.valid for a in range(start, start + count))

    def writable(self, area: str, start: int, count: int) -> Optional[int]:
        if not self.exists(area, start, count):
            return 2
        return self.write_check(area, start, count) if self.write_check else None

    def written(self, area: str, start: int, values: list) -> None:
        if self.on_write:
            try:
                self.on_write(area, start, values)
            except Exception as exc:
                logger.warning("Modbus write handler failed: %s", exc)

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
        self.writes = 0

    @property
    def clients(self) -> int:
        return len(self._writers)

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
            if fc in (5, 6, 15, 16):
                area = "coil" if fc in (5, 15) else "holding"
                if fc in (5, 6):
                    start, raw = struct.unpack(">HH", pdu[1:5])
                    if fc == 5 and raw not in (0, 0xFF00):
                        return err(3)
                    values = [raw == 0xFF00] if fc == 5 else [raw]
                else:
                    start, count, n = struct.unpack(">HHB", pdu[1:6])
                    if count < 1 or count > (1968 if fc == 15 else 123):
                        return err(3)
                    if fc == 15:
                        values = [bool(pdu[6 + i // 8] >> (i % 8) & 1) for i in range(count)]
                    else:
                        values = list(struct.unpack(f">{count}H", pdu[6:6 + 2 * count]))
                code = bank.writable(area, start, len(values))
                if code:
                    return err(code)
                for i, v in enumerate(values):
                    getattr(bank, area)[start + i] = v
                self.writes += 1
                bank.written(area, start, values)
                return pdu[:5]
            return err(1)
        except (struct.error, IndexError):
            return err(3)
