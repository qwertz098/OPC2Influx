"""
The value history sent with an alarm e-mail: rows before the alarm, the alarm
row and rows after it, as plain text, an HTML table and a CSV file.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime
from html import escape

from app.alerting.mail_style import BODY, NUMS
from app.references.store import RefValue

Row = tuple[datetime, float, list[RefValue]]      # time, offset to the alarm (s), values

# Larger histories go only as CSV file: a long or wide table is unreadable in a mail
MAX_TABLE_ROWS = 100
MAX_TABLE_SIGNALS = 12


def _fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.6g}"
    return str(v)


def _header(v: RefValue) -> str:
    return f"{v.label} [{v.unit}]" if v.unit else v.label


def _quality(values: list[RefValue]) -> str:
    return ", ".join(f"{v.label}: {v.quality}" for v in values if v.quality != "ok")


def _local(ts: datetime) -> str:
    return ts.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _offset(o: float) -> str:
    return f"{o:+g}" if o else "0"


def render(rows: list[Row], title: str) -> tuple[str, str, str]:
    """(text, html, csv) for the rows, oldest first."""
    if not rows:
        return title, f'<div style="{BODY}"><p>{escape(title)}</p></div>', ""
    heads = [_header(v) for v in rows[0][2]]
    tz = rows[0][0].astimezone().strftime("%Z") or "local"

    # CSV: one row per time, plain dot decimals
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([f"Time ({tz})", "Time (UTC)", "Offset (s)", *heads, "Quality"])
    for ts, off, values in rows:
        w.writerow([_local(ts), ts.strftime("%Y-%m-%dT%H:%M:%SZ"), _offset(off),
                    *[_fmt(v.value) for v in values], _quality(values)])
    csv_text = buf.getvalue()

    if len(rows) > MAX_TABLE_ROWS or len(heads) > MAX_TABLE_SIGNALS:
        note = (f"{len(rows)} rows × {len(heads)} signals: too many values for a table in the mail, "
                f"see the attached CSV file (at most {MAX_TABLE_ROWS} rows and {MAX_TABLE_SIGNALS} signals "
                f"are shown as a table).")
        return (f"{title}\n\n{note}\n",
                f'<div style="{BODY}"><p style="margin:0 0 4px;font-weight:600">{escape(title)}</p>'
                f'<p style="color:#666;margin:0">{escape(note)}</p></div>', csv_text)

    # Plain text: aligned columns
    table = [[f"Time ({tz})", "Offset (s)", *heads]] + [
        [_local(ts), _offset(off), *[_fmt(v.value) for v in values]] for ts, off, values in rows]
    widths = [max(len(r[i]) for r in table) for i in range(len(table[0]))]
    lines = ["  ".join(c.rjust(widths[i]) if i else c.ljust(widths[i]) for i, c in enumerate(r)) for r in table]
    text = title + "\n\nValues around the alarm (row with offset 0 = alarm):\n\n" + "\n".join(lines) + "\n"

    # HTML: the alarm row highlighted, problems in the last column
    th = 'style="padding:4px 10px;border-bottom:2px solid #999;text-align:right;white-space:nowrap;color:#555;font-weight:600"'
    td = 'style="padding:3px 10px;text-align:right;white-space:nowrap;border-bottom:1px solid #eee"'
    out = [f'<div style="{BODY}"><p style="margin:0 0 4px;font-weight:600">{escape(title)}</p><p style="color:#666;margin:0 0 10px">Values around the alarm '
           f'(highlighted row = alarm). Also attached as CSV.</p>',
           f'<table style="border-collapse:collapse;font-size:13px;{NUMS}">',
           f'<tr><th {th}>Time ({escape(tz)})</th><th {th}>Offset (s)</th>'
           + "".join(f"<th {th}>{escape(h)}</th>" for h in heads) + f'<th {th}>Quality</th></tr>']
    for ts, off, values in rows:
        bg = ' style="background:#ffe9a8;font-weight:bold"' if off == 0 else ""
        out.append(f"<tr{bg}><td {td}>{_local(ts)}</td><td {td}>{_offset(off)}</td>"
                   + "".join(f"<td {td}>{escape(_fmt(v.value))}</td>" for v in values)
                   + f"<td {td}>{escape(_quality(values))}</td></tr>")
    out.append("</table></div>")
    return text, "\n".join(out), csv_text
