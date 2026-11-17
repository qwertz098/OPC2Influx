"""Modbus TCP source: codec, block planning, client, manager and polling."""
import asyncio
import math
import types

import pytest

from app.config import ModbusPointConfig, ModbusServerSettings, ServerConfig, SubscriptionConfig
from app.modbus.client import ModbusError, ModbusTcpClient, parse_endpoint
from app.modbus.codec import Point, decode, encode, plan_blocks, scaled
from app.modbus.server import ModbusTcpServer, RegisterBank


# ---------------------------------------------------------------- codec
@pytest.mark.parametrize("dtype,value", [
    ("int16", -1234), ("uint16", 54321), ("int32", -123456789), ("uint32", 4000000000),
    ("float32", 12.5), ("int64", -2**40), ("uint64", 2**63 + 5), ("float64", math.pi)])
@pytest.mark.parametrize("order", ["ABCD", "CDAB", "BADC", "DCBA"])
def test_encode_decode_roundtrip(dtype, value, order):
    assert decode(encode(value, dtype, order), dtype, order) == pytest.approx(value)


def test_known_orders():
    # 123456.0 as float32 = 0x47F12000 → words big-endian 0x47F1, 0x2000
    assert decode([0x47F1, 0x2000], "float32", "ABCD") == 123456.0
    assert decode([0x2000, 0x47F1], "float32", "CDAB") == 123456.0
    assert decode([0xF147, 0x0020], "float32", "BADC") == 123456.0
    assert decode([0x0020, 0xF147], "float32", "DCBA") == 123456.0
    assert decode([0x1234], "uint16", "BADC") == 0x3412
    assert decode([0b1000], "uint16", bit=3) is True and decode([0b1000], "uint16", bit=2) is False


def test_scaled():
    assert scaled(1234, 1, 0) == 1234 and isinstance(scaled(1234, 1, 0), int)
    assert scaled(1234, 0.1, 0) == pytest.approx(123.4)
    assert scaled(10, 1, -5) == 5
    assert scaled(float("nan"), 1, 0) is None
    assert scaled(True, 10, 0) is True


def test_plan_blocks():
    pts = [Point(1, "holding", 0, "uint16"), Point(1, "holding", 1, "float32"), Point(1, "holding", 20, "uint16"),
           Point(1, "holding", 5, "uint16"), Point(2, "holding", 2, "uint16"), Point(1, "input", 0, "uint16"),
           Point(1, "coil", 0), Point(1, "coil", 100)]
    blocks = plan_blocks(pts, max_registers=125, max_gap=10)
    got = sorted((b.unit, b.area, b.start, b.count, sorted(b.points)) for b in blocks)
    assert got == [(1, "coil", 0, 101, [6, 7]),            # bits: gap of 10 * 16 still one read
                   (1, "holding", 0, 6, [0, 1, 3]),
                   (1, "holding", 20, 1, [2]),             # gap 14 > 10
                   (1, "input", 0, 1, [5]),
                   (2, "holding", 2, 1, [4])]
    small = plan_blocks([Point(1, "holding", a, "uint16") for a in range(0, 10)], max_registers=4)
    assert [(b.start, b.count) for b in small] == [(0, 4), (4, 4), (8, 2)]


def test_parse_endpoint():
    assert parse_endpoint("modbus-tcp://10.0.0.5:1502") == ("10.0.0.5", 1502)
    assert parse_endpoint("plc.local") == ("plc.local", 502)
    assert parse_endpoint("modbus-tcp://[fe80::1]:503/") == ("fe80::1", 503)


def test_node_id():
    assert ModbusPointConfig(address=100, data_type="float32", order="CDAB").node_id() == "hr100:float32:CDAB"
    assert ModbusPointConfig(area="coil", address=3).node_id() == "co3"
    assert ModbusPointConfig(address=5, bit=3, unit_id=2).node_id() == "hr5.3@2"
    assert ModbusPointConfig(area="input", address=7, scale=0.1, offset=-5).node_id() == "ir7:uint16*0.1-5"


