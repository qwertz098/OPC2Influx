"""Quiet setup: a rule whose condition is already met when it is saved sends no alert."""
import asyncio
import types
from datetime import datetime, timedelta, timezone

import pytest

from app.alerting import rule_checker as RC
from app.config import AlarmRuleConfig


class _Mail:
    def __init__(self):
        self.sent = []

    async def send_alert(self, key, subject, body):
        self.sent.append(subject)


@pytest.fixture
def state(minimal_state):
    values = {"sub": 5.0}
    minimal_state.polling_engine = types.SimpleNamespace(live_values=lambda: dict(values))
    minimal_state.alert_manager = _Mail()
    minimal_state._values = values
    return minimal_state


def _rule(state, **kw):
    rule = AlarmRuleConfig(id="r1", name="High", subscription_id="sub", condition=">", threshold="1", **kw)
    state.config.alarm_rules.rules = [rule]
    return rule


def _saved(state, ago=timedelta(0)):
    state.alarm_quiet_setup["r1"] = datetime.now(timezone.utc) - ago


def _tick(checker, state):
    asyncio.run(checker.tick(state))


def test_without_quiet_setup_alerts_at_once(state):
    _rule(state, latch=True, quiet_setup=False)
    _tick(RC.AlarmRuleChecker(), state)
    assert state.alert_manager.sent == ["Alarm: High"]


def test_latch_waits_until_condition_cleared_and_returns(state):
    _rule(state, latch=True)
    _saved(state)
    checker = RC.AlarmRuleChecker()
    _tick(checker, state)
    _tick(checker, state)
    assert state.alert_manager.sent == []
    assert "already met when the rule was saved" in state.active_alarms[-1].message
    state._values["sub"] = 0.0      # clears
    _tick(checker, state)
    state._values["sub"] = 5.0      # comes back → alert
    _tick(checker, state)
    assert state.alert_manager.sent == ["Alarm: High"]


def test_repeat_waits_for_the_cooldown(state):
    _rule(state, latch=False, cooldown_seconds=300)
    _saved(state)
    checker = RC.AlarmRuleChecker()
    _tick(checker, state)
    _tick(checker, state)
    assert state.alert_manager.sent == []
    checker.last_triggered["r1"] -= timedelta(seconds=301)   # cooldown over
    _tick(checker, state)
    assert state.alert_manager.sent == ["Alarm: High"]


def test_condition_not_met_at_save_alerts_normally_later(state):
    _rule(state, latch=True)
    state._values["sub"] = 0.0
    _saved(state)
    checker = RC.AlarmRuleChecker()
    _tick(checker, state)
    assert "r1" not in state.alarm_quiet_setup      # used up by the first check
    state._values["sub"] = 5.0
    _tick(checker, state)
    assert state.alert_manager.sent == ["Alarm: High"]


def test_stale_quiet_mark_is_ignored(state):
    # the first value came long after saving (e.g. server offline): alert normally
    _rule(state, latch=True)
    _saved(state, ago=RC.QUIET_SETUP_WINDOW + timedelta(minutes=1))
    _tick(RC.AlarmRuleChecker(), state)
    assert state.alert_manager.sent == ["Alarm: High"]


def test_no_value_yet_keeps_the_mark(state):
    _rule(state, latch=True)
    state._values.clear()
    _saved(state)
    _tick(RC.AlarmRuleChecker(), state)
    assert "r1" in state.alarm_quiet_setup


@pytest.mark.asyncio
async def test_routes_set_the_quiet_mark(async_client):
    import app.main as main_mod
    for name, extra in (("A", {"quiet_setup": "true"}), ("B", {})):
        resp = await async_client.post("/config/alarm-rules", data={
            "subscription_id": "s1", "condition": ">", "threshold": "1", "name": name, **extra})
        assert resp.status_code in (200, 303)
    state = main_mod.app.state.runtime
    rules = {r.name: r for r in state.config.alarm_rules.rules}
    assert rules["A"].quiet_setup and not rules["B"].quiet_setup
    assert rules["A"].id in state.alarm_quiet_setup and rules["B"].id not in state.alarm_quiet_setup
    # disabling drops the mark, enabling sets it again
    await async_client.post(f"/config/alarm-rules/{rules['A'].id}/toggle")
    assert rules["A"].id not in state.alarm_quiet_setup
    await async_client.post(f"/config/alarm-rules/{rules['A'].id}/toggle")
    assert rules["A"].id in state.alarm_quiet_setup
