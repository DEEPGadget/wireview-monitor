"""The whole daemon end to end (front + api + recorder processes) on the simulator."""
import json
import time

import pytest
from websockets.sync.client import connect as ws_connect

from conftest import start_daemon, wait_for


@pytest.fixture(scope="module")
def d():
    d = start_daemon("--allow-write", "--limit-total-w", "400", simulate="fault")
    yield d
    d.stop()


def test_health_info_latest(d):
    h = d.health()
    assert h["status"] == "ok" and h["connected"] and h["simulated"] and h["counters"]["ok"] >= 10
    assert h["api_ok"] and h["sampler_alive"] and h["writer_alive"]
    assert set(h["processes"]) >= {"recorder", "api", "front"}
    assert d.get("/api/v1/info").json()["hw_rev"] == "EF05"
    s = d.get("/api/v1/sensors/latest").json()
    assert len(s["pins"]) == 6 and s["temps_c"]["ext2"] is None and s["age_s"] < 1 and "gap_s" in s


def test_history_and_step(d):
    full = d.get("/api/v1/sensors/history?last=10s").json()
    assert full["count"] >= 10
    seq = full["samples"][0]["seq"]
    after = d.get(f"/api/v1/sensors/history?after_seq={seq}").json()
    assert after["samples"] and all(s["seq"] > seq for s in after["samples"])
    stepped = d.get("/api/v1/sensors/history?last=10s&step=1s").json()
    assert stepped["count"] < full["count"]
    assert d.get("/api/v1/sensors/history?last=banana").status_code == 400


def test_session_lifecycle_and_export(d):
    sid = d.post("/api/v1/sessions", json={"label": "t1", "meta": {"dut": "sim"}}).json()["id"]
    time.sleep(0.4)
    r = d.post(f"/api/v1/sessions/{sid}/stop").json()
    assert not r["active"] and r["stats"]["count"] >= 10 and r["meta"] == {"dut": "sim"}
    csv = d.get(f"/api/v1/sessions/{sid}/export?format=csv").text.splitlines()
    assert csv[0].startswith("ts,seq,total_w") and len(csv) == r["stats"]["count"] + 1
    assert d.get("/api/v1/sensors/stats", params={"session": sid}).json()["count"] == r["stats"]["count"]
    assert d.get("/api/v1/sessions").json()["sessions"][0]["stats"] == r["stats"]
    assert d.get("/api/v1/sessions/999").status_code == 404


def test_limit_event(d):
    types = wait_for(lambda: (lambda t: t if "limit.exceeded" in t else None)([e["type"] for e in d.events()]))
    assert types and "device.connected" in types


def test_websocket_stream_and_session_notice(d):
    with ws_connect(d.url.replace("http", "ws") + "/api/v1/stream") as ws:
        hello = json.loads(ws.recv(timeout=5))
        assert hello["kind"] == "hello" and hello["data"]["connected"]
        kinds = {json.loads(ws.recv(timeout=5))["kind"] for _ in range(5)}
        assert "sample" in kinds
        sid = d.post("/api/v1/sessions", json={"label": "ws"}).json()["id"]
        deadline = time.time() + 5
        seen = False
        while time.time() < deadline and not seen:
            m = json.loads(ws.recv(timeout=5))
            seen = m["kind"] == "session" and m["data"]["id"] == sid
        assert seen  # api -> recorder feed -> front -> client
        d.http.delete(f"/api/v1/sessions/{sid}")


def test_sse_stream(d):
    with d.http.stream("GET", "/api/v1/stream/sse?hz=10") as r:
        kinds = []
        for line in r.iter_lines():
            if line.startswith("event: "):
                kinds.append(line[7:])
            if len(kinds) >= 4:
                break
    assert kinds[0] == "hello" and "sample" in kinds


def test_metrics(d):
    m = d.get("/metrics").text
    assert "wvd_up 1" in m and 'wvd_pin_current_amps{pin="6"}' in m and "wvd_process_restarts_total" in m


def test_clear_faults(d):
    r = d.post("/api/v1/device/clear-faults", json={"fault": "OCP"}).json()
    assert r["keep_status_mask"] == 0xFFFB
    assert d.post("/api/v1/device/clear-faults", json={"fault": "NOPE"}).status_code == 400
    assert wait_for(lambda: d.events("command.clear_faults"))


def test_docs_proxied(d):
    assert d.get("/openapi.json").json()["info"]["title"] == "wvd"


def test_token_and_writes_off():
    d = start_daemon("--token", "s3cret", simulate="idle", min_seq=None)
    try:
        assert wait_for(lambda: d.health()["last_seq"] > 5, timeout=30)  # health stays open
        assert d.get("/api/v1/sensors/latest").status_code == 401
        assert d.get("/api/v1/sensors/history?last=1s").status_code == 401  # proxied routes too
        auth = {"Authorization": "Bearer s3cret"}
        assert d.get("/api/v1/sensors/latest", headers=auth).status_code == 200
        assert d.get("/api/v1/sensors/history?last=1s&token=s3cret").status_code == 200
        assert d.post("/api/v1/device/clear-faults", json={}, headers=auth).status_code == 403
    finally:
        d.stop()
