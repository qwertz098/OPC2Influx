"""Periodic e-mail reports: settings, create / edit, preview and send now."""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.config import ReportConfig, ReportSignalConfig, save_reports
from app.dependencies import get_state
from app.reports import periods as P
from app.reports.compute import compute
from app.reports.render import html_body
from app.reports.scheduler import mark_current_as_sent, send_report
from app.state import RuntimeState

router = APIRouter(prefix="/config/reports")

_TIME = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
_BACK = "/config/alerts#reports"


def _find(state: RuntimeState, report_id: str) -> ReportConfig | None:
    return next((r for r in state.config.reports.reports if r.id == report_id), None)


def _recipients(raw) -> list[str]:
    items = raw if isinstance(raw, list) else re.split(r"[,;\s]+", str(raw or ""))
    return [a.strip() for a in items if a and "@" in a]


@router.post("/settings")
async def save_settings(request: Request, state: RuntimeState = Depends(get_state)):
    form = await request.form()
    cfg = state.config.reports
    send_time = str(form.get("send_time", cfg.send_time)).strip()
    if _TIME.match(send_time):
        cfg.send_time = send_time.zfill(5)
    if form.get("timezone") in ("local", "utc"):
        cfg.timezone = form.get("timezone")
    save_reports(cfg, state.config_dir)
    return RedirectResponse(_BACK, status_code=303)


@router.post("")
async def save_report(request: Request, state: RuntimeState = Depends(get_state)):
    """Create (no id) or update a report from the editor's JSON."""
    body = await request.json()
    try:
        signals = [ReportSignalConfig(**s) for s in body.get("signals", []) if s.get("subscription_id")]
    except (TypeError, ValueError) as exc:
        return JSONResponse({"error": f"Invalid signal: {exc}"}, status_code=400)
    fields = dict(
        name=(body.get("name") or "Report").strip()[:80],
        daily=bool(body.get("daily")), weekly=bool(body.get("weekly")), monthly=bool(body.get("monthly")),
        recipients=_recipients(body.get("recipients")),
        signals=signals,
    )
    reports = state.config.reports.reports
    report = _find(state, body.get("id") or "")
    if report is None:
        report = ReportConfig(**fields)
        reports.append(report)
        mark_current_as_sent(state, report)   # starts with the next period
    else:
        for k, v in fields.items():
            setattr(report, k, v)
    save_reports(state.config.reports, state.config_dir)
    return JSONResponse({"ok": True, "id": report.id})


@router.post("/{report_id}/toggle")
async def toggle_report(report_id: str, state: RuntimeState = Depends(get_state)):
    report = _find(state, report_id)
    if report:
        report.enabled = not report.enabled
        if report.enabled:
            mark_current_as_sent(state, report)
        save_reports(state.config.reports, state.config_dir)
    return RedirectResponse(_BACK, status_code=303)


@router.post("/{report_id}/delete")
async def delete_report(report_id: str, state: RuntimeState = Depends(get_state)):
    state.config.reports.reports = [r for r in state.config.reports.reports if r.id != report_id]
    save_reports(state.config.reports, state.config_dir)
    return RedirectResponse(_BACK, status_code=303)


def _last_period(state: RuntimeState, kind: str) -> P.Period:
    tz = state.config.reports.timezone
    return P.last_completed(kind if kind in P.PERIODS else "daily", P.wall_now(tz), tz)


@router.get("/{report_id}/preview", response_class=HTMLResponse)
async def preview_report(report_id: str, period: str = "daily", state: RuntimeState = Depends(get_state)):
    """The report e-mail of the last completed period, as a web page (nothing is sent)."""
    report = _find(state, report_id)
    if report is None:
        return HTMLResponse("Report not found", status_code=404)
    fig = await compute(state, report, _last_period(state, period))
    return HTMLResponse('<!doctype html><meta charset="utf-8"><title>Report preview</title>'
                        '<body style="margin:24px;background:#fff">'
                        + html_body(fig, state.config.reports.timezone) + "</body>")


@router.post("/{report_id}/send")
async def send_now(report_id: str, period: str = "daily", state: RuntimeState = Depends(get_state)):
    """Send the report of the last completed period now (does not change the schedule)."""
    report = _find(state, report_id)
    if report is None:
        return JSONResponse({"error": "Report not found"}, status_code=404)
    p = _last_period(state, period)
    try:
        await send_report(state, report, p)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "message": f"Sent {p.label} to {', '.join(report.recipients)}"})
