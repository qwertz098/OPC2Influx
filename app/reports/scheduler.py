"""
ReportScheduler — sends the daily / weekly / monthly reports.

Every 30 s it looks for the most recent period of each enabled report whose
send time has passed. What was sent is kept in reports_state.json in the data
directory, so a restart neither repeats a report nor skips one: a report that
fell due while the gateway was off goes out once it runs again (only the latest
period, older ones are not sent afterwards).
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from app.config import ReportConfig, resolve_data_path
from app.reports import periods as P
from app.reports.compute import compute
from app.reports.render import html_body, subject, text_body

if TYPE_CHECKING:
    from app.state import RuntimeState

logger = logging.getLogger(__name__)

CHECK_INTERVAL = 30            # seconds
RETRY_AFTER = timedelta(minutes=10)


def state_path() -> Path:
    return Path(resolve_data_path("reports_state.json"))


def load_sent() -> dict:
    try:
        return json.loads(state_path().read_text())
    except (OSError, ValueError):
        return {}


def save_sent(sent: dict) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(sent, indent=1, sort_keys=True))
    tmp.replace(path)


def mark_current_as_sent(state: "RuntimeState", report: ReportConfig) -> None:
    """A new or changed report starts with the next period, not with the one already due."""
    cfg = state.config.reports
    now = P.wall_now(cfg.timezone)
    send_time = P.parse_send_time(cfg.send_time)
    sent = load_sent()
    entry = sent.setdefault(report.id, {})
    for kind in P.PERIODS:
        entry.setdefault(kind, P.due_period(kind, now, send_time, cfg.timezone).key)
    save_sent(sent)


async def send_report(state: "RuntimeState", report: ReportConfig, period: P.Period) -> None:
    """Compute and mail one report; raises on failure."""
    if not state.alert_manager:
        raise RuntimeError("Email is not available")
    tz_mode = state.config.reports.timezone
    fig = await compute(state, report, period)
    await state.alert_manager.send_report(report.recipients, subject(fig),
                                          text_body(fig, tz_mode), html_body(fig, tz_mode))
    logger.info("Report sent: %s %s to %s", report.name, period.key, ", ".join(report.recipients))


class ReportScheduler:
    def __init__(self) -> None:
        self._failed: dict[tuple[str, str], datetime] = {}   # (report, kind) → last failure

    async def run(self, state: "RuntimeState") -> None:
        while True:
            try:
                await asyncio.sleep(CHECK_INTERVAL)
                await self.tick(state)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error("ReportScheduler error: %s", exc)

    async def tick(self, state: "RuntimeState") -> None:
        cfg = state.config.reports
        reports = [r for r in cfg.reports if r.enabled and r.signals and r.recipients]
        if not reports:
            return
        now = P.wall_now(cfg.timezone)
        send_time = P.parse_send_time(cfg.send_time)
        sent = load_sent()
        for report in reports:
            for kind in P.PERIODS:
                if not getattr(report, kind):
                    continue
                period = P.due_period(kind, now, send_time, cfg.timezone)
                if sent.get(report.id, {}).get(kind) == period.key:
                    continue
                failed = self._failed.get((report.id, kind))
                if failed and datetime.now(timezone.utc) - failed < RETRY_AFTER:
                    continue
                try:
                    await send_report(state, report, period)
                except Exception as exc:
                    self._failed[(report.id, kind)] = datetime.now(timezone.utc)
                    logger.error("Report %s (%s) failed: %s", report.name, period.key, exc)
                    state.add_alarm("error", "report", f"Report {report.name} {period.key} failed: {exc}")
                    continue
                self._failed.pop((report.id, kind), None)
                sent.setdefault(report.id, {})[kind] = period.key
                save_sent(sent)
