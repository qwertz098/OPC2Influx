"""
Registers of a Modbus TCP server: each one is a subscription with a Modbus
point. Add, edit, enable / disable one or all, import / export CSV, test read.
"""
from __future__ import annotations

import csv
import io
import re

from fastapi import APIRouter, Depends, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app.config import (ModbusPointConfig, PollingConfig, ServerConfig, SubscriptionConfig,
                        SubscriptionInfluxConfig, save_subscriptions)
from app.dependencies import get_state
from app.modbus.codec import AREAS, TYPES
from app.state import RuntimeState

router = APIRouter(prefix="/config/servers/{server_id}/registers")
templates = Jinja2Templates(directory="templates")

CSV_COLUMNS = ["name", "area", "address", "type", "order", "bit", "scale", "offset", "unit_id",
               "interval_ms", "enabled", "measurement"]


def _server(state: RuntimeState, server_id: str) -> ServerConfig | None:
    return next((s for s in state.config.servers.servers if s.id == server_id and s.protocol == "modbus"), None)


def _registers(state: RuntimeState, server_id: str) -> list[SubscriptionConfig]:
    regs = [s for s in state.config.subscriptions.subscriptions if s.server_id == server_id and s.modbus]
    return sorted(regs, key=lambda s: (list(AREAS).index(s.modbus.area), s.modbus.unit_id or 0, s.modbus.address))


async def _apply(state: RuntimeState) -> None:
    save_subscriptions(state.config.subscriptions, state.config_dir)
    if state.polling_engine:
        await state.polling_engine.reconcile()


def _point(data: dict) -> ModbusPointConfig:
    """ModbusPointConfig from form / JSON / CSV fields (raises ValueError)."""
    def num(name, cast, default=None):
        v = data.get(name)
        v = "" if v is None else str(v).strip()      # 0 is a value, not empty
        if cast is float:
            v = v.replace(",", ".")             # 0,1 from a German spreadsheet
        if v == "":
            return default
        try:
            return cast(v)
        except ValueError:
            raise ValueError(f"{name}: not a number ({v})") from None
    area = str(data.get("area") or "holding").strip().lower()
    area = {"hr": "holding", "ir": "input", "co": "coil", "di": "discrete", "coils": "coil",
            "holding register": "holding", "input register": "input", "discrete input": "discrete"}.get(area, area)
    dtype = str(data.get("type") or data.get("data_type") or "uint16").strip().lower()
    bit = num("bit", int)
    try:
        mp = ModbusPointConfig(
            area=area, address=num("address", int, 0), data_type="bool" if AREAS.get(area, (0, False))[1] else (
                "uint16" if dtype == "bool" and bit is not None else dtype),
            order=str(data.get("order") or "").strip().upper(), bit=bit,
            scale=num("scale", float, 1.0), offset=num("offset", float, 0.0), unit_id=num("unit_id", int))
    except ValidationError as exc:
        err = exc.errors()[0]
        raise ValueError(f"{err['loc'][0]}: {err['msg']}") from None
    if not 0 <= mp.address <= 65535:
        raise ValueError("address: 0 … 65535")
    if mp.bit is not None and (AREAS[mp.area][1] or not 0 <= mp.bit <= 15):
        raise ValueError("bit: 0 … 15, only for registers")
    if mp.data_type == "bool" and not AREAS[mp.area][1] and mp.bit is None:
        raise ValueError("type bool needs a bit (0 … 15) in a register, or a coil / discrete input")
    if mp.address + TYPES[mp.data_type][0] > 65536:
        raise ValueError("address: the value runs past register 65535")
    if mp.unit_id is not None and not 0 <= mp.unit_id <= 255:
        raise ValueError("unit_id: 0 … 255")
    return mp


