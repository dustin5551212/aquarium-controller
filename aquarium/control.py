"""Relay handling and the control loops.

Two layers, deliberately separate:

* :class:`Hardware` owns the relays.  It enforces minimum on/off times (so a
  compressor or heater is not short-cycled), maximum run times, manual
  overrides and timed pulses for dosing pumps.
* :class:`Controller` decides what each loop *wants*.  It reads only trusted
  readings — a missing, stale or implausible probe turns its loop off rather
  than steering on bad data.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

from .actuators import Actuator, ActuatorError, dose_rate_ml_s
from .config import Config
from .model import ActuatorStatus, Command, Fault, Reading
from .units import is_plausible


@dataclass(slots=True)
class Pulse:
    until: float
    reason: str
    ml: float | None = None
    started: float = 0.0


class Hardware:
    def __init__(self, cfg: Config, actuators: dict[str, Actuator]) -> None:
        self.cfg = cfg
        self.actuators = actuators
        now = time.time()
        self.status: dict[str, ActuatorStatus] = {
            a.id: ActuatorStatus(id=a.id, kind=a.kind, on=False, since=0.0)
            for a in cfg.actuators
        }
        self._override: dict[str, tuple[bool, float]] = {}
        self._pulse: dict[str, Pulse] = {}
        self.events: deque[dict] = deque(maxlen=2000)
        self.dose_events: deque[dict] = deque(maxlen=500)
        self.faults: dict[str, Fault] = {}

    # -- manual commands -------------------------------------------------
    def override(self, actuator_id: str, on: bool, duration_s: float, now: float) -> None:
        if actuator_id not in self.status:
            raise KeyError(actuator_id)
        self._override[actuator_id] = (bool(on), now + max(1.0, duration_s))

    def clear_override(self, actuator_id: str, now: float) -> None:
        self._override.pop(actuator_id, None)
        self.status[actuator_id].override_until = 0.0
        self.status[actuator_id].mode = "auto"

    def pulse(self, actuator_id: str, seconds: float, now: float, reason: str,
              ml: float | None = None) -> None:
        if actuator_id not in self.status:
            raise KeyError(actuator_id)
        if self.status[actuator_id].on and actuator_id not in self._pulse:
            raise ActuatorError(f"{actuator_id} is already running")
        self._pulse[actuator_id] = Pulse(now + seconds, reason, ml, now)

    def dose(self, actuator_id: str, ml: float, now: float, reason: str = "manual dose") -> float:
        cfg = self.cfg.actuator(actuator_id)
        seconds = ml / dose_rate_ml_s(cfg)
        self.pulse(actuator_id, seconds, now, reason, ml)
        return seconds

    # -- reconciliation --------------------------------------------------
    def apply(self, desired: dict[str, Command], now: float,
              force_off: frozenset[str] = frozenset()) -> list[Fault]:
        faults: list[Fault] = []
        plan: dict[str, tuple[bool, str]] = {}

        # Pass 1 — what every relay *wants*, before safety shaping.
        for acfg in self.cfg.actuators:
            aid = acfg.id
            st = self.status[aid]
            target = bool(acfg.params.get("always_on", False))
            reason = "always on"

            cmd = desired.get(aid)
            if cmd is not None:
                target, reason = cmd.on, cmd.reason

            override = self._override.get(aid)
            if override is not None:
                value, until = override
                if now < until:
                    target = value
                    reason = f"manual override, {until - now:.0f}s left"
                    st.mode = "manual"
                    st.override_until = until
                else:
                    self._override.pop(aid, None)
                    st.mode = "auto"
                    st.override_until = 0.0

            pulse = self._pulse.get(aid)
            if pulse is not None:
                if now < pulse.until:
                    target, reason = True, pulse.reason
                else:
                    self._finish_pulse(aid, now, pulse)
                    target, reason = False, "dose complete"

            if aid in force_off:
                target, reason = False, "safety cut-out"
            plan[aid] = (target, reason)

        # Pass 2 — heating and cooling are mutually exclusive. Decided up
        # front so a relay that is still switching off cannot start the other.
        for acfg in self.cfg.actuators:
            if acfg.kind not in ("heater", "chiller") or not plan[acfg.id][0]:
                continue
            opposite = "chiller" if acfg.kind == "heater" else "heater"
            for other in self.cfg.actuators:
                if other.id != acfg.id and other.kind == opposite and plan[other.id][0]:
                    plan[acfg.id] = (False, f"interlock: {other.id} active")
                    break

        # Pass 3 — timing protection and the actual switch.
        for acfg in self.cfg.actuators:
            aid = acfg.id
            st = self.status[aid]
            act = self.actuators[aid]
            target, reason = plan[aid]
            pulse = self._pulse.get(aid)
            on_for = now - st.since

            if st.on and not target and on_for < acfg.min_on_s and aid not in force_off:
                target = True
                reason = f"min on-time hold ({acfg.min_on_s:.0f}s)"
            if (not st.on and target and st.mode == "auto" and pulse is None
                    and now - st.since < acfg.min_off_s):
                target = False
                reason = f"min off-time hold ({acfg.min_off_s:.0f}s)"
            if st.on and on_for >= acfg.max_on_s and pulse is None:
                target = False
                reason = "max on-time exceeded"
                faults.append(Fault(
                    key=f"max_on:{aid}",
                    message=f"{aid} ran for its {acfg.max_on_s:.0f}s limit and was switched off",
                    severity="warning",
                    actuator=aid,
                ))

            if target != st.on:
                try:
                    act.set(target)
                except ActuatorError as exc:
                    faults.append(Fault(
                        key=f"actuator:{aid}", message=str(exc),
                        severity="critical", actuator=aid,
                    ))
                    continue
                st.on = target
                st.since = now
                st.reason = reason
                self.events.append({
                    "ts": now, "actuator": aid, "on": target,
                    "reason": reason, "source": st.mode,
                })
            else:
                # Same state, but the reason may have moved on (override
                # expiring, interlock released, a fresh setpoint comment).
                st.reason = reason
        return faults

    def _finish_pulse(self, aid: str, now: float, pulse: Pulse) -> None:
        self._pulse.pop(aid, None)
        if pulse.ml:
            elapsed = max(0.0, min(now, pulse.until) - pulse.started)
            planned = max(1e-6, pulse.until - pulse.started)
            ml = pulse.ml * min(1.0, elapsed / planned)
            self.dose_events.append({
                "ts": now, "pump": aid, "ml": round(ml, 2), "reason": pulse.reason,
            })

    # -- reporting -------------------------------------------------------
    def drain_events(self) -> list[dict]:
        events = list(self.events)
        self.events.clear()
        return events

    def drain_doses(self) -> list[dict]:
        doses = list(self.dose_events)
        self.dose_events.clear()
        return doses

    def describe(self, now: float) -> list[dict]:
        described = []
        for acfg in self.cfg.actuators:
            payload = self.status[acfg.id].as_dict(now)
            payload["label"] = acfg.label or acfg.kind.replace("_", " ").title()
            described.append(payload)
        return described

    def active_pulses(self) -> dict[str, float]:
        return {k: v.until for k, v in self._pulse.items()}


class Controller:
    def __init__(self, cfg: Config, hardware: Hardware) -> None:
        self.cfg = cfg
        self.hw = hardware
        self._heat = False
        self._chill = False
        self._ato = False
        self._ato_started = 0.0
        self._ato_lock_until = 0.0
        self._co2 = False
        self._dosed: set[str] = set()
        self._dose_day = ""
        self._dose_today: dict[str, float] = {}
        self._dose_source = None
        self.loops: dict[str, dict] = {}

    # -- helpers ---------------------------------------------------------
    def _trusted(self, readings: dict[str, Reading], metric: str, now: float,
                 faults: list[Fault]) -> Reading | None:
        reading = readings.get(metric)
        if reading is None:
            faults.append(Fault(f"sensor:{metric}", f"no reading from {metric}", "critical", metric=metric))
            return None
        age = now - reading.ts
        if age > self.cfg.stale_after_s:
            faults.append(Fault(
                f"stale:{metric}",
                f"{metric} is stale ({age:.0f}s old)", "critical", metric=metric,
            ))
            return None
        if not is_plausible(metric, reading.value):
            faults.append(Fault(
                f"range:{metric}",
                f"{metric} reading {reading.value:.2f} is outside the plausible range",
                "critical", metric=metric,
            ))
            return None
        return reading

    def _off_dependents(self, cmds: dict[str, Command], *ids: str, reason: str) -> None:
        for aid in ids:
            if aid and aid in self.hw.status:
                cmds[aid] = Command(aid, False, reason)

    @staticmethod
    def _in_window(hour: float, start: float, end: float) -> bool:
        if start <= end:
            return start <= hour < end
        return hour >= start or hour < end

    @staticmethod
    def _hhmm(value: str) -> float:
        hh, mm = value.split(":")
        return int(hh) + int(mm) / 60.0

    # -- main tick -------------------------------------------------------
    def evaluate(self, readings: dict[str, Reading], now: float,
                 ) -> tuple[dict[str, Command], list[Fault]]:
        cmds: dict[str, Command] = {}
        faults: list[Fault] = []
        loops: dict[str, dict] = {}
        self._loop_temp(readings, now, cmds, faults, loops)
        self._loop_ato(readings, now, cmds, faults, loops)
        self._loop_light(now, cmds, loops)
        self._loop_co2(readings, now, cmds, faults, loops)
        self._loop_dose(now, cmds, faults, loops)
        self.loops = loops
        return cmds, faults

    # -- temperature -----------------------------------------------------
    def _loop_temp(self, readings, now, cmds, faults, loops) -> None:
        loop = self.cfg.loop("temp")
        if not loop.get("enabled", False):
            return
        heater = loop.get("heater")
        chiller = loop.get("chiller")
        reading = self._trusted(readings, "temp_c", now, faults)
        if reading is None:
            self._heat = self._chill = False
            self._off_dependents(cmds, heater, chiller, reason="temperature sensor fault")
            loops["temp"] = {"enabled": True, "state": "fault", "heater": heater,
                             "chiller": chiller}
            return

        setpoint = float(loop.get("setpoint_c", 25.5))
        band = max(0.05, float(loop.get("deadband_c", 0.35)))
        value = reading.value

        if value <= setpoint - band:
            self._heat = True
        elif value >= setpoint:
            self._heat = False
        if value >= setpoint + band:
            self._chill = True
        elif value <= setpoint:
            self._chill = False
        if self._heat and self._chill:          # never fight itself
            self._chill = False

        if heater:
            cmds[heater] = Command(heater, self._heat,
                                   f"{value:.2f}°C {'below' if self._heat else 'at/above'} setpoint {setpoint:.2f}°C")
        if chiller and chiller != heater:
            cmds[chiller] = Command(chiller, self._chill,
                                    f"{value:.2f}°C {'above' if self._chill else 'at/below'} setpoint {setpoint:.2f}°C")
        loops["temp"] = {
            "enabled": True,
            "state": "heating" if self._heat else ("cooling" if self._chill else "stable"),
            "target": setpoint,
            "reading": round(value, 2),
            "deadband": band,
        }

    # -- auto top-off ----------------------------------------------------
    def _loop_ato(self, readings, now, cmds, faults, loops) -> None:
        loop = self.cfg.loop("ato")
        if not loop.get("enabled", False):
            return
        pump = loop.get("pump")
        metric = str(loop.get("sensor", "level_pct"))
        low = float(loop.get("low_pct", 92.0))
        high = float(loop.get("high_pct", 96.0))
        max_run = float(loop.get("max_run_s", 120.0))
        cooldown = float(loop.get("cooldown_s", 300.0))

        reading = self._trusted(readings, metric, now, faults)
        if reading is None:
            self._ato = False
            self._off_dependents(cmds, pump, reason="level sensor fault")
            loops["ato"] = {"enabled": True, "state": "fault", "pump": pump}
            return

        value = reading.value
        note = ""
        if self._ato:
            if value >= high:
                self._ato = False
                self._ato_lock_until = now + cooldown
                note = "reached high mark"
            elif now - self._ato_started >= max_run:
                self._ato = False
                self._ato_lock_until = now + max(cooldown, 900.0)
                note = "max run exceeded"
                faults.append(Fault(
                    "ato:max_run",
                    f"top-off ran for {max_run:.0f}s without reaching the high mark — "
                    f"pump locked out (check for a siphon, blocked line or empty reservoir)",
                    "critical", actuator=pump, metric=metric,
                ))
        else:
            if value <= low and now >= self._ato_lock_until and pump:
                self._ato = True
                self._ato_started = now
                note = "below low mark"

        if pump:
            cmds[pump] = Command(pump, self._ato,
                                 f"level {value:.1f}% (low {low:g}%, high {high:g}%)"
                                 + (f" — {note}" if note else ""))
        loops["ato"] = {
            "enabled": True,
            "state": "filling" if self._ato else ("locked out" if now < self._ato_lock_until else "idle"),
            "reading": round(value, 1),
            "low": low,
            "high": high,
            "pump": pump,
            "lockout_s": round(max(0.0, self._ato_lock_until - now), 0),
        }

    # -- lighting --------------------------------------------------------
    def _loop_light(self, now, cmds, loops) -> None:
        loop = self.cfg.loop("light")
        if not loop.get("enabled", False):
            return
        actuator = loop.get("actuator")
        start = self._hhmm(str(loop.get("start", "10:00")))
        end = self._hhmm(str(loop.get("end", "20:00")))
        hour = time.localtime(now).tm_hour + time.localtime(now).tm_min / 60.0
        on = self._in_window(hour, start, end)
        if actuator:
            cmds[actuator] = Command(actuator, on,
                                     f"photoperiod {loop.get('start')}–{loop.get('end')}")
        loops["light"] = {"enabled": True, "state": "on" if on else "off",
                          "start": loop.get("start"), "end": loop.get("end")}

    # -- CO2 (planted tanks) --------------------------------------------
    def _loop_co2(self, readings, now, cmds, faults, loops) -> None:
        loop = self.cfg.loop("co2")
        if not loop.get("enabled", False):
            return
        solenoid = loop.get("solenoid")
        metric = str(loop.get("sensor", "ph"))
        target = float(loop.get("target", 6.7))
        hysteresis = float(loop.get("hysteresis", 0.05))
        reading = self._trusted(readings, metric, now, faults)
        if reading is None:
            self._co2 = False
            self._off_dependents(cmds, solenoid, reason="pH sensor fault")
            loops["co2"] = {"enabled": True, "state": "fault", "solenoid": solenoid}
            return

        hour = time.localtime(now).tm_hour + time.localtime(now).tm_min / 60.0
        lit = True
        if loop.get("lights_only", True):
            light_loop = self.cfg.loop("light")
            if light_loop.get("enabled", False):
                lit = self._in_window(hour, self._hhmm(str(light_loop.get("start", "10:00"))),
                                      self._hhmm(str(light_loop.get("end", "20:00"))))
            else:
                lit = self._in_window(hour, 10.0, 20.0)

        if not lit:
            self._co2 = False
        elif reading.value > target + hysteresis:
            self._co2 = True
        elif reading.value <= target:
            self._co2 = False

        if solenoid:
            cmds[solenoid] = Command(solenoid, self._co2,
                                     f"pH {reading.value:.2f} vs target {target:.2f}")
        loops["co2"] = {"enabled": True, "state": "injecting" if self._co2 else "off",
                        "target": target, "reading": round(reading.value, 2)}

    # -- dosing ----------------------------------------------------------
    def _loop_dose(self, now, cmds, faults, loops) -> None:
        doses = self.cfg.control.get("dose") or []
        if not doses:
            return
        lt = time.localtime(now)
        day = time.strftime("%Y-%m-%d", lt)
        if day != self._dose_day:
            self._dose_day = day
            self._dose_today.clear()
            self._dosed.clear()
        # The dose log is the source of truth, so a manual dose through the
        # API/CLI counts against the daily cap exactly like a scheduled one.
        if self._dose_source is not None:
            try:
                self._dose_today = dict(self._dose_source())
            except Exception:                            # noqa: BLE001
                pass

        status: dict[str, dict] = {}
        for spec in doses:
            pump = str(spec.get("pump"))
            if pump not in self.hw.status:
                continue
            ml = float(spec.get("ml", 0.0))
            cap = float(spec.get("daily_max_ml", ml) or ml)
            today = self._dose_today.get(pump, 0.0)

            for slot in spec.get("schedule", []):
                key = f"{pump}:{day}:{slot}"
                if key in self._dosed:
                    continue
                hh, mm = (int(x) for x in str(slot).split(":"))
                slot_ts = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, 0, -1))
                if now < slot_ts:
                    continue
                if now - slot_ts > 900:
                    self._dosed.add(key)          # window missed while running
                    continue
                if today + ml > cap + 1e-9:
                    self._dosed.add(key)
                    faults.append(Fault(
                        f"dose_cap:{pump}",
                        f"{pump}: daily cap {cap:g} ml reached, skipped {ml:g} ml at {slot}",
                        "warning", actuator=pump,
                    ))
                    continue
                try:
                    self.hw.dose(pump, ml, now, f"scheduled {ml:g} ml ({slot})")
                except ActuatorError as exc:
                    faults.append(Fault(f"dose:{pump}", str(exc), "warning", actuator=pump))
                    continue
                self._dosed.add(key)
                today += ml
                self._dose_today[pump] = today
                cmds[pump] = Command(pump, True, f"scheduled dose {ml:g} ml")

            status[pump] = {"today_ml": round(today, 2), "cap_ml": cap,
                            "next": self._next_slot(spec.get("schedule", []), lt)}
        loops["dose"] = status

    @staticmethod
    def _next_slot(schedule: list[str], lt: time.struct_time) -> str | None:
        now_minutes = lt.tm_hour * 60 + lt.tm_min
        ahead = sorted(
            (int(h) * 60 + int(m), s)
            for s in schedule
            for h, m in [str(s).split(":")]
        )
        for minutes, label in ahead:
            if minutes > now_minutes:
                return label
        return ahead[0][1] if ahead else None

    # -- reporting -------------------------------------------------------
    def dose_totals(self) -> dict[str, float]:
        return dict(self._dose_today)

    def seed_dose_totals(self, totals: dict[str, float], day: str) -> None:
        self._dose_day = day
        self._dose_today = dict(totals)

    def bind_dose_totals(self, source) -> None:
        """Point the loop at a live total (the persisted dose log)."""
        self._dose_source = source
