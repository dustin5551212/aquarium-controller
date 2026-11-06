"""Atlas Scientific EZO probes (pH, EC, ORP, DO) over serial or I²C.

EZO circuits answer asynchronously: a read command returns ``*OK`` and then
the value on the next line, which is why the serial reader scans lines until
it finds something numeric instead of assuming the first response is data.
"""

from __future__ import annotations

import time

from ..units import metric_info
from .base import Sensor, SensorError


class EzoSensor(Sensor):
    driver = "ezo"

    def __init__(self, metric: str, params: dict) -> None:
        super().__init__(metric, metric_info(metric).unit)
        self.read_command = str(params.get("command", "R"))
        self.timeout = float(params.get("timeout", 2.0))
        self.address = int(params.get("address", 99))
        self._ser = params.get("serial")
        self._bus = params.get("bus")
        self.port = params.get("port")
        self.baud = int(params.get("baud", 9600))
        if self._ser is None and self._bus is None:
            if self.port:
                try:
                    import serial  # type: ignore
                except ImportError as exc:  # pragma: no cover - depends on host
                    raise SensorError(
                        "pyserial is required for EZO serial probes: "
                        "pip install pyserial"
                    ) from exc
                self._ser = serial.Serial(self.port, self.baud, timeout=self.timeout)
            elif params.get("i2c_bus") is not None:
                try:
                    import smbus2  # type: ignore
                except ImportError as exc:  # pragma: no cover - depends on host
                    raise SensorError(
                        "smbus2 is required for EZO I2C probes: pip install smbus2"
                    ) from exc
                self._bus = smbus2.SMBus(int(params["i2c_bus"]))
            else:
                raise SensorError(
                    "EZO probe needs params.port (serial) or params.i2c_bus"
                )

    # -- transport -------------------------------------------------------
    def _command_serial(self, command: str) -> list[str]:
        self._ser.reset_input_buffer()
        self._ser.write(command.encode() + b"\r")
        self._ser.flush()
        deadline = time.monotonic() + self.timeout
        lines: list[str] = []
        while time.monotonic() < deadline:
            raw = self._ser.readline()
            if not raw:
                break
            text = raw.decode(errors="replace").strip()
            if text:
                lines.append(text)
                if not text.startswith("*"):
                    break
        if not lines:
            raise SensorError(f"EZO {self.metric}: no response")
        return lines

    def _command_i2c(self, command: str) -> list[str]:
        payload = command.encode()
        self._bus.write_i2c_block_data(self.address, 0, list(payload))
        time.sleep(0.9)                      # EZO needs ~600-900 ms
        raw = bytes(self._bus.read_i2c_block_data(self.address, 0, 32))
        code = raw[0] if raw else 0
        text = raw[1:].split(b"\x00")[0].decode(errors="replace").strip()
        if code == 254:
            raise SensorError("EZO: command pending")
        if code == 2:
            raise SensorError("EZO: syntax error")
        if code == 255:
            raise SensorError("EZO: no data to send")
        return [t for t in text.splitlines() if t]

    def command(self, command: str) -> list[str]:
        if self._ser is not None:
            return self._command_serial(command)
        return self._command_i2c(command)

    # -- sensor ----------------------------------------------------------
    def read(self):
        for line in reversed(self.command(self.read_command)):
            try:
                value = float(line)
            except ValueError:
                continue
            if not -1000.0 <= value <= 1_000_000.0:
                raise SensorError(f"EZO {self.metric}: implausible value {value}")
            return self._reading(value)
        raise SensorError(f"EZO {self.metric}: no numeric reply")

    def close(self) -> None:
        if self._ser is not None and hasattr(self._ser, "close"):
            self._ser.close()
