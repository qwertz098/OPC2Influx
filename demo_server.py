"""
Standalone OPC UA demo server for manual UI testing.

Namespace: urn:demo:opcua2influx
Endpoint:  opc.tcp://0.0.0.0:48400

Structure:
  Objects/
    Factory/
      Site_A/
        ProductionArea/
          Line_0..2/
            Status, Speed_rpm, ProductCount, Alarm, RecipeName, ActiveAlarm, OperatorID, CycleTime_ms
            Drives/
              Main/   Running, Speed_rpm, Current_A, Temp_C
              Feed/   Running, Speed_rpm, Current_A, Temp_C
            Conveyor/ Running, Speed_ms, Load_kg
            QualityControl/ InspectionOK, RejectCount, CameraOnline

      Site_B/
        Utilities/
          HeatExchanger_0..1/
            InletTemp_C, OutletTemp_C, FlowRate_m3h, Pressure_bar, Efficiency_pct, Pump_Running
          WaterTreatment/
            Tank_0..2/
              Level_pct, Temperature_C, Pressure_bar, InletValve_Open, OutletValve_Open

    PowerGrid/
      Mains/
        TotalPower_kW, Phase1_V, Phase1_A, Phase2_V, Phase2_A, Phase3_V, Phase3_A,
        Frequency_Hz, PowerFactor
      UPS/
        BatteryLevel_pct, OnBattery, EstRuntime_min

    TestBench/
      Sensors/
        Pressure/  Ch_0..2/  Raw, Filtered, Alarm
        Temperature/ Pt100_0..1/  Value, Offset, Ok
        Flow/ FT_001..2/  Flow_m3h, Velocity_ms, Totalizer
      Array/
        Subarray_0..1/
          Struct_0..1/
            BoolValue, IntValue, FloatValue, StringValue

Suggested strip prefixes for testing:
  "Factory.Site_A.ProductionArea"  → Line_0.Status, Line_1.Drives.Main.Speed_rpm …
  "Factory.Site_B.Utilities"       → HeatExchanger_0.InletTemp_C, WaterTreatment.Tank_0.Level_pct …
  "TestBench.Sensors"              → Pressure.Ch_0.Raw, Temperature.Pt100_0.Value …

Values update every 0.5 s with sine-wave synthesis.
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
import sys
from dataclasses import dataclass, field
from typing import Any

from asyncua import Server, ua

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger("demo_server")

ENDPOINT = "opc.tcp://localhost:48400"
NAMESPACE = "urn:demo:opcua2influx"
UPDATE_INTERVAL = 0.5

RECIPES = ["Recipe_A", "Recipe_B", "Recipe_C"]
ALARMS = ["OverTemp", "UnderSpeed", "E-Stop"]
_DEFAULT_OPERATOR_IDS = ["OP001", "OP002", "OP003"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clamp(v: float, lo: float, hi: float) -> float:
    v, lo, hi = float(v), float(lo), float(hi)
    return max(lo, min(hi, v))


def _noise(sigma: float) -> float:
    return random.gauss(0, sigma)


# ---------------------------------------------------------------------------
# Simulation state
# ---------------------------------------------------------------------------

@dataclass
class MotorState:
    speed_rpm: float
    setpoint_rpm: float
    current_a: float
    temp_c: float
    running: bool = True
    _t: float = field(default_factory=lambda: random.uniform(0, 20))

    def tick(self) -> None:
        self._t += UPDATE_INTERVAL
        if self.running:
            sp_frac = (1.0 + 0.22 * math.sin(self._t / 7.0)
                           + 0.09 * math.sin(self._t / 2.1))
            self.speed_rpm = _clamp(
                self.setpoint_rpm * sp_frac + _noise(15.0), 0, self.setpoint_rpm * 1.5)
            self.current_a = _clamp(
                self.speed_rpm / self.setpoint_rpm * 12.0 + _noise(0.8), 0, 20)
            self.temp_c = _clamp(
                65.0 + 8.0 * math.sin(self._t / 20.0) + _noise(0.5), 20, 120)
        else:
            self.speed_rpm = max(0.0, self.speed_rpm - self.setpoint_rpm * 0.1)
            self.current_a = max(0.0, self.current_a * 0.8)
            self.temp_c    = max(25.0, self.temp_c - 0.5)


@dataclass
class LineState:
    idx: int
    speed_rpm: float = 950.0
    product_count: int = 0
    alarm: bool = False
    status: bool = True
    motor_drive: MotorState = field(default_factory=lambda: MotorState(1000, 1000, 10, 55))
    motor_feed: MotorState = field(default_factory=lambda: MotorState(1500, 1500, 8, 48))
    conv_speed: float = 1.2
    conv_load: float = 80.0
    conv_running: bool = True
    recipe_idx: int = 0
    recipe_tick: int = 0
    active_alarm: str = ""
    alarm_tick: int = 0
    operator_id: str = "OP001"
    # QualityControl
    qc_ok: bool = True
    qc_rejects: int = 0
    cycle_time_ms: float = 850.0
    camera_online: bool = True
    _t: float = field(default_factory=lambda: random.uniform(0, 40))

    def tick(self) -> None:
        self._t += UPDATE_INTERVAL
        self.recipe_tick += 1
        if self.recipe_tick >= 120:
            self.recipe_idx = (self.recipe_idx + 1) % 3
            self.recipe_tick = 0

        if self.alarm_tick > 0:
            self.alarm_tick += 1
            if self.alarm_tick >= 60:
                self.active_alarm = ""
                self.alarm_tick = 0
        else:
            if random.random() < 0.003:
                self.active_alarm = random.choice(ALARMS)
                self.alarm_tick = 1

        if self.status:
            self.motor_drive.tick()
            self.motor_feed.tick()
            self.speed_rpm = _clamp(
                1000.0 + 200.0 * math.sin(self._t / 9.0 + self.idx * 2.1)
                       +  70.0 * math.sin(self._t / 2.5 + self.idx * 1.4)
                       + _noise(10.0), 500, 1500)
            self.product_count += random.randint(0, 2)
            self.conv_speed = _clamp(
                1.2 + 0.6 * math.sin(self._t / 11.0 + self.idx) + _noise(0.05), 0.3, 2.5)
            self.conv_load = _clamp(
                80.0 + 30.0 * math.sin(self._t / 8.0 + self.idx * 0.7) + _noise(3.0), 0, 200)
            if random.random() < 0.01:
                self.alarm = not self.alarm
            # QC
            self.cycle_time_ms = _clamp(
                850.0 + 80.0 * math.sin(self._t / 6.0 + self.idx) + _noise(5.0), 600, 1200)
            if random.random() < 0.02:
                self.qc_ok = not self.qc_ok
            if not self.qc_ok:
                self.qc_rejects += 1
        else:
            self.motor_drive.running = False
            self.motor_drive.tick()
            self.motor_feed.running = False
            self.motor_feed.tick()
            self.conv_running = False
            self.camera_online = False


@dataclass
class HxState:
    inlet_t: float = 90.0
    outlet_t: float = 60.0
    flow: float = 5.0
    pressure: float = 3.2
    efficiency: float = 82.0
    pump_running: bool = True
    _t: float = field(default_factory=lambda: random.uniform(0, 30))

    def tick(self) -> None:
        self._t += UPDATE_INTERVAL
        if self.pump_running:
            self.inlet_t = _clamp(
                90.0 + 12.0 * math.sin(self._t / 15.0) + _noise(0.5), 60, 120)
            self.outlet_t = _clamp(
                60.0 + 8.0 * math.sin(self._t / 11.0 + 1.0) + _noise(0.4), 40, 90)
            self.flow = _clamp(
                5.0 + 2.5 * math.sin(self._t / 8.0) + _noise(0.15), 1, 10)
            self.pressure = _clamp(
                3.2 + 0.4 * math.sin(self._t / 13.0) + _noise(0.05), 0.5, 8)
            eff_target = 80.0 + (self.flow / 5.0) * 5.0
            self.efficiency = _clamp(
                eff_target + 3.0 * math.sin(self._t / 17.0) + _noise(0.3), 50, 100)
            if random.random() < 0.005:
                self.pump_running = False
        else:
            self.flow *= 0.8
            if self.flow < 0.1:
                self.pump_running = True


@dataclass
class TankState:
    level: float = 50.0
    temp: float = 22.0
    pressure: float = 1.05
    inlet_open: bool = True
    outlet_open: bool = False
    _filling: bool = True
    just_filled: bool = False
    _t: float = field(default_factory=lambda: random.uniform(0, 60))

    def tick(self) -> None:
        self._t += UPDATE_INTERVAL
        self.just_filled = False
        if self._filling:
            self.level = _clamp(self.level + random.uniform(0.3, 0.8), 0, 100)
            if self.level >= 90.0:
                self._filling = False
                self.inlet_open = False
                self.outlet_open = True
                self.just_filled = True
        else:
            self.level = _clamp(self.level - random.uniform(0.3, 0.8), 0, 100)
            if self.level <= 10.0:
                self._filling = True
                self.inlet_open = True
                self.outlet_open = False
        self.temp = _clamp(
            22.0 + 2.0 * math.sin(self._t / 30.0) + _noise(0.05), 15, 40)
        self.pressure = _clamp(
            1.05 + 0.1 * math.sin(self._t / 18.0) + _noise(0.005), 0.9, 2.0)


@dataclass
class PowerState:
    _t: float = 0.0
    total_kw: float = 85.0
    phase_v: list = field(default_factory=lambda: [231.0, 230.5, 231.5])
    phase_a: list = field(default_factory=lambda: [120.0, 118.0, 122.0])
    freq: float = 50.0
    pf: float = 0.92

    def tick(self, line_states: list[LineState]) -> None:
        self._t += UPDATE_INTERVAL
        running = sum(1 for ls in line_states if ls.status)
        target_kw = 20.0 + running * 22.0
        self.total_kw = _clamp(
            target_kw + 15.0 * math.sin(self._t / 9.0) + _noise(2.0), 5, 200)
        self.pf = _clamp(
            0.92 + 0.05 * math.sin(self._t / 19.0) + _noise(0.005), 0.7, 1.0)
        for i in range(3):
            self.phase_v[i] = _clamp(
                230.0 + (i - 1) * 0.5 + 2.0 * math.sin(self._t / 22.0 + i) + _noise(0.8),
                210, 250)
            target_a = self.total_kw / 3 / self.phase_v[i] / self.pf * 1000
            self.phase_a[i] = _clamp(
                target_a + 5.0 * math.sin(self._t / 5.0 + i * 2) + _noise(2.0), 0, 500)
        self.freq = _clamp(
            50.0 + 0.15 * math.sin(self._t / 37.0) + _noise(0.015), 49.3, 50.7)


@dataclass
class UPSState:
    battery_pct: float = 98.0
    on_battery: bool = False
    runtime_min: float = 45.0
    _t: float = 0.0
    _discharge_tick: int = 0

    def tick(self) -> None:
        self._t += UPDATE_INTERVAL
        # Occasionally simulate a brief mains outage (~0.2 % chance per tick)
        if not self.on_battery and random.random() < 0.002:
            self.on_battery = True
            self._discharge_tick = 0
        if self.on_battery:
            self._discharge_tick += 1
            self.battery_pct = _clamp(self.battery_pct - 0.05, 0, 100)
            self.runtime_min = _clamp(self.battery_pct * 0.46, 0, 60)
            if self._discharge_tick >= 60:       # ~30 s simulated outage → mains restored
                self.on_battery = False
        else:
            self.battery_pct = _clamp(
                self.battery_pct + 0.02 + _noise(0.005), 0, 100)
            self.runtime_min = _clamp(self.battery_pct * 0.46, 0, 60)


@dataclass
class SensorChannelState:
    """Single pressure/flow/temperature channel."""
    raw: float
    _offset: float
    _amplitude: float
    _period: float
    alarm: bool = False
    ok: bool = True
    totalizer: float = 0.0
    _t: float = field(default_factory=lambda: random.uniform(0, 60))

    def tick(self) -> None:
        self._t += UPDATE_INTERVAL
        self.raw = _clamp(
            self._offset + self._amplitude * math.sin(self._t / self._period) + _noise(self._amplitude * 0.04),
            0, self._offset * 2,
        )
        filtered = _clamp(
            self._offset + self._amplitude * math.sin(self._t / self._period),
            0, self._offset * 2,
        )
        self.filtered = round(filtered, 3)
        self.alarm = self.raw > self._offset + self._amplitude * 0.9
        if random.random() < 0.001:
            self.ok = not self.ok
        self.totalizer += abs(self.raw) * UPDATE_INTERVAL


@dataclass
class StructState:
    bool_val: bool = False
    int_val: int = 0
    float_val: float = 0.0
    str_val: str = "A"
    _t: float = field(default_factory=lambda: random.uniform(0, 10))
    _str_tick: int = 0
    _str_idx: int = 0
    _STR_CYCLE: list = field(default_factory=lambda: ["Alpha", "Beta", "Gamma", "Delta"])

    def tick(self) -> None:
        self._t += UPDATE_INTERVAL
        self.bool_val = math.sin(self._t / 3.0) >= 0
        self.int_val  = int(50 + 40 * math.sin(self._t / 7.0)) + random.randint(-2, 2)
        self.float_val = round(1.5 + 1.0 * math.sin(self._t / 5.0) + _noise(0.05), 4)
        self._str_tick += 1
        if self._str_tick >= 40:
            self._str_tick = 0
            self._str_idx = (self._str_idx + 1) % len(self._STR_CYCLE)
        self.str_val = self._STR_CYCLE[self._str_idx]


# ---------------------------------------------------------------------------
# Node registry dataclasses
# ---------------------------------------------------------------------------

@dataclass
class LineNodes:
    status: Any; speed_rpm: Any; product_count: Any; alarm: Any
    drive_running: Any; drive_speed: Any; drive_current: Any; drive_temp: Any
    feed_running: Any; feed_speed: Any; feed_current: Any; feed_temp: Any
    conv_running: Any; conv_speed: Any; conv_load: Any
    recipe_name: Any; active_alarm_str: Any; operator_id: Any; cycle_time_ms: Any
    qc_ok: Any; qc_rejects: Any; camera_online: Any


@dataclass
class HxNodes:
    inlet_t: Any; outlet_t: Any; flow: Any; pressure: Any; efficiency: Any; pump_running: Any


@dataclass
class TankNodes:
    level: Any; temp: Any; pressure: Any; inlet_open: Any; outlet_open: Any


@dataclass
class PowerNodes:
    total_kw: Any; phase1_v: Any; phase1_a: Any; phase2_v: Any; phase2_a: Any
    phase3_v: Any; phase3_a: Any; freq: Any; pf: Any


@dataclass
class UPSNodes:
    battery_pct: Any; on_battery: Any; runtime_min: Any


@dataclass
class SensorChannelNodes:
    raw: Any; filtered: Any; alarm_or_ok: Any; extra: Any  # extra = totalizer or offset or None


@dataclass
class StructNodes:
    bool_val: Any; int_val: Any; float_val: Any; str_val: Any


# ---------------------------------------------------------------------------
# Namespace builder
# ---------------------------------------------------------------------------

async def _add_line(objects_parent, ns: int, name: str) -> LineNodes:
    line = await objects_parent.add_object(ns, name)

    status      = await line.add_variable(ns, "Status",       True)
    speed       = await line.add_variable(ns, "Speed_rpm",    1000.0)
    count       = await line.add_variable(ns, "ProductCount", 0)
    alarm       = await line.add_variable(ns, "Alarm",        False)
    cycle_time  = await line.add_variable(ns, "CycleTime_ms", 850.0)
    recipe      = await line.add_variable(ns, "RecipeName",   "Recipe_A")
    active_alarm= await line.add_variable(ns, "ActiveAlarm",  "")
    operator    = await line.add_variable(ns, "OperatorID",   "OP001")
    for n in (recipe, active_alarm, operator):
        await n.set_writable()

    drives = await line.add_object(ns, "Drives")
    main   = await drives.add_object(ns, "Main")
    d_run  = await main.add_variable(ns, "Running",   True)
    d_spd  = await main.add_variable(ns, "Speed_rpm", 1000.0)
    d_cur  = await main.add_variable(ns, "Current_A", 10.0)
    d_tmp  = await main.add_variable(ns, "Temp_C",    55.0)

    feed_obj = await drives.add_object(ns, "Feed")
    f_run  = await feed_obj.add_variable(ns, "Running",   True)
    f_spd  = await feed_obj.add_variable(ns, "Speed_rpm", 1500.0)
    f_cur  = await feed_obj.add_variable(ns, "Current_A", 8.0)
    f_tmp  = await feed_obj.add_variable(ns, "Temp_C",    48.0)

    conv   = await line.add_object(ns, "Conveyor")
    c_run  = await conv.add_variable(ns, "Running",  True)
    c_spd  = await conv.add_variable(ns, "Speed_ms", 1.2)
    c_lod  = await conv.add_variable(ns, "Load_kg",  80.0)

    qc      = await line.add_object(ns, "QualityControl")
    qc_ok   = await qc.add_variable(ns, "InspectionOK",  True)
    qc_rej  = await qc.add_variable(ns, "RejectCount",   0)
    cam_ok  = await qc.add_variable(ns, "CameraOnline",  True)

    for n in (status, speed, count, alarm, cycle_time,
              d_run, d_spd, d_cur, d_tmp,
              f_run, f_spd, f_cur, f_tmp,
              c_run, c_spd, c_lod,
              qc_ok, qc_rej, cam_ok):
        await n.set_writable()

    return LineNodes(
        status=status, speed_rpm=speed, product_count=count, alarm=alarm,
        drive_running=d_run, drive_speed=d_spd, drive_current=d_cur, drive_temp=d_tmp,
        feed_running=f_run, feed_speed=f_spd, feed_current=f_cur, feed_temp=f_tmp,
        conv_running=c_run, conv_speed=c_spd, conv_load=c_lod,
        recipe_name=recipe, active_alarm_str=active_alarm, operator_id=operator,
        cycle_time_ms=cycle_time, qc_ok=qc_ok, qc_rejects=qc_rej, camera_online=cam_ok,
    )


async def _add_hx(parent, ns: int, name: str) -> HxNodes:
    hx   = await parent.add_object(ns, name)
    n_it = await hx.add_variable(ns, "InletTemp_C",    90.0)
    n_ot = await hx.add_variable(ns, "OutletTemp_C",   60.0)
    n_fl = await hx.add_variable(ns, "FlowRate_m3h",   5.0)
    n_pr = await hx.add_variable(ns, "Pressure_bar",   3.2)
    n_ef = await hx.add_variable(ns, "Efficiency_pct", 82.0)
    n_pu = await hx.add_variable(ns, "Pump_Running",   True)
    for n in (n_it, n_ot, n_fl, n_pr, n_ef, n_pu):
        await n.set_writable()
    return HxNodes(n_it, n_ot, n_fl, n_pr, n_ef, n_pu)


async def _add_tank(parent, ns: int, name: str, init_level: float = 50.0) -> TankNodes:
    tank = await parent.add_object(ns, name)
    n_lv = await tank.add_variable(ns, "Level_pct",       init_level)
    n_tp = await tank.add_variable(ns, "Temperature_C",   22.0)
    n_pr = await tank.add_variable(ns, "Pressure_bar",    1.05)
    n_iv = await tank.add_variable(ns, "InletValve_Open", True)
    n_ov = await tank.add_variable(ns, "OutletValve_Open",False)
    for n in (n_lv, n_tp, n_pr, n_iv, n_ov):
        await n.set_writable()
    return TankNodes(n_lv, n_tp, n_pr, n_iv, n_ov)


async def build_namespace(server: Server, ns: int) -> tuple:
    objects = server.nodes.objects

    # -----------------------------------------------------------------------
    # Factory / Site_A / ProductionArea / Line_0..2
    # -----------------------------------------------------------------------
    factory   = await objects.add_object(ns, "Factory")
    site_a    = await factory.add_object(ns, "Site_A")
    prod_area = await site_a.add_object(ns, "ProductionArea")

    line_nodes: list[LineNodes] = []
    for i in range(3):
        ln = await _add_line(prod_area, ns, f"Line_{i}")
        line_nodes.append(ln)

    # -----------------------------------------------------------------------
    # Factory / Site_B / Utilities / HeatExchanger_0..1
    #                              / WaterTreatment / Tank_0..2
    # -----------------------------------------------------------------------
    site_b    = await factory.add_object(ns, "Site_B")
    utilities = await site_b.add_object(ns, "Utilities")

    hx_nodes: list[HxNodes] = []
    for i in range(2):
        hx_nodes.append(await _add_hx(utilities, ns, f"HeatExchanger_{i}"))

    water_treatment = await utilities.add_object(ns, "WaterTreatment")
    tank_nodes: list[TankNodes] = []
    for i in range(3):
        tank_nodes.append(await _add_tank(water_treatment, ns, f"Tank_{i}", 20.0 + i * 25))

    # -----------------------------------------------------------------------
    # PowerGrid / Mains  +  PowerGrid / UPS
    # -----------------------------------------------------------------------
    power_grid = await objects.add_object(ns, "PowerGrid")
    mains      = await power_grid.add_object(ns, "Mains")
    p_tot = await mains.add_variable(ns, "TotalPower_kW", 85.0)
    p1v   = await mains.add_variable(ns, "Phase1_V",      231.0)
    p1a   = await mains.add_variable(ns, "Phase1_A",      120.0)
    p2v   = await mains.add_variable(ns, "Phase2_V",      230.5)
    p2a   = await mains.add_variable(ns, "Phase2_A",      118.0)
    p3v   = await mains.add_variable(ns, "Phase3_V",      231.5)
    p3a   = await mains.add_variable(ns, "Phase3_A",      122.0)
    p_fr  = await mains.add_variable(ns, "Frequency_Hz",  50.0)
    p_pf  = await mains.add_variable(ns, "PowerFactor",   0.92)
    for n in (p_tot, p1v, p1a, p2v, p2a, p3v, p3a, p_fr, p_pf):
        await n.set_writable()
    power_nodes = PowerNodes(p_tot, p1v, p1a, p2v, p2a, p3v, p3a, p_fr, p_pf)

    ups_obj  = await power_grid.add_object(ns, "UPS")
    u_bat    = await ups_obj.add_variable(ns, "BatteryLevel_pct", 98.0)
    u_on     = await ups_obj.add_variable(ns, "OnBattery",        False)
    u_run    = await ups_obj.add_variable(ns, "EstRuntime_min",   45.0)
    for n in (u_bat, u_on, u_run):
        await n.set_writable()
    ups_nodes = UPSNodes(u_bat, u_on, u_run)

    # -----------------------------------------------------------------------
    # TestBench / Sensors / Pressure / Ch_0..2
    #                     / Temperature / Pt100_0..1
    #                     / Flow / FT_001..2
    # TestBench / Array / Subarray_0..1 / Struct_0..1
    # -----------------------------------------------------------------------
    testbench = await objects.add_object(ns, "TestBench")
    sensors   = await testbench.add_object(ns, "Sensors")

    # Pressure channels
    pressure_obj = await sensors.add_object(ns, "Pressure")
    pressure_nodes: list[SensorChannelNodes] = []
    for i in range(3):
        ch = await pressure_obj.add_object(ns, f"Ch_{i}")
        r  = await ch.add_variable(ns, "Raw",      0.0)
        f  = await ch.add_variable(ns, "Filtered", 0.0)
        a  = await ch.add_variable(ns, "Alarm",    False)
        for n in (r, f, a): await n.set_writable()
        pressure_nodes.append(SensorChannelNodes(r, f, a, None))

    # Temperature (Pt100)
    temp_obj = await sensors.add_object(ns, "Temperature")
    temp_nodes: list[SensorChannelNodes] = []
    for i in range(2):
        pt = await temp_obj.add_object(ns, f"Pt100_{i}")
        r  = await pt.add_variable(ns, "Value",  0.0)
        f  = await pt.add_variable(ns, "Offset", 0.0)
        a  = await pt.add_variable(ns, "Ok",     True)
        for n in (r, f, a): await n.set_writable()
        temp_nodes.append(SensorChannelNodes(r, f, a, None))

    # Flow transmitters
    flow_obj = await sensors.add_object(ns, "Flow")
    flow_nodes: list[SensorChannelNodes] = []
    for tag in ("FT_001", "FT_002"):
        ft   = await flow_obj.add_object(ns, tag)
        r    = await ft.add_variable(ns, "Flow_m3h",   0.0)
        f    = await ft.add_variable(ns, "Velocity_ms",0.0)
        tot  = await ft.add_variable(ns, "Totalizer",  0.0)
        for n in (r, f, tot): await n.set_writable()
        flow_nodes.append(SensorChannelNodes(r, f, tot, None))

    # Array (generic structured tree, kept for backwards-compatibility testing)
    array_obj  = await testbench.add_object(ns, "Array")
    array_nodes: list[list[StructNodes]] = []
    for si in range(2):
        sub_obj = await array_obj.add_object(ns, f"Subarray_{si}")
        sub_row: list[StructNodes] = []
        for ti in range(2):
            struct_obj = await sub_obj.add_object(ns, f"Struct_{ti}")
            sn_bool  = await struct_obj.add_variable(ns, "BoolValue",   False)
            sn_int   = await struct_obj.add_variable(ns, "IntValue",    0)
            sn_float = await struct_obj.add_variable(ns, "FloatValue",  0.0)
            sn_str   = await struct_obj.add_variable(ns, "StringValue", "init")
            for n in (sn_bool, sn_int, sn_float, sn_str):
                await n.set_writable()
            sub_row.append(StructNodes(sn_bool, sn_int, sn_float, sn_str))
        array_nodes.append(sub_row)

    return (line_nodes, hx_nodes, tank_nodes, power_nodes, ups_nodes,
            pressure_nodes, temp_nodes, flow_nodes, array_nodes)


# ---------------------------------------------------------------------------
# Simulation loop
# ---------------------------------------------------------------------------

async def simulate(
    line_nodes:     list[LineNodes],
    hx_nodes:       list[HxNodes],
    tank_nodes:     list[TankNodes],
    power_nodes:    PowerNodes,
    ups_nodes:      UPSNodes,
    pressure_nodes: list[SensorChannelNodes],
    temp_nodes:     list[SensorChannelNodes],
    flow_nodes:     list[SensorChannelNodes],
    array_nodes:    list[list[StructNodes]],
) -> None:
    line_states = [LineState(idx=i, operator_id=_DEFAULT_OPERATOR_IDS[i]) for i in range(3)]
    hx_states   = [HxState(inlet_t=90.0 + i * 5) for i in range(2)]
    tank_states = [TankState(level=20.0 + i * 25) for i in range(3)]
    power_state = PowerState()
    ups_state   = UPSState()

    # Pressure 0–2: offsets ~2, 5, 8 bar; amplitude ~0.8, 1.5, 2.0
    pressure_states = [
        SensorChannelState(raw=2.0, _offset=2.0, _amplitude=0.8,  _period=9.0),
        SensorChannelState(raw=5.0, _offset=5.0, _amplitude=1.5,  _period=12.0),
        SensorChannelState(raw=8.0, _offset=8.0, _amplitude=2.0,  _period=7.0),
    ]
    # Temperature (Pt100): ~60–90 °C, ~20–35 °C
    temp_states = [
        SensorChannelState(raw=75.0, _offset=75.0, _amplitude=8.0,  _period=20.0),
        SensorChannelState(raw=27.0, _offset=27.0, _amplitude=3.5,  _period=25.0),
    ]
    # Flow transmitters: m³/h
    flow_states = [
        SensorChannelState(raw=12.0, _offset=12.0, _amplitude=4.0, _period=14.0),
        SensorChannelState(raw=8.0,  _offset=8.0,  _amplitude=3.0, _period=18.0),
    ]
    array_states = [
        [StructState(_t=float(si * 5 + ti * 2)) for ti in range(2)]
        for si in range(2)
    ]

    tick = 0
    while True:
        await asyncio.sleep(UPDATE_INTERVAL)
        tick += 1
        try:
          await _simulate_tick(
              tick, line_states, line_nodes, hx_states, hx_nodes,
              tank_states, tank_nodes, power_state, power_nodes,
              ups_state, ups_nodes, pressure_states, pressure_nodes,
              temp_states, temp_nodes, flow_states, flow_nodes,
              array_states, array_nodes,
          )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Simulation tick %d failed — continuing", tick)


async def _simulate_tick(
    tick, line_states, line_nodes, hx_states, hx_nodes,
    tank_states, tank_nodes, power_state, power_nodes,
    ups_state, ups_nodes, pressure_states, pressure_nodes,
    temp_states, temp_nodes, flow_states, flow_nodes,
    array_states, array_nodes,
) -> None:
        # Production lines
        for i, (ls, ln) in enumerate(zip(line_states, line_nodes)):
            ls.tick()
            await ln.status.write_value(ls.status)
            await ln.speed_rpm.write_value(round(ls.speed_rpm, 1))
            await ln.product_count.write_value(ls.product_count)
            await ln.alarm.write_value(ls.alarm)
            await ln.cycle_time_ms.write_value(round(ls.cycle_time_ms, 1))
            await ln.recipe_name.write_value(RECIPES[ls.recipe_idx])
            await ln.active_alarm_str.write_value(ls.active_alarm)
            await ln.operator_id.write_value(ls.operator_id)

            m = ls.motor_drive
            await ln.drive_running.write_value(m.running)
            await ln.drive_speed.write_value(round(m.speed_rpm, 1))
            await ln.drive_current.write_value(round(m.current_a, 2))
            await ln.drive_temp.write_value(round(m.temp_c, 1))

            m = ls.motor_feed
            await ln.feed_running.write_value(m.running)
            await ln.feed_speed.write_value(round(m.speed_rpm, 1))
            await ln.feed_current.write_value(round(m.current_a, 2))
            await ln.feed_temp.write_value(round(m.temp_c, 1))

            await ln.conv_running.write_value(ls.conv_running)
            await ln.conv_speed.write_value(round(ls.conv_speed, 3))
            await ln.conv_load.write_value(round(ls.conv_load, 1))

            await ln.qc_ok.write_value(ls.qc_ok)
            await ln.qc_rejects.write_value(ls.qc_rejects)
            await ln.camera_online.write_value(ls.camera_online)

        # Heat exchangers
        for hs, hn in zip(hx_states, hx_nodes):
            hs.tick()
            await hn.inlet_t.write_value(round(hs.inlet_t, 1))
            await hn.outlet_t.write_value(round(hs.outlet_t, 1))
            await hn.flow.write_value(round(hs.flow, 2))
            await hn.pressure.write_value(round(hs.pressure, 3))
            await hn.efficiency.write_value(round(hs.efficiency, 1))
            await hn.pump_running.write_value(hs.pump_running)

        # Tanks
        for i, (ts, tn) in enumerate(zip(tank_states, tank_nodes)):
            ts.tick()
            await tn.level.write_value(round(ts.level, 1))
            await tn.temp.write_value(round(ts.temp, 1))
            await tn.pressure.write_value(round(ts.pressure, 3))
            await tn.inlet_open.write_value(ts.inlet_open)
            await tn.outlet_open.write_value(ts.outlet_open)
            if ts.just_filled:
                logger.info("WaterTreatment/Tank_%d FULL — switching to drain", i)

        # Power
        power_state.tick(line_states)
        pn = power_nodes
        await pn.total_kw.write_value(round(power_state.total_kw, 2))
        await pn.phase1_v.write_value(round(power_state.phase_v[0], 1))
        await pn.phase1_a.write_value(round(power_state.phase_a[0], 1))
        await pn.phase2_v.write_value(round(power_state.phase_v[1], 1))
        await pn.phase2_a.write_value(round(power_state.phase_a[1], 1))
        await pn.phase3_v.write_value(round(power_state.phase_v[2], 1))
        await pn.phase3_a.write_value(round(power_state.phase_a[2], 1))
        await pn.freq.write_value(round(power_state.freq, 3))
        await pn.pf.write_value(round(power_state.pf, 3))

        # UPS
        ups_state.tick()
        await ups_nodes.battery_pct.write_value(round(ups_state.battery_pct, 1))
        await ups_nodes.on_battery.write_value(ups_state.on_battery)
        await ups_nodes.runtime_min.write_value(round(ups_state.runtime_min, 1))

        # Pressure sensors
        for ss, sn in zip(pressure_states, pressure_nodes):
            ss.tick()
            await sn.raw.write_value(round(ss.raw, 3))
            await sn.filtered.write_value(ss.filtered)
            await sn.alarm_or_ok.write_value(ss.alarm)

        # Temperature sensors
        for ss, sn in zip(temp_states, temp_nodes):
            ss.tick()
            await sn.raw.write_value(round(ss.raw, 2))
            await sn.filtered.write_value(round(ss.filtered, 3))
            await sn.alarm_or_ok.write_value(ss.ok)

        # Flow transmitters
        for ss, sn in zip(flow_states, flow_nodes):
            ss.tick()
            velocity = ss.raw / 0.785  # pipe cross-section ~0.785 m²
            await sn.raw.write_value(round(ss.raw, 3))
            await sn.filtered.write_value(round(velocity, 3))
            await sn.alarm_or_ok.write_value(round(ss.totalizer, 1))

        # Array / Subarray / Struct
        for subarray_states, subarray_nodes in zip(array_states, array_nodes):
            for ss, sn in zip(subarray_states, subarray_nodes):
                ss.tick()
                await sn.bool_val.write_value(ss.bool_val)
                await sn.int_val.write_value(ss.int_val)
                await sn.float_val.write_value(ss.float_val)
                await sn.str_val.write_value(ss.str_val)

        if tick % 30 == 0:
            logger.info("Tick %d — Lines: %s  Power: %.1f kW  UPS: %.0f%%  OnBat: %s",
                        tick,
                        ["RUN" if ls.status else "STOP" for ls in line_states],
                        power_state.total_kw,
                        ups_state.battery_pct,
                        ups_state.on_battery)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    server = Server()
    await server.init()
    server.set_endpoint(ENDPOINT)
    server.set_server_name("OpcUA2InfluxDB Demo Server")

    ns = await server.register_namespace(NAMESPACE)
    nodes = await build_namespace(server, ns)

    async with server:
        logger.info("=" * 70)
        logger.info("OPC UA Demo Server running")
        logger.info("  Endpoint  : %s", ENDPOINT)
        logger.info("  Namespace : %s  (idx %d)", NAMESPACE, ns)
        logger.info("")
        logger.info("  Tree:")
        logger.info("    Factory/Site_A/ProductionArea/Line_0..2/")
        logger.info("      Status, Speed_rpm, ProductCount, Alarm, CycleTime_ms,")
        logger.info("      RecipeName, ActiveAlarm, OperatorID")
        logger.info("      Drives/Main/  Running, Speed_rpm, Current_A, Temp_C")
        logger.info("      Drives/Feed/  Running, Speed_rpm, Current_A, Temp_C")
        logger.info("      Conveyor/     Running, Speed_ms, Load_kg")
        logger.info("      QualityControl/ InspectionOK, RejectCount, CameraOnline")
        logger.info("    Factory/Site_B/Utilities/HeatExchanger_0..1/")
        logger.info("      InletTemp_C, OutletTemp_C, FlowRate_m3h, Pressure_bar,")
        logger.info("      Efficiency_pct, Pump_Running")
        logger.info("    Factory/Site_B/Utilities/WaterTreatment/Tank_0..2/")
        logger.info("      Level_pct, Temperature_C, Pressure_bar,")
        logger.info("      InletValve_Open, OutletValve_Open")
        logger.info("    PowerGrid/Mains/  TotalPower_kW, Phase*_V/A, Frequency_Hz, PowerFactor")
        logger.info("    PowerGrid/UPS/    BatteryLevel_pct, OnBattery, EstRuntime_min")
        logger.info("    TestBench/Sensors/Pressure/Ch_0..2/  Raw, Filtered, Alarm")
        logger.info("    TestBench/Sensors/Temperature/Pt100_0..1/  Value, Offset, Ok")
        logger.info("    TestBench/Sensors/Flow/FT_001..2/  Flow_m3h, Velocity_ms, Totalizer")
        logger.info("    TestBench/Array/Subarray_0..1/Struct_0..1/")
        logger.info("      BoolValue, IntValue, FloatValue, StringValue")
        logger.info("")
        logger.info("  Suggested strip prefixes:")
        logger.info("    'Factory.Site_A.ProductionArea'")
        logger.info("      → Line_0.Status, Line_1.Drives.Main.Speed_rpm …")
        logger.info("    'Factory.Site_B.Utilities'")
        logger.info("      → HeatExchanger_0.InletTemp_C, WaterTreatment.Tank_0.Level_pct …")
        logger.info("    'TestBench.Sensors'")
        logger.info("      → Pressure.Ch_0.Raw, Flow.FT_001.Flow_m3h …")
        logger.info("    'PowerGrid'")
        logger.info("      → Mains.TotalPower_kW, UPS.BatteryLevel_pct …")
        logger.info("")
        logger.info("  Update    : every %.1f s", UPDATE_INTERVAL)
        logger.info("  Press Ctrl+C to stop")
        logger.info("=" * 70)

        sim_task = asyncio.create_task(simulate(*nodes))

        def _on_sim_done(t: asyncio.Task) -> None:
            if not t.cancelled() and t.exception():
                logger.error("Simulation task crashed: %s", t.exception(), exc_info=t.exception())

        sim_task.add_done_callback(_on_sim_done)
        try:
            await asyncio.shield(sim_task)
        except (asyncio.CancelledError, KeyboardInterrupt, Exception):
            pass
        finally:
            sim_task.cancel()
            try:
                await sim_task
            except (asyncio.CancelledError, Exception):
                pass
    logger.info("Server stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
