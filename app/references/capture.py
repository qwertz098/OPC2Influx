"""Capture the current values of a reference table's signals."""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from app.config import ReferenceTableConfig, apply_name_strips
from app.references.store import RefValue

if TYPE_CHECKING:
    from app.state import RuntimeState

logger = logging.getLogger(__name__)


def signal_label(state: "RuntimeState", subscription_id: str) -> str:
    sub = next((s for s in state.config.subscriptions.subscriptions if s.id == subscription_id), None)
    if sub is None:
        return subscription_id
    if sub.display_name:
        return sub.display_name
    strips = next((s.name_strip_prefixes for s in state.config.servers.servers if s.id == sub.server_id), [])
    return apply_name_strips(sub.influxdb.measurement, strips)


def _plain(raw):
    """Numbers stay numbers (bool → 0 / 1), anything else becomes text."""
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, (int, float)):
        return raw
    return None if raw is None else str(raw)


async def capture(state: "RuntimeState", table: ReferenceTableConfig) -> list[RefValue]:
    """The latest polled value of every signal; a signal not polled yet is read
    from its server directly. Quality: ok · offline (server not connected, the
    last known value) · missing (no value)."""
    subs = {s.id: s for s in state.config.subscriptions.subscriptions}
    live = state.polling_engine.live_values() if state.polling_engine else {}
    values: list[RefValue] = []
    misses: dict[str, list[tuple[RefValue, str]]] = {}
    for sig in table.signals:
        sub = subs.get(sig.subscription_id)
        v = RefValue(subscription_id=sig.subscription_id, label=signal_label(state, sig.subscription_id),
                     unit=sig.unit)
        values.append(v)
        if sub is None:
            v.quality = "missing"
            continue
        status = state.server_status.get(sub.server_id)
        connected = bool(status and status.connected)
        if live.get(sub.id) is not None:
            v.value = _plain(live[sub.id])
            v.quality = "ok" if connected else "offline"
        elif connected and state.opcua_manager is not None:
            misses.setdefault(sub.server_id, []).append((v, sub.node_id))
        else:
            v.quality = "missing"

    for server_id, items in misses.items():
        try:
            raws = await asyncio.wait_for(
                state.opcua_manager.read_node_values(server_id, [n for _, n in items]), timeout=2.0)
            for (v, _), raw in zip(items, raws):
                v.value = _plain(raw)
                v.quality = "ok" if v.value is not None else "missing"
        except Exception as exc:
            logger.debug("reference read %s: %s", server_id, exc)
            for v, _ in items:
                v.quality = "missing"
    return values
