"""
Live Chart — browse OPC UA nodes and chart their values in real-time.
History — the same chart over values recorded in InfluxDB.
"""
from __future__ import annotations

import logging
import asyncio
import math
import re
from datetime import datetime, timezone
from urllib.parse import unquote

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from influxdb_client.client.influxdb_client_async import InfluxDBClientAsync

from app.config import (
    SavedChartConfig, UnresolvedEnvVarError, apply_name_strips, resolve_env, resolve_env_strict,
    save_saved_charts,
)
from app.dependencies import get_state
from app.opcua.browser import get_children
from app.state import RuntimeState

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/chart")
templates = Jinja2Templates(directory="templates")


def _chart_page(request: Request, state: RuntimeState, history: bool):
    template = "history.html" if history else "chart.html"
    strip_prefixes = {s.id: s.name_strip_prefixes for s in state.config.servers.servers}
    server_names = {s.id: s.display_name for s in state.config.servers.servers}
    subscriptions = sorted(
        state.config.subscriptions.subscriptions,
        key=lambda s: (server_names.get(s.server_id, ""), s.display_name or s.node_id),
    )
    stripped_measurements = {
        s.id: apply_name_strips(s.influxdb.measurement, strip_prefixes.get(s.server_id, []))
        for s in subscriptions
    }
    return templates.TemplateResponse(request, template, {
        "request": request,
        "servers": state.config.servers.servers,
        "server_status": state.server_status,
        "strip_prefixes": strip_prefixes,
        "subscriptions": subscriptions,
        "server_names": server_names,
        "stripped_measurements": stripped_measurements,
        "history": history,
        "influx_targets": _influx_targets(state) if history else [],
    })


@router.get("", response_class=HTMLResponse)
async def chart_page(request: Request, state: RuntimeState = Depends(get_state)):
    return _chart_page(request, state, history=False)


@router.get("/history", response_class=HTMLResponse)
async def history_page(request: Request, state: RuntimeState = Depends(get_state)):
    return _chart_page(request, state, history=True)


# Upper bound on points per signal and request; the browser chart stays fluid
_HISTORY_MAX_POINTS = 5000


def _flux_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def history_flux(bucket: str, measurement: str, field: str, type_suffix: bool,
                 start: datetime, stop: datetime, every_s: int) -> str:
    """Flux query: one signal's mean, min and max per every_s window in [start, stop).

    With type_suffix the field was written as e.g. value_float / value_bool, so
    any of the suffixes is accepted. Booleans become 0/1 via toFloat(). All
    series of the measurement (different tag sets) are merged into one line.
    Result rows carry the columns _time, mean, min and max.
    """
    if type_suffix:
        pattern = re.escape(field).replace("/", "\\/")
        field_filter = f"r._field =~ /^{pattern}(_float|_integer|_bool)?$/"
    else:
        field_filter = f"r._field == {_flux_str(field)}"
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    window = f'every: {every_s}s, createEmpty: false, timeSrc: "_start"'
    return (
        f"data = from(bucket: {_flux_str(bucket)})\n"
        f"  |> range(start: {start.strftime(fmt)}, stop: {stop.strftime(fmt)})\n"
        f"  |> filter(fn: (r) => r._measurement == {_flux_str(measurement)} and {field_filter})\n"
        f"  |> toFloat()\n"
        f"  |> group()\n"
        f'  |> sort(columns: ["_time"])\n'
        f"union(tables: [\n"
        f'  data |> aggregateWindow({window}, fn: mean) |> set(key: "agg", value: "mean"),\n'
        f'  data |> aggregateWindow({window}, fn: min) |> set(key: "agg", value: "min"),\n'
        f'  data |> aggregateWindow({window}, fn: max) |> set(key: "agg", value: "max"),\n'
        f"])\n"
        f'  |> keep(columns: ["_time", "_value", "agg"])\n'
        f"  |> group()\n"
        f'  |> pivot(rowKey: ["_time"], columnKey: ["agg"], valueColumn: "_value")\n'
        f'  |> sort(columns: ["_time"])\n'
    )


def _influx_targets(state: RuntimeState) -> list:
    return sorted((t for t in state.config.influxdb.targets if t.enabled), key=lambda t: t.priority)


