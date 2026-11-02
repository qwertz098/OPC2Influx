"""CSV export of SQLite snapshot tables: signal selection, time range, downsampling.

All functions here are synchronous (stdlib sqlite3) and meant to run in a
worker thread. Output is streamed, so memory use stays flat regardless of
the export size.
"""
from __future__ import annotations

import csv
import heapq
import io
import itertools
import sqlite3
import zipfile
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Iterator

# Downsampling bucket sizes offered in the dialog (key → seconds; "" = raw).
INTERVALS: dict[str, int] = {"": 0, "1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}
METHODS = ("avg", "min", "max", "last")
FORMATS = ("standard", "excel_de")

EXCEL_MAX_ROWS = 1_048_576
WARN_BYTES = 50 * 1024 * 1024
MAX_BYTES = 1024 * 1024 * 1024
ZIP_SUGGEST_BYTES = 20 * 1024 * 1024

_TIME_FMT = "%Y-%m-%d %H:%M:%S"
_CHUNK_ROWS = 2000
# Assumed text width of an aggregated (avg/min/max) value for size estimates.
_AGG_VALUE_LEN = 10


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _num(value) -> float | None:
    """Numeric view of a stored TEXT value; booleans count as 0/1, other text as NULL."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if s in ("True", "true"):
        return 1.0
    if s in ("False", "false"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return None


def connect(path: str) -> sqlite3.Connection:
    # Streaming responses advance the generator from varying worker threads
    # (one at a time), so the connection must not be pinned to its creator.
    db = sqlite3.connect(path, check_same_thread=False)
    db.create_function("num", 1, _num, deterministic=True)
    return db


def read_schema(db: sqlite3.Connection) -> dict[str, list[str]]:
    """Map table → data columns, for every table that has a ``time`` column."""
    tables = [r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )]
    schema: dict[str, list[str]] = {}
    for table in tables:
        cols = [r[1] for r in db.execute(f"PRAGMA table_info({quote_ident(table)})")]
        if "time" in cols:
            schema[table] = [c for c in cols if c != "time"]
    return schema


def time_bounds(db: sqlite3.Connection, tables: Iterable[str]) -> tuple[str | None, str | None]:
    lo = hi = None
    for table in tables:
        row = db.execute(f"SELECT MIN(time), MAX(time) FROM {quote_ident(table)}").fetchone()
        if row[0] is not None:
            lo = row[0] if lo is None else min(lo, row[0])
            hi = row[1] if hi is None else max(hi, row[1])
    return lo, hi


@dataclass
class ExportSpec:
    signals: dict[str, list[str]]  # table → columns, in output order
    start: str | None = None       # inclusive, UTC, "YYYY-MM-DD HH:MM:SS"
    end: str | None = None         # inclusive
    interval: int = 0              # bucket seconds, 0 = raw rows
    method: str = "avg"
    fmt: str = "standard"
    zip: bool = False


def _parse_time(value: str | None) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("T", " ")).strftime(_TIME_FMT)
    except ValueError:
        raise ValueError(f"Invalid time: {value!r}")


def parse_spec(
    schema: dict[str, list[str]],
    signals: list[str] | None,
    start: str | None = None,
    end: str | None = None,
    interval: str = "",
    method: str = "avg",
    fmt: str = "standard",
    zip_: bool = False,
) -> ExportSpec:
    """Validate request parameters against the DB schema.

    ``signals`` are ``"table/column"`` strings; ``None`` selects every signal.
    Raises ValueError with a user-facing message.
    """
    if signals is None:
        selected = {t: list(cols) for t, cols in schema.items() if cols}
    else:
        selected: dict[str, list[str]] = {}
        for sig in signals:
            table, _, col = sig.partition("/")
            if col not in schema.get(table, ()):
                raise ValueError(f"Unknown signal: {sig}")
            if col not in selected.setdefault(table, []):
                selected[table].append(col)
    if not selected:
        raise ValueError("Select at least one signal.")
    if interval not in INTERVALS:
        raise ValueError("Invalid downsampling interval.")
    if method not in METHODS:
        raise ValueError("Invalid aggregation method.")
    if fmt not in FORMATS:
        raise ValueError("Invalid CSV format.")
    spec = ExportSpec(
        signals=selected, start=_parse_time(start), end=_parse_time(end),
        interval=INTERVALS[interval], method=method, fmt=fmt, zip=zip_,
    )
    if spec.start and spec.end and spec.start > spec.end:
        raise ValueError("Start time is after end time.")
    return spec


def _key_expr(interval: int) -> str:
    """Row key: the timestamp itself, or the start of its UTC bucket."""
    if not interval:
        return "time"
    # interval comes from INTERVALS, never from user text
    return (f"strftime('%Y-%m-%d %H:%M:%S', "
            f"(CAST(strftime('%s', time) AS INTEGER) / {int(interval)}) * {int(interval)}, 'unixepoch')")


def _where(spec: ExportSpec) -> tuple[str, list]:
    clauses, params = [], []
    if spec.start:
        clauses.append("time >= ?")
        params.append(spec.start)
    if spec.end:
        clauses.append("time <= ?")
        params.append(spec.end)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def count_rows(db: sqlite3.Connection, spec: ExportSpec) -> int:
    """Exact number of CSV data rows (distinct timestamps/buckets across tables)."""
    where, params = _where(spec)
    key = _key_expr(spec.interval)
    union = " UNION ".join(f"SELECT DISTINCT {key} FROM {quote_ident(t)}{where}" for t in spec.signals)
    return db.execute(f"SELECT COUNT(*) FROM ({union})", params * len(spec.signals)).fetchone()[0]


def estimate(db: sqlite3.Connection, spec: ExportSpec) -> dict:
    """Row count plus a size estimate from a sample of recent values in range."""
    rows = count_rows(db, spec)
    where, params = _where(spec)
    row_len = 19 + 2  # timestamp + CRLF
    for table, cols in spec.signals.items():
        if spec.interval and spec.method != "last":
            row_len += len(cols) * (_AGG_VALUE_LEN + 1)
            continue
        avgs = ", ".join(f"AVG(LENGTH({quote_ident(c)}))" for c in cols)
        sample = (f"SELECT {', '.join(quote_ident(c) for c in cols)} FROM {quote_ident(table)}"
                  f"{where} ORDER BY time DESC LIMIT 1000")
        lengths = db.execute(f"SELECT {avgs} FROM ({sample})", params).fetchone()
        row_len += sum((n or 0) + 1 for n in lengths)
    size = rows * row_len

    warnings = []
    too_large = size > MAX_BYTES
    if too_large:
        warnings.append(f"Estimated size exceeds the {MAX_BYTES // 1024**3} GB limit — "
                        "narrow the time range, select fewer signals or downsample.")
    elif size > WARN_BYTES:
        warnings.append("Large export — consider downsampling or a shorter time range.")
    if rows > EXCEL_MAX_ROWS:
        warnings.append(f"More than {EXCEL_MAX_ROWS:,} rows — Excel will not open all of them.")
    return {
        "rows": rows,
        "bytes": size,
        "too_large": too_large,
        "suggest_zip": size > ZIP_SUGGEST_BYTES,
        "warnings": warnings,
    }


def _table_rows(db: sqlite3.Connection, table: str, cols: list[str], spec: ExportSpec) -> Iterator[tuple]:
    where, params = _where(spec)
    qcols = [quote_ident(c) for c in cols]
    qtable = quote_ident(table)
    if not spec.interval:
        sql = f"SELECT time, {', '.join(qcols)} FROM {qtable}{where} ORDER BY time"
        yield from db.execute(sql, params)
        return
    key = _key_expr(spec.interval)
    if spec.method == "last":
        # SQLite takes bare columns from the row that holds MAX(time).
        sql = (f"SELECT {key} AS k, MAX(time), {', '.join(qcols)} FROM {qtable}{where} "
               f"GROUP BY k ORDER BY k")
        for row in db.execute(sql, params):
            yield (row[0],) + row[2:]
        return
    agg = spec.method.upper()
    sql = (f"SELECT {key} AS k, {', '.join(f'{agg}(num({c}))' for c in qcols)} "
           f"FROM {qtable}{where} GROUP BY k ORDER BY k")
    yield from db.execute(sql, params)


def _format_value(value, decimal_comma: bool) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        text = repr(round(value, 6))
    else:
        text = str(value)
    if decimal_comma and "." in text and _num(text) is not None:
        text = text.replace(".", ",")
    return text


def _headers(spec: ExportSpec, labels: dict[str, str]) -> list[str]:
    time_header = "time (UTC)"
    if spec.interval:
        time_header = f"time (UTC, bucket start, {spec.method})"
    prefix_table = len(spec.signals) > 1
    headers = [time_header]
    for table, cols in spec.signals.items():
        for col in cols:
            label = labels.get(col, col)
            headers.append(f"{table} / {label}" if prefix_table else label)
    return headers


def iter_csv(db: sqlite3.Connection, spec: ExportSpec, labels: dict[str, str]) -> Iterator[str]:
    """Yield CSV text in chunks; tables are merged on their timestamp/bucket."""
    excel = spec.fmt == "excel_de"
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";" if excel else ",")
    if excel:
        buf.write("﻿")  # BOM so Excel detects UTF-8
    writer.writerow(_headers(spec, labels))

    tables = list(spec.signals.items())
    offsets = list(itertools.accumulate([0] + [len(c) for _, c in tables]))
    width = offsets[-1]
    def tagged(i: int, table: str, cols: list[str]) -> Iterator[tuple]:
        for row in _table_rows(db, table, cols, spec):
            yield row[0], i, row[1:]

    streams = [tagged(i, table, cols) for i, (table, cols) in enumerate(tables)]
    merged = heapq.merge(*streams, key=lambda r: r[0])
    for n, (key, group) in enumerate(itertools.groupby(merged, key=lambda r: r[0]), 1):
        out = [""] * width
        for _, i, values in group:
            out[offsets[i]:offsets[i + 1]] = [_format_value(v, excel) for v in values]
        writer.writerow([key] + out)
        if n % _CHUNK_ROWS == 0:
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate()
    yield buf.getvalue()


class _Sink(io.RawIOBase):
    """Unseekable write target; ZipFile then streams entries with data descriptors."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def writable(self) -> bool:
        return True

    def write(self, b) -> int:
        self._buf += b
        return len(b)

    def drain(self) -> bytes:
        out = bytes(self._buf)
        self._buf.clear()
        return out


def iter_export(path: str, spec: ExportSpec, labels: dict[str, str], csv_name: str) -> Iterator[bytes]:
    """Open the DB and stream the export as CSV or a single-entry ZIP."""
    db = connect(path)
    try:
        chunks = (c.encode("utf-8") for c in iter_csv(db, spec, labels))
        if not spec.zip:
            yield from chunks
            return
        sink = _Sink()
        with zipfile.ZipFile(sink, "w", zipfile.ZIP_DEFLATED) as zf:
            with zf.open(csv_name, "w", force_zip64=True) as entry:
                for chunk in chunks:
                    entry.write(chunk)
                    data = sink.drain()
                    if data:
                        yield data
        yield sink.drain()
    finally:
        db.close()
