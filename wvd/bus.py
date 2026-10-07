"""Local IPC between the wvd processes, over Unix sockets in a private directory.

The feed: the recorder broadcasts newline-delimited JSON messages
({"kind": ..., "data": ...}: sample, event, session, status, lag) to every
subscriber (front, api). Each subscriber has its own bounded queue and sender
thread, so a slow or stuck subscriber only loses its own live copy; the
recorder never waits on it.

The control channel: one JSON request per line, one JSON reply per line
(clear faults, publish a session change, ...), handled by the recorder.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from collections import deque
from typing import Callable

log = logging.getLogger("wvd.bus")

FEED_SOCKET = "feed.sock"
CONTROL_SOCKET = "control.sock"
API_SOCKET = "api.sock"
PROCS_FILE = "procs.json"
RECONNECT_S = 0.5


def encode(msg: dict) -> bytes:
    return (json.dumps(msg, separators=(",", ":")) + "\n").encode()


def _listen(path: str) -> socket.socket:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(path)
    os.chmod(path, 0o600)
    s.listen(16)
    return s


# -- recorder side ------------------------------------------------------------
class _Subscriber:
    def __init__(self, conn: socket.socket, maxlen: int, server: "FeedServer"):
        self.conn = conn
        self.queue: deque[bytes] = deque()
        self.maxlen = maxlen
        self.dropped = 0
        self.cond = threading.Condition()
        self.closed = False
        self.server = server
        self.thread = threading.Thread(target=self._send_loop, name="wvd-feed-send", daemon=True)

    def put(self, line: bytes) -> None:
        with self.cond:
            if len(self.queue) >= self.maxlen:
                # Too far behind: drop the backlog, tell it so, carry on live.
                n = len(self.queue)
                self.queue.clear()
                self.dropped += n
                self.server.dropped += n
                self.queue.append(encode({"kind": "lag", "data": {"ts": time.time(), "dropped": n}}))
            self.queue.append(line)
            self.cond.notify()

    def _send_loop(self) -> None:
        try:
            while True:
                with self.cond:
                    while not self.queue and not self.closed:
                        self.cond.wait()
                    if self.closed:
                        return
                    batch = b"".join(self.queue)
                    self.queue.clear()
                self.conn.sendall(batch)  # blocks only this thread, never the sampler
        except OSError:
            pass
        finally:
            self.close()

    def close(self) -> None:
        with self.cond:
            self.closed = True
            self.cond.notify()
        try:
            self.conn.close()
        except OSError:
            pass
        self.server._remove(self)


class FeedServer:
    """Broadcasts to subscribers; on_connect(send) lets the owner greet each new one."""

    def __init__(self, path: str, maxlen: int, on_connect: Callable[[Callable[[bytes], None]], None] | None = None):
        self.path = path
        self.maxlen = maxlen
        self.on_connect = on_connect
        self.dropped = 0
        self._subs: list[_Subscriber] = []
        self._lock = threading.Lock()
        self._sock = _listen(path)
        self._thread = threading.Thread(target=self._accept_loop, name="wvd-feed-accept", daemon=True)

    def start(self) -> None:
        self._thread.start()

    @property
    def subscribers(self) -> int:
        return len(self._subs)

    def publish(self, line: bytes) -> None:
        for sub in list(self._subs):
            sub.put(line)

    def _accept_loop(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            sub = _Subscriber(conn, self.maxlen, self)
            if self.on_connect:
                self.on_connect(sub.put)
            with self._lock:
                self._subs.append(sub)
            sub.thread.start()

    def _remove(self, sub: _Subscriber) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass
        for sub in list(self._subs):
            sub.close()


class ControlServer:
    """Line-delimited JSON request/reply; handler(request) -> reply dict."""

    def __init__(self, path: str, handler: Callable[[dict], dict]):
        self.handler = handler
        self._sock = _listen(path)
        self._thread = threading.Thread(target=self._accept_loop, name="wvd-control", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _accept_loop(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), name="wvd-control-conn", daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        with conn, conn.makefile("rwb") as f:
            for line in f:
                try:
                    reply = self.handler(json.loads(line))
                except Exception as e:  # reported to the caller, never fatal here
                    reply = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                f.write(encode(reply))
                f.flush()

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


# -- client side --------------------------------------------------------------
def control_call(path: str, request: dict, timeout: float = 10.0) -> dict:
    """One request to the recorder's control socket. Raises ConnectionError if it is not there."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(path)
            s.sendall(encode(request))
            with s.makefile("rb") as f:
                line = f.readline()
    except (OSError, socket.timeout) as e:
        raise ConnectionError(f"recorder control: {e}") from e
    if not line:
        raise ConnectionError("recorder control: no reply")
    return json.loads(line)


class FeedClient:
    """Thread that follows the feed, reconnecting forever; calls on_line(raw bytes)."""

    def __init__(self, path: str, on_line: Callable[[bytes], None], name: str = "wvd-feed"):
        self.path = path
        self.on_line = on_line
        self.connected = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.connect(self.path)
                    self.connected = True
                    with s.makefile("rb") as f:
                        for line in f:
                            if self._stop.is_set():
                                return
                            try:
                                self.on_line(line)
                            except Exception:
                                log.exception("feed handler failed")
            except OSError:
                pass
            self.connected = False
            self._stop.wait(RECONNECT_S)
