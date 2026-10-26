"""
Data viewer — serves the time-series chart page and query endpoints.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import aiosqlite
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.dependencies import get_state
from app.state import RuntimeState

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/data")
templates = Jinja2Templates(directory="templates")


@router.get("", response_class=HTMLResponse)
async def data_page(request: Request, state: RuntimeState = Depends(get_state)):
    targets = [t for t in state.config.sqlite.targets if t.enabled]
    return templates.TemplateResponse(request, "data_viewer.html", {
        "request": request,
        "targets": targets,
    })


# Schema only changes when the SQLite writer adds a column — cache the
# full-DB introspection briefly instead of re-opening every DB per request.
_SIGNALS_TTL_S = 30.0
_signals_cache: tuple[float, list[dict]] | None = None


@router.get("/signals")
async def list_signals(state: RuntimeState = Depends(get_state)):
    """
    Return list of available signals from all enabled SQLite targets.
    Each signal: {db, table, col, label}
    """
    global _signals_cache
    now = time.monotonic()
    if _signals_cache is not None and now - _signals_cache[0] < _SIGNALS_TTL_S:
        return JSONResponse(_signals_cache[1])
    signals: list[dict] = []
    for target in state.config.sqlite.targets:
        if not target.enabled:
            continue
        db_path = Path(target.file_path)
        if not db_path.exists():
            continue
        try:
            async with aiosqlite.connect(str(db_path)) as db:
                cursor = await db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
                tables = [row[0] async for row in cursor]
                for table in tables:
                    cursor2 = await db.execute(f"PRAGMA table_info([{table}])")
                    cols = [row[1] async for row in cursor2]
                    for col in cols:
                        if col == "time":
                            continue
                        signals.append({
                            "db": target.file_path,
                            "db_name": target.display_name,
                            "table": table,
                            "col": col,
                            "label": f"{target.display_name} / {table} / {col}",
                        })
        except Exception as exc:
            logger.warning("Error reading signals from %s: %s", target.file_path, exc)
    _signals_cache = (now, signals)
    return JSONResponse(signals)


@router.get("/query")
async def query_data(
    db: str = Query(...),
    table: str = Query(...),
    col: str = Query(...),
    from_ts: str = Query(default="", alias="from"),
    to_ts: str = Query(default="", alias="to"),
    limit: int = Query(default=2000),
):
    """
    Query time-series data from a SQLite target.
    Returns: {"series": [{"t": "...", "v": 1.23}, ...]}
    """
    db_path = Path(db)
    if not db_path.exists():
        return JSONResponse({"series": []})
    try:
        async with aiosqlite.connect(str(db_path)) as conn:
            params: list = []
            where_clauses = [f"[{col}] IS NOT NULL"]
            if from_ts:
                where_clauses.append("time >= ?")
                params.append(from_ts)
            if to_ts:
                where_clauses.append("time <= ?")
                params.append(to_ts)
            where = " AND ".join(where_clauses)
            params.append(limit)
            sql = (
                f"SELECT time, CAST([{col}] AS REAL) "
                f"FROM [{table}] "
                f"WHERE {where} "
                f"ORDER BY time "
                f"LIMIT ?"
            )
            cursor = await conn.execute(sql, params)
            rows = await cursor.fetchall()
            series = [{"t": row[0], "v": row[1]} for row in rows if row[1] is not None]
        return JSONResponse({"series": series})
    except Exception as exc:
        logger.warning("Query error db=%s table=%s col=%s: %s", db, table, col, exc)
        return JSONResponse({"series": [], "error": str(exc)})
