"""
OpcUA_2_InfluxDB — main FastAPI application.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import load_config
from app.state import RuntimeState
from app.opcua.client import OpcUaClientManager
from app.opcua.poller import PollingEngine
from app.influxdb.writer import InfluxWriterPool
from app.alerting.smtp import AlertManager
from app.watchdog.monitor import WatchdogService
from app.sqlite.writer import SQLiteWriterPool
from app.alerting.rule_checker import AlarmRuleChecker
from app.alerting.stale_checker import StaleValueChecker
from app.opcua.folder_scanner import FolderScanner

from app.routers import (
    dashboard, opcua_browser, subscriptions,
    server_config, influxdb_config, alerts_config,
)
from app.routers import sqlite_config, config_backup, tags_config, alarm_rules, data_viewer
from app.routers import chart, help_page, dynamic_folders, app_settings, stale_watchdog

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


_APP_VERSION = str(int(time.time()))

# site_name: env var SITE_NAME overrides config (useful for Docker).
# Resolved at startup; UI settings apply the config value when env var is absent.
_SITE_NAME_ENV = os.environ.get("SITE_NAME", "").strip()
_SITE_NAME = _SITE_NAME_ENV  # resolved in lifespan after config is loaded

_all_template_envs: list = []  # collected by _make_jinja_env


def effective_site_name(config_value: str) -> str:
    """Env var takes precedence over config."""
    return _SITE_NAME_ENV or config_value


def update_site_name(name: str) -> None:
    """Push a new site_name to all registered Jinja2 environments."""
    global _SITE_NAME
    _SITE_NAME = name
    for env in _all_template_envs:
        env.globals["site_name"] = name


def _make_jinja_env(templates: Jinja2Templates) -> None:
    """Register custom Jinja2 filters and globals."""
    _all_template_envs.append(templates.env)
    templates.env.globals["app_version"] = _APP_VERSION
    templates.env.globals["site_name"] = _SITE_NAME
    import re
    def slugify(value: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_-]", "_", str(value))
    templates.env.filters["slugify"] = slugify

    def format_dt(value) -> str:
        if value is None:
            return "—"
        return value.strftime("%Y-%m-%d %H:%M:%S")
    templates.env.filters["format_dt"] = format_dt

    def interval_label(ms: int) -> str:
        if ms < 1000:
            return f"{ms} ms"
        if ms < 60000:
            return f"{ms // 1000} s"
        return f"{ms // 60000} min"

    def heartbeat_label(s: int) -> str:
        if s == 0:
            return "Never"
        if s < 60:
            return f"{s} s"
        if s < 3600:
            return f"{s // 60} min"
        return f"{s // 3600} h"

    templates.env.globals["interval_label"] = interval_label
    templates.env.globals["heartbeat_label"] = heartbeat_label


@asynccontextmanager
async def lifespan(app: FastAPI):
    config_dir = Path(os.environ.get("APP_CONFIG_DIR", os.environ.get("CONFIG_DIR", "config")))
    config = load_config(config_dir)

    state = RuntimeState(config_dir=config_dir, config=config)
    app.state.runtime = state

    # Wire up services
    state.opcua_manager = OpcUaClientManager(state)
    state.polling_engine = PollingEngine(state)
    state.influx_pool = InfluxWriterPool(state)
    _resolved = effective_site_name(config.app_settings.site_name)
    update_site_name(_resolved)
    state.alert_manager = AlertManager(config.alerts.smtp, on_log=state.add_alarm, site_name=_resolved)
    state.watchdog = WatchdogService(state)
    state.sqlite_writer_pool = SQLiteWriterPool(state)

    # Start services
    await state.influx_pool.start()
    await state.sqlite_writer_pool.start()
    await state.opcua_manager.start_all()
    await state.polling_engine.reconcile()

    state.watchdog_task = asyncio.create_task(state.watchdog.run(), name="watchdog")
    asyncio.create_task(AlarmRuleChecker().run(state), name="alarm-checker")
    asyncio.create_task(StaleValueChecker().run(state), name="stale-watchdog")
    _scanner = FolderScanner(state)
    state.folder_scanner_task = asyncio.create_task(_scanner.run(), name="folder-scanner")
    state.folder_scanner = _scanner

    logger.info("OpcUA_2_InfluxDB started. Config dir: %s", config_dir)
    yield

    # Shutdown
    if state.watchdog_task:
        state.watchdog_task.cancel()
    if state.folder_scanner_task:
        state.folder_scanner_task.cancel()
    await state.polling_engine.stop_all()
    await state.opcua_manager.stop_all()
    await state.influx_pool.stop()
    if state.sqlite_writer_pool:
        await state.sqlite_writer_pool.stop()
    logger.info("OpcUA_2_InfluxDB shut down cleanly.")


class CachedStaticFiles(StaticFiles):
    """Static files with long-lived caching — safe because all references
    carry a ?v={app_version} cache-busting query parameter."""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp


app = FastAPI(title="OpcUA_2_InfluxDB", lifespan=lifespan)

app.add_middleware(GZipMiddleware, minimum_size=500)
app.mount("/static", CachedStaticFiles(directory="static"), name="static")

# Register routers
app.include_router(dashboard.router)
app.include_router(opcua_browser.router)
app.include_router(subscriptions.router)
app.include_router(server_config.router)
app.include_router(influxdb_config.router)
app.include_router(alerts_config.router)
app.include_router(sqlite_config.router)
app.include_router(config_backup.router)
app.include_router(tags_config.router)
app.include_router(alarm_rules.router)
app.include_router(stale_watchdog.router)
app.include_router(data_viewer.router)
app.include_router(chart.router)
app.include_router(help_page.router)
app.include_router(dynamic_folders.router)
app.include_router(app_settings.router)

# Apply Jinja2 custom filters to all template engines
_templates = Jinja2Templates(directory="templates")
_make_jinja_env(_templates)
# Also patch each router's template engine
for router_mod in [dashboard, opcua_browser, subscriptions, chart, help_page,
                   server_config, influxdb_config, alerts_config,
                   sqlite_config, config_backup, tags_config, alarm_rules, data_viewer,
                   app_settings, stale_watchdog]:
    if hasattr(router_mod, "templates"):
        _make_jinja_env(router_mod.templates)