# ---------------------------------------------------------------- client against the server
@pytest.fixture
def bank():
    b = RegisterBank()
    b.set_words("holding", 0, [1, 2, 3])
    b.set_words("holding", 10, encode(21.5, "float32", "CDAB"))
    b.set_words("input", 0, encode(-7, "int16"))
    b.coil[3] = True
    b.discrete[1] = True
    return b


def test_client_reads(bank):
    async def go():
        srv = ModbusTcpServer(bank, port=0)
        await srv.start()
        c = ModbusTcpClient("127.0.0.1", srv.port, timeout=1)
        await c.connect()
        assert await c.read(1, 3, 0, 3) == [1, 2, 3]
        assert decode(await c.read(1, 3, 10, 2), "float32", "CDAB") == 21.5
        assert decode(await c.read(1, 4, 0, 1), "int16") == -7
        assert await c.read(1, 1, 0, 5) == [False, False, False, True, False]
        assert await c.read(1, 2, 0, 2) == [False, True]
        bank.valid = {("holding", 0)}
        with pytest.raises(ModbusError) as e:
            await c.read(1, 3, 0, 2)
        assert e.value.code == 2 and c.connected            # exception keeps the connection
        await c.close()
        await srv.stop()
    asyncio.run(go())


def test_client_timeout_closes():
    async def go():
        async def silent(reader, writer):
            await asyncio.sleep(5)
        s = await asyncio.start_server(silent, "127.0.0.1", 0)
        port = s.sockets[0].getsockname()[1]
        c = ModbusTcpClient("127.0.0.1", port, timeout=0.2)
        await c.connect()
        with pytest.raises(ConnectionError, match="timeout"):
            await c.read(1, 3, 0, 1)
        assert not c.connected and c.closed.is_set()
        s.close()
    asyncio.run(go())


# ---------------------------------------------------------------- manager + poller
def _state(minimal_state, port, points):
    srv = ServerConfig(id="mb", display_name="PLC", protocol="modbus", endpoint_url=f"modbus-tcp://127.0.0.1:{port}",
                       reconnect_interval_seconds=1, modbus=ModbusServerSettings(timeout_ms=1000))
    minimal_state.config.servers.servers = [srv]
    subs = []
    for name, mp in points:
        subs.append(SubscriptionConfig(id=name, server_id="mb", node_id=mp.node_id(), display_name=name, modbus=mp))
    minimal_state.config.subscriptions.subscriptions = subs
    return minimal_state


def test_manager_reads_blocks_and_splits_on_bad_address(minimal_state, bank):
    from app.opcua.client import OpcUaClientManager

    async def go():
        server = ModbusTcpServer(bank, port=0)
        await server.start()
        st = _state(minimal_state, server.port, [
            ("a", ModbusPointConfig(address=0)), ("b", ModbusPointConfig(address=2, scale=0.5)),
            ("t", ModbusPointConfig(address=10, data_type="float32", order="CDAB")),
            ("i", ModbusPointConfig(area="input", address=0, data_type="int16")),
            ("c", ModbusPointConfig(area="coil", address=3)),
            ("bit", ModbusPointConfig(address=1, bit=1)),
        ])
        mgr = OpcUaClientManager(st)
        await mgr.start_all()
        for _ in range(50):
            if mgr.is_connected("mb"):
                break
            await asyncio.sleep(0.02)
        assert mgr.is_connected("mb") and st.server_status["mb"].connected
        nodes = [s.node_id for s in st.config.subscriptions.subscriptions]
        before = server.requests
        vals = await mgr.read_node_values("mb", nodes)
        assert vals == [1, 1.5, 21.5, -7, True, True]
        assert server.requests - before == 3                 # holding 0..11, input, coil
        # address 2 disappears: its block is read point by point, the others keep their values
        bank.valid = {("holding", a) for a in (0, 1, 10, 11)} | {("input", 0), ("coil", 3)}
        vals = await mgr.read_node_values("mb", nodes)
        assert vals == [1, None, 21.5, -7, True, True]
        assert any("illegal data address" in e for e in mgr.modbus.point_errors("mb").values())
        assert await mgr.read_node_attributes("mb", nodes[0]) == {}
        await mgr.stop_all()
        assert "mb" not in st.server_status
        await server.stop()
    asyncio.run(go())


