"""
Periodic scanner for dynamic folder subscriptions.

For each DynamicFolderConfig with enabled=True whose server is connected,
the scanner re-browses the folder at the configured interval and:
- Creates new SubscriptionConfig for nodes not yet managed
- Disables (does not delete) managed subs whose node vanished
- Re-enables managed subs whose node returned
"""
from __future__ import annotations

import asyncio
import logging
import time

from asyncua import ua

from app.config import (
    DynamicFolderConfig, PollingConfig, ServerPollingDefaults,
    SubscriptionConfig, SubscriptionInfluxConfig, SubTagActivation,
    save_subscriptions,
)
from app.opcua.browser import _NODE_CLASS, _DATATYPE_NAMES, _browse_paged, _resolve_browse_client

logger = logging.getLogger(__name__)

_TICK_SECONDS = 15
# Background scans run infrequently and never block other requests, so a
# generous ceiling only guards against a truly stuck/unresponsive session.
_SCAN_TIMEOUT_SECONDS = 180


def _browse_path_to_measurement(browse_path: str) -> str:
    parts = [p for p in browse_path.split("/") if p]
    return ".".join(parts) if parts else "opcua_value"


async def _scan_children(mgr, server_id: str, node_id: str | None) -> tuple[list[dict], bool]:
    """Fetch one node's children using small, individual per-node attribute
    requests, but a *page-capped* Browse to get the child list itself.

    Root cause found: the true hang was never the attribute reads — it was
    the initial Browse itself. Observed on a WAGO PFC300: browsing a folder
    with >100 children forces ContinuationPoint pagination, which either
    hangs indefinitely or (confirmed once, after 73 minutes) loops
    accumulating millions of duplicate references without the server ever
    signalling completion. Folders with <=100 children need no pagination
    and always complete normally (proven repeatedly on this PLC's "Modbus"
    group). _browse_paged enforces a hard page cap and reports truncation
    instead of ever blocking on/trusting server-side pagination completeness.

    Returns (children, truncated). truncated=True means the list is incomplete
    (page cap reached, browse error or failed attribute reads) — callers MUST
    NOT treat the result as a complete/authoritative child list in that case.
    """
    client = await _resolve_browse_client(mgr, server_id)
    if client is None:
        return [], True
    if node_id is None:
        parent = client.nodes.objects
    else:
        parent = client.get_node(node_id)
    try:
        refs, truncated = await _browse_paged(client, parent)
    except Exception as exc:
        logger.warning("FolderScanner: browse error server=%s node=%s: %s", server_id, node_id, exc)
        discard = getattr(mgr, "discard_browse_client", None)
        if discard:
            discard(server_id)
        return [], True

    # Node.read_attributes()/read_data_type()/get_children() all log at DEBUG
    # inside asyncua, so at INFO level a slow scan is invisible progress-wise —
    # log a heartbeat every 25 nodes so a stuck vs. slow-but-moving scan is
    # distinguishable from the app log alone.
    total = len(refs)
    result: list[dict] = []
    for i, ref in enumerate(refs):
        if i and i % 25 == 0:
            logger.info("FolderScanner: ...%d/%d nodes read (%s)", i, total, node_id or "root")
        n = client.get_node(ref.NodeId)
        try:
            attrs = await n.read_attributes([
                ua.AttributeIds.BrowseName,
                ua.AttributeIds.DisplayName,
                ua.AttributeIds.NodeClass,
            ])
            browse_name_val = attrs[0].Value.Value
            display_name_val = attrs[1].Value.Value
            node_class_val = attrs[2].Value.Value
            browse_name = str(browse_name_val.Name) if browse_name_val else ""
            display_name = str(display_name_val.Text) if display_name_val else browse_name
            node_class = _NODE_CLASS.get(node_class_val, "Unknown")
        except Exception as exc:
            logger.debug("FolderScanner: attribute read failed for %s: %s", n.nodeid, exc)
            truncated = True  # result is incomplete — callers must not treat it as authoritative
            continue

        data_type: str | None = None
        has_children = False
        if node_class == "Variable":
            try:
                dt_node_id = await n.read_data_type()
                data_type = _DATATYPE_NAMES.get(dt_node_id.Identifier, str(dt_node_id.Identifier))
            except Exception:
                data_type = "?"
            if data_type not in _DATATYPE_NAMES.values():
                # Possibly a struct exposing scalar members as browsable children.
                try:
                    struct_refs, _ = await _browse_paged(client, n, page_size=1)
                    has_children = len(struct_refs) > 0
                except Exception:
                    has_children = False

        result.append({
            "node_id": n.nodeid.to_string(),
            "browse_name": browse_name,
            "display_name": display_name,
            "node_class": node_class,
            "data_type": data_type,
            "has_children": has_children,
        })
    return result, truncated


