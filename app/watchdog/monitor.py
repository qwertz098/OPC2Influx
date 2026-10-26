"""
WatchdogService — monitors OPC UA and InfluxDB connections, triggers alerts.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.state import RuntimeState

logger = logging.getLogger(__name__)


class WatchdogService:
    def __init__(self, state: RuntimeState) -> None:
        self._state = state
        self._disconnect_since: dict[str, datetime] = {}

    async def run(self) -> None:
        while True:
            try:
                interval = self._state.config.alerts.watchdog.check_interval_seconds
                await asyncio.sleep(interval)
                await self._check()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error("Watchdog error: %s", exc)

    async def _check(self) -> None:
        alert_mgr = self._state.alert_manager
        cfg = self._state.config.alerts.watchdog
        delay = timedelta(seconds=cfg.connection_loss_alert_delay_seconds)
        now = datetime.now(timezone.utc)

        # Check OPC UA servers
        for srv in self._state.config.servers.servers:
            if not srv.enabled:
                continue
            status = self._state.server_status.get(srv.id)
            if status is None:
                continue

            key = f"opcua:{srv.id}"
            if not status.connected:
                if key not in self._disconnect_since:
                    self._disconnect_since[key] = now
                elif now - self._disconnect_since[key] >= delay:
                    if alert_mgr and not status.alerted:
                        await alert_mgr.send_alert(
                            key=key,
                            subject=f"Connection lost: {srv.display_name}",
                            body=(
                                f"OPC UA server '{srv.display_name}' ({srv.endpoint_url}) "
                                f"has been unreachable since {self._disconnect_since[key].isoformat()}.\n"
                                f"Last error: {status.last_error}\n"
                                f"Reconnect attempts: {status.reconnect_attempts}"
                            ),
                        )
                        status.alerted = True
                        self._state.add_alarm("error", key, f"OPC UA connection lost: {srv.display_name}")
            else:
                if key in self._disconnect_since:
                    del self._disconnect_since[key]
                if status.alerted and cfg.recovery_alert:
                    if alert_mgr:
                        await alert_mgr.send_alert(
                            key=f"{key}:recovery",
                            subject=f"Connection restored: {srv.display_name}",
                            body=f"OPC UA server '{srv.display_name}' is back online at {now.isoformat()}.",
                        )
                    status.alerted = False
                    self._state.add_alarm("info", key, f"OPC UA connection restored: {srv.display_name}")

        # Check InfluxDB targets
        for target in self._state.config.influxdb.targets:
            if not target.enabled:
                continue
            status = self._state.influx_status.get(target.id)
            if status is None:
                continue

            key = f"influx:{target.id}"
            if not status.connected and status.consecutive_failures >= 5:
                if alert_mgr and not status.alerted:
                    await alert_mgr.send_alert(
                        key=key,
                        subject=f"InfluxDB write failures: {target.display_name}",
                        body=(
                            f"InfluxDB target '{target.display_name}' ({target.url}) "
                            f"has {status.consecutive_failures} consecutive write failures.\n"
                            f"Last error: {status.last_error}"
                        ),
                    )
                    status.alerted = True
                    self._state.add_alarm("error", key, f"InfluxDB unavailable: {target.display_name}")
            elif status.connected and status.alerted:
                if alert_mgr and cfg.recovery_alert:
                    await alert_mgr.send_alert(
                        key=f"{key}:recovery",
                        subject=f"InfluxDB recovered: {target.display_name}",
                        body=f"InfluxDB target '{target.display_name}' is writing successfully again.",
                    )
                status.alerted = False
                self._state.add_alarm("info", key, f"InfluxDB recovered: {target.display_name}")
