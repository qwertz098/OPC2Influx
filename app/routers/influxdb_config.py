from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import InfluxTargetConfig, BatchConfig, save_influxdb, resolve_env_strict
from app.dependencies import get_state
from app.influxdb.bucket_admin import get_bucket_retention, set_bucket_retention
from app.state import RuntimeState

router = APIRouter(prefix="/config/influxdb")
templates = Jinja2Templates(directory="templates")


@router.get("", response_class=HTMLResponse)
async def list_targets(request: Request, state: RuntimeState = Depends(get_state)):
    return templates.TemplateResponse(request, "config/influxdb.html", {
        "request": request,
        "targets": state.config.influxdb.targets,
        "influx_status": state.influx_status,
    })


@router.post("")
async def add_target(request: Request, state: RuntimeState = Depends(get_state)):
    form = await request.form()
    token_source = form.get("token_source", "direct")
    if token_source == "env":
        env_var = form.get("token_env_var", "").strip()
        token = f"${{{env_var}}}" if env_var else ""
    else:
        token = form.get("token", "")
    target = InfluxTargetConfig(
        display_name=form.get("display_name", "InfluxDB"),
        url=form.get("url", "http://localhost:8086"),
        token=token,
        org=form.get("org", "myorg"),
        bucket=form.get("bucket", "opcua"),
        mode=form.get("mode", "always"),
        priority=int(form.get("priority", "1")),
        enabled=True,
        type_suffix=form.get("type_suffix") == "1",
        batch=BatchConfig(
            size=int(form.get("batch_size", "500")),
            flush_interval_ms=int(form.get("flush_interval_ms", "1000")),
            max_retries=int(form.get("max_retries", "3")),
        ),
    )
    state.config.influxdb.targets.append(target)
    save_influxdb(state.config.influxdb, state.config_dir)
    if state.influx_pool:
        await state.influx_pool.reconcile()
    return RedirectResponse("/config/influxdb", status_code=303)


@router.post("/{target_id}/delete")
async def delete_target(target_id: str, state: RuntimeState = Depends(get_state)):
    state.config.influxdb.targets = [t for t in state.config.influxdb.targets if t.id != target_id]
    save_influxdb(state.config.influxdb, state.config_dir)
    if state.influx_pool:
        await state.influx_pool.reconcile()
    return RedirectResponse("/config/influxdb", status_code=303)


@router.get("/{target_id}/edit", response_class=HTMLResponse)
async def edit_target_form(
    request: Request, target_id: str, state: RuntimeState = Depends(get_state)
):
    target = next((t for t in state.config.influxdb.targets if t.id == target_id), None)
    if target is None:
        return HTMLResponse('<span class="badge badge-error">Not found</span>')
    return templates.TemplateResponse(request, "partials/influxdb_edit_form.html", {
        "request": request, "t": target,
    })


@router.post("/{target_id}/update")
async def update_target(
    request: Request, target_id: str, state: RuntimeState = Depends(get_state)
):
    form = await request.form()
    target = next((t for t in state.config.influxdb.targets if t.id == target_id), None)
    if target is None:
        return RedirectResponse("/config/influxdb", status_code=303)

    target.display_name = form.get("display_name", target.display_name)
    target.url = form.get("url", target.url)
    token_source = form.get("token_source", "direct")
    if token_source == "env":
        env_var = form.get("token_env_var", "").strip()
        if env_var:
            target.token = f"${{{env_var}}}"
    else:
        token = form.get("token", "")
        if token:  # only update token if a new one was entered
            target.token = token
    target.org = form.get("org", target.org)
    target.bucket = form.get("bucket", target.bucket)
    target.mode = form.get("mode", target.mode)
    target.priority = int(form.get("priority", target.priority))
    target.type_suffix = form.get("type_suffix") == "1"
    target.batch.size = int(form.get("batch_size", target.batch.size))
    target.batch.flush_interval_ms = int(form.get("flush_interval_ms", target.batch.flush_interval_ms))
    target.batch.max_retries = int(form.get("max_retries", target.batch.max_retries))

    save_influxdb(state.config.influxdb, state.config_dir)
    if state.influx_pool:
        await state.influx_pool.reconcile()
    return RedirectResponse("/config/influxdb", status_code=303)


