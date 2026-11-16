"""
Values around an event, shared by reference tables and alarm rules.

- snapshot(): the latest polled value of a list of signals, with a quality
  (ok · offline: server not connected, last known value · missing).
- RingBuffer: per owner (a table or a rule) one sample per step over the
  time before an event, so rows from before it can be written or mailed.
"""
from __future__ import annotations

import math
from collections import deque
from datetime import datetime
from typing import TYPE_CHECKING, Iterable

from app.references.capture import signal_label
from app.references.store import RefValue

if TYPE_CHECKING:
    from app.state import RuntimeState

MAX_SIDE_ROWS = 300      # at most this many rows before and after an event


def side_rows(seconds: int, step: int) -> int:
    return min(MAX_SIDE_ROWS, math.ceil(max(0, seconds) / max(1, step)))


def snapshot(state: "RuntimeState", signals: Iterable[tuple[str, str]]) -> list[RefValue]:
    """Values of (subscription_id, unit) pairs from the latest poll; no server reads."""
    subs = {s.id: s for s in state.config.subscriptions.subscriptions}
    live = state.polling_engine.live_values() if state.polling_engine else {}
    out = []
    for sub_id, unit in signals:
        sub = subs.get(sub_id)
        v = RefValue(sub_id, signal_label(state, sub_id), unit)
        raw = live.get(sub_id)
        if sub is None or raw is None:
            v.quality = "missing"
        else:
            v.value = int(raw) if isinstance(raw, bool) else raw if isinstance(raw, (int, float)) else str(raw)
            status = state.server_status.get(sub.server_id)
            v.quality = "ok" if status and status.connected else "offline"
        out.append(v)
    return out


class RingBuffer:
    """Samples per owner; resets when the signals, step or length change."""

    def __init__(self) -> None:
        self._buf: dict[str, deque] = {}
        self._key: dict[str, tuple] = {}
        self._last: dict[str, datetime] = {}

    def sample(self, owner: str, now: datetime, state: "RuntimeState",
               signals: list[tuple[str, str]], pre_seconds: int, step: int) -> None:
        step = max(1, step)
        key = (tuple(signals), step, pre_seconds)
        if self._key.get(owner) != key:
            self._key[owner] = key
            # +1: the sample of the trigger tick itself belongs to the trigger row
            self._buf[owner] = deque(maxlen=side_rows(pre_seconds, step) + 1)
            self._last.pop(owner, None)
        if pre_seconds <= 0:
            return
        last = self._last.get(owner)
        if last is None or (now - last).total_seconds() >= step - 0.05:
            self._buf[owner].append((now, snapshot(state, signals)))
            self._last[owner] = now

    def before(self, owner: str, now: datetime) -> list[tuple[datetime, list[RefValue]]]:
        """Samples taken before ``now``, oldest first."""
        return [(ts, vals) for ts, vals in self._buf.get(owner, ()) if ts < now]

    def owners(self) -> list[str]:
        return list(self._key)

    def forget(self, owner: str) -> None:
        for d in (self._buf, self._key, self._last):
            d.pop(owner, None)
