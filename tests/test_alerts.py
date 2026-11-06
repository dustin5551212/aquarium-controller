import time

from aquarium.alerts import AlertEngine, Notifier
from aquarium.config import AlertCfg, NotifyCfg
from aquarium.model import Fault
from conftest import make_config, reading


def engine_with(alerts, **kwargs):
    cfg = make_config(alerts=alerts, **kwargs)
    return AlertEngine(cfg, None, Notifier(NotifyCfg())), cfg


def test_threshold_needs_hold_time_then_clears():
    engine, _ = engine_with([AlertCfg("temp_c", above=26.0, hold_s=10,
                                      severity="critical", message="Tank too hot")])
    hot = {"temp_c": reading("temp_c", 26.5, 1000)}
    assert engine.evaluate(hot, [], 1000) == []          # just breached
    assert engine.evaluate(hot, [], 1005) == []          # still inside the hold
    transitions = engine.evaluate(hot, [], 1012)
    assert [t[1] for t in transitions] == ["raised"]
    assert engine.snapshot(1012)[0]["message"] == "Tank too hot"
    assert engine.snapshot(1012)[0]["severity"] == "critical"

    cool = {"temp_c": reading("temp_c", 25.0, 1020)}
    transitions = engine.evaluate(cool, [], 1020)
    assert any(state == "cleared" for _alert, state in transitions)
    assert engine.snapshot(1020) == []


def test_hysteresis_rearms_after_clear():
    engine, _ = engine_with([AlertCfg("ph", below=7.8, hold_s=0, severity="warning")])
    low = {"ph": reading("ph", 7.7, 1000)}
    assert [s for _a, s in engine.evaluate(low, [], 1000)] == ["raised"]
    assert engine.evaluate({"ph": reading("ph", 7.9, 1005)}, [], 1005)
    assert [s for _a, s in engine.evaluate(low, [], 1010)] == ["raised"]


def test_critical_fault_raises_immediately_and_clears():
    engine, _ = engine_with([])
    fault = Fault("stale:temp_c", "temp_c is stale", "critical", metric="temp_c")
    transitions = engine.evaluate({}, [fault], time.time())
    assert [s for _a, s in transitions] == ["raised"]
    assert engine.snapshot(time.time())[0]["rule"] == "stale:temp_c"

    transitions = engine.evaluate({}, [], time.time())
    assert [s for _a, s in transitions] == ["cleared"]


def test_warning_fault_is_debounced():
    engine, _ = engine_with([])
    fault = Fault("ato:max_run", "top-off timeout", "warning", actuator="ato_pump")
    assert engine.evaluate({}, [fault], 1000) == []
    assert engine.evaluate({}, [fault], 1010) == []      # hold is 30 s by default
    assert [s for _a, s in engine.evaluate({}, [fault], 1031)] == ["raised"]


def test_acknowledge_clears_equipment_alert():
    engine, _ = engine_with([])
    engine.evaluate({}, [Fault("x", "broken", "critical")], 1000)
    assert engine.clear() == 1
    assert engine.snapshot(1000) == []
