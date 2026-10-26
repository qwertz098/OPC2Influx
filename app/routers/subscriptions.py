from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from openpyxl import Workbook
from openpyxl.styles import Font

from app.config import save_subscriptions, SubTagActivation, SubscriptionConfig, apply_name_strips
from app.dependencies import get_state
from app.state import RuntimeState

router = APIRouter(prefix="/subscriptions")
templates = Jinja2Templates(directory="templates")


@dataclass
class StaleEntry:
    sub: SubscriptionConfig
    reason: str


@router.get("", response_class=HTMLResponse)
async def list_subscriptions(request: Request, state: RuntimeState = Depends(get_state)):
    subs = state.config.subscriptions.subscriptions
    all_tags = {t.id: t for t in state.config.tags.tags}
    tag_cache = state.polling_engine._tag_cache if state.polling_engine else {}
    folders = state.config.dynamic_folders.folders
    server_map = {s.id: s.display_name for s in state.config.servers.servers}
    managed_counts = {
        f.id: sum(1 for s in subs if s.source_folder_id == f.id)
        for f in folders
    }
    folder_map = {f.id: f for f in folders}
    strip_prefixes = {s.id: s.name_strip_prefixes for s in state.config.servers.servers}
    stripped_measurements = {
        s.id: apply_name_strips(s.influxdb.measurement, strip_prefixes.get(s.server_id, []))
        for s in subs
    }
    return templates.TemplateResponse(request, "subscriptions.html", {
        "request": request,
        "subscriptions": subs,
        "all_tags": all_tags,
        "tag_cache": tag_cache,
        "folders": folders,
        "server_map": server_map,
        "managed_counts": managed_counts,
        "folder_map": folder_map,
        "stripped_measurements": stripped_measurements,
        "server_status": state.server_status,
    })


@router.get("/export")
async def export_subscriptions(state: RuntimeState = Depends(get_state)):
    """Export the full subscription list as an .xlsx file."""
    subs = state.config.subscriptions.subscriptions
    server_map = {s.id: s.display_name for s in state.config.servers.servers}
    folder_map = {f.id: f for f in state.config.dynamic_folders.folders}
    all_tags = {t.id: t for t in state.config.tags.tags}
    tag_cache = state.polling_engine._tag_cache if state.polling_engine else {}

    wb = Workbook()
    ws = wb.active
    ws.title = "Subscriptions"

    strip_prefixes = {s.id: s.name_strip_prefixes for s in state.config.servers.servers}

    headers = [
        "Name", "Server", "Node ID", "Browse Path", "Measurement", "Measurement (no prefix)", "Field Name",
        "Interval (ms)", "Write Mode", "Threshold", "Heartbeat (s)",
        "Tags", "Enabled", "Dynamic Folder",
    ]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    ws.freeze_panes = "A2"

    for sub in subs:
        tag_parts = []
        for activation in sub.active_tags:
            if not activation.active:
                continue
            tag = all_tags.get(activation.tag_id)
            if not tag:
                continue
            value = tag_cache.get(sub.id, {}).get(tag.id, "") if tag.is_dynamic else tag.static_value
            tag_parts.append(f"{tag.key}={value}")
        folder = folder_map.get(sub.source_folder_id) if sub.source_folder_id else None
        stripped = apply_name_strips(sub.influxdb.measurement, strip_prefixes.get(sub.server_id, []))
        ws.append([
            sub.display_name or sub.node_id,
            server_map.get(sub.server_id, sub.server_id),
            sub.node_id,
            sub.browse_path,
            sub.influxdb.measurement,
            stripped,
            sub.influxdb.field_name,
            sub.polling.interval_ms,
            sub.polling.write_mode,
            sub.polling.threshold,
            sub.polling.heartbeat_interval_seconds,
            "; ".join(tag_parts),
            "Yes" if sub.enabled else "No",
            folder.display_name if folder else "",
        ])

    for col_cells in ws.columns:
        length = max((len(str(c.value)) if c.value is not None else 0) for c in col_cells)
        ws.column_dimensions[col_cells[0].column_letter].width = min(max(length + 2, 10), 60)

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)

    from app.main import effective_site_name
    site_name = effective_site_name(state.config.app_settings.site_name)
    site_slug = re.sub(r'[^A-Za-z0-9]+', "_", site_name).strip("_")
    timestamp = datetime.now().strftime("%y-%m-%d_%H-%M")
    parts = ["subscriptions"] + ([site_slug] if site_slug else []) + [timestamp]
    filename = "_".join(parts) + ".xlsx"

    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{sub_id}/edit", response_class=HTMLResponse)
