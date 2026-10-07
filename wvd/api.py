"""HTTP API: REST, WebSocket/SSE streams, Prometheus metrics, dashboard."""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import re
import secrets
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__
from . import protocol as P
from .events import EventEngine, Limits
from .sampler import Sampler
from .samples import STAT_FIELDS, StatsAccumulator, compute_stats, downsample, flatten
from .store import Store

log = logging.getLogger("wvd")

STATIC_DIR = Path(__file__).parent / "static"
MAX_HISTORY_POINTS = 20_000
# Serialize sample lists this many at a time: one json.dumps (or csv
# writerows) call holds the GIL throughout, and 20k samples take ~160 ms.
ENCODE_CHUNK = 500
# A slow subscriber is at most this far behind before its backlog is dropped
# (and it is told so with a "lag" message).
SUBSCRIBER_QUEUE_S = 5
# Queries that decode DB rows or compute statistics run at most this many at
# a time; the rest wait, then get 503. Bounds how much CPU (and GIL) requests
# can take from the sampler however many arrive at once.
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
    # Called (from the dying thread) when the sampler or the writer thread ends.
    on_fatal: Callable[[str], None] | None = None


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


# -- pub/sub ----------------------------------------------------------------
class Hub:
    """Fans messages from the sampler thread out to asyncio subscribers."""

    def __init__(self, maxsize: int = 250):
        self.loop: asyncio.AbstractEventLoop | None = None
        self.subscribers: set[asyncio.Queue] = set()
        self.maxsize = maxsize
        self.dropped = 0

    def publish(self, msg: dict) -> None:
        if self.loop is not None and self.subscribers:
            self.loop.call_soon_threadsafe(self._fanout, msg)

    def _fanout(self, msg: dict) -> None:
        for q in self.subscribers:
            if q.full():
                # Slow consumer: drop its whole backlog rather than feed it stale
                # data, and say so. The lag message carries what was skipped.
                n = q.qsize()
                while not q.empty():
                    q.get_nowait()
                self.dropped += n
                q.put_nowait({"kind": "lag", "data": {"ts": time.time(), "dropped": n}})
            q.put_nowait(msg)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self.maxsize)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)


class SessionIn(BaseModel):
    label: str
    meta: dict = {}


class ClearFaultsIn(BaseModel):
    fault: str | None = None  # clear just this fault (status and log)
    keep_status_mask: int = 0
    keep_log_mask: int = 0


