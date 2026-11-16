"""
Reference table entries in SQLite (references.db in the data directory).

One row per capture. The values are stored as JSON together with the label
and unit each signal had at that moment, so old rows stay correct when the
signals of a table change later.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.config import resolve_data_path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ref_entries (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    table_id  TEXT NOT NULL,
    ts        TEXT NOT NULL,          -- UTC, ISO 8601
    author    TEXT NOT NULL DEFAULT '',
    comment   TEXT NOT NULL DEFAULT '',
    vals      TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS ref_entries_table ON ref_entries (table_id, ts);
"""
# Added later: automatic captures group their rows by event (number per table)
# and note each row's offset in seconds to the trigger (NULL = manual row)
_MIGRATIONS = [
    "ALTER TABLE ref_entries ADD COLUMN event INTEGER",
    "ALTER TABLE ref_entries ADD COLUMN offset_s REAL",
]

_lock = threading.Lock()


@dataclass
class RefValue:
    subscription_id: str
    label: str
    unit: str = ""
    value: Optional[float | str] = None
    quality: str = "ok"          # ok · offline (server not connected, last known value) · missing


@dataclass
class RefEntry:
    id: int
    table_id: str
    ts: datetime
    author: str
    comment: str
    values: list[RefValue] = field(default_factory=list)
    event: Optional[int] = None        # automatic capture: event number within the table
    offset_s: Optional[float] = None   # seconds relative to the trigger (0 = trigger row)

    def value_of(self, subscription_id: str) -> Optional[RefValue]:
        return next((v for v in self.values if v.subscription_id == subscription_id), None)

    @property
    def quality(self) -> str:
        """Worst quality of the row: ok < offline < missing."""
        order = {"ok": 0, "offline": 1, "missing": 2}
        return max((v.quality for v in self.values), key=lambda q: order.get(q, 0), default="ok")


def db_path() -> Path:
    return Path(resolve_data_path("references.db"))


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(_SCHEMA)
    have = {r[1] for r in db.execute("PRAGMA table_info(ref_entries)")}
    for stmt in _MIGRATIONS:
        if stmt.split()[5] not in have:
            db.execute(stmt)
    return db


_COLS = "id, table_id, ts, author, comment, vals, event, offset_s"


def _row(r) -> RefEntry:
    vals = [RefValue(**v) for v in json.loads(r[5] or "[]")]
    return RefEntry(id=r[0], table_id=r[1], ts=datetime.fromisoformat(r[2]), author=r[3], comment=r[4],
                    values=vals, event=r[6], offset_s=r[7])


def add_entry(table_id: str, values: list[RefValue], author: str = "", comment: str = "",
              ts: Optional[datetime] = None, event: Optional[int] = None,
              offset_s: Optional[float] = None) -> int:
    ts = (ts or datetime.now(timezone.utc)).astimezone(timezone.utc)
    payload = json.dumps([v.__dict__ for v in values])
    with _lock, _connect() as db:
        cur = db.execute("INSERT INTO ref_entries (table_id, ts, author, comment, vals, event, offset_s) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (table_id, ts.isoformat(), author.strip()[:80], comment.strip()[:2000], payload,
                          event, offset_s))
        return int(cur.lastrowid)


def next_event(table_id: str) -> int:
    with _lock, _connect() as db:
        return int(db.execute("SELECT COALESCE(MAX(event), 0) + 1 FROM ref_entries WHERE table_id = ?",
                              (table_id,)).fetchone()[0])


def delete_event(table_id: str, event: int) -> int:
    with _lock, _connect() as db:
        return db.execute("DELETE FROM ref_entries WHERE table_id = ? AND event = ?", (table_id, event)).rowcount


def list_entries(table_id: str) -> list[RefEntry]:
    """Newest first."""
    with _lock, _connect() as db:
        rows = db.execute(f"SELECT {_COLS} FROM ref_entries "
                          "WHERE table_id = ? ORDER BY ts DESC, id DESC", (table_id,)).fetchall()
    return [_row(r) for r in rows]


def count_entries() -> dict[str, int]:
    with _lock, _connect() as db:
        return dict(db.execute("SELECT table_id, COUNT(*) FROM ref_entries GROUP BY table_id").fetchall())


def update_comment(entry_id: int, comment: str) -> bool:
    with _lock, _connect() as db:
        return db.execute("UPDATE ref_entries SET comment = ? WHERE id = ?",
                          (comment.strip()[:2000], entry_id)).rowcount > 0


def delete_entry(entry_id: int) -> bool:
    with _lock, _connect() as db:
        return db.execute("DELETE FROM ref_entries WHERE id = ?", (entry_id,)).rowcount > 0


def delete_table(table_id: str) -> int:
    with _lock, _connect() as db:
        return db.execute("DELETE FROM ref_entries WHERE table_id = ?", (table_id,)).rowcount
