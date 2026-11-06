import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aquarium.config import (  # noqa: E402
    ActuatorCfg, AlertCfg, Config, NotifyCfg, SensorCfg, ServerCfg, TankCfg,
)
from aquarium.model import Reading  # noqa: E402


def make_config(*, temp_loop=True, ato_loop=True, co2_loop=False, light_loop=False,
                dose=None, alerts=None, sensors=None, actuators=None) -> Config:
    sensors = sensors if sensors is not None else [
        SensorCfg("temp_c", "sim", {"value": 25.4}),
        SensorCfg("ph", "sim", {"value": 8.14}),
        SensorCfg("level_pct", "sim", {"value": 94.0}),
    ]
    actuators = actuators if actuators is not None else [
        ActuatorCfg("heater", "heater", min_on_s=30, min_off_s=120, max_on_s=1800),
        ActuatorCfg("chiller", "chiller", min_on_s=60, min_off_s=300, max_on_s=1800),
        ActuatorCfg("ato_pump", "ato", min_on_s=2, min_off_s=120, max_on_s=120),
        ActuatorCfg("dose_alk", "doser", min_on_s=1, min_off_s=1, max_on_s=300,
                    params={"rate_ml_s": 2.0}),
        ActuatorCfg("co2", "co2", min_on_s=30, min_off_s=60, max_on_s=7200),
        ActuatorCfg("light", "light", min_on_s=1, min_off_s=1, max_on_s=43200),
    ]
    control: dict = {}
    if temp_loop:
        control["temp"] = {"enabled": True, "setpoint_c": 25.5, "deadband_c": 0.5,
                           "heater": "heater", "chiller": "chiller"}
    if ato_loop:
        control["ato"] = {"enabled": True, "sensor": "level_pct", "low_pct": 92.0,
                          "high_pct": 96.0, "pump": "ato_pump", "max_run_s": 120,
                          "cooldown_s": 300}
    if light_loop:
        control["light"] = {"enabled": True, "actuator": "light", "start": "10:00",
                            "end": "20:00"}
    if co2_loop:
        control["co2"] = {"enabled": True, "sensor": "ph", "target": 6.7,
                          "hysteresis": 0.05, "solenoid": "co2", "lights_only": False}
    if dose:
        control["dose"] = dose
    return Config(
        sensors=sensors,
        actuators=actuators,
        alerts=alerts or [],
        tank=TankCfg(name="Test tank", volume_l=200),
        server=ServerCfg(host="127.0.0.1", port=0, sample_interval_s=1,
                         control_interval_s=0.05, history_days=7),
        notify=NotifyCfg(),
        control=control,
        safety={"stale_after_s": 45},
    )


def reading(metric: str, value: float, ts: float) -> Reading:
    units = {"temp_c": "°C", "ph": "", "level_pct": "%", "ec_us": "µS/cm"}
    return Reading(metric, value, units.get(metric, ""), ts)


@pytest.fixture
def cfg() -> Config:
    return make_config()
