"""HTTP API: REST, WebSocket/SSE streams, Prometheus metrics, dashboard."""
from __future__ import annotations

import asyncio
import csv
import io
import json
import re
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__
from . import protocol as P
from .events import EventEngine, Limits
from .sampler import Sampler
from .samples import STAT_FIELDS, compute_stats, downsample, flatten
from .store import Store

STATIC_DIR = Path(__file__).parent / "static"
MAX_HISTORY_POINTS = 20_000
SUBSCRIBER_QUEUE = 2_000


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

    def __init__(self):
        self.loop: asyncio.AbstractEventLoop | None = None
        self.subscribers: set[asyncio.Queue] = set()

    def publish(self, msg: dict) -> None:
        if self.loop is not None and self.subscribers:
            self.loop.call_soon_threadsafe(self._fanout, msg)

    def _fanout(self, msg: dict) -> None:
        for q in self.subscribers:
            if q.full():  # slow consumer: drop its oldest message, keep the stream live
                q.get_nowait()
            q.put_nowait(msg)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE)
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
    hub = Hub()
    store = Store(cfg.db_path, ring_size=max(100, int(cfg.ring_s * cfg.rate_hz)), retention_s=cfg.retention_s)
    engine = EventEngine(cfg.limits)
    sampler = Sampler(store, engine, hub.publish, cfg.rate_hz, cfg.device_port, cfg.simulate)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        hub.loop = asyncio.get_running_loop()
        sampler.start()
        yield
        await asyncio.get_running_loop().run_in_executor(None, sampler.stop)
        store.close()

    app = FastAPI(title="wvd", version=__version__, lifespan=lifespan,
                  description="WireView Pro II measurement daemon")
    app.state.sampler, app.state.store, app.state.hub, app.state.cfg = sampler, store, hub, cfg

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
        ok = sampler.connected and age is not None and age < max(3.0, 5 * sampler.period)
        return {"status": "ok" if ok else "degraded", "connected": sampler.connected, "version": __version__,
                "simulated": bool(cfg.simulate), "rate_hz": cfg.rate_hz, "measured_hz": sampler.measured_hz,
                "last_sample_age_s": age, "last_seq": store.last_seq, "counters": sampler.counters,
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
        return s

    @app.get("/api/v1/sensors/history", dependencies=[api])
    def history(last: str | None = None, from_: str | None = Query(None, alias="from"), to: str | None = None,
                after_seq: int | None = None, step: str | None = None):
        if after_seq is not None:
            samples = store.after_seq(after_seq)
        else:
            samples = store.range(*window(last, from_, to))
        if step:
            samples = downsample(samples, parse_duration(step))
        elif len(samples) > MAX_HISTORY_POINTS:
            span = samples[-1]["ts"] - samples[0]["ts"]
            samples = downsample(samples, span / MAX_HISTORY_POINTS)
        return {"count": len(samples), "samples": samples}

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
        result = compute_stats(store.range(t0, t1))
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

    @app.post("/api/v1/sessions/{sid}/stop", dependencies=[api])
    def stop_session(sid: int):
        if store.session(sid) is None:
            raise HTTPException(404, f"no session {sid}")
        s = store.stop_session(sid)
        result = compute_stats(store.range(s["start_ts"], s["end_ts"]))
        hub.publish({"kind": "session", "data": s})
        return {**s, "stats": result}

    @app.get("/api/v1/sessions/{sid}", dependencies=[api])
    def get_session(sid: int):
        s = store.session(sid)
        if s is None:
            raise HTTPException(404, f"no session {sid}")
        return {**s, "stats": compute_stats(store.range(s["start_ts"], s["end_ts"] or time.time()))}

    @app.delete("/api/v1/sessions/{sid}", dependencies=[api])
    def delete_session(sid: int):
        if not store.delete_session(sid):
            raise HTTPException(404, f"no session {sid}")
        return {"deleted": sid}

    def export_response(samples: list[dict], fmt: str, name: str):
        if fmt == "jsonl":
            body = "".join(json.dumps(s, separators=(",", ":")) + "\n" for s in samples)
            media = "application/x-ndjson"
        elif fmt == "csv":
            buf = io.StringIO()
            cols = list(flatten(samples[0]).keys()) if samples else ["ts", "seq"] + STAT_FIELDS
            w = csv.DictWriter(buf, fieldnames=cols)
            w.writeheader()
            for s in samples:
                w.writerow(flatten(s))
            body, media = buf.getvalue(), "text/csv"
        else:
            raise HTTPException(400, "format must be csv or jsonl")
        return PlainTextResponse(body, media_type=media,
                                 headers={"Content-Disposition": f'attachment; filename="{name}.{fmt}"'})

    @app.get("/api/v1/sessions/{sid}/export", dependencies=[api])
    def export_session(sid: int, format: str = "csv"):
        s = store.session(sid)
        if s is None:
            raise HTTPException(404, f"no session {sid}")
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", s["label"])[:60] or "session"
        return export_response(store.range(s["start_ts"], s["end_ts"] or time.time()), format,
                               f"wvd-session-{sid}-{safe}")

    @app.get("/api/v1/export", dependencies=[api])
    def export(last: str | None = None, from_: str | None = Query(None, alias="from"), to: str | None = None,
               format: str = "csv"):
        t0, t1 = window(last, from_, to)
        return export_response(store.range(t0, t1), format, f"wvd-{int(t0)}-{int(t1)}")

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
                while not await request.is_disconnected():
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
