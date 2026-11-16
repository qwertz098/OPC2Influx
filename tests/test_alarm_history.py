"""Alarm rules with values before and after the alarm in their e-mail."""
import asyncio
import email
import types
from datetime import datetime, timedelta, timezone

import pytest

from app.alerting import rule_checker as RC
from app.alerting.history_mail import render
from app.alerting.smtp import AlertManager
from app.config import AlarmHistoryConfig, AlarmHistorySignalConfig, AlarmRuleConfig, SmtpConfig, SubscriptionConfig
from app.state import ServerStatus

T0 = datetime.now(timezone.utc) - timedelta(hours=1)   # in the past: rows after an alarm need no waiting


class _Mail:
    def __init__(self):
        self.sent = []

    async def send_alert(self, key, subject, body, html=None, attachments=None):
        self.sent.append({"subject": subject, "body": body, "html": html, "attachments": attachments})


@pytest.fixture
def state(minimal_state):
    minimal_state.config.subscriptions.subscriptions = [
        SubscriptionConfig(id="t", server_id="srv", node_id="n1", display_name="Temp"),
        SubscriptionConfig(id="p", server_id="srv", node_id="n2", display_name="Pressure"),
    ]
    minimal_state.server_status["srv"] = ServerStatus(connected=True)
    live = {"t": 20.0, "p": 1.0}
    minimal_state.polling_engine = types.SimpleNamespace(live_values=lambda: dict(live))
    minimal_state.alert_manager = _Mail()
    minimal_state._live = live
    return minimal_state


def _rule(state, history=True, **kw):
    h = AlarmHistoryConfig(enabled=history, pre_seconds=3, post_seconds=2, step_seconds=1,
                           signals=[AlarmHistorySignalConfig(subscription_id="p", unit="bar")])
    kw.setdefault("latch", True)
    rule = AlarmRuleConfig(id="r1", name="Hot", subscription_id="t", condition=">", threshold="50",
                           quiet_setup=False, history=h, **kw)
    state.config.alarm_rules.rules = [rule]
    return rule


def _run(checker, state, seconds, change, start=0):
    async def go():
        for i in range(start, start + seconds):
            change(i)
            await checker.tick(state, now=T0 + timedelta(seconds=i))
            await asyncio.sleep(0)
        await asyncio.gather(*checker._tasks)
    asyncio.run(go())


def test_mail_with_rows_before_and_after(state):
    _rule(state)
    checker = RC.AlarmRuleChecker()

    def change(i):
        state._live["t"] = 10.0 * i          # above 50 from second 6 on
        state._live["p"] = 1.0 + i
    _run(checker, state, 8, change)
    assert len(state.alert_manager.sent) == 1
    mail = state.alert_manager.sent[0]
    assert mail["subject"] == "Alarm: Hot"
    name, csv_text = mail["attachments"][0]
    assert name.startswith("alarm_") and name.endswith(".csv")
    lines = csv_text.strip().splitlines()
    assert lines[0].split(",")[2:] == ["Offset (s)", "Temp", "Pressure [bar]", "Quality"]
    assert [ln.split(",")[2] for ln in lines[1:]] == ["-3", "-2", "-1", "0", "+1", "+2"]
    assert [ln.split(",")[4] for ln in lines[1:]] == ["4", "5", "6", "7", "8", "8"]   # pressure
    assert "background:#ffe9a8" in mail["html"] and "Temp" in mail["body"]
    assert "Hot: 60.0 > 50" in state.active_alarms[-1].message        # event listed at once


def test_event_listed_before_mail_and_no_new_alarm_while_collecting(state):
    _rule(state, latch=False, cooldown_seconds=1)
    checker = RC.AlarmRuleChecker()
    state._live["t"] = 99.0

    async def go():
        await checker.tick(state, now=T0)
        assert "r1" in checker.busy and state.alert_manager.sent == []
        assert any("Hot" in a.message for a in state.active_alarms)
        # while busy: no evaluation, no second alarm even though the cooldown has passed
        n = len(state.active_alarms)
        await checker.tick(state, now=T0 + timedelta(seconds=1))
        assert len(state.active_alarms) == n
        await asyncio.gather(*checker._tasks)
        assert len(state.alert_manager.sent) == 1 and not checker.busy
    asyncio.run(go())


