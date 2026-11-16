"""
ModbusManager — one TCP connection per enabled Modbus server.

Offers the same read interface as the OPC UA manager (is_connected,
read_node_values, …) so the polling engine treats Modbus registers like OPC UA
nodes. Node ids are the subscriptions' Modbus point keys (ModbusPointConfig.node_id).
Reads are grouped into blocks; a block the device refuses (Modbus exception)
is read point by point from then on, so one bad address costs only its value.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from app.config import ModbusPointConfig, ServerConfig
from app.modbus.client import ModbusError, ModbusTcpClient, parse_endpoint
from app.modbus.codec import AREAS, Block, Point, plan_blocks, point_value
from app.state import ServerStatus

if TYPE_CHECKING:
    from app.state import RuntimeState

logger = logging.getLogger(__name__)


def make_point(srv: ServerConfig, mp: ModbusPointConfig) -> Point:
    return Point(unit=srv.modbus.unit_id if mp.unit_id is None else mp.unit_id, area=mp.area,
                 address=mp.address, data_type="bool" if AREAS[mp.area][1] else mp.data_type,
                 order=mp.order or srv.modbus.order, bit=mp.bit, scale=mp.scale, offset=mp.offset)


class ModbusManager:
    def __init__(self, state: "RuntimeState") -> None:
        self._state = state
        self._clients: dict[str, ModbusTcpClient] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._conn_keys: dict[str, tuple] = {}
        self._split: dict[str, set[tuple]] = {}       # server → blocks read point by point
        self._bad: dict[str, dict[str, str]] = {}      # server → node_id → last error

    # ------------------------------------------------------------------
    def servers(self) -> list[ServerConfig]:
        return [s for s in self._state.config.servers.servers if s.protocol == "modbus"]

    def is_modbus(self, server_id: str) -> bool:
        return any(s.id == server_id for s in self.servers())

    @staticmethod
    def _conn_key(srv: ServerConfig) -> tuple:
        return (srv.endpoint_url, srv.modbus.timeout_ms)

    async def reconcile(self) -> None:
        wanted = {s.id: s for s in self.servers() if s.enabled}
        for sid in list(self._tasks):
            if sid not in wanted or self._conn_keys.get(sid) != self._conn_key(wanted[sid]):
                await self._stop(sid)
        for sid, srv in wanted.items():
            if sid not in self._tasks:
                self._conn_keys[sid] = self._conn_key(srv)
                self._state.server_status[sid] = ServerStatus()
                self._tasks[sid] = asyncio.create_task(self._connect_loop(srv), name=f"modbus-connect-{sid}")
        for sid in list(self._split):                  # settings may have changed: plan anew
            self._split[sid].clear()

    async def _stop(self, sid: str) -> None:
        task = self._tasks.pop(sid, None)
        if task:
            task.cancel()
        client = self._clients.pop(sid, None)
        if client:
            await client.close()
        self._conn_keys.pop(sid, None)
        self._split.pop(sid, None)
        self._bad.pop(sid, None)
        self._state.server_status.pop(sid, None)

    async def stop_all(self) -> None:
        for sid in list(self._tasks):
            await self._stop(sid)

    async def _connect_loop(self, srv: ServerConfig) -> None:
        status = self._state.server_status[srv.id]
        host, port = parse_endpoint(srv.endpoint_url)
        while True:
            try:
                client = ModbusTcpClient(host, port, timeout=max(0.2, srv.modbus.timeout_ms / 1000))
                await client.connect()
                self._clients[srv.id] = client
                status.connected = True
                status.last_error = None
                status.reconnect_attempts = 0
                status.last_connected = datetime.now(timezone.utc)
                logger.info("Modbus connected: %s (%s:%s)", srv.display_name, host, port)
                await client.closed.wait()               # a failed request closes the connection
                raise ConnectionError(getattr(client, "last_error", "") or "connection closed")
            except asyncio.CancelledError:
                return
            except Exception as exc:
                text = str(exc) or type(exc).__name__
                if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
                    text = f"no connection to {host}:{port} (timeout)"
                status.connected = False
                status.last_error = text
                status.reconnect_attempts += 1
                self._clients.pop(srv.id, None)
                logger.warning("Modbus connection failed for %s: %s — retry in %ds",
                               srv.display_name, text, srv.reconnect_interval_seconds)
                try:
                    await asyncio.sleep(max(1, srv.reconnect_interval_seconds))
                except asyncio.CancelledError:
                    return

    # ------------------------------------------------------------------
    # Read interface (same as OpcUaClientManager)
    # ------------------------------------------------------------------
    def is_connected(self, server_id: str) -> bool:
        client = self._clients.get(server_id)
        status = self._state.server_status.get(server_id)
        return client is not None and client.connected and status is not None and status.connected

    def _points(self, srv: ServerConfig, node_ids: list[str]) -> list[Optional[Point]]:
        by_node = {s.node_id: s.modbus for s in self._state.config.subscriptions.subscriptions
                   if s.server_id == srv.id and s.modbus is not None}
        return [make_point(srv, by_node[n]) if by_node.get(n) else None for n in node_ids]

    async def read_node_values(self, server_id: str, node_ids: list[str]) -> list[Any]:
        srv = next((s for s in self.servers() if s.id == server_id), None)
        if srv is None:
            raise ConnectionError("not a Modbus server")
        return await self.read_points(server_id, self._points(srv, node_ids), srv)

    async def read_points(self, server_id: str, points: list[Optional[Point]],
                          srv: Optional[ServerConfig] = None) -> list[Any]:
        """Values in order; None for a point that could not be read."""
        client = self._clients.get(server_id)
        if client is None or not client.connected:
            raise ConnectionError("not connected")
        srv = srv or next(s for s in self.servers() if s.id == server_id)
        values: list[Any] = [None] * len(points)
        idx = [i for i, p in enumerate(points) if p is not None]
        real = [points[i] for i in idx]
        split = self._split.setdefault(server_id, set())
        bad = self._bad.setdefault(server_id, {})
        for block in plan_blocks(real, srv.modbus.max_registers, srv.modbus.max_gap):
            key = (block.unit, block.area, block.start, block.count)
            if key not in split:
                try:
                    data = await client.read(block.unit, AREAS[block.area][0], block.start, block.count)
                    for j in block.points:
                        values[idx[j]] = self._value(real[j], block.start, data, bad)
                    continue
                except ModbusError as exc:
                    if len(block.points) == 1:
                        bad[self._key(real[block.points[0]])] = str(exc)
                        continue
                    split.add(key)                    # read point by point from now on
                    logger.info("Modbus %s: block %s %s+%s refused (%s), reading points one by one",
                                srv.display_name, block.area, block.start, block.count, exc)
            for j in block.points:                    # point by point
                p = real[j]
                try:
                    data = await client.read(p.unit, AREAS[p.area][0], p.address, p.count)
                    values[idx[j]] = self._value(p, p.address, data, bad)
                except ModbusError as exc:
                    bad[self._key(p)] = str(exc)
        return values

    @staticmethod
    def _key(p: Point) -> str:
        return f"{p.unit}/{p.area}/{p.address}"

    def _value(self, p: Point, start: int, data: list, bad: dict) -> Any:
        bad.pop(self._key(p), None)
        try:
            return point_value(p, start, data)
        except Exception as exc:
            bad[self._key(p)] = f"decode: {exc}"
            return None

    def point_errors(self, server_id: str) -> dict[str, str]:
        """'unit/area/address' → last error, for the register page."""
        return dict(self._bad.get(server_id, {}))

    async def read_node_value(self, server_id: str, node_id: str) -> Any:
        return (await self.read_node_values(server_id, [node_id]))[0]

    async def read_node_attributes(self, server_id: str, node_id: str) -> dict[str, Any]:
        return {}
