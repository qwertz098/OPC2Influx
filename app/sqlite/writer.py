"""SQLite snapshot writer — one row per server per write interval."""
from __future__ import annotations
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiosqlite

if TYPE_CHECKING:
    from app.state import RuntimeState

logger = logging.getLogger(__name__)

# SQLite's default SQLITE_MAX_COLUMN: a table holds at most 2000 columns,
# including "time". Signals beyond that are skipped with a warning.
MAX_COLUMNS = 2000


@dataclass
class SQLiteTargetStatus:
    rows_written: int = 0
    last_write: datetime | None = None
    last_error: str | None = None
    started_at: datetime | None = None
    # Signals not recorded because their server's table is full
    skipped_signals: int = 0


class SQLiteWriter:
    """Writes one wide-row snapshot per server per interval to SQLite."""

    def __init__(self, target_cfg, state: "RuntimeState") -> None:
        self._cfg = target_cfg
        self._state = state
        self._status = SQLiteTargetStatus()
        self._task: asyncio.Task | None = None
        self._db: aiosqlite.Connection | None = None
        self._known_columns: dict[str, set[str]] = {}  # table → existing columns

    @property
    def status(self) -> SQLiteTargetStatus:
        return self._status

    async def start(self) -> None:
        Path(self._cfg.path).parent.mkdir(parents=True, exist_ok=True)
        self._task = asyncio.create_task(self._write_loop(), name=f"sqlite-{self._cfg.id}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._close_db()

    async def _get_db(self) -> aiosqlite.Connection:
        """Persistent connection, opened lazily; WAL so readers never block writes."""
        if self._db is None:
            self._db = await aiosqlite.connect(self._cfg.path)
            await self._db.execute("PRAGMA journal_mode=WAL")
            await self._db.execute("PRAGMA synchronous=NORMAL")
            self._known_columns = {}
        return self._db

    async def _close_db(self) -> None:
        if self._db is not None:
            try:
                await self._db.close()
            except Exception:
                pass
            self._db = None
            self._known_columns = {}

    async def _write_loop(self) -> None:
        while True:
            await asyncio.sleep(self._cfg.write_interval_seconds)
            try:
                await self._snapshot()
            except Exception as exc:
                self._status.last_error = str(exc)
                logger.warning("SQLite write error: %s", exc)
                # Self-heal: reopen the connection on the next cycle
                await self._close_db()

    async def _snapshot(self) -> None:
        """Write one row per server with all current values."""
        state = self._state
        if not state.polling_engine:
            return
        last_values = state.polling_engine.live_values()
        subs = state.config.subscriptions.subscriptions
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        # Group subscriptions by server
        by_server: dict[str, list] = {}
        for sub in subs:
            if not sub.enabled:
                continue
            by_server.setdefault(sub.server_id, []).append(sub)

        db = await self._get_db()
        skipped = 0
        for server_id, server_subs in by_server.items():
            srv = next((s for s in state.config.servers.servers if s.id == server_id), None)
            table = (srv.display_name if srv else server_id).replace(" ", "_").replace("-", "_")
            # Sanitize table name
            table = "".join(c if c.isalnum() or c == "_" else "_" for c in table)

            # Column → value; a later subscription with the same column name wins
            values: dict[str, str | None] = {}
            for sub in server_subs:
                col = (sub.influxdb.measurement.replace(".", "_") + "_" + sub.influxdb.field_name)
                col = "".join(c if c.isalnum() or c == "_" else "_" for c in col)
                val = last_values.get(sub.id)
                values[col] = str(val) if val is not None else None

            if not values:
                continue

            # Ensure table and columns exist; schema is introspected once per
            # table and tracked in _known_columns afterwards.
            existing = self._known_columns.get(table)
            if existing is None:
                await db.execute(
                    f"CREATE TABLE IF NOT EXISTS [{table}] (time TEXT)"
                )
                await db.execute(
                    f"CREATE INDEX IF NOT EXISTS [idx_{table}_time] ON [{table}](time)"
                )
                existing = {row[1] async for row in await db.execute(f"PRAGMA table_info([{table}])")}
                self._known_columns[table] = existing
            for col in list(values):
                if col in existing:
                    continue
                if len(existing) >= MAX_COLUMNS:
                    skipped += 1
                    del values[col]
                    continue
                await db.execute(f"ALTER TABLE [{table}] ADD COLUMN [{col}] TEXT")
                existing.add(col)

            cols, vals = list(values), list(values.values())
            placeholders = ", ".join(["?"] * (len(cols) + 1))
            col_names = ", ".join(["time"] + [f"[{c}]" for c in cols])
            await db.execute(
                f"INSERT INTO [{table}] ({col_names}) VALUES ({placeholders})",
                [now] + vals,
            )
        await db.commit()

        if self._status.started_at is None:
            self._status.started_at = datetime.now(timezone.utc)
        self._status.rows_written += len(by_server)
        self._status.last_write = datetime.now(timezone.utc)
        self._status.last_error = None
        self._report_skipped(skipped)

    def _report_skipped(self, skipped: int) -> None:
        """Warn (log + event) whenever the number of signals that do not fit changes."""
        if skipped == self._status.skipped_signals:
            return
        self._status.skipped_signals = skipped
        if not skipped:
            return
        msg = (f"SQLite '{self._cfg.display_name}': {skipped} signal(s) not recorded — "
               f"at most {MAX_COLUMNS - 1} signals per server fit into one table")
        logger.warning(msg)
        self._state.add_alarm("warning", f"sqlite:{self._cfg.id}", msg)


class SQLiteWriterPool:
    def __init__(self, state: "RuntimeState") -> None:
        self._state = state
        self._writers: dict[str, SQLiteWriter] = {}

    async def start(self) -> None:
        await self.reconcile()

    async def reconcile(self) -> None:
        all_ids = {t.id for t in self._state.config.sqlite.targets}
        cfg_ids = {t.id for t in self._state.config.sqlite.targets if t.enabled}
        # Stop removed/disabled writers
        for tid in list(self._writers):
            if tid not in cfg_ids:
                await self._writers.pop(tid).stop()
        # Remove status only for fully deleted targets (not just disabled)
        for tid in list(self._state.sqlite_status):
            if tid not in all_ids:
                del self._state.sqlite_status[tid]
        # Start new enabled writers
        for t in self._state.config.sqlite.targets:
            if t.enabled and t.id not in self._writers:
                w = SQLiteWriter(t, self._state)
                await w.start()
                self._writers[t.id] = w
        # Update status of active writers (disabled ones keep their last known status)
        for tid, w in self._writers.items():
            self._state.sqlite_status[tid] = w.status

    async def stop(self) -> None:
        for w in self._writers.values():
            await w.stop()
        self._writers.clear()
