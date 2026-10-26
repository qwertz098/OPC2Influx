from __future__ import annotations

import csv
import io
import sqlite3
import zipfile
from pathlib import Path

import aiosqlite
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from app.config import SQLiteTargetConfig, SQLiteConfig, save_sqlite
from app.dependencies import get_state
from app.state import RuntimeState

router = APIRouter(prefix="/config/sqlite")
templates = Jinja2Templates(directory="templates")


async def _count_total_rows(file_path: str) -> int | None:
    """Return total row count across all tables in the SQLite file."""
    path = Path(file_path)
    if not path.exists():
        return None
    try:
        async with aiosqlite.connect(str(path)) as db:
            cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [row[0] async for row in cursor]
            total = 0
            for table in tables:
                cur2 = await db.execute(f"SELECT COUNT(*) FROM [{table}]")
                row = await cur2.fetchone()
                if row:
                    total += row[0]
            return total
    except Exception:
        return None


@router.get("", response_class=HTMLResponse)
async def list_targets(request: Request, state: RuntimeState = Depends(get_state)):
    total_rows: dict[str, int | None] = {}
    for t in state.config.sqlite.targets:
        total_rows[t.id] = await _count_total_rows(t.file_path)
    return templates.TemplateResponse(request, "config/sqlite.html", {
        "request": request,
        "targets": state.config.sqlite.targets,
        "sqlite_status": state.sqlite_status,
        "total_rows": total_rows,
    })


@router.post("")
async def add_target(request: Request, state: RuntimeState = Depends(get_state)):
    form = await request.form()
    t = SQLiteTargetConfig(
        display_name=form.get("display_name", "SQLite Log"),
        file_path=form.get("file_path", "data/opcua_log.db"),
        write_interval_seconds=int(form.get("write_interval_seconds", 60)),
    )
    state.config.sqlite.targets.append(t)
    save_sqlite(state.config.sqlite, state.config_dir)
    if state.sqlite_writer_pool:
        await state.sqlite_writer_pool.reconcile()
    return RedirectResponse("/config/sqlite", status_code=303)


@router.get("/{target_id}/edit", response_class=HTMLResponse)
async def edit_target(target_id: str, request: Request, state: RuntimeState = Depends(get_state)):
    t = next((t for t in state.config.sqlite.targets if t.id == target_id), None)
    if t is None:
        return HTMLResponse("Not found", status_code=404)
    return templates.TemplateResponse(request, "partials/sqlite_edit_form.html", {
        "request": request,
        "t": t,
    })


@router.post("/{target_id}/update")
async def update_target(target_id: str, request: Request, state: RuntimeState = Depends(get_state)):
    form = await request.form()
    for t in state.config.sqlite.targets:
        if t.id == target_id:
            t.display_name = form.get("display_name", t.display_name).strip()
            t.file_path = form.get("file_path", t.file_path).strip()
            t.write_interval_seconds = int(form.get("write_interval_seconds", t.write_interval_seconds))
            break
    save_sqlite(state.config.sqlite, state.config_dir)
    if state.sqlite_writer_pool:
        await state.sqlite_writer_pool.reconcile()
    return RedirectResponse("/config/sqlite", status_code=303)


@router.post("/{target_id}/delete")
async def delete_target(target_id: str, state: RuntimeState = Depends(get_state)):
    state.config.sqlite.targets = [t for t in state.config.sqlite.targets if t.id != target_id]
    save_sqlite(state.config.sqlite, state.config_dir)
    if state.sqlite_writer_pool:
        await state.sqlite_writer_pool.reconcile()
    return RedirectResponse("/config/sqlite", status_code=303)


@router.get("/{target_id}/export")
async def export_csv(target_id: str, state: RuntimeState = Depends(get_state)):
    """Export a SQLite target as CSV (single file) or ZIP of CSVs (multiple tables)."""
    t = next((t for t in state.config.sqlite.targets if t.id == target_id), None)
    if t is None:
        return HTMLResponse("Not found", status_code=404)
    path = Path(t.file_path)
    if not path.exists():
        return HTMLResponse("Database file not found", status_code=404)

    # Build column → friendly header map from current subscriptions
    col_to_header: dict[str, str] = {}
    for sub in state.config.subscriptions.subscriptions:
        col = sub.influxdb.measurement.replace(".", "_") + "_" + sub.influxdb.field_name
        label = sub.influxdb.measurement
        if sub.influxdb.field_name != "value":
            label += f" ({sub.influxdb.field_name})"
        col_to_header[col] = label

    def _table_to_csv(db: sqlite3.Connection, table: str) -> str:
        cur = db.execute(f"PRAGMA table_info([{table}])")
        columns = [row[1] for row in cur.fetchall()]
        data_cols = [c for c in columns if c != "time"]
        headers = ["time"] + [col_to_header.get(c, c) for c in data_cols]
        col_clause = ", ".join(f"[{c}]" for c in columns)
        rows = db.execute(f"SELECT {col_clause} FROM [{table}] ORDER BY time").fetchall()
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(headers)
        for row in rows:
            # Replace None with empty string
            writer.writerow(["" if v is None else v for v in row])
        return buf.getvalue()

    def _generate():
        db = sqlite3.connect(str(path))
        try:
            tables = [r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()]
            if len(tables) == 1:
                yield _table_to_csv(db, tables[0]).encode("utf-8")
            else:
                zip_buf = io.BytesIO()
                with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                    for table in tables:
                        zf.writestr(f"{table}.csv", _table_to_csv(db, table))
                yield zip_buf.getvalue()
        finally:
            db.close()

    # Peek at table count to decide content type
    db_peek = sqlite3.connect(str(path))
    tables = [r[0] for r in db_peek.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    db_peek.close()

    safe_name = t.display_name.replace(" ", "_").replace("/", "_")
    if len(tables) == 1:
        return StreamingResponse(
            _generate(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{safe_name}.csv"'},
        )
    return StreamingResponse(
        _generate(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.zip"'},
    )


@router.post("/{target_id}/toggle")
async def toggle_target(target_id: str, state: RuntimeState = Depends(get_state)):
    for t in state.config.sqlite.targets:
        if t.id == target_id:
            t.enabled = not t.enabled
            break
    save_sqlite(state.config.sqlite, state.config_dir)
    if state.sqlite_writer_pool:
        await state.sqlite_writer_pool.reconcile()
    return RedirectResponse("/config/sqlite", status_code=303)
