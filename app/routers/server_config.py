from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import ModbusServerSettings, ServerConfig, ServerAuth, ServerStaticTag, save_servers
from app.dependencies import get_state
from app.state import RuntimeState

def _int(form, name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(form.get(name) or default)))
    except ValueError:
        return default


def _modbus_settings(form, old: ModbusServerSettings | None = None) -> tuple[str, ModbusServerSettings]:
    """(endpoint_url, settings) from the Modbus fields of the server form."""
    old = old or ModbusServerSettings()
    from app.modbus.client import parse_endpoint
    text = (form.get("mb_host") or "").strip()
    port = _int(form, "mb_port", 502, 1, 65535)
    host, typed_port = parse_endpoint(text)
    if typed_port != 502 or text.rstrip("/").endswith(":502"):   # "host:port" typed in the host field
        port = typed_port
    if ":" in host:
        host = f"[{host}]"                                        # IPv6
    order = form.get("mb_order", old.order)
    return f"modbus-tcp://{host}:{port}", ModbusServerSettings(
        unit_id=_int(form, "mb_unit_id", old.unit_id, 0, 255),
        timeout_ms=_int(form, "mb_timeout_ms", old.timeout_ms, 100, 60000),
        order=order if order in ("ABCD", "CDAB", "BADC", "DCBA") else old.order,
        max_registers=_int(form, "mb_max_registers", old.max_registers, 1, 125),
        max_gap=_int(form, "mb_max_gap", old.max_gap, 0, 100),
    )


router = APIRouter(prefix="/config/servers")
templates = Jinja2Templates(directory="templates")


@router.get("", response_class=HTMLResponse)
async def list_servers(request: Request, state: RuntimeState = Depends(get_state)):
    return templates.TemplateResponse(request, "config/servers.html", {
        "request": request,
        "servers": state.config.servers.servers,
        "server_status": state.server_status,
        "register_counts": _register_counts(state),
    })


def _register_counts(state: RuntimeState) -> dict[str, tuple[int, int]]:
    """Modbus server → (active registers, all registers)."""
    out: dict[str, list[int]] = {}
    for s in state.config.subscriptions.subscriptions:
        if s.modbus is not None:
            c = out.setdefault(s.server_id, [0, 0])
            c[0] += s.enabled
            c[1] += 1
    return {k: (v[0], v[1]) for k, v in out.items()}


@router.post("")
async def add_server(request: Request, state: RuntimeState = Depends(get_state)):
    form = await request.form()
    if form.get("protocol") == "modbus":
        url, mb = _modbus_settings(form)
        srv = ServerConfig(display_name=form.get("display_name", "New Server"), protocol="modbus",
                           endpoint_url=url, modbus=mb,
                           reconnect_interval_seconds=_int(form, "reconnect_interval", 10, 1, 3600))
        state.config.servers.servers.append(srv)
        save_servers(state.config.servers, state.config_dir)
        if state.opcua_manager:
            await state.opcua_manager.reconcile()
        return RedirectResponse(f"/config/servers/{srv.id}/registers", status_code=303)
    srv = ServerConfig(
        display_name=form.get("display_name", "New Server"),
        endpoint_url=form.get("endpoint_url", "opc.tcp://localhost:4840"),
        security_mode=form.get("security_mode", "None"),
        security_policy=form.get("security_policy", "Basic256Sha256"),
        auth=ServerAuth(
            type=form.get("auth_type", "anonymous"),
            username=form.get("username", ""),
            password=form.get("password", ""),
            certificate_path=form.get("certificate_path", "").strip(),
            private_key_path=form.get("private_key_path", "").strip(),
        ),
        reconnect_interval_seconds=int(form.get("reconnect_interval", "10")),
        enabled=form.get("enabled", "true") == "true",
    )
    state.config.servers.servers.append(srv)
    save_servers(state.config.servers, state.config_dir)
    if state.opcua_manager:
        await state.opcua_manager.reconcile()
    return RedirectResponse("/config/servers", status_code=303)


@router.get("/{server_id}/edit", response_class=HTMLResponse)
async def edit_server(
    server_id: str,
    request: Request,
    state: RuntimeState = Depends(get_state),
):
    srv = next((s for s in state.config.servers.servers if s.id == server_id), None)
    if srv is None:
        return HTMLResponse("Not found", status_code=404)
    return templates.TemplateResponse(request, "partials/server_edit_form.html", {
        "request": request,
        "srv": srv,
    })


