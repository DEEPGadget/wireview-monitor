"""Load test: does sampling keep its schedule while the API is hammered?

Not collected by pytest (takes minutes). Two modes:

    # local simulated daemon, DB pre-filled with 3 h of 50 Hz data and a 3 h session
    .venv/bin/python tests/wvd_stress.py --duration 120

    # an already running daemon (e.g. the real device); only reads, plus short
    # sessions it creates and deletes itself
    .venv/bin/python tests/wvd_stress.py --url http://127.0.0.1:8765 --duration 600

Workload, all at once for --duration: 1 h history / stats / CSV export loops,
dashboard-style session refreshes (list + 15 details), session start/stop
cycles, and --sse full-rate SSE subscribers. Verdict: the largest gap between
consecutive stored samples over the run window, read back from the daemon's
own export, must stay under --max-gap (default 100 ms); health counters must
show no DB drops.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from wvd.client import WireView  # noqa: E402


def http(url: str, method: str = "GET", body: dict | None = None, timeout: float = 120) -> bytes:
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"} if body is not None else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


LEGACY_SCHEMA = """
CREATE TABLE samples (seq INTEGER PRIMARY KEY, ts REAL NOT NULL, device TEXT NOT NULL, frame BLOB NOT NULL);
CREATE INDEX samples_ts ON samples(ts);
CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, type TEXT NOT NULL, data TEXT NOT NULL);
CREATE INDEX events_ts ON events(ts);
CREATE TABLE sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL, meta TEXT NOT NULL DEFAULT '{}',
                       start_ts REAL NOT NULL, end_ts REAL);