def test_reconnect_after_server_restart(minimal_state, bank):
    from app.modbus.manager import ModbusManager

    async def go():
        server = ModbusTcpServer(bank, port=0)
        await server.start()
        port = server.port
        st = _state(minimal_state, port, [("a", ModbusPointConfig(address=0))])
        mgr = ModbusManager(st)
        await mgr.reconcile()
        await asyncio.sleep(0.1)
        assert mgr.is_connected("mb")
        await server.stop()
        # the open connection breaks on the next read
        with pytest.raises(ConnectionError):
            for _ in range(3):
                await mgr.read_node_values("mb", ["hr0:uint16"])
                await asyncio.sleep(0.05)
        await asyncio.sleep(0.05)
        assert not mgr.is_connected("mb") and st.server_status["mb"].last_error
        server = ModbusTcpServer(bank, port=port)
        await server.start()
        for _ in range(100):
            if mgr.is_connected("mb"):
                break
            await asyncio.sleep(0.05)
        assert await mgr.read_node_values("mb", ["hr0:uint16"]) == [1]
        await mgr.stop_all()
        await server.stop()
    asyncio.run(go())


def test_poller_writes_modbus_values(minimal_state, bank):
    from app.opcua.client import OpcUaClientManager
    from app.opcua.poller import PollingEngine

    async def go():
        server = ModbusTcpServer(bank, port=0)
        await server.start()
        st = _state(minimal_state, server.port, [("t", ModbusPointConfig(address=10, data_type="float32", order="CDAB"))])
        st.config.subscriptions.subscriptions[0].polling.interval_ms = 50
        written = []

        async def enqueue(**kw):
            written.append(kw)
        st.influx_pool = types.SimpleNamespace(enqueue=enqueue)
        st.opcua_manager = OpcUaClientManager(st)
        await st.opcua_manager.start_all()
        st.polling_engine = PollingEngine(st)
        await st.polling_engine.reconcile()
        await asyncio.sleep(0.4)
        assert st.polling_engine.live_values() == {"t": 21.5}
        assert written and written[0]["value"] == 21.5 and written[0]["measurement"] == "opcua_value"
        await st.polling_engine.stop_all()
        await st.opcua_manager.stop_all()
        await server.stop()
    asyncio.run(go())