@router.post("/history/data")
async def history_data(request: Request, state: RuntimeState = Depends(get_state)):
    """Recorded values of the requested signals between ``from`` and ``to`` (epoch ms).

    Body: {"signals": [{server_id, node_id}], "from": ms, "to": ms, "points": n,
    "target_id": optional InfluxDB target}. Each signal is looked up as a
    subscription and read from InfluxDB under the measurement/field it is
    written with, reduced to at most ``points`` windows of
    ``[t_ms, mean, min, max]``. Signals without data come back with
    ``"points": []`` and ``"missing": true``.
    """
    body = await request.json()
    try:
        start_ms, end_ms = int(body["from"]), int(body["to"])
        max_points = max(10, min(int(body.get("points", 2000)), _HISTORY_MAX_POINTS))
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"error": "from/to required"}, status_code=400)
    if end_ms <= start_ms:
        return JSONResponse({"error": "empty range"}, status_code=400)

    targets = _influx_targets(state)
    target = next((t for t in targets if t.id == body.get("target_id")), targets[0] if targets else None)
    if target is None:
        return JSONResponse({"error": "No InfluxDB target configured"}, status_code=400)

    start = datetime.fromtimestamp(start_ms / 1000, timezone.utc)
    stop = datetime.fromtimestamp(end_ms / 1000, timezone.utc)
    every_s = max(1, math.ceil((end_ms - start_ms) / 1000 / max_points))
    strips = {s.id: s.name_strip_prefixes for s in state.config.servers.servers}
    subs = {(s.server_id, s.node_id): s for s in state.config.subscriptions.subscriptions}

    try:
        token = resolve_env_strict(target.token)
    except UnresolvedEnvVarError:
        token = resolve_env(target.token)

    results = []
    error = None
    async with InfluxDBClientAsync(url=target.url, token=token, org=target.org, timeout=30_000) as client:
        query_api = client.query_api()
        for item in body.get("signals", []):
            server_id, node_id = item.get("server_id", ""), item.get("node_id", "")
            sub = subs.get((server_id, node_id))
            points: list[list] = []
            if sub is not None:
                measurement = apply_name_strips(sub.influxdb.measurement, strips.get(server_id, []))
                flux = history_flux(target.bucket, measurement, sub.influxdb.field_name,
                                    target.type_suffix, start, stop, every_s)
                try:
                    for table in await query_api.query(flux, org=target.org):
                        for rec in table.records:
                            v = rec.values
                            if v.get("mean") is not None:
                                points.append([int(rec.get_time().timestamp() * 1000),
                                               v["mean"], v.get("min"), v.get("max")])
                except Exception as exc:
                    error = str(exc)[:200]
                    logger.warning("History query failed for %s: %s", measurement, exc)
            results.append({"server_id": server_id, "node_id": node_id,
                            "points": points, "missing": not points})
    return JSONResponse({"bucket_seconds": every_s, "target": target.display_name,
                         "error": error, "signals": results})


@router.get("/nodes/{server_id}", response_class=HTMLResponse)
@router.get("/nodes/{server_id}/{node_id:path}", response_class=HTMLResponse)
async def chart_browse_nodes(
    request: Request,
    server_id: str,
    node_id: str | None = None,
    parent_path: str = Query(default=""),
    state: RuntimeState = Depends(get_state),
):
    if node_id:
        node_id = unquote(node_id)
    mgr = state.opcua_manager
    if mgr is None:
        return HTMLResponse('<li class="text-muted p-2">OPC UA manager not initialized</li>')
    nodes = await get_children(mgr, server_id, node_id, parent_path=parent_path)
    return templates.TemplateResponse(request, "partials/chart_node_row.html", {
        "request": request,
        "nodes": nodes,
        "server_id": server_id,
    })


@router.get("/configs")
async def list_chart_configs(state: RuntimeState = Depends(get_state)):
    configs = state.config.saved_charts.configs
    return JSONResponse([{"id": c.id, "name": c.name} for c in configs])


