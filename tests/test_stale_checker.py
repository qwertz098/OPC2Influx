"""Tests for the stale-value watchdog (StaleValueChecker): fires when a
subscription's value hasn't changed within its rule's window, stays quiet
while the value keeps changing, and respects the per-rule server-disconnect
suppression option (the connectivity Watchdog already covers full outages)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import pytest

from app.config import (
    AppConfig, ServersConfig, SubscriptionsConfig, SubscriptionConfig,
    InfluxDBConfig, AlertsConfig, StaleWatchdogConfig, StaleWatchdogRuleConfig,
)
from app.state import RuntimeState
from app.alerting.stale_checker import StaleValueChecker

pytestmark = pytest.mark.asyncio


@dataclass
class _FakeSubState:
    last_changed_at: float = field(default_factory=time.monotonic)


class _FakeEngine:
    def __init__(self):
        self._sub_state: dict[str, _FakeSubState] = {}


class _FakeMgr:
    def __init__(self, connected: bool = True):
        self._connected = connected

    def is_connected(self, server_id: str) -> bool:
        return self._connected


class _RecordingAlertManager:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_alert(self, key: str, subject: str, body: str) -> None:
        self.sent.append({"key": key, "subject": subject, "body": body})


def _make_state(tmp_path, rule: StaleWatchdogRuleConfig, sub: SubscriptionConfig) -> RuntimeState:
    config = AppConfig(
        servers=ServersConfig(),
        subscriptions=SubscriptionsConfig(subscriptions=[sub]),
        influxdb=InfluxDBConfig(),
        alerts=AlertsConfig(),
        stale_watchdog=StaleWatchdogConfig(rules=[rule]),
    )
    state = RuntimeState(config_dir=tmp_path, config=config)
    state.polling_engine = _FakeEngine()
    state.opcua_manager = _FakeMgr(connected=True)
    state.alert_manager = _RecordingAlertManager()
    return state


def _sub(**kwargs) -> SubscriptionConfig:
    return SubscriptionConfig(server_id="srv1", node_id="ns=2;s=x", enabled=True, **kwargs)


async def test_fires_after_window_elapses_without_change(tmp_path):
    sub = _sub()
    rule = StaleWatchdogRuleConfig(subscription_id=sub.id, window_ms=50)
    state = _make_state(tmp_path, rule, sub)
    state.polling_engine._sub_state[sub.id] = _FakeSubState(last_changed_at=time.monotonic() - 1.0)

    checker = StaleValueChecker()
    await checker.check_once(state)

    assert len(state.alert_manager.sent) == 1
    assert state.alert_manager.sent[0]["key"] == f"stale:{rule.id}"
    assert len(state.active_alarms) == 1
    assert checker._alerted[rule.id] is True


async def test_no_alert_while_value_still_changing(tmp_path):
    sub = _sub()
    rule = StaleWatchdogRuleConfig(subscription_id=sub.id, window_ms=1000)
    state = _make_state(tmp_path, rule, sub)
    state.polling_engine._sub_state[sub.id] = _FakeSubState(last_changed_at=time.monotonic())

    checker = StaleValueChecker()
    await checker.check_once(state)

    assert state.alert_manager.sent == []
    assert state.active_alarms == []


async def test_does_not_realert_every_cycle_while_still_stale(tmp_path):
    sub = _sub()
    rule = StaleWatchdogRuleConfig(subscription_id=sub.id, window_ms=50)
    state = _make_state(tmp_path, rule, sub)
    state.polling_engine._sub_state[sub.id] = _FakeSubState(last_changed_at=time.monotonic() - 1.0)

    checker = StaleValueChecker()
    await checker.check_once(state)
    await checker.check_once(state)
    await checker.check_once(state)

    # AlertManager itself also has a 5-minute email cooldown per key, but the
    # in-process alarm log must independently only fire once per stale episode.
    assert len(state.alert_manager.sent) == 1
    assert len(state.active_alarms) == 1


async def test_rearms_after_value_changes_again(tmp_path):
    sub = _sub()
    rule = StaleWatchdogRuleConfig(subscription_id=sub.id, window_ms=50)
    state = _make_state(tmp_path, rule, sub)
    state.polling_engine._sub_state[sub.id] = _FakeSubState(last_changed_at=time.monotonic() - 1.0)

    checker = StaleValueChecker()
    await checker.check_once(state)
    assert len(state.active_alarms) == 1

    # Value changes again — timestamp bumps, checker must re-arm
    state.polling_engine._sub_state[sub.id].last_changed_at = time.monotonic()
    await checker.check_once(state)
    assert checker._alerted[rule.id] is False

    # ...and goes stale again — must alert a second time
    state.polling_engine._sub_state[sub.id].last_changed_at = time.monotonic() - 1.0
    await checker.check_once(state)
    assert len(state.active_alarms) == 2


async def test_suppressed_on_disconnect_when_option_enabled(tmp_path):
    sub = _sub()
    rule = StaleWatchdogRuleConfig(subscription_id=sub.id, window_ms=50, suppress_on_disconnect=True)
    state = _make_state(tmp_path, rule, sub)
    state.polling_engine._sub_state[sub.id] = _FakeSubState(last_changed_at=time.monotonic() - 1.0)
    state.opcua_manager = _FakeMgr(connected=False)

    checker = StaleValueChecker()
    await checker.check_once(state)

    assert state.alert_manager.sent == []
    assert state.active_alarms == []


async def test_still_fires_on_disconnect_when_suppression_disabled(tmp_path):
    sub = _sub()
    rule = StaleWatchdogRuleConfig(subscription_id=sub.id, window_ms=50, suppress_on_disconnect=False)
    state = _make_state(tmp_path, rule, sub)
    state.polling_engine._sub_state[sub.id] = _FakeSubState(last_changed_at=time.monotonic() - 1.0)
    state.opcua_manager = _FakeMgr(connected=False)

    checker = StaleValueChecker()
    await checker.check_once(state)

    assert len(state.active_alarms) == 1


async def test_disabled_rule_is_skipped(tmp_path):
    sub = _sub()
    rule = StaleWatchdogRuleConfig(subscription_id=sub.id, window_ms=50, enabled=False)
    state = _make_state(tmp_path, rule, sub)
    state.polling_engine._sub_state[sub.id] = _FakeSubState(last_changed_at=time.monotonic() - 1.0)

    checker = StaleValueChecker()
    await checker.check_once(state)

    assert state.active_alarms == []


async def test_send_email_false_skips_email_but_still_logs_alarm(tmp_path):
    sub = _sub()
    rule = StaleWatchdogRuleConfig(subscription_id=sub.id, window_ms=50, send_email=False)
    state = _make_state(tmp_path, rule, sub)
    state.polling_engine._sub_state[sub.id] = _FakeSubState(last_changed_at=time.monotonic() - 1.0)

    checker = StaleValueChecker()
    await checker.check_once(state)

    assert state.alert_manager.sent == []
    assert len(state.active_alarms) == 1


async def test_deleted_subscription_is_skipped(tmp_path):
    """A rule referencing a subscription that no longer exists must not crash."""
    sub = _sub()
    rule = StaleWatchdogRuleConfig(subscription_id="does-not-exist", window_ms=50)
    state = _make_state(tmp_path, rule, sub)
    state.polling_engine._sub_state[sub.id] = _FakeSubState(last_changed_at=time.monotonic() - 1.0)

    checker = StaleValueChecker()
    await checker.check_once(state)

    assert state.active_alarms == []
