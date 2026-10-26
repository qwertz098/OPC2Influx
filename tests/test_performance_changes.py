"""Tests for the performance refactor: batched OPC UA reads, grouped polling,
row-count caching, SQLite writer connection reuse, and HTTP transport tweaks."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import aiosqlite
import pytest
import pytest_asyncio
from asyncua import Client

from app.config import (
    AppConfig, ServersConfig, ServerConfig, SubscriptionsConfig, SubscriptionConfig,
    InfluxDBConfig, AlertsConfig, PollingConfig, SQLiteTargetConfig, SQLiteConfig,
)
from app.opcua.client import OpcUaClientManager
from app.opcua.poller import PollingEngine
from app.sqlite.writer import SQLiteWriter
from app.state import RuntimeState, ServerStatus

pytestmark = pytest.mark.asyncio(loop_scope="session")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state(tmp_path, servers=None, subscriptions=None, sqlite_targets=None) -> RuntimeState:
    config = AppConfig(
        servers=ServersConfig(servers=servers or []),
        subscriptions=SubscriptionsConfig(subscriptions=subscriptions or []),
        influxdb=InfluxDBConfig(),
        alerts=AlertsConfig(),
        sqlite=SQLiteConfig(targets=sqlite_targets or []),
    )
    return RuntimeState(config_dir=tmp_path, config=config)


@pytest_asyncio.fixture(loop_scope="session")
async def connected_mgr(opcua_server, tmp_path_factory):
    """OpcUaClientManager with a live client injected for the embedded server."""
    tmp = tmp_path_factory.mktemp("state")
    state = _make_state(tmp)
    mgr = OpcUaClientManager(state)
    client = Client(url=opcua_server._test_endpoint)
    await client.connect()
    mgr._clients["test"] = client
    state.server_status["test"] = ServerStatus(connected=True)
    yield mgr, opcua_server
    await client.disconnect()


class _RecordingPool:
    def __init__(self):
        self.points = []

    async def enqueue(self, **kwargs):
        self.points.append(kwargs)


class _FakeMgr:
    """Minimal manager stub for _process_value unit tests."""

    async def read_node_attributes(self, server_id, node_id):
        return {}

    async def read_node_value(self, server_id, node_id):
        return None


def _sub(server_id="srv1", node_id="ns=2;s=x", **polling_kwargs) -> SubscriptionConfig:
    return SubscriptionConfig(
        server_id=server_id,
        node_id=node_id,
        polling=PollingConfig(**polling_kwargs),
    )


# ---------------------------------------------------------------------------
# Batched reads (OpcUaClientManager.read_node_values)
# ---------------------------------------------------------------------------

async def test_read_node_values_matches_individual_reads(connected_mgr):
    mgr, server = connected_mgr
    speed = server._test_motor_nodes["Motor_0"]["Speed"]
    temp = server._test_motor_nodes["Motor_1"]["Temperature"]
    ids = [speed.nodeid.to_string(), temp.nodeid.to_string()]

    batch = await mgr.read_node_values("test", ids)
    individual = [await mgr.read_node_value("test", nid) for nid in ids]
    assert batch == individual
    assert all(isinstance(v, float) for v in batch)


async def test_read_node_values_bad_node_yields_none(connected_mgr):
    mgr, server = connected_mgr
    good = server._test_pressure.nodeid.to_string()
    values = await mgr.read_node_values("test", [good, "ns=2;s=does_not_exist"])
    assert isinstance(values[0], float)
    assert values[1] is None


async def test_node_cache_reuse_and_invalidation(connected_mgr):
    mgr, server = connected_mgr
    nid = server._test_pressure.nodeid.to_string()
    client = mgr._clients["test"]

    n1 = mgr._get_cached_node("test", nid, client)
    n2 = mgr._get_cached_node("test", nid, client)
    assert n1 is n2

    # Simulates what every disconnect/reconnect path does before swapping clients
    mgr._node_cache.pop("test", None)
    n3 = mgr._get_cached_node("test", nid, client)
    assert n3 is not n1


# ---------------------------------------------------------------------------
# PollingEngine group reconcile
# ---------------------------------------------------------------------------

async def test_reconcile_groups_by_server_and_interval(tmp_path):
    subs = [
        _sub("srv1", "ns=2;s=a", interval_ms=1000),
        _sub("srv1", "ns=2;s=b", interval_ms=1000),
        _sub("srv1", "ns=2;s=c", interval_ms=5000),
        _sub("srv2", "ns=2;s=d", interval_ms=1000),
    ]
    state = _make_state(tmp_path, subscriptions=subs)
    engine = PollingEngine(state)
    await engine.reconcile()
    try:
        assert set(engine._tasks) == {("srv1", 1000), ("srv1", 5000), ("srv2", 1000)}

        # Disabling a whole group removes its task; shrinking one restarts it
        subs[2].enabled = False
        subs[1].enabled = False
        await engine.reconcile()
        assert set(engine._tasks) == {("srv1", 1000), ("srv2", 1000)}
        assert [s.id for s in engine._groups[("srv1", 1000)]] == [subs[0].id]
    finally:
        await engine.stop_all()
    assert engine._tasks == {}


# ---------------------------------------------------------------------------
# PollingEngine._process_value (extracted per-value logic)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(loop_scope="session")
async def engine_with_pool(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("engine")
    srv = ServerConfig(id="srv1", display_name="S1", endpoint_url="opc.tcp://x")
    sub = _sub("srv1", "ns=2;s=x", write_mode="on_change", heartbeat_interval_seconds=0)
    state = _make_state(tmp, servers=[srv], subscriptions=[sub])
    engine = PollingEngine(state)
    await engine.reconcile()
    await engine.stop_all()  # only exercise _process_value directly
    pool = _RecordingPool()
    state.influx_pool = pool
    return engine, sub, srv, pool


async def test_process_value_on_change_suppression(engine_with_pool):
    engine, sub, srv, pool = engine_with_pool
    mgr = _FakeMgr()
    await engine._process_value(sub, 1.5, mgr)
    await engine._process_value(sub, 1.5, mgr)
    await engine._process_value(sub, 2.0, mgr)
    assert [p["value"] for p in pool.points] == [1.5, 2.0]
    assert engine._last_values[sub.id] == 2.0


async def test_process_value_heartbeat_forces_write(engine_with_pool):
    engine, sub, srv, pool = engine_with_pool
    mgr = _FakeMgr()
    sub.polling.heartbeat_interval_seconds = 60
    await engine._process_value(sub, 7.0, mgr)
    # Same value again — suppressed until the heartbeat is due
    await engine._process_value(sub, 7.0, mgr)
    assert len(pool.points) == 1
    engine._sub_state[sub.id].last_written_at = time.monotonic() - 120
    await engine._process_value(sub, 7.0, mgr)
    assert len(pool.points) == 2


async def test_process_value_bool_as_int(engine_with_pool):
    engine, sub, srv, pool = engine_with_pool
    mgr = _FakeMgr()
    srv.bool_as_int = True
    await engine._process_value(sub, True, mgr)
    assert pool.points[-1]["value"] == 1
    assert not isinstance(pool.points[-1]["value"], bool)


async def test_process_value_string_coercion_and_skip(engine_with_pool):
    engine, sub, srv, pool = engine_with_pool
    mgr = _FakeMgr()
    await engine._process_value(sub, "12.5", mgr)
    assert pool.points[-1]["value"] == 12.5
    before = len(pool.points)
    await engine._process_value(sub, "not_a_number", mgr)
    await engine._process_value(sub, "", mgr)
    assert len(pool.points) == before


async def test_process_value_unsupported_type_skipped(engine_with_pool):
    engine, sub, srv, pool = engine_with_pool
    mgr = _FakeMgr()
    before = len(pool.points)
    await engine._process_value(sub, [1, 2, 3], mgr)
    assert len(pool.points) == before
    assert engine._last_values.get(sub.id) != [1, 2, 3]


# ---------------------------------------------------------------------------
# /status row-count TTL cache
# ---------------------------------------------------------------------------

async def test_sqlite_total_rows_uses_ttl_cache(tmp_path, monkeypatch):
    from app.routers import dashboard

    target = SQLiteTargetConfig(file_path=str(tmp_path / "log.db"))
    state = _make_state(tmp_path, sqlite_targets=[target])

    calls = {"n": 0}

    async def fake_count(path):
        calls["n"] += 1
        return 42

    monkeypatch.setattr(dashboard, "_count_total_rows", fake_count)
    dashboard._row_count_cache.clear()

    r1 = await dashboard._sqlite_total_rows(state)
    r2 = await dashboard._sqlite_total_rows(state)
    assert r1 == r2 == {target.id: 42}
    assert calls["n"] == 1

    # Expired entry triggers a fresh count
    key = target.file_path
    ts, val = dashboard._row_count_cache[key]
    dashboard._row_count_cache[key] = (ts - dashboard._ROW_COUNT_TTL_S - 1, val)
    await dashboard._sqlite_total_rows(state)
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# SQLite writer: WAL, persistent connection, column cache, index
# ---------------------------------------------------------------------------

async def test_sqlite_writer_wal_index_and_columns(tmp_path):
    srv = ServerConfig(id="srv1", display_name="Line 1", endpoint_url="opc.tcp://x")
    sub1 = _sub("srv1", "ns=2;s=a")
    state = _make_state(tmp_path, servers=[srv], subscriptions=[sub1])
    live = {sub1.id: 3.14}
    state.polling_engine = SimpleNamespace(live_values=lambda: live)

    target = SQLiteTargetConfig(file_path=str(tmp_path / "snap.db"), write_interval_seconds=1)
    writer = SQLiteWriter(target, state)
    try:
        await writer._snapshot()
        db = writer._db
        assert db is not None

        mode = await (await db.execute("PRAGMA journal_mode")).fetchone()
        assert mode[0].lower() == "wal"
        idx = await (await db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%_time'"
        )).fetchall()
        assert idx, "time index missing"

        # New subscription between snapshots → column added via the cache path
        sub2 = _sub("srv1", "ns=2;s=b")
        sub2.influxdb.measurement = "other_measurement"
        state.config.subscriptions.subscriptions.append(sub2)
        live[sub2.id] = 99
        await writer._snapshot()
        assert writer._db is db, "connection must be reused across snapshots"

        rows = await (await db.execute("SELECT COUNT(*) FROM [Line_1]")).fetchone()
        assert rows[0] == 2
        cols = {r[1] for r in await (await db.execute("PRAGMA table_info([Line_1])")).fetchall()}
        assert len(cols) == 3  # time + 2 value columns
    finally:
        await writer._close_db()


# ---------------------------------------------------------------------------
# HTTP transport: gzip + static cache headers
# ---------------------------------------------------------------------------

async def test_gzip_and_static_cache_headers(async_client):
    resp = await async_client.get("/", headers={"Accept-Encoding": "gzip"})
    assert resp.status_code == 200
    assert resp.headers.get("content-encoding") == "gzip"

    css = await async_client.get("/static/css/app.css")
    assert css.status_code == 200
    assert "immutable" in css.headers.get("cache-control", "")


# ---------------------------------------------------------------------------
# Dedicated browse session (separate from the polling session)
# ---------------------------------------------------------------------------

async def test_get_browse_client_lazily_connects_separate_session(opcua_server, tmp_path):
    srv = ServerConfig(id="test", display_name="Test", endpoint_url=opcua_server._test_endpoint)
    state = _make_state(tmp_path, servers=[srv])
    mgr = OpcUaClientManager(state)
    # Main session is live (as in production, set up by _connect_loop)
    main_client = Client(url=opcua_server._test_endpoint)
    await main_client.connect()
    mgr._clients["test"] = main_client
    try:
        browse_client = await mgr.get_browse_client("test")
        assert browse_client is not None
        assert browse_client is not main_client, "browse session must be separate from the polling session"

        # Reused on subsequent calls, not reconnected each time
        again = await mgr.get_browse_client("test")
        assert again is browse_client

        # It's actually usable
        children = await browse_client.nodes.objects.get_children()
        assert children
    finally:
        await main_client.disconnect()
        for c in mgr._browse_clients.values():
            await c.disconnect()


async def test_get_browse_client_falls_back_when_connect_fails(tmp_path):
    # No matching ServerConfig — connect attempt inside get_browse_client
    # short-circuits to None, exercising the "srv is None" fallback path.
    state = _make_state(tmp_path, servers=[])
    mgr = OpcUaClientManager(state)
    mgr._clients["test"] = "main-session-sentinel"  # stand-in, never dereferenced here
    result = await mgr.get_browse_client("test")
    assert result == "main-session-sentinel"


async def test_discard_browse_client_forces_reconnect(opcua_server, tmp_path):
    srv = ServerConfig(id="test", display_name="Test", endpoint_url=opcua_server._test_endpoint)
    state = _make_state(tmp_path, servers=[srv])
    mgr = OpcUaClientManager(state)
    try:
        first = await mgr.get_browse_client("test")
        mgr.discard_browse_client("test")
        second = await mgr.get_browse_client("test")
        assert second is not first
    finally:
        for c in mgr._browse_clients.values():
            await c.disconnect()


# ---------------------------------------------------------------------------
# Stale-value tracking (_SubState.last_changed_at) — feeds the stale-value
# watchdog (app/alerting/stale_checker.py)
# ---------------------------------------------------------------------------

async def test_process_value_updates_last_changed_at_only_on_change(engine_with_pool):
    engine, sub, srv, pool = engine_with_pool
    mgr = _FakeMgr()
    await engine._process_value(sub, 1.0, mgr)
    first_changed_at = engine._sub_state[sub.id].last_changed_at

    await asyncio.sleep(0.05)
    await engine._process_value(sub, 1.0, mgr)  # same value — must not bump the timestamp
    assert engine._sub_state[sub.id].last_changed_at == first_changed_at

    await engine._process_value(sub, 2.0, mgr)  # changed — timestamp must advance
    assert engine._sub_state[sub.id].last_changed_at > first_changed_at
    assert engine._sub_state[sub.id].last_raw_value == 2.0
