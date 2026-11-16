"""Automatic captures of reference tables: trigger, rows before and after."""
import asyncio
import types
from datetime import datetime, timedelta, timezone

import pytest

from app.config import ReferenceSignalConfig, ReferenceTableConfig, ReferenceTriggerConfig, SubscriptionConfig
from app.references import store
from app.references.recorder import ReferenceRecorder, side_rows
from app.state import ServerStatus

T0 = datetime.now(timezone.utc) - timedelta(hours=1)   # in the past: rows after a trigger need no waiting


@pytest.fixture(autouse=True)
def ref_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "db_path", lambda: tmp_path / "references.db")


@pytest.fixture
def state(minimal_state):
    minimal_state.config.subscriptions.subscriptions = [
        SubscriptionConfig(id="p", server_id="srv", node_id="n1", display_name="Pressure"),
        SubscriptionConfig(id="err", server_id="srv", node_id="n2", display_name="Error"),
    ]
    minimal_state.server_status["srv"] = ServerStatus(connected=True)
    live = {"p": 1.0, "err": False}
    minimal_state.polling_engine = types.SimpleNamespace(live_values=lambda: dict(live))
    minimal_state._live = live
    return minimal_state


def _table(state, **trig):
    t = ReferenceTableConfig(id="T", name="Faults", signals=[ReferenceSignalConfig(subscription_id="p", unit="bar")],
                             trigger=ReferenceTriggerConfig(enabled=True, subscription_id="err", condition="=",
                                                            threshold="true", **trig))
    state.config.references.tables = [t]
    return t


def _run(rec, state, seconds, start=0, change=None):
    """One tick per second; change(i) may modify the live values before tick i."""
    for i in range(start, start + seconds):
        if change:
            change(i)
        asyncio.run(rec.tick(state, now=T0 + timedelta(seconds=i)))


def _rows():
    return sorted(store.list_entries("T"), key=lambda e: (e.ts, e.id))


def test_side_rows():
    assert side_rows(10, 1) == 10 and side_rows(10, 3) == 4 and side_rows(0, 1) == 0 and side_rows(99999, 1) == 300


def test_event_with_rows_before(state):
    _table(state, pre_seconds=3, step_seconds=1)
    rec = ReferenceRecorder()

    def change(i):
        state._live["p"] = 1.0 + i              # pressure rises every second
        state._live["err"] = i >= 5              # error from second 5 on
    _run(rec, state, 8, change=change)
    rows = _rows()
    assert [(r.event, r.offset_s) for r in rows] == [(1, -3.0), (1, -2.0), (1, -1.0), (1, 0.0)]
    assert [r.value_of("p").value for r in rows] == [3.0, 4.0, 5.0, 6.0]
    assert rows[-1].comment == "Auto: Error = true (True)"
    assert "event 1 captured" in state.active_alarms[-1].message


def test_rearm_only_after_condition_cleared(state):
    _table(state)
    rec = ReferenceRecorder()
    _run(rec, state, 12, change=lambda i: state._live.update(err=i in (2, 3, 4, 8, 9)))
    assert sorted({r.event for r in _rows()}) == [1, 2]                     # two rising edges


def test_condition_true_at_start_does_not_count(state):
    _table(state)
    state._live["err"] = True
    rec = ReferenceRecorder()
    _run(rec, state, 5)
    assert _rows() == []
    _run(rec, state, 3, start=5, change=lambda i: state._live.update(err=i != 5))   # clears once, comes back
    assert [r.event for r in _rows()] == [1]


def test_delay(state):
    _table(state, delay_seconds=3)
    rec = ReferenceRecorder()
    _run(rec, state, 6, change=lambda i: state._live.update(err=i in (1, 2)))  # too short
    assert _rows() == []
    _run(rec, state, 6, start=6, change=lambda i: state._live.update(err=i >= 7))
    rows = _rows()
    assert len(rows) == 1 and rows[0].ts == T0 + timedelta(seconds=10)      # true at 7, held 3 s


def test_rows_after_trigger(state):
    _table(state, post_seconds=4, step_seconds=2)
    rec = ReferenceRecorder()

    async def go():
        for i in range(4):
            state._live["err"] = i >= 2
            await rec.tick(state, now=T0 + timedelta(seconds=i))
        await asyncio.gather(*rec._tasks)
    asyncio.run(go())
    assert [(r.event, r.offset_s) for r in _rows()] == [(1, 0.0), (1, 2.0), (1, 4.0)]
    assert not rec.busy


def test_delete_event_and_csv_columns(state):
    from app.references.export import to_csv
    t = _table(state, pre_seconds=2)
    rec = ReferenceRecorder()
    _run(rec, state, 5, change=lambda i: state._live.update(err=i >= 3))
    store.add_entry("T", [], "KK", "manual")
    csv = to_csv(t, store.list_entries("T")).splitlines()
    assert csv[0].startswith("Time (UTC),Time (UTC),Event,Offset (s),")
    assert csv[1].split(",")[2:4] == ["1", "-2.0"]
    assert store.delete_event("T", 1) == 3
    assert [r.comment for r in store.list_entries("T")] == ["manual"]


def test_disabled_trigger_does_nothing(state):
    t = _table(state, pre_seconds=5)
    t.trigger.enabled = False
    rec = ReferenceRecorder()
    _run(rec, state, 6, change=lambda i: state._live.update(err=i >= 2))
    assert _rows() == [] and rec.buffer.owners() == []


def test_no_rearm_while_rows_after_are_written(state):
    # a noisy signal that drops below the threshold during the rows after an
    # event must not start the next event right after them
    _table(state, post_seconds=4, step_seconds=1)
    rec = ReferenceRecorder()
    rec.busy.add("T")                           # pretend: rows after an event are being written
    rec.armed["T"] = False
    rec.trigger_key["T"] = ("err", "=", "true", 0)
    _run(rec, state, 4, change=lambda i: state._live.update(err=i % 2 == 0))
    assert rec.armed["T"] is False and _rows() == []
    rec.busy.clear()
    _run(rec, state, 3, start=4, change=lambda i: state._live.update(err=i == 6))   # clears, comes back
    assert [r.event for r in _rows()] == [1]
