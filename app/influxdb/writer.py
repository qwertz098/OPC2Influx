"""
InfluxWriterPool — fan-out / failover write routing to multiple InfluxDB targets.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# Parses InfluxDB 422 body: extracts (measurement, field, got_type, stored_type) tuples
_CONFLICT_RE = re.compile(
    r'field type conflict: input field "([^"]+)" on measurement "([^"]+)" is type (\w+), already exists as type (\w+)'
)

from influxdb_client.client.influxdb_client_async import InfluxDBClientAsync
from influxdb_client.rest import ApiException

from app.config import InfluxTargetConfig, resolve_env, resolve_env_strict, UnresolvedEnvVarError
from app.state import RuntimeState, InfluxTargetStatus

logger = logging.getLogger(__name__)


@dataclass
class WritePoint:
    measurement: str
    tags: dict[str, str]
    field: str
    value: Any
    timestamp: datetime


# Line-protocol escaping, identical to influxdb_client.Point
_ESCAPE_MEASUREMENT = str.maketrans({",": r"\,", " ": r"\ ", "\n": r"\n", "\t": r"\t", "\r": r"\r"})
_ESCAPE_KEY = str.maketrans({",": r"\,", "=": r"\=", " ": r"\ ", "\n": r"\n", "\t": r"\t", "\r": r"\r"})
_ESCAPE_STRING = str.maketrans({'"': r'\"', "\\": r"\\"})
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_PREFIX_CACHE_MAX = 50_000


def _field_suffix(value: Any) -> str:
    if isinstance(value, bool):
        return "_bool"
    if isinstance(value, int):
        return "_integer"
    if isinstance(value, float):
        return "_float"
    return "_string"


def _escape_tag_value(value: str) -> str:
    ret = str(value).translate(_ESCAPE_KEY)
    return ret + " " if ret.endswith("\\") else ret


def _line_prefix(measurement: str, tags: tuple[tuple[str, str], ...], field: str) -> str:
    """'measurement,tag=v field=' — the part that is constant per series."""
    parts = [measurement.translate(_ESCAPE_MEASUREMENT)]
    for key, value in tags:
        k, v = str(key).translate(_ESCAPE_KEY), _escape_tag_value(value)
        if k and v:
            parts.append(f"{k}={v}")
    return ",".join(parts) + " " + str(field).translate(_ESCAPE_KEY) + "="


def _format_value(value: Any) -> str | None:
    """Field value as line protocol (None = not representable, point is dropped)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return f"{value}i"
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        s = str(value)
        return s[:-2] if s.endswith(".0") else s
    if isinstance(value, str):
        return '"' + value.translate(_ESCAPE_STRING) + '"'
    return None


def _timestamp_ns(ts: datetime) -> int:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta = ts - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1_000


