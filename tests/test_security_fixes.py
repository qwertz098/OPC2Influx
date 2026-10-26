"""Regression tests for data-viewer query validation and failover routing."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import aiosqlite
import pytest

from app.influxdb.writer import InfluxWriterPool


@pytest.mark.asyncio
async def test_data_query_rejects_unconfigured_db(async_client, tmp_path):
    db = tmp_path / "other.db"
    async with aiosqlite.connect(db) as conn:
        await conn.execute("CREATE TABLE t (time TEXT, v REAL)")
        await conn.commit()
    resp = await async_client.get("/data/query", params={"db": str(db), "table": "t", "col": "v"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_data_query_rejects_injected_identifiers(async_client, tmp_path):
    from app.config import SQLiteTargetConfig
    import app.main as main_mod

    db = tmp_path / "snap.db"
    async with aiosqlite.connect(db) as conn:
        await conn.execute("CREATE TABLE t (time TEXT, v REAL)")
        await conn.execute("INSERT INTO t VALUES ('2026-01-01', 1.5)")
        await conn.commit()
    main_mod.app.state.runtime.config.sqlite.targets.append(SQLiteTargetConfig(file_path=str(db)))

    ok = await async_client.get("/data/query", params={"db": str(db), "table": "t", "col": "v"})
    assert ok.json()["series"] == [{"t": "2026-01-01", "v": 1.5}]
    bad = await async_client.get(
        "/data/query",
        params={"db": str(db), "table": "t", "col": "v]) FROM sqlite_master--"},
    )
    assert bad.status_code == 400


class _FakeWriter:
    def __init__(self, mode, priority, connected):
        self.cfg = SimpleNamespace(mode=mode, priority=priority)
        self.status = SimpleNamespace(connected=connected)
        self.points = []

    async def enqueue(self, point):
        self.points.append(point)


@pytest.mark.asyncio
async def test_failover_skips_disconnected_target():
    pool = InfluxWriterPool(SimpleNamespace())
    down, up = _FakeWriter("failover", 1, False), _FakeWriter("failover", 2, True)
    pool._writers = {"a": down, "b": up}
    await pool.enqueue("m", {}, "value", 1.0, datetime.now(timezone.utc))
    assert not down.points and len(up.points) == 1
