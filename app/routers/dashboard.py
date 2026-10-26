from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.dependencies import get_state
from app.state import RuntimeState
from app.routers.sqlite_config import _count_total_rows


def _bytes_per_day(total_bytes: int, started_at: datetime | None) -> int | None:
    """Estimate bytes/day from cumulative bytes and start time. Returns None if too little data."""
    if not started_at or total_bytes == 0:
        return None
    now = datetime.now(timezone.utc)
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    elapsed_s = (now - started_at).total_seconds()
    if elapsed_s < 60:
        return None
    return int(total_bytes / elapsed_s * 86400)


def _influx_bytes_per_day(state: RuntimeState) -> dict[str, int | None]:
    result = {}
    for t in state.config.influxdb.targets:
        st = state.influx_status.get(t.id)
        if st:
            result[t.id] = _bytes_per_day(st.bytes_estimated, st.started_at)
    return result


def _sqlite_bytes_per_day(state: RuntimeState) -> dict[str, int | None]:
    result = {}
    for t in state.config.sqlite.targets:
        st = state.sqlite_status.get(t.id) if state.sqlite_status else None
        if st:
            # SQLite rows are wide (one row per server snapshot) — ~200 bytes/row
            est = st.rows_written * 200
            result[t.id] = _bytes_per_day(est, st.started_at)
    return result


def _sqlite_file_sizes(state: RuntimeState) -> dict[str, int | None]:
    sizes: dict[str, int | None] = {}
    for t in state.config.sqlite.targets:
        p = Path(t.file_path)
        sizes[t.id] = p.stat().st_size if p.exists() else None
    return sizes

router = APIRouter()
templates = Jinja2Templates(directory="templates")


# COUNT(*) is a full table scan in SQLite — far too expensive for the 5s
# status poll once the DB has grown. Cache per file with a short TTL.
_ROW_COUNT_TTL_S = 60.0
_row_count_cache: dict[str, tuple[float, int | None]] = {}


async def _sqlite_total_rows(state: RuntimeState) -> dict[str, int | None]:
    result = {}
    now = time.monotonic()
    for t in state.config.sqlite.targets:
        cached = _row_count_cache.get(t.file_path)
        if cached is not None and now - cached[0] < _ROW_COUNT_TTL_S:
            result[t.id] = cached[1]
        else:
            count = await _count_total_rows(t.file_path)
            _row_count_cache[t.file_path] = (now, count)
            result[t.id] = count
    return result


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, state: RuntimeState = Depends(get_state)):
    servers = state.config.servers.servers
    targets = state.config.influxdb.targets
    subs_count = len([s for s in state.config.subscriptions.subscriptions if s.enabled])
    return templates.TemplateResponse(request, "dashboard.html", {
        "request": request,
        "servers": servers,
        "server_status": state.server_status,
        "targets": targets,
        "influx_status": state.influx_status,
        "influx_bytes_per_day": _influx_bytes_per_day(state),
        "subs_count": subs_count,
        "active_alarms": state.active_alarms[-20:],
        "sqlite_targets": state.config.sqlite.targets,
        "sqlite_file_sizes": _sqlite_file_sizes(state),
        "sqlite_status": state.sqlite_status,
        "sqlite_total_rows": await _sqlite_total_rows(state),
        "sqlite_bytes_per_day": _sqlite_bytes_per_day(state),
    })


@router.get("/status", response_class=HTMLResponse)
async def status_fragment(request: Request, state: RuntimeState = Depends(get_state)):
    """HTMX fragment for auto-refreshing status cards."""
    servers = state.config.servers.servers
    targets = state.config.influxdb.targets
    subs_count = len([s for s in state.config.subscriptions.subscriptions if s.enabled])
    return templates.TemplateResponse(request, "partials/status_card.html", {
        "request": request,
        "servers": servers,
        "server_status": state.server_status,
        "targets": targets,
        "influx_status": state.influx_status,
        "influx_bytes_per_day": _influx_bytes_per_day(state),
        "subs_count": subs_count,
        "active_alarms": state.active_alarms[-50:],
        "sqlite_targets": state.config.sqlite.targets,
        "sqlite_file_sizes": _sqlite_file_sizes(state),
        "sqlite_status": state.sqlite_status,
        "sqlite_total_rows": await _sqlite_total_rows(state),
        "sqlite_bytes_per_day": _sqlite_bytes_per_day(state),
    })


@router.get("/dashboard/alarms", response_class=HTMLResponse)
async def alarms_fragment(request: Request, state: RuntimeState = Depends(get_state)):
    """HTMX fragment for auto-refreshing Events card."""
    return templates.TemplateResponse(request, "partials/alarms_card.html", {
        "request": request,
        "active_alarms": state.active_alarms[-50:],
    })


@router.post("/dashboard/alarms/clear", response_class=HTMLResponse)
async def clear_alarms(request: Request, state: RuntimeState = Depends(get_state)):
    """Clear all events from the in-memory list."""
    state.active_alarms.clear()
    return templates.TemplateResponse(request, "partials/alarms_card.html", {
        "request": request,
        "active_alarms": [],
    })
