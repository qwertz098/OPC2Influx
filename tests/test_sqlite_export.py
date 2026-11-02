"""Tests for the SQLite CSV export: signal selection, range, downsampling, size guard."""
from __future__ import annotations

import csv
import io
import sqlite3
import zipfile

import pytest

from app.sqlite import export as sx


def _make_db(path) -> str:
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE PLC_A (time TEXT, temp_value TEXT, run_value TEXT, name_value TEXT)")
    db.execute("CREATE TABLE PLC_B (time TEXT, flow_value TEXT)")
    for m in range(10):  # 10 snapshots, one per minute
        t = f"2026-01-01 00:{m:02d}:00"
        db.execute("INSERT INTO PLC_A VALUES (?, ?, ?, ?)", (t, str(20.0 + m), str(m % 2 == 0), f"batch.{m}"))
        if m % 2 == 0:
            db.execute("INSERT INTO PLC_B VALUES (?, ?)", (t, str(m * 1.5)))
    db.commit()
    db.close()
    return str(path)


def _rows(path, spec, labels=None):
    db = sx.connect(path)
    try:
        text = "".join(sx.iter_csv(db, spec, labels or {}))
    finally:
        db.close()
    delim = ";" if spec.fmt == "excel_de" else ","
    return list(csv.reader(io.StringIO(text.lstrip("﻿")), delimiter=delim))


def _schema(path):
    db = sx.connect(path)
    try:
        return sx.read_schema(db)
    finally:
        db.close()


def test_select_signals_and_range_merges_tables(tmp_path):
    path = _make_db(tmp_path / "a.db")
    spec = sx.parse_spec(_schema(path), ["PLC_A/temp_value", "PLC_B/flow_value"],
                         start="2026-01-01T00:02", end="2026-01-01 00:05:00")
    rows = _rows(path, spec, {"temp_value": "Temp"})
    assert rows[0] == ["time (UTC)", "PLC_A / Temp", "PLC_B / flow_value"]
    assert [r[0] for r in rows[1:]] == [f"2026-01-01 00:0{m}:00" for m in range(2, 6)]
    assert rows[1] == ["2026-01-01 00:02:00", "22.0", "3.0"]
    assert rows[2] == ["2026-01-01 00:03:00", "23.0", ""]  # PLC_B has no row at :03


def test_downsampling_avg_treats_booleans_as_numbers(tmp_path):
    path = _make_db(tmp_path / "a.db")
    spec = sx.parse_spec(_schema(path), ["PLC_A/temp_value", "PLC_A/run_value", "PLC_A/name_value"],
                         interval="5m", method="avg")
    rows = _rows(path, spec)
    assert len(rows) == 3
    assert rows[1] == ["2026-01-01 00:00:00", "22.0", "0.6", ""]  # avg 20..24, 3 of 5 True
    assert rows[2][:3] == ["2026-01-01 00:05:00", "27.0", "0.4"]


def test_downsampling_last_keeps_text(tmp_path):
    path = _make_db(tmp_path / "a.db")
    spec = sx.parse_spec(_schema(path), ["PLC_A/name_value"], interval="5m", method="last")
    assert [r[1] for r in _rows(path, spec)[1:]] == ["batch.4", "batch.9"]


def test_excel_format_uses_semicolon_and_decimal_comma(tmp_path):
    path = _make_db(tmp_path / "a.db")
    spec = sx.parse_spec(_schema(path), ["PLC_A/temp_value", "PLC_A/name_value"], fmt="excel_de")
    rows = _rows(path, spec)
    assert rows[1][1:] == ["20,0", "batch.0"]  # non-numeric text keeps its dot


def test_estimate_counts_rows_and_flags_limits(tmp_path, monkeypatch):
    path = _make_db(tmp_path / "a.db")
    schema = _schema(path)
    db = sx.connect(path)
    try:
        est = sx.estimate(db, sx.parse_spec(schema, None))
        assert est["rows"] == 10 and est["bytes"] > 0 and not est["too_large"]
        assert sx.estimate(db, sx.parse_spec(schema, None, interval="5m"))["rows"] == 2
        monkeypatch.setattr(sx, "MAX_BYTES", 10)
        assert sx.estimate(db, sx.parse_spec(schema, None))["too_large"]
    finally:
        db.close()


def test_parse_spec_rejects_unknown_signal_and_bad_options(tmp_path):
    schema = _schema(_make_db(tmp_path / "a.db"))
    for kwargs in (
        {"signals": ['PLC_A/x" FROM sqlite_master--']},
        {"signals": []},
        {"signals": None, "interval": "7s"},
        {"signals": None, "method": "sum"},
        {"signals": None, "start": "2026-02-01", "end": "2026-01-01"},
    ):
        with pytest.raises(ValueError):
            sx.parse_spec(schema, **kwargs)


def test_zip_export_streams_valid_archive(tmp_path):
    path = _make_db(tmp_path / "a.db")
    spec = sx.parse_spec(_schema(path), None, zip_=True)
    data = b"".join(sx.iter_export(path, spec, {}, "out.csv"))
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        text = zf.read("out.csv").decode()
    assert text.count("\n") == 11  # header + 10 rows


@pytest.mark.asyncio
async def test_export_endpoints(async_client, tmp_path):
    from app.config import SQLiteTargetConfig
    import app.main as main_mod

    path = _make_db(tmp_path / "snap.db")
    target = SQLiteTargetConfig(display_name="Log", file_path=path)
    main_mod.app.state.runtime.config.sqlite.targets.append(target)
    base = f"/config/sqlite/{target.id}/export"

    dialog = await async_client.get(base + "/dialog")
    assert dialog.status_code == 200 and 'value="PLC_B/flow_value"' in dialog.text

    form = {"signal": ["PLC_A/temp_value"], "interval": "5m", "method": "max"}
    est = await async_client.post(base + "/estimate", data=form)
    assert est.json()["rows"] == 2
    bad = await async_client.post(base + "/estimate", data={"signal": ["PLC_A/nope"]})
    assert bad.status_code == 400

    resp = await async_client.post(base, data=form)
    assert resp.status_code == 200
    assert resp.text.splitlines()[1:] == ["2026-01-01 00:00:00,24.0", "2026-01-01 00:05:00,29.0"]

    full = await async_client.get(base)
    assert full.status_code == 200 and len(full.text.splitlines()) == 11
