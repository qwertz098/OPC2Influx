"""The app as a Modbus TCP server: register map, status bits, alarm coils, writes."""
import asyncio
import types
from datetime import datetime, timedelta, timezone

import pytest

from app.alerting.rule_checker import AlarmRuleChecker
from app.config import AlarmRuleConfig, ModbusServerOutputConfig, OutputPointConfig, SubscriptionConfig
from app.modbus.client import ModbusError, ModbusTcpClient
from app.modbus.codec import decode
from app.outputs.modbus_out import ModbusOutputService, overlaps, to_words
from app.state import ServerStatus


@pytest.fixture
def state(minimal_state):
    minimal_state.config.subscriptions.subscriptions = [
        SubscriptionConfig(id="t", server_id="srv", node_id="n1", display_name="Temp"),
        SubscriptionConfig(id="run", server_id="srv", node_id="n2", display_name="Run"),
        SubscriptionConfig(id="gone", server_id="srv", node_id="n3", display_name="Gone"),
    ]
    minimal_state.server_status["srv"] = ServerStatus(connected=True)
    live = {"t": 21.5, "run": True}
    minimal_state.polling_engine = types.SimpleNamespace(live_values=lambda: dict(live))
    minimal_state._live = live
    minimal_state.config.alarm_rules.rules = [AlarmRuleConfig(id="a1", name="Hot", subscription_id="t",
                                                              condition=">", threshold="50", alert_delay_seconds=5)]
    minimal_state.alarm_checker = AlarmRuleChecker()
    written = []

    async def enqueue(**kw):
        written.append(kw)
    minimal_state.influx_pool = types.SimpleNamespace(enqueue=enqueue)
    minimal_state._written = written
    minimal_state.config.outputs.modbus_server = ModbusServerOutputConfig(enabled=True, host="127.0.0.1", port=0)
    minimal_state.config.outputs.points = [
        OutputPointConfig(id="p1", kind="value", source_id="t", area="holding", address=0, data_type="float32",
                          status_address=0),
        OutputPointConfig(id="p2", kind="value", source_id="t", area="input", address=0, data_type="int16",
                          scale=10),
        OutputPointConfig(id="p3", kind="value", source_id="run", area="coil", address=0, data_type="bool",
                          status_address=1),
        OutputPointConfig(id="p4", kind="value", source_id="gone", area="holding", address=2, data_type="uint16",
                          status_address=2),
        OutputPointConfig(id="p5", kind="alarm", source_id="a1", area="coil", address=10),
        OutputPointConfig(id="p6", kind="received", name="Setpoint", area="holding", address=100,
                          data_type="int16", scale=10, record=True),
        OutputPointConfig(id="p7", kind="received", name="Ack", area="coil", address=20),
    ]
    return minimal_state


