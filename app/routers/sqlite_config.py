from __future__ import annotations

from pathlib import Path

import aiosqlite
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from app.config import SQLiteTargetConfig, SQLiteConfig, save_sqlite
from app.dependencies import get_state
from app.sqlite import export as sqlite_export
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
        total_rows[t.id] = await _count_total_rows(t.path)
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


def _signal_labels(state: RuntimeState) -> dict[str, str]:
    """Map SQLite column name → friendly label from current subscriptions."""
    labels: dict[str, str] = {}
    for sub in state.config.subscriptions.subscriptions:
        col = sub.influxdb.measurement.replace(".", "_") + "_" + sub.influxdb.field_name
        col = "".join(c if c.isalnum() or c == "_" else "_" for c in col)
        label = sub.influxdb.measurement
        if sub.influxdb.field_name != "value":
            label += f" ({sub.influxdb.field_name})"
        labels[col] = label
    return labels


def _export_target(state: RuntimeState, target_id: str) -> SQLiteTargetConfig | None:
    t = next((t for t in state.config.sqlite.targets if t.id == target_id), None)
    if t is None or not Path(t.path).exists():
        return None
    return t


def _read_schema(path: str) -> dict[str, list[str]]:
    db = sqlite_export.connect(path)
    try:
        return sqlite_export.read_schema(db)
    finally:
        db.close()


async def _spec_from_form(request: Request, path: str) -> sqlite_export.ExportSpec:
    form = await request.form()
    schema = await run_in_threadpool(_read_schema, path)
    return sqlite_export.parse_spec(
        schema,
        signals=form.getlist("signal"),
        start=form.get("from"),
        end=form.get("to"),
        interval=form.get("interval", ""),
        method=form.get("method", "avg"),
        fmt=form.get("format", "standard"),
        zip_=form.get("zip") == "1",
    )


def _estimate(path: str, spec: sqlite_export.ExportSpec) -> dict:
    db = sqlite_export.connect(path)
    try:
        return sqlite_export.estimate(db, spec)
    finally:
        db.close()


@router.get("/{target_id}/export/dialog", response_class=HTMLResponse)
async def export_dialog(target_id: str, request: Request, state: RuntimeState = Depends(get_state)):
    t = _export_target(state, target_id)
    if t is None:
        return HTMLResponse("Database file not found", status_code=404)

    def _load():
        db = sqlite_export.connect(t.path)
        try:
            schema = sqlite_export.read_schema(db)
            return schema, sqlite_export.time_bounds(db, schema)
        finally:
            db.close()

    schema, (first, last) = await run_in_threadpool(_load)
    labels = _signal_labels(state)
    groups = [
        {"table": table, "signals": [
            {"value": f"{table}/{col}", "col": col, "label": labels.get(col, col)} for col in cols
        ]}
        for table, cols in schema.items() if cols
    ]
    return templates.TemplateResponse(request, "partials/sqlite_export_dialog.html", {
        "request": request,
        "t": t,
        "groups": groups,
        "first": first,
        "last": last,
    })


@router.post("/{target_id}/export/estimate")
async def export_estimate(target_id: str, request: Request, state: RuntimeState = Depends(get_state)):
    t = _export_target(state, target_id)
    if t is None:
        return JSONResponse({"error": "Database file not found"}, status_code=404)
    try:
        spec = await _spec_from_form(request, t.path)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(await run_in_threadpool(_estimate, t.path, spec))


async def _export_response(t: SQLiteTargetConfig, spec: sqlite_export.ExportSpec, state: RuntimeState):
    est = await run_in_threadpool(_estimate, t.path, spec)
    if est["too_large"]:
        return HTMLResponse(" ".join(est["warnings"]), status_code=413)
    name = "".join(c if c.isalnum() or c in "-_" else "_" for c in t.display_name)
    if spec.start or spec.end:
        start, end = ((s or "")[:10].replace("-", "") for s in (spec.start, spec.end))
        name += f"_{start}-{end}"
    csv_name = f"{name}.csv"
    file_name = f"{name}.zip" if spec.zip else csv_name
    return StreamingResponse(
        sqlite_export.iter_export(t.path, spec, _signal_labels(state), csv_name),
        media_type="application/zip" if spec.zip else "text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{file_name}"'},
    )


@router.post("/{target_id}/export")
async def export_csv(target_id: str, request: Request, state: RuntimeState = Depends(get_state)):
    """Export the signals, time range and downsampling chosen in the export dialog."""
    t = _export_target(state, target_id)
    if t is None:
        return HTMLResponse("Database file not found", status_code=404)
    try:
        spec = await _spec_from_form(request, t.path)
    except ValueError as exc:
        return HTMLResponse(str(exc), status_code=400)
    return await _export_response(t, spec, state)


@router.get("/{target_id}/export")
async def export_csv_all(target_id: str, state: RuntimeState = Depends(get_state)):
    """Export every signal over the full time range (no dialog)."""
    t = _export_target(state, target_id)
    if t is None:
        return HTMLResponse("Database file not found", status_code=404)
    schema = await run_in_threadpool(_read_schema, t.path)
    try:
        spec = sqlite_export.parse_spec(schema, signals=None)
    except ValueError as exc:
        return HTMLResponse(str(exc), status_code=400)
    return await _export_response(t, spec, state)


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
