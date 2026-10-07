"""Sampling keeps its schedule whatever the device, the DB, the API or a process does."""
import asyncio
import os
import signal
import time

import pytest
from fastapi.testclient import TestClient

import wvd.api
import wvd.store
from conftest import start_daemon, wait_for
from wvd import samples as S
from wvd.api import Config, create_app
from wvd.front import Hub, Msg
from wvd.samples import StatsAccumulator, compute_stats
from wvd.store import Store
from wvd.transport import SimulatedDevice


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# -- the daemon -------------------------------------------------------------------
def test_termios_error_reconnects_in_place(daemon):
    d = daemon("--test-fault", "read-termios:25")
    h = wait_for(lambda: (lambda h: h if h["counters"]["connects"] >= 3 else None)(d.health()))
    assert h and h["sampler_alive"] and h["counters"]["disconnects"] >= 2
    assert h["processes"]["recorder"]["restarts"] == 0  # handled inside the recorder
    assert "Input/output error" in d.events("device.disconnected")[-1]["reason"]
    seq = h["last_seq"]
    assert wait_for(lambda: d.health()["last_seq"] > seq + 10)


def test_loop_error_survives_and_logs_gap(daemon):
    d = daemon("--test-fault", "loop-error:40")
    h = wait_for(lambda: (lambda h: h if h["counters"]["loop_errors"] >= 1 and h["gaps_total"] >= 1 else None)(
        d.health()))
    assert h and h["sampler_alive"] and h["max_gap_s_5m"] >= 0.5 and h["last_gap"]["gap_s"] >= 0.5
    assert wait_for(lambda: d.events("sampler.gap"))


def test_dead_sampler_restarts_recorder(daemon):
    d = daemon("--test-fault", "kill:100")  # the sampler thread dies 2 s into each recorder
    pid0 = d.health()["processes"]["recorder"]["pid"]
    assert wait_for(lambda: d.health()["status"] == "down", timeout=5)  # seen at once via the closed feed
    h = wait_for(lambda: (lambda h: h if h["processes"]["recorder"]["pid"] != pid0 and h["status"] == "ok"
                          else None)(d.health()), timeout=15)
    assert h and h["processes"]["recorder"]["pid"] != pid0 and h["processes"]["recorder"]["last_exit"] == 70
    # Seqs keep rising across the restart, and the pause is on record.
    gaps = wait_for(lambda: [e for e in d.events("sampler.gap") if e["gap_s"] >= 1])
    assert gaps
    assert d.get("/api/v1/sensors/latest").json()["seq"] > gaps[0]["seq"] - 1


def test_supervisor_gives_up_on_crash_loop():
    d = start_daemon("--test-fault", "kill:5", min_seq=None)
    try:
        assert d.proc.wait(timeout=60) == 70
    finally:
        d.stop()


def test_api_and_front_restart(daemon):
    d = daemon()
    procs = d.health()["processes"]
    os.kill(procs["api"]["pid"], signal.SIGKILL)
    assert wait_for(lambda: d.health()["api_ok"] is False, timeout=3)
    assert wait_for(lambda: (lambda h: h["api_ok"] and h["processes"]["api"]["restarts"] == 1)(d.health()))
    assert d.get("/api/v1/sensors/history?last=1s").status_code == 200
    os.kill(procs["front"]["pid"], signal.SIGKILL)
    h = wait_for(lambda: (lambda h: h if h["processes"]["front"]["restarts"] == 1 and h["status"] == "ok"
                          else None)(d.health()))
    assert h and h["processes"]["recorder"]["pid"] == procs["recorder"]["pid"]  # recording never stopped
    assert h["gaps_total"] == 0


def test_children_die_with_supervisor(daemon):
    d = daemon()
    procs = d.health()["processes"]
    os.kill(d.proc.pid, signal.SIGKILL)
    pids = [procs[r]["pid"] for r in ("recorder", "api", "front")]
    assert wait_for(lambda: not any(pid_alive(p) for p in pids), timeout=10)


def test_slow_db_does_not_stall_sampling(daemon):
    d = daemon("--test-fault", "db-slow:1.5")
    seq0 = d.health()["last_seq"]
    time.sleep(3)
    h = d.health()
    assert h["last_seq"] - seq0 >= 120 and h["max_gap_s_5m"] < 0.3 and h["db_queue"] > 0
    # The api serves the not yet committed tail from its feed-fed ring.
    got = d.get("/api/v1/sensors/history?last=1s").json()
    assert got["count"] >= 30