def _upsert(state: RuntimeState, srv: ServerConfig, data: dict, reg_id: str = "") -> SubscriptionConfig:
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError("name: required")
    mp = _point(data)
    node_id = mp.node_id()
    regs = _registers(state, srv.id)
    for other in regs:
        if other.id != reg_id and other.node_id == node_id:
            raise ValueError(f"{node_id} is already configured as “{other.display_name}”")
    interval = int(data.get("interval_ms") or srv.polling_defaults.interval_ms)
    measurement = str(data.get("measurement") or "").strip() or name
    sub = next((r for r in regs if r.id == reg_id), None) if reg_id else None
    if sub is None:
        d = srv.polling_defaults
        sub = SubscriptionConfig(
            server_id=srv.id, node_id=node_id, display_name=name, modbus=mp,
            influxdb=SubscriptionInfluxConfig(measurement=measurement),
            polling=PollingConfig(interval_ms=interval, write_mode=d.write_mode, threshold=d.threshold,
                                  heartbeat_interval_seconds=d.heartbeat_interval_seconds))
        state.config.subscriptions.subscriptions.append(sub)
    else:
        if sub.influxdb.measurement == sub.display_name or data.get("measurement"):
            sub.influxdb.measurement = measurement        # follows the name unless set apart
        sub.display_name, sub.node_id, sub.modbus = name, node_id, mp
        sub.polling.interval_ms = interval
    if "enabled" in data and str(data["enabled"]).strip() != "":
        sub.enabled = str(data["enabled"]).strip().lower() in ("1", "true", "yes", "on", "x")
    return sub


@router.get("", response_class=HTMLResponse)
async def page(server_id: str, request: Request, state: RuntimeState = Depends(get_state)):
    srv = _server(state, server_id)
    if srv is None:
        return RedirectResponse("/config/servers", status_code=302)
    regs = _registers(state, server_id)
    return templates.TemplateResponse(request, "config/modbus_registers.html", {
        "request": request, "srv": srv, "status": state.server_status.get(srv.id), "regs": regs,
        "regs_json": [{"id": r.id, "name": r.display_name, "measurement": r.influxdb.measurement,
                       "interval_ms": r.polling.interval_ms, **r.modbus.model_dump()} for r in regs],
        "areas": AREAS, "types": list(TYPES),
    })


@router.get("/values")
async def values(server_id: str, state: RuntimeState = Depends(get_state)):
    """Latest values and read errors for the page (refreshed every few seconds)."""
    srv = _server(state, server_id)
    if srv is None:
        return JSONResponse({}, status_code=404)
    live = state.polling_engine.live_values() if state.polling_engine else {}
    mgr = state.opcua_manager.modbus if state.opcua_manager else None
    errors = mgr.point_errors(srv.id) if mgr else {}
    from app.modbus.manager import make_point
    out = {}
    for r in _registers(state, server_id):
        p = make_point(srv, r.modbus)
        v = live.get(r.id)
        out[r.id] = {"value": v if not isinstance(v, float) else float(f"{v:.6g}"),
                     "error": errors.get(f"{p.unit}/{p.area}/{p.address}", "")}
    status = state.server_status.get(srv.id)
    return {"connected": bool(status and status.connected), "error": status.last_error if status else "",
            "registers": out}


