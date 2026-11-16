from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import AlarmHistoryConfig, AlarmHistorySignalConfig, AlarmRuleConfig, save_alarm_rules
from app.dependencies import get_state
from app.state import RuntimeState


def _quiet(state: RuntimeState, rule: AlarmRuleConfig) -> None:
    """Remember a just saved / enabled rule: if its condition is already met at
    the next check, that counts as already reported (see AlarmRuleChecker)."""
    if rule.quiet_setup and rule.enabled:
        state.alarm_quiet_setup[rule.id] = datetime.now(timezone.utc)
    else:
        state.alarm_quiet_setup.pop(rule.id, None)

def choice_label(sub) -> str:
    """How a signal is offered in the pickers of the alerts page."""
    name = sub.display_name or sub.node_id
    return name if name == sub.influxdb.measurement else f"{name} · {sub.influxdb.measurement}"


def _int(form, name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(form.get(name) or default)))
    except ValueError:
        return default


def _history(state: RuntimeState, form) -> AlarmHistoryConfig:
    """The value history fields of the rule form (signals by label or ID)."""
    by_label = {choice_label(s): s.id for s in state.config.subscriptions.subscriptions}
    ids = {s.id for s in state.config.subscriptions.subscriptions}
    signals = []
    for text, unit in zip(form.getlist("history_signal"), form.getlist("history_unit")):
        text = text.strip()
        sub_id = by_label.get(text) or (text if text in ids else "")
        if sub_id and sub_id not in (x.subscription_id for x in signals):
            signals.append(AlarmHistorySignalConfig(subscription_id=sub_id, unit=unit.strip()))
    return AlarmHistoryConfig(
        enabled=form.get("history_enabled") == "true",
        signals=signals,
        pre_seconds=_int(form, "history_pre", 60, 0, 3600),
        post_seconds=_int(form, "history_post", 30, 0, 3600),
        step_seconds=_int(form, "history_step", 1, 1, 3600),
    )


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
        quiet_setup=form.get("quiet_setup") == "true",
        history=_history(state, form),
    )
    state.config.alarm_rules.rules.append(rule)
    _quiet(state, rule)
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
        "hist_labels": {s.id: choice_label(s) for s in subs},
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
            rule.quiet_setup = form.get("quiet_setup") == "true"
            rule.history = _history(state, form)
            _quiet(state, rule)
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
            _quiet(state, rule)
            break
    save_alarm_rules(state.config.alarm_rules, state.config_dir)
    return RedirectResponse("/config/alerts", status_code=303)
