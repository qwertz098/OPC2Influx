"""
Shared test fixtures:
- Embedded OPC UA test server (asyncua.server.Server)
- FastAPI AsyncClient (with lifespan via asgi-lifespan)
- Temporary config directory
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from asyncua import Server as OpcUaServer
from httpx import ASGITransport, AsyncClient

from app.config import AppConfig, ServersConfig, SubscriptionsConfig, InfluxDBConfig, AlertsConfig
from app.state import RuntimeState


# ---------------------------------------------------------------------------
# Embedded OPC UA server (session-scoped, shared across all tests)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def opcua_server():
    """
    Start an in-process OPC UA server with a realistic namespace:
      Objects/
        Motors/
          Motor_0/  Speed (Float), Temperature (Float)
          Motor_1/  Speed (Float), Temperature (Float)
          Motor_2/  Speed (Float), Temperature (Float)
        Sensors/
          Pressure (Float)
    """
    server = OpcUaServer()
    await server.init()
    server.set_endpoint("opc.tcp://127.0.0.1:48401")

    ns = await server.register_namespace("urn:test:opcua2influx")
    objects = server.nodes.objects

    motors = await objects.add_object(ns, "Motors")
    motor_nodes = {}
    for i in range(3):
        motor = await motors.add_object(ns, f"Motor_{i}")
        speed = await motor.add_variable(ns, "Speed", float(i * 100.0))
        temp = await motor.add_variable(ns, "Temperature", float(20.0 + i * 2))
        await speed.set_writable()
        await temp.set_writable()
        motor_nodes[f"Motor_{i}"] = {"node": motor, "Speed": speed, "Temperature": temp}

    sensors = await objects.add_object(ns, "Sensors")
    pressure = await sensors.add_variable(ns, "Pressure", 1013.25)
    await pressure.set_writable()

    async with server:
        server._test_motor_nodes = motor_nodes
        server._test_pressure = pressure
        server._test_endpoint = "opc.tcp://127.0.0.1:48401"
        server._test_ns = ns
        yield server


# ---------------------------------------------------------------------------
# FastAPI test client (function-scoped)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def async_client(tmp_config_dir):
    """AsyncClient for the FastAPI app with lifespan and a temp config dir."""
    os.environ["CONFIG_DIR"] = str(tmp_config_dir)

    # Re-import to get a fresh module (avoid stale state between tests)
    import importlib
    import app.main as main_mod
    importlib.reload(main_mod)
    the_app = main_mod.app

    async with LifespanManager(the_app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app),
            base_url="http://test",
        ) as client:
            yield client


# ---------------------------------------------------------------------------
# Temp config directory
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_config_dir(tmp_path: Path) -> Path:
    d = tmp_path / "config"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Minimal RuntimeState for unit tests
# ---------------------------------------------------------------------------

@pytest.fixture
def minimal_state(tmp_config_dir):
    config = AppConfig(
        servers=ServersConfig(),
        subscriptions=SubscriptionsConfig(),
        influxdb=InfluxDBConfig(),
        alerts=AlertsConfig(),
    )
    return RuntimeState(config_dir=tmp_config_dir, config=config)
