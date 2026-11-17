"""
Values written to external Modbus devices.

Every entry names a source (a subscription's value or an alarm rule's state)
and a target on a Modbus server of the Servers page, whose connection is used.
A value is written when it changes and, with resend_seconds, again after that
time (so a restarted device gets every value back). Without a valid value
nothing is written; the optional status coil is written 0 then (1 while the
value is valid). Nothing is written while the master switch is off.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from app.config import ModbusWriteConfig, OutputPointConfig
from app.modbus.client import ModbusError
from app.outputs.modbus_out import to_words

if TYPE_CHECKING:
    from app.state import RuntimeState

logger = logging.getLogger(__name__)

TICK = 0.5


class WriteResult:
    __slots__ = ("value", "words", "status", "at", "error", "sent_at")

    def __init__(self) -> None:
        self.value: Any = None
        self.words: Optional[list] = None
        self.status: Optional[bool] = None
        self.at: Optional[datetime] = None     # last successful write
        self.error = ""
        self.sent_at = 0.0                     # monotonic time of the last write


def _as_point(t: ModbusWriteConfig) -> OutputPointConfig:
    """The output point that to_words understands."""
    return OutputPointConfig(area="coil" if t.area == "coil" else "holding", address=t.address,
                             data_type="bool" if t.area == "coil" else t.data_type, order=t.order, scale=t.scale)


class ModbusWriteService:
    def __init__(self, state: "RuntimeState") -> None:
        self._state = state
        self.results: dict[str, WriteResult] = {}

    @property
    def cfg(self):
        return self._state.config.outputs.modbus_writes

    async def run(self) -> None:
        while True:
            try:
                await asyncio.sleep(TICK)
                await self.tick()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error("Modbus write error: %s", exc)

    def _source(self, t: ModbusWriteConfig, now: datetime) -> tuple[Any, bool]:
        """(value, valid) of the entry's source."""
        state = self._state
        if t.kind == "alarm":
            rule = next((r for r in state.config.alarm_rules.rules if r.id == t.source_id), None)
            checker = getattr(state, "alarm_checker", None)
            if rule is None or not rule.enabled:
                return None, False
            return bool(checker and checker.is_active(rule, now)), True
        sub = next((s for s in state.config.subscriptions.subscriptions if s.id == t.source_id), None)
        live = state.polling_engine.live_values() if state.polling_engine else {}
        if sub is None or not sub.enabled or t.source_id not in live:
            return None, False
        return live[t.source_id], True

    async def tick(self, now: Optional[datetime] = None) -> None:
        if not self.cfg.enabled:
            return
        mgr = self._state.opcua_manager.modbus if self._state.opcua_manager else None
        if mgr is None:
            return
        now = now or datetime.now(timezone.utc)
        mono = time.monotonic()
        servers = {s.id: s for s in mgr.servers()}
        ids = set()
        for t in self.cfg.targets:
            ids.add(t.id)
            if not t.enabled:
                continue
            srv = servers.get(t.server_id)
            res = self.results.setdefault(t.id, WriteResult())
            if srv is None:
                res.error = "device not found (a Modbus server on the Servers page)"
                continue
            if not mgr.is_connected(srv.id):
                res.error = "device not connected" if srv.enabled else "device is disabled on the Servers page"
                continue
            value, valid = self._source(t, now)
            point = _as_point(t)
            point.order = t.order or srv.modbus.order
            words = to_words(point, value, point.order) if valid else None
            unit = srv.modbus.unit_id if t.unit_id is None else t.unit_id
            due = self.cfg.resend_seconds > 0 and mono - res.sent_at >= self.cfg.resend_seconds
            try:
                if words is not None and (words != res.words or due):
                    await mgr.write(srv.id, unit, t.area, t.address, words)
                    res.value, res.words, res.at, res.sent_at = value, words, now, mono
                if t.status_address is not None and (valid != res.status or due):
                    await mgr.write(srv.id, unit, "coil", t.status_address, [valid])
                    res.status = valid
                    if words is None:
                        res.sent_at = mono
                res.error = "" if valid else "no valid value: not written"
            except ModbusError as exc:
                res.error = str(exc)
            except ConnectionError as exc:
                res.error = f"write failed: {exc}"
        for gone in set(self.results) - ids:
            del self.results[gone]
