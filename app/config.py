"""
Configuration models and persistence for OpcUA_2_InfluxDB.

All config is stored in YAML files inside CONFIG_DIR (default: /config).
Writes are atomic: temp file + os.replace().
${ENV_VAR} references in string values are resolved from os.environ at load time.
"""
from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ENV_RE = re.compile(r"\$\{([^}]+)\}")


def resolve_env(value: Any) -> Any:
    """Recursively replace ${VAR} placeholders with environment values.

    Unresolved placeholders (VAR not set) are left as literal "${VAR}" text —
    use resolve_env_strict() where a missing env var must be surfaced as an error
    (e.g. resolving credentials right before an outgoing connection).
    """
    if isinstance(value, str):
        def _sub(m: re.Match) -> str:
            return os.environ.get(m.group(1), m.group(0))
        return _ENV_RE.sub(_sub, value)
    if isinstance(value, dict):
        return {k: resolve_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_env(v) for v in value]
    return value


class UnresolvedEnvVarError(ValueError):
    """Raised when a ${VAR} placeholder could not be resolved from the environment."""


def resolve_env_strict(value: str) -> str:
    """Like resolve_env(), but raises UnresolvedEnvVarError if any ${VAR} placeholder
    in `value` names an environment variable that isn't actually set.

    Use this immediately before using a secret (e.g. an API token) to connect to an
    external service, so a missing env var fails loudly instead of silently sending
    the literal "${VAR}" text as the credential.
    """
    missing = sorted({m.group(1) for m in _ENV_RE.finditer(value) if m.group(1) not in os.environ})
    if missing:
        names = ", ".join(missing)
        raise UnresolvedEnvVarError(
            f"Environment variable(s) not set: {names}. Set them in the process "
            f"environment (Docker -e / .env, or export before starting the app), "
            f"or switch this field back to a direct value."
        )
    return resolve_env(value)


def apply_name_strips(name: str, strips: list[str]) -> str:
    """Remove each strip string from the start of name if it matches, then clean up separators."""
    for s in strips:
        if s and name.startswith(s):
            name = name[len(s):].lstrip("./")
    return name


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return resolve_env(data)


