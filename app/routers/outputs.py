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

from app.config import ModbusServerOutputConfig, OutputPointConfig, save_outputs
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


def _point_json(state: RuntimeState, p: OutputPointConfig, bad: dict) -> dict:
    return {**p.model_dump(), "label": label_of(p, state), "modicon": modicon(p.area, p.address),
            "count": register_count(p.area, point_type(p)), "problem": bad.get(p.id, "")}


@router.get("", response_class=HTMLResponse)
async def page(request: Request, state: RuntimeState = Depends(get_state)):
    out = state.config.outputs
    bad = overlaps(out.points)
    subs = state.config.subscriptions.subscriptions
    from app.references.capture import signal_label
    return templates.TemplateResponse(request, "outputs.html", {
        "request": request, "ms": out.modbus_server,
        "points": [_point_json(state, p, bad) for p in out.points],
        "subs": [{"id": s.id, "label": signal_label(state, s.id), "measurement": s.influxdb.measurement,
                  "enabled": s.enabled} for s in subs],
        "rules": [{"id": r.id, "label": r.name or signal_label(state, r.subscription_id)}
                  for r in state.config.alarm_rules.rules],
        "status": state.modbus_output.status() if state.modbus_output else {},
        "types": list(TYPES),
    })


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
    return {"status": svc.status(), "points": out}


@router.get("/export.csv")
async def export_csv(state: RuntimeState = Depends(get_state)):
    """The register map for whoever programs the reading side (PLC, SCADA)."""
    ms = state.config.outputs.modbus_server
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["name", "kind", "access", "area", "address", "modicon", "type", "order", "registers", "scale",
                "status_discrete_input", "status_modicon", "enabled"])
    for p in sorted(state.config.outputs.points, key=lambda p: (list(AREAS).index(p.area), p.address)):
        t = point_type(p)
        w.writerow([label_of(p, state), p.kind, "write" if p.kind == "received" else "read", p.area, p.address,
                    modicon(p.area, p.address), t, "" if t == "bool" or TYPES[t][0] == 1 else (p.order or ms.order),
                    register_count(p.area, t), "" if t == "bool" else f"{p.scale:g}",
                    "" if p.status_address is None else p.status_address,
                    "" if p.status_address is None else modicon("discrete", p.status_address),
                    1 if p.enabled else 0])
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="modbus_register_map.csv"'})