async def edit_subscription(sub_id: str, request: Request, state: RuntimeState = Depends(get_state)):
    sub = next((s for s in state.config.subscriptions.subscriptions if s.id == sub_id), None)
    if sub is None:
        return HTMLResponse("Not found", status_code=404)
    return templates.TemplateResponse(request, "partials/subscription_edit_form.html", {
        "request": request,
        "sub": sub,
    })


@router.post("/{sub_id}/update", response_class=HTMLResponse)
async def update_subscription(sub_id: str, request: Request, state: RuntimeState = Depends(get_state)):
    form = await request.form()
    for sub in state.config.subscriptions.subscriptions:
        if sub.id == sub_id:
            sub.display_name = form.get("display_name", sub.display_name)
            sub.influxdb.measurement = form.get("measurement", sub.influxdb.measurement)
            sub.influxdb.field_name = form.get("field_name", sub.influxdb.field_name)
            sub.polling.interval_ms = int(form.get("interval_ms", sub.polling.interval_ms))
            sub.polling.write_mode = form.get("write_mode", sub.polling.write_mode)
            sub.polling.threshold = float(form.get("threshold", sub.polling.threshold))
            sub.polling.heartbeat_interval_seconds = int(
                form.get("heartbeat_interval_seconds", sub.polling.heartbeat_interval_seconds)
            )
            break
    save_subscriptions(state.config.subscriptions, state.config_dir)
    if state.polling_engine:
        await state.polling_engine.reconcile()
    sub = next((s for s in state.config.subscriptions.subscriptions if s.id == sub_id), None)
    all_tags = {t.id: t for t in state.config.tags.tags}
    tag_cache = state.polling_engine._tag_cache if state.polling_engine else {}
    srv = next((s for s in state.config.servers.servers if s.id == sub.server_id), None) if sub else None
    stripped_measurement = apply_name_strips(sub.influxdb.measurement, srv.name_strip_prefixes if srv else []) if sub else ""
    return templates.TemplateResponse(request, "partials/subscription_row.html", {
        "request": request,
        "sub": sub,
        "all_tags": all_tags,
        "tag_cache": tag_cache,
        "stripped_measurement": stripped_measurement,
    })


@router.post("/{sub_id}/toggle")
async def toggle_subscription(sub_id: str, state: RuntimeState = Depends(get_state)):
    for sub in state.config.subscriptions.subscriptions:
        if sub.id == sub_id:
            sub.enabled = not sub.enabled
            break
    save_subscriptions(state.config.subscriptions, state.config_dir)
    if state.polling_engine:
        await state.polling_engine.reconcile()
    return RedirectResponse("/subscriptions", status_code=303)


@router.post("/{sub_id}/toggle-tag/{tag_id}", response_class=HTMLResponse)
async def toggle_tag(sub_id: str, tag_id: str, request: Request, state: RuntimeState = Depends(get_state)):
    """Toggle a central tag's active state on a subscription. Returns updated tag badges HTML."""
    for sub in state.config.subscriptions.subscriptions:
        if sub.id == sub_id:
            existing = next((a for a in sub.active_tags if a.tag_id == tag_id), None)
            if existing:
                existing.active = not existing.active
            else:
                sub.active_tags.append(SubTagActivation(tag_id=tag_id, active=True))
            break
    save_subscriptions(state.config.subscriptions, state.config_dir)
    # Clear tag cache so changes take effect on next poll
    if state.polling_engine:
        state.polling_engine._tag_cache.pop(sub_id, None)
    sub = next((s for s in state.config.subscriptions.subscriptions if s.id == sub_id), None)
    all_tags = {t.id: t for t in state.config.tags.tags}
    tag_cache = state.polling_engine._tag_cache if state.polling_engine else {}
    return templates.TemplateResponse(request, "partials/subscription_tag_badges.html", {
        "request": request,
        "sub": sub,
        "all_tags": all_tags,
        "tag_cache": tag_cache,
    })


