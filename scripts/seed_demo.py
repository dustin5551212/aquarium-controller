#!/usr/bin/env python3
"""Backfill simulated history so the dashboard has something to draw.

This runs the real control loops against the simulated tank and writes the
resulting readings to the database.  It is demo data for simulation mode only
— never point it at a controller with real probes attached.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aquarium.app import Runtime, default_db_path  # noqa: E402
from aquarium.config import load  # noqa: E402
from aquarium.model import Reading  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--hours", type=float, default=6.0)
    parser.add_argument("--step", type=float, default=5.0)
    args = parser.parse_args()

    cfg = load(args.config)
    if cfg.mode != "simulation":
        print("refusing to seed: configuration is not in simulation mode", file=sys.stderr)
        return 1

    db = Path(args.db) if args.db else default_db_path()
    runtime = Runtime(cfg, db)
    if runtime.world is None:
        print("no simulated world configured", file=sys.stderr)
        return 1

    start = time.time() - args.hours * 3600
    now = start
    written = 0
    while now < time.time():
        runtime.world.tick(now)
        readings: dict[str, Reading] = {}
        for metric, sensor in runtime.sensors.items():
            try:
                fresh = sensor.read()
            except Exception:
                continue
            readings[metric] = Reading(metric, fresh.value, fresh.unit, now)
        cmds, _faults = runtime.ctl.evaluate(readings, now)
        runtime.hw.apply(cmds, now)
        runtime._persist_side_effects(now)
        runtime.store.add_readings(list(readings.values()))
        written += len(readings)
        now += args.step

    runtime.stop()
    print(f"wrote {written} readings to {db} covering {args.hours:g} h")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
