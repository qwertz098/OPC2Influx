from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import unquote

from fastapi import APIRouter, Depends, Request, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import (
    DynamicFolderConfig, ServerPollingDefaults, SubscriptionConfig, SubscriptionInfluxConfig,
    PollingConfig, SubTagActivation, save_subscriptions, save_dynamic_folders,
)
from app.dependencies import get_state
from app.opcua.browser import get_children, find_siblings
from app.state import RuntimeState

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/browser")
templates = Jinja2Templates(directory="templates")


def _default_active_tags(state: "RuntimeState") -> list[SubTagActivation]:
    """Return SubTagActivation list for all default_active central tags."""
    return [
        SubTagActivation(tag_id=t.id, active=True)
        for t in state.config.tags.tags
        if t.default_active
    ]


def browse_path_to_measurement(browse_path: str) -> str:
    """'/Motors/Motor_0/Speed' → 'Motors.Motor_0.Speed'"""
    parts = [p for p in browse_path.split("/") if p]
    return ".".join(parts) if parts else "opcua_value"


@dataclass
class NodeCtx:
    node_id: str
    server_id: str
    browse_path: str
    display_name: str


def _subscribed_map(state: RuntimeState, server_id: str) -> dict[str, str]:
    return {
        s.node_id: s.id
        for s in state.config.subscriptions.subscriptions
        if s.server_id == server_id
    }


def _subscribed_ancestor_paths(state: RuntimeState, server_id: str) -> set[str]:
    """Browse paths of all folders that have at least one subscribed descendant."""
    result: set[str] = set()
    for s in state.config.subscriptions.subscriptions:
        if s.server_id == server_id and s.browse_path:
            parts = [p for p in s.browse_path.split("/") if p]
            for i in range(1, len(parts)):
                result.add("/" + "/".join(parts[:i]))
    return result


def _dynamic_folder_paths(state: RuntimeState, server_id: str) -> set[str]:
    """Browse paths of folders registered as dynamic (for badge rendering)."""
    return {
        f.folder_browse_path
        for f in state.config.dynamic_folders.folders
        if f.server_id == server_id and f.enabled
    }


def _dynamic_sub_node_ids(state: RuntimeState, server_id: str) -> set[str]:
    """Node ids of subscriptions managed by a dynamic folder watcher."""
    return {
        s.node_id
        for s in state.config.subscriptions.subscriptions
        if s.server_id == server_id and s.source_folder_id
    }


def _is_dyn_locked(state: RuntimeState, server_id: str, browse_path: str) -> bool:
    """True when the node sits on or under a dynamic-watcher folder — manual
    (un)subscribing is disabled there to avoid fighting with the watcher."""
    return any(
        browse_path == p or browse_path.startswith(p + "/")
        for p in _dynamic_folder_paths(state, server_id)
    )



@router.get("", response_class=HTMLResponse)
async def browser_page(request: Request, state: RuntimeState = Depends(get_state)):
    servers = state.config.servers.servers
    server_status = state.server_status
    # Set of server IDs that have at least one subscription
    servers_with_subs: set[str] = {
        s.server_id for s in state.config.subscriptions.subscriptions
    }
    return templates.TemplateResponse(request, "browser.html", {
        "request": request,
        "servers": servers,
        "server_status": server_status,
        "servers_with_subs": servers_with_subs,
    })


@router.get("/nodes/{server_id}", response_class=HTMLResponse)
@router.get("/nodes/{server_id}/{node_id:path}", response_class=HTMLResponse)
async def browse_nodes(
    request: Request,
    server_id: str,
    node_id: str | None = None,
    parent_path: str = Query(default=""),
    state: RuntimeState = Depends(get_state),
):
    """Return HTMX fragment with child nodes of the given node."""
    if node_id:
        node_id = unquote(node_id)

    mgr = state.opcua_manager
    if mgr is None:
        return HTMLResponse('<li class="text-red-500 p-2">OPC UA manager not initialized</li>')

    nodes = await get_children(mgr, server_id, node_id, parent_path=parent_path)
    return templates.TemplateResponse(request, "partials/node_row.html", {
        "request": request,
        "nodes": nodes,
        "server_id": server_id,
        "subscribed_map": _subscribed_map(state, server_id),
        "subscribed_ancestor_paths": list(_subscribed_ancestor_paths(state, server_id)),
        "dynamic_folder_paths": list(_dynamic_folder_paths(state, server_id)),
        "dynamic_sub_node_ids": _dynamic_sub_node_ids(state, server_id),
    })


