"""
OpcUaClientManager — manages asyncua Client connections per configured server.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from asyncua import Client, ua
from asyncua.common.node import Node

from app.config import ServerConfig, ServerAuth, resolve_env_strict
from app.state import RuntimeState, ServerStatus

logger = logging.getLogger(__name__)


class OpcUaClientManager:
    def __init__(self, state: RuntimeState) -> None:
        self._state = state
        self._clients: dict[str, Client] = {}
        self._reconnect_tasks: dict[str, asyncio.Task] = {}
        # server_id → node_id → Node. Node objects hold a reference to the
        # client session, so the per-server cache must be dropped whenever
        # that client is replaced or disconnected.
        self._node_cache: dict[str, dict[str, Node]] = {}
        self._conn_keys: dict[str, tuple] = {}
        # Separate, lazily-connected session used only for browsing (Browser
        # tab tree, dynamic folder scans). Keeping this off the main session
        # means a slow/stuck Browse call can never starve or be starved by
        # the continuous polling traffic that also runs on the main session —
        # mirrors how a standalone OPC UA browser tool (e.g. Prosys) connects
        # separately from any client that's actively polling the server.
        self._browse_clients: dict[str, Client] = {}
        self._browse_locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start_all(self) -> None:
        for srv in self._state.config.servers.servers:
            if srv.enabled:
                self._conn_keys[srv.id] = self._conn_key(srv)
                self._state.server_status[srv.id] = ServerStatus()
                self._reconnect_tasks[srv.id] = asyncio.create_task(
                    self._connect_loop(srv), name=f"opcua-connect-{srv.id}"
                )

    async def stop_all(self) -> None:
        for task in self._reconnect_tasks.values():
            task.cancel()
        for client in self._clients.values():
            try:
                await client.disconnect()
            except Exception:
                pass
        self._clients.clear()
        self._reconnect_tasks.clear()
        self._node_cache.clear()
        for client in self._browse_clients.values():
            try:
                await client.disconnect()
            except Exception:
                pass
        self._browse_clients.clear()
        self._browse_locks.clear()

    @staticmethod
    def _conn_key(srv: ServerConfig) -> tuple:
        return (srv.endpoint_url, srv.security_mode, srv.security_policy,
                srv.auth.model_dump(), srv.session_timeout_ms)

    async def reconcile(self) -> None:
        """Called when server config changes. Adds new, removes deleted, restarts changed servers."""
        configured = {s.id: s for s in self._state.config.servers.servers if s.enabled}
        current_ids = set(self._reconnect_tasks.keys())
        changed = {
            sid for sid in current_ids & set(configured)
            if self._conn_keys.get(sid) != self._conn_key(configured[sid])
        }

        for sid in (current_ids - set(configured)) | changed:
            self._conn_keys.pop(sid, None)
            self._reconnect_tasks[sid].cancel()
            self._reconnect_tasks.pop(sid, None)
            client = self._clients.pop(sid, None)
            self._node_cache.pop(sid, None)
            if client:
                try:
                    await client.disconnect()
                except Exception:
                    pass
            browse_client = self._browse_clients.pop(sid, None)
            self._browse_locks.pop(sid, None)
            if browse_client:
                try:
                    await browse_client.disconnect()
                except Exception:
                    pass
            self._state.server_status.pop(sid, None)

        for srv in self._state.config.servers.servers:
            if srv.enabled and srv.id not in self._reconnect_tasks:
                self._conn_keys[srv.id] = self._conn_key(srv)
                self._state.server_status[srv.id] = ServerStatus()
                self._reconnect_tasks[srv.id] = asyncio.create_task(
                    self._connect_loop(srv), name=f"opcua-connect-{srv.id}"
                )

    # ------------------------------------------------------------------
    # Connection loop
    # ------------------------------------------------------------------

    async def _connect_loop(self, srv: ServerConfig) -> None:
        status = self._state.server_status[srv.id]
        while True:
            try:
                client = await self._make_client(srv)
                await client.connect()
                self._node_cache.pop(srv.id, None)
                self._clients[srv.id] = client
                status.connected = True
                status.last_error = None
                status.reconnect_attempts = 0
                from datetime import datetime, timezone
                status.last_connected = datetime.now(timezone.utc)
                logger.info("OPC UA connected: %s (%s)", srv.display_name, srv.endpoint_url)
                # Stay alive — wait until disconnected
                await self._wait_until_disconnected(srv.id, client)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                import re as _re
                err_str = str(exc)
                if "Multiple exceptions:" in err_str:
                    m = _re.search(r'\[(?:Errno|WinError) \d+\][^,)\]]+', err_str)
                    err_str = m.group(0).strip() if m else "Connection failed"
                status.connected = False
                status.last_error = err_str
                status.reconnect_attempts += 1
                logger.warning(
                    "OPC UA connection failed for %s: %s — retry in %ds",
                    srv.display_name, exc, srv.reconnect_interval_seconds
                )
                self._clients.pop(srv.id, None)
                self._node_cache.pop(srv.id, None)
                try:
                    await asyncio.sleep(srv.reconnect_interval_seconds)
                except asyncio.CancelledError:
                    return

    async def _wait_until_disconnected(self, server_id: str, client: Client) -> None:
        """Polls session status; returns when connection is lost."""
        while True:
            try:
                await asyncio.sleep(5)
                await client.get_namespace_index("http://opcfoundation.org/UA/")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                status = self._state.server_status.get(server_id)
                if status:
                    status.connected = False
                    status.last_error = str(exc)
                self._clients.pop(server_id, None)
                self._node_cache.pop(server_id, None)
                logger.warning("OPC UA session lost for %s: %s", server_id, exc)
                return

    async def _make_client(self, srv: ServerConfig) -> Client:
        client = Client(
            url=srv.endpoint_url,
            timeout=srv.session_timeout_ms / 1000,
        )
        auth = srv.auth
        if srv.security_mode != "None" or auth.type == "certificate":
            if not auth.certificate_path or not auth.private_key_path:
                raise ValueError(
                    f"Security mode '{srv.security_mode}' / auth '{auth.type}' needs a "
                    f"client certificate and private key path"
                )
        if srv.security_mode != "None":
            await client.set_security_string(
                f"{srv.security_policy},{srv.security_mode},"
                f"{auth.certificate_path},{auth.private_key_path}"
            )
        if auth.type == "username":
            client.set_user(resolve_env_strict(auth.username))
            client.set_password(resolve_env_strict(auth.password))
        elif auth.type == "certificate":
            await client.load_client_certificate(auth.certificate_path)
            await client.load_private_key(auth.private_key_path)
        return client

    # ------------------------------------------------------------------
    # Browse / read API
    # ------------------------------------------------------------------

    def get_client(self, server_id: str) -> Client | None:
        return self._clients.get(server_id)

    async def get_browse_client(self, server_id: str) -> Client | None:
        """Return the dedicated browse-only session for a server, connecting
        it lazily on first use. Falls back to the main (polling) session if
        a fresh connection can't be established (e.g. the PLC caps
        concurrent sessions) — browsing still works, just without the
        isolation benefit.
        """
        client = self._browse_clients.get(server_id)
        if client is not None:
            return client

        lock = self._browse_locks.setdefault(server_id, asyncio.Lock())
        async with lock:
            # Re-check: another caller may have connected while we waited for the lock.
            client = self._browse_clients.get(server_id)
            if client is not None:
                return client

            srv = next((s for s in self._state.config.servers.servers if s.id == server_id), None)
            if srv is None:
                return self._clients.get(server_id)
            try:
                client = await self._make_client(srv)
                await client.connect()
                self._browse_clients[server_id] = client
                return client
            except Exception as exc:
                logger.warning(
                    "Browse session connect failed for %s: %s — falling back to main session",
                    srv.display_name, exc,
                )
                return self._clients.get(server_id)

    def discard_browse_client(self, server_id: str) -> None:
        """Drop the browse session so the next get_browse_client() call
        reconnects — self-heal after a failed/stuck browse, no dedicated
        keepalive loop needed since it's only used on demand."""
        self._browse_clients.pop(server_id, None)

    def is_connected(self, server_id: str) -> bool:
        return server_id in self._clients and (
            self._state.server_status.get(server_id, ServerStatus()).connected
        )

    async def browse(self, server_id: str, node_id: str | None = None) -> list[Node]:
        client = self._require_client(server_id)
        if node_id is None:
            return await client.nodes.objects.get_children()
        node = client.get_node(node_id)
        return await node.get_children()

    def _get_cached_node(self, server_id: str, node_id: str, client: Client) -> Node:
        per_server = self._node_cache.setdefault(server_id, {})
        node = per_server.get(node_id)
        if node is None:
            node = client.get_node(node_id)
            per_server[node_id] = node
        return node

    async def read_node_value(self, server_id: str, node_id: str) -> Any:
        client = self._require_client(server_id)
        node = self._get_cached_node(server_id, node_id, client)
        return await node.read_value()

    # PLC stacks enforce hard OperationLimits (WAGO PFC300: MaxNodesPerRead=100)
    # and reject larger requests with BadTooManyOperations.
    _READ_CHUNK = 100

    async def read_node_values(self, server_id: str, node_ids: list[str]) -> list[Any]:
        """Read many node values in as few OPC UA Read service calls as possible.

        Returns one entry per node id, in order; nodes with a bad status
        code yield None instead of raising.
        """
        client = self._require_client(server_id)
        nodes = [self._get_cached_node(server_id, nid, client) for nid in node_ids]
        values: list[Any] = []
        for start in range(0, len(nodes), self._READ_CHUNK):
            values.extend(await client.read_values(nodes[start:start + self._READ_CHUNK]))
        return values

    async def read_node_attributes(self, server_id: str, node_id: str) -> dict[str, Any]:
        client = self._require_client(server_id)
        node = client.get_node(node_id)
        attrs = await node.read_attributes([
            ua.AttributeIds.NodeId,
            ua.AttributeIds.BrowseName,
            ua.AttributeIds.DisplayName,
            ua.AttributeIds.Description,
            ua.AttributeIds.NodeClass,
            ua.AttributeIds.DataType,
        ])
        result = {}
        names = ["NodeId", "BrowseName", "DisplayName", "Description", "NodeClass", "DataType"]
        for name, dv in zip(names, attrs):
            try:
                result[name] = str(dv.Value.Value) if dv.Value.Value is not None else ""
            except Exception:
                result[name] = ""
        return result

    async def get_node_browse_path(self, server_id: str, node_id: str) -> str | None:
        """Return the node's *current* browse path, normalized to the same format
        subscriptions store (leading '/', relative to the Objects root, no namespace
        prefixes), e.g. '/Factory/Site_A/Line_0/Status'.

        Returns None if the path cannot be determined (e.g. broken references), so
        callers can distinguish "can't verify" from a confirmed mismatch. Raises if
        the node id does not exist on the server.
        """
        client = self._require_client(server_id)
        node = client.get_node(node_id)
        # get_path returns nodes from Root down to (and including) this node.
        # as_string=True yields qualified browse names like '0:Root', '2:Tank_0'.
        path_parts = await node.get_path(as_string=True)
        if not path_parts:
            return None
        names: list[str] = []
        for part in path_parts:
            # Strip a leading 'ns:' qualifier (e.g. '2:Tank_0' -> 'Tank_0').
            name = part.split(":", 1)[1] if ":" in part and part.split(":", 1)[0].isdigit() else part
            names.append(name)
        # Drop everything up to and including the standard 'Objects' folder, since
        # stored browse paths are relative to Objects' children.
        if "Objects" in names:
            names = names[names.index("Objects") + 1:]
        return "/" + "/".join(names)

    def _require_client(self, server_id: str) -> Client:
        client = self._clients.get(server_id)
        if client is None:
            raise ConnectionError(f"Server '{server_id}' is not connected")
        return client
