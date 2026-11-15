"""
AlarmRuleChecker — evaluates threshold-based alarm rules every second.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.state import RuntimeState

logger = logging.getLogger(__name__)


def _as_bool(raw) -> bool:
    if isinstance(raw, (bool, int, float)):
        return bool(raw)
    return str(raw).strip().lower() in ("true", "1", "yes")


def _evaluate(raw, condition: str, threshold_str: str) -> bool:
    """Return True if the alarm condition is met.

    Booleans count as 1 (true) and 0 (false), so a bool signal works with a
    threshold of true/false as well as 1/0, and with every condition:
    "> 0", "= true" and "<> false" all fire while the signal is true.
    """
    t = threshold_str.strip().lower()

    if t in ("true", "false"):
        # Bool threshold: any non-zero number or "true"/"1"/"yes" counts as true
        num_thresh = 1.0 if t == "true" else 0.0
        num_val = 1.0 if _as_bool(raw) else 0.0
    else:
        try:
            num_thresh = float(t)
            if isinstance(raw, str) and raw.strip().lower() in ("true", "false"):
                num_val = 1.0 if raw.strip().lower() == "true" else 0.0
            else:
                num_val = float(raw)   # bool → 1.0 / 0.0
        except (TypeError, ValueError):
            return False

    if condition == "<":
        return num_val < num_thresh
    if condition == ">":
        return num_val > num_thresh
    if condition == "=":
        return num_val == num_thresh
    if condition == "<>":
        return num_val != num_thresh
    return False


QUIET_SETUP_WINDOW = timedelta(minutes=10)   # how long a saved rule waits for its first value


class AlarmRuleChecker:
    CHECK_INTERVAL = 1.0  # seconds

    def __init__(self) -> None:
        self.last_triggered: dict[str, datetime] = {}
        self.condition_was_active: dict[str, bool] = {}   # latch: was condition True last tick
        self.condition_since: dict[str, datetime] = {}    # alert delay: when condition first became True

    async def run(self, state: "RuntimeState") -> None:
        while True:
            try:
                await asyncio.sleep(self.CHECK_INTERVAL)
                await self.tick(state)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error("AlarmRuleChecker error: %s", exc)

    async def tick(self, state: "RuntimeState") -> None:
        """Check every enabled rule once against the latest polled values."""
        if not state.polling_engine:
            return
        if not any(r.enabled for r in state.config.alarm_rules.rules):
            return

        last_values = state.polling_engine.live_values()
        last_triggered = self.last_triggered
        condition_was_active = self.condition_was_active
        condition_since = self.condition_since

        for rule in state.config.alarm_rules.rules:
            if not rule.enabled:
                continue
            raw = last_values.get(rule.subscription_id)
            if raw is None:
                continue

            condition_met = _evaluate(raw, rule.condition, rule.threshold)
            now = datetime.now(timezone.utc)
            sub = next(
                (s for s in state.config.subscriptions.subscriptions
                 if s.id == rule.subscription_id),
                None,
            )
            label = rule.name or (sub.display_name if sub else rule.subscription_id)

            # Quiet setup: first check after the rule was saved / enabled.
            # A condition that is already met counts as reported: latch
            # waits until it clears, repeat until the cooldown has passed.
            saved_at = state.alarm_quiet_setup.pop(rule.id, None)
            if saved_at is not None and condition_met and now - saved_at < QUIET_SETUP_WINDOW:
                condition_since.setdefault(rule.id, now)
                condition_was_active[rule.id] = True
                last_triggered[rule.id] = now
                state.add_alarm("info", "alarm",
                                f"{label}: condition already met when the rule was saved "
                                f"({raw} {rule.condition} {rule.threshold}), no alert sent")
                continue

            # Track how long condition has been continuously True (alert delay)
            if condition_met:
                if rule.id not in condition_since:
                    condition_since[rule.id] = now
            else:
                condition_since.pop(rule.id, None)
                condition_was_active[rule.id] = False  # re-arm latch
                continue

            # Alert delay: condition must hold for alert_delay_seconds before firing
            if rule.alert_delay_seconds > 0:
                since = condition_since.get(rule.id, now)
                if (now - since).total_seconds() < rule.alert_delay_seconds:
                    continue

            if rule.latch:
                was_active = condition_was_active.get(rule.id, False)
                condition_was_active[rule.id] = True
                if was_active:
                    continue  # already fired; wait for condition to clear
            else:
                condition_was_active[rule.id] = True
                # Check cooldown
                prev = last_triggered.get(rule.id)
                if prev and (now - prev).total_seconds() < rule.cooldown_seconds:
                    continue

            last_triggered[rule.id] = now
            msg = f"{label}: {raw} {rule.condition} {rule.threshold}"

            state.add_alarm("warning", "alarm", msg)
            logger.warning("Alarm triggered: %s", msg)

            if state.alert_manager and rule.send_email:
                await state.alert_manager.send_alert(
                    key=f"alarm:{rule.id}",
                    subject=f"Alarm: {label}",
                    body=msg,
                )
