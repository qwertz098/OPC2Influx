"""Unit tests for watchdog alert hysteresis."""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.alerting.smtp import AlertManager, COOLDOWN_SECONDS
from app.config import SmtpConfig


def _mgr_with_mock() -> tuple:
    """Return (AlertManager, sent_list) with SMTP configured and _send mocked."""
    mgr = AlertManager(SmtpConfig(host="smtp.test.local", to_addresses=["test@test.com"]))
    sent = []

    async def mock_send(subject, body):
        sent.append(subject)

    mgr._send = mock_send
    return mgr, sent


@pytest.mark.asyncio
async def test_alert_suppressed_within_cooldown():
    mgr, sent = _mgr_with_mock()
    await mgr.send_alert("test_key", "First alert", "body")
    await mgr.send_alert("test_key", "Second alert - should be suppressed", "body")
    assert len(sent) == 1  # only first goes through


@pytest.mark.asyncio
async def test_alert_sent_after_cooldown():
    mgr, sent = _mgr_with_mock()
    # Fake that last sent was COOLDOWN_SECONDS + 1 ago
    from datetime import timezone
    past = datetime.now(timezone.utc) - timedelta(seconds=COOLDOWN_SECONDS + 1)
    mgr._last_sent["test_key2"] = past
    await mgr.send_alert("test_key2", "Should send", "body")
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_no_smtp_config_does_not_raise():
    # Empty host/to_addresses → should log a warning, not raise
    mgr = AlertManager(SmtpConfig(host="", to_addresses=[]))
    await mgr.send_alert("k", "subject", "body")


@pytest.mark.asyncio
async def test_different_keys_not_suppressed():
    mgr, sent = _mgr_with_mock()
    await mgr.send_alert("key_a", "Alert A", "body")
    await mgr.send_alert("key_b", "Alert B", "body")
    assert len(sent) == 2
