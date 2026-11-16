"""Reference tables: store, capture, CSV export and routes."""
import asyncio
import types
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
import yaml

from app.config import ReferenceSignalConfig, ReferenceTableConfig, SubscriptionConfig
from app.references import store
from app.references.capture import capture
from app.references.export import columns, to_csv
from app.references.store import RefValue
from app.state import ServerStatus


@pytest.fixture(autouse=True)
def ref_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "db_path", lambda: tmp_path / "references.db")
    return tmp_path / "references.db"


def _vals(p=2.5, t=None, q="ok"):
    return [RefValue("s1", "Pressure", "bar", p, q), RefValue("s2", "Temp", "°C", t, "missing" if t is None else "ok")]


# ─── Store ───────────────────────────────────────────────────────────────────
def test_add_list_comment_delete():
    a = store.add_entry("T", _vals(1.0), "KK", "first", ts=datetime(2026, 11, 1, 10, tzinfo=timezone.utc))
    b = store.add_entry("T", _vals(2.0, 21.5), "KK", "", ts=datetime(2026, 11, 2, 10, tzinfo=timezone.utc))
    store.add_entry("other", _vals(), "", "")
    entries = store.list_entries("T")
    assert [e.id for e in entries] == [b, a]                     # newest first
    assert entries[0].value_of("s2").value == 21.5 and entries[1].quality == "missing"
    assert store.count_entries() == {"T": 2, "other": 1}
    assert store.update_comment(a, "  changed  ") and store.list_entries("T")[1].comment == "changed"
    assert store.delete_entry(b) and not store.delete_entry(b)
    assert store.delete_table("T") == 1 and store.list_entries("T") == []


# ─── Capture ─────────────────────────────────────────────────────────────────
def _state(minimal_state, live, connected=True, read=None):
    minimal_state.config.subscriptions.subscriptions = [
        SubscriptionConfig(id="s1", server_id="srv", node_id="n1", display_name="Pressure"),
        SubscriptionConfig(id="s2", server_id="srv", node_id="n2", display_name="Temp"),
        SubscriptionConfig(id="s3", server_id="srv", node_id="n3", display_name="Valve"),
    ]
    minimal_state.server_status["srv"] = ServerStatus(connected=connected)
    minimal_state.polling_engine = types.SimpleNamespace(live_values=lambda: dict(live))

    async def read_node_values(server_id, node_ids):
        return [read.get(n) for n in node_ids]
    minimal_state.opcua_manager = types.SimpleNamespace(read_node_values=read_node_values) if read is not None else None
    return ReferenceTableConfig(id="T", name="T", signals=[
        ReferenceSignalConfig(subscription_id="s1", unit="bar"), ReferenceSignalConfig(subscription_id="s2"),
        ReferenceSignalConfig(subscription_id="s3"), ReferenceSignalConfig(subscription_id="gone")])


def test_capture_values_and_quality(minimal_state):
    table = _state(minimal_state, {"s1": 2.5, "s2": True}, read={"n3": 7.0})
    vals = asyncio.run(capture(minimal_state, table))
    got = {v.subscription_id: (v.label, v.unit, v.value, v.quality) for v in vals}
    assert got["s1"] == ("Pressure", "bar", 2.5, "ok")
    assert got["s2"] == ("Temp", "", 1, "ok")                    # bool → 1
    assert got["s3"] == ("Valve", "", 7.0, "ok")                 # not polled yet: read directly
    assert got["gone"][3] == "missing"


def test_capture_server_offline(minimal_state):
    table = _state(minimal_state, {"s1": 2.5}, connected=False)
    got = {v.subscription_id: v.quality for v in asyncio.run(capture(minimal_state, table))}
    assert got == {"s1": "offline", "s2": "missing", "s3": "missing", "gone": "missing"}


# ─── CSV ─────────────────────────────────────────────────────────────────────
def test_csv_standard_and_excel():
    table = ReferenceTableConfig(id="T", name="T", signals=[ReferenceSignalConfig(subscription_id="s1", unit="bar")])
    store.add_entry("T", _vals(2.5, 21.5), "KK", "after service", ts=datetime(2026, 11, 15, 21, 47, 53, tzinfo=timezone.utc))
    store.add_entry("T", [RefValue("s1", "Pressure", "bar", 2.75, "offline")], "", "",
                    ts=datetime(2026, 11, 16, 8, 0, tzinfo=timezone.utc))
    entries = store.list_entries("T")
    # table signal first, then the removed signal that older rows still have
    assert [c[0] for c in columns(table, entries)] == ["s1", "s2"]
    std = to_csv(table, entries).splitlines()
    assert std[0] == "Time (UTC),Time (UTC),Name,Comment,Pressure [bar],Temp [°C],Quality"
    assert std[1] == "2026-11-15 21:47:53,2026-11-15 21:47:53,KK,after service,2.5,21.5,"   # oldest first
    assert std[2].endswith(",2.75,,Pressure: offline")
    xl = to_csv(table, entries, excel_de=True, tz=ZoneInfo("Europe/Berlin"), tz_name="Europe/Berlin")
    lines = xl.lstrip("﻿").splitlines()
    assert xl.startswith("﻿") and lines[0].startswith("Time (Europe/Berlin);Time (UTC);")
    assert lines[1].startswith("2026-11-15 22:47:53;2026-11-15 21:47:53;KK;after service;2,5;21,5")


# ─── Routes ──────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_routes(tmp_config_dir, async_client):
    import app.main as main_mod
    st = main_mod.app.state.runtime
    st.config.subscriptions.subscriptions = [SubscriptionConfig(id="s1", server_id="srv", node_id="n1",
                                                                display_name="Pressure")]
    r = await async_client.post("/references/tables", json={"name": "Commissioning", "signals": []})
    assert r.status_code == 400
    r = await async_client.post("/references/tables", json={"name": "Commissioning",
                                                            "signals": [{"subscription_id": "s1", "unit": "bar"}]})
    tid = r.json()["id"]
    saved = yaml.safe_load((tmp_config_dir / "reference_tables.yaml").read_text())
    assert saved["tables"][0]["signals"] == [{"subscription_id": "s1", "unit": "bar"}]

    r = await async_client.post(f"/references/tables/{tid}/capture", json={"author": "KK", "comment": "c1"})
    body = r.json()
    assert r.status_code == 200 and body["problems"] == ["Pressure: missing"]     # not polled, no server
    eid = body["id"]
    page = await async_client.get(f"/references?table={tid}")
    assert page.status_code == 200 and "Commissioning" in page.text and "c1" in page.text
    assert (await async_client.post(f"/references/entries/{eid}/comment", json={"comment": "c2"})).status_code == 200
    csv_r = await async_client.get(f"/references/tables/{tid}/export.csv?format=excel_de&tz=Europe/Berlin")
    assert csv_r.status_code == 200 and "c2" in csv_r.text and "Commissioning.csv" in csv_r.headers["content-disposition"]
    assert (await async_client.post(f"/references/entries/{eid}/delete")).status_code == 200
    assert (await async_client.post(f"/references/entries/{eid}/delete")).status_code == 404
    await async_client.post(f"/references/tables/{tid}/delete")
    assert st.config.references.tables == []
