"""Reference tables: capture characteristic values on demand, comment, delete, CSV."""
from __future__ import annotations

import re
from datetime import timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app.config import ReferenceSignalConfig, ReferenceTableConfig, ReferenceTriggerConfig, save_references
from app.dependencies import get_state
from app.references import store
from app.references.capture import capture, signal_label
from app.references.export import columns, to_csv
from app.state import RuntimeState

router = APIRouter(prefix="/references")
templates = Jinja2Templates(directory="templates")


def _choice_label(state: RuntimeState, sub) -> str:
    """Signal choice in the editor: name · measurement (once if both are the same)."""
    name = signal_label(state, sub.id)
    return name if name == sub.influxdb.measurement else f"{name} · {sub.influxdb.measurement}"


def _table(state: RuntimeState, table_id: str) -> ReferenceTableConfig | None:
    return next((t for t in state.config.references.tables if t.id == table_id), None)


@router.get("", response_class=HTMLResponse)
async def references_page(request: Request, table: str = "", state: RuntimeState = Depends(get_state)):
    tables = state.config.references.tables
    current = _table(state, table) or (tables[0] if tables else None)
    entries = store.list_entries(current.id) if current else []
    subs = state.config.subscriptions.subscriptions
    return templates.TemplateResponse(request, "references.html", {
        "request": request,
        "tables": tables,
        "current": current,
        "entries": entries,
        "cols": columns(current, entries) if current else [],
        "has_events": any(e.event is not None for e in entries),
        "trigger_label": signal_label(state, current.trigger.subscription_id) if current else "",
        "counts": store.count_entries(),
        "tables_json": [t.model_dump() for t in tables],
        "ref_subs": [{"id": s.id, "label": _choice_label(state, s)} for s in subs],
    })


@router.post("/tables")
async def save_table(request: Request, state: RuntimeState = Depends(get_state)):
    """Create (no id) or update a table from the editor's JSON {id?, name, signals}."""
    body = await request.json()
    try:
        signals = [ReferenceSignalConfig(**s) for s in body.get("signals", []) if s.get("subscription_id")]
    except (TypeError, ValueError) as exc:
        return JSONResponse({"error": f"Invalid signal: {exc}"}, status_code=400)
    if not signals:
        return JSONResponse({"error": "Add at least one signal"}, status_code=400)
    name = (body.get("name") or "").strip()[:80] or "Reference"
    try:
        trigger = ReferenceTriggerConfig(**(body.get("trigger") or {}))
    except (TypeError, ValueError) as exc:
        return JSONResponse({"error": f"Invalid trigger: {exc}"}, status_code=400)
    if trigger.enabled and not trigger.subscription_id:
        return JSONResponse({"error": "Choose the signal of the trigger"}, status_code=400)
    trigger.delay_seconds = max(0, min(trigger.delay_seconds, 86400))
    trigger.step_seconds = max(1, min(trigger.step_seconds, 3600))
    trigger.pre_seconds = max(0, min(trigger.pre_seconds, 3600))
    trigger.post_seconds = max(0, min(trigger.post_seconds, 3600))
    table = _table(state, body.get("id") or "")
    if table is None:
        table = ReferenceTableConfig(name=name, signals=signals, trigger=trigger)
        state.config.references.tables.append(table)
    else:
        table.name, table.signals, table.trigger = name, signals, trigger
    save_references(state.config.references, state.config_dir)
    return JSONResponse({"ok": True, "id": table.id})


@router.post("/tables/{table_id}/delete")
async def delete_table(table_id: str, state: RuntimeState = Depends(get_state)):
    state.config.references.tables = [t for t in state.config.references.tables if t.id != table_id]
    save_references(state.config.references, state.config_dir)
    store.delete_table(table_id)
    return RedirectResponse("/references", status_code=303)


@router.post("/tables/{table_id}/capture")
async def capture_now(table_id: str, request: Request, state: RuntimeState = Depends(get_state)):
    """Write one row with the current values; body {author, comment}."""
    table = _table(state, table_id)
    if table is None:
        return JSONResponse({"error": "Table not found"}, status_code=404)
    body = await request.json()
    values = await capture(state, table)
    entry_id = store.add_entry(table.id, values, author=str(body.get("author") or ""),
                               comment=str(body.get("comment") or ""))
    problems = [f"{v.label}: {v.quality}" for v in values if v.quality != "ok"]
    return JSONResponse({"ok": True, "id": entry_id, "problems": problems})


@router.post("/entries/{entry_id}/comment")
async def edit_comment(entry_id: int, request: Request):
    body = await request.json()
    if not store.update_comment(entry_id, str(body.get("comment") or "")):
        return JSONResponse({"error": "Entry not found"}, status_code=404)
    return JSONResponse({"ok": True})


@router.post("/entries/{entry_id}/delete")
async def delete_entry(entry_id: int):
    if not store.delete_entry(entry_id):
        return JSONResponse({"error": "Entry not found"}, status_code=404)
    return JSONResponse({"ok": True})


@router.post("/tables/{table_id}/events/{event}/delete")
async def delete_event(table_id: str, event: int):
    n = store.delete_event(table_id, event)
    if not n:
        return JSONResponse({"error": "Event not found"}, status_code=404)
    return JSONResponse({"ok": True, "deleted": n})


@router.get("/tables/{table_id}/export.csv")
async def export_csv(table_id: str, format: str = "standard", tz: str = "UTC",
                     state: RuntimeState = Depends(get_state)):
    """CSV of all rows, oldest first; times in the browser's zone (tz) and UTC."""
    table = _table(state, table_id)
    if table is None:
        return Response("Table not found", status_code=404)
    zone, zone_name = timezone.utc, "UTC"
    if tz and tz != "UTC":
        try:
            from zoneinfo import ZoneInfo
            zone, zone_name = ZoneInfo(tz), tz
        except Exception:
            pass
    text = to_csv(table, store.list_entries(table.id), excel_de=format == "excel_de", tz=zone, tz_name=zone_name)
    fname = re.sub(r"[^A-Za-z0-9_.-]+", "_", table.name).strip("_") or "reference"
    return Response(text, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{fname}.csv"'})