@router.post("")
async def save(server_id: str, request: Request, state: RuntimeState = Depends(get_state)):
    srv = _server(state, server_id)
    if srv is None:
        return JSONResponse({"error": "Server not found"}, status_code=404)
    data = await request.json()
    try:
        sub = _upsert(state, srv, data, data.get("id") or "")
    except (ValueError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    await _apply(state)
    return {"id": sub.id}


@router.post("/test")
async def test_read(server_id: str, request: Request, state: RuntimeState = Depends(get_state)):
    """Read one register as configured in the form, without saving it."""
    srv = _server(state, server_id)
    if srv is None:
        return JSONResponse({"error": "Server not found"}, status_code=404)
    try:
        mp = _point(await request.json())
    except (ValueError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    from app.modbus.client import ModbusError
    from app.modbus.codec import decode, interpretations, point_value
    from app.modbus.manager import ModbusManager, make_point
    p = make_point(srv, mp)
    mgr = state.opcua_manager.modbus if state.opcua_manager else ModbusManager(state)
    fc = AREAS[p.area][0]
    try:
        bits = AREAS[p.area][1]
        data = None
        if not bits and p.count < 4:              # a few registers more for the type preview
            try:
                data = await mgr.read_raw(srv, p.unit, fc, p.address, 4)
            except ModbusError:
                data = None
        if data is None:
            data = await mgr.read_raw(srv, p.unit, fc, p.address, p.count)
        out = {"node_id": mp.node_id(), "value": point_value(p, p.address, data)}
        if bits:
            out["raw"] = [int(data[0])]
        else:
            words = [int(x) for x in data]
            out["raw"] = words[:p.count]
            out["extra"] = words[p.count:]
            out["unscaled"] = decode(words[:p.count], p.data_type, p.order, p.bit)
            out["bits"] = [(words[0] >> b) & 1 for b in range(16)]
            out["types"] = interpretations(words)
        return out
    except ModbusError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": str(exc) or "no connection"}, status_code=400)


@router.post("/bits")
async def add_bits(server_id: str, request: Request, state: RuntimeState = Depends(get_state)):
    """Several bits of one register as separate registers (bool), e.g. a status word."""
    srv = _server(state, server_id)
    if srv is None:
        return JSONResponse({"error": "Server not found"}, status_code=404)
    data = await request.json()
    bits = sorted({int(b) for b in data.get("bits") or [] if 0 <= int(b) <= 15})
    if not bits:
        return JSONResponse({"error": "Choose at least one bit"}, status_code=400)
    names = data.get("names") or {}
    base = str(data.get("name") or "").strip() or "Bit"
    added, errors = [], []
    for b in bits:
        row = {**data, "type": "uint16", "bit": b, "order": "", "scale": 1, "offset": 0, "measurement": "",
               "name": str(names.get(str(b)) or "").strip() or f"{base}.bit{b}"}
        try:
            added.append(_upsert(state, srv, row).id)
        except (ValueError, TypeError) as exc:
            errors.append(f"bit {b}: {exc}")
    await _apply(state)
    return {"added": len(added), "errors": errors}


@router.post("/{reg_id}/toggle")
async def toggle(server_id: str, reg_id: str, request: Request, state: RuntimeState = Depends(get_state)):
    data = await request.json()
    for r in _registers(state, server_id):
        if r.id == reg_id:
            r.enabled = bool(data.get("enabled"))
            await _apply(state)
            return {"enabled": r.enabled}
    return JSONResponse({"error": "Register not found"}, status_code=404)


@router.post("/all")
async def set_all(server_id: str, request: Request, state: RuntimeState = Depends(get_state)):
    """Alle an / Alle ab: every register of the server, or only the given ids."""
    data = await request.json()
    on = bool(data.get("enabled"))
    ids = set(data.get("ids") or [])
    n = 0
    for r in _registers(state, server_id):
        if (not ids or r.id in ids) and r.enabled != on:
            r.enabled = on
            n += 1
    await _apply(state)
    return {"changed": n}


@router.post("/{reg_id}/delete")
async def delete(server_id: str, reg_id: str, state: RuntimeState = Depends(get_state)):
    subs = state.config.subscriptions.subscriptions
    state.config.subscriptions.subscriptions = [s for s in subs if not (s.id == reg_id and s.server_id == server_id)]
    await _apply(state)
    return RedirectResponse(f"/config/servers/{server_id}/registers", status_code=303)


@router.get("/export.csv")
async def export_csv(server_id: str, state: RuntimeState = Depends(get_state)):
    srv = _server(state, server_id)
    if srv is None:
        return Response("Server not found", status_code=404)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    for r in _registers(state, server_id):
        m = r.modbus
        w.writerow([r.display_name, m.area, m.address, m.data_type, m.order, "" if m.bit is None else m.bit,
                    f"{m.scale:g}", f"{m.offset:g}", "" if m.unit_id is None else m.unit_id,
                    r.polling.interval_ms, 1 if r.enabled else 0,
                    "" if r.influxdb.measurement == r.display_name else r.influxdb.measurement])
    fname = re.sub(r"[^A-Za-z0-9_.-]+", "_", srv.display_name).strip("_") or "modbus"
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{fname}_registers.csv"'})


def _plan_import(state: RuntimeState, srv: ServerConfig, text: str, modicon: bool) -> list[dict]:
    """What importing the text would do, row by row: new / update / error."""
    from app.modbus.table_import import normalize, parse_rows
    regs = _registers(state, srv.id)
    by_name = {r.display_name: r for r in regs}
    taken = {r.node_id: r for r in regs}
    seen_names, seen_nodes = set(), {}
    plan = []
    for n, raw in enumerate(parse_rows(text), start=1):
        item = {"row": n, "status": "error", "fields": {"name": raw.get("name") or raw.get("description") or ""}}
        try:
            fields = normalize(raw, modicon=modicon)
            item["fields"] = fields
            node_id = _point(fields).node_id()
            item["node_id"] = node_id
            name = fields["name"]
            if name in seen_names:
                raise ValueError(f"name {name} appears twice")
            if node_id in seen_nodes:
                raise ValueError(f"{node_id} appears twice (also {seen_nodes[node_id]})")
            other = taken.get(node_id)
            existing = by_name.get(name)
            if other is not None and other is not existing:
                raise ValueError(f"{node_id} is already configured as “{other.display_name}”")
            seen_names.add(name)
            seen_nodes[node_id] = name
            item["status"] = "update" if existing else "new"
            item["id"] = existing.id if existing else ""
        except (ValueError, TypeError) as exc:
            item["error"] = str(exc)
        plan.append(item)
    return plan


async def _run_import(state: RuntimeState, srv: ServerConfig, text: str, modicon: bool) -> dict:
    plan = _plan_import(state, srv, text, modicon)
    added = updated = 0
    errors = [f"row {p['row']}: {p['error']}" for p in plan if p["status"] == "error"]
    for p in plan:
        if p["status"] == "error":
            continue
        try:
            _upsert(state, srv, p["fields"], p["id"])
            added += p["status"] == "new"
            updated += p["status"] == "update"
        except (ValueError, TypeError) as exc:
            errors.append(f"row {p['row']}: {exc}")
    await _apply(state)
    return {"added": added, "updated": updated, "errors": errors[:20]}


@router.post("/import")
async def import_csv(server_id: str, file: UploadFile, state: RuntimeState = Depends(get_state)):
    """Add registers from a CSV file; a row whose name exists updates that register."""
    srv = _server(state, server_id)
    if srv is None:
        return JSONResponse({"error": "Server not found"}, status_code=404)
    raw = await file.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("cp1252")
    first = text.lstrip("\ufeff").split("\n", 1)[0].lower()
    if "address" not in first and "adresse" not in first and "register" not in first:
        return JSONResponse({"error": "The CSV needs a header line with at least a name and an address column"},
                            status_code=400)
    # our own export has 0-based addresses: no Modicon numbers there
    return await _run_import(state, srv, text, modicon="interval_ms" not in first)


@router.post("/paste/preview")
async def paste_preview(server_id: str, request: Request, state: RuntimeState = Depends(get_state)):
    srv = _server(state, server_id)
    if srv is None:
        return JSONResponse({"error": "Server not found"}, status_code=404)
    data = await request.json()
    return {"rows": _plan_import(state, srv, data.get("text") or "", bool(data.get("modicon", True)))}


@router.post("/paste")
async def paste_import(server_id: str, request: Request, state: RuntimeState = Depends(get_state)):
    srv = _server(state, server_id)
    if srv is None:
        return JSONResponse({"error": "Server not found"}, status_code=404)
    data = await request.json()
    return await _run_import(state, srv, data.get("text") or "", bool(data.get("modicon", True)))


@router.get("/ai-prompt")
async def ai_prompt():
    from app.modbus.table_import import PROMPT
    return Response(PROMPT, media_type="text/plain; charset=utf-8")