@router.post("/{target_id}/toggle")
async def toggle_target(target_id: str, state: RuntimeState = Depends(get_state)):
    for t in state.config.influxdb.targets:
        if t.id == target_id:
            t.enabled = not t.enabled
            break
    save_influxdb(state.config.influxdb, state.config_dir)
    if state.influx_pool:
        await state.influx_pool.reconcile()
    return RedirectResponse("/config/influxdb", status_code=303)


@router.get("/{target_id}/test", response_class=HTMLResponse)
async def test_target(
    request: Request,
    target_id: str,
    state: RuntimeState = Depends(get_state),
):
    target = next((t for t in state.config.influxdb.targets if t.id == target_id), None)
    if target is None:
        return HTMLResponse('<span class="badge badge-error">Target not found</span>')

    import time
    from influxdb_client.client.influxdb_client_async import InfluxDBClientAsync
    t0 = time.monotonic()
    try:
        async with InfluxDBClientAsync(url=target.url, token=resolve_env_strict(target.token), org=target.org) as client:
            ready = await client.ping()
        latency_ms = int((time.monotonic() - t0) * 1000)
        result = "ok" if ready else "error"
        msg = f"Ping OK ({latency_ms} ms)" if ready else "Ping returned false"
    except Exception as exc:
        result = "error"
        msg = str(exc)

    return templates.TemplateResponse(request, "partials/test_result.html", {
        "request": request,
        "result": result,
        "message": msg,
    })


@router.get("/{target_id}/retention", response_class=HTMLResponse)
async def retention_form(
    request: Request, target_id: str, state: RuntimeState = Depends(get_state)
):
    target = next((t for t in state.config.influxdb.targets if t.id == target_id), None)
    if target is None:
        return HTMLResponse('<span class="badge badge-error">Not found</span>')
    error = None
    current = {"every_seconds": 0, "shard_group_duration_seconds": None}
    try:
        current = await get_bucket_retention(target)
    except Exception as exc:
        error = str(exc)
    return templates.TemplateResponse(request, "partials/influxdb_retention_form.html", {
        "request": request, "t": target, "current": current, "error": error,
    })


@router.post("/{target_id}/retention", response_class=HTMLResponse)
async def retention_update(
    request: Request, target_id: str, state: RuntimeState = Depends(get_state)
):
    form = await request.form()
    target = next((t for t in state.config.influxdb.targets if t.id == target_id), None)
    if target is None:
        return HTMLResponse('<span class="badge badge-error">Not found</span>')

    retention_days = float(form.get("retention_days", "30"))
    every_seconds = 0 if retention_days <= 0 else int(retention_days * 86400)

    shard_mode = form.get("shard_mode", "auto")
    shard_group_seconds = None
    if shard_mode == "manual":
        shard_group_hours = float(form.get("shard_group_hours", "24"))
        shard_group_seconds = int(shard_group_hours * 3600)

    try:
        await set_bucket_retention(target, every_seconds, shard_group_seconds)
        result, msg = "ok", "Retention updated"
    except Exception as exc:
        result, msg = "error", str(exc)

    return templates.TemplateResponse(request, "partials/test_result.html", {
        "request": request, "result": result, "message": msg,
    })


@router.post("/{target_id}/clear-conflict")
async def clear_conflict(
    target_id: str,
    conflict: str = Form(...),
    state: RuntimeState = Depends(get_state),
):
    """Remove a single resolved schema conflict entry from in-memory status."""
    status = state.influx_status.get(target_id)
    if status and conflict in status.schema_conflicts:
        status.schema_conflicts.remove(conflict)
    return RedirectResponse("/", status_code=303)