async def _get_all_variables(
    mgr, server_id: str, node_id: str, browse_path: str, allowed_types: set[str]
) -> tuple[list[dict], bool]:
    """Recursively collect variable nodes as dicts under the given folder.

    Variables whose data_type isn't in allowed_types are skipped — this keeps
    structs/arrays/custom types (which never appear in allowed_types, since it
    only lists known scalar names) and any explicitly-deselected scalar types
    from ever getting a subscription created for them.

    Returns (variables, truncated). truncated=True if this folder or any
    subfolder visited had more children than the Browse page cap could
    retrieve — callers must not treat the result as complete.
    """
    result: list[dict] = []
    children, truncated = await _scan_children(mgr, server_id, node_id)
    for child in children:
        child_path = f"{browse_path}/{child['browse_name']}" if child["browse_name"] else browse_path
        if child["node_class"] == "Variable" and child["has_children"]:
            # Structured variable (e.g. a PLC struct whose Value is one ExtensionObject)
            # that also exposes its members as browsable child Variables — recurse into
            # those instead of subscribing to the (unsupported) struct value itself.
            sub, sub_truncated = await _get_all_variables(
                mgr, server_id, child["node_id"], child_path, allowed_types
            )
            result.extend(sub)
            truncated = truncated or sub_truncated
        elif child["node_class"] == "Variable":
            if child["data_type"] not in allowed_types:
                continue
            result.append({
                "node_id": child["node_id"],
                "browse_path": child_path,
                "display_name": child["display_name"],
            })
        elif child["node_class"] in ("Object", "View"):
            sub, sub_truncated = await _get_all_variables(
                mgr, server_id, child["node_id"], child_path, allowed_types
            )
            result.extend(sub)
            truncated = truncated or sub_truncated
    return result, truncated


def _default_active_tags(state) -> list[SubTagActivation]:
    return [
        SubTagActivation(tag_id=t.id, active=True)
        for t in state.config.tags.tags
        if t.default_active
    ]


