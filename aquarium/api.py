"""HTTP API and static dashboard hosting (stdlib only)."""

from __future__ import annotations

import json
import logging
import queue
import mimetypes
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .app import Runtime

log = logging.getLogger("aquarium.api")

WEB_DIR = Path(__file__).resolve().parent / "web"
MAX_BODY = 64 * 1024
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".json": "application/json",
    ".png": "image/png",
}


class ApiHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AquariumController/0.1"

    # -- plumbing --------------------------------------------------------
    @property
    def runtime(self) -> Runtime:
        return self.server.runtime  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:      # noqa: A003
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: bytes, content_type: str,
              extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: dict | list, status: int = 200) -> None:
        self._send(status, json.dumps(payload, separators=(",", ":")).encode(),
                   "application/json")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    # -- auth ------------------------------------------------------------
    def _authorised(self, query: dict[str, list[str]]) -> bool:
        token = self.runtime.cfg.server.token
        if not token:
            return True
        header = self.headers.get("Authorization", "")
        supplied = header[7:] if header.startswith("Bearer ") else ""
        supplied = supplied or (query.get("token", [""])[0])
        return supplied == token

    # -- GET -------------------------------------------------------------
    def do_GET(self) -> None:                            # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path.startswith("/api/"):
            if not self._authorised(query):
                self._error(401, "unauthorised")
                return
            try:
                self._api_get(path, query)
            except BrokenPipeError:
                pass
            except Exception as exc:                     # noqa: BLE001
                log.exception("GET %s failed", path)
                self._error(500, str(exc))
            return
        self._static(path)

    do_HEAD = do_GET

    def _api_get(self, path: str, query: dict[str, list[str]]) -> None:
        rt = self.runtime
        if path == "/api/health":
            snap = rt.snapshot()
            self._json({
                "ok": True,
                "mode": snap["mode"],
                "uptime_s": snap["uptime_s"],
                "alerts": len(snap["alerts"]),
                "readings": len(snap["readings"]),
                "time": time.time(),
            })
        elif path == "/api/state":
            self._json(rt.snapshot())
        elif path == "/api/history":
            metrics = [m for m in query.get("metrics", [""])[0].split(",") if m]
            minutes = float(query.get("minutes", ["180"])[0])
            points = int(query.get("points", ["240"])[0])
            self._json({"since": time.time() - minutes * 60,
                        "series": rt.history(metrics, minutes, points)})
        elif path == "/api/events":
            limit = int(query.get("limit", ["50"])[0])
            self._json({"events": rt.store.events(limit)})
        elif path == "/api/doses":
            limit = int(query.get("limit", ["50"])[0])
            self._json({"doses": rt.store.doses(limit)})
        elif path == "/api/alerts":
            limit = int(query.get("limit", ["100"])[0])
            self._json({"alerts": rt.store.alert_history(limit)})
        elif path == "/api/config":
            self._json(rt.cfg.public_dict())
        elif path == "/api/stream":
            self._stream()
        else:
            self._error(404, "no such endpoint")

    def _stream(self) -> None:
        q = self.runtime.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(b"retry: 2000\n\n")
            self.wfile.flush()
            while True:
                try:
                    snapshot = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                payload = json.dumps(snapshot, separators=(",", ":")).encode()
                self.wfile.write(b"data: " + payload + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.runtime.unsubscribe(q)

    # -- static ----------------------------------------------------------
    def _static(self, path: str) -> None:
        rel = "index.html" if path in ("", "/") else unquote(path.lstrip("/"))
        target = (WEB_DIR / rel).resolve()
        if not str(target).startswith(str(WEB_DIR.resolve())) or not target.is_file():
            self._error(404, "not found")
            return
        content_type = CONTENT_TYPES.get(target.suffix)
        if content_type is None:
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self._send(200, target.read_bytes(), content_type,
                   {"Cache-Control": "no-cache"})

    # -- POST / DELETE ---------------------------------------------------
    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError("request body too large")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("expected a JSON object")
        return payload

    def _routes(self) -> list[str]:
        return [p for p in urlparse(self.path).path.split("/") if p]

    def do_POST(self) -> None:                           # noqa: N802
        query = parse_qs(urlparse(self.path).query)
        if not self._authorised(query):
            self._error(401, "unauthorised")
            return
        parts = self._routes()
        try:
            body = self._body()
        except ValueError as exc:
            self._error(400, str(exc))
            return

        rt = self.runtime
        try:
            if len(parts) == 3 and parts[:2] == ["api", "actuators"]:
                actuator_id = parts[2]
                if actuator_id not in rt.hw.status:
                    self._error(404, f"unknown actuator {actuator_id}")
                    return
                on = bool(body.get("on"))
                minutes = float(body.get("minutes", 15))
                if not 0.1 <= minutes <= 720:
                    self._error(400, "minutes must be between 0.1 and 720")
                    return
                rt.set_actuator(actuator_id, on, minutes)
                self._json({"ok": True, "actuator": actuator_id, "on": on,
                            "minutes": minutes})
            elif len(parts) == 4 and parts[:2] == ["api", "actuators"] and parts[3] == "dose":
                actuator_id = parts[2]
                if actuator_id not in rt.hw.status:
                    self._error(404, f"unknown actuator {actuator_id}")
                    return
                ml = float(body.get("ml", 0))
                if not 0 < ml <= 500:
                    self._error(400, "ml must be between 0 and 500")
                    return
                seconds = rt.dose(actuator_id, ml)
                self._json({"ok": True, "pump": actuator_id, "ml": ml,
                            "seconds": round(seconds, 1)})
            elif parts == ["api", "alerts", "clear"]:
                removed = rt.clear_alerts(body.get("rule"))
                self._json({"ok": True, "cleared": removed})
            else:
                self._error(404, "no such endpoint")
        except Exception as exc:                         # noqa: BLE001
            log.exception("POST %s failed", self.path)
            self._error(400, str(exc))

    def do_DELETE(self) -> None:                         # noqa: N802
        query = parse_qs(urlparse(self.path).query)
        if not self._authorised(query):
            self._error(401, "unauthorised")
            return
        parts = self._routes()
        if len(parts) == 3 and parts[:2] == ["api", "actuators"]:
            actuator_id = parts[2]
            if actuator_id not in self.runtime.hw.status:
                self._error(404, f"unknown actuator {actuator_id}")
                return
            self.runtime.clear_override(actuator_id)
            self._json({"ok": True, "actuator": actuator_id, "mode": "auto"})
        else:
            self._error(404, "no such endpoint")


class ApiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, runtime: Runtime, host: str, port: int) -> None:
        self.runtime = runtime
        super().__init__((host, port), ApiHandler)


def serve(runtime: Runtime, host: str | None = None, port: int | None = None) -> ApiServer:
    cfg = runtime.cfg.server
    server = ApiServer(runtime, host or cfg.host, port or cfg.port)
    log.info("dashboard on http://%s:%s", host or cfg.host, server.server_address[1])
    return server
