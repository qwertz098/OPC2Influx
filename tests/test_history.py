"""History tab: Flux query building and endpoint guards."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.routers.chart import history_flux

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
STOP = datetime(2026, 1, 2, tzinfo=timezone.utc)


def test_flux_escapes_strings_and_filters_exact_field():
    flux = history_flux('opc"ua', 'Line 1."x"', "value", False, START, STOP, 60)
    assert 'from(bucket: "opc\\"ua")' in flux
    assert 'r._measurement == "Line 1.\\"x\\""' in flux
    assert 'r._field == "value"' in flux
    assert "range(start: 2026-01-01T00:00:00Z, stop: 2026-01-02T00:00:00Z)" in flux
    for fn in ("mean", "min", "max"):
        assert f"aggregateWindow(every: 60s, createEmpty: false, timeSrc: \"_start\", fn: {fn})" in flux


def test_flux_accepts_type_suffixes():
    flux = history_flux("b", "m", "va/l", True, START, STOP, 5)
    assert r"r._field =~ /^va\/l(_float|_integer|_bool)?$/" in flux


@pytest.mark.asyncio
async def test_history_page_and_data_without_influx_target(async_client):
    page = await async_client.get("/chart/history")
    assert page.status_code == 200 and "no InfluxDB target is enabled" in page.text
    resp = await async_client.post("/chart/history/data", json={"signals": [], "from": 1, "to": 2})
    assert resp.status_code == 400
    bad = await async_client.post("/chart/history/data", json={"signals": [], "from": 5, "to": 2})
    assert bad.status_code == 400
