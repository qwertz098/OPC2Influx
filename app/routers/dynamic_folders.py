"""Dynamic folder watcher actions (interval/scan/toggle/delete).

The watchers are managed inline on the /subscriptions page — there is no
standalone page anymore; the old URL just redirects there.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from app.config import save_dynamic_folders, save_subscriptions
from app.dependencies import get_state
from app.state import RuntimeState

router = APIRouter(prefix="/config/dynamic-folders")


@router.get("")
async def list_dynamic_folders():
    return RedirectResponse("/subscriptions", status_code=302)


@router.post("/{folder_id}/toggle")
async def toggle_folder(folder_id: str, state: RuntimeState = Depends(get_state)):
    for f in state.config.dynamic_folders.folders:
        if f.id == folder_id:
            f.enabled = not f.enabled
            break
    save_dynamic_folders(state.config.dynamic_folders, state.config_dir)
    return RedirectResponse("/subscriptions", status_code=303)


@router.post("/{folder_id}/interval")
async def update_interval(folder_id: str, request: Request, state: RuntimeState = Depends(get_state)):
    form = await request.form()
    seconds = int(form.get("rescan_interval_seconds", 300))
    for f in state.config.dynamic_folders.folders:
        if f.id == folder_id:
            f.rescan_interval_seconds = max(15, seconds)
            break
    save_dynamic_folders(state.config.dynamic_folders, state.config_dir)
    return RedirectResponse("/subscriptions", status_code=303)


@router.post("/{folder_id}/scan")
async def scan_folder_now(folder_id: str, state: RuntimeState = Depends(get_state)):
    """Trigger an immediate rescan of one watcher, bypassing its rescan_interval_seconds."""
    scanner = getattr(state, "folder_scanner", None)
    if scanner:
        await scanner.scan_now(folder_id)
    return RedirectResponse("/subscriptions", status_code=303)


@router.post("/{folder_id}/delete")
async def delete_folder(folder_id: str, request: Request, state: RuntimeState = Depends(get_state)):
    form = await request.form()
    delete_subs = form.get("delete_subs") == "1"
    if delete_subs:
        state.config.subscriptions.subscriptions = [
            s for s in state.config.subscriptions.subscriptions
            if s.source_folder_id != folder_id
        ]
        save_subscriptions(state.config.subscriptions, state.config_dir)
        if state.polling_engine:
            await state.polling_engine.reconcile()
    state.config.dynamic_folders.folders = [
        f for f in state.config.dynamic_folders.folders if f.id != folder_id
    ]
    save_dynamic_folders(state.config.dynamic_folders, state.config_dir)
    return RedirectResponse("/subscriptions", status_code=303)
