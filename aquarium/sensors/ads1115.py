"""ADS1115 16-bit ADC — the cheap analog front end for hobby probes.

Two wiring styles are covered:

* ``ph4502c``  — the common blue pH interface board (BNC + two pots). Its
  output is ~2.5 V at pH 7 and falls ~0.18 V per pH unit, so the default
  conversion is pH = 7 + (V - 2.5) * -5.7.  Calibrate the two pots, then
  adjust ``neutral_v`` / ``slope_ph_per_v`` after a two-point buffer test.
* ``tds_dfrobot`` — DFRobot-style TDS board, cubic curve from the datasheet.
* ``linear`` — value = offset + scale * volts, for anything else.

Probes are ratiometric against the ADC reference, so feed the ADC and the
probe board from the same 5 V rail, and keep the probe's analog ground
separate from pump/relay ground.
"""

from __future__ import annotations

import time

from ..units import metric_info
from .base import Sensor, SensorError

# PGA setting -> full-scale volts
_GAIN_FS = {2 / 3: 6.144, 1: 4.096, 2: 2.048, 4: 1.024, 8: 0.512, 16: 0.256}
_MUX_SINGLE = {0: 0b100, 1: 0b101, 2: 0b110, 3: 0b111}
_MUX_DIFF = {"0-1": 0b000, "0-3": 0b001, "1-3": 0b010, "2-3": 0b011}
_REG_CONVERT = 0x00
_REG_CONFIG = 0x01


class ADS1115Sensor(Sensor):
    driver = "ads1115"

    def __init__(self, metric: str, params: dict) -> None:
        super().__init__(metric, metric_info(metric).unit)
        self.metric = metric
        self.address = int(params.get("address", 0x48))
        self.gain = float(params.get("gain", 1))
        if self.gain not in _GAIN_FS:
            raise SensorError(f"ADS1115: unsupported gain {self.gain}")
        channel = params.get("channel", 0)
        if isinstance(channel, str) and "-" in channel:
            if channel not in _MUX_DIFF:
                raise SensorError(f"ADS1115: unsupported differential {channel!r}")
            self.mux = _MUX_DIFF[channel]
        else:
            self.mux = _MUX_SINGLE[int(channel) % 4]
        self.data_rate = int(params.get("data_rate", 128))
        self.mode = str(params.get("mode", "linear"))
        self.scale = float(params.get("scale", 1.0))
        self.offset = float(params.get("offset", 0.0))
        self.neutral_v = float(params.get("neutral_v", 2.5))
        self.slope_ph_per_v = float(params.get("slope_ph_per_v", -5.7))
        self.temp_comp = params.get("temp_c")
        self._bus = params.get("bus")
        if self._bus is None:
            try:
                import smbus2  # type: ignore
            except ImportError as exc:  # pragma: no cover - depends on host
                raise SensorError(
                    "smbus2 is required for ADS1115: pip install smbus2"
                ) from exc
            self._bus = smbus2.SMBus(int(params.get("i2c_bus", 1)))

    # -- raw -------------------------------------------------------------
    def _write_config(self) -> None:
        config = (
            (0b1 << 15)                      # start a single conversion
            | (self.mux << 12)
            | (self._pga_bits() << 9)
            | (0b1 << 8)                     # single-shot mode
            | (self._dr_bits() << 5)
            | 0b11                           # comparator disabled
        )
        self._bus.write_i2c_block_data(
            self.address, _REG_CONFIG, [(config >> 8) & 0xFF, config & 0xFF]
        )

    def _pga_bits(self) -> int:
        return {2 / 3: 0b000, 1: 0b001, 2: 0b010, 4: 0b011,
                8: 0b100, 16: 0b101}[self.gain]

    def _dr_bits(self) -> int:
        return {8: 0, 16: 1, 32: 2, 64: 3, 128: 4, 250: 5, 475: 6, 860: 7}.get(
            self.data_rate, 4
        )

    def voltage(self) -> float:
        self._write_config()
        time.sleep(max(0.004, 1.2 / self.data_rate))
        raw = self._bus.read_i2c_block_data(self.address, _REG_CONVERT, 2)
        if len(raw) != 2:
            raise SensorError("ADS1115: short conversion read")
        counts = (raw[0] << 8) | raw[1]
        if counts >= 0x8000:
            counts -= 0x10000
        return counts * _GAIN_FS[self.gain] / 32768.0

    # -- conversion ------------------------------------------------------
    def to_value(self, volts: float) -> float:
        if self.mode == "ph4502c":
            return 7.0 + (volts - self.neutral_v) * self.slope_ph_per_v
        if self.mode == "tds_dfrobot":
            v = max(0.0, volts)
            ppm = (133.42 * v**3 - 255.86 * v**2 + 857.39 * v) * 0.5
            if self.temp_comp is not None:
                ppm *= 1.0 + 0.02 * (float(self.temp_comp) - 25.0)
            return ppm
        return self.offset + self.scale * volts

    def read(self):
        volts = self.voltage()
        value = self.to_value(volts)
        if self.metric == "ph" and not 0.0 <= value <= 14.0:
            raise SensorError(f"pH conversion out of range ({value:.2f} from {volts:.3f} V)")
        if self.metric == "ec_us" and value < 0:
            raise SensorError(f"negative conductivity ({value:.1f})")
        return self._reading(value)

    def close(self) -> None:
        close = getattr(self._bus, "close", None)
        if callable(close):
            close()