@router.post("/{server_id}/update")
async def update_server(
    server_id: str,
    request: Request,
    state: RuntimeState = Depends(get_state),
):
    form = await request.form()
    for srv in state.config.servers.servers:
        if srv.id == server_id and srv.protocol == "modbus":
            srv.display_name = form.get("display_name", srv.display_name)
            srv.endpoint_url, srv.modbus = _modbus_settings(form, srv.modbus)
            srv.reconnect_interval_seconds = _int(form, "reconnect_interval", srv.reconnect_interval_seconds, 1, 3600)
            srv.bool_as_int = form.get("bool_as_int") == "1"
            break
        if srv.id == server_id:
            srv.display_name = form.get("display_name", srv.display_name)
            srv.endpoint_url = form.get("endpoint_url", srv.endpoint_url)
            srv.security_mode = form.get("security_mode", srv.security_mode)
            srv.security_policy = form.get("security_policy", srv.security_policy)
            srv.auth.certificate_path = form.get("certificate_path", srv.auth.certificate_path).strip()
            srv.auth.private_key_path = form.get("private_key_path", srv.auth.private_key_path).strip()
            srv.reconnect_interval_seconds = int(
                form.get("reconnect_interval", srv.reconnect_interval_seconds)
            )
            auth_type = form.get("auth_type", srv.auth.type)
            srv.auth.type = auth_type
            if auth_type == "username":
                srv.auth.username = form.get("username", srv.auth.username)
                new_pw = form.get("password", "")
                if new_pw:
                    srv.auth.password = new_pw
            srv.bool_as_int = form.get("bool_as_int") == "1"
            break
    save_servers(state.config.servers, state.config_dir)
    if state.opcua_manager:
        await state.opcua_manager.reconcile()
    return RedirectResponse("/config/servers", status_code=303)


@router.post("/{server_id}/polling-defaults")
async def save_polling_defaults(server_id: str, request: Request, state: RuntimeState = Depends(get_state)):
    form = await request.form()
    for srv in state.config.servers.servers:
        if srv.id == server_id:
            srv.polling_defaults.interval_ms = int(form.get("interval_ms", 1000))
            srv.polling_defaults.write_mode = form.get("write_mode", "on_change")
            srv.polling_defaults.threshold = float(form.get("threshold", 0.0))
            srv.polling_defaults.heartbeat_interval_seconds = int(
                form.get("heartbeat_interval_seconds", 60)
            )
            break
    save_servers(state.config.servers, state.config_dir)
    return RedirectResponse("/config/servers", status_code=303)


@router.post("/{server_id}/delete")
async def delete_server(server_id: str, state: RuntimeState = Depends(get_state)):
    state.config.servers.servers = [s for s in state.config.servers.servers if s.id != server_id]
    save_servers(state.config.servers, state.config_dir)
    if state.opcua_manager:
        await state.opcua_manager.reconcile()
    return RedirectResponse("/config/servers", status_code=303)


@router.post("/{server_id}/toggle")
async def toggle_server(server_id: str, state: RuntimeState = Depends(get_state)):
    for srv in state.config.servers.servers:
        if srv.id == server_id:
            srv.enabled = not srv.enabled
            break
    save_servers(state.config.servers, state.config_dir)
    if state.opcua_manager:
        await state.opcua_manager.reconcile()
    return RedirectResponse("/config/servers", status_code=303)


@router.post("/{server_id}/static-tags")
async def add_static_tag(server_id: str, request: Request, state: RuntimeState = Depends(get_state)):
    form = await request.form()
    key = form.get("key", "").strip()
    value = form.get("value", "").strip()
    if key:
        for srv in state.config.servers.servers:
            if srv.id == server_id:
                srv.static_tags.append(ServerStaticTag(key=key, value=value))
                break
        save_servers(state.config.servers, state.config_dir)
    return RedirectResponse("/config/servers", status_code=303)


@router.post("/{server_id}/static-tags/{tag_index}/delete")
async def delete_static_tag(server_id: str, tag_index: int, state: RuntimeState = Depends(get_state)):
    for srv in state.config.servers.servers:
        if srv.id == server_id:
            if 0 <= tag_index < len(srv.static_tags):
                srv.static_tags.pop(tag_index)
            break
    save_servers(state.config.servers, state.config_dir)
    return RedirectResponse("/config/servers", status_code=303)


