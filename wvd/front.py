"""The front process: the one port clients see.

Serves what must stay responsive whatever else is going on: the dashboard
files, the live streams (WebSocket/SSE), the latest sample, health and
metrics. All of it comes from the recorder's feed held in memory; nothing
here touches the database or decodes history. Every other /api/* request is
passed through to the api process over its Unix socket, so heavy queries
never run in this process (or in the recorder's).

Auth is checked here, for everything including proxied requests; the api
socket sits in a private directory and is not reachable otherwise.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from . import __version__, bus
from . import protocol as P
from .api import Config

log = logging.getLogger("wvd.front")
logging.getLogger("httpx").setLevel(logging.WARNING)  # one INFO line per proxied request otherwise

STATIC_DIR = Path(__file__).parent / "static"
# A slow subscriber is at most this far behind before its backlog is dropped
# (and it is told so with a "lag" message).
SUBSCRIBER_QUEUE_S = 5
STATUS_STALE_S = 3.0
API_ALIVE_TIMEOUT_S = 2.0
HOP_HEADERS = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade",
               "proxy-authorization", "proxy-authenticate", "host", "content-length", "authorization"}


class Msg:
    """A feed message, with its wire forms encoded at most once for all clients."""

    __slots__ = ("kind", "data", "line", "_sse")

    def __init__(self, kind: str, data: dict, line: str | None = None):
        self.kind, self.data = kind, data
        self.line = line or json.dumps({"kind": kind, "data": data}, separators=(",", ":"))
        self._sse = None

    @property
    def sse(self) -> str:
        if self._sse is None:
            self._sse = f"event: {self.kind}\ndata: {json.dumps(self.data, separators=(',', ':'))}\n\n"
        return self._sse


class Hub:
    """Fans feed messages out to WS/SSE subscribers, each with a bounded queue."""

    def __init__(self, maxsize: int = 250):
        self.subscribers: set[asyncio.Queue] = set()
        self.maxsize = maxsize
        self.dropped = 0

    def publish(self, msg: Msg) -> None:
        for q in self.subscribers:
            if q.full():
                # Slow consumer: drop its whole backlog rather than feed it stale
                # data, and say so. The lag message carries what was skipped.
                n = q.qsize()
                while not q.empty():
                    q.get_nowait()
                self.dropped += n
                q.put_nowait(Msg("lag", {"ts": time.time(), "dropped": n}))
            q.put_nowait(msg)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self.maxsize)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)


def _read_procs(runtime_dir: str) -> dict:
    try:
        with open(os.path.join(runtime_dir, bus.PROCS_FILE)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def create_front_app(cfg: Config, runtime_dir: str) -> FastAPI:
    hub = Hub(maxsize=max(50, int(SUBSCRIBER_QUEUE_S * cfg.rate_hz)))
    state = {"latest": None, "status": None, "status_mono": None, "feed": False}
    started = time.time()
    feed_path = os.path.join(runtime_dir, bus.FEED_SOCKET)
    proxy = httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=os.path.join(runtime_dir, bus.API_SOCKET)),
                              base_url="http://wvd-api", timeout=httpx.Timeout(None, connect=5.0))

    async def follow_feed() -> None:
        while True:
            try:
                reader, writer = await asyncio.open_unix_connection(feed_path, limit=1 << 20)
            except OSError:
                await asyncio.sleep(bus.RECONNECT_S)
                continue
            state["feed"] = True
            try:
                while line := await reader.readline():
                    text = line.decode().rstrip("\n")
                    m = json.loads(text)
                    kind, data = m["kind"], m["data"]
                    if kind == "sample":
                        state["latest"] = data
                    elif kind == "status":
                        state["status"], state["status_mono"] = data, time.monotonic()
                        continue  # internal: not relayed to clients
                    hub.publish(Msg(kind, data, text))
            except (OSError, ValueError, asyncio.IncompleteReadError):
                log.exception("feed read failed")
            finally:
                state["feed"] = False
                writer.close()
            await asyncio.sleep(bus.RECONNECT_S)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(follow_feed())
        yield
        task.cancel()
        await proxy.aclose()

    app = FastAPI(title="wvd", version=__version__, lifespan=lifespan,
                  description="WireView Pro II measurement daemon", docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.hub, app.state.cfg, app.state.front = hub, cfg, state

    def check_token(supplied: str | None) -> bool:
        return cfg.token is None or (supplied is not None and secrets.compare_digest(supplied, cfg.token))

    def auth(request: Request) -> None:
        h = request.headers.get("authorization", "")
        supplied = h[7:] if h.lower().startswith("bearer ") else request.query_params.get("token")
        if not check_token(supplied):
            raise HTTPException(401, "missing or bad token", headers={"WWW-Authenticate": "Bearer"})

    api = Depends(auth)

    def status() -> dict | None:
        """The recorder's last status, if recent enough to trust."""
        st = state["status"]
        if st is None or not state["feed"] or time.monotonic() - state["status_mono"] > STATUS_STALE_S:
            return None
        return st

    def info_dict() -> dict | None:
        st = status()
        return st["info"] if st and st["connected"] else None

    # -- status -----------------------------------------------------------
    @app.get("/api/v1/health")
    async def health():
        st = state["status"]
        status_age = round(time.monotonic() - state["status_mono"], 3) if st else None
        # A closed feed means the recorder is gone (or restarting): no need to wait for staleness.
        fresh = st is not None and status_age <= STATUS_STALE_S and state["feed"]
        latest = state["latest"]
        age = round(time.time() - latest["ts"], 3) if latest else None
        try:
            r = await proxy.get("/api/v1/_alive", timeout=API_ALIVE_TIMEOUT_S)
            api_ok = r.status_code == 200
        except httpx.HTTPError:
            api_ok = False
        recorder_ok = fresh and st["sampler_alive"] and st["writer_alive"] and not st["fatal"]
        ok = recorder_ok and api_ok and st["connected"] and age is not None \
            and age < max(3.0, 5 / cfg.rate_hz)
        g = (lambda k, d=None: st.get(k, d)) if st else (lambda k, d=None: d)
        return {"status": "ok" if ok else ("degraded" if recorder_ok else "down"),
                "connected": bool(fresh and st["connected"]), "version": __version__,
                "simulated": bool(cfg.simulate), "rate_hz": cfg.rate_hz, "measured_hz": g("measured_hz", 0.0),
                "last_sample_age_s": age, "last_sample_wall": g("last_sample_wall"),
                "last_seq": latest["seq"] if latest else g("last_seq"), "counters": g("counters", {}),
                "sampler_alive": bool(fresh and st["sampler_alive"]),
                "writer_alive": bool(fresh and st["writer_alive"]), "fatal": g("fatal"),
                "gaps_total": g("gaps_total", 0), "max_gap_s_5m": g("max_gap_s_5m", 0.0), "last_gap": g("last_gap"),
                "db_queue": g("db_queue"), "db_dropped": g("db_dropped", 0),
                "feed_dropped": g("feed_dropped", 0), "stream_dropped": hub.dropped,
                "recorder_status_age_s": status_age, "api_ok": api_ok, "feed_connected": state["feed"],
                "processes": _read_procs(runtime_dir),
                "last_error": g("last_error"), "uptime_s": round(time.time() - started, 1),
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

    @app.get("/api/v1/sensors/latest", dependencies=[api])
    def latest():
        s = state["latest"]
        if s is None:
            raise HTTPException(503, "no samples yet")
        return {**s, "age_s": round(time.time() - s["ts"], 3)}

    # -- streams ----------------------------------------------------------
    def hello() -> Msg:
        st = status()
        latest = state["latest"]
        return Msg("hello", {"info": info_dict(), "limits": cfg.limits.as_dict(),
                             "last_seq": latest["seq"] if latest else (st["last_seq"] if st else 0),
                             "rate_hz": cfg.rate_hz, "connected": bool(st and st["connected"])})

    def throttle(hz: float | None):
        min_dt = 1.0 / hz if hz and hz > 0 else 0.0
        last = [0.0]

        def keep(msg: Msg) -> bool:
            if msg.kind != "sample" or min_dt == 0:
                return True
            if msg.data["ts"] - last[0] >= min_dt * 0.98:
                last[0] = msg.data["ts"]
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
            await ws.send_text(hello().line)
            while True:
                msg = await q.get()
                if keep(msg):
                    await ws.send_text(msg.line)
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
                yield hello().sse
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
                        yield msg.sse
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

        st = status() or {}
        connected = bool(st.get("connected"))
        counters = st.get("counters", {})
        m("wvd_up", "1 when the device is connected", "gauge", [("", int(connected))])
        m("wvd_samples_total", "sensor reads by result", "counter",
          [(f'{{result="{k}"}}', counters.get(k, 0)) for k in ("ok", "corrupt", "failed")])
        m("wvd_sampler_alive", "1 while the recorder's sampler and writer threads run", "gauge",
          [("", int(bool(st.get("sampler_alive")) and bool(st.get("writer_alive"))))])
        m("wvd_gaps_total", "sampling pauses longer than two periods", "counter", [("", st.get("gaps_total", 0))])
        m("wvd_max_gap_seconds", "longest sampling pause in the last 5 minutes", "gauge",
          [("", st.get("max_gap_s_5m", 0.0))])
        m("wvd_db_dropped_total", "samples not written to the DB (writer backlog full or write error)", "counter",
          [("", st.get("db_dropped", 0))])
        m("wvd_stream_dropped_total", "stream messages dropped for slow subscribers", "counter",
          [("", hub.dropped + st.get("feed_dropped", 0))])
        procs = _read_procs(runtime_dir)
        m("wvd_process_restarts_total", "child process restarts by the supervisor", "counter",
          [(f'{{process="{k}"}}', v.get("restarts", 0)) for k, v in procs.items()])
        s = state["latest"]
        if s is not None and connected:
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

    # -- everything else: the api process ----------------------------------
    async def forward(request: Request) -> Response:
        headers = [(k, v) for k, v in request.headers.items() if k.lower() not in HOP_HEADERS]
        req = proxy.build_request(request.method, request.url.path, params=request.query_params,
                                  headers=headers, content=request.stream())
        try:
            r = await proxy.send(req, stream=True)
        except httpx.HTTPError as e:
            return JSONResponse({"detail": f"api process unavailable: {type(e).__name__}"}, status_code=503,
                                headers={"Retry-After": "2"})
        out = {k: v for k, v in r.headers.items() if k.lower() not in HOP_HEADERS}
        return StreamingResponse(r.aiter_raw(), status_code=r.status_code, headers=out,
                                 background=BackgroundTask(r.aclose))

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], dependencies=[api],
                   include_in_schema=False)
    async def api_passthrough(request: Request, path: str):
        return await forward(request)

    @app.api_route("/docs", methods=["GET"], include_in_schema=False)
    @app.api_route("/openapi.json", methods=["GET"], include_in_schema=False)
    @app.api_route("/docs/oauth2-redirect", methods=["GET"], include_in_schema=False)
    async def docs_passthrough(request: Request):
        return await forward(request)

    if STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="dashboard")
    return app
