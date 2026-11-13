"""Small helpers for building Flux queries."""
from __future__ import annotations

import re
from datetime import datetime

FLUX_TIME = "%Y-%m-%dT%H:%M:%SZ"


def flux_str(value: str) -> str:
    """A Flux string literal."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def flux_time(t: datetime) -> str:
    """Aware datetime → Flux time literal (UTC)."""
    from datetime import timezone
    return t.astimezone(timezone.utc).strftime(FLUX_TIME)


def field_filter(field: str, type_suffix: bool) -> str:
    """Filter expression for a field; with type_suffix the field was written as
    e.g. value_float / value_bool, so any of the suffixes is accepted."""
    if type_suffix:
        pattern = re.escape(field).replace("/", "\\/")
        return f"r._field =~ /^{pattern}(_float|_integer|_bool)?$/"
    return f"r._field == {flux_str(field)}"


def signal_source(bucket: str, measurement: str, field: str, type_suffix: bool,
                  start: datetime, stop: datetime) -> str:
    """One signal's values in [start, stop) as floats, all tag sets merged, sorted."""
    return (
        f"from(bucket: {flux_str(bucket)})\n"
        f"  |> range(start: {flux_time(start)}, stop: {flux_time(stop)})\n"
        f"  |> filter(fn: (r) => r._measurement == {flux_str(measurement)} and {field_filter(field, type_suffix)})\n"
        f"  |> toFloat()\n"
        f"  |> group()\n"
        f'  |> sort(columns: ["_time"])\n'
    )