@router.post("/{server_id}/name-strip-prefixes")
async def add_name_strip_prefix(server_id: str, request: Request, state: RuntimeState = Depends(get_state)):
    form = await request.form()
    prefix = form.get("prefix", "").strip()
    if prefix:
        for srv in state.config.servers.servers:
            if srv.id == server_id:
                if prefix not in srv.name_strip_prefixes:
                    srv.name_strip_prefixes.append(prefix)
                break
        save_servers(state.config.servers, state.config_dir)
    return RedirectResponse("/config/servers", status_code=303)


@router.post("/{server_id}/name-strip-prefixes/{prefix_index}/delete")
async def delete_name_strip_prefix(server_id: str, prefix_index: int, state: RuntimeState = Depends(get_state)):
    for srv in state.config.servers.servers:
        if srv.id == server_id:
            if 0 <= prefix_index < len(srv.name_strip_prefixes):
                srv.name_strip_prefixes.pop(prefix_index)
            break
    save_servers(state.config.servers, state.config_dir)
    return RedirectResponse("/config/servers", status_code=303)


def _suggest_strip_prefix(measurements: list[str]) -> str:
    """Return the longest common dot-separated prefix across all measurements, leaving ≥1 trailing segment."""
    if not measurements:
        return ""
    parts_list = [m.split(".") for m in measurements]
    if len(parts_list) == 1:
        # single measurement: suggest all-but-last segment
        parts = parts_list[0]
        return ".".join(parts[:-1]) if len(parts) > 1 else ""
    # Find common prefix segments
    min_len = min(len(p) for p in parts_list)
    common: list[str] = []
    for i in range(min_len):
        segment = parts_list[0][i]
        if all(p[i] == segment for p in parts_list):
            common.append(segment)
        else:
            break
    # Never consume the entire name — leave at least one distinct segment
    max_len = min(len(p) for p in parts_list) - 1
    common = common[:max_len]
    return ".".join(common)


@router.get("/{server_id}/suggest-name-strip-prefix", response_class=HTMLResponse)
async def suggest_name_strip_prefix(
    server_id: str,
    request: Request,
    state: RuntimeState = Depends(get_state),
):
    measurements = [
        s.influxdb.measurement
        for s in state.config.subscriptions.subscriptions
        if s.server_id == server_id
    ]
    suggestion = _suggest_strip_prefix(measurements)
    return templates.TemplateResponse(request, "partials/suggest_name_strip_prefix.html", {
        "request": request,
        "suggestion": suggestion,
        "server_id": server_id,
        "total": len(measurements),
    })


@router.get("/{server_id}/test", response_class=HTMLResponse)
async def test_server(
    request: Request,
    server_id: str,
    state: RuntimeState = Depends(get_state),
):
    """Test connection to a server and return a status fragment."""
    from asyncua import Client
    srv = next((s for s in state.config.servers.servers if s.id == server_id), None)
    if srv is None:
        return HTMLResponse('<span class="badge badge-error">Server not found</span>')
    if srv.protocol == "modbus":
        from app.modbus.client import ModbusError, ModbusTcpClient, parse_endpoint
        host, port = parse_endpoint(srv.endpoint_url)
        client = ModbusTcpClient(host, port, timeout=max(0.2, srv.modbus.timeout_ms / 1000))
        try:
            await client.connect()
            first = next((s.modbus for s in state.config.subscriptions.subscriptions
                          if s.server_id == srv.id and s.modbus and s.enabled), None)
            msg = f"Connected to {host}:{port}"
            if first is not None:
                from app.modbus.codec import AREAS
                from app.modbus.manager import make_point
                p = make_point(srv, first)
                try:
                    await client.read(p.unit, AREAS[p.area][0], p.address, p.count)
                    msg += f", read {first.node_id()} OK"
                except ModbusError as exc:
                    msg += f", but reading {first.node_id()}: {exc}"
            result = "ok"
        except Exception as exc:
            result, msg = "error", str(exc) or f"no connection to {host}:{port}"
        finally:
            await client.close()
        return templates.TemplateResponse(request, "partials/test_result.html", {
            "request": request, "result": result, "message": msg})

    try:
        client = Client(url=srv.endpoint_url, timeout=5)
        await client.connect()
        await client.disconnect()
        result = "ok"
        msg = "Connected successfully"
    except Exception as exc:
        result = "error"
        msg = str(exc)

    return templates.TemplateResponse(request, "partials/test_result.html", {
        "request": request,
        "result": result,
        "message": msg,
    })
