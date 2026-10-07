"""1.1: the sampler keeps its schedule whatever the device, the DB or the API do."""
import time

import pytest
from fastapi.testclient import TestClient

import wvd.api
import wvd.store
from wvd.api import Config, Hub, create_app
from wvd.samples import StatsAccumulator, compute_stats


def wait_for(fn, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    return fn()


def run(cfg, min_seq=10):
    c = TestClient(create_app(cfg))
    c.__enter__()
    wait_for(lambda: c.get("/api/v1/health").json()["last_seq"] >= min_seq)
    return c


def test_termios_error_reconnects():
    c = run(Config(simulate="load", rate_hz=50, db_path=":memory:", test_fault="read-termios:25"))
    try:
        h = wait_for(lambda: (lambda h: h if h["counters"]["connects"] >= 3 else None)(
            c.get("/api/v1/health").json()))
        assert h and h["sampler_alive"] and h["counters"]["disconnects"] >= 2
        ev = c.get("/api/v1/events?type=device.disconnected").json()["events"]
        assert ev and "Input/output error" in ev[-1]["reason"]
        seq = h["last_seq"]
        assert wait_for(lambda: c.get("/api/v1/health").json()["last_seq"] > seq + 10)
    finally:
        c.__exit__(None, None, None)


def test_loop_error_survives_and_logs_gap():
    c = run(Config(simulate="load", rate_hz=50, db_path=":memory:", test_fault="loop-error:40"))
    try:
        h = wait_for(lambda: (lambda h: h if h["counters"]["loop_errors"] >= 1 and h["gaps_total"] >= 1 else None)(
            c.get("/api/v1/health").json()))
        assert h and h["sampler_alive"] and h["max_gap_s_5m"] >= 0.5
        assert h["last_gap"]["gap_s"] >= 0.5
        assert wait_for(lambda: any(e["type"] == "sampler.gap"
                                    for e in c.get("/api/v1/events?last=1h").json()["events"]))
        assert c.get("/api/v1/sensors/latest").json()["gap_s"] is not None
    finally:
        c.__exit__(None, None, None)


def test_dead_sampler_reports_down_and_calls_fatal():
    called = []
    c = run(Config(simulate="load", rate_hz=50, db_path=":memory:", test_fault="kill:5", on_fatal=called.append),
            min_seq=0)
    try:
        assert wait_for(lambda: called)
        h = c.get("/api/v1/health").json()
        assert h["status"] == "down" and not h["sampler_alive"] and not h["connected"]
        assert "died" in h["fatal"]
    finally:
        c.__exit__(None, None, None)


def test_slow_db_does_not_stall_sampling(monkeypatch):
    c = run(Config(simulate="load", rate_hz=50, db_path=":memory:"))
    try:
        store = c.app.state.store
        real = store._write_batch

        def slow(items):
            time.sleep(1.5)
            real(items)
        monkeypatch.setattr(store, "_write_batch", slow)
        seq0 = store.last_seq
        time.sleep(2.0)
        h = c.get("/api/v1/health").json()
        assert h["last_seq"] - seq0 >= 80          # ~100 expected at 50 Hz
        assert h["max_gap_s_5m"] < 0.3
        # Rows the writer has not committed yet still come back from queries.
        got = c.get("/api/v1/sensors/history?last=1s").json()
        assert got["count"] >= 30
    finally:
        c.__exit__(None, None, None)


def test_db_query_merges_unflushed_tail(monkeypatch):
    monkeypatch.setattr(wvd.api.Config, "ring_s", 1.0)  # force DB reads for a 3 s window
    c = run(Config(simulate="load", rate_hz=50, db_path=":memory:"), min_seq=150)
    try:
        s = c.get("/api/v1/sensors/history?last=3s").json()["samples"]
        seqs = [x["seq"] for x in s]
        assert seqs == sorted(set(seqs)) and seqs[-1] - seqs[0] == len(seqs) - 1
    finally:
        c.__exit__(None, None, None)


def test_history_truncation_is_flagged(monkeypatch):
    monkeypatch.setattr(wvd.store, "MAX_QUERY_SAMPLES", 20)
    c = run(Config(simulate="load", rate_hz=50, db_path=":memory:"), min_seq=40)
    try:
        r = c.get("/api/v1/sensors/history?last=1h").json()
        assert r["truncated"] and r["count"] == 20
    finally:
        c.__exit__(None, None, None)


def test_export_streams_past_query_cap(monkeypatch):
    monkeypatch.setattr(wvd.store, "MAX_QUERY_SAMPLES", 20)
    monkeypatch.setattr(wvd.store, "CHUNK_SAMPLES", 7)
    c = run(Config(simulate="load", rate_hz=50, db_path=":memory:"), min_seq=60)
    try:
        lines = c.get("/api/v1/export?last=1h&format=csv").text.splitlines()
        assert lines[0].startswith("ts,seq,total_w") and lines[0].endswith("gap_s")
        assert len(lines) - 1 >= 60
        seqs = [int(l.split(",")[1]) for l in lines[1:]]
        assert seqs == sorted(set(seqs))
    finally:
        c.__exit__(None, None, None)


def test_heavy_queries_are_capped(monkeypatch):
    monkeypatch.setattr(wvd.api, "HEAVY_WAIT_S", 0.2)
    c = run(Config(simulate="idle", rate_hz=50, db_path=":memory:"))
    try:
        sem = c.app.state.heavy_sem
        assert sem.acquire() and sem.acquire()
        try:
            r = c.get("/api/v1/sensors/stats?last=5s")
            assert r.status_code == 503 and r.headers["retry-after"] == "5"
            assert c.get("/api/v1/sensors/latest").status_code == 200  # light requests unaffected
        finally:
            sem.release()
            sem.release()
        assert c.get("/api/v1/sensors/stats?last=5s").status_code == 200
    finally:
        c.__exit__(None, None, None)


def test_session_stats_stored_once():
    c = run(Config(simulate="load", rate_hz=50, db_path=":memory:"))
    try:
        sid = c.post("/api/v1/sessions", json={"label": "s"}).json()["id"]
        assert c.get("/api/v1/sessions").json()["sessions"][0]["stats"] is None  # running: not in the list
        time.sleep(0.3)
        stopped = c.post(f"/api/v1/sessions/{sid}/stop").json()
        listed = c.get("/api/v1/sessions").json()["sessions"][0]
        assert listed["stats"] == stopped["stats"] and listed["stats"]["count"] >= 10
        assert "max_gap_s" in listed["stats"]
    finally:
        c.__exit__(None, None, None)


def test_hub_drops_backlog_with_lag_message():
    import asyncio

    async def go():
        hub = Hub(maxsize=3)
        hub.loop = asyncio.get_running_loop()
        q = hub.subscribe()
        for i in range(5):
            hub._fanout({"kind": "sample", "data": {"i": i}})
        got = [q.get_nowait() for _ in range(q.qsize())]
        return hub, got

    hub, got = asyncio.run(go())
    assert got[0]["kind"] == "lag" and got[0]["data"]["dropped"] == 3
    assert [m["data"]["i"] for m in got[1:]] == [3, 4] and hub.dropped == 3


def test_stats_accumulator_matches_and_strides(monkeypatch):
    from wvd import samples as S
    from wvd.transport import SimulatedDevice

    dev = SimulatedDevice("load", seed=1)
    dev.open()
    xs = [S.build_sample(i, 1000 + i * 0.02, "d", dev.read_sensor_frame(), 0.02) for i in range(500)]
    whole = compute_stats(xs)
    acc = StatsAccumulator(len(xs))
    for i in range(0, 500, 64):
        acc.add(xs[i:i + 64])
    assert acc.result() == whole
    monkeypatch.setattr(S, "P95_KEEP", 100)
    strided = StatsAccumulator(len(xs))
    strided.add(xs)
    r = strided.result()
    assert r["p95_stride"] == 5 and r["fields"]["total_w"]["max"] == whole["fields"]["total_w"]["max"]
