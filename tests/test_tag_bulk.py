"""Tags for many subscriptions at once: helper, routes, dynamic folder tags."""
import types

import pytest
import yaml

from app.config import (
    CentralTagConfig, DynamicFolderConfig, SubTagActivation, SubscriptionConfig, load_config,
)
from app.opcua.folder_scanner import _default_active_tags
from app.tag_bulk import apply_tag, tag_usage


def _sub(i, folder="", tags=()):
    return SubscriptionConfig(id=f"s{i}", server_id="srv", node_id=f"ns=2;i={i}",
                              source_folder_id=folder, active_tags=list(tags))


@pytest.fixture
def state(minimal_state):
    minimal_state.config.tags.tags = [
        CentralTagConfig(id="t1", name="Machine", short_name="mach", key="machine", static_value="TG1"),
        CentralTagConfig(id="t2", name="Line", short_name="line", key="line", static_value="L1"),
    ]
    minimal_state.config.subscriptions.subscriptions = [
        _sub(1), _sub(2, tags=[SubTagActivation(tag_id="t1", active=False)]),
        _sub(3, tags=[SubTagActivation(tag_id="t1", active=True)]), _sub(4),
    ]
    engine = types.SimpleNamespace(_tag_cache={"s1": {"t1": "x"}, "s4": {"t1": "y"}})
    minimal_state.polling_engine = engine
    return minimal_state


def _tags(state, sid):
    sub = next(s for s in state.config.subscriptions.subscriptions if s.id == sid)
    return {a.tag_id: a.active for a in sub.active_tags}


def test_switch_on_adds_or_activates(state):
    assert apply_tag(state, ["s1", "s2", "s3"], "t1", "on") == 2      # s3 was on already
    assert [_tags(state, s) for s in ("s1", "s2", "s3", "s4")] == [{"t1": True}] * 3 + [{}]
    assert "s1" not in state.polling_engine._tag_cache                  # cache cleared
    assert "s4" in state.polling_engine._tag_cache                      # untouched


def test_switch_off_keeps_assignment(state):
    assert apply_tag(state, ["s1", "s3"], "t1", "off") == 2
    assert _tags(state, "s1") == {"t1": False} and _tags(state, "s3") == {"t1": False}


def test_remove_deletes_assignment(state):
    assert apply_tag(state, ["s1", "s2", "s3"], "t1", "remove") == 2    # s1 had none
    assert all(_tags(state, s) == {} for s in ("s1", "s2", "s3"))


def test_saved_once_to_disk(state):
    apply_tag(state, ["s1", "s4"], "t2", "on")
    saved = yaml.safe_load((state.config_dir / "subscriptions.yaml").read_text())
    by_id = {s["id"]: s["active_tags"] for s in saved["subscriptions"]}
    assert by_id["s1"] == [{"tag_id": "t2", "active": True}]


def test_invalid_input(state):
    with pytest.raises(ValueError):
        apply_tag(state, ["s1"], "nope", "on")
    with pytest.raises(ValueError):
        apply_tag(state, ["s1"], "t1", "toggle")


def test_usage_counts_active_only(state):
    assert tag_usage(state) == {"t1": 1, "t2": 0}


def test_new_folder_subscription_gets_folder_and_default_tags(state):
    state.config.tags.tags[1].default_active = True
    folder = DynamicFolderConfig(id="f1", server_id="srv", folder_node_id="ns=2;i=1",
                                 folder_browse_path="/X", tag_ids=["t1"])
    assert {a.tag_id for a in _default_active_tags(state, folder)} == {"t1", "t2"}
    assert {a.tag_id for a in _default_active_tags(state)} == {"t2"}


# ─── Routes ──────────────────────────────────────────────────────────────────
@pytest.fixture
def config_with_tags(tmp_config_dir):
    (tmp_config_dir / "tags.yaml").write_text(yaml.safe_dump({"tags": [
        {"id": "t1", "name": "Machine", "short_name": "mach", "key": "machine", "static_value": "TG1"}]}))
    subs = [_sub(1, folder="f1").model_dump(), _sub(2, folder="f1").model_dump(), _sub(3).model_dump()]
    (tmp_config_dir / "subscriptions.yaml").write_text(yaml.safe_dump({"subscriptions": subs}))
    (tmp_config_dir / "dynamic_folders.yaml").write_text(yaml.safe_dump({"folders": [
        {"id": "f1", "server_id": "srv", "folder_node_id": "ns=2;i=1", "folder_browse_path": "/X",
         "enabled": False}]}))
    return tmp_config_dir


@pytest.mark.asyncio
async def test_bulk_route(config_with_tags, async_client):
    resp = await async_client.post("/subscriptions/bulk-tag",
                                   json={"tag_id": "t1", "action": "on", "sub_ids": ["s1", "s3"]})
    assert resp.status_code == 200 and resp.json()["changed"] == 2
    bad = await async_client.post("/subscriptions/bulk-tag", json={"tag_id": "t1", "action": "x", "sub_ids": []})
    assert bad.status_code == 400
    page = await async_client.get("/subscriptions")
    assert page.status_code == 200 and 'id="fText"' in page.text and "data-tags=" in page.text
    cfg = load_config(config_with_tags)
    assert {s.id: [(a.tag_id, a.active) for a in s.active_tags] for s in cfg.subscriptions.subscriptions} == \
        {"s1": [("t1", True)], "s2": [], "s3": [("t1", True)]}


@pytest.mark.asyncio
async def test_apply_all_and_folder_tag_routes(config_with_tags, async_client):
    await async_client.post("/config/tags/t1/apply-all", data={"action": "off"})
    cfg = load_config(config_with_tags)
    assert all(s.active_tags == [SubTagActivation(tag_id="t1", active=False)] for s in cfg.subscriptions.subscriptions)
    page = await async_client.get("/config/tags")
    assert "0 / 3" in page.text

    await async_client.post("/config/dynamic-folders/f1/toggle-tag/t1")      # on for the folder
    cfg = load_config(config_with_tags)
    assert cfg.dynamic_folders.folders[0].tag_ids == ["t1"]
    assert {s.id: s.active_tags[0].active for s in cfg.subscriptions.subscriptions} == \
        {"s1": True, "s2": True, "s3": False}                                 # s3 is not in the folder

    await async_client.post("/config/dynamic-folders/f1/toggle-tag/t1")      # off again
    cfg = load_config(config_with_tags)
    assert cfg.dynamic_folders.folders[0].tag_ids == []
    assert not any(a.active for s in cfg.subscriptions.subscriptions for a in s.active_tags)

    await async_client.post("/config/tags/t1/apply-all", data={"action": "remove"})
    cfg = load_config(config_with_tags)
    assert all(s.active_tags == [] for s in cfg.subscriptions.subscriptions)
