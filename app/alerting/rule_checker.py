"""
AlarmRuleChecker — evaluates threshold-based alarm rules every second.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.state import RuntimeState

logger = logging.getLogger(__name__)


def _evaluate(raw, condition: str, threshold_str: str) -> bool:
    """Return True if the alarm condition is met."""
    t = threshold_str.strip().lower()

    # Boolean comparison
    if t in ("true", "false"):
        bool_thresh = t == "true"
        if isinstance(raw, bool):
            val = raw
        elif isinstance(raw, (int, float)):
            val = bool(raw)
        else:
            val = str(raw).strip().lower() in ("true", "1", "yes")
        if condition == "=":
            return val == bool_thresh
        if condition == "<>":
            return val != bool_thresh
        return False

    # Numeric comparison
    try:
        num_thresh = float(t)
        if isinstance(raw, bool):
            num_val = float(raw)
        else:
            num_val = float(raw)
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


class AlarmRuleChecker:
    CHECK_INTERVAL = 1.0  # seconds

    async def run(self, state: "RuntimeState") -> None:
        last_triggered: dict[str, datetime] = {}
        condition_was_active: dict[str, bool] = {}   # latch: was condition True last tick
        condition_since: dict[str, datetime] = {}    # alert delay: when condition first became True

        while True:
            try:
                await asyncio.sleep(self.CHECK_INTERVAL)
                if not state.polling_engine:
                    continue

                last_values = state.polling_engine.live_values()

                for rule in state.config.alarm_rules.rules:
                    if not rule.enabled:
                        continue
                    raw = last_values.get(rule.subscription_id)
                    if raw is None:
                        continue

                    condition_met = _evaluate(raw, rule.condition, rule.threshold)
                    now = datetime.now(timezone.utc)

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

                    sub = next(
                        (s for s in state.config.subscriptions.subscriptions
                         if s.id == rule.subscription_id),
                        None,
                    )
                    label = rule.name or (sub.display_name if sub else rule.subscription_id)
                    msg = f"{label}: {raw} {rule.condition} {rule.threshold}"

                    state.add_alarm("warning", "alarm", msg)
                    logger.warning("Alarm triggered: %s", msg)

                    if state.alert_manager and rule.send_email:
                        await state.alert_manager.send_alert(
                            key=f"alarm:{rule.id}",
                            subject=f"Alarm: {label}",
                            body=msg,
                        )

            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error("AlarmRuleChecker error: %s", exc)
