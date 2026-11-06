"""The runtime: wires probes, relays, control loops, storage and alerts together."""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from pathlib import Path

from .actuators import build_actuators
from .alerts import AlertEngine, Notifier
from .config import Config
from .control import Controller, Hardware
from .model import Fault, Reading
from .sensors import build_sensors, build_world
from .storage import Storage
from .units import CONVERTERS, DERIVED, METRICS, metric_info

log = logging.getLogger("aquarium.runtime")


def default_db_path() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "aquarium" / "aquarium.db"


class Runtime:
    def __init__(self, cfg: Config, db_path: str | Path | None = None) -> None:
        self.cfg = cfg
        self.started = time.time()
        self.world = build_world(cfg)
        self.sensors = build_sensors(cfg, self.world)
        self.actuators = build_actuators(cfg, self.world)
        self.hw = Hardware(cfg, self.actuators)
        self.store = Storage(db_path or default_db_path())
        self.alerts = AlertEngine(cfg, self.store, Notifier(cfg.notify))
        self.ctl = Controller(cfg, self.hw)

        self.readings: dict[str, Reading] = {}
        self.sensor_errors: dict[str, str] = {}
        self.faults: list[Fault] = []
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._subscribers: list[queue.Queue] = []
        self._last_prune = 0.0
        self._samples = 0

        self._seed_dosing()
        self.ctl.bind_dose_totals(self._dose_totals_today)

    # -- dosing continuity across restarts -------------------------------
    def _seed_dosing(self) -> None:
        start = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        try:
            totals = self.store.doses_since(start)
        except Exception:  # pragma: no cover - storage is best effort here
            totals = {}
        self.ctl.seed_dose_totals(totals, time.strftime("%Y-%m-%d"))

    def _dose_totals_today(self) -> dict[str, float]:
        start = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        return self.store.doses_since(start)

    # -- sensor polling --------------------------------------------------
    def _poll_sensors(self, now: float) -> None:
        for metric, sensor in self.sensors.items():
            try:
                reading = sensor.read()
            except Exception as exc:                     # noqa: BLE001 - driver boundary
                self.sensor_errors[metric] = str(exc)
                log.debug("sensor %s: %s", metric, exc)
                continue
            if reading.ts <= 0:
                reading = Reading(reading.metric, reading.value, reading.unit, now,
                                  reading.quality, reading.source)
            self.readings[metric] = reading
            self.sensor_errors.pop(metric, None)

    def _derive(self, now: float) -> None:
        for metric, (source, converter) in DERIVED.items():
            base = self.readings.get(source)
            if base is None or now - base.ts > self.cfg.stale_after_s:
                continue
            params = {}
            sensor_cfg = self.cfg.sensor_for(source)
            if sensor_cfg:
                params = sensor_cfg.params
            factor = params.get(f"{metric}_factor")
            fn = CONVERTERS[converter]
            value = fn(base.value, float(factor)) if factor is not None else fn(base.value)
            self.readings[metric] = Reading(
                metric, value, metric_info(metric).unit, base.ts, base.quality, "derived"
            )

    # -- one control cycle ----------------------------------------------
    def poll(self, now: float | None = None) -> list[Fault]:
        now = time.time() if now is None else now
        with self._lock:
            if self.world is not None:
                self.world.tick(now)
            self._poll_sensors(now)
            self._derive(now)
            commands, faults = self.ctl.evaluate(self.readings, now)
            faults = list(faults)
            faults.extend(self.hw.apply(commands, now))
            self.faults = faults
            self.alerts.evaluate(self.readings, faults, now)
            self._persist_side_effects(now)
        self._publish()
        return faults

    def _persist_side_effects(self, now: float) -> None:
        for event in self.hw.drain_events():
            self.store.add_event(event["ts"], event["actuator"], event["on"],
                                 event.get("reason", ""), event.get("source", "auto"))
        for dose in self.hw.drain_doses():
            self.store.add_dose(dose["ts"], dose["pump"], dose["ml"], dose.get("reason", ""))

    def _sample(self) -> None:
        with self._lock:
            readings = [r for r in self.readings.values() if r.ts > 0]
        if readings:
            self.store.add_readings(readings)
            self._samples += 1
        now = time.time()
        if now - self._last_prune > 3600:
            self._last_prune = now
            try:
                self.store.prune(self.cfg.server.history_days)
            except Exception as exc:                     # pragma: no cover
                log.warning("prune failed: %s", exc)

    # -- loops -----------------------------------------------------------
    def _control_loop(self) -> None:
        interval = self.cfg.server.control_interval_s
        next_at = time.time()
        while not self._stop.is_set():
            try:
                self.poll()
            except Exception:                            # noqa: BLE001 - keep running
                log.exception("control cycle failed")
            next_at += interval
            self._stop.wait(max(0.01, next_at - time.time()))

    def _sample_loop(self) -> None:
        interval = self.cfg.server.sample_interval_s
        next_at = time.time() + interval
        while not self._stop.is_set():
            self._stop.wait(max(0.05, next_at - time.time()))
            if self._stop.is_set():
                break
            try:
                self._sample()
            except Exception:                            # noqa: BLE001
                log.exception("sampling failed")
            next_at += interval

    def start(self) -> None:
        for target, name in ((self._control_loop, "control"), (self._sample_loop, "sampler")):
            thread = threading.Thread(target=target, name=f"aq-{name}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=3)
        with self._lock:
            # Fail safe: everything off on shutdown.
            for aid, status in self.hw.status.items():
                try:
                    if status.on:
                        self.actuators[aid].set(False)
                        self.store.add_event(time.time(), aid, False, "controller shutdown", "system")
                except Exception:                        # noqa: BLE001
                    log.warning("could not switch %s off on shutdown", aid)
            for sensor in self.sensors.values():
                try:
                    sensor.close()
                except Exception:                        # noqa: BLE001
                    pass
            for actuator in self.actuators.values():
                try:
                    actuator.close()
                except Exception:                        # noqa: BLE001
                    pass
            self.store.close()

    # -- manual control --------------------------------------------------
    def set_actuator(self, actuator_id: str, on: bool, minutes: float) -> None:
        with self._lock:
            self.hw.override(actuator_id, on, minutes, time.time())
            self.poll()
        log.info("manual override: %s -> %s for %s min", actuator_id, on, minutes)

    def clear_override(self, actuator_id: str) -> None:
        with self._lock:
            self.hw.clear_override(actuator_id, time.time())
            self.poll()

    def dose(self, actuator_id: str, ml: float) -> float:
        with self._lock:
            seconds = self.hw.dose(actuator_id, ml, time.time())
            self.poll()
        log.info("manual dose: %s %.1f ml (%.0fs)", actuator_id, ml, seconds)
        return seconds

    def clear_alerts(self, rule: str | None = None) -> int:
        with self._lock:
            return self.alerts.clear(rule)

    # -- reporting -------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            now = time.time()
            readings: dict[str, dict] = {}
            for metric, reading in self.readings.items():
                info = metric_info(metric)
                payload = reading.as_dict()
                payload["label"] = info.label
                readings[metric] = payload
            loops = {k: v for k, v in self.ctl.loops.items() if k != "dose"}
            return {
                "tank": {"name": self.cfg.tank.name, "volume_l": self.cfg.tank.volume_l,
                         "water_type": self.cfg.tank.water_type},
                "now": round(now, 3),
                "started": round(self.started, 3),
                "uptime_s": round(max(0.0, now - self.started), 1),
                "mode": self.cfg.mode,
                "samples": self._samples,
                "readings": readings,
                "actuators": self.hw.describe(now),
                "alerts": self.alerts.snapshot(now),
                "dosing": self.ctl.loops.get("dose", {}),
                "loops": loops,
                "sensor_errors": dict(self.sensor_errors),
            }

    def history(self, metrics: list[str], minutes: float, points: int = 240) -> dict:
        since = time.time() - max(60.0, minutes * 60)
        known = [m for m in metrics if m in METRICS]
        if not known:
            known = list(self.readings)
        return self.store.series(known, since, points)

    # -- SSE fan-out -----------------------------------------------------
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=8)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def _publish(self) -> None:
        if not self._subscribers:
            return
        snap = self.snapshot()
        with self._lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(snap)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(snap)
                except queue.Empty:                      # pragma: no cover
                    pass