"""


def seed_db(path: str, hours: float, rate: float, legacy: bool = False) -> None:
    """Fill a fresh DB with simulated frames ending now, and one ended session over all of it."""
    from wvd.store import Store
    from wvd.transport import SimulatedDevice

    if legacy:  # the 1.0 schema, for comparison runs against 1.0 code
        db = sqlite3.connect(path)
        db.executescript(LEGACY_SCHEMA)
        db.execute("PRAGMA journal_mode=WAL")
        db.close()
    else:
        Store(path, ring_size=100, retention_s=7 * 86400).close()  # schema
    dev = SimulatedDevice("load", seed=1)
    dev.open()
    frames = [dev.read_sensor_frame() for _ in range(500)]
    n = int(hours * 3600 * rate)
    t0 = time.time() - hours * 3600
    db = sqlite3.connect(path)
    db.execute("BEGIN")
    rows = ((i + 1, t0 + i / rate, "STRESS", frames[i % len(frames)], 1 / rate) for i in range(n))
    if legacy:
        db.executemany("INSERT INTO samples (seq, ts, device, frame) VALUES (?,?,?,?)", (r[:4] for r in rows))
    else:
        db.executemany("INSERT INTO samples (seq, ts, device, frame, gap) VALUES (?,?,?,?,?)", rows)
    db.execute("INSERT INTO sessions (label, meta, start_ts, end_ts) VALUES ('stress-3h', '{}', ?, ?)",
               (t0, t0 + n / rate))
    db.commit()
    db.close()
    print(f"seeded {n} samples ({hours} h at {rate} Hz) into {path}")


class Load:
    def __init__(self, url: str, stop: threading.Event):
        self.url, self.stop = url, stop
        self.counts: dict[str, int] = {}
        self.errors: dict[str, int] = {}
        self.lock = threading.Lock()

    def loop(self, name: str, fn) -> threading.Thread:
        def run():
            while not self.stop.is_set():
                try:
                    fn()
                    key = self.counts
                except urllib.error.HTTPError as e:
                    key = self.errors
                    name_ = f"{name}:{e.code}"
                    with self.lock:
                        key[name_] = key.get(name_, 0) + 1
                    time.sleep(0.5)
                    continue
                except Exception as e:  # noqa: BLE001
                    key = self.errors
                    with self.lock:
                        key[f"{name}:{type(e).__name__}"] = key.get(f"{name}:{type(e).__name__}", 0) + 1
                    time.sleep(0.5)
                    continue
                with self.lock:
                    key[name] = key.get(name, 0) + 1
        t = threading.Thread(target=run, name=name, daemon=True)
        t.start()
        return t


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", help="daemon to test (default: start a simulated one)")
    ap.add_argument("--duration", type=float, default=120, help="seconds of load (default 120)")
    ap.add_argument("--rate", type=float, default=50, help="sample rate of the local daemon (default 50)")
    ap.add_argument("--seed-hours", type=float, default=3, help="history in the local daemon's DB (default 3)")
    ap.add_argument("--sse", type=int, default=5, help="full-rate SSE subscribers (default 5)")
    ap.add_argument("--max-gap", type=float, default=0.1, help="pass threshold, seconds (default 0.1)")
    ap.add_argument("--workload", choices=("full", "dashboard", "none"), default="full",
                    help="full: everything below (default); dashboard: session refreshes, session cycles and "
                         "SSE only (what the 1.0 incidents saw); none: idle baseline")
    ap.add_argument("--wvd-src", help="run the local daemon from this source tree instead (e.g. a 1.0 checkout); "
                                      "seeds the 1.0 schema")
    args = ap.parse_args()

    proc = tmp = None
    url = args.url
    if not url:
        tmp = tempfile.mkdtemp(prefix="wvd-stress-")
        db = os.path.join(tmp, "wvd.db")
        seed_db(db, args.seed_hours, args.rate, legacy=bool(args.wvd_src))
        port = free_port()
        url = f"http://127.0.0.1:{port}"
        proc = subprocess.Popen([sys.executable, "-m", "wvd", "--simulate", "load", "--host", "127.0.0.1",
                                 "--port", str(port), "--db", db, "--rate", str(args.rate),
                                 "--retention", "7d", "--log-level", "warning"],
                                cwd=args.wvd_src or None,
                                env={**os.environ, "PYTHONPATH": args.wvd_src} if args.wvd_src else None)
    wv = WireView(url)
    try:
        wv.wait_ready(timeout=30)
        stop = threading.Event()
        load = Load(url, stop)

        def dashboard_sessions():
            sessions = json.loads(http(f"{url}/api/v1/sessions?limit=15"))["sessions"]
            for s in sessions:  # the 1.0 dashboard's pattern, worst case
                http(f"{url}/api/v1/sessions/{s['id']}")

        def session_cycle():
            sid = json.loads(http(f"{url}/api/v1/sessions", "POST", {"label": "stress-cycle"}))["id"]
            time.sleep(1)
            http(f"{url}/api/v1/sessions/{sid}/stop", "POST")
            http(f"{url}/api/v1/sessions/{sid}", "DELETE")

        lat = {"max": 0.0, "n": 0, "over_100ms": 0}

        def sse():
            req = urllib.request.Request(f"{url}/api/v1/stream/sse")
            with urllib.request.urlopen(req, timeout=30) as r:
                kind = None
                for raw in r:
                    if stop.is_set():
                        return
                    line = raw.decode().rstrip("\n")
                    if line.startswith("event: "):
                        kind = line[7:]
                    elif line.startswith("data: ") and kind == "sample":
                        # Delivery delay: arrival minus sampling time (same host clock).
                        d = time.time() - json.loads(line[6:])["ts"]
                        lat["n"] += 1
                        lat["max"] = max(lat["max"], d)
                        lat["over_100ms"] += d > 0.1

        t_start = time.time()
        threads = []
        if args.workload == "full":
            threads += [
                load.loop("history-1h", lambda: http(f"{url}/api/v1/sensors/history?last=1h")),
                load.loop("stats-1h", lambda: http(f"{url}/api/v1/sensors/stats?last=1h")),
                load.loop("export-1h", lambda: http(f"{url}/api/v1/export?last=1h&format=csv")),
            ]
        threads.append(load.loop("sse-latency", sse))
        if args.workload in ("full", "dashboard"):
            threads += [
                load.loop("dashboard-sessions", dashboard_sessions),
                load.loop("dashboard-sessions-2", dashboard_sessions),
                load.loop("session-cycle", session_cycle),
            ] + [load.loop(f"sse-{i}", sse) for i in range(args.sse)]
        while time.time() - t_start < args.duration:
            time.sleep(5)
            try:
                h = wv.health()
            except Exception as e:  # noqa: BLE001 - an overloaded 1.0 daemon stops answering
                print(f"  t={time.time() - t_start:5.0f}s  health failed: {type(e).__name__}", flush=True)
                continue
            print(f"  t={time.time() - t_start:5.0f}s  max_gap_5m={h.get('max_gap_s_5m')}  "
                  f"gaps={h.get('gaps_total')}  db_queue={h.get('db_queue')}  ok={load.counts}  err={load.errors}",
                  flush=True)
        stop.set()
        t_end = time.time()
        for t in threads:
            t.join(timeout=10)

        time.sleep(1)  # let the writer commit the tail
        lines = http(f"{url}/api/v1/export?from={t_start}&to={t_end}&format=csv").decode().splitlines()
        rows = [ln.split(",") for ln in lines[1:] if not ln.startswith("#")]
        ts = [float(r[0]) for r in rows]
        seqs = [int(r[1]) for r in rows]
        gaps = sorted((b - a for a, b in zip(ts, ts[1:])), reverse=True)
        h = wv.health()
        max_gap = gaps[0] if gaps else float("inf")
        expected = (t_end - t_start) * float(h["rate_hz"])
        print(f"\nsamples {len(ts)} of ~{expected:.0f} expected, seq contiguous: "
              f"{seqs == list(range(seqs[0], seqs[0] + len(seqs))) if seqs else False}")
        print(f"largest gaps (s): {[round(g, 4) for g in gaps[:5]]}")
        print(f"health: gaps_total={h.get('gaps_total')} db_dropped={h.get('db_dropped')} "
              f"stream_dropped={h.get('stream_dropped')} loop_errors={h['counters'].get('loop_errors')}")
        print(f"stream delivery: max {lat['max'] * 1000:.1f} ms over {lat['n']} samples, "
              f"{lat['over_100ms']} later than 100 ms")
        print(f"requests ok={load.counts} errors={load.errors}")
        ok = max_gap < args.max_gap and not h.get("db_dropped")
        print(f"\n{'PASS' if ok else 'FAIL'}: max gap {max_gap * 1000:.1f} ms (limit {args.max_gap * 1000:.0f} ms)")
        return 0 if ok else 1
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                print("daemon did not stop within 15 s, killing it")
                proc.kill()
                proc.wait()
        if tmp:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
