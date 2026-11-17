"""
Outputs page: the app's Modbus TCP server and its register map.
"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app.config import ModbusServerOutputConfig, ModbusWriteConfig, OutputPointConfig, save_outputs
from app.dependencies import get_state
from app.modbus.codec import AREAS, TYPES, register_count
from app.outputs.modbus_out import footprint, label_of, overlaps, point_type
from app.state import RuntimeState

router = APIRouter(prefix="/outputs")
templates = Jinja2Templates(directory="templates")

MODICON = {"coil": 1, "discrete": 10001, "input": 30001, "holding": 40001}


def modicon(area: str, address: int) -> str:
    n = MODICON[area] + address
    return f"{n:05d}" if address < 9999 else str(n)


def _save(state: RuntimeState) -> None:
    save_outputs(state.config.outputs, state.config_dir)


def _next_free(points: list[OutputPointConfig], area: str, count: int = 1) -> int:
    used = {a for p in points for (ar, a) in footprint(p) if ar == area}
    addr = 0
    while any(addr + i in used for i in range(count)):
        addr += 1
    return addr


def _point_json(state: RuntimeState, p: OutputPointConfig, bad: dict, labels: dict | None = None) -> dict:
    label = labels.get(p.source_id) if labels and p.kind == "value" and not p.name else None
    return {**p.model_dump(), "label": label or label_of(p, state), "modicon": modicon(p.area, p.address),
            "count": register_count(p.area, point_type(p)), "problem": bad.get(p.id, "")}


@router.get("", response_class=HTMLResponse)
async def page(request: Request, state: RuntimeState = Depends(get_state)):
    out = state.config.outputs
    bad = overlaps(out.points)
    subs = state.config.subscriptions.subscriptions
    from app.references.capture import signal_labels
    labels = signal_labels(state)
    wbad = write_overlaps(out.modbus_writes.targets)
    return templates.TemplateResponse(request, "outputs.html", {
        "request": request, "ms": out.modbus_server,
        "points": [_point_json(state, p, bad, labels) for p in out.points],
        "subs": [{"id": s.id, "label": labels.get(s.id, s.id), "measurement": s.influxdb.measurement,
                  "enabled": s.enabled} for s in subs],
        "rules": [{"id": r.id, "label": r.name or labels.get(r.subscription_id, r.subscription_id)}
                  for r in state.config.alarm_rules.rules],
        "status": state.modbus_output.status() if state.modbus_output else {},
        "mw": out.modbus_writes,
        "tiles": out.shown_tiles(),
        "writes": [_write_json(state, t, labels, wbad) for t in out.modbus_writes.targets],
        "devices": [{"id": s.id, "label": s.display_name, "enabled": s.enabled, "order": s.modbus.order,
                     "unit_id": s.modbus.unit_id}
                    for s in state.config.servers.servers if s.protocol == "modbus"],
        "types": list(TYPES),
    })


@router.post("/tiles")
async def tiles(request: Request, state: RuntimeState = Depends(get_state)):
    """Add a tile, or remove one: that switches its function off; its settings are kept."""
    from app.config import OUTPUT_TILES
    data = await request.json()
    out = state.config.outputs
    shown = out.shown_tiles()
    name = data.get("add") or data.get("remove")
    if name not in OUTPUT_TILES or name == "mqtt_write":
        return JSONResponse({"error": "unknown tile"}, status_code=400)
    if data.get("add") and name not in shown:
        shown.append(name)
    elif data.get("remove") and name in shown:
        shown.remove(name)
        if name == "modbus_server":
            out.modbus_server.enabled = False
        elif name == "modbus_write":
            out.modbus_writes.enabled = False
    out.tiles = shown
    _save(state)
    if state.modbus_output:
        await state.modbus_output.sync()
    return {"tiles": shown}


@router.post("/server")
async def save_server(request: Request, state: RuntimeState = Depends(get_state)):
    data = await request.json()
    try:
        state.config.outputs.modbus_server = ModbusServerOutputConfig(**{
            **state.config.outputs.modbus_server.model_dump(), **data})
    except ValidationError as exc:
        return JSONResponse({"error": exc.errors()[0]["msg"]}, status_code=400)
    _save(state)
    if state.modbus_output:
        await state.modbus_output.sync()
    return state.modbus_output.status() if state.modbus_output else {}


def _clean(data: dict) -> dict:
    out = {}
    for k, v in data.items():
        if v == "" and k in ("status_address",):
            out[k] = None
        elif k in ("address", "status_address") and v not in (None, ""):
            out[k] = int(v)
        elif k == "scale" and v not in (None, ""):
            out[k] = float(str(v).replace(",", "."))
        elif v is not None:
            out[k] = v
    return out


@router.post("/points")
async def save_point(request: Request, state: RuntimeState = Depends(get_state)):
    data = await request.json()
    points = state.config.outputs.points
    old = next((p for p in points if p.id == data.get("id")), None)
    try:
        base = old.model_dump() if old else {}
        p = OutputPointConfig(**{**base, **_clean({k: v for k, v in data.items() if k != "id"}),
                                 **({"id": old.id} if old else {})})
    except (ValidationError, ValueError) as exc:
        msg = exc.errors()[0]["msg"] if isinstance(exc, ValidationError) else str(exc)
        return JSONResponse({"error": msg}, status_code=400)
    if p.kind == "received" and p.area not in ("holding", "coil"):
        return JSONResponse({"error": "Clients can only write holding registers and coils"}, status_code=400)
    if p.kind == "received" and not p.name.strip():
        return JSONResponse({"error": "A written register needs a name"}, status_code=400)
    if p.kind == "alarm" and not AREAS[p.area][1]:
        return JSONResponse({"error": "Alarms are coils or discrete inputs"}, status_code=400)
    if p.scale == 0:
        return JSONResponse({"error": "scale must not be 0"}, status_code=400)
    if p.address < 0 or p.address + register_count(p.area, point_type(p)) > 65536:
        return JSONResponse({"error": "address: 0 … 65535"}, status_code=400)
    trial = [x for x in points if x.id != p.id] + [p]
    problem = overlaps(trial).get(p.id)
    if problem:
        return JSONResponse({"error": problem}, status_code=400)
    if old:
        points[points.index(old)] = p
    else:
        points.append(p)
    _save(state)
    return {"id": p.id}


@router.post("/points/add-signals")
async def add_signals(request: Request, state: RuntimeState = Depends(get_state)):
    """Several subscriptions at once, at the next free addresses."""
    data = await request.json()
    points = state.config.outputs.points
    subs = {s.id: s for s in state.config.subscriptions.subscriptions}
    live = state.polling_engine.live_values() if state.polling_engine else {}
    area = data.get("area") if data.get("area") in ("holding", "input") else "holding"
    dtype = data.get("type") if data.get("type") in TYPES and data.get("type") != "bool" else "float32"
    bool_area = data.get("bool_area") if data.get("bool_area") in ("coil", "discrete") else "coil"
    with_status = bool(data.get("status"))
    added = 0
    for sid in data.get("ids") or []:
        if sid not in subs or any(p.kind == "value" and p.source_id == sid for p in points):
            continue
        is_bool = isinstance(live.get(sid), bool) or (subs[sid].modbus is not None and
                                                      (subs[sid].modbus.bit is not None or AREAS[subs[sid].modbus.area][1]))
        p_area = bool_area if is_bool else area
        p = OutputPointConfig(kind="value", source_id=sid, area=p_area,
                              data_type="bool" if is_bool else dtype)
        p.address = _next_free(points, p_area, register_count(p_area, point_type(p)))
        if with_status:
            p.status_address = _next_free(points + [p], "discrete")
        points.append(p)
        added += 1
    _save(state)
    return {"added": added}


@router.post("/points/add-alarms")
async def add_alarms(request: Request, state: RuntimeState = Depends(get_state)):
    """Every alarm rule not in the map yet, as coils (or discrete inputs)."""
    data = await request.json()
    area = data.get("area") if data.get("area") in ("coil", "discrete") else "coil"
    points = state.config.outputs.points
    added = 0
    for r in state.config.alarm_rules.rules:
        if any(p.kind == "alarm" and p.source_id == r.id for p in points):
            continue
        p = OutputPointConfig(kind="alarm", source_id=r.id, area=area, data_type="bool")
        p.address = _next_free(points, area)
        points.append(p)
        added += 1
    _save(state)
    return {"added": added}


@router.post("/points/all")
async def set_all(request: Request, state: RuntimeState = Depends(get_state)):
    data = await request.json()
    on, ids = bool(data.get("enabled")), set(data.get("ids") or [])
    n = 0
    for p in state.config.outputs.points:
        if (not ids or p.id in ids) and p.enabled != on:
            p.enabled = on
            n += 1
    _save(state)
    return {"changed": n}


@router.post("/points/{point_id}/toggle")
async def toggle(point_id: str, request: Request, state: RuntimeState = Depends(get_state)):
    data = await request.json()
    for p in state.config.outputs.points:
        if p.id == point_id:
            p.enabled = bool(data.get("enabled"))
            _save(state)
            return {"enabled": p.enabled}
    return JSONResponse({"error": "not found"}, status_code=404)


@router.post("/points/{point_id}/delete")
async def delete(point_id: str, state: RuntimeState = Depends(get_state)):
    state.config.outputs.points = [p for p in state.config.outputs.points if p.id != point_id]
    _save(state)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Writing to external Modbus devices
# ---------------------------------------------------------------------------

def _write_cells(t: ModbusWriteConfig, units: dict) -> list[tuple]:
    unit = units.get(t.server_id, 1) if t.unit_id is None else t.unit_id
    n = 1 if t.area == "coil" else TYPES[t.data_type][0]
    cells = [(t.server_id, unit, t.area, t.address + i) for i in range(n)]
    if t.status_address is not None:
        cells.append((t.server_id, unit, "coil", t.status_address))
    return cells


def write_overlaps(targets: list[ModbusWriteConfig], units: dict | None = None) -> dict[str, str]:
    used: dict[tuple, ModbusWriteConfig] = {}
    out = {}
    for t in targets:
        for c in _write_cells(t, units or {}):
            if c in used and used[c].id != t.id:
                out[t.id] = f"{c[2]} {c[3]} on this device is also written by another entry"
                break
        else:
            for c in _write_cells(t, units or {}):
                used[c] = t
    return out


def _write_label(state: RuntimeState, t: ModbusWriteConfig, labels: dict) -> str:
    if t.kind == "alarm":
        rule = next((r for r in state.config.alarm_rules.rules if r.id == t.source_id), None)
        return "Alarm: " + ((rule.name or labels.get(rule.subscription_id, rule.subscription_id)) if rule else t.source_id)
    return labels.get(t.source_id, t.source_id)


def _read_conflict(state: RuntimeState, t: ModbusWriteConfig) -> str:
    """A register the app also reads from this device (writing may overwrite a device value)."""
    srv = next((s for s in state.config.servers.servers if s.id == t.server_id), None)
    if srv is None:
        return ""
    unit = srv.modbus.unit_id if t.unit_id is None else t.unit_id
    mine = {(c[2], c[3]) for c in _write_cells(t, {t.server_id: srv.modbus.unit_id}) if c[1] == unit}
    for s in state.config.subscriptions.subscriptions:
        m = s.modbus
        if s.server_id != t.server_id or m is None or (srv.modbus.unit_id if m.unit_id is None else m.unit_id) != unit:
            continue
        n = 1 if AREAS[m.area][1] else TYPES[m.data_type][0]
        if any((m.area, m.address + i) in mine for i in range(n)):
            return f"also read from this device as “{s.display_name}”: writing changes that value"
    return ""


def _write_json(state: RuntimeState, t: ModbusWriteConfig, labels: dict, bad: dict) -> dict:
    srv = next((s for s in state.config.servers.servers if s.id == t.server_id), None)
    return {**t.model_dump(), "label": _write_label(state, t, labels), "device": srv.display_name if srv else "?",
            "modicon": modicon(t.area, t.address), "problem": bad.get(t.id, ""), "warning": _read_conflict(state, t)}


def _next_free_write(targets: list[ModbusWriteConfig], server_id: str, area: str, count: int = 1,
                     start: int = 0) -> int:
    used = {c[3] for t in targets for c in _write_cells(t, {}) if c[0] == server_id and c[2] == area}
    addr = start
    while any(addr + i in used for i in range(count)):
        addr += 1
    return addr


@router.post("/writes/settings")
async def write_settings(request: Request, state: RuntimeState = Depends(get_state)):
    data = await request.json()
    mw = state.config.outputs.modbus_writes
    if "enabled" in data:
        mw.enabled = bool(data["enabled"])
    if "resend_seconds" in data:
        mw.resend_seconds = max(0, min(86400, int(data.get("resend_seconds") or 0)))
    _save(state)
    return {"enabled": mw.enabled, "resend_seconds": mw.resend_seconds}


@router.post("/writes")
async def save_write(request: Request, state: RuntimeState = Depends(get_state)):
    data = await request.json()
    targets = state.config.outputs.modbus_writes.targets
    old = next((t for t in targets if t.id == data.get("id")), None)
    try:
        clean = _clean({k: v for k, v in data.items() if k != "id"})
        if clean.get("unit_id") in ("", None):
            clean["unit_id"] = None
        else:
            clean["unit_id"] = int(clean["unit_id"])
        t = ModbusWriteConfig(**{**(old.model_dump() if old else {}), **clean, **({"id": old.id} if old else {})})
    except (ValidationError, ValueError) as exc:
        msg = exc.errors()[0]["msg"] if isinstance(exc, ValidationError) else str(exc)
        return JSONResponse({"error": msg}, status_code=400)
    if not any(s.id == t.server_id and s.protocol == "modbus" for s in state.config.servers.servers):
        return JSONResponse({"error": "Choose a device (a Modbus server on the Servers page)"}, status_code=400)
    if t.scale == 0:
        return JSONResponse({"error": "scale must not be 0"}, status_code=400)
    if t.area == "coil":
        t.data_type = "bool"
    elif t.data_type == "bool":
        return JSONResponse({"error": "Registers need a number type"}, status_code=400)
    problem = write_overlaps([x for x in targets if x.id != t.id] + [t]).get(t.id)
    if problem:
        return JSONResponse({"error": problem}, status_code=400)
    if old:
        targets[targets.index(old)] = t
    else:
        targets.append(t)
    _save(state)
    return {"id": t.id}


@router.post("/writes/add")
async def add_writes(request: Request, state: RuntimeState = Depends(get_state)):
    """Many signals (ids) or all alarm rules (alarms: true) to one device, at the next free addresses."""
    data = await request.json()
    server_id = data.get("server_id") or ""
    if not any(s.id == server_id and s.protocol == "modbus" for s in state.config.servers.servers):
        return JSONResponse({"error": "Choose a device"}, status_code=400)
    targets = state.config.outputs.modbus_writes.targets
    live = state.polling_engine.live_values() if state.polling_engine else {}
    subs = {s.id: s for s in state.config.subscriptions.subscriptions}
    # no default: writing to a device's registers at 0 could overwrite anything
    try:
        start_reg = int(data.get("start_register"))
        start_coil = int(data.get("start_coil"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "Enter the first address to write to (registers and coils)"}, status_code=400)
    if not (0 <= start_reg <= 65535 and 0 <= start_coil <= 65535):
        return JSONResponse({"error": "addresses: 0 … 65535"}, status_code=400)
    dtype = data.get("type") if data.get("type") in TYPES and data.get("type") != "bool" else "float32"
    status = bool(data.get("status"))
    if data.get("alarms"):
        items = [("alarm", r.id, True) for r in state.config.alarm_rules.rules]
    else:
        items = [("value", sid, isinstance(live.get(sid), bool) or (subs[sid].modbus is not None and (
                     subs[sid].modbus.bit is not None or AREAS[subs[sid].modbus.area][1])))
                 for sid in data.get("ids") or [] if sid in subs]
    added = 0
    for kind, sid, is_bool in items:
        if any(t.kind == kind and t.source_id == sid and t.server_id == server_id for t in targets):
            continue
        area = "coil" if is_bool else "holding"
        t = ModbusWriteConfig(kind=kind, source_id=sid, server_id=server_id, area=area,
                              data_type="bool" if is_bool else dtype)
        t.address = _next_free_write(targets, server_id, area, 1 if is_bool else TYPES[dtype][0],
                                     start_coil if is_bool else start_reg)
        if status and kind == "value":
            t.status_address = _next_free_write(targets + [t], server_id, "coil", 1, start_coil)
        targets.append(t)
        added += 1
    _save(state)
    return {"added": added}


@router.post("/writes/all")
async def writes_all(request: Request, state: RuntimeState = Depends(get_state)):
    data = await request.json()
    on, ids = bool(data.get("enabled")), set(data.get("ids") or [])
    n = 0
    for t in state.config.outputs.modbus_writes.targets:
        if (not ids or t.id in ids) and t.enabled != on:
            t.enabled = on
            n += 1
    _save(state)
    return {"changed": n}


@router.post("/writes/{write_id}/toggle")
async def write_toggle(write_id: str, request: Request, state: RuntimeState = Depends(get_state)):
    data = await request.json()
    for t in state.config.outputs.modbus_writes.targets:
        if t.id == write_id:
            t.enabled = bool(data.get("enabled"))
            _save(state)
            return {"enabled": t.enabled}
    return JSONResponse({"error": "not found"}, status_code=404)


@router.post("/writes/{write_id}/delete")
async def write_delete(write_id: str, state: RuntimeState = Depends(get_state)):
    mw = state.config.outputs.modbus_writes
    mw.targets = [t for t in mw.targets if t.id != write_id]
    _save(state)
    return {"ok": True}


@router.get("/values")
async def values(state: RuntimeState = Depends(get_state)):
    svc = state.modbus_output
    if svc is None:
        return {"status": {}, "points": {}}
    out = {}
    for p in state.config.outputs.points:
        v = svc.values.get(p.id)
        rec = svc.received.get(p.id)
        valid = bool(svc.bank.discrete.get(p.status_address)) if p.status_address is not None else None
        out[p.id] = {"value": float(f"{v:.6g}") if isinstance(v, float) else v, "valid": valid,
                     "written": rec[1].isoformat() if rec else None}
    writes = {}
    wr = state.modbus_writer
    for t in state.config.outputs.modbus_writes.targets:
        r = wr.results.get(t.id) if wr else None
        v = r.value if r else None
        writes[t.id] = {"value": float(f"{v:.6g}") if isinstance(v, float) else v,
                        "at": r.at.isoformat() if r and r.at else None, "error": r.error if r else "",
                        "status": r.status if r else None}
    return {"status": svc.status(), "points": out, "writes": writes,
            "writing": state.config.outputs.modbus_writes.enabled}


@router.get("/export.csv")
async def export_csv(state: RuntimeState = Depends(get_state)):
    """The register map for whoever programs the reading side (PLC, SCADA)."""
    ms = state.config.outputs.modbus_server
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    from app.references.capture import signal_labels
    labels = signal_labels(state)
    w.writerow(["name", "kind", "access", "area", "address", "modicon", "type", "order", "registers", "scale",
                "status_discrete_input", "status_modicon", "enabled"])
    for p in sorted(state.config.outputs.points, key=lambda p: (list(AREAS).index(p.area), p.address)):
        t = point_type(p)
        name = labels.get(p.source_id) if p.kind == "value" and not p.name else None
        w.writerow([name or label_of(p, state), p.kind, "write" if p.kind == "received" else "read", p.area, p.address,
                    modicon(p.area, p.address), t, "" if t == "bool" or TYPES[t][0] == 1 else (p.order or ms.order),
                    register_count(p.area, t), "" if t == "bool" else f"{p.scale:g}",
                    "" if p.status_address is None else p.status_address,
                    "" if p.status_address is None else modicon("discrete", p.status_address),
                    1 if p.enabled else 0])
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="modbus_register_map.csv"'})