def test_update_values_status_and_alarm(state):
    svc = ModbusOutputService(state)
    svc.update()
    b = svc.bank
    assert decode([b.holding[0], b.holding[1]], "float32") == 21.5
    assert decode([b.input[0]], "int16") == 215                    # scale 10
    assert b.coil[0] is True and b.discrete[0] is True and b.discrete[1] is True
    assert b.discrete[2] is False and 2 not in b.holding           # no value: status 0, register empty
    assert b.coil[10] is False                                      # alarm not active
    # alarm active after its delay
    rule = state.config.alarm_rules.rules[0]
    state.alarm_checker.condition_since["a1"] = datetime.now(timezone.utc) - timedelta(seconds=10)
    svc.update()
    assert b.coil[10] is True
    state.alarm_checker.condition_since["a1"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    svc.update()
    assert b.coil[10] is False and not state.alarm_checker.is_active(rule)
    # value lost: keeps the last value, status 0
    state._live.pop("t")
    svc.update()
    assert decode([b.holding[0], b.holding[1]], "float32") == 21.5 and b.discrete[0] is False
    # disabled point and removed points disappear from the bank
    state.config.outputs.points[1].enabled = False
    svc.update()
    assert 0 not in b.input


def test_overlaps_and_words():
    a = OutputPointConfig(id="a", area="holding", address=0, data_type="float32")
    b = OutputPointConfig(id="b", area="holding", address=1, data_type="uint16")
    c = OutputPointConfig(id="c", area="holding", address=5, data_type="uint16", status_address=0)
    d = OutputPointConfig(id="d", area="discrete", address=0)
    assert overlaps([a, b, c, d]) == {"b": "holding 1 is also used by a", "d": "discrete 0 is also used by c"}
    assert to_words(OutputPointConfig(data_type="uint16"), -5, "ABCD") == [0]          # clamped
    assert to_words(OutputPointConfig(data_type="int16", scale=10), 3.14159, "ABCD") == [31]
    assert to_words(OutputPointConfig(area="coil"), 2.0, "ABCD") == [True]
    assert to_words(OutputPointConfig(), "text", "ABCD") is None


def test_server_reads_and_writes(state):
    async def go():
        svc = ModbusOutputService(state)
        await svc.sync()
        assert svc.server is not None, svc.error
        svc.update()
        c = ModbusTcpClient("127.0.0.1", svc.server.port, timeout=1)
        await c.connect()
        assert decode(await c.read(7, 3, 0, 2), "float32") == 21.5     # any unit id
        assert await c.read(1, 2, 0, 3) == [True, True, False]
        # writing a value register is refused, the write registers work
        with pytest.raises(ModbusError) as e:
            await c.write_registers(1, 0, [1])
        assert e.value.code == 2
        await c.write_registers(1, 100, [235])
        await c.write_coils(1, 20, [True])
        await asyncio.sleep(0.05)
        assert svc.received["p6"][0] == 23.5 and svc.received["p7"][0] is True
        assert state._written[0]["measurement"] == "Setpoint" and state._written[0]["value"] == 23.5
        assert len(state._written) == 1                                  # Ack is not recorded
        svc.update()                                                     # keeps written values
        assert await c.read(1, 3, 100, 1) == [235]
        # not writable: illegal function
        state.config.outputs.modbus_server.writable = False
        with pytest.raises(ModbusError) as e:
            await c.write_registers(1, 100, [1])
        assert e.value.code == 1
        assert svc.status()["writes"] == 2 and svc.status()["clients"] == 1
        await c.close()
        # port change restarts, disable stops
        state.config.outputs.modbus_server.enabled = False
        await svc.sync()
        assert svc.server is None
    asyncio.run(go())


def test_port_in_use_reports_error(state):
    async def go():
        blocker = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        port = blocker.sockets[0].getsockname()[1]
        state.config.outputs.modbus_server.port = port
        svc = ModbusOutputService(state)
        await svc.sync()
        assert svc.server is None and f"port {port}" in svc.error
        blocker.close()
    asyncio.run(go())


@pytest.mark.asyncio
async def test_output_routes(async_client, tmp_config_dir):
    import yaml
    c = async_client
    page = await c.get("/outputs")
    assert page.status_code == 200 and "Register map" in page.text
    # a write register, an overlap, a bad area
    r = await c.post("/outputs/points", json={"kind": "received", "name": "SP", "area": "holding", "address": "10",
                                              "data_type": "int16", "scale": "10", "record": True})
    assert r.status_code == 200
    r = await c.post("/outputs/points", json={"kind": "received", "name": "X", "area": "holding", "address": "10"})
    assert r.status_code == 400 and "also used by SP" in r.json()["error"]
    r = await c.post("/outputs/points", json={"kind": "received", "name": "Y", "area": "input", "address": "0"})
    assert r.status_code == 400
    r = await c.post("/outputs/server", json={"enabled": False, "port": 15021, "writable": False})
    assert r.status_code == 200
    cfg = yaml.safe_load((tmp_config_dir / "outputs.yaml").read_text())
    assert cfg["modbus_server"]["port"] == 15021 and cfg["points"][0]["scale"] == 10.0
    r = await c.post("/outputs/points/all", json={"enabled": False})
    assert r.json() == {"changed": 1}
    csv = (await c.get("/outputs/export.csv")).text.splitlines()
    assert csv[0].startswith("name;kind;access;area;address;modicon") and csv[1].startswith("SP;received;write;holding;10;40011")
    assert (await c.get("/outputs/values")).status_code == 200


@pytest.mark.asyncio
async def test_add_signals_and_alarms(async_client, tmp_config_dir):
    import yaml
    c = async_client
    await c.post("/config/alarm-rules", data={"subscription_id": "x", "condition": ">", "threshold": "1"})
    await c.post("/config/alarm-rules", data={"subscription_id": "y", "condition": ">", "threshold": "1"})
    assert (await c.post("/outputs/points/add-alarms", json={})).json() == {"added": 2}
    assert (await c.post("/outputs/points/add-alarms", json={})).json() == {"added": 0}
    # subscriptions via the Modbus register page (simplest way to create some)
    r = await c.post("/config/servers", data={"protocol": "modbus", "display_name": "D", "mb_host": "10.0.0.1"},
                     follow_redirects=False)
    base = r.headers["location"]
    ids = [(await c.post(base, json={"name": n, "address": str(a), "type": t, **extra})).json()["id"]
           for n, a, t, extra in [("A", 0, "float32", {}), ("B", 2, "uint16", {}), ("C", 3, "uint16", {"bit": "1"})]]
    r = await c.post("/outputs/points/add-signals", json={"ids": ids, "type": "float32", "status": True})
    assert r.json() == {"added": 3}
    pts = yaml.safe_load((tmp_config_dir / "outputs.yaml").read_text())["points"]
    got = [(p["kind"], p["area"], p["address"], p["data_type"], p["status_address"]) for p in pts]
    assert got == [("alarm", "coil", 0, "bool", None), ("alarm", "coil", 1, "bool", None),
                   ("value", "holding", 0, "float32", 0), ("value", "holding", 2, "float32", 1),
                   ("value", "coil", 2, "bool", 2)]                     # the bit goes to a coil


def test_signal_labels_unique(minimal_state):
    from app.config import SubscriptionInfluxConfig
    from app.references.capture import signal_labels
    minimal_state.config.subscriptions.subscriptions = [
        SubscriptionConfig(id="a", server_id="s", node_id="n1", display_name="rValue",
                           influxdb=SubscriptionInfluxConfig(measurement="Pump_A.rValue")),
        SubscriptionConfig(id="b", server_id="s", node_id="n2", display_name="rValue",
                           influxdb=SubscriptionInfluxConfig(measurement="Pump_B.rValue")),
        SubscriptionConfig(id="c", server_id="s", node_id="n3", display_name="Speed",
                           influxdb=SubscriptionInfluxConfig(measurement="Plant.Speed")),
    ]
    assert signal_labels(minimal_state) == {"a": "Pump_A.rValue", "b": "Pump_B.rValue",
                                            "c": "Speed"}
