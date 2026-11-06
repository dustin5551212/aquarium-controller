"""DS18B20 1-Wire temperature probe (Raspberry Pi / any w1-gpio host).

The kernel exposes one directory per device under /sys/bus/w1/devices; the
probe needs ``dtoverlay=w1-gpio`` (or the w1-gpio module loaded) to appear.
"""

from __future__ import annotations

from pathlib import Path

from ..units import metric_info
from .base import Sensor, SensorError


class DS18B20Sensor(Sensor):
    driver = "ds18b20"

    def __init__(self, metric: str, params: dict) -> None:
        super().__init__(metric, metric_info(metric).unit)
        if metric != "temp_c":
            raise SensorError("DS18B20 only measures temp_c")
        explicit = params.get("path")
        if explicit:
            self.path = Path(explicit)
        else:
            base = Path(params.get("base_dir", "/sys/bus/w1/devices"))
            sensor_id = params.get("sensor_id")
            if sensor_id:
                self.path = base / str(sensor_id) / "w1_slave"
            else:
                found = sorted(base.glob("28-*/w1_slave"))
                if not found:
                    raise SensorError(
                        f"no DS18B20 found under {base} (is w1-gpio enabled?)"
                    )
                self.path = found[0]

    def read(self):
        try:
            lines = self.path.read_text().splitlines()
        except OSError as exc:
            raise SensorError(f"cannot read {self.path}: {exc}") from exc
        if not lines or not lines[0].strip().endswith("YES"):
            raise SensorError(f"DS18B20 CRC failure at {self.path}")
        marker = lines[-1].find("t=")
        if marker < 0:
            raise SensorError(f"unexpected DS18B20 payload: {lines[-1]!r}")
        try:
            celsius = int(lines[-1][marker + 2:]) / 1000.0
        except ValueError as exc:
            raise SensorError(f"bad DS18B20 value: {lines[-1]!r}") from exc
        if not -55.0 <= celsius <= 125.0:
            raise SensorError(f"DS18B20 out of range: {celsius:.2f} °C")
        return self._reading(celsius)
