"""
StaleValueChecker — evaluates stale-value watchdog rules every second.

Each rule watches one subscription and fires when its value has not changed
within the configured window. Distinct from AlarmRuleChecker (threshold
conditions) and WatchdogService (server/InfluxDB connectivity) — a signal can
be technically "live" (server connected, subscription polling successfully)
while still reporting a frozen value, which neither of those catch.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.state import RuntimeState

logger = logging.getLogger(__name__)


class StaleValueChecker:
    CHECK_INTERVAL = 1.0  # seconds

    def __init__(self) -> None:
        self._alerted: dict[str, bool] = {}  # rule.id → already alerted for the current stale episode

    async def run(self, state: "RuntimeState") -> None:
        while True:
            try:
                await asyncio.sleep(self.CHECK_INTERVAL)
                await self.check_once(state)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error("StaleValueChecker error: %s", exc)

    async def check_once(self, state: "RuntimeState") -> None:
        """Evaluate all rules once. Split out from run() so it can be driven
        directly (tests) without a real sleep/loop."""
        engine = state.polling_engine
        mgr = state.opcua_manager
        if engine is None:
            return

        for rule in state.config.stale_watchdog.rules:
            if not rule.enabled:
                continue

            sub = next(
                (s for s in state.config.subscriptions.subscriptions
                 if s.id == rule.subscription_id),
                None,
            )
            if sub is None or not sub.enabled:
                continue

            if rule.suppress_on_disconnect and (mgr is None or not mgr.is_connected(sub.server_id)):
                continue

            sub_state = engine._sub_state.get(sub.id)
            if sub_state is None:
                continue  # no poll has completed for this subscription yet

            elapsed = time.monotonic() - sub_state.last_changed_at
            window_seconds = rule.window_ms / 1000.0

            if elapsed <= window_seconds:
                self._alerted[rule.id] = False  # value is changing again — re-arm
                continue

            if self._alerted.get(rule.id):
                continue  # already alerted for this stale episode

            self._alerted[rule.id] = True
            label = rule.name or sub.display_name or sub.node_id
            msg = (
                f"{label}: value unchanged for {elapsed:.1f}s "
                f"(window {rule.window_ms} ms)"
            )

            state.add_alarm("warning", "stale", msg)
            logger.warning("Stale value: %s", msg)

            if state.alert_manager and rule.send_email:
                await state.alert_manager.send_alert(
                    key=f"stale:{rule.id}",
                    subject=f"Stale value: {label}",
                    body=msg,
                )
