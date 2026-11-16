"""
A small asynchronous Modbus TCP client: read coils, discrete inputs,
input and holding registers (function codes 1 to 4).

One request at a time per connection. A Modbus exception (e.g. illegal
address) raises ModbusError and keeps the connection; a timeout or a broken
connection raises ConnectionError and closes it.
"""
from __future__ import annotations

import asyncio
import struct
from typing import Optional

EXCEPTION_TEXT = {
    1: "illegal function", 2: "illegal data address", 3: "illegal data value",
    4: "server device failure", 5: "acknowledge", 6: "server device busy",
    10: "gateway path unavailable", 11: "gateway target device failed to respond",
}


class ModbusError(Exception):
    """The device answered with a Modbus exception."""

    def __init__(self, function: int, code: int) -> None:
        self.function, self.code = function, code
        super().__init__(f"Modbus exception {code} ({EXCEPTION_TEXT.get(code, 'unknown')}) "
                         f"for function {function}")


def parse_endpoint(url: str) -> tuple[str, int]:
    """'modbus-tcp://host:502', 'host:502' or 'host' → (host, port)."""
    rest = url.split("://", 1)[-1].strip().rstrip("/")
    if rest.startswith("["):                                 # [IPv6]:port
        host, _, port = rest[1:].partition("]")
        port = port.lstrip(":")
    elif rest.count(":") == 1:
        host, port = rest.split(":")
    else:
        host, port = rest, ""
    return host, int(port) if port else 502


class ModbusTcpClient:
    def __init__(self, host: str, port: int = 502, timeout: float = 2.0) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._lock = asyncio.Lock()
        self._tid = 0
        self.closed = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self.closed.is_set()

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=self.timeout)
        self.closed.clear()

    async def close(self) -> None:
        w, self._writer, self._reader = self._writer, None, None
        self.closed.set()
        if w is not None:
            try:
                w.close()
                await asyncio.wait_for(w.wait_closed(), timeout=1.0)
            except Exception:
                pass

    async def _request(self, unit: int, pdu: bytes) -> bytes:
        """Send one PDU, return the response PDU (function code first)."""
        async with self._lock:
            if not self.connected:
                raise ConnectionError("not connected")
            self._tid = (self._tid + 1) & 0xFFFF
            tid = self._tid
            frame = struct.pack(">HHHB", tid, 0, len(pdu) + 1, unit & 0xFF) + pdu
            try:
                self._writer.write(frame)
                await self._writer.drain()
                while True:
                    head = await asyncio.wait_for(self._reader.readexactly(7), timeout=self.timeout)
                    rtid, proto, length, _unit = struct.unpack(">HHHB", head)
                    if length < 2 or length > 260:
                        raise ConnectionError(f"bad frame length {length}")
                    body = await asyncio.wait_for(self._reader.readexactly(length - 1), timeout=self.timeout)
                    if rtid == tid and proto == 0:
                        break                  # else: a late answer to an earlier request, skip it
            except (asyncio.TimeoutError, asyncio.IncompleteReadError, OSError, ConnectionError) as exc:
                await self.close()
                text = "no answer (timeout)" if isinstance(exc, asyncio.TimeoutError) else str(exc) or type(exc).__name__
                raise ConnectionError(text) from exc
        if body[0] & 0x80:
            raise ModbusError(body[0] & 0x7F, body[1] if len(body) > 1 else 0)
        if body[0] != pdu[0]:
            raise ConnectionError(f"answer for function {body[0]}, expected {pdu[0]}")
        return body

    async def read(self, unit: int, function: int, address: int, count: int) -> list:
        """Function 1/2: list of bools; function 3/4: list of register words."""
        body = await self._request(unit, struct.pack(">BHH", function, address, count))
        data = body[2:2 + body[1]]
        if function in (1, 2):
            if len(data) < (count + 7) // 8:
                raise ConnectionError("short answer")
            return [bool(data[i // 8] >> (i % 8) & 1) for i in range(count)]
        if len(data) < 2 * count:
            raise ConnectionError("short answer")
        return list(struct.unpack(f">{count}H", data[:2 * count]))

    async def write_coils(self, unit: int, address: int, values: list[bool]) -> None:
        """Function 5 for one coil, 15 for several."""
        if len(values) == 1:
            await self._request(unit, struct.pack(">BHH", 5, address, 0xFF00 if values[0] else 0))
            return
        data = bytearray((len(values) + 7) // 8)
        for i, v in enumerate(values):
            if v:
                data[i // 8] |= 1 << (i % 8)
        await self._request(unit, struct.pack(">BHHB", 15, address, len(values), len(data)) + bytes(data))

    async def write_registers(self, unit: int, address: int, words: list[int]) -> None:
        """Function 6 for one register, 16 for several."""
        if len(words) == 1:
            await self._request(unit, struct.pack(">BHH", 6, address, words[0] & 0xFFFF))
            return
        await self._request(unit, struct.pack(f">BHHB{len(words)}H", 16, address, len(words), 2 * len(words),
                                              *[w & 0xFFFF for w in words]))