# ---------------------------------------------------------------- register page
@pytest.mark.asyncio
async def test_register_routes(async_client, tmp_config_dir):
    import yaml
    c = async_client
    r = await c.post("/config/servers", data={"protocol": "modbus", "display_name": "Dev", "mb_host": "10.0.0.9:1502",
                                               "mb_unit_id": "3", "mb_order": "CDAB"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/registers")
    base = r.headers["location"]
    srv = yaml.safe_load((tmp_config_dir / "servers.yaml").read_text())["servers"][0]
    assert srv["protocol"] == "modbus" and srv["endpoint_url"] == "modbus-tcp://10.0.0.9:1502"
    assert srv["modbus"]["unit_id"] == 3 and srv["modbus"]["order"] == "CDAB"

    r = await c.post(base, json={"name": "Power", "area": "holding", "address": "0", "type": "float32"})
    assert r.status_code == 200
    rid = r.json()["id"]
    r = await c.post(base, json={"name": "Dup", "area": "holding", "address": "0", "type": "float32"})
    assert r.status_code == 400 and "already configured" in r.json()["error"]
    r = await c.post(base, json={"name": "Bad", "address": "1", "type": "bool"})
    assert r.status_code == 400 and "bit" in r.json()["error"]
    r = await c.post(base, json={"name": "X", "address": "abc"})
    assert r.json()["error"] == "address: not a number (abc)"

    csv_text = ("name;area;address;type;bit;scale;enabled\nTemp;hr;4;uint16;;0,1;1\nRun;holding;5;;0;;1\n"
                "Pump;coil;0;;;;0\nPower;holding;0;float32;;;1\n")
    r = await c.post(base + "/import", files={"file": ("r.csv", csv_text.encode(), "text/csv")})
    assert r.json() == {"added": 3, "updated": 1, "errors": []}
    subs = yaml.safe_load((tmp_config_dir / "subscriptions.yaml").read_text())["subscriptions"]
    by = {s["display_name"]: s for s in subs}
    assert by["Temp"]["node_id"] == "hr4:uint16*0.1" and by["Temp"]["modbus"]["scale"] == 0.1
    assert by["Run"]["node_id"] == "hr5.0" and by["Pump"]["enabled"] is False
    assert by["Power"]["influxdb"]["measurement"] == "Power"

    r = await c.post(f"{base}/all", json={"enabled": True})
    assert r.json()["changed"] == 1
    r = await c.post(f"{base}/all", json={"enabled": False, "ids": [rid]})
    assert r.json()["changed"] == 1
    r = await c.post(f"{base}/{rid}/toggle", json={"enabled": True})
    assert r.json() == {"enabled": True}

    r = await c.get(base + "/export.csv")
    lines = r.text.strip().splitlines()
    assert lines[0].startswith("name,area,address,type") and len(lines) == 5
    page = await c.get(base)
    assert page.status_code == 200 and "Alle an" in page.text and "hr5.0" in page.text
    r = await c.get(base + "/values")
    assert r.json()["connected"] is False and set(r.json()["registers"]) == {s["id"] for s in subs}
    servers_page = await c.get("/config/servers")
    assert "Registers (4/4)" in servers_page.text


@pytest.mark.asyncio
async def test_preview_and_bits(async_client, tmp_config_dir):
    import yaml
    from app.modbus.codec import interpretations
    bank = RegisterBank()
    bank.set_words("holding", 0, [0b1000_0000_0000_0101])
    bank.set_words("holding", 1, encode(21.5, "float32", "CDAB"))
    server = ModbusTcpServer(bank, port=0)
    await server.start()
    try:
        c = async_client
        r = await c.post("/config/servers", data={"protocol": "modbus", "display_name": "Dev",
                                                   "mb_host": "127.0.0.1", "mb_port": str(server.port)},
                         follow_redirects=False)
        base = r.headers["location"]
        r = await c.post(base + "/test", json={"area": "holding", "address": "0", "type": "uint16", "scale": "0,5"})
        d = r.json()
        assert d["value"] == 0x8005 * 0.5 and d["unscaled"] == 0x8005 and d["raw"] == [0x8005]
        assert d["bits"][:4] == [1, 0, 1, 0] and d["bits"][15] == 1 and len(d["extra"]) == 3
        r = await c.post(base + "/test", json={"area": "holding", "address": "1", "type": "uint16"})
        assert ["float32 CDAB", 21.5] in r.json()["types"]      # what the next registers would be
        r = await c.post(base + "/test", json={"area": "holding", "address": "0", "type": "uint16", "bit": "2"})
        assert r.json()["value"] is True
        r = await c.post(base + "/test", json={"area": "coil", "address": "0"})
        assert r.json()["value"] is False and "bits" not in r.json()

        r = await c.post(base + "/bits", json={"name": "Status", "area": "holding", "address": "0", "bits": [0, 2, 15],
                                                "interval_ms": "1000"})
        assert r.json() == {"added": 3, "errors": []}
        r = await c.post(base + "/bits", json={"name": "Status", "area": "holding", "address": "0", "bits": [2]})
        assert r.json()["added"] == 0 and "already configured" in r.json()["errors"][0]
        subs = yaml.safe_load((tmp_config_dir / "subscriptions.yaml").read_text())["subscriptions"]
        assert sorted((s["display_name"], s["node_id"]) for s in subs) == [
            ("Status.bit0", "hr0.0"), ("Status.bit15", "hr0.15"), ("Status.bit2", "hr0.2")]
    finally:
        await server.stop()
    assert ("float32 CDAB", 21.5) in interpretations(encode(21.5, "float32", "CDAB"))
