from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import AlarmRuleConfig, save_alarm_rules
from app.dependencies import get_state
from app.state import RuntimeState

router = APIRouter(prefix="/config/alarm-rules")
templates = Jinja2Templates(directory="templates")


@router.get("")
async def list_rules():
    return RedirectResponse("/config/alerts", status_code=302)


@router.post("")
async def add_rule(request: Request, state: RuntimeState = Depends(get_state)):
    form = await request.form()
    rule = AlarmRuleConfig(
        name=form.get("name", "").strip(),
        subscription_id=form.get("subscription_id", "").strip(),
        condition=form.get("condition", ">"),
        threshold=form.get("threshold", "0").strip(),
        cooldown_seconds=int(form.get("cooldown_seconds") or 300),
        alert_delay_seconds=int(form.get("alert_delay_seconds") or 0),
        latch=form.get("latch") == "true",
        send_email=form.get("send_email") == "true",
    )
    state.config.alarm_rules.rules.append(rule)
    save_alarm_rules(state.config.alarm_rules, state.config_dir)
    return RedirectResponse("/config/alerts", status_code=303)


@router.get("/{rule_id}/edit", response_class=HTMLResponse)
async def edit_rule(rule_id: str, request: Request, state: RuntimeState = Depends(get_state)):
    rule = next((r for r in state.config.alarm_rules.rules if r.id == rule_id), None)
    if rule is None:
        return HTMLResponse("Not found", status_code=404)
    subs = state.config.subscriptions.subscriptions
    return templates.TemplateResponse(request, "partials/alarm_rule_edit_form.html", {
        "request": request,
        "rule": rule,
        "subscriptions": subs,
    })


@router.post("/{rule_id}/update")
async def update_rule(rule_id: str, request: Request, state: RuntimeState = Depends(get_state)):
    form = await request.form()
    for rule in state.config.alarm_rules.rules:
        if rule.id == rule_id:
            rule.name = form.get("name", rule.name).strip()
            rule.subscription_id = form.get("subscription_id", rule.subscription_id).strip()
            rule.condition = form.get("condition", rule.condition)
            rule.threshold = form.get("threshold", rule.threshold).strip()
            rule.cooldown_seconds = int(form.get("cooldown_seconds") or rule.cooldown_seconds)
            rule.alert_delay_seconds = int(form.get("alert_delay_seconds") or 0)
            rule.latch = form.get("latch") == "true"
            rule.send_email = form.get("send_email") == "true"
            break
    save_alarm_rules(state.config.alarm_rules, state.config_dir)
    return RedirectResponse("/config/alerts", status_code=303)


@router.post("/{rule_id}/delete")
async def delete_rule(rule_id: str, state: RuntimeState = Depends(get_state)):
    state.config.alarm_rules.rules = [
        r for r in state.config.alarm_rules.rules if r.id != rule_id
    ]
    save_alarm_rules(state.config.alarm_rules, state.config_dir)
    return RedirectResponse("/config/alerts", status_code=303)


@router.post("/{rule_id}/toggle")
async def toggle_rule(rule_id: str, state: RuntimeState = Depends(get_state)):
    for rule in state.config.alarm_rules.rules:
        if rule.id == rule_id:
            rule.enabled = not rule.enabled
            break
    save_alarm_rules(state.config.alarm_rules, state.config_dir)
    return RedirectResponse("/config/alerts", status_code=303)
