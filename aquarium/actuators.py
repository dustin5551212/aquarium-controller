"""Actuator drivers: simulated relays, HTTP relays and Raspberry Pi GPIO."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod

from .config import ActuatorCfg
from .sensors.sim import SimWorld


class ActuatorError(Exception):
    """A relay could not be switched."""


class Actuator(ABC):
    driver: str = "base"

    def __init__(self, cfg: ActuatorCfg) -> None:
        self.id = cfg.id
        self.kind = cfg.kind
        self.cfg = cfg

    @abstractmethod
    def set(self, on: bool) -> None: ...

    def close(self) -> None:  # pragma: no cover - optional cleanup
        pass


class SimActuator(Actuator):
    """Writes into the simulated tank so sensors react to relay changes."""

    driver = "sim"

    def __init__(self, cfg: ActuatorCfg, world: SimWorld | None = None) -> None:
        super().__init__(cfg)
        self.world = world or SimWorld()
        self.world.actuators.setdefault(cfg.id, False)

    def set(self, on: bool) -> None:
        self.world.actuators[self.id] = bool(on)

    @property
    def state(self) -> bool:
        return bool(self.world.actuators.get(self.id))


class HttpActuator(Actuator):
    """A network relay (Shelly, Tasmota, ESPHome REST, ...).

    ``params.on_url`` / ``params.off_url`` are used when given, otherwise a
    single ``params.url`` receives ``{"id": ..., "state": true|false}``.
    """

    driver = "http"

    def __init__(self, cfg: ActuatorCfg) -> None:
        super().__init__(cfg)
        self.url = str(cfg.params.get("url", ""))
        self.on_url = str(cfg.params.get("on_url", "")) or self.url
        self.off_url = str(cfg.params.get("off_url", "")) or self.url
        self.method = str(cfg.params.get("method", "POST")).upper()
        self.timeout = float(cfg.params.get("timeout", 3.0))
        self.headers = dict(cfg.params.get("headers") or {})
        if not self.url:
            raise ActuatorError(f"{cfg.id}: http driver needs params.url")
        self._state = False

    def _call(self, url: str, on: bool) -> None:
        body = None
        headers = {"Content-Type": "application/json", **self.headers}
        if self.method == "POST":
            body = json.dumps({"id": self.id, "state": on}).encode()
        req = urllib.request.Request(url, data=body, headers=headers, method=self.method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if resp.status >= 400:
                    raise ActuatorError(f"{self.id}: HTTP {resp.status} from {url}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ActuatorError(f"{self.id}: {url}: {exc}") from exc

    def set(self, on: bool) -> None:
        self._call(self.on_url if on else self.off_url, on)
        self._state = bool(on)

    @property
    def state(self) -> bool:
        return self._state


class GpioActuator(Actuator):
    """A relay on a Linux GPIO line, via gpiod (v2 or v1 bindings)."""

    driver = "gpio"

    def __init__(self, cfg: ActuatorCfg) -> None:
        super().__init__(cfg)
        self.pin = int(cfg.params.get("pin", -1))
        if self.pin < 0:
            raise ActuatorError(f"{cfg.id}: gpio driver needs params.pin")
        self.chip = str(cfg.params.get("chip", "gpiochip0"))
        self.active_low = bool(cfg.params.get("active_low", False))
        self._state = False
        try:
            import gpiod  # type: ignore
        except ImportError as exc:  # pragma: no cover - depends on host
            raise ActuatorError(
                f"{cfg.id}: gpiod is required for GPIO relays "
                f"(apt install python3-gpiod)"
            ) from exc

        self._gpiod = gpiod
        if hasattr(gpiod, "request_lines"):                    # libgpiod >= 2
            self._req = gpiod.request_lines(
                f"/dev/{self.chip}",
                consumer="aquarium-controller",
                config={
                    self.pin: gpiod.LineSettings(
                        direction=gpiod.line.Direction.OUTPUT,
                        output_value=gpiod.line.Value.INACTIVE,
                        active_low=self.active_low,
                    )
                },
            )
            self._line = None
        else:                                                  # libgpiod 1.x
            self._req = None
            chip = gpiod.Chip(self.chip)
            self._line = chip.get_line(self.pin)
            self._line.request(consumer="aquarium-controller", type=gpiod.LINE_REQ_DIR_OUT)

    def set(self, on: bool) -> None:
        try:
            if self._req is not None:
                value = (
                    self._gpiod.line.Value.ACTIVE if on
                    else self._gpiod.line.Value.INACTIVE
                )
                self._req.set_value(self.pin, value)
            else:
                self._line.set_value(1 if on else 0)
        except OSError as exc:
            raise ActuatorError(f"{self.id}: GPIO write failed: {exc}") from exc
        self._state = bool(on)

    @property
    def state(self) -> bool:
        return self._state

    def close(self) -> None:
        try:
            if self._req is not None:
                self.set(False)
                self._req.release()
            elif self._line is not None:
                self._line.set_value(0)
                self._line.release()
        except Exception:  # pragma: no cover - best effort on shutdown
            pass


def build_actuator(cfg: ActuatorCfg, world: SimWorld | None = None) -> Actuator:
    if cfg.driver == "sim":
        return SimActuator(cfg, world)
    if cfg.driver == "http":
        return HttpActuator(cfg)
    if cfg.driver == "gpio":
        return GpioActuator(cfg)
    raise ActuatorError(f"unknown actuator driver {cfg.driver!r}")


def build_actuators(cfg, world: SimWorld | None = None) -> dict[str, Actuator]:
    return {a.id: build_actuator(a, world) for a in cfg.actuators}


def dose_rate_ml_s(cfg: ActuatorCfg) -> float:
    return float(cfg.params.get("rate_ml_s", 1.0)) or 1.0
