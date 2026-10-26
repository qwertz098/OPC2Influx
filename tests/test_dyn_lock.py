"""Unit tests for the dynamic-folder lock check used to disable manual
(un)subscribe checkboxes under a watcher — prevents interference between
manual and watcher-managed subscriptions."""
from __future__ import annotations

from app.config import AppConfig, ServersConfig, SubscriptionsConfig, InfluxDBConfig, AlertsConfig
from app.config import DynamicFoldersConfig, DynamicFolderConfig
from app.state import RuntimeState
from app.routers.opcua_browser import _is_dyn_locked


def _state(tmp_path, watched_path: str) -> RuntimeState:
    watcher = DynamicFolderConfig(
        server_id="srv1", folder_node_id="ns=2;s=x",
        folder_browse_path=watched_path, display_name="w",
    )
    config = AppConfig(
        servers=ServersConfig(), subscriptions=SubscriptionsConfig(),
        influxdb=InfluxDBConfig(), alerts=AlertsConfig(),
        dynamic_folders=DynamicFoldersConfig(folders=[watcher]),
    )
    return RuntimeState(config_dir=tmp_path, config=config)


def test_watched_folder_itself_is_locked(tmp_path):
    state = _state(tmp_path, "/GlobalVars/Globals")
    assert _is_dyn_locked(state, "srv1", "/GlobalVars/Globals") is True


def test_descendant_of_watched_folder_is_locked(tmp_path):
    state = _state(tmp_path, "/GlobalVars/Globals")
    assert _is_dyn_locked(state, "srv1", "/GlobalVars/Globals/AlarmGlobals/Counter") is True


def test_sibling_folder_is_not_locked(tmp_path):
    state = _state(tmp_path, "/GlobalVars/Globals")
    assert _is_dyn_locked(state, "srv1", "/GlobalVars/Modbus") is False


def test_prefix_collision_without_separator_is_not_locked(tmp_path):
    """'/GlobalVars/Globals2' must NOT be treated as under '/GlobalVars/Globals'."""
    state = _state(tmp_path, "/GlobalVars/Globals")
    assert _is_dyn_locked(state, "srv1", "/GlobalVars/Globals2") is False


def test_different_server_is_not_locked(tmp_path):
    state = _state(tmp_path, "/GlobalVars/Globals")
    assert _is_dyn_locked(state, "srv2", "/GlobalVars/Globals/x") is False
