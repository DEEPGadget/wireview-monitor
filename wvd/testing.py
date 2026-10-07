"""pytest plugin: the `wireview` fixture.

With WVD_URL set it talks to that daemon (real hardware); otherwise it starts
a simulated daemon for the test session (scenario from WVD_SIMULATE, default
"load"). Tests are the same either way.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys

import pytest

from .client import WireView


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def wireview():
    url = os.environ.get("WVD_URL")
    if url:
        wv = WireView(url)
        wv.wait_ready()
        yield wv
        return
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "wvd", "--simulate", os.environ.get("WVD_SIMULATE", "load"),
         "--host", "127.0.0.1", "--port", str(port), "--db", ":memory:", "--rate", "20", "--log-level", "warning"])
    try:
        wv = WireView(f"http://127.0.0.1:{port}")
        wv.wait_ready(timeout=20)
        yield wv
    finally:
        proc.terminate()
        proc.wait(timeout=10)
