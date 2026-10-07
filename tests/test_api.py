"""API end to end against the simulator, in-process."""
import time

import pytest
from fastapi.testclient import TestClient

from wvd.api import Config, create_app
from wvd.events import Limits


@pytest.fixture
def client():
    cfg = Config(simulate="fault", rate_hz=50, db_path=":memory:", allow_write=True,
                 limits=Limits(total_w=400))
    with TestClient(create_app(cfg)) as c:
        deadline = time.time() + 5
        while time.time() < deadline and c.get("/api/v1/health").json()["last_seq"] < 10:
            time.sleep(0.05)
        yield c


def test_health_info_latest(client):
    h = client.get("/api/v1/health").json()
    assert h["connected"] and h["simulated"] and h["counters"]["ok"] >= 10
    assert client.get("/api/v1/info").json()["hw_rev"] == "EF05"
    s = client.get("/api/v1/sensors/latest").json()
    assert len(s["pins"]) == 6 and s["temps_c"]["ext2"] is None


def test_history_and_step(client):
    full = client.get("/api/v1/sensors/history?last=10s").json()
    assert full["count"] >= 10
    seq = full["samples"][0]["seq"]
    after = client.get(f"/api/v1/sensors/history?after_seq={seq}").json()
    assert all(s["seq"] > seq for s in after["samples"])
    stepped = client.get("/api/v1/sensors/history?last=10s&step=1s").json()
    assert stepped["count"] < full["count"]
    assert client.get("/api/v1/sensors/history?last=banana").status_code == 400


def test_session_lifecycle_and_export(client):
    sid = client.post("/api/v1/sessions", json={"label": "t1", "meta": {"dut": "sim"}}).json()["id"]
    time.sleep(0.4)
    r = client.post(f"/api/v1/sessions/{sid}/stop").json()
    assert not r["active"] and r["stats"]["count"] >= 10 and r["meta"] == {"dut": "sim"}
    csv = client.get(f"/api/v1/sessions/{sid}/export?format=csv").text.splitlines()
    assert csv[0].startswith("ts,seq,total_w") and len(csv) == r["stats"]["count"] + 1
    assert client.get("/api/v1/sensors/stats", params={"session": sid}).json()["count"] == r["stats"]["count"]
    assert client.get("/api/v1/sessions/999").status_code == 404


def test_limit_event(client):
    deadline = time.time() + 5
    while time.time() < deadline:
        types = [e["type"] for e in client.get("/api/v1/events?last=1h").json()["events"]]
        if "limit.exceeded" in types:
            break
        time.sleep(0.1)
    assert "device.connected" in types and "limit.exceeded" in types


def test_websocket_stream(client):
    with client.websocket_connect("/api/v1/stream") as ws:
        hello = ws.receive_json()
        assert hello["kind"] == "hello" and hello["data"]["connected"]
        kinds = {ws.receive_json()["kind"] for _ in range(5)}
        assert "sample" in kinds


def test_metrics(client):
    m = client.get("/metrics").text
    assert "wvd_up 1" in m and 'wvd_pin_current_amps{pin="6"}' in m


def test_clear_faults(client):
    assert client.post("/api/v1/device/clear-faults", json={"fault": "OCP"}).json()["keep_status_mask"] == 0xFFFB
    assert client.post("/api/v1/device/clear-faults", json={"fault": "NOPE"}).status_code == 400


def test_token_required():
    cfg = Config(simulate="idle", db_path=":memory:", token="s3cret")
    with TestClient(create_app(cfg)) as c:
        assert c.get("/api/v1/health").status_code == 200  # open for monitors
        assert c.get("/api/v1/sensors/latest").status_code == 401
        time.sleep(0.3)
        assert c.get("/api/v1/sensors/latest", headers={"Authorization": "Bearer s3cret"}).status_code == 200
        assert c.get("/api/v1/sensors/latest?token=s3cret").status_code == 200


def test_writes_off_by_default():
    with TestClient(create_app(Config(simulate="idle", db_path=":memory:"))) as c:
        assert c.post("/api/v1/device/clear-faults", json={}).status_code == 403