class FolderScanner:
    def __init__(self, state) -> None:
        self._state = state
        self._last_scan: dict[str, float] = {}   # folder_id → epoch seconds
        self._scanning: set[str] = set()          # folder_id → scan in flight

    async def run(self) -> None:
        logger.info("FolderScanner started")
        try:
            while True:
                await asyncio.sleep(_TICK_SECONDS)
                await self._scan_due()
        except asyncio.CancelledError:
            logger.info("FolderScanner stopped")
            return

    async def scan_now(self, folder_id: str) -> None:
        """Force an immediate scan for one specific watcher (used on creation)."""
        watcher = next(
            (f for f in self._state.config.dynamic_folders.folders if f.id == folder_id),
            None,
        )
        if watcher:
            await self._scan_folder(watcher)

    async def _scan_due(self) -> None:
        now = time.monotonic()
        for watcher in list(self._state.config.dynamic_folders.folders):
            if not watcher.enabled:
                continue
            mgr = self._state.opcua_manager
            if mgr is None or not mgr.is_connected(watcher.server_id):
                continue
            last = self._last_scan.get(watcher.id, 0.0)
            if now - last < watcher.rescan_interval_seconds:
                continue
            await self._scan_folder(watcher)

    async def _scan_folder(self, watcher: DynamicFolderConfig) -> None:
        # Guard against pile-up: if a previous scan of this watcher is still
        # stuck waiting on the OPC UA server, a new click/tick must not start
        # a second one competing for the same session.
        if watcher.id in self._scanning:
            logger.info("FolderScanner: scan already in progress for %s, skipping", watcher.folder_browse_path)
            return
        self._scanning.add(watcher.id)
        mgr = self._state.opcua_manager
        state = self._state
        allowed_types = set(state.config.app_settings.dynamic_folder_allowed_types)
        scan_started = time.monotonic()
        try:
            current_nodes, truncated = await asyncio.wait_for(
                _get_all_variables(
                    mgr, watcher.server_id, watcher.folder_node_id, watcher.folder_browse_path,
                    allowed_types,
                ),
                timeout=_SCAN_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "FolderScanner: browse timed out after %ss for %s — server unresponsive or "
                "session degraded (e.g. exhausted continuation points); will retry next cycle",
                _SCAN_TIMEOUT_SECONDS, watcher.folder_browse_path,
            )
            # The stuck request may still resolve late on the server side (a
            # "Future ... already done" warning shows up if it does) — don't
            # reuse this session for the retry, start the next attempt fresh.
            discard = getattr(mgr, "discard_browse_client", None)
            if discard:
                discard(watcher.server_id)
            return
        except Exception as exc:
            logger.warning("FolderScanner: browse failed for %s: %s", watcher.folder_browse_path, exc)
            return
        finally:
            self._scanning.discard(watcher.id)

        logger.info(
            "FolderScanner: browsed %s (%d nodes%s) in %.1fs",
            watcher.folder_browse_path, len(current_nodes),
            ", INCOMPLETE — page cap reached or read errors" if truncated else "",
            time.monotonic() - scan_started,
        )

        self._last_scan[watcher.id] = time.monotonic()

        current_map: dict[str, dict] = {n["node_id"]: n for n in current_nodes}
        managed: dict[str, SubscriptionConfig] = {
            s.node_id: s
            for s in state.config.subscriptions.subscriptions
            if s.source_folder_id == watcher.id
        }
        existing_ids = {
            s.node_id for s in state.config.subscriptions.subscriptions
            if s.server_id == watcher.server_id
        }

        changed = False

        # New nodes
        for node_id, node in current_map.items():
            if node_id in managed:
                # Already managed — re-enable if it was disabled
                sub = managed[node_id]
                if not sub.enabled:
                    sub.enabled = True
                    changed = True
                    logger.info("FolderScanner: re-enabled %s", node["browse_path"])
            elif node_id not in existing_ids:
                # Brand new node — subscribe it
                srv = next((s for s in state.config.servers.servers if s.id == watcher.server_id), None)
                defaults = srv.polling_defaults if srv else ServerPollingDefaults()
                new_sub = SubscriptionConfig(
                    server_id=watcher.server_id,
                    node_id=node_id,
                    browse_path=node["browse_path"],
                    display_name=node["display_name"],
                    source_folder_id=watcher.id,
                    influxdb=SubscriptionInfluxConfig(
                        measurement=_browse_path_to_measurement(node["browse_path"]),
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
                state.config.subscriptions.subscriptions.append(new_sub)
                changed = True
                logger.info("FolderScanner: added %s", node["browse_path"])

        # Vanished nodes — never disable based on a truncated (incomplete)
        # scan result, or nodes past the page cap would be wrongly marked as
        # gone even though they were simply never fetched this cycle.
        if truncated:
            logger.warning(
                "FolderScanner: scan of %s was truncated — skipping vanished-node "
                "detection this cycle to avoid disabling still-existing subscriptions",
                watcher.folder_browse_path,
            )
        else:
            for node_id, sub in managed.items():
                if node_id not in current_map and sub.enabled:
                    sub.enabled = False
                    changed = True
                    logger.info("FolderScanner: disabled %s (node vanished)", sub.browse_path)

        if changed:
            save_subscriptions(state.config.subscriptions, state.config_dir)
            if state.polling_engine:
                await state.polling_engine.reconcile()
