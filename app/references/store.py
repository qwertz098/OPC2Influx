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
    return db


def _row(r) -> RefEntry:
    vals = [RefValue(**v) for v in json.loads(r[5] or "[]")]
    return RefEntry(id=r[0], table_id=r[1], ts=datetime.fromisoformat(r[2]), author=r[3], comment=r[4], values=vals)


def add_entry(table_id: str, values: list[RefValue], author: str = "", comment: str = "",
              ts: Optional[datetime] = None) -> int:
    ts = (ts or datetime.now(timezone.utc)).astimezone(timezone.utc)
    payload = json.dumps([v.__dict__ for v in values])
    with _lock, _connect() as db:
        cur = db.execute("INSERT INTO ref_entries (table_id, ts, author, comment, vals) VALUES (?, ?, ?, ?, ?)",
                         (table_id, ts.isoformat(), author.strip()[:80], comment.strip()[:2000], payload))
        return int(cur.lastrowid)


def list_entries(table_id: str) -> list[RefEntry]:
    """Newest first."""
    with _lock, _connect() as db:
        rows = db.execute("SELECT id, table_id, ts, author, comment, vals FROM ref_entries "
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
