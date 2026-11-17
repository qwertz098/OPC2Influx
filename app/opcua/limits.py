"""
How many nodes one OPC UA Read / Browse request may carry.

PLC stacks enforce hard OperationLimits and refuse larger requests with
BadTooManyOperations (some report MaxNodesPerRead = MaxNodesPerBrowse = 100).
After connecting, the app reads the limits the server reports under
Server / ServerCapabilities / OperationLimits; a value set on the server's
settings page wins; without either, DEFAULT is used. The values are kept on
the asyncua client object, so every reader of that session uses them.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from asyncua import ua

logger = logging.getLogger(__name__)

DEFAULT = 100        # when neither the server nor the settings name a limit
MAX = 10000          # upper bound for a single request, whatever the server says


async def detect(client: Any) -> tuple[int, int]:
    """(MaxNodesPerRead, MaxNodesPerBrowse) as reported by the server; 0 = not reported / no limit."""
    ids = (ua.ObjectIds.Server_ServerCapabilities_OperationLimits_MaxNodesPerRead,
           ua.ObjectIds.Server_ServerCapabilities_OperationLimits_MaxNodesPerBrowse)
    out = []
    for i in ids:
        try:
            v = await client.get_node(ua.NodeId(i)).read_value()
            out.append(int(v) if v else 0)
        except Exception:
            out.append(0)
    return out[0], out[1]


def effective(configured: int, detected: int) -> tuple[int, str]:
    """(limit, source): the setting wins, then the server's value, then DEFAULT."""
    if configured and configured > 0:
        return min(configured, MAX), "setting"
    if detected and detected > 0:
        return min(detected, MAX), "server"
    return DEFAULT, "default"


def apply(client: Any, srv: Any, detected: tuple[int, int]) -> dict:
    """Store the limits on the client; returns them for the status display."""
    read, read_src = effective(getattr(srv, "max_nodes_per_read", 0), detected[0])
    browse, browse_src = effective(getattr(srv, "max_nodes_per_browse", 0), detected[1])
    client.o2i_max_read = read
    client.o2i_max_browse = browse
    return {"read": read, "read_from": read_src, "browse": browse, "browse_from": browse_src,
            "server_read": detected[0], "server_browse": detected[1]}


def read_limit(client: Optional[Any]) -> int:
    return getattr(client, "o2i_max_read", DEFAULT) or DEFAULT


def browse_limit(client: Optional[Any]) -> int:
    return getattr(client, "o2i_max_browse", DEFAULT) or DEFAULT
