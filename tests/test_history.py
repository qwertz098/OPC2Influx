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


def test_parse_embed_spec_defaults_order_and_unknown_tokens():
    from app.routers.chart import parse_embed_spec

    assert parse_embed_spec("") == {"scheme": "dark", "look": "studio", "shaded": False}
    assert parse_embed_spec("SHADED+contrast+light") == {"scheme": "light", "look": "contrast", "shaded": True}
    assert parse_embed_spec("dark_shaded+bogus") == {"scheme": "dark", "look": "studio", "shaded": False}


@pytest.mark.asyncio
async def test_embed_page_applies_spec(async_client):
    import app.main as main_mod
    from app.config import SavedChartConfig

    cfg = SavedChartConfig(name="View", signals=[], shaded=True)
    main_mod.app.state.runtime.config.saved_charts.configs.append(cfg)
    plain = await async_client.get(f"/chart/embed/{cfg.id}")
    assert 'data-theme="dark"' in plain.text and 'data-look="studio"' in plain.text
    assert "const SHADED = false" in plain.text  # the view's own setting does not apply
    styled = await async_client.get(f"/chart/embed/{cfg.id}/solarized-light+mono+shaded")
    assert 'data-theme="solarized-light"' in styled.text and 'data-look="mono"' in styled.text
    assert "const SHADED = true" in styled.text
    missing = await async_client.get("/chart/embed/nope/light")
    assert missing.status_code == 404
