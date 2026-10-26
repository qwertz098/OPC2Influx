"""
Runtime state — in-memory only, rebuilt from config on restart.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.config import AppConfig
    from app.opcua.client import OpcUaClientManager
    from app.opcua.poller import PollingEngine
    from app.influxdb.writer import InfluxWriterPool
    from app.alerting.smtp import AlertManager
    from app.watchdog.monitor import WatchdogService
    from app.sqlite.writer import SQLiteWriterPool, SQLiteTargetStatus


@dataclass
class ServerStatus:
    connected: bool = False
    last_connected: datetime | None = None
    last_error: str | None = None
    reconnect_attempts: int = 0
    alerted: bool = False          # True once a "lost" alert has been sent


@dataclass
class InfluxTargetStatus:
    connected: bool = False
    consecutive_failures: int = 0
    writes_total: int = 0       # points successfully written
    writes_partial: int = 0     # points accepted with schema conflicts (partial write)
    writes_attempted: int = 0
    bytes_estimated: int = 0    # rough byte estimate (~150 bytes/point)
    started_at: datetime | None = None
    last_write: datetime | None = None
    last_error: str | None = None
    schema_conflicts: list = field(default_factory=list)  # list of "measurement.field (got X, stored as Y)"
    alerted: bool = False


@dataclass
class Alarm:
    timestamp: datetime
    level: str        # "error" | "warning" | "info"
    source: str       # e.g. "opcua:plc_1" or "influx:primary"
    message: str


@dataclass
class RuntimeState:
    config_dir: object  # Path
    config: "AppConfig"

    # Services (set during lifespan startup)
    opcua_manager: "OpcUaClientManager | None" = None
    polling_engine: "PollingEngine | None" = None
    influx_pool: "InfluxWriterPool | None" = None
    alert_manager: "AlertManager | None" = None
    watchdog: "WatchdogService | None" = None
    sqlite_writer_pool: Any = None

    # Background task handles
    watchdog_task: asyncio.Task | None = None
    folder_scanner_task: asyncio.Task | None = None

    # Live status (updated by background services)
    server_status: dict[str, ServerStatus] = field(default_factory=dict)
    influx_status: dict[str, InfluxTargetStatus] = field(default_factory=dict)
    sqlite_status: dict[str, Any] = field(default_factory=dict)
    active_alarms: list[Alarm] = field(default_factory=list)

    def add_alarm(self, level: str, source: str, message: str) -> None:
        self.active_alarms.append(Alarm(
            timestamp=datetime.now(timezone.utc),
            level=level,
            source=source,
            message=message,
        ))
        # Keep only the last 100 alarms
        if len(self.active_alarms) > 100:
            self.active_alarms = self.active_alarms[-100:]
