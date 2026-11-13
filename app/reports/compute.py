"""
Report figures from InfluxDB.

gauge   – time-weighted mean, min and max (with time) of the period, the mean
          of the period before, and optionally energy = value × hours.
          The values are written on change, so a value holds until the next
          one: the signal is resampled to one value per minute, gaps are
          filled with the previous value, and mean / energy are taken from
          that series.
counter – meter reading at the end of the period, of the period before and
          of the one before that: the change since the last report and the
          change in the period before, to compare the two.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from app.config import ReportConfig, ReportSignalConfig, apply_name_strips
from app.influxdb.flux import signal_source
from app.reports.periods import Period

logger = logging.getLogger(__name__)

COUNTER_LOOKBACK = timedelta(days=45)   # how far back to look for the last value before a time


@dataclass
class SignalFigures:
    label: str
    kind: str
    unit: str = ""
    # gauge
    mean: Optional[float] = None
    prev_mean: Optional[float] = None
    min: Optional[float] = None
    min_time: Optional[datetime] = None
    max: Optional[float] = None
    max_time: Optional[datetime] = None
    energy: Optional[float] = None
    prev_energy: Optional[float] = None
    energy_unit: str = ""
    # counter
    reading: Optional[float] = None
    prev_reading: Optional[float] = None
    prev2_reading: Optional[float] = None
    error: str = ""

    @property
    def counter_change(self) -> Optional[float]:
        """Change since the last report (this period)."""
        if self.reading is None or self.prev_reading is None:
            return None
        return self.reading - self.prev_reading

    @property
    def prev_counter_change(self) -> Optional[float]:
        """Change in the period before, to compare with."""
        if self.prev_reading is None or self.prev2_reading is None:
            return None
        return self.prev_reading - self.prev2_reading


@dataclass
class ReportFigures:
    report: ReportConfig
    period: Period
    signals: list[SignalFigures] = field(default_factory=list)
    source: str = ""
    error: str = ""


def gauge_flux(src_period: str, src_prev: str, seed_period: str, seed_prev: str, energy: bool) -> str:
    """Flux for one gauge signal; src_* / seed_* are signal_source() expressions.

    The signal is resampled to one value per minute and gaps are filled with
    the previous value. Minutes before the first value of a period take the
    last value before the period (seed_*), so a value that has not changed for
    a long time still counts.
    """
    def minutes(src: str, seed: str, name: str) -> str:
        return (
            f'{name}s = {seed} |> last() |> findColumn(fn: (key) => true, column: "_value")\n'
            f"{name}m = {src} |> aggregateWindow(every: 1m, fn: mean, createEmpty: true)\n"
            f"  |> fill(usePrevious: true)\n"
            f"  |> map(fn: (r) => ({{r with _value: if exists r._value then r._value\n"
            f"                      else if length(arr: {name}s) > 0 then {name}s[0] else r._value}}))\n"
        )

    parts = [
        # Flux reads a line starting with "(" as a call on the line before:
        # every source gets a name first
        f"cur = {src_period}",
        f"prev = {src_prev}",
        f"seedc = {seed_period}",
        f"seedp = {seed_prev}",
        minutes("cur", "seedc", "cur"),
        minutes("prev", "seedp", "prev"),
        'curm |> mean() |> set(key: "f", value: "mean") |> yield(name: "mean")',
        'prevm |> mean() |> set(key: "f", value: "prev_mean") |> yield(name: "prev_mean")',
        # the value before the period: used when the period has no value at all
        'seedc |> last() |> set(key: "f", value: "seed") |> yield(name: "seed")',
        'seedp |> last() |> set(key: "f", value: "prev_seed") |> yield(name: "prev_seed")',
        'cur |> min() |> set(key: "f", value: "min") |> yield(name: "min")',
        'cur |> max() |> set(key: "f", value: "max") |> yield(name: "max")',
    ]
    if energy:
        # one value per minute: sum / 60 = value × hours
        parts += [
            'curm |> sum() |> map(fn: (r) => ({r with _value: r._value / 60.0})) '
            '|> set(key: "f", value: "energy") |> yield(name: "energy")',
            'prevm |> sum() |> map(fn: (r) => ({r with _value: r._value / 60.0})) '
            '|> set(key: "f", value: "prev_energy") |> yield(name: "prev_energy")',
        ]
    return "\n".join(parts) + "\n"


def counter_flux(src_end: str, src_prev_end: str, src_prev2_end: str) -> str:
    return (
        f"cur = {src_end}\nprev = {src_prev_end}\nprev2 = {src_prev2_end}\n"
        'cur |> last() |> set(key: "f", value: "reading") |> yield(name: "reading")\n'
        'prev |> last() |> set(key: "f", value: "prev_reading") |> yield(name: "prev_reading")\n'
        'prev2 |> last() |> set(key: "f", value: "prev2_reading") |> yield(name: "prev2_reading")\n'
    )


async def compute(state, report: ReportConfig, period: Period) -> ReportFigures:
    """Query the figures of every signal of ``report`` for ``period``."""
    from influxdb_client.client.influxdb_client_async import InfluxDBClientAsync
    from app.config import UnresolvedEnvVarError, resolve_env, resolve_env_strict

    result = ReportFigures(report=report, period=period)
    targets = sorted((t for t in state.config.influxdb.targets if t.enabled), key=lambda t: t.priority)
    if not targets:
        result.error = "No InfluxDB target configured"
        return result
    target = targets[0]
    result.source = target.display_name
    try:
        token = resolve_env_strict(target.token)
    except UnresolvedEnvVarError:
        token = resolve_env(target.token)

    subs = {s.id: s for s in state.config.subscriptions.subscriptions}
    strips = {s.id: s.name_strip_prefixes for s in state.config.servers.servers}

    async with InfluxDBClientAsync(url=target.url, token=token, org=target.org, timeout=60_000) as client:
        query_api = client.query_api()
        for sig in report.signals:
            sub = subs.get(sig.subscription_id)
            fig = SignalFigures(label=sig.subscription_id, kind=sig.kind, unit=sig.unit,
                                energy_unit=sig.energy_unit)
            result.signals.append(fig)
            if sub is None:
                fig.error = "subscription not found"
                continue
            measurement = apply_name_strips(sub.influxdb.measurement, strips.get(sub.server_id, []))
            fig.label = sub.display_name or measurement
            src = lambda a, b: signal_source(target.bucket, measurement, sub.influxdb.field_name,  # noqa: E731
                                             target.type_suffix, a, b)
            if sig.kind == "counter":
                flux = counter_flux(src(period.end - COUNTER_LOOKBACK, period.end),
                                    src(period.start - COUNTER_LOOKBACK, period.start),
                                    src(period.prev_start - COUNTER_LOOKBACK, period.prev_start))
            else:
                flux = gauge_flux(src(period.start, period.end), src(period.prev_start, period.start),
                                  src(period.start - COUNTER_LOOKBACK, period.start),
                                  src(period.prev_start - COUNTER_LOOKBACK, period.prev_start), sig.energy)
            try:
                values: dict[str, tuple[float, datetime]] = {}
                for table in await query_api.query(flux, org=target.org):
                    for rec in table.records:
                        if rec.get_value() is not None:
                            values[rec.values.get("f")] = (float(rec.get_value()), rec.values.get("_time"))
            except Exception as exc:
                fig.error = str(exc)[:200]
                logger.warning("Report query failed for %s: %s", measurement, exc)
                continue
            _apply(fig, sig, values, (period.end - period.start).total_seconds() / 3600,
                   (period.start - period.prev_start).total_seconds() / 3600)
    return result


def _apply(fig: SignalFigures, sig: ReportSignalConfig, values: dict,
           hours: float = 0.0, prev_hours: float = 0.0) -> None:
    get = lambda k: values[k][0] if k in values else None  # noqa: E731
    if sig.kind == "counter":
        fig.reading, fig.prev_reading, fig.prev2_reading = get("reading"), get("prev_reading"), get("prev2_reading")
        return
    fig.mean, fig.prev_mean = get("mean"), get("prev_mean")
    e, pe = get("energy"), get("prev_energy")
    # No value inside a period: the last value before it held the whole time
    if fig.mean is None and get("seed") is not None:
        fig.mean, e = get("seed"), get("seed") * hours
    if fig.prev_mean is None and get("prev_seed") is not None:
        fig.prev_mean, pe = get("prev_seed"), get("prev_seed") * prev_hours
    if "min" in values:
        fig.min, fig.min_time = values["min"]
    if "max" in values:
        fig.max, fig.max_time = values["max"]
    if sig.energy:
        fig.energy = e * sig.energy_factor if e is not None else None
        fig.prev_energy = pe * sig.energy_factor if pe is not None else None
