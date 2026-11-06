import time

import pytest

from aquarium.config import ConfigError, load, validate
from aquarium.model import Reading
from aquarium.storage import Storage


def test_readings_roundtrip():
    store = Storage(":memory:")
    now = time.time()
    store.add_readings([
        Reading("temp_c", 25.1, "°C", now - 20),
        Reading("temp_c", 25.3, "°C", now - 10),
        Reading("ph", 8.1, "", now - 10),
    ])
    assert store.latest(["temp_c", "ph"]) == {"temp_c": 25.3, "ph": 8.1}
    series = store.series(["temp_c"], now - 60, points=10)
    assert len(series["temp_c"]["v"]) >= 1
    assert store.stats()["readings"] == 3
    store.close()


def test_series_downsampling_buckets():
    store = Storage(":memory:")
    now = time.time()
    readings = [Reading("temp_c", 25.0 + i / 100, "°C", now - 3600 + i * 30)
                for i in range(120)]
    store.add_readings(readings)
    series = store.series(["temp_c"], now - 3600, points=30)
    assert 10 <= len(series["temp_c"]["v"]) <= 40
    store.close()


def test_events_and_doses():
    store = Storage(":memory:")
    now = time.time()
    store.add_event(now, "heater", True, "below setpoint", "auto")
    store.add_dose(now, "dose_alk", 12.0, "scheduled")
    assert store.events()[0]["actuator"] == "heater"
    assert store.doses()[0]["ml"] == 12.0
    assert store.doses_since(now - 60) == {"dose_alk": 12.0}
    store.close()


def test_prune_removes_old_rows():
    store = Storage(":memory:")
    now = time.time()
    store.add_readings([Reading("temp_c", 25.0, "°C", now - 40 * 86400),
                        Reading("temp_c", 25.1, "°C", now)])
    removed = store.prune(days=30)
    assert removed == 1
    assert store.stats()["readings"] == 1
    store.close()


def test_default_config_is_valid():
    cfg = load("config/default.toml")
    assert cfg.tank.water_type == "reef"
    assert cfg.mode == "simulation"
    assert any(s.metric == "ph" for s in cfg.sensors)
    assert len(cfg.alerts) >= 4


def test_config_rejects_unknown_actuator(tmp_path):
    text = """
[tank]
name = "bad"
volume_l = 100

[[sensors]]
metric = "temp_c"
driver = "sim"

[[actuators]]
id = "heater"
kind = "heater"
driver = "sim"

[control.temp]
enabled = true
setpoint_c = 25.0
heater = "heater_typo"
"""
    path = tmp_path / "bad.toml"
    path.write_text(text)
    with pytest.raises(ConfigError, match="heater_typo"):
        load(path)


def test_config_rejects_duplicate_metric(tmp_path):
    text = """
[[sensors]]
metric = "ph"
driver = "sim"

[[sensors]]
metric = "ph"
driver = "sim"

[[actuators]]
id = "p"
kind = "pump"
driver = "sim"
"""
    path = tmp_path / "dup.toml"
    path.write_text(text)
    with pytest.raises(ConfigError, match="duplicate"):
        load(path)


def test_config_rejects_bad_schedule(tmp_path):
    cfg = load("config/default.toml")
    cfg.control["dose"][0]["schedule"] = ["25:00"]
    with pytest.raises(ConfigError, match="schedule"):
        validate(cfg)


def test_config_rejects_alert_without_limit(tmp_path):
    cfg = load("config/default.toml")
    cfg.alerts[0].above = None
    with pytest.raises(ConfigError):
        validate(cfg)
