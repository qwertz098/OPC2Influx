"""CSV export of a reference table."""
from __future__ import annotations

import csv
import io
from datetime import timezone, tzinfo
from typing import Iterable

from app.config import ReferenceTableConfig
from app.references.store import RefEntry
from app.sqlite.export import _format_value


def columns(table: ReferenceTableConfig, entries: Iterable[RefEntry]) -> list[tuple[str, str, str]]:
    """(subscription_id, label, unit) of every value column: the table's
    signals first, then signals that only older rows have (removed since)."""
    cols: dict[str, tuple[str, str, str]] = {}
    entries = list(entries)
    for sig in table.signals:
        last = next((e.value_of(sig.subscription_id) for e in entries if e.value_of(sig.subscription_id)), None)
        cols[sig.subscription_id] = (sig.subscription_id, last.label if last else sig.subscription_id, sig.unit)
    for e in entries:
        for v in e.values:
            cols.setdefault(v.subscription_id, (v.subscription_id, v.label, v.unit))
    return list(cols.values())


def _header(label: str, unit: str) -> str:
    return f"{label} [{unit}]" if unit else label


def to_csv(table: ReferenceTableConfig, entries: list[RefEntry], excel_de: bool = False,
           tz: tzinfo = timezone.utc, tz_name: str = "UTC") -> str:
    """Oldest first. excel_de: ';' as separator and decimal comma."""
    cols = columns(table, entries)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";" if excel_de else ",")
    w.writerow([f"Time ({tz_name})", "Time (UTC)", "Event", "Offset (s)", "Name", "Comment"]
               + [_header(label, unit) for _, label, unit in cols] + ["Quality"])
    for e in sorted(entries, key=lambda e: (e.ts, e.id)):
        offset = "" if e.offset_s is None else _format_value(e.offset_s, excel_de)
        row = [e.ts.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S"),
               e.ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
               "" if e.event is None else e.event, offset, e.author, e.comment]
        issues = []
        for sid, label, _ in cols:
            v = e.value_of(sid)
            row.append("" if v is None or v.value is None else _format_value(v.value, excel_de))
            if v is not None and v.quality != "ok":
                issues.append(f"{label}: {v.quality}")
        row.append("; ".join(issues) if not excel_de else ", ".join(issues))
        w.writerow(row)
    return "﻿" + buf.getvalue() if excel_de else buf.getvalue()
