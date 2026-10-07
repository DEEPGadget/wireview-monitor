"""The recorder process: owns the device and the database writes.

Threads: the sampler (device reads on a fixed schedule), the store writer
(SQLite inserts and pruning), the feed sender per subscriber, the control
listener and a 1 Hz status publisher. None of them decodes history or
computes statistics, which happens in the api process, so nothing here
competes with the sampler for the GIL beyond a few microseconds per sample.
"""
from __future__ import annotations

import logging
import os
import signal
import threading
import time

from . import bus
from .events import EventEngine
from .sampler import Sampler
from .store import Store

log = logging.getLogger("wvd.recorder")

STATUS_INTERVAL_S = 1.0
FEED_QUEUE_S = 5
EXIT_FATAL = 70
FATAL_EXIT_GRACE_S = 10


class Recorder:
    def __init__(self, cfg, runtime_dir: str):
        self.cfg = cfg
        self.store = Store(cfg.db_path, ring_size=1, retention_s=cfg.retention_s, rate_hz=cfg.rate_hz, writer=True)
        self.engine = EventEngine(cfg.limits)
        self.fatal: str | None = None
        self.stopping = threading.Event()
        self.started_at = time.time()
        self._latest_line: bytes | None = None
        self.feed = bus.FeedServer(os.path.join(runtime_dir, bus.FEED_SOCKET),
                                   maxlen=max(50, int(FEED_QUEUE_S * cfg.rate_hz)), on_connect=self._greet)
        self.sampler = Sampler(self.store, self.engine, self._publish, cfg.rate_hz, cfg.device_port, cfg.simulate,
                               test_fault=cfg.test_fault, on_fatal=self._on_fatal)
        if cfg.test_fault and cfg.test_fault.startswith("db-slow:"):
            delay, write = float(cfg.test_fault.partition(":")[2]), self.store._write_batch

            def slow_write(items):
                if items:
                    time.sleep(delay)
                write(items)
            self.store._write_batch = slow_write
        # The first sample after a restart measures its gap from the last stored one.
        self.sampler._prev_ts = self.store.last_stored_ts
        self.control = bus.ControlServer(os.path.join(runtime_dir, bus.CONTROL_SOCKET), self._handle)
        self._status_thread = threading.Thread(target=self._status_loop, name="wvd-status", daemon=True)

    # -- feed ---------------------------------------------------------------
    def _publish(self, msg: dict) -> None:
        line = bus.encode(msg)
        if msg["kind"] == "sample":
            self._latest_line = line
        self.feed.publish(line)

    def _greet(self, put) -> None:
        put(bus.encode({"kind": "status", "data": self.status()}))
        if self._latest_line:
            put(self._latest_line)

    def status(self) -> dict:
        s, st = self.sampler, self.store
        return {
            "pid": os.getpid(), "ts": time.time(), "started_at": self.started_at,
            "connected": s.connected, "info": s.info.as_dict() if s.info and s.connected else None,
            "rate_hz": self.cfg.rate_hz, "measured_hz": s.measured_hz, "last_sample_wall": s.last_sample_wall,
            "last_seq": st.last_seq, "counters": dict(s.counters), "last_error": s.last_error,
            "sampler_alive": s.alive, "writer_alive": st.writer_alive, "fatal": self.fatal,
            "gaps_total": s.gaps_total, "max_gap_s_5m": round(s.max_gap_s(), 4), "last_gap": s.last_gap,
            "db_queue": st.pending, "db_dropped": st.db_dropped, "feed_dropped": self.feed.dropped,
            "simulated": bool(self.cfg.simulate),
        }

    def _status_loop(self) -> None:
        while not self.stopping.wait(STATUS_INTERVAL_S):
            try:
                self._publish({"kind": "status", "data": self.status()})
            except Exception:
                log.exception("status publish failed")

    # -- control ------------------------------------------------------------
    def _handle(self, req: dict) -> dict:
        op = req.get("op")
        if op == "status":
            return {"ok": True, "status": self.status()}
        if op == "publish":  # e.g. a session change from the api process
            self._publish(req["msg"])
            return {"ok": True}
        if op == "clear_faults":
            ks, kl = int(req.get("keep_status_mask", 0)), int(req.get("keep_log_mask", 0))
            self.sampler.run_command(lambda d: d.clear_faults(ks, kl))
            self.sampler._emit({"ts": time.time(), "type": "command.clear_faults",
                                "keep_status_mask": ks, "keep_log_mask": kl})
            return {"ok": True, "keep_status_mask": ks, "keep_log_mask": kl}
        return {"ok": False, "error": f"unknown op {op!r}"}

    # -- lifecycle ----------------------------------------------------------
    def _on_fatal(self, reason: str) -> None:
        log.critical("%s: recorder exits so it is restarted", reason)
        self.fatal = reason
        t = threading.Timer(FATAL_EXIT_GRACE_S, lambda: os._exit(EXIT_FATAL))
        t.daemon = True
        t.start()
        self.stopping.set()

    def run(self) -> int:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self.stopping.set())
        self.store.start()
        self.feed.start()
        self.control.start()
        self.sampler.start()
        self._status_thread.start()
        log.info("recording at %.0f Hz into %s", self.cfg.rate_hz, self.store.db_path)
        while not self.stopping.wait(1.0):
            pass
        self.sampler.stop()
        self.store.close()
        self.control.close()
        self.feed.close()
        return EXIT_FATAL if self.fatal else 0


def main(cfg, runtime_dir: str) -> int:
    return Recorder(cfg, runtime_dir).run()

