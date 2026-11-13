"""Report e-mail: subject, plain text and HTML from ReportFigures."""
from __future__ import annotations

import html
from datetime import datetime
from typing import Optional

from app.reports.compute import ReportFigures, SignalFigures
from app.reports.periods import PERIOD_LABELS


def fmt_num(v: Optional[float]) -> str:
    if v is None:
        return "–"
    a = abs(v)
    if a >= 1000:
        return f"{v:,.0f}".replace(",", " ")
    if a >= 100:
        return f"{v:.1f}"
    if a >= 1:
        return f"{v:.2f}"
    return f"{v:.3g}"


def fmt_change(cur: Optional[float], prev: Optional[float]) -> str:
    """'+12.5 (+3.1 %)' — difference to the period before."""
    if cur is None or prev is None:
        return "–"
    d = cur - prev
    sign = "+" if d >= 0 else "−"
    text = f"{sign}{fmt_num(abs(d))}"
    if prev:
        text += f" ({sign}{abs(d) / abs(prev) * 100:.1f} %)"
    return text


def fmt_when(t: Optional[datetime], tz_mode: str) -> str:
    if t is None:
        return ""
    local = t if tz_mode == "utc" else t.astimezone()
    return local.strftime("%d.%m. %H:%M")


def _with_unit(v: Optional[float], unit: str) -> str:
    return f"{fmt_num(v)} {unit}".strip() if v is not None else "–"


def subject(fig: ReportFigures) -> str:
    return f"{PERIOD_LABELS[fig.period.kind]} report {fig.report.name}: {fig.period.label}"


def _gauge_rows(sigs: list[SignalFigures], tz_mode: str) -> list[list[str]]:
    rows = []
    for s in sigs:
        rows.append([
            s.label,
            _with_unit(s.mean, s.unit), fmt_change(s.mean, s.prev_mean),
            f"{_with_unit(s.min, s.unit)} {fmt_when(s.min_time, tz_mode)}".strip(),
            f"{_with_unit(s.max, s.unit)} {fmt_when(s.max_time, tz_mode)}".strip(),
            _with_unit(s.energy, s.energy_unit) if s.energy is not None else "",
            fmt_change(s.energy, s.prev_energy) if s.energy is not None else "",
            s.error,
        ])
    return rows


def _counter_rows(sigs: list[SignalFigures]) -> list[list[str]]:
    return [[s.label, _with_unit(s.reading, s.unit), _with_unit(s.prev_reading, s.unit),
             _with_unit(s.counter_change, s.unit), _with_unit(s.prev_counter_change, s.unit),
             fmt_change(s.counter_change, s.prev_counter_change), s.error] for s in sigs]


GAUGE_HEAD = ["Trend value", "Mean", "vs. previous", "Min", "Max", "Integral", "vs. previous", ""]
COUNTER_HEAD = ["Counter reading", "Reading", "Last report", "Change", "Change before", "vs. previous", ""]


def text_body(fig: ReportFigures, tz_mode: str) -> str:
    p = fig.period
    lines = [subject(fig), "",
             f"Period: {fmt_when(p.start, tz_mode)} – {fmt_when(p.end, tz_mode)}"
             f" ({'UTC' if tz_mode == 'utc' else 'local time'})",
             f"Source: InfluxDB {fig.source}" if fig.source else ""]
    if fig.error:
        lines += ["", f"Error: {fig.error}"]
    gauges = [s for s in fig.signals if s.kind == "gauge"]
    counters = [s for s in fig.signals if s.kind == "counter"]
    for head, rows in ((GAUGE_HEAD, _gauge_rows(gauges, tz_mode)), (COUNTER_HEAD, _counter_rows(counters))):
        if not rows:
            continue
        lines.append("")
        for r in rows:
            lines.append(r[0])
            lines += [f"  {h}: {v}" for h, v in zip(head[1:], r[1:]) if h and v]
            if r[-1]:
                lines.append(f"  Error: {r[-1]}")
    return "\n".join(lines) + "\n"


def html_body(fig: ReportFigures, tz_mode: str) -> str:
    e = html.escape
    p = fig.period
    cell = 'style="padding:4px 10px;border-bottom:1px solid #ddd;text-align:right;white-space:nowrap"'
    first = 'style="padding:4px 10px;border-bottom:1px solid #ddd;text-align:left"'
    th = 'style="padding:4px 10px;border-bottom:2px solid #999;text-align:right;color:#555;font-weight:600"'
    th1 = 'style="padding:4px 10px;border-bottom:2px solid #999;text-align:left;color:#555;font-weight:600"'

    def table(head: list[str], rows: list[list[str]]) -> str:
        has_err = any(r[-1] for r in rows)
        heads = head if has_err else head[:-1]
        out = ['<table style="border-collapse:collapse;font-family:Arial,sans-serif;font-size:13px;margin:12px 0">',
               "<tr>" + "".join(f"<th {th1 if i == 0 else th}>{e(h)}</th>" for i, h in enumerate(heads)) + "</tr>"]
        for r in rows:
            cells = r if has_err else r[:-1]
            out.append("<tr>" + "".join(
                f"<td {first if i == 0 else cell}>{e(v)}</td>" for i, v in enumerate(cells)) + "</tr>")
        out.append("</table>")
        return "\n".join(out)

    gauges = [s for s in fig.signals if s.kind == "gauge"]
    counters = [s for s in fig.signals if s.kind == "counter"]
    parts = [f'<div style="font-family:Arial,sans-serif;font-size:14px;color:#222">',
             f"<h2 style=\"font-size:17px;margin:0 0 4px\">{e(subject(fig))}</h2>",
             f'<div style="color:#666;font-size:12px">Period {e(fmt_when(p.start, tz_mode))} – '
             f"{e(fmt_when(p.end, tz_mode))} ({'UTC' if tz_mode == 'utc' else 'local time'})"
             + (f" · InfluxDB {e(fig.source)}" if fig.source else "") + "</div>"]
    if fig.error:
        parts.append(f'<p style="color:#b00">Error: {e(fig.error)}</p>')
    if gauges:
        rows = _gauge_rows(gauges, tz_mode)
        head = GAUGE_HEAD if any(s.energy is not None for s in gauges) else GAUGE_HEAD[:5] + GAUGE_HEAD[7:]
        if head is not GAUGE_HEAD:
            rows = [r[:5] + r[7:] for r in rows]
        parts.append(table(head, rows))
    if counters:
        parts.append(table(COUNTER_HEAD, _counter_rows(counters)))
    parts.append('<div style="color:#999;font-size:11px">"vs. previous": difference to the period before. '
                 "Mean and integral are time-weighted: a value holds until the next one.</div></div>")
    return "\n".join(parts)
