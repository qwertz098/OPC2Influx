"""
PollingEngine — one asyncio.Task per (server, interval) group of subscriptions.

All nodes of a group are read in a single OPC UA Read service call per cycle
(see OpcUaClientManager.read_node_values), which keeps the request count per
poll cycle constant instead of linear in the number of subscriptions.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.config import ServerConfig, SubscriptionConfig, PollingConfig, apply_name_strips
from app.state import RuntimeState

logger = logging.getLogger(__name__)


def should_write(new_val: Any, old_val: Any, mode: str, threshold: float) -> bool:
    """Pure function — determines whether a value should be written to InfluxDB."""
    if old_val is None:
        return True
    if mode == "always":
        return True
    if mode == "on_change":
        return new_val != old_val
    try:
        delta = abs(float(new_val) - float(old_val))
        if mode == "on_change_absolute":
            return delta > threshold
        if mode == "on_change_relative":
            base = abs(float(old_val)) if float(old_val) != 0 else 1.0
            return (delta / base * 100) > threshold
    except (TypeError, ValueError):
        return new_val != old_val
    return True


def _resolve_tags(sub: SubscriptionConfig, node_attrs: dict[str, str]) -> dict[str, str]:
    """Build the InfluxDB tags dict from a subscription's per-sub tag config."""
    tags: dict[str, str] = {}
    for tag_cfg in sub.influxdb.tags:
        if tag_cfg.value_type == "static":
            tags[tag_cfg.key] = tag_cfg.value
        elif tag_cfg.value_type == "node_attr":
            tags[tag_cfg.key] = node_attrs.get(tag_cfg.attr, "")
    return tags


def _absolute_browse_path(base_path: str, relative_path: str) -> str | None:
    """Resolve a relative OPC UA browse path against a base browse path.

    Syntax:
      ./tagName       → sibling of base
      ../tagName      → parent's child
      ../../Sub/tag   → 2 levels up then descend
    """
    parts = [p for p in base_path.split("/") if p]
    segments = relative_path.replace("\\", "/").split("/")
    for seg in segments:
        if seg == ".":
            continue
        elif seg == "..":
            if parts:
                parts.pop()
            else:
                return None
        else:
            parts.append(seg)
    return "/" + "/".join(parts) if parts else None


@dataclass
class _SubState:
    """Per-subscription write state, preserved across group-task restarts."""
    last_val: Any = None
    last_written_at: float = 0.0
    # Tracks the raw polled value regardless of write_mode — used by the
    # stale-value watchdog (app/alerting/stale_checker.py) to detect a signal
    # that hasn't changed within its configured window. Independent of
    # last_val/last_written_at, which only update when a write actually
    # happens (never, for write_mode="always" writes on every poll but
    # last_val would be overwritten too, so it's not equivalent).
    last_raw_value: Any = None
    last_changed_at: float = field(default_factory=time.monotonic)


