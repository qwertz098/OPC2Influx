"""Real InfluxDB bucket size from /metrics."""
from __future__ import annotations

from collections import deque

from app.influxdb.size import BucketSizeMonitor, parse_metrics

METRICS = """\
# HELP storage_shard_disk_size Gauge of the disk size for the shard
# TYPE storage_shard_disk_size gauge
storage_shard_disk_size{bucket="aaa",engine="tsm1",id="1",path="/x/1",walPath="/w/1"} 1000
storage_shard_disk_size{bucket="aaa",engine="tsm1",id="2",path="/x/2",walPath="/w/2"} 2.5e+03
storage_shard_disk_size{bucket="bbb",engine="tsm1",id="3",path="/x/3",walPath="/w/3"} 7
storage_wal_size{bucket="aaa",engine="tsm1",id="1",path="/x/1",walPath="/w/1"} 500
storage_tsm_files_disk_bytes{bucket="aaa",engine="tsm1",id="1"} 500
storage_writer_ok_points{bucket="aaa"} 99
"""


def test_parse_metrics_sums_shards_per_bucket_wal_is_part_of_it():
    sizes = parse_metrics(METRICS)
    assert (sizes["aaa"].total, sizes["aaa"].wal) == (3500, 500)
    assert sizes["bbb"].total == 7


def test_growth_needs_span_and_ignores_shrinking():
    mon = BucketSizeMonitor()
    mon._history["t"] = deque([(0.0, 1000), (600.0, 2000)])
    assert mon.bytes_per_day("t") is None            # 10 min: too short
    mon._history["t"].append((3600.0, 4600))
    assert mon.bytes_per_day("t") == 86400           # 3600 B in 3600 s → 86 400 B/day
    mon._history["t"].append((7200.0, 900))          # compaction shrank the bucket
    assert mon.bytes_per_day("t") is None