async def _get_all_variables(mgr, server_id: str, node_id: str, browse_path: str) -> list[NodeCtx]:
    """Recursively collect all subscribable Variable NodeCtx objects under the given node."""
    result: list[NodeCtx] = []
    children = await get_children(mgr, server_id, node_id, parent_path=browse_path)
    for child in children:
        if child.node_class == "Variable" and child.has_children:
            # Structured variable exposing its members as browsable children (e.g. a
            # PLC struct) — recurse instead of subscribing to the struct value itself.
            result.extend(await _get_all_variables(mgr, server_id, child.node_id, child.browse_path))
        elif child.node_class == "Variable":
            result.append(NodeCtx(node_id=child.node_id, server_id=server_id,
                                  browse_path=child.browse_path, display_name=child.display_name))
        elif child.node_class in ("Object", "View"):
            result.extend(await _get_all_variables(mgr, server_id, child.node_id, child.browse_path))
    return result


@router.post("/subscribe-folder")
async def subscribe_folder(request: Request, state: RuntimeState = Depends(get_state)):
    form = await request.form()
    server_id = form.get("server_id", "")
    node_id = form.get("node_id", "")
    browse_path = form.get("browse_path", "")

    mgr = state.opcua_manager
    if mgr:
        srv = next((s for s in state.config.servers.servers if s.id == server_id), None)
        defaults = srv.polling_defaults if srv else ServerPollingDefaults()
        subscribed_ids = {s.node_id for s in state.config.subscriptions.subscriptions if s.server_id == server_id}

        active_tags = _default_active_tags(state)
        nodes = await _get_all_variables(mgr, server_id, node_id, browse_path)
        for n in nodes:
            if n.node_id in subscribed_ids:
                continue
            state.config.subscriptions.subscriptions.append(SubscriptionConfig(
                server_id=server_id, node_id=n.node_id, browse_path=n.browse_path,
                display_name=n.display_name,
                influxdb=SubscriptionInfluxConfig(
                    measurement=browse_path_to_measurement(n.browse_path), field_name="value"),
                polling=PollingConfig(
                    interval_ms=defaults.interval_ms, write_mode=defaults.write_mode,
                    threshold=defaults.threshold,
                    heartbeat_interval_seconds=defaults.heartbeat_interval_seconds),
                active_tags=active_tags,
            ))
        save_subscriptions(state.config.subscriptions, state.config_dir)
        if state.polling_engine:
            await state.polling_engine.reconcile()
    return HTMLResponse("ok")


@router.post("/unsubscribe-folder")
async def unsubscribe_folder(request: Request, state: RuntimeState = Depends(get_state)):
    form = await request.form()
    server_id = form.get("server_id", "")
    browse_path = form.get("browse_path", "").rstrip("/")
    prefix = browse_path + "/"
    state.config.subscriptions.subscriptions = [
        s for s in state.config.subscriptions.subscriptions
        if not (s.server_id == server_id and
                (s.browse_path == browse_path or s.browse_path.startswith(prefix)))
    ]
    # Also remove dynamic folder watcher if this folder was watched
    removed_ids = {
        f.id for f in state.config.dynamic_folders.folders
        if f.server_id == server_id and (
            f.folder_browse_path == browse_path or f.folder_browse_path.startswith(prefix)
        )
    }
    if removed_ids:
        state.config.dynamic_folders.folders = [
            f for f in state.config.dynamic_folders.folders if f.id not in removed_ids
        ]
        save_dynamic_folders(state.config.dynamic_folders, state.config_dir)
    save_subscriptions(state.config.subscriptions, state.config_dir)
    if state.polling_engine:
        await state.polling_engine.reconcile()
    return HTMLResponse("ok")


