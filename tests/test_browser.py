"""Integration tests for OPC UA browser against the embedded test server."""
from __future__ import annotations

import pytest
import pytest_asyncio
from asyncua import Client

from app.opcua.browser import get_children, find_siblings, get_node_info

# All tests in this module share the session-scoped OPC UA server/client fixtures,
# so they must run on the same session-scoped event loop.
pytestmark = pytest.mark.asyncio(loop_scope="session")


# ---------------------------------------------------------------------------
# Helper: a connected asyncua Client for the test server
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(loop_scope="session")
async def ua_client(opcua_server):
    client = Client(url=opcua_server._test_endpoint)
    await client.connect()
    yield client
    await client.disconnect()


# ---------------------------------------------------------------------------
# Lightweight mock that wraps a real asyncua Client
# ---------------------------------------------------------------------------

class _MockClientManager:
    def __init__(self, client):
        self._client = client
        self.discarded_browse_clients: list[str] = []

    def get_client(self, server_id: str):
        return self._client

    async def get_browse_client(self, server_id: str):
        return self._client

    def discard_browse_client(self, server_id: str) -> None:
        self.discarded_browse_clients.append(server_id)

    def is_connected(self, server_id: str) -> bool:
        return True


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_get_children_of_objects_root(ua_client):
    mgr = _MockClientManager(ua_client)
    children = await get_children(mgr, "test", node_id=None)
    names = [c.browse_name for c in children]
    # The test server has Motors and Sensors under Objects
    assert any("Motor" in n for n in names) or any("Sensors" in n for n in names)


async def test_get_children_returns_motor_instances(ua_client, opcua_server):
    mgr = _MockClientManager(ua_client)
    motors_node = opcua_server._test_motor_nodes["Motor_0"]["node"]
    # Browse the parent of Motor_0 (the Motors container)
    motors_parent = await motors_node.get_parent()
    motors_node_id = (await ua_client.get_node(motors_parent).get_children())[0]

    # Get children of Motors container
    motors_container_children = await get_children(
        mgr, "test",
        node_id=(await ua_client.get_node(motors_parent).get_children())[0].nodeid.to_string()
        if False else None
    )
    # Simpler: browse root and find Motors
    root_children = await get_children(mgr, "test", node_id=None)
    motors_info = next((n for n in root_children if "Motors" in n.browse_name), None)
    if motors_info is None:
        pytest.skip("Motors node not found at root level (server namespace structure)")

    motors_children = await get_children(mgr, "test", node_id=motors_info.node_id)
    names = [c.browse_name for c in motors_children]
    assert "Motor_0" in names
    assert "Motor_1" in names
    assert "Motor_2" in names


async def test_variable_nodes_have_data_type_and_value(ua_client, opcua_server):
    mgr = _MockClientManager(ua_client)
    speed_node = opcua_server._test_motor_nodes["Motor_0"]["Speed"]
    info = await get_node_info(speed_node, server_id="test")
    assert info.node_class == "Variable"
    assert info.data_type is not None
    assert info.value is not None
    assert float(info.value) == 0.0  # Motor_0 speed = 0 * 100


async def test_object_node_has_no_value(ua_client, opcua_server):
    motor_node = opcua_server._test_motor_nodes["Motor_0"]["node"]
    info = await get_node_info(motor_node, server_id="test")
    assert info.node_class == "Object"
    assert info.value is None
    assert info.has_children is True


async def test_find_siblings_returns_all_motors(ua_client, opcua_server):
    mgr = _MockClientManager(ua_client)
    # Use Motor_0 node id to find siblings
    motor_0 = opcua_server._test_motor_nodes["Motor_0"]["node"]
    motor_0_id = motor_0.nodeid.to_string()

    siblings = await find_siblings(mgr, "test", motor_0_id)
    browse_names = {s.browse_name for s in siblings}
    # Should find all 3 motor instances
    assert "Motor_0" in browse_names
    assert "Motor_1" in browse_names
    assert "Motor_2" in browse_names


async def test_find_siblings_filters_by_child_names(ua_client, opcua_server):
    mgr = _MockClientManager(ua_client)
    motor_0 = opcua_server._test_motor_nodes["Motor_0"]["node"]
    motor_0_id = motor_0.nodeid.to_string()

    # Filter for siblings that have both Speed and Temperature
    siblings = await find_siblings(mgr, "test", motor_0_id,
                                    child_browse_names=["Speed", "Temperature"])
    assert len(siblings) == 3

    # Filter for a non-existent child — should return 0
    siblings_none = await find_siblings(mgr, "test", motor_0_id,
                                         child_browse_names=["NonExistentTag"])
    assert len(siblings_none) == 0


