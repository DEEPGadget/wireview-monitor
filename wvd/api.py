"""The api process: queries over the stored data, sessions, device commands.

Since 1.2 it listens on a private Unix socket behind the front process, which
handles auth, the live streams, health and the dashboard. Everything here
may be CPU-heavy (decoding windows, statistics, exports); running it in its
own process keeps that from ever delaying sampling or the live streams.

The ring (last ring_s seconds) is filled from the recorder's feed, so queries
include samples the recorder has not committed to the DB yet.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from . import __version__, bus
from . import protocol as P
from .events import Limits
from .samples import STAT_FIELDS, StatsAccumulator, compute_stats, downsample, flatten
from .store import Store

log = logging.getLogger("wvd")

MAX_HISTORY_POINTS = 20_000
# Serialize sample lists this many at a time: one json.dumps (or csv
# writerows) call holds the GIL throughout, and 20k samples take ~160 ms.
ENCODE_CHUNK = 500
# Queries that decode DB rows or compute statistics run at most this many at
# a time; the rest wait, then get 503. Bounds the CPU and memory requests can
# take however many arrive at once (sampling is in another process since 1.2).
HEAVY_CONCURRENCY = 2
HEAVY_WAIT_S = 30
EXPORT_CHUNK_WAIT_S = 120
ACTIVE_SESSION_STATS_TTL_S = 30


@dataclass
class Config:
    host: str = "0.0.0.0"
    port: int = 8765
    token: str | None = None
    allow_write: bool = False
    rate_hz: float = 10.0
    retention_s: float = 72 * 3600
    ring_s: float = 900
    db_path: str | None = None
    device_port: str | None = None
    simulate: str | None = None
    limits: Limits = field(default_factory=Limits)
    test_fault: str | None = None


# -- parameter parsing ------------------------------------------------------
_DUR = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)?\s*$")
_DUR_UNITS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, None: 1}


def parse_duration(text: str) -> float:
    m = _DUR.match(text)
    if not m:
        raise HTTPException(400, f"bad duration {text!r} (use e.g. 500ms, 30s, 5m, 1h, 7d)")
    return float(m.group(1)) * _DUR_UNITS[m.group(2)]


def parse_time(text: str) -> float:
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        raise HTTPException(400, f"bad time {text!r} (epoch seconds or ISO 8601)")


def window(last: str | None, t_from: str | None, t_to: str | None, default_last: float = 60) -> tuple[float, float]:
    now = time.time()
    to = parse_time(t_to) if t_to else now
    if t_from:
        return parse_time(t_from), to
    return to - (parse_duration(last) if last else default_last), to


class SessionIn(BaseModel):
    label: str
    meta: dict = {}


class ClearFaultsIn(BaseModel):
    fault: str | None = None  # clear just this fault (status and log)
    keep_status_mask: int = 0
    keep_log_mask: int = 0


def create_app(cfg: Config, runtime_dir: str | None = None) -> FastAPI:
    """runtime_dir: where the recorder's sockets are. None runs on the DB alone
    (no live tail, no device commands), which the unit tests use."""
    store = Store(cfg.db_path, ring_size=max(100, int(cfg.ring_s * cfg.rate_hz)), retention_s=cfg.retention_s,
                  rate_hz=cfg.rate_hz, writer=False)
    heavy_sem = threading.BoundedSemaphore(HEAVY_CONCURRENCY)
    session_stats_cache: dict[int, tuple[float, dict]] = {}

    def on_feed(line: bytes) -> None:
        # Only samples matter here; skip the JSON parse for everything else.
        if line.startswith(b'{"kind":"sample"'):
            store.add(json.loads(line)["data"])

    feed = bus.FeedClient(os.path.join(runtime_dir, bus.FEED_SOCKET), on_feed, name="wvd-api-feed") \
        if runtime_dir else None

    def control(request: dict) -> dict:
        if runtime_dir is None:
            raise HTTPException(503, "no recorder")
        try:
            reply = bus.control_call(os.path.join(runtime_dir, bus.CONTROL_SOCKET), request)
        except ConnectionError as e:
            raise HTTPException(503, f"recorder unavailable: {e}")
        if not reply.get("ok"):
            raise HTTPException(503, reply.get("error", "recorder error"))
        return reply

    def publish(msg: dict) -> None:
        try:
            control({"op": "publish", "msg": msg})
        except HTTPException:
            log.warning("could not publish %s change: recorder unavailable", msg["kind"])

    @contextmanager
    def heavy(wait_s: float | None = None):
        if not heavy_sem.acquire(timeout=HEAVY_WAIT_S if wait_s is None else wait_s):
            raise HTTPException(503, f"busy: {HEAVY_CONCURRENCY} heavy queries already running, retry later",
                                headers={"Retry-After": "5"})
        try:
            yield
        finally:
            heavy_sem.release()

    def window_stats(t0: float, t1: float) -> dict:
        acc = StatsAccumulator(store.estimate(t0, t1))
        for part in store.iter_range(t0, t1):
            acc.add(part)
        return acc.result()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if feed:
            feed.start()
        yield
        if feed:
            feed.stop()
        store.close()

    app = FastAPI(title="wvd", version=__version__, lifespan=lifespan,
                  description="WireView Pro II measurement daemon")
    app.state.store, app.state.cfg, app.state.heavy_sem = store, cfg, heavy_sem

    @app.get("/api/v1/_alive", include_in_schema=False)
    async def alive():
        # Async on purpose: answers from the event loop even while worker
        # threads are busy, so the front can tell "slow" from "gone".
        return {"ok": True, "pid": os.getpid(), "feed": bool(feed and feed.connected)}

    # -- sensors ----------------------------------------------------------
    @app.get("/api/v1/sensors/history")
    def history(last: str | None = None, from_: str | None = Query(None, alias="from"), to: str | None = None,
                after_seq: int | None = None, step: str | None = None):
        truncated = False
        if after_seq is not None:
            samples = store.after_seq(after_seq)
        else:
            t0, t1 = window(last, from_, to)
            step_s = parse_duration(step) if step else None
            with heavy():
                samples, truncated = store.query(t0, t1)
                if step_s:
                    samples = downsample(samples, step_s)
                elif len(samples) > MAX_HISTORY_POINTS:
                    span = samples[-1]["ts"] - samples[0]["ts"]
                    samples = downsample(samples, span / MAX_HISTORY_POINTS)
        return samples_response(samples, truncated)

    def samples_response(samples: list[dict], truncated: bool = False) -> Response:
        """{"count", "samples"[, "truncated"]} encoded in small slices, bypassing
        FastAPI's jsonable_encoder (pure Python, ~1 s per 20k samples)."""
        parts = [f'{{"count":{len(samples)},' + ('"truncated":true,' if truncated else "") + '"samples":[',
                 ",".join(json.dumps(samples[i:i + ENCODE_CHUNK], separators=(",", ":"))[1:-1]
                          for i in range(0, len(samples), ENCODE_CHUNK)),
                 "]}"]
        return Response("".join(parts), media_type="application/json")

    def stats_window(last, from_, to, session) -> tuple[float, float, dict | None]:
        if session is not None:
            s = store.session(session)
            if s is None:
                raise HTTPException(404, f"no session {session}")
            return s["start_ts"], s["end_ts"] or time.time(), s
        t0, t1 = window(last, from_, to)
        return t0, t1, None

    @app.get("/api/v1/sensors/stats")
    def stats(last: str | None = None, from_: str | None = Query(None, alias="from"), to: str | None = None,
              session: int | None = None):
        t0, t1, sess = stats_window(last, from_, to, session)
        with heavy():
            result = window_stats(t0, t1)
        result["window"] = {"from": t0, "to": t1}
        if sess:
            result["session"] = sess
        return result

    # -- events -----------------------------------------------------------
    @app.get("/api/v1/events")
    def events(last: str | None = None, from_: str | None = Query(None, alias="from"), to: str | None = None,
               type: str | None = None, limit: int = 1000):
        t0, t1 = window(last, from_, to, default_last=24 * 3600)
        return {"events": store.events(t0, t1, type, limit)}

    # -- sessions ---------------------------------------------------------
    @app.get("/api/v1/sessions")
    def sessions(limit: int = 100):
        return {"sessions": store.sessions(limit)}

    @app.post("/api/v1/sessions", status_code=201)
    def start_session(body: SessionIn):
        s = store.start_session(body.label, body.meta)
        publish({"kind": "session", "data": s})
        return s

    def session_stats(s: dict) -> dict:
        """A finished session's stats are computed once and stored; a running
        one's are cached for a while unless the ring holds the whole session."""
        if s["stats"] is not None:
            return s["stats"]
        if s["end_ts"] is not None:
            with heavy():
                st = window_stats(s["start_ts"], s["end_ts"])
            store.set_session_stats(s["id"], st)
            session_stats_cache.pop(s["id"], None)
            return st
        if store.ring_covers(s["start_ts"]):
            with heavy():
                return compute_stats(store.range(s["start_ts"], time.time()))
        hit = session_stats_cache.get(s["id"])
        if hit and time.monotonic() - hit[0] < ACTIVE_SESSION_STATS_TTL_S:
            return hit[1]
        with heavy():
            st = window_stats(s["start_ts"], time.time())
        session_stats_cache[s["id"]] = (time.monotonic(), st)
        return st

    @app.post("/api/v1/sessions/{sid}/stop")
    def stop_session(sid: int):
        if store.session(sid) is None:
            raise HTTPException(404, f"no session {sid}")
        s = store.stop_session(sid)
        publish({"kind": "session", "data": s})
        return {**s, "stats": session_stats(s)}

    @app.get("/api/v1/sessions/{sid}")
    def get_session(sid: int):
        s = store.session(sid)
        if s is None:
            raise HTTPException(404, f"no session {sid}")
        return {**s, "stats": session_stats(s)}

    @app.delete("/api/v1/sessions/{sid}")
    def delete_session(sid: int):
        if not store.delete_session(sid):
            raise HTTPException(404, f"no session {sid}")
        session_stats_cache.pop(sid, None)
        return {"deleted": sid}

    def export_response(t0: float, t1: float, fmt: str, name: str):
        """Stream the window chunk by chunk: constant memory, no row cap. Each
        chunk takes a heavy-query slot, so a long export shares the CPU with
        other requests instead of holding it. If no slot frees up in time the
        file ends with an explicit error line rather than silently short."""
        if fmt not in ("csv", "jsonl"):
            raise HTTPException(400, "format must be csv or jsonl")

        def chunks():
            parts = store.iter_range(t0, t1)
            header = fmt != "csv"
            while True:
                try:
                    with heavy(EXPORT_CHUNK_WAIT_S):
                        part = next(parts, None)
                        if part is None:
                            if not header:  # empty window: header only
                                yield ",".join(["ts", "seq"] + STAT_FIELDS) + "\n"
                            return
                        buf = io.StringIO()
                        if fmt == "jsonl":
                            for s in part:
                                buf.write(json.dumps(s, separators=(",", ":")) + "\n")
                        else:
                            rows = [flatten(s) for s in part]
                            w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
                            if not header:
                                w.writeheader()
                                header = True
                            for i in range(0, len(rows), ENCODE_CHUNK):
                                w.writerows(rows[i:i + ENCODE_CHUNK])
                    yield buf.getvalue()
                except HTTPException:
                    log.warning("export %s cut short: server busy", name)
                    msg = "export incomplete: server busy, retry with a shorter window"
                    yield (f"# {msg}\n" if fmt == "csv" else json.dumps({"error": msg}) + "\n")
                    return

        media = "text/csv" if fmt == "csv" else "application/x-ndjson"
        return StreamingResponse(chunks(), media_type=media,
                                 headers={"Content-Disposition": f'attachment; filename="{name}.{fmt}"'})

    @app.get("/api/v1/sessions/{sid}/export")
    def export_session(sid: int, format: str = "csv"):
        s = store.session(sid)
        if s is None:
            raise HTTPException(404, f"no session {sid}")
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", s["label"])[:60] or "session"
        return export_response(s["start_ts"], s["end_ts"] or time.time(), format, f"wvd-session-{sid}-{safe}")

    @app.get("/api/v1/export")
    def export(last: str | None = None, from_: str | None = Query(None, alias="from"), to: str | None = None,
               format: str = "csv"):
        t0, t1 = window(last, from_, to)
        return export_response(t0, t1, format, f"wvd-{int(t0)}-{int(t1)}")

    # -- device commands (opt-in) ----------------------------------------
    @app.post("/api/v1/device/clear-faults")
    def clear_faults(body: ClearFaultsIn):
        if not cfg.allow_write:
            raise HTTPException(403, "write commands are disabled (start wvd with --allow-write)")
        keep_s, keep_l = body.keep_status_mask, body.keep_log_mask
        if body.fault:
            if body.fault not in P.FAULTS:
                raise HTTPException(400, f"unknown fault {body.fault!r}, one of {P.FAULTS}")
            keep_s = keep_l = 0xFFFF & ~(1 << P.FAULTS.index(body.fault))
        r = control({"op": "clear_faults", "keep_status_mask": keep_s, "keep_log_mask": keep_l})
        return {"ok": True, "keep_status_mask": r["keep_status_mask"], "keep_log_mask": r["keep_log_mask"]}

    return app
