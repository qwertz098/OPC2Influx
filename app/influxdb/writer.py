"""
InfluxWriterPool — fan-out / failover write routing to multiple InfluxDB targets.
"""
from __future__ import annotations

import asyncio
import json
import logging
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
from influxdb_client import Point
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
        # Snapshot of connection params at creation — used to detect config changes
        self._conn_snapshot = (cfg.url, resolve_env(cfg.token), cfg.org, cfg.bucket)
        # Rate-limit repeated schema-conflict log messages (log at most once per 60 s)
        self._last_schema_warn: float = 0.0

    def connection_changed(self) -> bool:
        return self._conn_snapshot != (
            self.cfg.url, resolve_env(self.cfg.token), self.cfg.org, self.cfg.bucket
        )

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

                # Collect up to batch_size items or until flush_interval expires
                while len(batch) < batch_size:
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

    async def _write_batch(self, batch: list[WritePoint]) -> None:
        if self._client is None:
            return
        self.status.writes_attempted += len(batch)
        try:
            write_api = self._client.write_api()
            points = []
            for wp in batch:
                p = Point(wp.measurement)
                for k, v in wp.tags.items():
                    p = p.tag(k, v)
                # Native types are passed through unchanged:
                #   OPC UA Float/Double → Python float  → InfluxDB float
                #   OPC UA Int*         → Python int    → InfluxDB integer
                #   OPC UA Boolean      → Python bool   → InfluxDB boolean
                #                         (or int 0/1 if bool_as_int is set on the server)
                # When type_suffix is enabled, append the Python type to the field name
                # (e.g. value → value_float) to prevent InfluxDB schema conflicts.
                if self.cfg.type_suffix:
                    if isinstance(wp.value, bool):
                        field_name = wp.field + '_bool'
                    elif isinstance(wp.value, int):
                        field_name = wp.field + '_integer'
                    elif isinstance(wp.value, float):
                        field_name = wp.field + '_float'
                    else:
                        field_name = wp.field + '_string'
                else:
                    field_name = wp.field
                p = p.field(field_name, wp.value)
                p = p.time(wp.timestamp)
                points.append(p)

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
            if tid in self._writers and self._writers[tid].connection_changed():
                # Connection params changed (e.g. new token) — restart writer
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

        always_writers = sorted(
            [w for tid, w in self._writers.items() if w.cfg.mode == "always"],
            key=lambda w: w.cfg.priority,
        )
        failover_writers = sorted(
            [w for tid, w in self._writers.items() if w.cfg.mode == "failover"],
            key=lambda w: w.cfg.priority,
        )

        if always_writers:
            # Fan-out to all always targets
            await asyncio.gather(
                *[w.enqueue(point) for w in always_writers],
                return_exceptions=True,
            )
            # Check if all always targets are down → activate failover
            all_always_down = all(not w.status.connected for w in always_writers)
            if all_always_down and failover_writers:
                await failover_writers[0].enqueue(point)
        elif failover_writers:
            # No always targets — use first available failover
            for w in failover_writers:
                await w.enqueue(point)
                break
