"""Sample, event and session storage.

Recent samples live in a memory ring for live views. Every raw 100-byte frame
is also written to SQLite so any window can be re-decoded later; frames older
than the retention are pruned unless a session covers them.

The sampler never waits on this module: add() and add_event() only append to
the ring and to a queue. A writer thread owns all inserts and pruning, and
readers use their own connections (WAL lets them run beside the writer), so
no query holds anything the sampler needs.
"""
from __future__ import annotations

import bisect
import json
import logging
import os
import queue
import shutil
import sqlite3
import tempfile
import threading
import time
from collections import deque
from typing import Callable, Iterator

from .samples import build_sample

log = logging.getLogger("wvd.store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    seq    INTEGER PRIMARY KEY,
    ts     REAL NOT NULL,
    device TEXT NOT NULL,
    frame  BLOB NOT NULL,
    gap    REAL
);
CREATE INDEX IF NOT EXISTS samples_ts ON samples(ts);
CREATE TABLE IF NOT EXISTS events (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    ts   REAL NOT NULL,
    type TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS sessions (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    label    TEXT NOT NULL,
    meta     TEXT NOT NULL DEFAULT '{}',
    start_ts REAL NOT NULL,
    end_ts   REAL,
    stats    TEXT
);
"""
# Columns added after 1.0, for databases created by it.
MIGRATIONS = (("samples", "gap", "REAL"), ("sessions", "stats", "TEXT"))

MAX_QUERY_SAMPLES = 500_000
CHUNK_SAMPLES = 10_000
FLUSH_INTERVAL_S = 0.5
PRUNE_INTERVAL_S = 60
PRUNE_CHUNK = 5_000
PRUNE_MAX_CHUNKS = 20      # per pass; 100k rows, far above the 3k/min written at 50 Hz
MAX_PENDING_S = 300        # writer backlog kept before samples are dropped from the DB
BUSY_TIMEOUT_MS = 10_000


def default_db_path() -> str:
    base = os.environ.get("STATE_DIRECTORY") or os.path.join(
        os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "wvd")
    return os.path.join(base, "wvd.db")


class Store:
    def __init__(self, db_path: str | None, ring_size: int, retention_s: float, rate_hz: float = 10.0):
        self.retention_s = retention_s
        self._ring: deque[dict] = deque(maxlen=ring_size)
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._max_pending = max(1000, int(MAX_PENDING_S * rate_hz))
        self.db_dropped = 0
        self.flushed_seq = 0
        # ':memory:' cannot be shared between connections; use a throwaway file.
        self._tmpdir = None
        if not db_path or db_path == ":memory:":
            self._tmpdir = tempfile.mkdtemp(prefix="wvd-")
            db_path = os.path.join(self._tmpdir, "wvd.db")
        else:
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.db_path = db_path
        self._local = threading.local()
        self._conns: list[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()

        db = self._conn()
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)
        for table, col, kind in MIGRATIONS:
            if col not in {r[1] for r in db.execute(f"PRAGMA table_info({table})")}:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {kind}")
        self.last_seq = db.execute("SELECT MAX(seq) FROM samples").fetchone()[0] or 0
        self.flushed_seq = self.last_seq
        self._last_event_id = db.execute("SELECT MAX(id) FROM events").fetchone()[0] or 0
        self._event_lock = threading.Lock()

        self._stop = threading.Event()
        self._writer = threading.Thread(target=self._writer_main, name="wvd-writer", daemon=True)
        self.on_fatal: Callable[[str], None] | None = None

    # -- connections ------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        """This thread's connection. Readers never share one, so none waits on another."""
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
            c.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            c.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = c
            with self._conns_lock:
                self._conns.append(c)
        return c

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        self._writer.start()

    @property
    def writer_alive(self) -> bool:
        return self._writer.is_alive()

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    def close(self) -> None:
        if self._writer.is_alive():
            self._stop.set()
            self._writer.join(timeout=10)
        else:
            self._write_batch(self._drain())  # never started (or died): write what is left here
        with self._conns_lock:
            for c in self._conns:
                try:
                    c.close()
                except sqlite3.Error:
                    pass
            self._conns.clear()
        if self._tmpdir:
            shutil.rmtree(self._tmpdir, ignore_errors=True)

    # -- writer thread ----------------------------------------------------
    def _writer_main(self) -> None:
        try:
            last_prune = time.monotonic()
            while not self._stop.wait(FLUSH_INTERVAL_S):
                self._write_batch(self._drain())
                if time.monotonic() - last_prune >= PRUNE_INTERVAL_S:
                    last_prune = time.monotonic()
                    self._prune(time.time())
            self._write_batch(self._drain())
        except BaseException as e:
            log.critical("writer thread died", exc_info=True)
            if self.on_fatal:
                self.on_fatal(f"writer thread died: {e!r}")

    def _drain(self) -> list[tuple]:
        items = []
        while True:
            try:
                items.append(self._queue.get_nowait())
            except queue.Empty:
                return items

    def _write_batch(self, items: list[tuple]) -> None:
        if not items:
            return
        samples = [i[1] for i in items if i[0] == "s"]
        events = [i[1] for i in items if i[0] == "e"]
        db = self._conn()
        try:
            db.execute("BEGIN")
            db.executemany("INSERT OR REPLACE INTO samples (seq, ts, device, frame, gap) VALUES (?,?,?,?,?)", samples)
            db.executemany("INSERT OR REPLACE INTO events (id, ts, type, data) VALUES (?,?,?,?)", events)
            db.execute("COMMIT")
        except sqlite3.Error:
            log.exception("DB write failed, %d samples and %d events lost", len(samples), len(events))
            self.db_dropped += len(samples)
            try:
                db.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            return
        if samples:
            self.flushed_seq = samples[-1][0]

    def _prune(self, now: float) -> None:
        """Delete expired rows in short transactions so readers and writes interleave."""
        cutoff = now - self.retention_s
        db = self._conn()
        try:
            for _ in range(PRUNE_MAX_CHUNKS):
                n = db.execute(
                    """DELETE FROM samples WHERE seq IN (
                         SELECT seq FROM samples WHERE ts < ? AND NOT EXISTS (
                           SELECT 1 FROM sessions s
                           WHERE samples.ts >= s.start_ts AND samples.ts <= COALESCE(s.end_ts, ?))
                         ORDER BY seq LIMIT ?)""",
                    (cutoff, now, PRUNE_CHUNK)).rowcount
                if n < PRUNE_CHUNK:
                    break
                # Let the sample backlog through between big chunks.
                self._write_batch(self._drain())
            db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        except sqlite3.Error:
            log.exception("prune failed")

    # -- samples (sampler thread) -----------------------------------------
    def next_seq(self) -> int:
        # Only the sampler thread assigns seqs.
        self.last_seq += 1
        return self.last_seq

    def add(self, sample: dict, frame: bytes) -> None:
        self._ring.append(sample)
        if self._queue.qsize() >= self._max_pending:
            self.db_dropped += 1  # writer stuck: keep sampling, lose the DB copy
            return
        self._queue.put(("s", (sample["seq"], sample["ts"], sample["device"], frame, sample.get("gap_s"))))

    # -- samples (readers) ------------------------------------------------
    def _ring_copy(self) -> list[dict]:
        # list(deque) runs entirely in C under the GIL, so it is a consistent
        # snapshot even while the sampler appends.
        return list(self._ring)

    def latest(self) -> dict | None:
        try:
            return self._ring[-1]
        except IndexError:
            return None

    def ring_covers(self, t_from: float) -> bool:
        try:
            return self._ring[0]["ts"] <= t_from
        except IndexError:
            return False

    @staticmethod
    def _ring_slice(ring: list[dict], t_from: float, t_to: float) -> list[dict]:
        lo = bisect.bisect_left(ring, t_from, key=lambda s: s["ts"])
        hi = bisect.bisect_right(ring, t_to, key=lambda s: s["ts"])
        return ring[lo:hi]

    def _seq_bounds(self, t_from: float, t_to: float) -> tuple[int, int] | None:
        db = self._conn()
        lo = db.execute("SELECT seq FROM samples WHERE ts >= ? ORDER BY ts LIMIT 1", (t_from,)).fetchone()
        hi = db.execute("SELECT seq FROM samples WHERE ts <= ? ORDER BY ts DESC LIMIT 1", (t_to,)).fetchone()
        if lo is None or hi is None or lo[0] > hi[0]:
            return None
        return lo[0], hi[0]

    def estimate(self, t_from: float, t_to: float) -> int:
        """Rough sample count of a window (seq span), for sizing work up front."""
        if self.ring_covers(t_from):
            return len(self._ring_slice(self._ring_copy(), t_from, t_to))
        b = self._seq_bounds(t_from, t_to)
        db_n = b[1] - b[0] + 1 if b else 0
        return db_n + max(0, self.last_seq - self.flushed_seq)

    def iter_range(self, t_from: float, t_to: float, chunk: int = CHUNK_SAMPLES) -> Iterator[list[dict]]:
        """Yield the window as lists of samples in seq order, chunk by chunk.

        Reads the DB by primary-key ranges, then appends what the writer has
        not committed yet from the ring."""
        ring = self._ring_copy()
        if ring and ring[0]["ts"] <= t_from:
            part = self._ring_slice(ring, t_from, t_to)
            for i in range(0, len(part), chunk):
                yield part[i:i + chunk]
            return
        last = 0
        bounds = self._seq_bounds(t_from, t_to)
        if bounds:
            cursor, hi = bounds[0] - 1, bounds[1]
            while cursor < hi:
                # Look the connection up per chunk: a streaming response may
                # resume this generator on a different worker thread.
                rows = self._conn().execute(
                    "SELECT seq, ts, device, frame, gap FROM samples WHERE seq > ? AND seq <= ? ORDER BY seq LIMIT ?",
                    (cursor, hi, chunk)).fetchall()
                if not rows:
                    break
                cursor = rows[-1][0]
                out = [build_sample(*r) for r in rows if t_from <= r[1] <= t_to]
                if out:
                    last = out[-1]["seq"]
                    yield out
        tail = [s for s in self._ring_slice(ring, t_from, t_to) if s["seq"] > last]
        for i in range(0, len(tail), chunk):
            yield tail[i:i + chunk]

    def query(self, t_from: float, t_to: float, limit: int | None = None) -> tuple[list[dict], bool]:
        """The window as one list, cut at limit. Returns (samples, truncated)."""
        limit = MAX_QUERY_SAMPLES if limit is None else limit
        out: list[dict] = []
        for part in self.iter_range(t_from, t_to):
            if len(out) + len(part) > limit:
                out.extend(part[:limit - len(out)])
                return out, True
            out.extend(part)
        return out, False

    def range(self, t_from: float, t_to: float) -> list[dict]:
        return self.query(t_from, t_to)[0]

    def after_seq(self, seq: int, limit: int = 10_000) -> list[dict]:
        ring = self._ring_copy()
        if ring and ring[0]["seq"] <= seq + 1:
            lo = bisect.bisect_right(ring, seq, key=lambda s: s["seq"])
            return ring[lo:lo + limit]
        rows = self._conn().execute(
            "SELECT seq, ts, device, frame, gap FROM samples WHERE seq > ? ORDER BY seq LIMIT ?",
            (seq, limit)).fetchall()
        out = [build_sample(*r) for r in rows]
        last = out[-1]["seq"] if out else seq
        lo = bisect.bisect_right(ring, last, key=lambda s: s["seq"])
        return out + ring[lo:lo + limit - len(out)]

    # -- events -----------------------------------------------------------
    def add_event(self, event: dict) -> dict:
        """Assign an id now and queue the insert: callable from the sampler."""
        data = {k: v for k, v in event.items() if k not in ("ts", "type")}
        with self._event_lock:
            self._last_event_id += 1
            eid = self._last_event_id
        self._queue.put(("e", (eid, event["ts"], event["type"], json.dumps(data))))
        return {"id": eid, **event}

    def events(self, t_from: float, t_to: float, type_prefix: str | None = None, limit: int = 1000) -> list[dict]:
        q = "SELECT id, ts, type, data FROM events WHERE ts BETWEEN ? AND ?"
        args: list = [t_from, t_to]
        if type_prefix:
            q += " AND type LIKE ?"
            args.append(type_prefix + "%")
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        rows = self._conn().execute(q, args).fetchall()
        return [{"id": i, "ts": ts, "type": t, **json.loads(d)} for i, ts, t, d in reversed(rows)]

    # -- sessions ---------------------------------------------------------
    _SESSION_COLS = "id, label, meta, start_ts, end_ts, stats"

    def start_session(self, label: str, meta: dict | None = None) -> dict:
        cur = self._conn().execute("INSERT INTO sessions (label, meta, start_ts) VALUES (?,?,?)",
                                   (label, json.dumps(meta or {}), time.time()))
        return self.session(cur.lastrowid)

    def stop_session(self, sid: int) -> dict | None:
        self._conn().execute("UPDATE sessions SET end_ts = ? WHERE id = ? AND end_ts IS NULL", (time.time(), sid))
        return self.session(sid)

    def set_session_stats(self, sid: int, stats: dict) -> None:
        self._conn().execute("UPDATE sessions SET stats = ? WHERE id = ?", (json.dumps(stats), sid))

    def session(self, sid: int) -> dict | None:
        r = self._conn().execute(f"SELECT {self._SESSION_COLS} FROM sessions WHERE id = ?", (sid,)).fetchone()
        return _session_row(r) if r else None

    def sessions(self, limit: int = 100) -> list[dict]:
        rows = self._conn().execute(
            f"SELECT {self._SESSION_COLS} FROM sessions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [_session_row(r) for r in rows]

    def delete_session(self, sid: int) -> bool:
        return self._conn().execute("DELETE FROM sessions WHERE id = ?", (sid,)).rowcount > 0


def _session_row(r) -> dict:
    sid, label, meta, start, end, stats = r
    return {"id": sid, "label": label, "meta": json.loads(meta), "start_ts": start, "end_ts": end,
            "active": end is None, "stats": json.loads(stats) if stats else None}