async def test_bulk_browse_matches_per_node_path(ua_client, opcua_server):
    """The bulk path in get_children must produce the same NodeInfo fields as
    the old per-node get_node_info loop."""
    from app.opcua.browser import get_node_info

    mgr = _MockClientManager(ua_client)
    root_children = await get_children(mgr, "test", node_id=None)
    motors_info = next((n for n in root_children if "Motors" in n.browse_name), None)
    assert motors_info is not None

    bulk = await get_children(mgr, "test", node_id=motors_info.node_id, parent_path="/Motors")

    motors_node = ua_client.get_node(motors_info.node_id)
    reference = []
    for n in await motors_node.get_children():
        reference.append(await get_node_info(n, server_id="test", browse_path="/Motors"))

    assert len(bulk) == 3
    by_name_bulk = {i.browse_name: i for i in bulk}
    by_name_ref = {i.browse_name: i for i in reference}
    assert set(by_name_bulk) == set(by_name_ref)
    for name, ref in by_name_ref.items():
        b = by_name_bulk[name]
        assert b.node_id == ref.node_id
        assert b.display_name == ref.display_name
        assert b.description == ref.description
        assert b.node_class == ref.node_class
        assert b.data_type == ref.data_type
        assert b.value == ref.value
        assert b.has_children == ref.has_children
        assert b.browse_path == ref.browse_path
        assert b.server_id == ref.server_id


async def test_bulk_browse_variable_leaves(ua_client, opcua_server):
    """Variable children get data type + value and no has_children flag."""
    mgr = _MockClientManager(ua_client)
    motor_0 = opcua_server._test_motor_nodes["Motor_0"]["node"]
    children = await get_children(mgr, "test", node_id=motor_0.nodeid.to_string())
    by_name = {c.browse_name: c for c in children}
    assert by_name["Speed"].node_class == "Variable"
    assert by_name["Speed"].data_type in ("Float", "Double")
    assert float(by_name["Speed"].value) == 0.0
    assert by_name["Speed"].has_children is False


async def test_get_children_include_value_false_skips_value_read(ua_client, opcua_server):
    """Scanner path: DataType still populated, Value stays None, has_children unaffected."""
    mgr = _MockClientManager(ua_client)
    motor_0 = opcua_server._test_motor_nodes["Motor_0"]["node"]
    children = await get_children(mgr, "test", node_id=motor_0.nodeid.to_string(), include_value=False)
    by_name = {c.browse_name: c for c in children}
    assert by_name["Speed"].node_class == "Variable"
    assert by_name["Speed"].data_type in ("Float", "Double")
    assert by_name["Speed"].value is None
    assert by_name["Speed"].has_children is False


async def test_scan_children_matches_bulk_browse_minus_value(ua_client, opcua_server):
    """FolderScanner's per-node scan path must find the same nodes/types as
    the interactive bulk-browse path (just without Value, which it never
    reads)."""
    from app.opcua.folder_scanner import _scan_children

    mgr = _MockClientManager(ua_client)
    motor_0 = opcua_server._test_motor_nodes["Motor_0"]["node"]

    scanned, truncated = await _scan_children(mgr, "test", motor_0.nodeid.to_string())
    assert truncated is False
    bulk = await get_children(mgr, "test", node_id=motor_0.nodeid.to_string(), include_value=False)

    by_name_scanned = {c["browse_name"]: c for c in scanned}
    by_name_bulk = {c.browse_name: c for c in bulk}
    assert set(by_name_scanned) == set(by_name_bulk) == {"Speed", "Temperature"}
    for name, ref in by_name_bulk.items():
        s = by_name_scanned[name]
        assert s["node_id"] == ref.node_id
        assert s["display_name"] == ref.display_name
        assert s["node_class"] == ref.node_class
        assert s["data_type"] == ref.data_type
        assert s["has_children"] == ref.has_children


async def test_browse_paged_respects_hard_page_cap():
    """Simulates a PLC whose Browse/BrowseNext never terminates (the bug
    found on a WAGO PFC300 — millions of duplicate refs over 73 minutes).
    _browse_paged must stop after _MAX_BROWSE_PAGES and report truncated,
    never loop indefinitely."""
    from app.opcua import browser as browser_module
    from asyncua import ua as ua_module

    class _FakeRef:
        def __init__(self, i):
            self.NodeId = f"ns=2;i={i}"

    class _FakeBrowseResult:
        def __init__(self, refs, cp):
            self.References = refs
            self.ContinuationPoint = cp

    class _FakeUaClient:
        def __init__(self):
            self.browse_calls = 0
            self.browse_next_calls = 0

        async def browse(self, params):
            self.browse_calls += 1
            # Always claims there's more — a never-ending continuation point
            return [_FakeBrowseResult([_FakeRef(0)], b"cp-forever")]

        async def browse_next(self, params):
            if getattr(params, "ReleaseContinuationPoints", False):
                return [_FakeBrowseResult([], None)]
            self.browse_next_calls += 1
            return [_FakeBrowseResult([_FakeRef(self.browse_next_calls)], b"cp-forever")]

    class _FakeClient:
        def __init__(self):
            self.uaclient = _FakeUaClient()

    class _FakeNode:
        nodeid = "ns=2;i=1"

    client = _FakeClient()
    refs, truncated = await browser_module._browse_paged(client, _FakeNode())

    assert truncated is True
    assert client.uaclient.browse_calls == 1
    # First page + (_MAX_BROWSE_PAGES - 1) BrowseNext pages, then one release call
    assert client.uaclient.browse_next_calls == browser_module._MAX_BROWSE_PAGES - 1
    assert len(refs) == browser_module._MAX_BROWSE_PAGES