def test_latch_needs_clear_after_post_run(state):
    _rule(state)
    checker = RC.AlarmRuleChecker()
    _run(checker, state, 6, lambda i: state._live.update(t=99.0 if i >= 1 else 0.0))
    assert len(state.alert_manager.sent) == 1
    _run(checker, state, 3, lambda i: state._live.update(t=99.0), start=6)        # still true: nothing
    assert len(state.alert_manager.sent) == 1
    _run(checker, state, 6, lambda i: state._live.update(t=0.0 if i == 9 else 99.0), start=9)   # clears, back
    assert len(state.alert_manager.sent) == 2


def test_without_history_mail_unchanged(state):
    _rule(state, history=False)
    checker = RC.AlarmRuleChecker()
    _run(checker, state, 2, lambda i: state._live.update(t=99.0 if i else 0.0))
    assert state.alert_manager.sent[0]["attachments"] is None
    assert checker.history.owners() == [] and not checker.busy


def test_history_off_without_send_email(state):
    _rule(state, send_email=False)
    checker = RC.AlarmRuleChecker()
    _run(checker, state, 3, lambda i: state._live.update(t=99.0 if i else 0.0))
    assert state.alert_manager.sent == [] and checker.history.owners() == []


def test_alarm_signal_listed_once(state):
    rule = _rule(state)
    rule.history.signals.append(AlarmHistorySignalConfig(subscription_id="t"))
    assert RC.AlarmRuleChecker.history_signals(rule) == [("t", ""), ("p", "bar")]


def test_render_large_history_csv_only():
    from app.references.store import RefValue
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [(t0 + timedelta(seconds=i), float(i - 60), [RefValue("t", "Temp", "", float(i))]) for i in range(121)]
    text, html, csv_text = render(rows, "Alarm")
    assert "<table" not in html and "121 rows × 1 signals" in text and "attached CSV" in html
    assert len(csv_text.strip().splitlines()) == 122
    text, html, _ = render(rows[:100], "Alarm")
    assert "<table" in html


def test_render_empty():
    text, html, csv_text = render([], "x")
    assert text == "x" and csv_text == ""


def test_mime_with_attachment(monkeypatch):
    mgr = AlertManager(SmtpConfig(enabled=True, host="h", to_addresses=["a@b.c"]))
    sent = []

    async def fake_send(msg, **kw):
        sent.append(msg)
    import aiosmtplib
    monkeypatch.setattr(aiosmtplib, "send", fake_send)
    asyncio.run(mgr._send("S", "plain", html="<p>h</p>", attachments=[("a.csv", "x,y\n1,2\n")]))
    msg = email.message_from_string(sent[0].as_string())
    parts = [p.get_content_type() for p in msg.walk()]
    assert parts == ["multipart/mixed", "multipart/alternative", "text/plain", "text/html", "text/csv"]
    assert msg.get_payload()[1].get_filename() == "a.csv"


def test_form_parses_history(state):
    from starlette.datastructures import FormData
    from app.routers.alarm_rules import _history, choice_label
    pressure = state.config.subscriptions.subscriptions[1]
    form = FormData([("history_enabled", "true"), ("history_pre", "120"), ("history_post", "9999"),
                     ("history_step", "5"),
                     ("history_signal", choice_label(pressure)), ("history_unit", "bar"),
                     ("history_signal", "unknown"), ("history_unit", ""),
                     ("history_signal", "p"), ("history_unit", "")])        # duplicate by ID
    h = _history(state, form)
    assert h.enabled and h.pre_seconds == 120 and h.post_seconds == 3600 and h.step_seconds == 5
    assert [(s.subscription_id, s.unit) for s in h.signals] == [("p", "bar")]


@pytest.mark.asyncio
async def test_alerts_page_renders_history_fields(async_client):
    await async_client.post("/config/alarm-rules", data={
        "subscription_id": "x", "condition": ">", "threshold": "1", "send_email": "true",
        "history_enabled": "true", "history_pre": "10"})
    await async_client.post("/config/stale-watchdog", data={"subscription_id": "x", "window_seconds": "60"})
    r = await async_client.get("/config/alerts")
    assert "Values in mail: 10 s before" in r.text
    assert r.status_code == 200 and 'name="history_enabled"' in r.text and 'id="histSubList"' in r.text
