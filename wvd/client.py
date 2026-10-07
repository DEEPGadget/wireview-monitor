"""Python client for a running wvd. Standard library only, so test code and
the CLI can use it without the server's dependencies."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterator

DEFAULT_URL = "http://127.0.0.1:8765"


class WvdError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class Session:
    """A test window on the daemon. As a context manager it starts on enter and
    stops on exit; stats() then covers exactly that window."""

    def __init__(self, client: "WireView", label: str, meta: dict | None = None):
        self.client, self.label, self.meta = client, label, meta or {}
        self.id: int | None = None
        self.result: dict | None = None

    def start(self) -> "Session":
        self.id = self.client._req("POST", "/api/v1/sessions", {"label": self.label, "meta": self.meta})["id"]
        return self

    def stop(self) -> dict:
        self.result = self.client._req("POST", f"/api/v1/sessions/{self.id}/stop")
        return self.result

    def stats(self) -> dict:
        if self.result is not None:
            return self.result["stats"]
        return self.client._req("GET", f"/api/v1/sessions/{self.id}")["stats"]

    @property
    def faults(self) -> list[str]:
        return self.stats()["faults_seen"]

    def export(self, fmt: str = "csv") -> bytes:
        return self.client._raw("GET", f"/api/v1/sessions/{self.id}/export?format={fmt}")

    def __enter__(self) -> "Session":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


class WireView:
    def __init__(self, url: str | None = None, token: str | None = None, timeout: float = 10.0):
        self.url = (url or os.environ.get("WVD_URL") or DEFAULT_URL).rstrip("/")
        self.token = token if token is not None else os.environ.get("WVD_TOKEN")
        self.timeout = timeout

    # -- transport --------------------------------------------------------
    def _request(self, method: str, path: str, body: dict | None = None) -> urllib.request.Request:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        return req

    def _raw(self, method: str, path: str, body: dict | None = None) -> bytes:
        try:
            with urllib.request.urlopen(self._request(method, path, body), timeout=self.timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read()).get("detail", e.reason)
            except Exception:
                detail = e.reason
            raise WvdError(f"{method} {path}: {e.code} {detail}", e.code) from None
        except urllib.error.URLError as e:
            raise WvdError(f"cannot reach wvd at {self.url}: {e.reason}") from None

    def _req(self, method: str, path: str, body: dict | None = None):
        return json.loads(self._raw(method, path, body))

    @staticmethod
    def _q(**params) -> str:
        p = {("from" if k == "from_" else k): v for k, v in params.items() if v is not None}
        return ("?" + urllib.parse.urlencode(p)) if p else ""

    # -- API --------------------------------------------------------------
    def health(self) -> dict:
        return self._req("GET", "/api/v1/health")

    def info(self) -> dict:
        return self._req("GET", "/api/v1/info")

    def limits(self) -> dict:
        return self._req("GET", "/api/v1/limits")

    def latest(self) -> dict:
        return self._req("GET", "/api/v1/sensors/latest")

    def history(self, last: str | None = None, from_: float | None = None, to: float | None = None,
                step: str | None = None, after_seq: int | None = None) -> list[dict]:
        q = self._q(last=last, from_=from_, to=to, step=step, after_seq=after_seq)
        return self._req("GET", "/api/v1/sensors/history" + q)["samples"]

    def stats(self, last: str | None = None, from_: float | None = None, to: float | None = None,
              session: int | None = None) -> dict:
        return self._req("GET", "/api/v1/sensors/stats" + self._q(last=last, from_=from_, to=to, session=session))

    def events(self, last: str | None = None, type: str | None = None) -> list[dict]:
        return self._req("GET", "/api/v1/events" + self._q(last=last, type=type))["events"]

    def sessions(self) -> list[dict]:
        return self._req("GET", "/api/v1/sessions")["sessions"]

    def session(self, label: str, meta: dict | None = None) -> Session:
        return Session(self, label, meta)

    def export(self, last: str | None = None, from_: float | None = None, to: float | None = None,
               fmt: str = "csv") -> bytes:
        return self._raw("GET", "/api/v1/export" + self._q(last=last, from_=from_, to=to, format=fmt))

    def clear_faults(self, fault: str | None = None) -> dict:
        return self._req("POST", "/api/v1/device/clear-faults", {"fault": fault} if fault else {})

    def stream(self, hz: float | None = None) -> Iterator[tuple[str, dict]]:
        """Yield (kind, data) from the SSE stream: hello, sample, event, session."""
        req = self._request("GET", "/api/v1/stream/sse" + self._q(hz=hz))
        try:
            resp = urllib.request.urlopen(req, timeout=60)
        except urllib.error.URLError as e:
            raise WvdError(f"cannot reach wvd at {self.url}: {getattr(e, 'reason', e)}") from None
        with resp:
            kind, data = None, []
            for raw in resp:
                line = raw.decode().rstrip("\n")
                if line.startswith("event:"):
                    kind = line[6:].strip()
                elif line.startswith("data:"):
                    data.append(line[5:].strip())
                elif line == "" and kind:
                    yield kind, json.loads("\n".join(data))
                    kind, data = None, []

    def wait_ready(self, timeout: float = 15.0) -> dict:
        """Block until the daemon answers and has a connected device with samples."""
        deadline = time.monotonic() + timeout
        last: Exception | None = None
        while time.monotonic() < deadline:
            try:
                h = self.health()
                if h["connected"] and h["last_seq"] > 0:
                    return h
            except WvdError as e:
                last = e
            time.sleep(0.2)
        raise WvdError(f"wvd at {self.url} not ready after {timeout}s" + (f": {last}" if last else ""))
