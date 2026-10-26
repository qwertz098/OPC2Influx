from __future__ import annotations
import io
import zipfile
from pathlib import Path
from fastapi import APIRouter, Depends, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from app.dependencies import get_state
from app.state import RuntimeState

router = APIRouter(prefix="/config")
templates = Jinja2Templates(directory="templates")

CONFIG_FILES = [
    "servers.yaml", "subscriptions.yaml", "influxdb.yaml",
    "alerts.yaml", "sqlite_targets.yaml", "tags.yaml",
    "alarm_rules.yaml", "stale_watchdog.yaml", "saved_charts.yaml", "dynamic_folders.yaml",
    "app_settings.yaml",
]


@router.get("/backup")
async def download_backup(state: RuntimeState = Depends(get_state)):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname in CONFIG_FILES:
            fpath = Path(state.config_dir) / fname
            if fpath.exists():
                zf.write(fpath, fname)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=opcua2influxdb_config.zip"},
    )


@router.get("/backup/page", response_class=HTMLResponse)
async def backup_page(request: Request, state: RuntimeState = Depends(get_state)):
    return templates.TemplateResponse(request, "config/backup.html", {"request": request})


@router.post("/restore")
async def restore_backup(
    request: Request,
    state: RuntimeState = Depends(get_state),
    file: UploadFile = File(...),
):
    content = await file.read()
    buf = io.BytesIO(content)
    config_dir = Path(state.config_dir)
    with zipfile.ZipFile(buf, "r") as zf:
        for name in zf.namelist():
            if name.endswith(".yaml") and "/" not in name:
                zf.extract(name, config_dir)
    # Reload config
    from app.config import load_config
    state.config = load_config(config_dir)
    if state.opcua_manager:
        await state.opcua_manager.reconcile()
    if state.polling_engine:
        await state.polling_engine.reconcile()
    if state.influx_pool:
        await state.influx_pool.reconcile()
    if state.sqlite_writer_pool:
        await state.sqlite_writer_pool.reconcile()
    return RedirectResponse("/config/backup/page", status_code=303)
