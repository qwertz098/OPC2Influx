"""End-to-end API tests using FastAPI AsyncClient."""
from __future__ import annotations

import pytest
from pathlib import Path


@pytest.mark.asyncio
async def test_dashboard_returns_200(async_client):
    resp = await async_client.get("/")
    assert resp.status_code == 200
    assert "Dashboard" in resp.text


@pytest.mark.asyncio
async def test_browser_page_returns_200(async_client):
    resp = await async_client.get("/browser")
    assert resp.status_code == 200
    assert "OPC UA Browser" in resp.text


@pytest.mark.asyncio
async def test_subscriptions_page_returns_200(async_client):
    resp = await async_client.get("/subscriptions")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_config_servers_page_returns_200(async_client):
    resp = await async_client.get("/config/servers")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_config_influxdb_page_returns_200(async_client):
    resp = await async_client.get("/config/influxdb")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_config_alerts_page_returns_200(async_client):
    resp = await async_client.get("/config/alerts")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_status_fragment_returns_200(async_client):
    resp = await async_client.get("/status")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_add_server_and_see_in_list(async_client):
    resp = await async_client.post("/config/servers", data={
        "display_name": "Test PLC",
        "endpoint_url": "opc.tcp://192.168.1.99:4840",
        "security_mode": "None",
        "auth_type": "anonymous",
        "reconnect_interval": "10",
        "enabled": "true",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert "Test PLC" in resp.text


@pytest.mark.asyncio
async def test_add_influxdb_target(async_client):
    resp = await async_client.post("/config/influxdb", data={
        "display_name": "Test InfluxDB",
        "url": "http://localhost:8086",
        "token": "test-token",
        "org": "testorg",
        "bucket": "testbucket",
        "mode": "always",
        "priority": "1",
        "batch_size": "100",
        "flush_interval_ms": "500",
        "max_retries": "3",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert "Test InfluxDB" in resp.text




@pytest.mark.asyncio
async def test_delete_all_dynamic_and_manual(async_client):
    resp = await async_client.post("/subscriptions/delete-all-dynamic")
    assert resp.status_code == 303
    resp = await async_client.post("/subscriptions/delete-all-manual")
    assert resp.status_code == 303
    resp = await async_client.get("/subscriptions")
    assert resp.status_code == 200
