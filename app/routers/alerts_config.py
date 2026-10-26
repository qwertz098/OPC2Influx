from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import AlertsConfig, WatchdogConfig, save_alerts
from app.dependencies import get_state
from app.state import RuntimeState

router = APIRouter(prefix="/config/alerts")
templates = Jinja2Templates(directory="templates")


@router.get("", response_class=HTMLResponse)
async def alerts_page(request: Request, state: RuntimeState = Depends(get_state)):
    return templates.TemplateResponse(request, "config/alerts.html", {
        "request": request,
        "alerts": state.config.alerts,
        "rules": state.config.alarm_rules.rules,
        "stale_rules": state.config.stale_watchdog.rules,
        "subscriptions": state.config.subscriptions.subscriptions,
    })


@router.post("", response_class=HTMLResponse)
async def save_alerts_config(request: Request, state: RuntimeState = Depends(get_state)):
    form = await request.form()
    to_raw = form.get("to_addresses", "")
    to_list = [e.strip() for e in to_raw.split("\n") if e.strip()]

    state.config.alerts = AlertsConfig(
        watchdog=WatchdogConfig(
            check_interval_seconds=int(form.get("check_interval", "30")),
            connection_loss_alert_delay_seconds=int(form.get("alert_delay", "60")),
            recovery_alert=form.get("recovery_alert", "false") == "true",
        ),
        smtp=state.config.alerts.smtp,  # unchanged — managed from Settings
    )
    save_alerts(state.config.alerts, state.config_dir)
    return HTMLResponse('<span class="badge badge-ok save-confirm">✓ Saved</span>')


@router.post("/test", response_class=HTMLResponse)
async def test_alert(request: Request, state: RuntimeState = Depends(get_state)):
    if state.alert_manager is None:
        return HTMLResponse('<span class="badge badge-error">Alert manager not initialized</span>')
    result_msg = await state.alert_manager.send_test()
    result = "ok" if result_msg == "ok" else "error"
    return templates.TemplateResponse(request, "partials/test_result.html", {
        "request": request,
        "result": result,
        "message": "Test email sent successfully" if result == "ok" else result_msg,
    })
