"""
Live Chart — browse OPC UA nodes and chart their values in real-time.
"""
from __future__ import annotations

import logging
import asyncio
from datetime import datetime, timezone
from urllib.parse import unquote

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.config import SavedChartConfig, save_saved_charts, apply_name_strips
from app.dependencies import get_state
from app.opcua.browser import get_children
from app.state import RuntimeState

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/chart")
templates = Jinja2Templates(directory="templates")


@router.get("", response_class=HTMLResponse)
async def chart_page(request: Request, state: RuntimeState = Depends(get_state)):
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
    return templates.TemplateResponse(request, "chart.html", {
        "request": request,
        "servers": state.config.servers.servers,
        "server_status": state.server_status,
        "strip_prefixes": strip_prefixes,
        "subscriptions": subscriptions,
        "server_names": server_names,
        "stripped_measurements": stripped_measurements,
    })


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
        multipliers=body.get("multipliers", {}),
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
    engine = state.polling_engine
    if engine is not None:
        lv = engine.live_values()  # {sub.id: raw_value}
        for sub in state.config.subscriptions.subscriptions:
            if sub.id in lv:
                raw = lv[sub.id]
                try:
                    cached[(sub.server_id, sub.node_id)] = float(int(raw) if isinstance(raw, bool) else raw)
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