@router.post("/configs")
async def save_chart_config(request: Request, state: RuntimeState = Depends(get_state)):
    body = await request.json()
    now = datetime.now(timezone.utc).isoformat()
    cfg = SavedChartConfig(
        name=body.get("name", "Unnamed"),
        signals=body.get("signals", []),
        interval_ms=body.get("interval_ms", 1000),
        max_time_seconds=body.get("max_time_seconds", 3600),
        log_time_mode=bool(body.get("log_time_mode", False)),
        auto_scale_mode=bool(body.get("auto_scale_mode", False)),
        fill_mode=bool(body.get("fill_mode", False)),
        multipliers=body.get("multipliers", {}),
        history_range_seconds=int(body.get("history_range_seconds") or 0),
        created_at=now,
    )
    state.config.saved_charts.configs.append(cfg)
    save_saved_charts(state.config.saved_charts, state.config_dir)
    return JSONResponse({"id": cfg.id, "name": cfg.name})


@router.get("/configs/{config_id}")
async def get_chart_config(config_id: str, state: RuntimeState = Depends(get_state)):
    cfg = next((c for c in state.config.saved_charts.configs if c.id == config_id), None)
    if cfg is None:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return JSONResponse(cfg.model_dump())


@router.post("/configs/{config_id}/delete")
async def delete_chart_config(config_id: str, state: RuntimeState = Depends(get_state)):
    state.config.saved_charts.configs = [
        c for c in state.config.saved_charts.configs if c.id != config_id
    ]
    save_saved_charts(state.config.saved_charts, state.config_dir)
    return JSONResponse({"ok": True})


@router.get("/embed/{config_id}", response_class=HTMLResponse)
@router.get("/embed/{config_id}/{theme}", response_class=HTMLResponse)
async def chart_embed(config_id: str, request: Request,
                      state: RuntimeState = Depends(get_state),
                      theme: str = "dark"):
    cfg = next((c for c in state.config.saved_charts.configs if c.id == config_id), None)
    if cfg is None:
        return HTMLResponse("Chart view not found", status_code=404)
    if theme not in {"dark", "dark_shaded", "light", "light_shaded"}:
        theme = "dark"
    strip_prefixes = {s.id: s.name_strip_prefixes for s in state.config.servers.servers}
    return templates.TemplateResponse(request, "chart_embed.html", {
        "request": request,
        "cfg": cfg,
        "theme": theme,
        "strip_prefixes": strip_prefixes,
    })


@router.post("/read")
async def read_nodes(request: Request, state: RuntimeState = Depends(get_state)):
    """Read current OPC UA values for a list of {server_id, node_id} pairs.

    Uses the polling-engine's last-known-value cache first to avoid extra
    OPC UA round-trips. Falls back to a live read only when the cache has
    no entry (e.g. node added to chart but not yet polled).
    """
    body = await request.json()
    mgr = state.opcua_manager

    # Build (server_id, node_id) → last_value map from the polling-engine cache
    cached: dict[tuple[str, str], float] = {}
    wanted = {(item.get("server_id", ""), item.get("node_id", "")) for item in body}
    engine = state.polling_engine
    if engine is not None:
        lv = engine.live_values()  # {sub.id: raw_value}
        for sub in state.config.subscriptions.subscriptions:
            key = (sub.server_id, sub.node_id)
            if key in wanted and key not in cached and sub.id in lv:
                raw = lv[sub.id]
                try:
                    cached[key] = float(int(raw) if isinstance(raw, bool) else raw)
                except (TypeError, ValueError):
                    pass

    # Group cache misses per server and read each group in one OPC UA call.
    misses_by_server: dict[str, list[str]] = {}
    for item in body:
        key = (item.get("server_id", ""), item.get("node_id", ""))
        if key not in cached:
            misses_by_server.setdefault(key[0], []).append(key[1])

    live: dict[tuple[str, str], float] = {}
    for server_id, node_ids in misses_by_server.items():
        try:
            raws = await asyncio.wait_for(
                mgr.read_node_values(server_id, node_ids), timeout=2.0
            )
            for node_id, raw in zip(node_ids, raws):
                try:
                    live[(server_id, node_id)] = float(int(raw) if isinstance(raw, bool) else raw)
                except (TypeError, ValueError):
                    pass
        except Exception as exc:
            logger.debug("chart batch read %s (%d nodes): %s", server_id, len(node_ids), exc)

    results = []
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    for item in body:
        server_id = item.get("server_id", "")
        node_id   = item.get("node_id", "")
        key = (server_id, node_id)
        value = cached.get(key, live.get(key))
        results.append({"server_id": server_id, "node_id": node_id, "v": value, "t": now_ms})
    return JSONResponse(results)