def create_app(cfg: Config) -> FastAPI:
    hub = Hub(maxsize=max(50, int(SUBSCRIBER_QUEUE_S * cfg.rate_hz)))
    store = Store(cfg.db_path, ring_size=max(100, int(cfg.ring_s * cfg.rate_hz)), retention_s=cfg.retention_s,
                  rate_hz=cfg.rate_hz)
    engine = EventEngine(cfg.limits)
    fatal: list[str] = []

    def on_fatal(reason: str) -> None:
        fatal.append(reason)
        if cfg.on_fatal:
            cfg.on_fatal(reason)

    store.on_fatal = on_fatal
    sampler = Sampler(store, engine, hub.publish, cfg.rate_hz, cfg.device_port, cfg.simulate,
                      test_fault=cfg.test_fault, on_fatal=on_fatal)
    heavy_sem = threading.BoundedSemaphore(HEAVY_CONCURRENCY)
    session_stats_cache: dict[int, tuple[float, dict]] = {}

    @contextmanager
    def heavy(wait_s: float = HEAVY_WAIT_S):
        if not heavy_sem.acquire(timeout=wait_s):
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
        hub.loop = asyncio.get_running_loop()
        store.start()
        sampler.start()
        yield
        await asyncio.get_running_loop().run_in_executor(None, sampler.stop)
        store.close()

    app = FastAPI(title="wvd", version=__version__, lifespan=lifespan,
                  description="WireView Pro II measurement daemon")
    app.state.sampler, app.state.store, app.state.hub, app.state.cfg = sampler, store, hub, cfg
    app.state.fatal = fatal
    app.state.heavy_sem = heavy_sem

    def check_token(supplied: str | None) -> bool:
        return cfg.token is None or (supplied is not None and secrets.compare_digest(supplied, cfg.token))

    def auth(request: Request) -> None:
        h = request.headers.get("authorization", "")
        supplied = h[7:] if h.lower().startswith("bearer ") else request.query_params.get("token")
        if not check_token(supplied):
            raise HTTPException(401, "missing or bad token", headers={"WWW-Authenticate": "Bearer"})

    api = Depends(auth)

    def info_dict() -> dict | None:
        return sampler.info.as_dict() if sampler.info and sampler.connected else None

    # -- status -----------------------------------------------------------
    @app.get("/api/v1/health")
    def health():
        latest = store.latest()
        age = round(time.time() - latest["ts"], 3) if latest else None
        threads_ok = sampler.alive and store.writer_alive and not fatal
        ok = threads_ok and sampler.connected and age is not None and age < max(3.0, 5 * sampler.period)
        return {"status": "ok" if ok else ("degraded" if threads_ok else "down"),
                "connected": sampler.connected, "version": __version__,
                "simulated": bool(cfg.simulate), "rate_hz": cfg.rate_hz, "measured_hz": sampler.measured_hz,
                "last_sample_age_s": age, "last_sample_wall": sampler.last_sample_wall,
                "last_seq": store.last_seq, "counters": sampler.counters,
                "sampler_alive": sampler.alive, "writer_alive": store.writer_alive, "fatal": fatal[0] if fatal else None,
                "gaps_total": sampler.gaps_total, "max_gap_s_5m": round(sampler.max_gap_s(), 4),
                "last_gap": sampler.last_gap,
                "db_queue": store.pending, "db_dropped": store.db_dropped, "stream_dropped": hub.dropped,
                "last_error": sampler.last_error, "uptime_s": round(time.time() - sampler.started_at, 1),
                "auth": cfg.token is not None, "write_enabled": cfg.allow_write}

    @app.get("/api/v1/info", dependencies=[api])
    def info():
        d = info_dict()
        if d is None:
            raise HTTPException(503, "device not connected")
        return d

    @app.get("/api/v1/limits", dependencies=[api])
    def limits():
        return {**cfg.limits.as_dict(), "imbalance_min_load_a": P.IMBALANCE_MIN_LOAD_A,
                "faults": [{"bit": i, "name": n, "label": P.FAULT_LABELS[n]} for i, n in enumerate(P.FAULTS)]}

    # -- sensors ----------------------------------------------------------
    @app.get("/api/v1/sensors/latest", dependencies=[api])
    def latest():
        s = store.latest()
        if s is None:
            raise HTTPException(503, "no samples yet")
        return {**s, "age_s": round(time.time() - s["ts"], 3)}

    @app.get("/api/v1/sensors/history", dependencies=[api])
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

    @app.get("/api/v1/sensors/stats", dependencies=[api])
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
    @app.get("/api/v1/events", dependencies=[api])
    def events(last: str | None = None, from_: str | None = Query(None, alias="from"), to: str | None = None,
               type: str | None = None, limit: int = 1000):
        t0, t1 = window(last, from_, to, default_last=24 * 3600)
        return {"events": store.events(t0, t1, type, limit)}

    # -- sessions ---------------------------------------------------------
    @app.get("/api/v1/sessions", dependencies=[api])
    def sessions(limit: int = 100):
        return {"sessions": store.sessions(limit)}

    @app.post("/api/v1/sessions", dependencies=[api], status_code=201)
    def start_session(body: SessionIn):
        s = store.start_session(body.label, body.meta)
        hub.publish({"kind": "session", "data": s})
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

    @app.post("/api/v1/sessions/{sid}/stop", dependencies=[api])
    def stop_session(sid: int):
        if store.session(sid) is None:
            raise HTTPException(404, f"no session {sid}")
        s = store.stop_session(sid)
        hub.publish({"kind": "session", "data": s})
        return {**s, "stats": session_stats(s)}

    @app.get("/api/v1/sessions/{sid}", dependencies=[api])
    def get_session(sid: int):
        s = store.session(sid)
        if s is None:
            raise HTTPException(404, f"no session {sid}")
        return {**s, "stats": session_stats(s)}

    @app.delete("/api/v1/sessions/{sid}", dependencies=[api])
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

    @app.get("/api/v1/sessions/{sid}/export", dependencies=[api])
    def export_session(sid: int, format: str = "csv"):
        s = store.session(sid)
        if s is None:
            raise HTTPException(404, f"no session {sid}")
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", s["label"])[:60] or "session"
        return export_response(s["start_ts"], s["end_ts"] or time.time(), format, f"wvd-session-{sid}-{safe}")

    @app.get("/api/v1/export", dependencies=[api])
    def export(last: str | None = None, from_: str | None = Query(None, alias="from"), to: str | None = None,
               format: str = "csv"):
        t0, t1 = window(last, from_, to)
        return export_response(t0, t1, format, f"wvd-{int(t0)}-{int(t1)}")

    # -- device commands (opt-in) ----------------------------------------
    @app.post("/api/v1/device/clear-faults", dependencies=[api])
    def clear_faults(body: ClearFaultsIn):
        if not cfg.allow_write:
            raise HTTPException(403, "write commands are disabled (start wvd with --allow-write)")
        keep_s, keep_l = body.keep_status_mask, body.keep_log_mask
        if body.fault:
            if body.fault not in P.FAULTS:
                raise HTTPException(400, f"unknown fault {body.fault!r}, one of {P.FAULTS}")
            keep_s = keep_l = 0xFFFF & ~(1 << P.FAULTS.index(body.fault))
        try:
            sampler.run_command(lambda d: d.clear_faults(keep_s, keep_l))
        except Exception as e:
            raise HTTPException(503, f"clear-faults failed: {e}")
        ev = store.add_event({"ts": time.time(), "type": "command.clear_faults",
                              "keep_status_mask": keep_s, "keep_log_mask": keep_l})
        hub.publish({"kind": "event", "data": ev})
        return {"ok": True, "keep_status_mask": keep_s, "keep_log_mask": keep_l}

    # -- streams ----------------------------------------------------------
    def hello() -> dict:
        return {"kind": "hello", "data": {"info": info_dict(), "limits": cfg.limits.as_dict(),
                                          "last_seq": store.last_seq, "rate_hz": cfg.rate_hz,
                                          "connected": sampler.connected}}

    def throttle(hz: float | None):
        min_dt = 1.0 / hz if hz and hz > 0 else 0.0
        last = [0.0]

        def keep(msg: dict) -> bool:
            if msg["kind"] != "sample" or min_dt == 0:
                return True
            if msg["data"]["ts"] - last[0] >= min_dt * 0.98:
                last[0] = msg["data"]["ts"]
                return True
            return False
        return keep

    @app.websocket("/api/v1/stream")
    async def stream_ws(ws: WebSocket, hz: float | None = None, token: str | None = None):
        auth_header = ws.headers.get("authorization", "")
        supplied = auth_header[7:] if auth_header.lower().startswith("bearer ") else token
        if not check_token(supplied):
            await ws.close(code=4401)
            return
        await ws.accept()
        q = hub.subscribe()
        keep = throttle(hz)
        try:
            await ws.send_json(hello())
            while True:
                msg = await q.get()
                if keep(msg):
                    await ws.send_json(msg)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            hub.unsubscribe(q)

    @app.get("/api/v1/stream/sse", dependencies=[api])
    async def stream_sse(request: Request, hz: float | None = None):
        q = hub.subscribe()
        keep = throttle(hz)

        async def gen():
            try:
                h = hello()
                yield f"event: hello\ndata: {json.dumps(h['data'])}\n\n"
                checked = time.monotonic()
                while True:
                    if time.monotonic() - checked >= 1.0:  # not per message: 50 Hz x clients adds up
                        if await request.is_disconnected():
                            break
                        checked = time.monotonic()
                    try:
                        msg = await asyncio.wait_for(q.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    if keep(msg):
                        yield f"event: {msg['kind']}\ndata: {json.dumps(msg['data'], separators=(',', ':'))}\n\n"
            finally:
                hub.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # -- prometheus -------------------------------------------------------
    @app.get("/metrics", dependencies=[api], response_class=PlainTextResponse)
    def metrics():
        lines = []

        def m(name: str, help_: str, kind: str, values: list[tuple[str, float]]):
            lines.append(f"# HELP {name} {help_}")
            lines.append(f"# TYPE {name} {kind}")
            for labels, v in values:
                lines.append(f"{name}{labels} {v}")

        m("wvd_up", "1 when the device is connected", "gauge", [("", int(sampler.connected))])
        m("wvd_samples_total", "sensor reads by result", "counter",
          [(f'{{result="{k}"}}', sampler.counters[k]) for k in ("ok", "corrupt", "failed")])
        m("wvd_sampler_alive", "1 while the sampler and writer threads run", "gauge",
          [("", int(sampler.alive and store.writer_alive))])
        m("wvd_gaps_total", "sampling pauses longer than two periods", "counter", [("", sampler.gaps_total)])
        m("wvd_max_gap_seconds", "longest sampling pause in the last 5 minutes", "gauge",
          [("", round(sampler.max_gap_s(), 4))])
        m("wvd_db_dropped_total", "samples not written to the DB (writer backlog full or write error)", "counter",
          [("", store.db_dropped)])
        m("wvd_stream_dropped_total", "stream messages dropped for slow subscribers", "counter",
          [("", hub.dropped)])
        s = store.latest()
        if s is not None and sampler.connected:
            m("wvd_power_watts", "total power", "gauge", [("", s["total_w"])])
            m("wvd_current_amps", "total current", "gauge", [("", s["total_a"])])
            m("wvd_voltage_volts", "average voltage", "gauge", [("", s["avg_v"])])
            m("wvd_pin_current_amps", "per-pin current", "gauge",
              [(f'{{pin="{i}"}}', p["a"]) for i, p in enumerate(s["pins"], 1)])
            m("wvd_pin_voltage_volts", "per-pin voltage", "gauge",
              [(f'{{pin="{i}"}}', p["v"]) for i, p in enumerate(s["pins"], 1)])
            m("wvd_temperature_celsius", "temperature sensors", "gauge",
              [(f'{{sensor="{k}"}}', v) for k, v in s["temps_c"].items() if v is not None])
            if s["derived"]["pin_imbalance"] is not None:
                m("wvd_pin_imbalance_ratio", "max pin current / mean pin current", "gauge",
                  [("", s["derived"]["pin_imbalance"])])
            m("wvd_fan_duty_percent", "fan duty", "gauge", [("", s["fan_pct"])])
            m("wvd_fault_active", "active fault bits", "gauge",
              [(f'{{fault="{n}"}}', int(bool(s["fault_status"] & (1 << b)))) for b, n in enumerate(P.FAULTS)])
            m("wvd_fault_logged", "latched fault log bits", "gauge",
              [(f'{{fault="{n}"}}', int(bool(s["fault_log"] & (1 << b)))) for b, n in enumerate(P.FAULTS)])
        return "\n".join(lines) + "\n"

    @app.exception_handler(TimeoutError)
    async def _timeout(_, exc):
        return JSONResponse({"detail": "device command timed out"}, status_code=504)

    if STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="dashboard")
    return app
