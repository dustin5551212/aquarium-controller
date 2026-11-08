import time

import pytest

from aquarium.actuators import build_actuators
from aquarium.control import Controller, Hardware
from aquarium.sensors.sim import SimWorld
from conftest import make_config, reading


class Rig:
    def __init__(self, cfg, temp: float = 25.0):
        self.cfg = cfg
        self.world = SimWorld(temp_c=temp)
        self.hw = Hardware(cfg, build_actuators(cfg, self.world))
        self.ctl = Controller(cfg, self.hw)

    def step(self, now, **values):
        readings = {"temp_c": reading("temp_c", values.get("temp_c", 25.0), now),
                    "level_pct": reading("level_pct", values.get("level_pct", 94.0), now)}
        if "ph" in values:
            readings["ph"] = reading("ph", values["ph"], now)
        if "temp_ts" in values:
            readings["temp_c"] = reading("temp_c", values["temp_c"], values["temp_ts"])
        cmds, faults = self.ctl.evaluate(readings, now)
        faults += self.hw.apply(cmds, now)
        return cmds, faults

    def on(self, actuator):
        return self.hw.status[actuator].on


def test_heater_switches_on_below_band():
    rig = Rig(make_config())
    rig.step(1000, temp_c=25.4)
    assert rig.on("heater") is False
    rig.step(1001, temp_c=24.9)
    assert rig.on("heater") is True
    rig.step(1045, temp_c=26.2)
    assert rig.on("heater") is False
    assert rig.on("chiller") is True


def test_heater_and_chiller_never_both_on():
    rig = Rig(make_config())
    for i, temp in enumerate([24.0, 25.0, 25.5, 26.5, 27.0, 25.5, 24.0]):
        rig.step(1000 + i * 400, temp_c=temp)
        assert not (rig.on("heater") and rig.on("chiller"))


def test_min_on_time_holds_heater():
    rig = Rig(make_config())
    rig.step(1000, temp_c=24.0)
    assert rig.on("heater")
    rig.step(1005, temp_c=26.0)          # wants off, but min_on_s=30
    assert rig.on("heater")
    rig.step(1040, temp_c=26.0)
    assert not rig.on("heater")


def test_min_off_time_blocks_restart():
    rig = Rig(make_config())
    rig.step(1000, temp_c=24.0)
    rig.step(1040, temp_c=26.0)
    rig.step(1400, temp_c=24.0)          # long enough after the last off
    assert rig.on("heater")
    rig.step(1440, temp_c=26.0)          # min_on satisfied → off
    assert not rig.on("heater")
    rig.step(1480, temp_c=24.0)          # only 40 s off, min_off=120
    assert not rig.on("heater")
    rig.step(1570, temp_c=24.0)
    assert rig.on("heater")


def test_stale_reading_shuts_loop_down():
    rig = Rig(make_config())
    rig.step(1000, temp_c=24.0)
    assert rig.on("heater")
    _, faults = rig.step(1200, temp_c=24.0, temp_ts=1000)   # 200 s old
    assert not rig.on("heater")
    assert any(f.key == "stale:temp_c" for f in faults)


def test_implausible_reading_shuts_loop_down():
    rig = Rig(make_config())
    _, faults = rig.step(1000, temp_c=99.0)
    assert not rig.on("heater")
    assert any(f.key == "range:temp_c" for f in faults)


def test_max_on_time_cuts_relay():
    cfg = make_config()
    cfg.actuators[0].max_on_s = 120        # heater
    rig = Rig(cfg)
    rig.step(1000, temp_c=24.0)
    assert rig.on("heater")
    _, faults = rig.step(1130, temp_c=24.0)
    assert not rig.on("heater")
    assert any(f.key == "max_on:heater" for f in faults)


def test_ato_fills_and_locks_out():
    rig = Rig(make_config())
    rig.step(1000, level_pct=91.0)
    assert rig.on("ato_pump")
    rig.step(1010, level_pct=96.5)
    assert not rig.on("ato_pump")
    rig.step(1030, level_pct=91.0)         # inside cooldown
    assert not rig.on("ato_pump")
    rig.step(1320, level_pct=91.0)
    assert rig.on("ato_pump")


