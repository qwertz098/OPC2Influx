"""Real size of an InfluxDB 2.x bucket, read from the server's Prometheus /metrics.

InfluxDB reports per shard and bucket id the shard's disk size
(storage_shard_disk_size: compacted TSM files plus write-ahead log) and, as part
of it, the not yet compacted write-ahead log (storage_wal_size). The bucket's
series index is not included; it is small next to the data. The bucket id is
looked up once via /api/v2/buckets.
Servers without /metrics (InfluxDB Cloud, --metrics-disabled, 1.x) yield None,
and callers fall back to the write-count estimate.
"""
from __future__ import annotations

import logging
import re
import time
from collections import deque
from dataclasses import dataclass

import aiohttp

from app.config import InfluxTargetConfig, UnresolvedEnvVarError, resolve_env, resolve_env_strict

logger = logging.getLogger(__name__)

_TTL_S = 60.0                 # the dashboard polls every 5 s; ask InfluxDB at most once a minute
_TIMEOUT = aiohttp.ClientTimeout(total=3)
_HISTORY_S = 26 * 3600        # samples kept for the growth rate
_MIN_GROWTH_SPAN_S = 15 * 60  # shorter spans give no meaningful per-day figure
_METRIC_RE = re.compile(r"^(storage_shard_disk_size|storage_wal_size)\{([^}]*)\}\s+(\S+)")
_LABEL_RE = re.compile(r'(\w+)="([^"]*)"')


@dataclass
class BucketSize:
    total: int  # all shards: TSM files + write-ahead log
    wal: int    # part of total still in the write-ahead log (not yet compacted)


def parse_metrics(text: str) -> dict[str, BucketSize]:
    """Sum shard sizes (and their WAL part) per bucket id from Prometheus text output."""
    sizes: dict[str, BucketSize] = {}
    for line in text.splitlines():
        m = _METRIC_RE.match(line)
        if not m:
            continue
        bucket = dict(_LABEL_RE.findall(m.group(2))).get("bucket")
        if not bucket:
            continue
        size = sizes.setdefault(bucket, BucketSize(0, 0))
        value = int(float(m.group(3)))
        if m.group(1) == "storage_shard_disk_size":
            size.total += value
        else:
            size.wal += value
    return sizes


def _token(target: InfluxTargetConfig) -> str:
    try:
        return resolve_env_strict(target.token)
    except UnresolvedEnvVarError:
        return resolve_env(target.token)


class BucketSizeMonitor:
    """Cached bucket sizes per InfluxDB target, plus a growth rate from past samples."""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, BucketSize | None]] = {}
        self._bucket_ids: dict[tuple[str, str, str], str] = {}  # (url, org, bucket) → id
        self._history: dict[str, deque[tuple[float, int]]] = {}

    async def size(self, target: InfluxTargetConfig) -> BucketSize | None:
        now = time.monotonic()
        cached = self._cache.get(target.id)
        if cached is not None and now - cached[0] < _TTL_S:
            return cached[1]
        size = None
        try:
            size = await self._fetch(target)
        except Exception as exc:
            logger.debug("InfluxDB size for %s unavailable: %s", target.display_name, exc)
        self._cache[target.id] = (now, size)
        if size is not None:
            hist = self._history.setdefault(target.id, deque())
            hist.append((now, size.total))
            while hist and now - hist[0][0] > _HISTORY_S:
                hist.popleft()
        return size

    def bytes_per_day(self, target_id: str) -> int | None:
        """Growth extrapolated from the oldest and newest sample (None if too short or shrinking)."""
        hist = self._history.get(target_id)
        if not hist or len(hist) < 2:
            return None
        (t0, s0), (t1, s1) = hist[0], hist[-1]
        if t1 - t0 < _MIN_GROWTH_SPAN_S or s1 <= s0:
            return None
        return int((s1 - s0) / (t1 - t0) * 86400)

    async def _fetch(self, target: InfluxTargetConfig) -> BucketSize | None:
        base = target.url.rstrip("/")
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            key = (base, target.org, target.bucket)
            bucket_id = self._bucket_ids.get(key)
            if bucket_id is None:
                async with session.get(
                    f"{base}/api/v2/buckets",
                    params={"name": target.bucket, "org": target.org},
                    headers={"Authorization": f"Token {_token(target)}"},
                ) as resp:
                    resp.raise_for_status()
                    buckets = (await resp.json()).get("buckets", [])
                if not buckets:
                    return None
                bucket_id = self._bucket_ids[key] = buckets[0]["id"]
            async with session.get(f"{base}/metrics") as resp:
                resp.raise_for_status()
                sizes = parse_metrics(await resp.text())
        return sizes.get(bucket_id, BucketSize(0, 0))


bucket_sizes = BucketSizeMonitor()
