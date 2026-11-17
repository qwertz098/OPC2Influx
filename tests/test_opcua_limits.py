"""OPC UA request sizes: from the server's OperationLimits, the settings, or 100."""
from __future__ import annotations

import types

import pytest
import pytest_asyncio
from asyncua import Client, ua

from app.opcua import limits
from app.opcua.browser import _bulk_read


def test_effective():
    assert limits.effective(0, 0) == (100, "default")
    assert limits.effective(0, 250) == (250, "server")
    assert limits.effective(50, 250) == (50, "setting")
    assert limits.effective(0, 10**6) == (limits.MAX, "server")


def test_apply_and_lookup():
    client = types.SimpleNamespace()
    srv = types.SimpleNamespace(max_nodes_per_read=0, max_nodes_per_browse=20)
    info = limits.apply(client, srv, (100, 0))
    assert limits.read_limit(client) == 100 and limits.browse_limit(client) == 20
    assert info["read_from"] == "server" and info["browse_from"] == "setting"
    assert limits.read_limit(None) == 100          # a client without limits: the default


@pytest_asyncio.fixture(loop_scope="session")
async def ua_client(opcua_server):
    client = Client(url=opcua_server._test_endpoint)
    await client.connect()
    yield client
    await client.disconnect()


@pytest.mark.asyncio(loop_scope="session")
async def test_detect_and_chunked_read(ua_client, opcua_server):
    # the test server may or may not report limits; detect never fails
    read, browse = await limits.detect(ua_client)
    assert read >= 0 and browse >= 0
    # with a limit of 2 a read of 5 attributes is split into 3 requests
    calls = []
    real = ua_client.uaclient.read

    async def counting(params):
        calls.append(len(params.NodesToRead))
        return await real(params)
    ua_client.uaclient.read = counting
    try:
        limits.apply(ua_client, types.SimpleNamespace(max_nodes_per_read=2, max_nodes_per_browse=0), (0, 0))
        node = opcua_server._test_pressure.nodeid
        out = await _bulk_read(ua_client, [(node, ua.AttributeIds.Value)] * 5)
        assert len(out) == 5 and calls == [2, 2, 1]
    finally:
        ua_client.uaclient.read = real
        ua_client.o2i_max_read = 100
