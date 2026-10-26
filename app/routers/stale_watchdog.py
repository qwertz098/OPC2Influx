from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import StaleWatchdogRuleConfig, save_stale_watchdog
from app.dependencies import get_state
from app.state import RuntimeState

router = APIRouter(prefix="/config/stale-watchdog")
templates = Jinja2Templates(directory="templates")


@router.get("")
async def list_rules():
    return RedirectResponse("/config/alerts", status_code=302)


@router.post("")
async def add_rule(request: Request, state: RuntimeState = Depends(get_state)):
    form = await request.form()
    rule = StaleWatchdogRuleConfig(
        name=form.get("name", "").strip(),
        subscription_id=form.get("subscription_id", "").strip(),
        window_ms=int(form.get("window_ms") or 1000),
        suppress_on_disconnect=form.get("suppress_on_disconnect") == "true",
        send_email=form.get("send_email") == "true",
    )
    state.config.stale_watchdog.rules.append(rule)
    save_stale_watchdog(state.config.stale_watchdog, state.config_dir)
    return RedirectResponse("/config/alerts", status_code=303)


@router.get("/{rule_id}/edit", response_class=HTMLResponse)
async def edit_rule(rule_id: str, request: Request, state: RuntimeState = Depends(get_state)):
    rule = next((r for r in state.config.stale_watchdog.rules if r.id == rule_id), None)
    if rule is None:
        return HTMLResponse("Not found", status_code=404)
    subs = state.config.subscriptions.subscriptions
    return templates.TemplateResponse(request, "partials/stale_watchdog_rule_edit_form.html", {
        "request": request,
        "rule": rule,
        "subscriptions": subs,
    })


@router.post("/{rule_id}/update")
async def update_rule(rule_id: str, request: Request, state: RuntimeState = Depends(get_state)):
    form = await request.form()
    for rule in state.config.stale_watchdog.rules:
        if rule.id == rule_id:
            rule.name = form.get("name", rule.name).strip()
            rule.subscription_id = form.get("subscription_id", rule.subscription_id).strip()
            rule.window_ms = int(form.get("window_ms") or rule.window_ms)
            rule.suppress_on_disconnect = form.get("suppress_on_disconnect") == "true"
            rule.send_email = form.get("send_email") == "true"
            break
    save_stale_watchdog(state.config.stale_watchdog, state.config_dir)
    return RedirectResponse("/config/alerts", status_code=303)


@router.post("/{rule_id}/delete")
async def delete_rule(rule_id: str, state: RuntimeState = Depends(get_state)):
    state.config.stale_watchdog.rules = [
        r for r in state.config.stale_watchdog.rules if r.id != rule_id
    ]
    save_stale_watchdog(state.config.stale_watchdog, state.config_dir)
    return RedirectResponse("/config/alerts", status_code=303)


@router.post("/{rule_id}/toggle")
async def toggle_rule(rule_id: str, state: RuntimeState = Depends(get_state)):
    for rule in state.config.stale_watchdog.rules:
        if rule.id == rule_id:
            rule.enabled = not rule.enabled
            break
    save_stale_watchdog(state.config.stale_watchdog, state.config_dir)
    return RedirectResponse("/config/alerts", status_code=303)
