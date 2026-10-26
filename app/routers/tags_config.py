"""Central tag configuration — CRUD for CentralTagConfig."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import CentralTagConfig, save_tags, SubTagActivation
from app.dependencies import get_state
from app.state import RuntimeState

router = APIRouter(prefix="/config/tags")
templates = Jinja2Templates(directory="templates")


@router.get("", response_class=HTMLResponse)
async def list_tags(request: Request, state: RuntimeState = Depends(get_state)):
    return templates.TemplateResponse(request, "config/tags.html", {
        "request": request,
        "tags": state.config.tags.tags,
    })


@router.post("")
async def add_tag(request: Request, state: RuntimeState = Depends(get_state)):
    form = await request.form()
    is_dynamic = form.get("is_dynamic", "false") == "true"
    tag = CentralTagConfig(
        name=form.get("name", "").strip(),
        short_name=form.get("short_name", "").strip(),
        key=form.get("key", "").strip(),
        is_dynamic=is_dynamic,
        static_value=form.get("static_value", "").strip(),
        relative_path=form.get("relative_path", "").strip(),
        default_active=form.get("default_active", "false") == "true",
    )
    if not tag.name or not tag.key:
        return RedirectResponse("/config/tags", status_code=303)
    state.config.tags.tags.append(tag)
    # Populate default active_tags on all existing subscriptions if default_active
    if tag.default_active:
        for sub in state.config.subscriptions.subscriptions:
            if not any(a.tag_id == tag.id for a in sub.active_tags):
                sub.active_tags.append(SubTagActivation(tag_id=tag.id, active=True))
        from app.config import save_subscriptions
        save_subscriptions(state.config.subscriptions, state.config_dir)
    save_tags(state.config.tags, state.config_dir)
    return RedirectResponse("/config/tags", status_code=303)


@router.post("/{tag_id}/delete")
async def delete_tag(tag_id: str, state: RuntimeState = Depends(get_state)):
    state.config.tags.tags = [t for t in state.config.tags.tags if t.id != tag_id]
    # Remove activations from all subscriptions
    for sub in state.config.subscriptions.subscriptions:
        sub.active_tags = [a for a in sub.active_tags if a.tag_id != tag_id]
    save_tags(state.config.tags, state.config_dir)
    from app.config import save_subscriptions
    save_subscriptions(state.config.subscriptions, state.config_dir)
    return RedirectResponse("/config/tags", status_code=303)


@router.get("/{tag_id}/edit", response_class=HTMLResponse)
async def edit_tag(tag_id: str, request: Request, state: RuntimeState = Depends(get_state)):
    tag = next((t for t in state.config.tags.tags if t.id == tag_id), None)
    if not tag:
        return HTMLResponse("Not found", status_code=404)
    return templates.TemplateResponse(request, "partials/tag_edit_form.html", {
        "request": request,
        "tag": tag,
    })


@router.post("/{tag_id}/update")
async def update_tag(tag_id: str, request: Request, state: RuntimeState = Depends(get_state)):
    form = await request.form()
    for tag in state.config.tags.tags:
        if tag.id == tag_id:
            tag.name = form.get("name", tag.name).strip()
            tag.short_name = form.get("short_name", tag.short_name).strip()
            tag.key = form.get("key", tag.key).strip()
            tag.is_dynamic = form.get("is_dynamic", "false") == "true"
            tag.static_value = form.get("static_value", tag.static_value).strip()
            tag.relative_path = form.get("relative_path", tag.relative_path).strip()
            tag.default_active = form.get("default_active", "false") == "true"
            break
    # Clear dynamic tag cache so new path is resolved on next poll
    if state.polling_engine:
        state.polling_engine._tag_cache.clear()
    save_tags(state.config.tags, state.config_dir)
    return RedirectResponse("/config/tags", status_code=303)
