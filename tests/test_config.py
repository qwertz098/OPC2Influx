"""Unit tests for config load/save, env var substitution, atomic write."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.config import (
    AppConfig, ServersConfig, ServerConfig, SubscriptionsConfig,
    InfluxDBConfig, InfluxTargetConfig, AlertsConfig,
    load_config, save_servers, save_subscriptions, save_influxdb,
    resolve_env,
)


def test_load_empty_config(tmp_path):
    """Loading a non-existent config dir returns default empty config."""
    config = load_config(tmp_path / "config")
    assert isinstance(config, AppConfig)
    assert config.servers.servers == []
    assert config.subscriptions.subscriptions == []
    assert config.influxdb.targets == []


def test_save_and_reload_servers(tmp_path):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()

    servers = ServersConfig(servers=[
        ServerConfig(id="srv1", display_name="PLC 1", endpoint_url="opc.tcp://1.2.3.4:4840")
    ])
    save_servers(servers, cfg_dir)

    reloaded = load_config(cfg_dir)
    assert len(reloaded.servers.servers) == 1
    assert reloaded.servers.servers[0].display_name == "PLC 1"
    assert reloaded.servers.servers[0].endpoint_url == "opc.tcp://1.2.3.4:4840"


def test_atomic_write_creates_no_tmp_file(tmp_path):
    """After saving, no .tmp file should remain."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    servers = ServersConfig(servers=[
        ServerConfig(id="x", display_name="X", endpoint_url="opc.tcp://localhost:4840")
    ])
    save_servers(servers, cfg_dir)
    assert not (cfg_dir / "servers.tmp").exists()
    assert (cfg_dir / "servers.yaml").exists()


def test_env_var_substitution(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_INFLUX_TOKEN", "secret-token-abc")
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()

    # Write a YAML file manually with ${} reference
    (cfg_dir / "influxdb.yaml").write_text(
        "targets:\n"
        "  - id: t1\n"
        "    display_name: Primary\n"
        "    url: http://localhost:8086\n"
        "    token: '${TEST_INFLUX_TOKEN}'\n"
        "    org: myorg\n"
        "    bucket: opcua\n"
        "    mode: always\n"
        "    priority: 1\n"
        "    enabled: true\n"
        "    batch:\n"
        "      size: 500\n"
        "      flush_interval_ms: 1000\n"
        "      max_retries: 3\n"
    )
    config = load_config(cfg_dir)
    assert config.influxdb.targets[0].token == "secret-token-abc"


def test_env_var_missing_kept_as_placeholder(tmp_path):
    """Undefined env vars stay as ${VAR} (no KeyError)."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "influxdb.yaml").write_text(
        "targets:\n"
        "  - id: t1\n"
        "    display_name: X\n"
        "    url: http://localhost:8086\n"
        "    token: '${UNDEFINED_VAR_XYZ}'\n"
        "    org: myorg\n"
        "    bucket: opcua\n"
        "    mode: always\n"
        "    priority: 1\n"
        "    enabled: true\n"
        "    batch: {size: 500, flush_interval_ms: 1000, max_retries: 3}\n"
    )
    config = load_config(cfg_dir)
    # Should keep the placeholder, not raise
    assert "${UNDEFINED_VAR_XYZ}" in config.influxdb.targets[0].token


def test_save_and_reload_influxdb_targets(tmp_path):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    cfg = InfluxDBConfig(targets=[
        InfluxTargetConfig(
            id="p1", display_name="Primary", url="http://influx:8086",
            token="tok", org="myorg", bucket="opcua", mode="always", priority=1
        ),
        InfluxTargetConfig(
            id="s1", display_name="Secondary", url="http://influx2:8086",
            token="tok2", org="myorg", bucket="opcua", mode="failover", priority=2
        ),
    ])
    save_influxdb(cfg, cfg_dir)
    reloaded = load_config(cfg_dir)
    assert len(reloaded.influxdb.targets) == 2
    assert reloaded.influxdb.targets[1].mode == "failover"


def test_resolve_env_resolves_at_runtime(monkeypatch):
    """resolve_env can be called at any time, not just at config load."""
    monkeypatch.setenv("MY_RT_TOKEN", "runtime-value")
    assert resolve_env("${MY_RT_TOKEN}") == "runtime-value"
    assert resolve_env("plain-token") == "plain-token"
    assert resolve_env("${MISSING_VAR_XYZ}") == "${MISSING_VAR_XYZ}"