class PollingEngine:
    def __init__(self, state: RuntimeState) -> None:
        self._state = state
        self._tasks: dict[tuple[str, int], asyncio.Task] = {}  # key: (server_id, interval_ms)
        self._groups: dict[tuple[str, int], list[SubscriptionConfig]] = {}
        self._node_attrs_cache: dict[tuple[str, str], dict[str, str]] = {}
        self._last_values: dict[str, Any] = {}           # sub.id → last read value
        self._tag_cache: dict[str, dict[str, str]] = {}  # sub.id → {tag_id: resolved_str}
        self._sub_state: dict[str, _SubState] = {}       # sub.id → write state
        self._unsupported_type_warned: set[tuple[str, str]] = set()  # (server_id, node_id) already logged
        # Lookup dicts rebuilt on every reconcile — avoid linear scans in hot loops.
        self._server_by_id: dict[str, ServerConfig] = {}
        self._node_id_by_path: dict[tuple[str, str], str] = {}

    async def reconcile(self) -> None:
        """Diff current group tasks against current config and update."""
        self._server_by_id = {s.id: s for s in self._state.config.servers.servers}
        self._node_id_by_path = {
            (s.server_id, s.browse_path): s.node_id
            for s in self._state.config.subscriptions.subscriptions
            if s.browse_path
        }

        groups: dict[tuple[str, int], list[SubscriptionConfig]] = {}
        for s in self._state.config.subscriptions.subscriptions:
            if s.enabled:
                groups.setdefault((s.server_id, s.polling.interval_ms), []).append(s)

        # Restart a group task when its membership changed (compared by object
        # identity, so a config restore with fresh objects also restarts).
        for key in set(self._tasks) | set(groups):
            old_subs = self._groups.get(key, [])
            new_subs = groups.get(key, [])
            unchanged = len(old_subs) == len(new_subs) and all(
                a is b for a, b in zip(old_subs, new_subs)
            )
            if unchanged and key in self._tasks and not self._tasks[key].done():
                continue
            task = self._tasks.pop(key, None)
            if task is not None:
                task.cancel()
            if key in groups:
                self._tasks[key] = asyncio.create_task(
                    self._poll_group_loop(key[0], key[1], groups[key]),
                    name=f"poll-{key[0]}-{key[1]}ms",
                )
        self._groups = groups

        # Drop write state of removed subscriptions
        active_ids = {s.id for g in groups.values() for s in g}
        for sid in set(self._sub_state) - active_ids:
            del self._sub_state[sid]

    async def _resolve_central_tags(self, sub: SubscriptionConfig, mgr: Any) -> dict[str, str]:
        """Resolve active central tags for a subscription; cache dynamic values on first poll."""
        result: dict[str, str] = {}
        if not sub.active_tags:
            return result
        all_tags = {t.id: t for t in self._state.config.tags.tags}
        sub_cache = self._tag_cache.setdefault(sub.id, {})
        for activation in sub.active_tags:
            if not activation.active:
                continue
            cfg = all_tags.get(activation.tag_id)
            if cfg is None:
                continue
            if not cfg.is_dynamic:
                result[cfg.key] = cfg.static_value
            else:
                if activation.tag_id not in sub_cache:
                    abs_path = _absolute_browse_path(sub.browse_path, cfg.relative_path)
                    resolved = ""
                    if abs_path:
                        node_id_for_tag = self._node_id_by_path.get((sub.server_id, abs_path))
                        if node_id_for_tag:
                            try:
                                val = await mgr.read_node_value(sub.server_id, node_id_for_tag)
                                resolved = str(val) if val is not None else ""
                            except Exception:
                                pass
                    sub_cache[activation.tag_id] = resolved
                cached = sub_cache.get(activation.tag_id, "")
                if cached:
                    result[cfg.key] = cached
        return result

    async def _poll_group_loop(
        self, server_id: str, interval_ms: int, subs: list[SubscriptionConfig]
    ) -> None:
        interval = interval_ms / 1000.0
        node_ids = [s.node_id for s in subs]

        while True:
            try:
                await asyncio.sleep(interval)

                mgr = self._state.opcua_manager
                if mgr is None or not mgr.is_connected(server_id):
                    continue

                # read_node_values chunks internally to stay within the
                # server's OperationLimits (e.g. WAGO MaxNodesPerRead=100).
                values = await mgr.read_node_values(server_id, node_ids)

                for sub, value in zip(subs, values):
                    try:
                        await self._process_value(sub, value, mgr)
                    except Exception as exc:
                        logger.debug("Poll error sub=%s: %s", sub.id, exc)

            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.debug("Poll error group=%s/%sms: %s", server_id, interval_ms, exc)

    async def _process_value(self, sub: SubscriptionConfig, value: Any, mgr: Any) -> None:
        """Handle one freshly read value for one subscription.

        Logic moved verbatim from the former per-subscription poll loop:
        Variant unwrap, string coercion, bool_as_int, unsupported-type skip,
        snapshot update, heartbeat / write-mode decision, tag resolution,
        InfluxDB enqueue.
        """
        if value is None:
            return
        now = time.monotonic()

        # Lazily cache node attributes
        cache_key = (sub.server_id, sub.node_id)
        if cache_key not in self._node_attrs_cache:
            try:
                self._node_attrs_cache[cache_key] = await mgr.read_node_attributes(
                    sub.server_id, sub.node_id
                )
            except Exception:
                self._node_attrs_cache[cache_key] = {}
        node_attrs = self._node_attrs_cache[cache_key]

        srv = self._server_by_id.get(sub.server_id)

        # Unwrap asyncua Variant/DataValue wrappers that may appear in edge cases
        if hasattr(value, 'Value') and not isinstance(value, bool):
            value = value.Value
        if hasattr(value, 'value') and not isinstance(value, bool):
            value = value.value
        if value is None:
            return
        # Coerce string values: "true"/"false" → bool, numeric strings → number, others → skip
        if isinstance(value, str):
            low = value.strip().lower()
            if low in ('true', 'false'):
                value = low == 'true'
            elif low:
                try:
                    value = float(low) if '.' in low else int(low)
                except (ValueError, TypeError):
                    return  # non-numeric string — skip to avoid InfluxDB type conflicts
            else:
                return  # empty string — skip
        if isinstance(value, bool) and srv and srv.bool_as_int:
            value = int(value)
        if not isinstance(value, (bool, int, float, str)):
            # Structured/array OPC UA types (e.g. ExtensionObject) have no direct
            # InfluxDB field representation — skip rather than crash the poll loop.
            if cache_key not in self._unsupported_type_warned:
                self._unsupported_type_warned.add(cache_key)
                path_hint = f" at browse path '{sub.browse_path}'" if sub.browse_path else ""
                logger.warning(
                    "Subscription %s (%s)%s: unsupported value type %s — skipping. "
                    "Subscribe to a scalar child node instead of a struct/array node.",
                    sub.id, sub.node_id, path_hint, type(value),
                )
            return
        self._last_values[sub.id] = value   # always update snapshot for SQLite

        st = self._sub_state.setdefault(sub.id, _SubState())
        if value != st.last_raw_value:
            st.last_raw_value = value
            st.last_changed_at = now

        heartbeat = sub.polling.heartbeat_interval_seconds
        heartbeat_due = heartbeat > 0 and (now - st.last_written_at) >= heartbeat

        if heartbeat_due or should_write(value, st.last_val, sub.polling.write_mode, sub.polling.threshold):
            # Per-subscription tags (node attrs etc.)
            tags = _resolve_tags(sub, node_attrs)
            if srv:
                for stat_tag in srv.static_tags:
                    tags.setdefault(stat_tag.key, stat_tag.value)
            # Central tags active for this subscription
            central = await self._resolve_central_tags(sub, mgr)
            tags.update(central)

            pool = self._state.influx_pool
            if pool is not None:
                strips = srv.name_strip_prefixes if srv else []
                measurement = apply_name_strips(sub.influxdb.measurement, strips)
                await pool.enqueue(
                    measurement=measurement,
                    tags=tags,
                    field=sub.influxdb.field_name,
                    value=value,
                    timestamp=datetime.now(timezone.utc),
                )
            st.last_written_at = now
            st.last_val = value

    async def stop_all(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()
        self._groups = {}
