"""Sample, event and session storage.

Recent samples live in a memory ring for live views. Every raw 100-byte frame
is also written to SQLite (batched) so any window can be re-decoded later;
frames older than the retention are pruned unless a session covers them.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections import deque

from .samples import build_sample

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    seq    INTEGER PRIMARY KEY,
    ts     REAL NOT NULL,
    device TEXT NOT NULL,
    frame  BLOB NOT NULL
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
    end_ts   REAL
);
"""

MAX_QUERY_SAMPLES = 500_000


def default_db_path() -> str:
    base = os.environ.get("STATE_DIRECTORY") or os.path.join(
        os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "wvd")
    return os.path.join(base, "wvd.db")


class Store:
    def __init__(self, db_path: str | None, ring_size: int, retention_s: float):
        self.retention_s = retention_s
        self._ring: deque[dict] = deque(maxlen=ring_size)
        self._pending: list[tuple] = []
        self._lock = threading.RLock()
        if db_path and db_path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._db = sqlite3.connect(db_path or ":memory:", check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)
        row = self._db.execute("SELECT MAX(seq) FROM samples").fetchone()
        self.last_seq = row[0] or 0
        self._last_prune = 0.0

    # -- samples ----------------------------------------------------------
    def next_seq(self) -> int:
        with self._lock:
            self.last_seq += 1
            return self.last_seq

    def add(self, sample: dict, frame: bytes) -> None:
        with self._lock:
            self._ring.append(sample)
            self._pending.append((sample["seq"], sample["ts"], sample["device"], frame))

    def flush(self) -> None:
        with self._lock:
            rows, self._pending = self._pending, []
            if rows:
                self._db.execute("BEGIN")
                self._db.executemany("INSERT OR REPLACE INTO samples VALUES (?,?,?,?)", rows)
                self._db.execute("COMMIT")
            now = time.time()
            if now - self._last_prune > 60:
                self._last_prune = now
                self._prune(now)

    def _prune(self, now: float) -> None:
        cutoff = now - self.retention_s
        self._db.execute(
            """DELETE FROM samples WHERE ts < ? AND NOT EXISTS (
                 SELECT 1 FROM sessions s
                 WHERE samples.ts >= s.start_ts AND samples.ts <= COALESCE(s.end_ts, ?))""",
            (cutoff, now))
        self._db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))

    def latest(self) -> dict | None:
        with self._lock:
            return self._ring[-1] if self._ring else None

    def range(self, t_from: float, t_to: float) -> list[dict]:
        with self._lock:
            if self._ring and self._ring[0]["ts"] <= t_from:
                return [s for s in self._ring if t_from <= s["ts"] <= t_to]
            self.flush()
            rows = self._db.execute(
                "SELECT seq, ts, device, frame FROM samples WHERE ts BETWEEN ? AND ? ORDER BY seq LIMIT ?",
                (t_from, t_to, MAX_QUERY_SAMPLES)).fetchall()
        return [build_sample(*r) for r in rows]

    def after_seq(self, seq: int, limit: int = 10_000) -> list[dict]:
        with self._lock:
            if self._ring and self._ring[0]["seq"] <= seq + 1:
                return [s for s in self._ring if s["seq"] > seq][:limit]
            self.flush()
            rows = self._db.execute(
                "SELECT seq, ts, device, frame FROM samples WHERE seq > ? ORDER BY seq LIMIT ?",
                (seq, limit)).fetchall()
        return [build_sample(*r) for r in rows]

    # -- events -----------------------------------------------------------
    def add_event(self, event: dict) -> dict:
        data = {k: v for k, v in event.items() if k not in ("ts", "type")}
        with self._lock:
            cur = self._db.execute("INSERT INTO events (ts, type, data) VALUES (?,?,?)",
                                   (event["ts"], event["type"], json.dumps(data)))
            return {"id": cur.lastrowid, **event}

    def events(self, t_from: float, t_to: float, type_prefix: str | None = None, limit: int = 1000) -> list[dict]:
        q = "SELECT id, ts, type, data FROM events WHERE ts BETWEEN ? AND ?"
        args: list = [t_from, t_to]
        if type_prefix:
            q += " AND type LIKE ?"
            args.append(type_prefix + "%")
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(q, args).fetchall()
        return [{"id": i, "ts": ts, "type": t, **json.loads(d)} for i, ts, t, d in reversed(rows)]

    # -- sessions ---------------------------------------------------------
    def start_session(self, label: str, meta: dict | None = None) -> dict:
        with self._lock:
            ts = time.time()
            cur = self._db.execute("INSERT INTO sessions (label, meta, start_ts) VALUES (?,?,?)",
                                   (label, json.dumps(meta or {}), ts))
            return self.session(cur.lastrowid)

    def stop_session(self, sid: int) -> dict | None:
        with self._lock:
            self._db.execute("UPDATE sessions SET end_ts = ? WHERE id = ? AND end_ts IS NULL", (time.time(), sid))
            return self.session(sid)

    def session(self, sid: int) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT id, label, meta, start_ts, end_ts FROM sessions WHERE id = ?",
                                 (sid,)).fetchone()
        return _session_row(r) if r else None

    def sessions(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, label, meta, start_ts, end_ts FROM sessions ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
        return [_session_row(r) for r in rows]

    def delete_session(self, sid: int) -> bool:
        with self._lock:
            return self._db.execute("DELETE FROM sessions WHERE id = ?", (sid,)).rowcount > 0

    def close(self) -> None:
        with self._lock:
            self.flush()
            self._db.close()


def _session_row(r) -> dict:
    sid, label, meta, start, end = r
    return {"id": sid, "label": label, "meta": json.loads(meta), "start_ts": start, "end_ts": end,
            "active": end is None}
