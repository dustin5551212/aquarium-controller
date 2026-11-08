import json
import threading
import urllib.error
import urllib.request

import pytest

from aquarium.api import ApiServer
from aquarium.app import Runtime
from conftest import make_config


def call(base, path, method="GET", payload=None, token=None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=5) as resp:
        body = resp.read()
        content_type = resp.headers.get("Content-Type", "")
        return resp.status, (json.loads(body) if "json" in content_type else body)


@pytest.fixture
def running(tmp_path):
    cfg = make_config()
    runtime = Runtime(cfg, tmp_path / "test.db")
    runtime.poll()
    runtime.store.add_readings(list(runtime.readings.values()))
    server = ApiServer(runtime, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield runtime, base
    finally:
        server.shutdown()
        server.server_close()
        runtime.stop()


def test_health_and_state(running):
    _runtime, base = running
    status, health = call(base, "/api/health")
    assert status == 200 and health["ok"] is True

    status, state = call(base, "/api/state")
    assert status == 200
    assert state["tank"]["name"] == "Test tank"
    assert "temp_c" in state["readings"]
    assert state["readings"]["temp_c"]["unit"] == "°C"
    assert isinstance(state["actuators"], list)
    assert any(a["id"] == "heater" for a in state["actuators"])
    # derived metric computed from EC even though no EC probe in this config
    assert state["mode"] == "simulation"


def test_manual_override_and_release(running):
    runtime, base = running
    status, body = call(base, "/api/actuators/heater", "POST",
                        {"on": True, "minutes": 5})
    assert status == 200 and body["ok"] is True
    assert runtime.hw.status["heater"].on is True
    assert runtime.hw.status["heater"].mode == "manual"

    # `minutes` is minutes: a 5 minute override must not expire in 5 seconds.
    _status, state = call(base, "/api/state")
    heater = next(a for a in state["actuators"] if a["id"] == "heater")
    assert 290 <= heater["override_remaining_s"] <= 300

    status, body = call(base, "/api/actuators/heater", "DELETE")
    assert status == 200
    assert runtime.hw.status["heater"].mode == "auto"


def test_manual_override_validation(running):
    _runtime, base = running
    with pytest.raises(urllib.error.HTTPError) as exc:
        call(base, "/api/actuators/nope", "POST", {"on": True, "minutes": 5})
    assert exc.value.code == 404

    with pytest.raises(urllib.error.HTTPError) as exc:
        call(base, "/api/actuators/heater", "POST", {"on": True, "minutes": 5000})
    assert exc.value.code == 400


def test_manual_dose(running):
    runtime, base = running
    status, body = call(base, "/api/actuators/dose_alk/dose", "POST", {"ml": 4})
    assert status == 200
    assert body["seconds"] == pytest.approx(2.0)
    assert runtime.hw.status["dose_alk"].on is True


def test_history_and_assets(running):
    _runtime, base = running
    status, history = call(base, "/api/history?minutes=15")
    assert status == 200
    assert "temp_c" in history["series"]

    status, html = call(base, "/")
    assert status == 200
    assert b"Aquarium Controller" in html


def test_unknown_endpoint(running):
    _runtime, base = running
    with pytest.raises(urllib.error.HTTPError) as exc:
        call(base, "/api/nope")
    assert exc.value.code == 404


def test_token_protection(tmp_path):
    cfg = make_config()
    cfg.server.token = "s3cret"
    runtime = Runtime(cfg, tmp_path / "auth.db")
    server = ApiServer(runtime, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            call(base, "/api/state")
        assert exc.value.code == 401
        status, _ = call(base, "/api/state?token=s3cret")
        assert status == 200
    finally:
        server.shutdown()
        server.server_close()
        runtime.stop()
