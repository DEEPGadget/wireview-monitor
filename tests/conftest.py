"""Shared fixtures: a real wvd (supervisor + recorder + api + front) on a free port."""
import os
import signal
import socket
import subprocess
import sys
import time

import httpx
import pytest


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(fn, timeout=10.0, every=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            v = fn()
        except (httpx.HTTPError, KeyError, TypeError):
            v = None
        if v:
            return v
        time.sleep(every)
    return None


class Daemon:
    def __init__(self, proc: subprocess.Popen, url: str, headers: dict | None = None):
        self.proc, self.url = proc, url
        self.http = httpx.Client(base_url=url, timeout=30, headers=headers or {})

    def get(self, path, **kw):
        return self.http.get(path, **kw)

    def post(self, path, **kw):
        return self.http.post(path, **kw)

    def health(self) -> dict:
        return self.http.get("/api/v1/health").json()

    def events(self, type_=None) -> list[dict]:
        params = {"last": "1h"} | ({"type": type_} if type_ else {})
        return self.http.get("/api/v1/events", params=params).json()["events"]

    def stop(self) -> None:
        self.http.close()
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()


def start_daemon(*extra, simulate="load", rate=50, min_seq=10, env=None, headers=None) -> Daemon:
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "wvd", "--simulate", simulate, "--host", "127.0.0.1", "--port", str(port),
         "--db", ":memory:", "--rate", str(rate), "--log-level", "warning", *extra],
        env={**os.environ, **(env or {})})
    d = Daemon(proc, f"http://127.0.0.1:{port}", headers)
    if min_seq is not None:
        ok = wait_for(lambda: d.health()["last_seq"] >= min_seq and d.health()["api_ok"], timeout=30)
        if not ok:
            d.stop()
            raise RuntimeError("daemon did not come up")
    return d


@pytest.fixture
def daemon():
    """Factory: daemon(*extra_args, simulate=..., rate=...) -> Daemon; all stopped at teardown."""
    started: list[Daemon] = []

    def make(*extra, **kw):
        d = start_daemon(*extra, **kw)
        started.append(d)
        return d

    yield make
    for d in started:
        d.stop()