def test_ato_runaway_is_cut_and_locked_out():
    rig = Rig(make_config())
    rig.step(1000, level_pct=90.0)
    assert rig.on("ato_pump")
    _, faults = rig.step(1130, level_pct=90.0)
    assert not rig.on("ato_pump")
    assert any(f.key == "ato:max_run" for f in faults)
    rig.step(1400, level_pct=90.0)         # still locked out
    assert not rig.on("ato_pump")


def test_dose_fires_once_per_slot_and_records_ml():
    now = time.time()
    slot = time.strftime("%H:%M", time.localtime(now - 30))
    cfg = make_config(dose=[{"pump": "dose_alk", "schedule": [slot], "ml": 12.0,
                             "daily_max_ml": 30.0}])
    rig = Rig(cfg)
    rig.step(now, temp_c=25.0)
    assert "dose_alk" in rig.hw.active_pulses()

    rig.step(now + 1, temp_c=25.0)         # must not queue a second dose
    pulses = rig.hw.active_pulses()
    assert len(pulses) == 1

    rig.step(now + 8, temp_c=25.0)         # 12 ml at 2 ml/s = 6 s
    doses = rig.hw.drain_doses()
    assert len(doses) == 1
    assert doses[0]["pump"] == "dose_alk"
    assert doses[0]["ml"] == pytest.approx(12.0, abs=0.01)


def test_manual_dose_counts_against_daily_cap():
    now = time.time()
    slot = time.strftime("%H:%M", time.localtime(now - 30))
    cfg = make_config(dose=[{"pump": "dose_alk", "schedule": [slot], "ml": 12.0,
                             "daily_max_ml": 13.0}])
    rig = Rig(cfg)
    rig.ctl.bind_dose_totals(lambda: {"dose_alk": 12.0})   # already dosed by hand
    _, faults = rig.step(now, temp_c=25.0)
    assert "dose_alk" not in rig.hw.active_pulses()
    assert any(f.key == "dose_cap:dose_alk" for f in faults)


def test_dose_respects_daily_cap():
    now = time.time()
    slot = time.strftime("%H:%M", time.localtime(now - 30))
    cfg = make_config(dose=[{"pump": "dose_alk", "schedule": [slot], "ml": 12.0,
                             "daily_max_ml": 5.0}])
    rig = Rig(cfg)
    _, faults = rig.step(now, temp_c=25.0)
    assert "dose_alk" not in rig.hw.active_pulses()
    assert any(f.key == "dose_cap:dose_alk" for f in faults)


def test_reason_tracks_decision_without_a_state_change():
    rig = Rig(make_config())
    rig.step(1000, temp_c=24.0)
    assert rig.on("heater")
    rig.step(1002, temp_c=26.0)          # wants off, min_on_s=30 holds it on
    assert rig.on("heater")
    assert "min on-time" in rig.hw.status["heater"].reason


def test_manual_override_expires():
    rig = Rig(make_config())
    rig.hw.override("heater", False, 60, 1000)
    rig.step(1001, temp_c=24.0)
    assert not rig.on("heater")
    rig.step(1065, temp_c=24.0)
    assert rig.on("heater")


def test_manual_dose_bypasses_min_off():
    rig = Rig(make_config())
    rig.hw.dose("dose_alk", 4.0, 1000)
    rig.step(1001, temp_c=25.0)
    assert rig.on("dose_alk")
    rig.step(1003, temp_c=25.0)
    assert not rig.on("dose_alk")
    doses = rig.hw.drain_doses()
    assert doses[0]["ml"] == pytest.approx(4.0, abs=0.01)


def test_co2_follows_ph_target():
    cfg = make_config(co2_loop=True)
    rig = Rig(cfg)
    rig.step(1000, temp_c=25.0, ph=7.4)
    assert rig.on("co2")
    rig.step(1030, temp_c=25.0, ph=6.65)
    assert not rig.on("co2")
