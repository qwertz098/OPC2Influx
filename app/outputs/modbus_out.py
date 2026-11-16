"""
The app as a Modbus TCP server.

The register map (outputs.yaml) maps:
- value points: a subscription's latest value (holding / input registers, or
  coils / discrete inputs for on/off), with an optional status bit (discrete
  input, 1 = the value is valid: subscription on, server connected, value read);
  without a valid value the register keeps its last value;
- alarm points: 1 while an alarm rule's condition holds (after its delay);
- received points: registers clients write into (holding registers / coils)
  when the server is writable; values can be recorded to InfluxDB.
Every other write is refused (illegal data address), all of them when the
server is not writable (illegal function).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from app.config import OutputPointConfig
from app.modbus.codec import AREAS, TYPES, decode, encode, register_count
from app.modbus.server import ModbusTcpServer, RegisterBank

if TYPE_CHECKING:
    from app.state import RuntimeState

logger = logging.getLogger(__name__)

INT_RANGES = {"int16": (-2**15, 2**15 - 1), "uint16": (0, 2**16 - 1), "int32": (-2**31, 2**31 - 1),
              "uint32": (0, 2**32 - 1), "int64": (-2**63, 2**63 - 1), "uint64": (0, 2**64 - 1)}


def point_type(p: OutputPointConfig) -> str:
    return "bool" if AREAS[p.area][1] else p.data_type


def footprint(p: OutputPointConfig) -> list[tuple[str, int]]:
    """(area, address) cells a point uses, its status bit included."""
    cells = [(p.area, p.address + i) for i in range(register_count(p.area, point_type(p)))]
    if p.status_address is not None and p.kind == "value":
        cells.append(("discrete", p.status_address))
    return cells


def overlaps(points: list[OutputPointConfig]) -> dict[str, str]:
    """point id → message for points that use a cell an earlier point already uses."""
    used: dict[tuple[str, int], OutputPointConfig] = {}
    out = {}
    for p in points:
        for cell in footprint(p):
            other = used.get(cell)
            if other is not None and other.id != p.id:
                out[p.id] = f"{cell[0]} {cell[1]} is also used by {label_of(other)}"
                break
        else:
            for cell in footprint(p):
                used[cell] = p
    return out


def label_of(p: OutputPointConfig, state: "RuntimeState | None" = None) -> str:
    if p.name:
        return p.name
    if state is not None:
        if p.kind == "value":
            from app.references.capture import signal_label
            return signal_label(state, p.source_id)
        if p.kind == "alarm":
            rule = next((r for r in state.config.alarm_rules.rules if r.id == p.source_id), None)
            if rule:
                return "Alarm: " + (rule.name or rule.subscription_id)
    return p.source_id or p.id


def to_words(p: OutputPointConfig, value: Any, order: str) -> Optional[list]:
    """Register words (or [bool] for bit areas) for a value; None if it cannot be shown."""
    if isinstance(value, str) or value is None:
        return None
    if AREAS[p.area][1]:
        return [bool(value)]
    v = float(value) * p.scale if not isinstance(value, bool) else (1.0 if value else 0.0) * p.scale
    if p.data_type in INT_RANGES:
        lo, hi = INT_RANGES[p.data_type]
        v = int(max(lo, min(hi, round(v))))
    elif p.data_type == "bool":
        v = 1 if v else 0
        return [v]
    return encode(v, p.data_type, p.order or order)


def from_words(p: OutputPointConfig, words: list, order: str) -> Any:
    if AREAS[p.area][1]:
        return bool(words[0])
    raw = decode(words, p.data_type, p.order or order)
    if isinstance(raw, bool):
        return raw
    v = raw / p.scale if p.scale not in (0, 1) else raw
    return round(v, 9) if isinstance(v, float) else v


class ModbusOutputService:
    def __init__(self, state: "RuntimeState") -> None:
        self._state = state
        self.bank = RegisterBank()
        self.bank.write_check = self._write_check
        self.bank.on_write = self._on_write
        self.server: Optional[ModbusTcpServer] = None
        self._key: Optional[tuple] = None
        self.error = ""
        self.received: dict[str, tuple[Any, datetime]] = {}     # point id → (value, when)
        self.values: dict[str, Any] = {}                         # point id → value shown

    @property
    def cfg(self):
        return self._state.config.outputs

    def status(self) -> dict:
        s = self.server
        return {"running": s is not None, "port": s.port if s else None, "clients": s.clients if s else 0,
                "requests": s.requests if s else 0, "writes": s.writes if s else 0, "error": self.error}

    async def run(self) -> None:
        while True:
            try:
                await self.sync()
                self.update()
                await asyncio.sleep(max(0.1, self.cfg.modbus_server.update_ms / 1000))
            except asyncio.CancelledError:
                await self.stop()
                return
            except Exception as exc:
                logger.error("Modbus output error: %s", exc)
                await asyncio.sleep(1)

    async def stop(self) -> None:
        if self.server:
            await self.server.stop()
            self.server = None
        self._key = None

    async def sync(self) -> None:
        """Start, stop or move the server to match the settings."""
        ms = self.cfg.modbus_server
        key = (ms.enabled, ms.host, ms.port)
        if key == self._key:
            return
        await self.stop()
        self._key = key
        self.error = ""
        if not ms.enabled:
            return
        server = ModbusTcpServer({1: self.bank}, host=ms.host, port=ms.port, any_unit=True)
        try:
            await server.start()
            self.server = server
            logger.info("Modbus server listening on %s:%s", ms.host, server.port)
        except OSError as exc:
            self.error = f"port {ms.port}: {exc.strerror or exc}"
            logger.error("Modbus server could not start on %s:%s: %s", ms.host, ms.port, exc)

    # ------------------------------------------------------------------
    def _points(self) -> list[OutputPointConfig]:
        bad = overlaps(self.cfg.points)
        return [p for p in self.cfg.points if p.enabled and p.id not in bad]

    def update(self, now: Optional[datetime] = None) -> None:
        """Write the current values into the register bank."""
        state = self._state
        now = now or datetime.now(timezone.utc)
        live = state.polling_engine.live_values() if state.polling_engine else {}
        subs = {s.id: s for s in state.config.subscriptions.subscriptions}
        rules = {r.id: r for r in state.config.alarm_rules.rules}
        checker = getattr(state, "alarm_checker", None)
        order = self.cfg.modbus_server.order
        points = self._points()
        keep = set()
        for p in points:
            keep.update(footprint(p))
            if p.kind == "received":
                continue
            value, valid = None, False
            if p.kind == "value":
                sub = subs.get(p.source_id)
                if sub is not None and sub.enabled and p.source_id in live:
                    value, valid = live[p.source_id], True
            elif p.kind == "alarm":
                rule = rules.get(p.source_id)
                if rule is not None:
                    value = bool(checker and checker.is_active(rule, now))
                    valid = rule.enabled
            words = to_words(p, value, order) if valid else None
            if words is None:
                valid = False                    # no valid value: the register keeps its last value
            else:
                if AREAS[p.area][1]:
                    getattr(self.bank, p.area)[p.address] = words[0]
                else:
                    self.bank.set_words(p.area, p.address, words)
                self.values[p.id] = value
            if p.status_address is not None and p.kind == "value":
                self.bank.discrete[p.status_address] = valid
        # cells no longer in the map read 0 again
        for area in AREAS:
            data = getattr(self.bank, area)
            for addr in [a for a in data if (area, a) not in keep]:
                del data[addr]

    # ------------------------------------------------------------------
    def _received_at(self, area: str, address: int) -> Optional[OutputPointConfig]:
        for p in self._points():
            if p.kind == "received" and p.area == area and \
                    p.address <= address < p.address + register_count(p.area, point_type(p)):
                return p
        return None

    def _write_check(self, area: str, start: int, count: int) -> Optional[int]:
        if not self.cfg.modbus_server.writable:
            return 1                                           # illegal function
        for a in range(start, start + count):
            if self._received_at(area, a) is None:
                return 2                                       # illegal data address
        return None

    def _on_write(self, area: str, start: int, values: list) -> None:
        now = datetime.now(timezone.utc)
        order = self.cfg.modbus_server.order
        done = set()
        for a in range(start, start + len(values)):
            p = self._received_at(area, a)
            if p is None or p.id in done:
                continue
            done.add(p.id)
            n = register_count(p.area, point_type(p))
            words = [getattr(self.bank, area).get(p.address + i, 0) for i in range(n)]
            value = from_words(p, words, order)
            self.received[p.id] = (value, now)
            self.values[p.id] = value
            logger.info("Modbus write: %s = %s", label_of(p), value)
            if p.record and self._state.influx_pool is not None:
                asyncio.get_running_loop().create_task(self._state.influx_pool.enqueue(
                    measurement=p.name or p.id, tags={"source": "modbus_write"}, field="value",
                    value=value, timestamp=now))
