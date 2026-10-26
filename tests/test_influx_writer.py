"""Unit tests for InfluxWriterPool fan-out and failover routing."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import InfluxTargetConfig, BatchConfig, InfluxDBConfig, AppConfig, ServersConfig, SubscriptionsConfig, AlertsConfig
from app.influxdb.writer import InfluxWriterPool, InfluxTargetWriter, WritePoint
from app.state import RuntimeState, InfluxTargetStatus


def _make_target(id_, mode="always", priority=1) -> InfluxTargetConfig:
    return InfluxTargetConfig(
        id=id_, display_name=id_, url="http://localhost:8086",
        token="tok", org="myorg", bucket="opcua",
        mode=mode, priority=priority, enabled=True,
        batch=BatchConfig(size=10, flush_interval_ms=100, max_retries=1),
    )


def _make_state(*targets):
    config = AppConfig(
        servers=ServersConfig(),
        subscriptions=SubscriptionsConfig(),
        influxdb=InfluxDBConfig(targets=list(targets)),
        alerts=AlertsConfig(),
    )
    return RuntimeState(config_dir="/tmp", config=config)


@pytest.mark.asyncio
async def test_fanout_enqueues_to_all_always_targets():
    t1 = _make_target("t1", mode="always", priority=1)
    t2 = _make_target("t2", mode="always", priority=2)
    state = _make_state(t1, t2)

    pool = InfluxWriterPool(state)
    # Replace writers with mock writers
    m1 = MagicMock(); m1.cfg = t1; m1.status = InfluxTargetStatus(connected=True); m1.enqueue = AsyncMock()
    m2 = MagicMock(); m2.cfg = t2; m2.status = InfluxTargetStatus(connected=True); m2.enqueue = AsyncMock()
    pool._writers = {"t1": m1, "t2": m2}

    point = WritePoint("m", {}, "v", 1.0, datetime.now(timezone.utc))
    state.influx_status["t1"] = m1.status
    state.influx_status["t2"] = m2.status

    await pool.enqueue("m", {}, "v", 1.0, datetime.now(timezone.utc))

    m1.enqueue.assert_called_once()
    m2.enqueue.assert_called_once()


@pytest.mark.asyncio
async def test_failover_not_written_when_primary_ok():
    t1 = _make_target("t1", mode="always", priority=1)
    t2 = _make_target("t2", mode="failover", priority=2)
    state = _make_state(t1, t2)

    pool = InfluxWriterPool(state)
    m1 = MagicMock(); m1.cfg = t1; m1.status = InfluxTargetStatus(connected=True); m1.enqueue = AsyncMock()
    m2 = MagicMock(); m2.cfg = t2; m2.status = InfluxTargetStatus(connected=True); m2.enqueue = AsyncMock()
    pool._writers = {"t1": m1, "t2": m2}
    state.influx_status["t1"] = m1.status

    await pool.enqueue("m", {}, "v", 1.0, datetime.now(timezone.utc))

    m1.enqueue.assert_called_once()
    m2.enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_failover_activated_when_primary_down():
    t1 = _make_target("t1", mode="always", priority=1)
    t2 = _make_target("t2", mode="failover", priority=2)
    state = _make_state(t1, t2)

    pool = InfluxWriterPool(state)
    # Primary marked as disconnected
    m1 = MagicMock(); m1.cfg = t1; m1.status = InfluxTargetStatus(connected=False); m1.enqueue = AsyncMock()
    m2 = MagicMock(); m2.cfg = t2; m2.status = InfluxTargetStatus(connected=True); m2.enqueue = AsyncMock()
    pool._writers = {"t1": m1, "t2": m2}
    state.influx_status["t1"] = m1.status

    await pool.enqueue("m", {}, "v", 1.0, datetime.now(timezone.utc))

    m1.enqueue.assert_called_once()   # still attempted (fan-out)
    m2.enqueue.assert_called_once()   # failover activated


@pytest.mark.asyncio
async def test_queue_full_does_not_raise():
    """QueueFull should be caught and logged, not crash the pool."""
    t1 = _make_target("t1")
    state = _make_state(t1)
    status = InfluxTargetStatus()
    writer = InfluxTargetWriter(t1, status)
    # Fill queue to max
    writer._queue = asyncio.Queue(maxsize=1)
    writer._queue.put_nowait(WritePoint("m", {}, "v", 1.0, datetime.now(timezone.utc)))
    # This should not raise
    point = WritePoint("m", {}, "v", 2.0, datetime.now(timezone.utc))
    await writer.enqueue(point)  # Queue is full — should log and return
