"""Simulated probes driven by :class:`SimWorld`."""

from __future__ import annotations

import random
import time

from ..units import metric_info
from .base import Sensor, SensorError
from .sim import SimWorld


class SimSensor(Sensor):
    driver = "sim"

    def __init__(self, metric: str, params: dict, world: SimWorld | None = None) -> None:
        info = metric_info(metric)
        super().__init__(metric, info.unit)
        self.world = world or SimWorld()
        self.noise = float(params.get("noise", 0.0))
        self.drift_per_hour = float(params.get("drift_per_hour", 0.0))
        self.fault = params.get("fault")
        self.rng = random.Random(int(params.get("seed", 11)) + hash(metric) % 1000)
        self._t0 = time.time()
        self._stuck: float | None = None

    def read(self):
        if self.fault == "dropout":
            raise SensorError(f"{self.metric}: simulated dropout")
        raw = self.world.value(self.metric)
        if self.noise:
            raw += self.rng.gauss(0.0, self.noise)
        if self.drift_per_hour:
            raw += self.drift_per_hour * (time.time() - self._t0) / 3600.0
        if self.fault == "stuck":
            if self._stuck is None:
                self._stuck = raw
            return self._reading(self._stuck, quality=0.3)
        return self._reading(raw)
