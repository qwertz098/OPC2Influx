"""
Value histories of alarms in SQLite (alarm_history.db in the data directory).

One record per alarm: the rows before it and the alarm row are written when
it fires, the rows after it once they are collected (``complete``). Each row
keeps the label and unit its signals had then. The newest KEEP records stay.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.config import resolve_data_path
from app.references.store import RefValue

KEEP = 1000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS alarm_history (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id   TEXT NOT NULL,
    ts        TEXT NOT NULL,          -- alarm time, UTC ISO 8601
    title     TEXT NOT NULL DEFAULT '',
    rows      TEXT NOT NULL DEFAULT '[]',
    complete  INTEGER NOT NULL DEFAULT 0
);
"""
_lock = threading.Lock()

Row = tuple[datetime, float, list[RefValue]]


@dataclass
class AlarmHistory:
    id: int
    rule_id: str
    ts: datetime
    title: str
    rows: list[Row]
    complete: bool


def db_path() -> Path:
    return Path(resolve_data_path("alarm_history.db"))


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(_SCHEMA)
    return db


def _dump(rows: list[Row]) -> str:
    return json.dumps([{"ts": ts.astimezone(timezone.utc).isoformat(), "offset": off,
                        "values": [v.__dict__ for v in vals]} for ts, off, vals in rows])


def _load(text: str) -> list[Row]:
    return [(datetime.fromisoformat(r["ts"]), r["offset"], [RefValue(**v) for v in r["values"]])
            for r in json.loads(text or "[]")]


def create(rule_id: str, ts: datetime, title: str, rows: list[Row], complete: bool = False) -> int:
    with _lock, _connect() as db:
        cur = db.execute("INSERT INTO alarm_history (rule_id, ts, title, rows, complete) VALUES (?, ?, ?, ?, ?)",
                         (rule_id, ts.astimezone(timezone.utc).isoformat(), title, _dump(rows), int(complete)))
        new_id = int(cur.lastrowid)
        db.execute("DELETE FROM alarm_history WHERE id <= ?", (new_id - KEEP,))
        return new_id


def finish(history_id: int, rows: list[Row]) -> None:
    with _lock, _connect() as db:
        db.execute("UPDATE alarm_history SET rows = ?, complete = 1 WHERE id = ?", (_dump(rows), history_id))


def get(history_id: int) -> Optional[AlarmHistory]:
    with _lock, _connect() as db:
        r = db.execute("SELECT id, rule_id, ts, title, rows, complete FROM alarm_history WHERE id = ?",
                       (history_id,)).fetchone()
    if r is None:
        return None
    return AlarmHistory(id=r[0], rule_id=r[1], ts=datetime.fromisoformat(r[2]), title=r[3],
                        rows=_load(r[4]), complete=bool(r[5]))