def _save_yaml(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(yaml.dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# OPC UA Server config
# ---------------------------------------------------------------------------

class ServerAuth(BaseModel):
    type: Literal["anonymous", "username", "certificate"] = "anonymous"
    username: str = ""
    password: str = ""
    certificate_path: str = ""
    private_key_path: str = ""


class ServerPollingDefaults(BaseModel):
    interval_ms: int = 1000
    write_mode: Literal["always", "on_change", "on_change_absolute", "on_change_relative"] = "on_change"
    threshold: float = 0.0
    heartbeat_interval_seconds: int = 60   # 0 = disabled


class ServerStaticTag(BaseModel):
    key: str
    value: str


class ServerConfig(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    display_name: str
    endpoint_url: str
    security_mode: Literal["None", "Sign", "SignAndEncrypt"] = "None"
    security_policy: str = "Basic256Sha256"
    auth: ServerAuth = Field(default_factory=ServerAuth)
    reconnect_interval_seconds: int = 10
    session_timeout_ms: int = 30000
    enabled: bool = True
    polling_defaults: ServerPollingDefaults = Field(default_factory=ServerPollingDefaults)
    static_tags: list[ServerStaticTag] = []
    bool_as_int: bool = False
    name_strip_prefixes: list[str] = []


class ServersConfig(BaseModel):
    servers: list[ServerConfig] = []


# ---------------------------------------------------------------------------
# Subscription config
# ---------------------------------------------------------------------------

class InfluxTagConfig(BaseModel):
    key: str
    value_type: Literal["static", "node_attr"] = "static"
    value: str = ""          # used when value_type == "static"
    attr: str = ""           # used when value_type == "node_attr": NodeId|BrowseName|DisplayName|Description


class SubscriptionInfluxConfig(BaseModel):
    measurement: str = "opcua_value"
    field_name: str = "value"
    tags: list[InfluxTagConfig] = []


class PollingConfig(BaseModel):
    interval_ms: int = 1000
    write_mode: Literal["always", "on_change", "on_change_absolute", "on_change_relative"] = "on_change"
    threshold: float = 0.0
    heartbeat_interval_seconds: int = 60   # 0 = disabled; force write even if no change


class SubTagActivation(BaseModel):
    tag_id: str
    active: bool = True


class SubscriptionConfig(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    server_id: str
    node_id: str
    browse_path: str = ""
    display_name: str = ""
    influxdb: SubscriptionInfluxConfig = Field(default_factory=SubscriptionInfluxConfig)
    polling: PollingConfig = Field(default_factory=PollingConfig)
    enabled: bool = True
    active_tags: list[SubTagActivation] = []
    source_folder_id: str = ""   # non-empty = managed by a DynamicFolderConfig


class SubscriptionsConfig(BaseModel):
    subscriptions: list[SubscriptionConfig] = []


# ---------------------------------------------------------------------------
# InfluxDB targets config
# ---------------------------------------------------------------------------

class BatchConfig(BaseModel):
    size: int = 500
    flush_interval_ms: int = 1000
    max_retries: int = 3


class InfluxTargetConfig(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    display_name: str = "InfluxDB"
    url: str = "http://localhost:8086"
    token: str = ""
    org: str = "myorg"
    bucket: str = "opcua"
    mode: Literal["always", "failover"] = "always"
    priority: int = 1
    enabled: bool = True
    type_suffix: bool = False   # Append _bool/_float/_integer/_string to field name to avoid schema conflicts
    batch: BatchConfig = Field(default_factory=BatchConfig)


class InfluxDBConfig(BaseModel):
    targets: list[InfluxTargetConfig] = []


# ---------------------------------------------------------------------------
# Alerts / watchdog config
# ---------------------------------------------------------------------------

class WatchdogConfig(BaseModel):
    check_interval_seconds: int = 30
    connection_loss_alert_delay_seconds: int = 60
    recovery_alert: bool = True


class SmtpConfig(BaseModel):
    enabled: bool = True   # master switch — False suppresses all outgoing email
    host: str = ""
    port: int = 587
    tls: bool = True
    username: str = ""
    password: str = ""
    from_address: str = ""
    to_addresses: list[str] = []


class AlertsConfig(BaseModel):
    watchdog: WatchdogConfig = Field(default_factory=WatchdogConfig)
    smtp: SmtpConfig = Field(default_factory=SmtpConfig)


# ---------------------------------------------------------------------------
# Central tags config
# ---------------------------------------------------------------------------

class CentralTagConfig(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    name: str
    short_name: str
    key: str                        # InfluxDB / SQL tag key
    is_dynamic: bool = False
    static_value: str = ""          # used when not dynamic
    relative_path: str = ""         # used when dynamic, e.g. "./tagName" or "../../Sub/tag"
    default_active: bool = False


class TagsConfig(BaseModel):
    tags: list[CentralTagConfig] = []


# ---------------------------------------------------------------------------
# SQLite targets config
# ---------------------------------------------------------------------------

class SQLiteTargetConfig(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    display_name: str = "SQLite Log"
    file_path: str = "data/opcua_log.db"
    write_interval_seconds: int = 60
    enabled: bool = True


class SQLiteConfig(BaseModel):
    targets: list[SQLiteTargetConfig] = []


# ---------------------------------------------------------------------------
# Alarm rules config
# ---------------------------------------------------------------------------

class AlarmRuleConfig(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    name: str = ""
    subscription_id: str = ""
    condition: Literal["<", "=", ">", "<>"] = ">"
    threshold: str = "0"        # "1.5" or "true"/"false" for booleans
    enabled: bool = True
    cooldown_seconds: int = 300
    alert_delay_seconds: int = 0  # condition must hold for this many seconds before firing
    latch: bool = False           # True = fire once on rising edge, re-arm only after condition clears
    send_email: bool = True       # False = log alarm only, skip email


class AlarmRulesConfig(BaseModel):
    rules: list[AlarmRuleConfig] = []


# ---------------------------------------------------------------------------
# Stale-value watchdog rules — alert when a subscribed signal's value does
# not change within a configurable time window. Rule-per-signal, same shape
# as alarm rules, rather than one global on/off switch.
# ---------------------------------------------------------------------------

class StaleWatchdogRuleConfig(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    name: str = ""
    subscription_id: str = ""
    window_ms: int = 1000   # same choices as PollingConfig.interval_ms
    enabled: bool = True
    suppress_on_disconnect: bool = True  # skip while the server is down — the
                                          # connectivity Watchdog already alerts for that
    send_email: bool = True


class StaleWatchdogConfig(BaseModel):
    rules: list[StaleWatchdogRuleConfig] = []


# ---------------------------------------------------------------------------
# Dynamic folder subscriptions
# ---------------------------------------------------------------------------

class DynamicFolderConfig(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    server_id: str
    folder_node_id: str
    folder_browse_path: str
    display_name: str = ""
    rescan_interval_seconds: int = 300
    enabled: bool = True


class DynamicFoldersConfig(BaseModel):
    folders: list[DynamicFolderConfig] = []


# ---------------------------------------------------------------------------
# Saved chart views
# ---------------------------------------------------------------------------

class SavedChartConfig(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    name: str
    signals: list[dict] = []        # [{server_id, node_id, path, name, color, label}]
    interval_ms: int = 1000
    max_time_seconds: int = 3600
    log_time_mode: bool = False
    auto_scale_mode: bool = False
    multipliers: dict[str, float] = {}
    created_at: str = ""


class SavedChartsConfig(BaseModel):
    configs: list[SavedChartConfig] = []


# Scalar OPC UA data types the app can actually write to InfluxDB/SQLite
# (matches app/opcua/browser.py's _DATATYPE_NAMES). Structs, arrays, and other
# non-scalar types are never subscribable via dynamic folders regardless of
# this setting, since their type name won't appear in this list.
DYNAMIC_FOLDER_TYPE_CHOICES = [
    "Boolean", "SByte", "Byte", "Int16", "UInt16", "Int32", "UInt32",
    "Int64", "UInt64", "Float", "Double", "String", "DateTime",
]


class AppSettingsConfig(BaseModel):
    site_name: str = ""
    # Which scalar data types dynamic folder watchers auto-subscribe to.
    # Default excludes DateTime — the poller can't write datetime values today,
    # so subscribing to them would just create permanently-silent subscriptions.
    # Default also excludes String — numeric/boolean I/O is the common case for
    # auto-discovery; string signals are usually intentional and best added explicitly.
    dynamic_folder_allowed_types: list[str] = Field(default_factory=lambda: [
        "Boolean", "SByte", "Byte", "Int16", "UInt16", "Int32", "UInt32",
        "Int64", "UInt64", "Float", "Double",
    ])


# ---------------------------------------------------------------------------
# Aggregate app config
# ---------------------------------------------------------------------------

class AppConfig(BaseModel):
    servers: ServersConfig = Field(default_factory=ServersConfig)
    subscriptions: SubscriptionsConfig = Field(default_factory=SubscriptionsConfig)
    influxdb: InfluxDBConfig = Field(default_factory=InfluxDBConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    tags: TagsConfig = Field(default_factory=TagsConfig)
    sqlite: SQLiteConfig = Field(default_factory=SQLiteConfig)
    alarm_rules: AlarmRulesConfig = Field(default_factory=AlarmRulesConfig)
    stale_watchdog: StaleWatchdogConfig = Field(default_factory=StaleWatchdogConfig)
    saved_charts: SavedChartsConfig = Field(default_factory=SavedChartsConfig)
    dynamic_folders: DynamicFoldersConfig = Field(default_factory=DynamicFoldersConfig)
    app_settings: AppSettingsConfig = Field(default_factory=AppSettingsConfig)


# ---------------------------------------------------------------------------
# Load / save functions
# ---------------------------------------------------------------------------

def _load_model(cls, path: Path):
    return cls(**_load_yaml(path)) if path.exists() else cls()


def load_config(config_dir: Path) -> AppConfig:
    config_dir.mkdir(parents=True, exist_ok=True)
    d = config_dir
    return AppConfig(
        servers=_load_model(ServersConfig, d / "servers.yaml"),
        subscriptions=_load_model(SubscriptionsConfig, d / "subscriptions.yaml"),
        influxdb=_load_model(InfluxDBConfig, d / "influxdb.yaml"),
        alerts=_load_model(AlertsConfig, d / "alerts.yaml"),
        tags=_load_model(TagsConfig, d / "tags.yaml"),
        sqlite=_load_model(SQLiteConfig, d / "sqlite_targets.yaml"),
        alarm_rules=_load_model(AlarmRulesConfig, d / "alarm_rules.yaml"),
        stale_watchdog=_load_model(StaleWatchdogConfig, d / "stale_watchdog.yaml"),
        saved_charts=_load_model(SavedChartsConfig, d / "saved_charts.yaml"),
        dynamic_folders=_load_model(DynamicFoldersConfig, d / "dynamic_folders.yaml"),
        app_settings=_load_model(AppSettingsConfig, d / "app_settings.yaml"),
    )


def save_app_settings(config: AppSettingsConfig, config_dir: Path) -> None:
    _save_yaml(config_dir / "app_settings.yaml", config.model_dump())


def save_servers(config: ServersConfig, config_dir: Path) -> None:
    _save_yaml(config_dir / "servers.yaml", config.model_dump())


def save_subscriptions(config: SubscriptionsConfig, config_dir: Path) -> None:
    _save_yaml(config_dir / "subscriptions.yaml", config.model_dump())




def save_influxdb(config: InfluxDBConfig, config_dir: Path) -> None:
    _save_yaml(config_dir / "influxdb.yaml", config.model_dump())


def save_alerts(config: AlertsConfig, config_dir: Path) -> None:
    _save_yaml(config_dir / "alerts.yaml", config.model_dump())


def save_tags(config: TagsConfig, config_dir: Path) -> None:
    _save_yaml(config_dir / "tags.yaml", config.model_dump())


def save_sqlite(config: SQLiteConfig, config_dir: Path) -> None:
    _save_yaml(config_dir / "sqlite_targets.yaml", config.model_dump())


def save_alarm_rules(config: AlarmRulesConfig, config_dir: Path) -> None:
    _save_yaml(config_dir / "alarm_rules.yaml", config.model_dump())


def save_stale_watchdog(config: StaleWatchdogConfig, config_dir: Path) -> None:
    _save_yaml(config_dir / "stale_watchdog.yaml", config.model_dump())


def save_saved_charts(config: SavedChartsConfig, config_dir: Path) -> None:
    _save_yaml(config_dir / "saved_charts.yaml", config.model_dump())


def save_dynamic_folders(config: DynamicFoldersConfig, config_dir: Path) -> None:
    _save_yaml(config_dir / "dynamic_folders.yaml", config.model_dump())
