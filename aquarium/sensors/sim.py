"""A small physical model of the tank, used by the simulation drivers.

The point is not to be a faithful reef model — it is to make the control
loops observable: turn the heater on and the temperature climbs, let the sump
evaporate and the ATO refills it, run the CO2 solenoid and pH falls.  That
makes the whole stack testable and demoable without plumbing.
"""

from __future__ import annotations

import math
import random
import time

AMBIENT_C = 22.0
# Sized to a real tank: ~1000 W of heating into 340 L is roughly 7 °C/h, and
# the tank loses about 1 °C/h to a 22 °C room (tau ≈ 3 h).  Slow enough that
# the charts show a believable heater cycle rather than a square wave.
HEATER_C_PER_S = 0.0020
CHILLER_C_PER_S = 0.0026
THERMAL_TAU_S = 10_800.0      # passive drift back toward ambient
EVAP_PCT_PER_S = 0.00002      # ≈1.7 %/day
ATO_PCT_PER_S = 0.05          # ≈3 %/min, a small top-off pump


class SimWorld:
    """Shared mutable state for simulated probes and simulated relays."""

    def __init__(
        self,
        *,
        temp_c: float = 25.4,
        ph: float = 8.14,
        ec_us: float = 53_200.0,
        orp_mv: float = 342.0,
        do_mg_l: float = 6.6,
        level_pct: float = 94.0,
        volume_l: float = 340.0,
        photoperiod: tuple[int, int] = (10, 20),
        seed: int = 7,
    ) -> None:
        self.temp_c = temp_c
        self.ph = ph
        self.ec_us = ec_us
        self.orp_mv = orp_mv
        self.do_mg_l = do_mg_l
        self.level_pct = level_pct
        self.volume_l = volume_l
        self.photoperiod = photoperiod
        self.actuators: dict[str, bool] = {}
        self.dosed_ml: dict[str, float] = {}
        self.rng = random.Random(seed)
        self._last = time.time()
        self._hours = 0.0

    # -- helpers ---------------------------------------------------------
    def _hour(self, now: float | None = None) -> float:
        lt = time.localtime(now if now is not None else time.time())
        return lt.tm_hour + lt.tm_min / 60.0

    def lights_on(self, now: float | None = None) -> bool:
        if "light" in self.actuators:
            return bool(self.actuators["light"])
        start, end = self.photoperiod
        hour = self._hour(now)
        return start <= hour < end

    def value(self, metric: str, now: float | None = None) -> float:
        if metric == "temp_c":
            return self.temp_c
        if metric == "ph":
            return self.ph
        if metric == "ec_us":
            return self.ec_us
        if metric == "orp_mv":
            return self.orp_mv
        if metric == "do_mg_l":
            return self.do_mg_l
        if metric == "level_pct":
            return self.level_pct
        if metric == "turbidity_ntu":
            return 0.4
        raise KeyError(metric)

    # -- integration -----------------------------------------------------
    def tick(self, now: float | None = None, dt: float | None = None) -> None:
        now = time.time() if now is None else now
        dt = (now - self._last) if dt is None else dt
        self._last = now
        if dt <= 0:
            return
        dt = min(dt, 5.0)

        heater = bool(self.actuators.get("heater"))
        chiller = bool(self.actuators.get("chiller"))
        fan = bool(self.actuators.get("fan"))
        ato = bool(self.actuators.get("ato_pump"))
        co2 = bool(self.actuators.get("co2"))
        lit = self.lights_on(now)

        # Temperature: actuator input + passive exchange with the room.
        drive = HEATER_C_PER_S * heater - CHILLER_C_PER_S * chiller - 0.0005 * fan
        drive += (AMBIENT_C - self.temp_c) / THERMAL_TAU_S
        self.temp_c += drive * dt
        self.temp_c = min(max(self.temp_c, 15.0), 35.0)

        # Evaporation and top-off.
        self.level_pct -= EVAP_PCT_PER_S * dt * (1.4 if self.temp_c > 26 else 1.0)
        if ato:
            self.level_pct += ATO_PCT_PER_S * dt
        self.level_pct = min(max(self.level_pct, 40.0), 100.0)

        # Evaporation concentrates salts; top-off dilutes them back.
        concentration = (94.0 / max(self.level_pct, 50.0)) ** 0.35
        self.ec_us += (53_200.0 * concentration - self.ec_us) * min(1.0, dt / 3600.0)

        # pH: photosynthesis swing, CO2 injection, alkalinity dosing.
        swing = 0.07 * math.sin(2 * math.pi * (self._hour(now) - 6.0) / 24.0)
        target = 8.14 + swing + (0.03 if lit else 0.0)
        if co2:
            target -= 0.45
        self.ph += (target - self.ph) * min(1.0, dt / 1800.0)
        dose_alk = bool(self.actuators.get("dose_alk"))
        if dose_alk:
            self.ph += 0.012 * dt
        self.ph = min(max(self.ph, 6.0), 9.5)

        # ORP: slow mean reversion, suppressed while CO2 is running.
        orp_target = 342.0 if not co2 else 300.0
        self.orp_mv += (orp_target - self.orp_mv) * min(1.0, dt / 3600.0)

        # Dissolved oxygen: falls with temperature, rises under lighting.
        do_target = 7.0 - 0.12 * (self.temp_c - 25.0) + (0.25 if lit else 0.0)
        self.do_mg_l += (do_target - self.do_mg_l) * min(1.0, dt / 1800.0)

        self._hours += dt / 3600.0
