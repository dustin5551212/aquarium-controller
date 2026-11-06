import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from aquarium.actuators import HttpActuator, build_actuators
from aquarium.config import ActuatorCfg
from aquarium.sensors.ads1115 import ADS1115Sensor
from aquarium.sensors.ds18b20 import DS18B20Sensor
from aquarium.sensors.ezo import EzoSensor
from aquarium.sensors.sim import SimWorld
from aquarium.sensors.sim_probe import SimSensor


# ------------------------------------------------------------------ simulated

def test_sim_world_heats_under_heater():
    world = SimWorld(temp_c=24.0)
    world.actuators["heater"] = True
    start = world.temp_c
    for _ in range(400):                      # ~6.5 minutes of heating
        world.tick(dt=1.0)
    assert world.temp_c > start + 0.5


def test_sim_world_evaporates_and_refills():
    world = SimWorld(level_pct=94.0)
    before = world.level_pct
    for _ in range(60):
        world.tick(dt=1.0)
    assert world.level_pct < before
    world.actuators["ato_pump"] = True
    for _ in range(10):
        world.tick(dt=1.0)
    assert world.level_pct > 94.0


def test_sim_sensor_noise_and_faults():
    world = SimWorld(temp_c=25.0)
    sensor = SimSensor("temp_c", {"noise": 0.01}, world)
    value = sensor.read().value
    assert abs(value - 25.0) < 0.2

    stuck = SimSensor("temp_c", {"fault": "stuck"}, world)
    first = stuck.read().value
    world.temp_c = 30.0
    assert stuck.read().value == first

    with pytest.raises(Exception):
        SimSensor("temp_c", {"fault": "dropout"}, world).read()


# ------------------------------------------------------------------- DS18B20

def test_ds18b20_parses_w1_slave(tmp_path):
    payload = "72 01 4b 46 7f ff 0e 10 57 : crc=57 YES\n72 01 4b 46 7f ff 0e 10 57 t=25250\n"
    path = tmp_path / "w1_slave"
    path.write_text(payload)
    sensor = DS18B20Sensor("temp_c", {"path": str(path)})
    assert sensor.read().value == pytest.approx(25.25)


def test_ds18b20_rejects_crc_failure(tmp_path):
    path = tmp_path / "w1_slave"
    path.write_text("00 01 02 : crc=00 NO\n00 01 02 t=25250\n")
    sensor = DS18B20Sensor("temp_c", {"path": str(path)})
    with pytest.raises(Exception):
        sensor.read()


# ------------------------------------------------------------------------ EZO

class FakeSerial:
    def __init__(self, chunk: bytes):
        self.chunk = chunk
        self.buffer = b""
        self.written = b""

    def reset_input_buffer(self):
        self.buffer = b""

    def write(self, data):
        self.written += data
        self.buffer = self.chunk

    def flush(self):
        pass

    def readline(self):
        if b"\r\n" not in self.buffer:
            return b""
        line, self.buffer = self.buffer.split(b"\r\n", 1)
        return line + b"\r\n"

    def close(self):
        pass


def test_ezo_reads_value_after_ok():
    fake = FakeSerial(b"*OK\r\n8.142\r\n")
    sensor = EzoSensor("ph", {"serial": fake})
    assert sensor.read().value == pytest.approx(8.142)
    assert fake.written == b"R\r"


def test_ezo_raises_without_numeric_reply():
    fake = FakeSerial(b"*ER,-2\r\n")
    sensor = EzoSensor("ph", {"serial": fake})
    with pytest.raises(Exception):
        sensor.read()


# -------------------------------------------------------------------- ADS1115

class FakeBus:
    def __init__(self, counts: int):
        self.counts = counts
        self.writes = []

    def write_i2c_block_data(self, address, register, data):
        self.writes.append((address, register, list(data)))

    def read_i2c_block_data(self, address, register, length):
        raw = self.counts & 0xFFFF
        return [(raw >> 8) & 0xFF, raw & 0xFF]

    def close(self):
        pass


def test_ads1115_voltage_and_linear_conversion():
    bus = FakeBus(0x2000)                      # quarter scale
    sensor = ADS1115Sensor("ph", {"bus": bus, "gain": 1, "mode": "linear",
                                  "scale": 1.0, "offset": 0.0})
    assert sensor.voltage() == pytest.approx(1.024, abs=1e-4)
    assert sensor.to_value(2.0) == pytest.approx(2.0)


def test_ads1115_ph4502c_conversion():
    sensor = ADS1115Sensor("ph", {"bus": FakeBus(0), "mode": "ph4502c"})
    assert sensor.to_value(2.5) == pytest.approx(7.0)
    assert sensor.to_value(3.025) == pytest.approx(4.0, abs=0.02)


def test_ads1115_tds_conversion():
    sensor = ADS1115Sensor("ec_us", {"bus": FakeBus(0), "mode": "tds_dfrobot"})
    assert sensor.to_value(0.0) == pytest.approx(0.0)
    assert 800 < sensor.to_value(2.3) < 1500


# ---------------------------------------------------------------------- HTTP

class _RelayHandler(BaseHTTPRequestHandler):
    states: list = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        _RelayHandler.states.append(body.decode())
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def test_http_actuator_posts_state():
    _RelayHandler.states = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RelayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        cfg = ActuatorCfg("heater", "heater", driver="http",
                          params={"url": f"http://127.0.0.1:{port}/relay"})
        actuator = HttpActuator(cfg)
        actuator.set(True)
        actuator.set(False)
        assert len(_RelayHandler.states) == 2
        assert '"state": true' in _RelayHandler.states[0]
        assert '"state": false' in _RelayHandler.states[1]
    finally:
        server.shutdown()
        server.server_close()


def test_build_actuators_uses_world():
    from conftest import make_config

    cfg = make_config()
    world = SimWorld()
    actuators = build_actuators(cfg, world)
    actuators["heater"].set(True)
    assert world.actuators["heater"] is True
