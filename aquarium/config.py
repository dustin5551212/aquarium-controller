"""Configuration loading and validation.

A single TOML file describes the tank, its probes, its relays and how the
control loops should behave.  Everything is validated up front: a controller
that silently ignores a typo'd actuator name would be worse than one that
refuses to start.
"""

from __future__ import annotations

import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .units import METRICS, metric_info, DERIVED

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "aquarium" / "config.toml"
BUNDLED_CONFIG = Path(__file__).resolve().parent.parent / "config" / "default.toml"

SENSOR_DRIVERS = {"sim", "ds18b20", "ezo", "ads1115"}
ACTUATOR_DRIVERS = {"sim", "http", "gpio"}
ACTUATOR_KINDS = {"heater", "chiller", "ato", "doser", "co2", "light", "pump", "fan"}


class ConfigError(Exception):
    """Raised when the configuration is missing, malformed or inconsistent."""


@dataclass(slots=True)
class SensorCfg:
    metric: str
    driver: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ActuatorCfg:
    id: str
    kind: str
    label: str = ""
    driver: str = "sim"
    min_on_s: float = 30.0
    min_off_s: float = 60.0
    max_on_s: float = 3600.0
    fail_safe_off: bool = True
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AlertCfg:
    metric: str
    above: float | None = None
    below: float | None = None
    hold_s: float = 60.0
    severity: str = "warning"
    message: str = ""

    @property
    def rule_id(self) -> str:
        if self.above is not None:
            return f"{self.metric}>={self.above:g}"
        if self.below is not None:
            return f"{self.metric}<={self.below:g}"
        return f"{self.metric}:unconfigured"


@dataclass(slots=True)
class TankCfg:
    name: str = "Aquarium"
    volume_l: float = 200.0
    water_type: str = "reef"


@dataclass(slots=True)
class ServerCfg:
    host: str = "127.0.0.1"
    port: int = 5176
    sample_interval_s: float = 5.0
    control_interval_s: float = 1.0
    history_days: int = 30
    token: str = ""


@dataclass(slots=True)
class NotifyCfg:
    webhook: str = ""
    command: str = ""
    cooldown_s: float = 300.0


@dataclass(slots=True)
class Config:
    sensors: list[SensorCfg]
    actuators: list[ActuatorCfg]
    alerts: list[AlertCfg]
    tank: TankCfg
    server: ServerCfg
    notify: NotifyCfg
    control: dict[str, Any] = field(default_factory=dict)
    safety: dict[str, Any] = field(default_factory=dict)
    source: Path | None = None

    # -- lookups ---------------------------------------------------------
    def actuator(self, actuator_id: str) -> ActuatorCfg:
        for a in self.actuators:
            if a.id == actuator_id:
                return a
        raise ConfigError(f"unknown actuator {actuator_id!r}")

    def sensor_for(self, metric: str) -> SensorCfg | None:
        for s in self.sensors:
            if s.metric == metric:
                return s
        return None

    def loop(self, name: str) -> dict[str, Any]:
        """Return a control loop section, defaulting to enabled=False."""
        section = self.control.get(name) or {}
        return section if isinstance(section, dict) else {}

    @property
    def stale_after_s(self) -> float:
        return float(self.safety.get("stale_after_s", 45.0))

    @property
    def mode(self) -> str:
        if any(s.driver == "sim" for s in self.sensors) or any(
            a.driver == "sim" for a in self.actuators
        ):
            return "simulation"
        return "hardware"

    def public_dict(self) -> dict:
        return {
            "tank": {"name": self.tank.name, "volume_l": self.tank.volume_l,
                     "water_type": self.tank.water_type},
            "server": {"host": self.server.host, "port": self.server.port,
                       "sample_interval_s": self.server.sample_interval_s,
                       "history_days": self.server.history_days},
            "sensors": [{"metric": s.metric, "driver": s.driver} for s in self.sensors],
            "actuators": [{"id": a.id, "kind": a.kind, "label": a.label,
                           "driver": a.driver} for a in self.actuators],
            "mode": self.mode,
            "source": str(self.source) if self.source else None,
        }


# ------------------------------------------------------------------ loading --

