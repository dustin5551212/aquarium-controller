"""Sensor driver interface.

Drivers return a :class:`Reading` or raise :class:`SensorError`.  The control
loop treats an exception (or a reading that stops arriving) as a fault, so a
driver must never invent a value it did not measure.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod

from ..model import Reading


class SensorError(Exception):
    """A probe could not be read."""


class Sensor(ABC):
    driver: str = "base"

    def __init__(self, metric: str, unit: str = "") -> None:
        self.metric = metric
        self.unit = unit

    @abstractmethod
    def read(self) -> Reading: ...

    def close(self) -> None:  # pragma: no cover - optional cleanup
        pass

    def _reading(self, value: float, quality: float = 1.0) -> Reading:
        return Reading(
            metric=self.metric,
            value=float(value),
            unit=self.unit,
            ts=time.time(),
            quality=quality,
            source=self.driver,
        )