class InfluxTargetWriter:
    """Manages writes to a single InfluxDB target."""

    FAILURE_THRESHOLD = 5  # consecutive failures before marking as unavailable

    def __init__(self, cfg: InfluxTargetConfig, status: InfluxTargetStatus,
                 on_alarm=None) -> None:
        self.cfg = cfg
        self.status = status
        self._on_alarm = on_alarm
        self._queue: asyncio.Queue[WritePoint] = asyncio.Queue(maxsize=5000)
        self._task: asyncio.Task | None = None
        self._client: InfluxDBClientAsync | None = None
        # Snapshot of all settings at creation — used to detect config changes
        self._snapshot = (cfg.model_dump(), resolve_env(cfg.token))
        # Rate-limit repeated schema-conflict log messages (log at most once per 60 s)
        self._last_schema_warn: float = 0.0
        # (measurement, tags, field) → escaped line prefix; series repeat every poll
        self._prefix_cache: dict[tuple, str] = {}

    def config_changed(self, cfg: InfluxTargetConfig) -> bool:
        return self._snapshot != (cfg.model_dump(), resolve_env(cfg.token))

    def _resolve_token(self) -> str:
        try:
            return resolve_env_strict(self.cfg.token)
        except UnresolvedEnvVarError as exc:
            msg = f"InfluxDB target '{self.cfg.display_name}': {exc}"
            logger.error(msg)
            self.status.last_error = str(exc)
            # Fall back to the unresolved literal so behavior degrades to the previous
            # (auth-failure) path rather than crashing writer startup outright.
            return resolve_env(self.cfg.token)

    async def start(self) -> None:
        self._client = InfluxDBClientAsync(
            url=self.cfg.url,
            token=self._resolve_token(),
            org=self.cfg.org,
        )
        self.status.connected = True  # Optimistic: assume reachable until writes fail
        self._task = asyncio.create_task(self._flush_loop(), name=f"influx-{self.cfg.id}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self._client:
            await self._client.close()

    async def enqueue(self, point: WritePoint) -> None:
        try:
            self._queue.put_nowait(point)
        except asyncio.QueueFull:
            logger.warning("InfluxDB queue full for target %s — dropping point", self.cfg.id)

    async def _flush_loop(self) -> None:
        flush_interval = self.cfg.batch.flush_interval_ms / 1000.0
        batch_size = self.cfg.batch.size

        while True:
            try:
                batch: list[WritePoint] = []
                deadline = asyncio.get_event_loop().time() + flush_interval

                # Collect up to batch_size items or until flush_interval expires.
                # Drain what is already queued without wait_for(), which would
                # create a timer task per point.
                while len(batch) < batch_size:
                    if not self._queue.empty():
                        batch.append(self._queue.get_nowait())
                        continue
                    remaining = deadline - asyncio.get_event_loop().time()
                    if remaining <= 0:
                        break
                    try:
                        item = await asyncio.wait_for(self._queue.get(), timeout=remaining)
                        batch.append(item)
                    except asyncio.TimeoutError:
                        break

                if batch:
                    await self._write_batch(batch)

            except asyncio.CancelledError:
                # Flush remaining on shutdown
                remaining_batch: list[WritePoint] = []
                while not self._queue.empty():
                    try:
                        remaining_batch.append(self._queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                if remaining_batch:
                    try:
                        await self._write_batch(remaining_batch)
                    except Exception:
                        pass
                return
            except Exception as exc:
                logger.error("InfluxDB flush loop error for %s: %s", self.cfg.id, exc)
                await asyncio.sleep(1)

    def _to_line(self, wp: WritePoint) -> str | None:
        """Serialize one point to line protocol, as influxdb_client.Point would.

        Native types are passed through unchanged:
          OPC UA Float/Double → Python float → InfluxDB float
          OPC UA Int*         → Python int   → InfluxDB integer
          OPC UA Boolean      → Python bool  → InfluxDB boolean
                                (or int 0/1 if bool_as_int is set on the server)
        With type_suffix the Python type is appended to the field name
        (e.g. value → value_float) to prevent InfluxDB schema conflicts.
        """
        value = _format_value(wp.value)
        if value is None:
            return None
        field = wp.field + _field_suffix(wp.value) if self.cfg.type_suffix else wp.field
        key = (wp.measurement, tuple(sorted(wp.tags.items())), field)
        prefix = self._prefix_cache.get(key)
        if prefix is None:
            if len(self._prefix_cache) >= _PREFIX_CACHE_MAX:
                self._prefix_cache.clear()
            prefix = self._prefix_cache[key] = _line_prefix(*key)
        return f"{prefix}{value} {_timestamp_ns(wp.timestamp)}"

    async def _write_batch(self, batch: list[WritePoint]) -> None:
        if self._client is None:
            return
        self.status.writes_attempted += len(batch)
        try:
            write_api = self._client.write_api()
            points = [line for line in map(self._to_line, batch) if line is not None]
            if points:
                await write_api.write(bucket=self.cfg.bucket, org=self.cfg.org, record=points)
            self.status.connected = True
            self.status.consecutive_failures = 0
            if self.status.started_at is None:
                self.status.started_at = datetime.now(timezone.utc)
            self.status.writes_total += len(batch)
            self.status.bytes_estimated += len(batch) * 150
            self.status.last_write = datetime.now(timezone.utc)
            self.status.last_error = None
        except ApiException as exc:
            if exc.status == 422:
                # Schema conflict — partial write: InfluxDB wrote all non-conflicting points
                # and dropped only the field(s) with a type mismatch.
                self.status.writes_partial += len(batch)
                self.status.last_write = datetime.now(timezone.utc)
                # Parse which measurement+field caused the conflict
                body_str = exc.body or ""
                if isinstance(body_str, bytes):
                    body_str = body_str.decode("utf-8", errors="replace")
                try:
                    msg = json.loads(body_str).get("message", body_str)
                except Exception:
                    msg = body_str
                for m in _CONFLICT_RE.finditer(msg):
                    field_name, measurement, got, stored = m.group(1), m.group(2), m.group(3), m.group(4)
                    entry = f"{measurement}.{field_name} (input: {got}, stored: {stored})"
                    if entry not in self.status.schema_conflicts:
                        self.status.schema_conflicts.append(entry)
                first_occurrence = self.status.last_error is None
                self.status.last_error = (
                    f"Schema conflict (422): field type mismatch — affected points dropped. "
                    f"Fix: in InfluxDB delete the conflicting field or re-create the bucket, "
                    f"then the app will write cleanly."
                )
                if first_occurrence and self._on_alarm:
                    self._on_alarm(
                        "error",
                        f"influx:{self.cfg.id}",
                        f"InfluxDB schema conflict in bucket '{self.cfg.bucket}': field type "
                        f"mismatch — affected points dropped. Delete the conflicting field in "
                        f"InfluxDB (Data Explorer → Delete) to resume clean writes."
                    )
                now = time.monotonic()
                if now - self._last_schema_warn >= 60.0:
                    self._last_schema_warn = now
                    logger.warning(
                        "InfluxDB schema conflict target=%s (partial write dropped): %s",
                        self.cfg.id, exc.body
                    )
            else:
                first_occurrence = self.status.last_error is None
                self.status.consecutive_failures += 1
                self.status.last_error = str(exc)
                if self.status.consecutive_failures >= self.FAILURE_THRESHOLD:
                    self.status.connected = False
                if first_occurrence and self._on_alarm:
                    self._on_alarm("error", f"influx:{self.cfg.id}",
                                   f"InfluxDB write error ({self.cfg.display_name}): {str(exc)[:120]}")
                logger.warning("InfluxDB write error target=%s: %s", self.cfg.id, exc)
                raise
        except Exception as exc:
            first_occurrence = self.status.last_error is None
            self.status.consecutive_failures += 1
            self.status.last_error = str(exc)
            if self.status.consecutive_failures >= self.FAILURE_THRESHOLD:
                self.status.connected = False
            if first_occurrence and self._on_alarm:
                self._on_alarm("error", f"influx:{self.cfg.id}",
                               f"InfluxDB write error ({self.cfg.display_name}): {str(exc)[:120]}")
            logger.warning("InfluxDB write error target=%s: %s", self.cfg.id, exc)
            raise


class InfluxWriterPool:
    """Routes write points to always-targets (fan-out) and failover-targets."""

    def __init__(self, state: RuntimeState) -> None:
        self._state = state
        self._writers: dict[str, InfluxTargetWriter] = {}

    async def start(self) -> None:
        for target in self._state.config.influxdb.targets:
            if target.enabled:
                status = InfluxTargetStatus()
                self._state.influx_status[target.id] = status
                writer = InfluxTargetWriter(target, status, on_alarm=self._state.add_alarm)
                await writer.start()
                self._writers[target.id] = writer

    async def stop(self) -> None:
        for writer in self._writers.values():
            await writer.stop()
        self._writers.clear()

    async def reconcile(self) -> None:
        """Called when InfluxDB config changes."""
        all_ids = {t.id for t in self._state.config.influxdb.targets}
        configured = {t.id: t for t in self._state.config.influxdb.targets if t.enabled}
        current = set(self._writers.keys())

        # Stop disabled/removed writers
        for tid in current - set(configured):
            await self._writers[tid].stop()
            del self._writers[tid]
            # Keep status in influx_status — only clear for fully deleted targets

        # Remove status only for fully deleted targets
        for tid in list(self._state.influx_status):
            if tid not in all_ids:
                self._state.influx_status.pop(tid, None)

        for tid, cfg in configured.items():
            if tid in self._writers and self._writers[tid].config_changed(cfg):
                # Settings changed (edit or backup restore) — restart writer
                await self._writers[tid].stop()
                del self._writers[tid]
            if tid not in self._writers:
                status = self._state.influx_status.get(tid) or InfluxTargetStatus()
                self._state.influx_status[tid] = status
                writer = InfluxTargetWriter(cfg, status, on_alarm=self._state.add_alarm)
                await writer.start()
                self._writers[tid] = writer

    async def enqueue(
        self,
        measurement: str,
        tags: dict[str, str],
        field: str,
        value: Any,
        timestamp: datetime,
    ) -> None:
        point = WritePoint(measurement=measurement, tags=tags, field=field,
                           value=value, timestamp=timestamp)

        always_writers: list[InfluxTargetWriter] = []
        failover_writers: list[InfluxTargetWriter] = []
        for w in self._writers.values():
            (always_writers if w.cfg.mode == "always" else failover_writers).append(w)
        if len(failover_writers) > 1:
            failover_writers.sort(key=lambda w: w.cfg.priority)

        if always_writers:
            # Fan-out to all always targets. Writer.enqueue only does a
            # put_nowait, so awaiting each in turn avoids gather()'s per-point
            # task creation.
            for w in always_writers:
                try:
                    await w.enqueue(point)
                except Exception as exc:
                    logger.debug("enqueue to %s failed: %s", w.cfg.id, exc)
            # Check if all always targets are down → activate failover
            all_always_down = all(not w.status.connected for w in always_writers)
            if all_always_down and failover_writers:
                await self._first_available(failover_writers).enqueue(point)
        elif failover_writers:
            await self._first_available(failover_writers).enqueue(point)

    @staticmethod
    def _first_available(writers: list[InfluxTargetWriter]) -> InfluxTargetWriter:
        return next((w for w in writers if w.status.connected), writers[0])