@router.post("/subscribe-folder-dynamic", response_class=HTMLResponse)
async def subscribe_folder_dynamic(request: Request, state: RuntimeState = Depends(get_state)):
    """Register a folder as dynamic and run an initial scan immediately."""
    form = await request.form()
    server_id = form.get("server_id", "")
    node_id = form.get("node_id", "")
    browse_path = form.get("browse_path", "")
    display_name = form.get("display_name", browse_path)

    # Idempotent: if already registered, remove it (toggle off)
    existing = next(
        (f for f in state.config.dynamic_folders.folders
         if f.server_id == server_id and f.folder_browse_path == browse_path),
        None,
    )
    if existing:
        state.config.dynamic_folders.folders.remove(existing)
        save_dynamic_folders(state.config.dynamic_folders, state.config_dir)
        return HTMLResponse("")   # empty = badge removed (caller swaps innerHTML of badge slot)

    watcher = DynamicFolderConfig(
        server_id=server_id,
        folder_node_id=node_id,
        folder_browse_path=browse_path,
        display_name=display_name,
    )
    state.config.dynamic_folders.folders.append(watcher)
    save_dynamic_folders(state.config.dynamic_folders, state.config_dir)

    # Run initial scan immediately via the scanner
    scanner = getattr(state, "folder_scanner", None)
    if scanner:
        await scanner.scan_now(watcher.id)

    return HTMLResponse('<span class="badge badge-dyn dyn-pill" title="Dynamic — auto-syncs new/removed nodes">⟳ auto</span>')


@router.post("/subscribe", response_class=HTMLResponse)
async def subscribe_node(request: Request, state: RuntimeState = Depends(get_state)):
    form = await request.form()
    node_id = form.get("node_id", "")
    server_id = form.get("server_id", "")
    browse_path = form.get("browse_path", "")
    display_name = form.get("display_name", "")

    srv = next((s for s in state.config.servers.servers if s.id == server_id), None)
    defaults = srv.polling_defaults if srv else ServerPollingDefaults()

    sub = SubscriptionConfig(
        server_id=server_id,
        node_id=node_id,
        browse_path=browse_path,
        display_name=display_name,
        influxdb=SubscriptionInfluxConfig(
            measurement=browse_path_to_measurement(browse_path),
            field_name="value",
        ),
        polling=PollingConfig(
            interval_ms=defaults.interval_ms,
            write_mode=defaults.write_mode,
            threshold=defaults.threshold,
            heartbeat_interval_seconds=defaults.heartbeat_interval_seconds,
        ),
        active_tags=_default_active_tags(state),
    )
    state.config.subscriptions.subscriptions.append(sub)
    save_subscriptions(state.config.subscriptions, state.config_dir)
    if state.polling_engine:
        await state.polling_engine.reconcile()

    return templates.TemplateResponse(request, "partials/subscribe_checkbox.html", {
        "request": request,
        "node": NodeCtx(node_id=node_id, server_id=server_id,
                        browse_path=browse_path, display_name=display_name),
        "sub_id": sub.id,
        "is_dyn": False,
        "dyn_locked": _is_dyn_locked(state, server_id, browse_path),
    })


@router.post("/unsubscribe", response_class=HTMLResponse)
async def unsubscribe_node(request: Request, state: RuntimeState = Depends(get_state)):
    form = await request.form()
    sub_id = form.get("sub_id", "")
    node_id = form.get("node_id", "")
    server_id = form.get("server_id", "")
    browse_path = form.get("browse_path", "")
    display_name = form.get("display_name", "")

    state.config.subscriptions.subscriptions = [
        s for s in state.config.subscriptions.subscriptions if s.id != sub_id
    ]
    save_subscriptions(state.config.subscriptions, state.config_dir)
    if state.polling_engine:
        await state.polling_engine.reconcile()

    return templates.TemplateResponse(request, "partials/subscribe_checkbox.html", {
        "request": request,
        "node": NodeCtx(node_id=node_id, server_id=server_id,
                        browse_path=browse_path, display_name=display_name),
        "sub_id": None,
        "is_dyn": False,
        "dyn_locked": _is_dyn_locked(state, server_id, browse_path),
    })


@router.get("/siblings/{server_id}/{node_id:path}", response_class=HTMLResponse)
async def get_siblings(
    request: Request,
    server_id: str,
    node_id: str,
    child_names: str = Query(default=""),
    state: RuntimeState = Depends(get_state),
):
    """Return sibling instances for 'Apply to siblings' feature."""
    mgr = state.opcua_manager
    if mgr is None:
        return HTMLResponse('<p class="text-red-500">Not connected</p>')

    node_id = unquote(node_id)
    filter_names = [n.strip() for n in child_names.split(",") if n.strip()] if child_names else None

    siblings = await find_siblings(mgr, server_id, node_id, child_browse_names=filter_names)
    return templates.TemplateResponse(request, "partials/siblings_panel.html", {
        "request": request,
        "siblings": siblings,
        "server_id": server_id,
        "source_node_id": node_id,
        "child_names": filter_names or [],
    })
