"""Bucket retention / shard-group-duration management.

The write path uses InfluxDBClientAsync, which has no bucket-management API.
Bucket CRUD only exists on the synchronous InfluxDBClient, so these rare admin
calls are wrapped in asyncio.to_thread() to avoid blocking the event loop.
"""
from __future__ import annotations

import asyncio
import json

from influxdb_client import InfluxDBClient
from influxdb_client.domain.bucket_retention_rules import BucketRetentionRules
from influxdb_client.rest import ApiException

from app.config import InfluxTargetConfig, resolve_env_strict


def _clean_api_error(exc: ApiException) -> str:
    """Extract the JSON body's message from an InfluxDB ApiException, falling back
    to the raw body/reason if it isn't JSON. str(exc) alone only prints the HTTP
    headers, not the actual rejection reason InfluxDB sent in the response body."""
    body = exc.body or ""
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    message = body
    try:
        parsed = json.loads(body)
        message = parsed.get("message") or parsed.get("error") or body
    except Exception:
        pass
    return f"InfluxDB rejected the request ({exc.status} {exc.reason}): {message}"


def _sync_get_retention(cfg: InfluxTargetConfig) -> dict:
    with InfluxDBClient(url=cfg.url, token=resolve_env_strict(cfg.token), org=cfg.org) as client:
        try:
            bucket = client.buckets_api().find_bucket_by_name(cfg.bucket)
        except ApiException as exc:
            raise ValueError(_clean_api_error(exc)) from exc
        if bucket is None:
            raise ValueError(f"Bucket '{cfg.bucket}' not found")
        rules = bucket.retention_rules[0] if bucket.retention_rules else None
        return {
            "every_seconds": rules.every_seconds if rules else 0,
            "shard_group_duration_seconds": (
                getattr(rules, "shard_group_duration_seconds", None) if rules else None
            ),
        }


async def get_bucket_retention(cfg: InfluxTargetConfig) -> dict:
    return await asyncio.to_thread(_sync_get_retention, cfg)


def _sync_set_retention(
    cfg: InfluxTargetConfig, every_seconds: int, shard_group_seconds: int | None
) -> None:
    with InfluxDBClient(url=cfg.url, token=resolve_env_strict(cfg.token), org=cfg.org) as client:
        api = client.buckets_api()
        try:
            bucket = api.find_bucket_by_name(cfg.bucket)
        except ApiException as exc:
            raise ValueError(_clean_api_error(exc)) from exc
        if bucket is None:
            raise ValueError(f"Bucket '{cfg.bucket}' not found")
        rule = BucketRetentionRules(
            type="expire",
            every_seconds=every_seconds,
            shard_group_duration_seconds=shard_group_seconds,  # None = let InfluxDB auto-calc
        )
        bucket.retention_rules = [rule]
        try:
            api.update_bucket(bucket=bucket)
        except ApiException as exc:
            raise ValueError(_clean_api_error(exc)) from exc


async def set_bucket_retention(
    cfg: InfluxTargetConfig, every_seconds: int, shard_group_seconds: int | None
) -> None:
    await asyncio.to_thread(_sync_set_retention, cfg, every_seconds, shard_group_seconds)