@router.get("/stale", response_class=HTMLResponse)
async def check_stale(request: Request, state: RuntimeState = Depends(get_state)):
    """Check every subscription's node against its server; return a summary fragment."""
    configured_server_ids = {s.id for s in state.config.servers.servers}
    mgr = state.opcua_manager

    stale: list[StaleEntry] = []
    skipped: list[SubscriptionConfig] = []  # server exists but is not connected

    for sub in state.config.subscriptions.subscriptions:
        if sub.server_id not in configured_server_ids:
            stale.append(StaleEntry(sub=sub, reason="Server no longer configured"))
            continue
        if mgr is None or not mgr.is_connected(sub.server_id):
            skipped.append(sub)
            continue
        try:
            await mgr.read_node_attributes(sub.server_id, sub.node_id)
        except Exception:
            stale.append(StaleEntry(sub=sub, reason="Node not found on server"))
            continue
        # The node id still exists — but node ids can be reused/reassigned when a
        # server's address space is restructured. Verify it still sits at the stored
        # browse path; if not, the original node is gone.
        if sub.browse_path:
            try:
                actual_path = await mgr.get_node_browse_path(sub.server_id, sub.node_id)
            except Exception:
                actual_path = None
            if actual_path is not None and actual_path != sub.browse_path:
                stale.append(StaleEntry(
                    sub=sub,
                    reason=f"Node moved/reassigned (now at {actual_path})",
                ))

    server_name_map = {s.id: s.display_name for s in state.config.servers.servers}
    return templates.TemplateResponse(request, "partials/stale_subscriptions.html", {
        "request": request,
        "stale": stale,
        "skipped": skipped,
        "server_name_map": server_name_map,
        "total_checked": len(state.config.subscriptions.subscriptions) - len(skipped),
    })


@router.post("/delete-stale")
async def delete_stale(request: Request, state: RuntimeState = Depends(get_state)):
    form = await request.form()
    ids_to_delete = set(form.getlist("sub_id"))
    state.config.subscriptions.subscriptions = [
        s for s in state.config.subscriptions.subscriptions
        if s.id not in ids_to_delete
    ]
    save_subscriptions(state.config.subscriptions, state.config_dir)
    if state.polling_engine:
        await state.polling_engine.reconcile()
    return RedirectResponse("/subscriptions", status_code=303)


@router.post("/delete-all-dynamic")
async def delete_all_dynamic(state: RuntimeState = Depends(get_state)):
    """Delete every subscription managed by a dynamic folder watcher.

    Note: enabled watchers will re-create these on their next rescan — the
    UI confirmation points the user at removing/disabling watchers first.
    """
    state.config.subscriptions.subscriptions = [
        s for s in state.config.subscriptions.subscriptions if not s.source_folder_id
    ]
    save_subscriptions(state.config.subscriptions, state.config_dir)
    if state.polling_engine:
        await state.polling_engine.reconcile()
    return RedirectResponse("/subscriptions", status_code=303)


@router.post("/delete-all-manual")
async def delete_all_manual(state: RuntimeState = Depends(get_state)):
    """Delete every manually created subscription (keeps watcher-managed ones)."""
    state.config.subscriptions.subscriptions = [
        s for s in state.config.subscriptions.subscriptions if s.source_folder_id
    ]
    save_subscriptions(state.config.subscriptions, state.config_dir)
    if state.polling_engine:
        await state.polling_engine.reconcile()
    return RedirectResponse("/subscriptions", status_code=303)


@router.post("/{sub_id}/delete")
async def delete_subscription(sub_id: str, state: RuntimeState = Depends(get_state)):
    state.config.subscriptions.subscriptions = [
        s for s in state.config.subscriptions.subscriptions if s.id != sub_id
    ]
    save_subscriptions(state.config.subscriptions, state.config_dir)
    if state.polling_engine:
        await state.polling_engine.reconcile()
    return RedirectResponse("/subscriptions", status_code=303)