# -- the api process alone, on a pre-filled DB ----------------------------------------
def fill(path, n, rate=50.0, t_end=None):
    st = Store(str(path), ring_size=1, retention_s=1e9, rate_hz=rate, writer=True)
    st.start()
    dev = SimulatedDevice("load", seed=1)
    dev.open()
    t0 = (t_end or time.time()) - n / rate
    for i in range(n):
        frame = dev.read_sensor_frame()
        st.add(S.build_sample(st.next_seq(), t0 + i / rate, "D", frame, 1 / rate), frame)
    st.close()


@pytest.fixture
def api(tmp_path):
    def make(n=300, **cfg):
        fill(tmp_path / "wvd.db", n, t_end=time.time() - 10)
        c = TestClient(create_app(Config(db_path=str(tmp_path / "wvd.db"), rate_hz=50, **cfg)))
        c.__enter__()
        made.append(c)
        return c
    made = []
    yield make
    for c in made:
        c.__exit__(None, None, None)


def test_history_truncation_is_flagged(api, monkeypatch):
    monkeypatch.setattr(wvd.store, "MAX_QUERY_SAMPLES", 20)
    r = api().get("/api/v1/sensors/history?last=1h").json()
    assert r["truncated"] and r["count"] == 20


def test_export_streams_past_query_cap(api, monkeypatch):
    monkeypatch.setattr(wvd.store, "MAX_QUERY_SAMPLES", 20)
    monkeypatch.setattr(wvd.store, "CHUNK_SAMPLES", 7)
    lines = api().get("/api/v1/export?last=1h&format=csv").text.splitlines()
    assert lines[0].startswith("ts,seq,total_w") and lines[0].endswith("gap_s")
    seqs = [int(ln.split(",")[1]) for ln in lines[1:]]
    assert seqs == list(range(1, 301))


def test_ring_tail_merges_with_db(api):
    c = api()
    store = c.app.state.store
    last = store.query(0, time.time() + 10)[0][-1]
    dev = SimulatedDevice("idle", seed=2)
    dev.open()
    for i in range(1, 6):  # in the feed but not committed yet
        store.add(S.build_sample(last["seq"] + i, last["ts"] + i * 0.02, "D", dev.read_sensor_frame(), 0.02))
    seqs = [s["seq"] for s in c.get("/api/v1/sensors/history?last=1h").json()["samples"]]
    assert seqs == list(range(1, last["seq"] + 6))


def test_heavy_queries_are_capped(api, monkeypatch):
    monkeypatch.setattr(wvd.api, "HEAVY_WAIT_S", 0.2)
    c = api()
    sem = c.app.state.heavy_sem
    assert sem.acquire() and sem.acquire()
    try:
        r = c.get("/api/v1/sensors/stats?last=5s")
        assert r.status_code == 503 and r.headers["retry-after"] == "5"
        assert c.get("/api/v1/sessions").status_code == 200  # light requests unaffected
    finally:
        sem.release()
        sem.release()
    assert c.get("/api/v1/sensors/stats?last=1h").json()["count"] == 300


def test_session_stats_stored_once(api):
    c = api()
    sid = c.post("/api/v1/sessions", json={"label": "s"}).json()["id"]
    assert c.get("/api/v1/sessions").json()["sessions"][0]["stats"] is None  # running: not in the list
    stopped = c.post(f"/api/v1/sessions/{sid}/stop").json()
    listed = c.get("/api/v1/sessions").json()["sessions"][0]
    assert listed["stats"] == stopped["stats"] and "max_gap_s" in listed["stats"]


def test_device_commands_need_the_recorder(api):
    c = api(allow_write=True)
    assert c.post("/api/v1/device/clear-faults", json={}).status_code == 503


# -- pieces -------------------------------------------------------------------------
def test_hub_drops_backlog_with_lag_message():
    async def go():
        hub = Hub(maxsize=3)
        q = hub.subscribe()
        for i in range(5):
            hub.publish(Msg("sample", {"i": i}))
        return hub, [q.get_nowait() for _ in range(q.qsize())]

    hub, got = asyncio.run(go())
    assert got[0].kind == "lag" and got[0].data["dropped"] == 3
    assert [m.data["i"] for m in got[1:]] == [3, 4] and hub.dropped == 3


def test_store_seq_survives_unflushed_crash(tmp_path):
    st = Store(str(tmp_path / "w.db"), ring_size=1, retention_s=1e9, rate_hz=50, writer=True)
    st.start()
    handed_out = [st.next_seq() for _ in range(10)]  # published, never committed
    st._stop.set()  # writer gone without flushing, like a SIGKILL
    st2 = Store(str(tmp_path / "w.db"), ring_size=1, retention_s=1e9, rate_hz=50, writer=True)
    assert st2.next_seq() > max(handed_out)


def test_stats_accumulator_matches_and_strides(monkeypatch):
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
