"""Tests for FolderScanner's hang-protection: per-watcher scan lock and timeout.

These guard against a real incident: a stuck OPC UA Browse/Read (e.g. from
exhausted continuation points on the PLC) used to hang scan_now() forever,
and every repeated "Scan now" click piled another zombie task onto the same
session with no way to recover short of restarting the app.
"""
from __future__ import annotations

import asyncio

import pytest

from app.config import AppConfig, ServersConfig, SubscriptionsConfig, InfluxDBConfig, AlertsConfig
from app.config import DynamicFoldersConfig, DynamicFolderConfig
from app.state import RuntimeState
from app.opcua import folder_scanner as fs_module
from app.opcua.folder_scanner import FolderScanner

pytestmark = pytest.mark.asyncio


def _make_state(tmp_path, watcher: DynamicFolderConfig) -> RuntimeState:
    config = AppConfig(
        servers=ServersConfig(),
        subscriptions=SubscriptionsConfig(),
        influxdb=InfluxDBConfig(),
        alerts=AlertsConfig(),
        dynamic_folders=DynamicFoldersConfig(folders=[watcher]),
    )
    state = RuntimeState(config_dir=tmp_path, config=config)
    state.opcua_manager = object()  # unused — _get_all_variables is monkeypatched
    return state


def _watcher(**kwargs) -> DynamicFolderConfig:
    return DynamicFolderConfig(
        server_id="srv1",
        folder_node_id="ns=2;s=folder",
        folder_browse_path="/Folder",
        display_name="Folder",
        **kwargs,
    )


async def test_scan_now_times_out_instead_of_hanging_forever(tmp_path, monkeypatch):
    watcher = _watcher()
    state = _make_state(tmp_path, watcher)
    scanner = FolderScanner(state)
    monkeypatch.setattr(fs_module, "_SCAN_TIMEOUT_SECONDS", 0.05)

    async def _hangs_forever(*args, **kwargs):
        await asyncio.sleep(10)
        return [], False

    monkeypatch.setattr(fs_module, "_get_all_variables", _hangs_forever)

    await asyncio.wait_for(scanner.scan_now(watcher.id), timeout=2.0)
    assert watcher.id not in scanner._scanning
    assert watcher.id not in scanner._last_scan  # a timed-out scan must not count as completed


async def test_concurrent_scan_of_same_watcher_is_skipped(tmp_path, monkeypatch):
    watcher = _watcher()
    state = _make_state(tmp_path, watcher)
    scanner = FolderScanner(state)

    calls = 0
    release = asyncio.Event()

    async def _slow(*args, **kwargs):
        nonlocal calls
        calls += 1
        await release.wait()
        return [], False

    monkeypatch.setattr(fs_module, "_get_all_variables", _slow)

    first = asyncio.create_task(scanner.scan_now(watcher.id))
    await asyncio.sleep(0)  # let the first scan register itself as in-flight
    await scanner.scan_now(watcher.id)  # second call must return immediately, skipped
    assert calls == 1

    release.set()
    await asyncio.wait_for(first, timeout=2.0)
    assert watcher.id not in scanner._scanning


async def test_truncated_scan_does_not_disable_missing_subscriptions(tmp_path, monkeypatch):
    """The core safety fix: if the browse hit the page cap, nodes that
    weren't in the (incomplete) result must NOT be treated as vanished and
    disabled — they may simply not have been fetched yet."""
    from app.config import SubscriptionConfig, SubscriptionsConfig

    watcher = _watcher()
    state = _make_state(tmp_path, watcher)
    existing_sub = SubscriptionConfig(
        id="existing1", server_id="srv1", node_id="ns=2;s=already-managed",
        browse_path="/Folder/already-managed", source_folder_id=watcher.id, enabled=True,
    )
    state.config.subscriptions = SubscriptionsConfig(subscriptions=[existing_sub])
    scanner = FolderScanner(state)

    async def _truncated_result(*args, **kwargs):
        # Browse only found ONE (different) node this cycle and hit the page cap —
        # existing_sub's node simply wasn't reached, not actually gone.
        return [{
            "node_id": "ns=2;s=other-node",
            "browse_path": "/Folder/other-node",
            "display_name": "other-node",
        }], True

    monkeypatch.setattr(fs_module, "_get_all_variables", _truncated_result)

    await scanner.scan_now(watcher.id)

    assert existing_sub.enabled is True, "truncated scan must never disable subs it didn't see"


async def test_complete_scan_still_disables_vanished_subscriptions(tmp_path, monkeypatch):
    """Sanity check: the safety change above must not break normal (complete,
    non-truncated) vanished-node detection."""
    from app.config import SubscriptionConfig, SubscriptionsConfig

    watcher = _watcher()
    state = _make_state(tmp_path, watcher)
    vanished_sub = SubscriptionConfig(
        id="gone1", server_id="srv1", node_id="ns=2;s=vanished",
        browse_path="/Folder/vanished", source_folder_id=watcher.id, enabled=True,
    )
    state.config.subscriptions = SubscriptionsConfig(subscriptions=[vanished_sub])
    scanner = FolderScanner(state)

    async def _complete_empty_result(*args, **kwargs):
        return [], False  # complete scan, node genuinely gone

    monkeypatch.setattr(fs_module, "_get_all_variables", _complete_empty_result)

    await scanner.scan_now(watcher.id)

    assert vanished_sub.enabled is False
