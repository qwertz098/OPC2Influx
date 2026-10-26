from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import (
    save_app_settings, save_alerts, AlertsConfig, SmtpConfig, DYNAMIC_FOLDER_TYPE_CHOICES,
)
from app.dependencies import get_state
from app.state import RuntimeState

router = APIRouter(prefix="/config")
templates = Jinja2Templates(directory="templates")


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, state: RuntimeState = Depends(get_state)):
    import os
    from app.main import smtp_env_overrides
    return templates.TemplateResponse(request, "config/settings.html", {
        "request": request,
        "cfg": state.config.app_settings,
        "alerts": state.config.alerts,
        "site_name_env": os.environ.get("SITE_NAME", "").strip(),
        "smtp_env": smtp_env_overrides(),
        "dynamic_folder_type_choices": DYNAMIC_FOLDER_TYPE_CHOICES,
    })


@router.post("/settings")
async def save_settings(request: Request, state: RuntimeState = Depends(get_state)):
    import os
    from app.main import effective_site_name, update_site_name
    form = await request.form()
    new_name = form.get("site_name", "").strip()
    state.config.app_settings.site_name = new_name
    save_app_settings(state.config.app_settings, state.config_dir)
    resolved = effective_site_name(new_name)
    update_site_name(resolved)
    if state.alert_manager:
        state.alert_manager._site_name = resolved
    return RedirectResponse("/config/settings", status_code=303)


@router.post("/settings/dynamic-folder-types", response_class=HTMLResponse)
async def save_dynamic_folder_types(request: Request, state: RuntimeState = Depends(get_state)):
    form = await request.form()
    selected = [t for t in DYNAMIC_FOLDER_TYPE_CHOICES if form.get(f"dtype_{t}") == "1"]
    state.config.app_settings.dynamic_folder_allowed_types = selected
    save_app_settings(state.config.app_settings, state.config_dir)
    return HTMLResponse('<span class="badge badge-ok save-confirm">✓ Saved</span>')


@router.post("/settings/smtp", response_class=HTMLResponse)
async def save_smtp(request: Request, state: RuntimeState = Depends(get_state)):
    form = await request.form()
    to_list = [e.strip() for e in form.get("to_addresses", "").split("\n") if e.strip()]
    state.config.alerts = AlertsConfig(
        watchdog=state.config.alerts.watchdog,
        smtp=SmtpConfig(
            enabled=form.get("smtp_enabled", "false") == "true",
            host=form.get("smtp_host", ""),
            port=int(form.get("smtp_port", "587")),
            tls=form.get("smtp_tls", "false") == "true",
            username=form.get("smtp_username", ""),
            password=form.get("smtp_password", ""),
            from_address=form.get("smtp_from", ""),
            to_addresses=to_list,
        ),
    )
    save_alerts(state.config.alerts, state.config_dir)
    from app.main import apply_smtp_env
    apply_smtp_env(state.config.alerts.smtp)
    if state.alert_manager:
        state.alert_manager.update_config(state.config.alerts.smtp)
    return HTMLResponse('<span class="badge badge-ok save-confirm">✓ Saved</span>')
