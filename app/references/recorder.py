"""
Automatic captures for reference tables.

Every second, for each table with an enabled trigger:
- the values of its signals go into a ring buffer (one sample per step) that
  covers the pre-trigger time, so rows from before the event can be written;
- the trigger condition is evaluated like an alarm rule. When it becomes true
  (and has held for the delay), one event is written: the buffered rows before
  it, the trigger row and, over the following seconds, rows after it. All rows
  of an event share its number; each row notes its offset to the trigger.
  The next event needs the condition to clear after the rows after the event
  are written. A condition that is already true when the app starts or the
  trigger is set up does not count.
"""
from __future__ import annotations

import asyncio
import logging
import math
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from app.alerting.rule_checker import _evaluate
from app.config import ReferenceTableConfig
from app.references import store
from app.references.capture import capture, signal_label
from app.references.store import RefValue

if TYPE_CHECKING:
    from app.state import RuntimeState

logger = logging.getLogger(__name__)

MAX_SIDE_ROWS = 300      # at most this many rows before and after a trigger


def side_rows(seconds: int, step: int) -> int:
    return min(MAX_SIDE_ROWS, math.ceil(max(0, seconds) / max(1, step)))


def snapshot(state: "RuntimeState", table: ReferenceTableConfig) -> list[RefValue]:
    """The table's values from the latest poll (no server reads: fast, every second)."""
    subs = {s.id: s for s in state.config.subscriptions.subscriptions}
    live = state.polling_engine.live_values() if state.polling_engine else {}
    out = []
    for sig in table.signals:
        sub = subs.get(sig.subscription_id)
        v = RefValue(sig.subscription_id, signal_label(state, sig.subscription_id), sig.unit)
        raw = live.get(sig.subscription_id)
        if sub is None or raw is None:
            v.quality = "missing"
        else:
            v.value = int(raw) if isinstance(raw, bool) else raw if isinstance(raw, (int, float)) else str(raw)
            status = state.server_status.get(sub.server_id)
            v.quality = "ok" if status and status.connected else "offline"
        out.append(v)
    return out


class ReferenceRecorder:
    TICK = 1.0

    def __init__(self) -> None:
        self.buffers: dict[str, deque] = {}        # table → deque[(ts, values)]
        self.buffer_key: dict[str, tuple] = {}
        self.last_sample: dict[str, datetime] = {}
        self.trigger_key: dict[str, tuple] = {}
        self.armed: dict[str, bool] = {}           # True: the condition was false, may trigger
        self.since: dict[str, datetime] = {}       # condition true since (delay)
        self.busy: set[str] = set()                # tables writing post-trigger rows
        self._tasks: set[asyncio.Task] = set()

    async def run(self, state: "RuntimeState") -> None:
        while True:
            try:
                await asyncio.sleep(self.TICK)
                await self.tick(state)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error("ReferenceRecorder error: %s", exc)

    def _forget(self, table_id: str) -> None:
        for d in (self.buffers, self.buffer_key, self.last_sample, self.trigger_key, self.armed, self.since):
            d.pop(table_id, None)

    async def tick(self, state: "RuntimeState", now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        live = state.polling_engine.live_values() if state.polling_engine else {}
        active = set()
        for table in state.config.references.tables:
            trig = table.trigger
            if not (trig.enabled and trig.subscription_id and table.signals):
                continue
            active.add(table.id)
            step = max(1, trig.step_seconds)

            # Ring buffer for the rows before a trigger
            bkey = (tuple(s.subscription_id for s in table.signals), step, trig.pre_seconds)
            if self.buffer_key.get(table.id) != bkey:
                self.buffer_key[table.id] = bkey
                # +1: the sample of the trigger tick itself belongs to the trigger row
                self.buffers[table.id] = deque(maxlen=side_rows(trig.pre_seconds, step) + 1)
                self.last_sample.pop(table.id, None)
            if trig.pre_seconds > 0:
                last = self.last_sample.get(table.id)
                if last is None or (now - last).total_seconds() >= step - 0.05:
                    self.buffers[table.id].append((now, snapshot(state, table)))
                    self.last_sample[table.id] = now

            # Trigger condition (rising edge, after the delay)
            tkey = (trig.subscription_id, trig.condition, trig.threshold, trig.delay_seconds)
            if self.trigger_key.get(table.id) != tkey:      # new or changed trigger: observe first
                self.trigger_key[table.id] = tkey
                self.armed.pop(table.id, None)
                self.since.pop(table.id, None)
            if table.id in self.busy:
                continue        # rows after an event are still being written: no re-arming meanwhile
            raw = live.get(trig.subscription_id)
            if raw is None:
                continue
            met = _evaluate(raw, trig.condition, trig.threshold)
            if table.id not in self.armed:                  # first value: an already true condition does not count
                self.armed[table.id] = not met
            if not met:
                self.armed[table.id] = True
                self.since.pop(table.id, None)
                continue
            since = self.since.setdefault(table.id, now)
            if (now - since).total_seconds() < trig.delay_seconds:
                continue
            if not self.armed[table.id]:
                continue
            self.armed[table.id] = False
            await self._fire(state, table, now, raw)

        for table_id in list(self.buffer_key):              # disabled or removed tables
            if table_id not in active:
                self._forget(table_id)

    async def _fire(self, state: "RuntimeState", table: ReferenceTableConfig, now: datetime, raw) -> None:
        trig = table.trigger
        event = store.next_event(table.id)
        for ts, values in list(self.buffers.get(table.id, ())):
            if ts < now:
                store.add_entry(table.id, values, ts=ts, event=event,
                                offset_s=round((ts - now).total_seconds(), 1))
        label = signal_label(state, trig.subscription_id)
        comment = f"Auto: {label} {trig.condition} {trig.threshold} ({raw})"
        store.add_entry(table.id, await capture(state, table), comment=comment, ts=now, event=event, offset_s=0.0)
        state.add_alarm("info", "reference", f"{table.name}: event {event} captured ({comment[6:]})")
        logger.info("Reference table %s: event %s (%s)", table.name, event, comment)
        rows = side_rows(trig.post_seconds, trig.step_seconds)
        if rows:
            self.busy.add(table.id)
            task = asyncio.create_task(self._post(state, table.id, event, now, max(1, trig.step_seconds), rows))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _post(self, state: "RuntimeState", table_id: str, event: int, t0: datetime, step: int, rows: int) -> None:
        try:
            for k in range(1, rows + 1):
                due = t0 + timedelta(seconds=k * step)
                await asyncio.sleep(max(0.0, (due - datetime.now(timezone.utc)).total_seconds()))
                table = next((t for t in state.config.references.tables if t.id == table_id), None)
                if table is None:
                    return                                   # table deleted meanwhile
                store.add_entry(table_id, snapshot(state, table), ts=due, event=event, offset_s=float(k * step))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Reference table %s: rows after event %s failed: %s", table_id, event, exc)
        finally:
            self.busy.discard(table_id)
