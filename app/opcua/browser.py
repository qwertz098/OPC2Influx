"""
OPC UA browser helpers — convert asyncua nodes into serializable NodeInfo objects.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from asyncua import ua
from asyncua.common.node import Node

logger = logging.getLogger(__name__)

# Map UA NodeClass int to string
_NODE_CLASS = {
    ua.NodeClass.Object: "Object",
    ua.NodeClass.Variable: "Variable",
    ua.NodeClass.Method: "Method",
    ua.NodeClass.ObjectType: "ObjectType",
    ua.NodeClass.VariableType: "VariableType",
    ua.NodeClass.ReferenceType: "ReferenceType",
    ua.NodeClass.DataType: "DataType",
    ua.NodeClass.View: "View",
}

# DataType NodeId → friendly name (common ones)
_DATATYPE_NAMES: dict[int, str] = {
    1: "Boolean", 2: "SByte", 3: "Byte", 4: "Int16", 5: "UInt16",
    6: "Int32", 7: "UInt32", 8: "Int64", 9: "UInt64",
    10: "Float", 11: "Double", 12: "String", 13: "DateTime",
}


@dataclass
class NodeInfo:
    node_id: str
    browse_name: str
    display_name: str
    description: str
    node_class: str
    data_type: str | None
    value: Any | None
    has_children: bool
    server_id: str = ""
    browse_path: str = ""


async def get_node_info(
    node: Node, server_id: str = "", browse_path: str = "", include_value: bool = True,
) -> NodeInfo:
    """Fetch all display attributes for a single node.

    include_value=False skips the Value read for Variable nodes — see
    _children_bulk for why that matters for background folder scanning.
    """
    try:
        attrs = await node.read_attributes([
            ua.AttributeIds.NodeId,
            ua.AttributeIds.BrowseName,
            ua.AttributeIds.DisplayName,
            ua.AttributeIds.Description,
            ua.AttributeIds.NodeClass,
        ])
        node_id_val = attrs[0].Value.Value
        browse_name_val = attrs[1].Value.Value
        display_name_val = attrs[2].Value.Value
        description_val = attrs[3].Value.Value
        node_class_val = attrs[4].Value.Value

        node_id_str = node_id_val.to_string() if node_id_val else str(node.nodeid)
        browse_name = str(browse_name_val.Name) if browse_name_val else ""
        display_name = str(display_name_val.Text) if display_name_val else browse_name
        description = str(description_val.Text) if description_val and description_val.Text else ""
        node_class = _NODE_CLASS.get(node_class_val, "Unknown")

    except Exception as exc:
        logger.debug("Failed reading attributes for node %s: %s", node.nodeid, exc)
        node_id_str = str(node.nodeid)
        browse_name = node_id_str
        display_name = node_id_str
        description = ""
        node_class = "Unknown"
        node_class_val = None

    data_type: str | None = None
    value: Any = None
    if node_class == "Variable":
        try:
            dt_node_id = await node.read_data_type()
            data_type = _DATATYPE_NAMES.get(dt_node_id.Identifier, str(dt_node_id.Identifier))
        except Exception:
            data_type = "?"
        if include_value:
            try:
                value = await node.read_value()
                if hasattr(value, 'value'):
                    value = value.value
            except Exception:
                value = None

    has_children = False
    if node_class in ("Object", "ObjectType", "View", "Unknown"):
        try:
            children = await node.get_children()
            has_children = len(children) > 0
        except Exception:
            has_children = False
    elif node_class == "Variable" and data_type not in _DATATYPE_NAMES.values():
        # Structured/custom-typed variables (e.g. a PLC "struct" whose Value is a
        # single ExtensionObject) are often ALSO exposed by the server as a set of
        # individually browsable scalar child Variables (one per struct member).
        # Only bother checking for known scalar types to skip this extra round trip
        # for the common case of a plain Boolean/Int32/Float/etc. leaf variable.
        try:
            children = await node.get_children()
            has_children = len(children) > 0
        except Exception:
            has_children = False

    full_path = f"{browse_path}/{browse_name}" if browse_name else browse_path

    return NodeInfo(
        node_id=node_id_str,
        browse_name=browse_name,
        display_name=display_name,
        description=description,
        node_class=node_class,
        data_type=data_type,
        value=value,
        has_children=has_children,
        server_id=server_id,
        browse_path=full_path,
    )


# Cap items per bulk service call. PLC stacks enforce hard OperationLimits —
# the WAGO PFC300 reports MaxNodesPerRead=100 and MaxNodesPerBrowse=100 and
# rejects anything larger with BadTooManyOperations.
_BULK_READ_CHUNK = 100
_BULK_BROWSE_CHUNK = 100

# Hard cap on Browse pagination (ContinuationPoint/BrowseNext) for a single
# node's children. Observed on a WAGO PFC300: browsing a folder with >100
# children (which forces pagination) either hangs indefinitely or — worse —
# loops accumulating millions of duplicate references over many minutes
# without the server ever returning an empty ContinuationPoint. A folder
# with <=100 children (fits in one page, no BrowseNext needed) always
# completes normally. So: never follow more than a few pages, and bound
# every individual Browse/BrowseNext call with its own timeout — a stuck
# or malformed pagination sequence must fail fast, not hang or explode.
_MAX_BROWSE_PAGES = 3
_BROWSE_PAGE_TIMEOUT = 20


async def _browse_paged(client, node: Node, page_size: int = 100) -> tuple[list, bool]:
    """Browse one node's children with a hard page cap.

    Returns (references, truncated). truncated=True means the folder has
    more children than the cap could retrieve (or a page failed/timed out) —
    callers that need completeness (e.g. dynamic folder scanning) must NOT
    treat a truncated result as "these are all the current children".
    """
    params = ua.BrowseParameters()
    params.View.Timestamp = ua.get_win_epoch()
    params.RequestedMaxReferencesPerNode = page_size
    desc = ua.BrowseDescription()
    desc.NodeId = node.nodeid
    desc.BrowseDirection = ua.BrowseDirection.Forward
    desc.ReferenceTypeId = ua.NodeId(ua.ObjectIds.HierarchicalReferences)
    desc.IncludeSubtypes = True
    desc.NodeClassMask = 0
    desc.ResultMask = ua.BrowseResultMask.All
    params.NodesToBrowse.append(desc)

    try:
        results = await asyncio.wait_for(client.uaclient.browse(params), timeout=_BROWSE_PAGE_TIMEOUT)
    except Exception as exc:
        logger.warning("browse: first page failed/timed out for %s: %s", node.nodeid, exc)
        return [], True

    refs = list(results[0].References)
    cp = results[0].ContinuationPoint
    pages = 1
    while cp and pages < _MAX_BROWSE_PAGES:
        bn = ua.BrowseNextParameters()
        bn.ReleaseContinuationPoints = False
        bn.ContinuationPoints = [cp]
        try:
            nxt = await asyncio.wait_for(client.uaclient.browse_next(bn), timeout=_BROWSE_PAGE_TIMEOUT)
        except Exception as exc:
            logger.warning("browse: page %d failed/timed out for %s: %s", pages + 1, node.nodeid, exc)
            return refs, True
        new_refs = nxt[0].References
        if not new_refs:
            cp = None
            break
        refs.extend(new_refs)
        cp = nxt[0].ContinuationPoint
        pages += 1

    truncated = bool(cp)
    if truncated:
        logger.warning(
            "browse: %s has more children than the %d-page/%d-per-page cap could "
            "fetch (%d refs collected) — result is incomplete",
            node.nodeid, _MAX_BROWSE_PAGES, page_size, len(refs),
        )
        # Best-effort: release the still-open continuation point so it
        # doesn't linger server-side.
        try:
            rel = ua.BrowseNextParameters()
            rel.ReleaseContinuationPoints = True
            rel.ContinuationPoints = [cp]
            await asyncio.wait_for(client.uaclient.browse_next(rel), timeout=5)
        except Exception:
            pass
    return refs, truncated


async def _bulk_read(client, items: list[tuple[Any, int]]) -> list[Any]:
    """One Read service call for many (node_id, attribute_id) pairs.

    Returns the raw DataValue per item (None Value on bad status).
    """
    results: list[Any] = []
    for start in range(0, len(items), _BULK_READ_CHUNK):
        params = ua.ReadParameters()
        for nid, attr in items[start:start + _BULK_READ_CHUNK]:
            rv = ua.ReadValueId()
            rv.NodeId = nid
            rv.AttributeId = attr
            params.NodesToRead.append(rv)
        results.extend(await client.uaclient.read(params))
    return results


async def _bulk_has_children(client, node_ids: list[Any]) -> list[bool]:
    """One Browse service call answering 'has any hierarchical child?' per node."""
    flags: list[bool] = []
    for start in range(0, len(node_ids), _BULK_BROWSE_CHUNK):
        params = ua.BrowseParameters()
        params.RequestedMaxReferencesPerNode = 1
        for nid in node_ids[start:start + _BULK_BROWSE_CHUNK]:
            bd = ua.BrowseDescription()
            bd.NodeId = nid
            bd.BrowseDirection = ua.BrowseDirection.Forward
            bd.ReferenceTypeId = ua.NodeId(ua.ObjectIds.HierarchicalReferences)
            bd.IncludeSubtypes = True
            bd.NodeClassMask = 0
            bd.ResultMask = 0
            params.NodesToBrowse.append(bd)
        results = await client.uaclient.browse(params)
        flags.extend(len(r.References) > 0 for r in results)
        # Browsing with RequestedMaxReferencesPerNode=1 allocates a continuation
        # point on the server for every node with more references. Release them
        # immediately — PLCs have few CP slots (and never releasing them starves
        # the whole session until Browse/BrowseNext stalls server-wide).
        cps = [r.ContinuationPoint for r in results if r.ContinuationPoint]
        if cps:
            bn = ua.BrowseNextParameters()
            bn.ReleaseContinuationPoints = True
            bn.ContinuationPoints = cps
            try:
                await client.uaclient.browse_next(bn)
            except Exception:
                pass  # release is best-effort; session close frees them anyway
    return flags


async def _children_bulk(
    client, parent: Node, server_id: str, parent_path: str, include_value: bool = True,
) -> tuple[list[NodeInfo], bool]:
    """Build NodeInfo for all children with a handful of service calls total:
    one Browse (ids/names/classes), one Read (descriptions, variable types,
    and — unless include_value is False — values), one Browse (has-children
    flags).

    include_value=False skips reading the Value attribute. This is for
    background folder scanning, which only needs DataType to filter by
    allowed type — reading live Value for every node is pure overhead there,
    and on some PLC stacks reading Value for many "global" variables at once
    is dramatically slower than reading their other attributes (observed:
    a WAGO PFC300 taking >120s for a Value-inclusive bulk read that returns
    in under a second once Value is dropped).

    Returns (infos, truncated) — see _browse_paged.
    """
    descs, truncated = await _browse_paged(client, parent)
    if not descs:
        return [], truncated

    node_classes = [_NODE_CLASS.get(d.NodeClass, "Unknown") for d in descs]

    # One Read call: Description for every child; DataType (+ Value) for variables
    read_items: list[tuple[Any, int]] = []
    read_map: list[tuple[int, str]] = []  # parallel: (child index, attribute kind)
    for i, d in enumerate(descs):
        read_items.append((d.NodeId, ua.AttributeIds.Description))
        read_map.append((i, "description"))
        if node_classes[i] == "Variable":
            read_items.append((d.NodeId, ua.AttributeIds.DataType))
            read_map.append((i, "data_type"))
            if include_value:
                read_items.append((d.NodeId, ua.AttributeIds.Value))
                read_map.append((i, "value"))
    raw = await _bulk_read(client, read_items)

    descriptions = [""] * len(descs)
    data_types: list[str | None] = [None] * len(descs)
    values: list[Any] = [None] * len(descs)
    for (i, kind), dv in zip(read_map, raw):
        val = dv.Value.Value if dv.Value is not None else None
        if kind == "description":
            descriptions[i] = str(val.Text) if val is not None and val.Text else ""
        elif kind == "data_type":
            if val is not None:
                data_types[i] = _DATATYPE_NAMES.get(val.Identifier, str(val.Identifier))
            else:
                data_types[i] = "?"
        elif kind == "value":
            if hasattr(val, 'value'):
                val = val.value
            values[i] = val

    # One Browse call for has_children — same candidate rule as get_node_info:
    # container classes always, variables only when not a known scalar type.
    candidates = [
        i for i, cls in enumerate(node_classes)
        if cls in ("Object", "ObjectType", "View", "Unknown")
        or (cls == "Variable" and data_types[i] not in _DATATYPE_NAMES.values())
    ]
    has_flags = await _bulk_has_children(client, [descs[i].NodeId for i in candidates])
    has_children = [False] * len(descs)
    for i, flag in zip(candidates, has_flags):
        has_children[i] = flag

    infos = []
    for i, d in enumerate(descs):
        browse_name = str(d.BrowseName.Name) if d.BrowseName else ""
        display_name = str(d.DisplayName.Text) if d.DisplayName and d.DisplayName.Text else browse_name
        infos.append(NodeInfo(
            node_id=d.NodeId.to_string(),
            browse_name=browse_name,
            display_name=display_name,
            description=descriptions[i],
            node_class=node_classes[i],
            data_type=data_types[i],
            value=values[i],
            has_children=has_children[i],
            server_id=server_id,
            browse_path=f"{parent_path}/{browse_name}" if browse_name else parent_path,
        ))
    return infos, truncated


async def get_children(
    client_manager,
    server_id: str,
    node_id: str | None,
    parent_path: str = "",
    include_value: bool = True,
) -> list[NodeInfo]:
    """Return NodeInfo list for all children of the given node (or Objects root).

    include_value=False skips live Value reads — see _children_bulk for why
    that matters for background scanning. The per-node fallback path also
    skips its Value read in that case.

    Uses the manager's dedicated browse session when available (isolates
    browsing from continuous polling traffic on the main session), falling
    back to the main session otherwise.

    A folder with more children than _browse_paged's page cap allows shows
    only the first page here — see get_children_full for a variant that
    reports truncation, used by dynamic folder scanning where completeness
    is required for correctness.
    """
    infos, _truncated = await get_children_full(
        client_manager, server_id, node_id, parent_path, include_value=include_value,
    )
    return infos


async def get_children_full(
    client_manager,
    server_id: str,
    node_id: str | None,
    parent_path: str = "",
    include_value: bool = True,
) -> tuple[list[NodeInfo], bool]:
    """Like get_children, but also reports whether the result is truncated
    (folder has more children than the page cap could retrieve)."""
    client = await _resolve_browse_client(client_manager, server_id)
    if client is None:
        return [], False
    if node_id is None:
        parent = client.nodes.objects
    else:
        parent = client.get_node(node_id)
    try:
        return await _children_bulk(client, parent, server_id, parent_path, include_value=include_value)
    except Exception as exc:
        logger.warning(
            "bulk browse failed server=%s node=%s (%s) — falling back to per-node reads",
            server_id, node_id, exc,
        )
        discard = getattr(client_manager, "discard_browse_client", None)
        if discard:
            discard(server_id)
    try:
        # Same page-capped Browse as the bulk path (not the unbounded
        # Node.get_children()) — a genuine bulk-read failure shouldn't fall
        # through into the same unbounded-pagination hang this whole
        # mechanism exists to avoid.
        refs, truncated = await _browse_paged(client, parent)
        infos = []
        for ref in refs:
            n = client.get_node(ref.NodeId)
            info = await get_node_info(n, server_id=server_id, browse_path=parent_path, include_value=include_value)
            infos.append(info)
        return infos, truncated
    except Exception as exc:
        logger.warning("browse error server=%s node=%s: %s", server_id, node_id, exc)
        return [], False


async def _resolve_browse_client(client_manager, server_id: str):
    """Prefer the manager's dedicated browse session; fall back to the main
    (polling) session if unavailable."""
    get_browse_client = getattr(client_manager, "get_browse_client", None)
    client = await get_browse_client(server_id) if get_browse_client else None
    if client is None:
        client = client_manager.get_client(server_id)
    return client


async def find_siblings(
    client_manager,
    server_id: str,
    node_id: str,
    child_browse_names: list[str] | None = None,
) -> list[NodeInfo]:
    """
    Find all sibling Object nodes of the given node's parent.
    Returns NodeInfo for each sibling (the instance level, not the children).
    Optionally filters to siblings that have all of child_browse_names as children.
    """
    client = await _resolve_browse_client(client_manager, server_id)
    if client is None:
        return []

    target_node = client.get_node(node_id)
    try:
        parent_refs = await target_node.get_parent()
        if parent_refs is None:
            return []
        parent = client.get_node(parent_refs)
        siblings_raw = await parent.get_children()
    except Exception as exc:
        logger.warning("find_siblings error: %s", exc)
        return []

    result = []
    for sib in siblings_raw:
        try:
            info = await get_node_info(sib, server_id=server_id)
            if info.node_class != "Object":
                continue
            if child_browse_names:
                # Verify all required child browse names exist
                children = await sib.get_children()
                child_names = set()
                for ch in children:
                    try:
                        bn_attr = await ch.read_attribute(ua.AttributeIds.BrowseName)
                        child_names.add(bn_attr.Value.Value.Name)
                    except Exception:
                        pass
                if not all(n in child_names for n in child_browse_names):
                    continue
            result.append(info)
        except Exception:
            continue

    return result
