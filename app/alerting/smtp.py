"""
AlertManager — SMTP email alerts with hysteresis.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Optional

from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from app.config import SmtpConfig, effective_smtp

logger = logging.getLogger(__name__)

COOLDOWN_SECONDS = 300  # suppress duplicate alerts within 5 minutes


class AlertManager:
    def __init__(
        self,
        smtp_cfg: SmtpConfig,
        on_log: Optional[Callable[[str, str, str], None]] = None,
        site_name: str = "",
    ) -> None:
        self._cfg = smtp_cfg
        self._last_sent: dict[str, datetime] = {}
        self._on_log = on_log
        self._site_name = (site_name or "").strip()

    def update_config(self, smtp_cfg: SmtpConfig) -> None:
        self._cfg = smtp_cfg

    async def send_alert(self, key: str, subject: str, body: str) -> None:
        """Send an alert email, suppressing duplicates within COOLDOWN_SECONDS."""
        now = datetime.now(timezone.utc)
        last = self._last_sent.get(key)
        if last and (now - last) < timedelta(seconds=COOLDOWN_SECONDS):
            logger.debug("Alert suppressed (cooldown): %s", key)
            return

        cfg = effective_smtp(self._cfg)
        if not cfg.enabled:
            logger.debug("Email disabled — alert dropped: %s", subject)
            return

        if not cfg.host or not cfg.to_addresses:
            logger.warning("SMTP not configured — alert dropped: %s", subject)
            return

        try:
            await self._send(subject, body)
            self._last_sent[key] = now
            logger.info("Alert sent: %s", subject)
        except Exception as exc:
            logger.error("Failed to send alert email: %s", exc)
            if self._on_log:
                self._on_log("error", "smtp", f"Email send failed: {exc}")

    async def send_test(self) -> str:
        """Send a test email and return 'ok' or an error message."""
        try:
            await self._send(
                subject="Test alert",
                body="This is a test alert from OpcUA_2_InfluxDB. SMTP is working correctly.",
            )
            return "ok"
        except Exception as exc:
            if self._on_log:
                self._on_log("error", "smtp", f"Test email failed: {exc}")
            return str(exc)

    async def send_report(self, recipients: list[str], subject: str, text: str, html: str) -> None:
        """Send a report e-mail to its own recipients; raises on failure."""
        cfg = effective_smtp(self._cfg)
        if not cfg.enabled:
            raise RuntimeError("Email sending is switched off in the settings")
        if not cfg.host:
            raise RuntimeError("SMTP is not configured")
        if not recipients:
            raise RuntimeError("The report has no recipients")
        await self._send(subject, text, to=recipients, html=html)

    async def _send(self, subject: str, body: str, to: Optional[list[str]] = None,
                    html: Optional[str] = None) -> None:
        cfg = effective_smtp(self._cfg)
        msg = MIMEMultipart("alternative")
        prefix = f"[OpcUA2InfluxDB · {self._site_name}]" if self._site_name else "[OpcUA2InfluxDB]"
        msg["Subject"] = f"{prefix} {subject}"
        msg["From"] = cfg.from_address or cfg.username
        msg["To"] = ", ".join(to if to is not None else cfg.to_addresses)
        msg.attach(MIMEText(body, "plain"))
        if html:
            msg.attach(MIMEText(html, "html"))   # last part = preferred by mail clients

        import aiosmtplib   # ~14 MB, loaded on the first alert mail
        await aiosmtplib.send(
            msg,
            hostname=cfg.host,
            port=cfg.port,
            username=cfg.username or None,
            password=cfg.password or None,
            use_tls=False,
            start_tls=cfg.tls,
        )
