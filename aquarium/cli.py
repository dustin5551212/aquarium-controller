"""Command line entry point: ``aqctl``."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
import time
import urllib.error
import urllib.request

from . import __version__
from .api import serve
from .config import ConfigError, DEFAULT_CONFIG_PATH, init_config, load
from .units import metric_info


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def cmd_init(args: argparse.Namespace) -> int:
    try:
        path = init_config(args.config, force=args.force)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {path}")
    try:
        cfg = load(path)
        print(f"config ok: {len(cfg.sensors)} sensors, {len(cfg.actuators)} actuators, "
              f"{len(cfg.alerts)} alerts, mode={cfg.mode}")
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    try:
        cfg = load(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(cfg.public_dict(), indent=2))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from .app import Runtime

    try:
        cfg = load(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    runtime = Runtime(cfg, args.db)
    runtime.start()
    server = serve(runtime, args.host, args.port)

    # serve_forever() must not run in the thread that calls shutdown(), or the
    # shutdown deadlocks waiting for a loop that is waiting for the signal
    # handler to return.
    stop = threading.Event()

    def _shutdown(*_: object) -> None:
        stop.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5},
                              name="aq-http", daemon=True)
    thread.start()
    try:
        while not stop.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    print("\nshutting down…", file=sys.stderr)
    runtime.stop()
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()
    return 0


def _fetch(url: str, token: str = "", timeout: float = 5.0) -> dict:
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def cmd_status(args: argparse.Namespace) -> int:
    base = args.url.rstrip("/")
    try:
        state = _fetch(f"{base}/api/state", args.token)
    except (urllib.error.URLError, OSError) as exc:
        print(f"error: cannot reach {base}: {exc}", file=sys.stderr)
        return 1

    tank = state.get("tank", {})
    print(f"{tank.get('name', 'aquarium')}  [{state.get('mode', '?')}]  "
          f"up {state.get('uptime_s', 0) / 3600:.1f} h")
    print("-" * 58)
    order = ["temp_c", "ph", "salinity_ppt", "ec_us", "orp_mv", "do_mg_l", "level_pct"]
    readings = state.get("readings", {})
    shown = [m for m in order if m in readings] + [m for m in readings if m not in order]
    for metric in shown:
        item = readings[metric]
        info = metric_info(metric)
        age = max(0.0, time.time() - item["ts"])
        stale = " STALE" if age > 60 else ""
        print(f"{info.label:<18} {info.fmt(item['value']):>9} {info.unit:<7}"
              f"  {age:4.0f}s ago{stale}")

    if state.get("alerts"):
        print("\nALERTS")
        for alert in state["alerts"]:
            print(f"  {alert['severity'].upper():<8} {alert['message']} "
                  f"({alert['age_s']:.0f}s)")

    print("\nEQUIPMENT")
    for act in state.get("actuators", []):
        state_label = "ON " if act["on"] else "off"
        extra = f"{act['on_for_s']:.0f}s" if act["on"] else "—"
        mode = " (manual)" if act["mode"] == "manual" else ""
        print(f"  {act['id']:<12} {state_label} {extra:>7}  {act['reason'][:38]}{mode}")

    dosing = state.get("dosing") or {}
    if dosing:
        print("\nDOSING TODAY")
        for pump, info in dosing.items():
            print(f"  {pump:<12} {info['today_ml']:>6.1f} / {info['cap_ml']:.0f} ml"
                  f"   next {info.get('next') or '—'}")
    return 0


def cmd_dose(args: argparse.Namespace) -> int:
    base = args.url.rstrip("/")
    payload = json.dumps({"ml": args.ml}).encode()
    req = urllib.request.Request(
        f"{base}/api/actuators/{args.pump}/dose", data=payload,
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {args.token}"} if args.token else {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(json.dumps(json.loads(resp.read()), indent=2))
    except (urllib.error.URLError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aqctl", description="Aquarium controller: sensing, control and alerts"
    )
    parser.add_argument("--version", action="version", version=f"aqctl {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="install a default configuration file")
    init.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_init)

    check = sub.add_parser("check", help="validate a configuration file")
    check.add_argument("--config", default=None)
    check.set_defaults(func=cmd_check)

    run = sub.add_parser("run", help="run the controller and dashboard")
    run.add_argument("--config", default=None)
    run.add_argument("--host", default=None)
    run.add_argument("--port", type=int, default=None)
    run.add_argument("--db", default=None, help="SQLite path (default: XDG data dir)")
    run.add_argument("--log-level", default="INFO")
    run.set_defaults(func=cmd_run)

    status = sub.add_parser("status", help="print live state from a running controller")
    status.add_argument("--url", default="http://127.0.0.1:5176")
    status.add_argument("--token", default="")
    status.set_defaults(func=cmd_status)

    dose = sub.add_parser("dose", help="trigger a manual dose")
    dose.add_argument("pump")
    dose.add_argument("ml", type=float)
    dose.add_argument("--url", default="http://127.0.0.1:5176")
    dose.add_argument("--token", default="")
    dose.set_defaults(func=cmd_dose)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        _configure_logging(getattr(args, "log_level", "INFO"))
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