def _as_float(value: Any, name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be a number, got {value!r}") from exc


def load(path: str | Path | None = None) -> Config:
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not path.exists():
        raise ConfigError(
            f"no configuration at {path}. Run `aqctl init` to install the "
            f"default configuration, or pass --config."
        )
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc

    sensors = [
        SensorCfg(
            metric=str(item["metric"]),
            driver=str(item.get("driver", "sim")),
            params=dict(item.get("params") or {}),
        )
        for item in raw.get("sensors", [])
    ]

    actuators = [
        ActuatorCfg(
            id=str(item["id"]),
            kind=str(item.get("kind", "pump")),
            label=str(item.get("label", "")),
            driver=str(item.get("driver", "sim")),
            min_on_s=_as_float(item.get("min_on_s", 30), "min_on_s"),
            min_off_s=_as_float(item.get("min_off_s", 60), "min_off_s"),
            max_on_s=_as_float(item.get("max_on_s", 3600), "max_on_s"),
            fail_safe_off=bool(item.get("fail_safe_off", True)),
            params=dict(item.get("params") or {}),
        )
        for item in raw.get("actuators", [])
    ]

    alerts = [
        AlertCfg(
            metric=str(item["metric"]),
            above=_as_float(item["above"], "above") if "above" in item else None,
            below=_as_float(item["below"], "below") if "below" in item else None,
            hold_s=_as_float(item.get("hold_s", 60), "hold_s"),
            severity=str(item.get("severity", "warning")),
            message=str(item.get("message") or f"{item['metric']} out of range"),
        )
        for item in raw.get("alerts", [])
    ]

    tank_raw = raw.get("tank") or {}
    tank = TankCfg(
        name=str(tank_raw.get("name", "Aquarium")),
        volume_l=_as_float(tank_raw.get("volume_l", 200), "volume_l"),
        water_type=str(tank_raw.get("water_type", "reef")),
    )

    server_raw = raw.get("server") or {}
    server = ServerCfg(
        host=str(server_raw.get("host", "127.0.0.1")),
        port=int(server_raw.get("port", 5176)),
        sample_interval_s=_as_float(server_raw.get("sample_interval_s", 5), "sample_interval_s"),
        control_interval_s=_as_float(server_raw.get("control_interval_s", 1.0), "control_interval_s"),
        history_days=int(server_raw.get("history_days", 30)),
        token=str(server_raw.get("token", "")),
    )

    notify_raw = raw.get("notify") or {}
    notify = NotifyCfg(
        webhook=str(notify_raw.get("webhook", "")),
        command=str(notify_raw.get("command", "")),
        cooldown_s=_as_float(notify_raw.get("cooldown_s", 300), "cooldown_s"),
    )

    cfg = Config(
        sensors=sensors,
        actuators=actuators,
        alerts=alerts,
        tank=tank,
        server=server,
        notify=notify,
        control=dict(raw.get("control") or {}),
        safety=dict(raw.get("safety") or {}),
        source=path,
    )
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    if not cfg.sensors:
        raise ConfigError("no sensors configured")
    if not cfg.actuators:
        raise ConfigError("no actuators configured")

    seen_metrics: set[str] = set()
    for s in cfg.sensors:
        if s.metric not in METRICS:
            known = ", ".join(sorted(METRICS))
            raise ConfigError(f"unknown metric {s.metric!r} (known: {known})")
        if s.metric in seen_metrics:
            raise ConfigError(f"duplicate sensor for metric {s.metric!r}")
        seen_metrics.add(s.metric)
        if s.driver not in SENSOR_DRIVERS:
            raise ConfigError(f"sensor {s.metric}: unknown driver {s.driver!r}")

    ids: set[str] = set()
    for a in cfg.actuators:
        if a.id in ids:
            raise ConfigError(f"duplicate actuator id {a.id!r}")
        ids.add(a.id)
        if a.driver not in ACTUATOR_DRIVERS:
            raise ConfigError(f"actuator {a.id}: unknown driver {a.driver!r}")
        if a.kind not in ACTUATOR_KINDS:
            raise ConfigError(f"actuator {a.id}: unknown kind {a.kind!r}")
        if a.max_on_s <= 0:
            raise ConfigError(f"actuator {a.id}: max_on_s must be positive")

    available = seen_metrics | set(DERIVED)

    def need_metric(metric: Any, where: str) -> None:
        if metric not in available:
            raise ConfigError(
                f"{where} references metric {metric!r}, which no sensor provides"
            )

    def need_actuator(actuator_id: Any, where: str) -> None:
        if actuator_id not in ids:
            raise ConfigError(f"{where} references unknown actuator {actuator_id!r}")

    temp_loop = cfg.loop("temp")
    if temp_loop.get("enabled", False):
        need_metric("temp_c", "control.temp")
        if temp_loop.get("heater"):
            need_actuator(temp_loop["heater"], "control.temp.heater")
        if temp_loop.get("chiller"):
            need_actuator(temp_loop["chiller"], "control.temp.chiller")

    ato = cfg.loop("ato")
    if ato.get("enabled", False):
        need_metric(ato.get("sensor", "level_pct"), "control.ato")
        need_actuator(ato.get("pump"), "control.ato.pump")

    light = cfg.loop("light")
    if light.get("enabled", False):
        need_actuator(light.get("actuator"), "control.light")

    co2 = cfg.loop("co2")
    if co2.get("enabled", False):
        need_metric(co2.get("sensor", "ph"), "control.co2")
        need_actuator(co2.get("solenoid"), "control.co2")

    for dose in cfg.control.get("dose", []) or []:
        need_actuator(dose.get("pump"), "control.dose")
        if not dose.get("schedule"):
            raise ConfigError(f"dose pump {dose.get('pump')!r}: schedule is empty")
        for slot in dose["schedule"]:
            if not _valid_hhmm(str(slot)):
                raise ConfigError(f"dose pump {dose.get('pump')!r}: bad schedule slot {slot!r}")

    for alert in cfg.alerts:
        need_metric(alert.metric, f"alert {alert.rule_id}")
        if alert.above is None and alert.below is None:
            raise ConfigError(f"alert {alert.rule_id}: needs above or below")
        if alert.above is not None and alert.below is not None:
            raise ConfigError(f"alert {alert.rule_id}: set either above or below, not both")
        if alert.above is not None and alert.above > metric_info(alert.metric).high:
            raise ConfigError(f"alert {alert.rule_id}: limit outside plausible range")
        if alert.below is not None and alert.below < metric_info(alert.metric).low:
            raise ConfigError(f"alert {alert.rule_id}: limit outside plausible range")

    if not (1 <= cfg.server.port <= 65535):
        raise ConfigError(f"server.port out of range: {cfg.server.port}")
    if cfg.server.control_interval_s <= 0 or cfg.server.sample_interval_s <= 0:
        raise ConfigError("control_interval_s and sample_interval_s must be positive")


def _valid_hhmm(value: str) -> bool:
    try:
        hh, mm = value.split(":")
        return 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59
    except (ValueError, AttributeError):
        return False


def init_config(target: str | Path | None = None, force: bool = False) -> Path:
    target = Path(target).expanduser() if target else DEFAULT_CONFIG_PATH
    if target.exists() and not force:
        raise ConfigError(f"{target} already exists (use --force to overwrite)")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(BUNDLED_CONFIG, target)
    return target
