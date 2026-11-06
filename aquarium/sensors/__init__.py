"""Sensor and world construction from configuration."""

from __future__ import annotations

from ..config import ActuatorCfg, Config, SensorCfg
from ..units import metric_info
from .base import Sensor, SensorError
from .sim import SimWorld

_SEED_METRICS = ("temp_c", "ph", "ec_us", "orp_mv", "do_mg_l", "level_pct")

_DOSE_ML_PER_S = 2.0


def build_world(cfg: Config) -> SimWorld | None:
    """Create the simulated tank if any probe or relay is simulated."""
    if not any(s.driver == "sim" for s in cfg.sensors) and not any(
        a.driver == "sim" for a in cfg.actuators
    ):
        return None

    seeds: dict[str, float] = {}
    for sensor in cfg.sensors:
        if sensor.driver == "sim" and sensor.metric in _SEED_METRICS:
            if "value" in sensor.params:
                seeds[sensor.metric] = float(sensor.params["value"])

    light = cfg.loop("light")
    photoperiod = (10, 20)
    if light.get("start") and light.get("end"):
        try:
            photoperiod = (int(str(light["start"]).split(":")[0]),
                           int(str(light["end"]).split(":")[0]))
        except (ValueError, IndexError):
            pass

    return SimWorld(
        temp_c=seeds.get("temp_c", 25.4),
        ph=seeds.get("ph", 8.14),
        ec_us=seeds.get("ec_us", 53_200.0),
        orp_mv=seeds.get("orp_mv", 342.0),
        do_mg_l=seeds.get("do_mg_l", 6.6),
        level_pct=seeds.get("level_pct", 94.0),
        volume_l=cfg.tank.volume_l,
        photoperiod=photoperiod,
    )


def build_sensor(cfg: SensorCfg, world: SimWorld | None = None) -> Sensor:
    params = dict(cfg.params)
    params.setdefault("value", 0.0)
    if cfg.driver == "sim":
        from .sim_probe import SimSensor

        return SimSensor(cfg.metric, params, world)
    if cfg.driver == "ds18b20":
        from .ds18b20 import DS18B20Sensor

        return DS18B20Sensor(cfg.metric, params)
    if cfg.driver == "ezo":
        from .ezo import EzoSensor

        return EzoSensor(cfg.metric, params)
    if cfg.driver == "ads1115":
        from .ads1115 import ADS1115Sensor

        return ADS1115Sensor(cfg.metric, params)
    raise SensorError(f"unknown sensor driver {cfg.driver!r}")


def build_sensors(cfg: Config, world: SimWorld | None = None) -> dict[str, Sensor]:
    sensors: dict[str, Sensor] = {}
    for sensor_cfg in cfg.sensors:
        try:
            sensors[sensor_cfg.metric] = build_sensor(sensor_cfg, world)
        except SensorError as exc:
            raise SensorError(f"sensor {sensor_cfg.metric} ({sensor_cfg.driver}): {exc}") from exc
    return sensors


def sensor_units(cfg: Config) -> dict[str, str]:
    return {s.metric: metric_info(s.metric).unit for s in cfg.sensors}
