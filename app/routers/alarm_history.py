"""The values stored around an alarm (linked from its Event)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from app.alerting import history_store
from app.alerting.history_mail import render
from app.dependencies import get_state
from app.state import RuntimeState

router = APIRouter(prefix="/alarm-history")
templates = Jinja2Templates(directory="templates")


@router.get("/{history_id}", response_class=HTMLResponse)
async def show(history_id: int, request: Request, state: RuntimeState = Depends(get_state)):
    h = history_store.get(history_id)
    if h is None:
        return HTMLResponse("Value history not found (only the newest "
                            f"{history_store.KEEP} are kept)", status_code=404)
    rule = next((r for r in state.config.alarm_rules.rules if r.id == h.rule_id), None)
    heads = [(v.label, v.unit) for v in h.rows[0][2]] if h.rows else []
    return templates.TemplateResponse(request, "alarm_history.html", {
        "request": request, "h": h, "rule": rule, "heads": heads,
    })


@router.get("/{history_id}/export.csv")
async def export_csv(history_id: int):
    h = history_store.get(history_id)
    if h is None:
        return Response("Value history not found", status_code=404)
    _, _, text = render(h.rows, h.title)
    name = f"alarm_{h.ts.astimezone().strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(text, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})
