"""`wvd` entry point: parse options, then run the three processes.

`wvd` itself is a small supervisor. It starts, in order:

  recorder  device reads, DB writes, events; publishes the live feed
  api       queries, sessions, exports (Unix socket, behind front)
  front     the HTTP port: dashboard, streams, health, latest, metrics;
            passes other /api/* requests to api

and restarts any of them that exits, after RESTART_DELAY_S. A child that
needs more than MAX_RESTARTS restarts within RESTART_WINDOW_S makes wvd exit
with EXIT_FATAL so the service manager (systemd Restart=on-failure) takes
over. Children die with the supervisor (PR_SET_PDEATHSIG).

The same command runs each child, with the hidden --role option.
"""
from __future__ import annotations

import argparse
import ctypes
import ipaddress
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from . import bus
from .events import Limits
from .store import default_db_path
from .transport import SCENARIOS


def _limit(text: str) -> float | None:
    return None if text.lower() in ("none", "off", "") else float(text)


ROLES = ("recorder", "api", "front")
RESTART_DELAY_S = 1.0
MAX_RESTARTS = 5
RESTART_WINDOW_S = 60
START_TIMEOUT_S = 20
STOP_TIMEOUT_S = 8
EXIT_FATAL = 70


def build_parser() -> argparse.ArgumentParser:
    env = os.environ.get
    p = argparse.ArgumentParser(prog="wvd", description="WireView Pro II measurement daemon (REST/WS API + dashboard)")
    p.add_argument("--host", default=env("WVD_HOST", "0.0.0.0"), help="bind address (default 0.0.0.0, all interfaces)")
    p.add_argument("--port", type=int, default=int(env("WVD_PORT", "8765")), help="HTTP port (default 8765)")
    p.add_argument("--device", default=env("WVD_DEVICE"), help="serial port (default: auto-detect 0483:5740)")
    p.add_argument("--simulate", choices=SCENARIOS, default=env("WVD_SIMULATE"),
                   help="use a simulated device instead of hardware")
    p.add_argument("--rate", type=float, default=float(env("WVD_RATE", "10")), help="samples per second, 1-50 (default 10)")
    p.add_argument("--db", default=env("WVD_DB", default_db_path()), help="SQLite path (':memory:' for none)")
    p.add_argument("--retention", default=env("WVD_RETENTION", "72h"), help="keep raw samples this long (default 72h)")
    p.add_argument("--token", default=env("WVD_TOKEN"), help="require this Bearer token (env WVD_TOKEN)")
    p.add_argument("--allow-write", action="store_true", default=env("WVD_ALLOW_WRITE") == "1",
                   help="enable device write commands (clear-faults)")
    p.add_argument("--limit-pin-a", type=_limit, default=9.5, help="per-pin current warning, A (default 9.5)")
    p.add_argument("--limit-total-w", type=_limit, default=600.0, help="total power warning, W (default 600)")
    p.add_argument("--limit-temp-c", type=_limit, default=80.0, help="temperature warning, C (default 80)")
    p.add_argument("--limit-imbalance", type=_limit, default=1.5, help="pin imbalance warning (default 1.5)")
    p.add_argument("--log-level", default=env("WVD_LOG_LEVEL", "info"))
    p.add_argument("--test-fault", default=env("WVD_TEST_FAULT"), help=argparse.SUPPRESS)
    p.add_argument("--role", choices=ROLES, help=argparse.SUPPRESS)
    p.add_argument("--runtime-dir", help=argparse.SUPPRESS)
    return p


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _config(args):
    from .api import Config, parse_duration

    return Config(host=args.host, port=args.port, token=args.token, allow_write=args.allow_write,
                  rate_hz=args.rate, retention_s=parse_duration(args.retention), db_path=args.db,
                  device_port=args.device, simulate=args.simulate,
                  limits=Limits(args.limit_pin_a, args.limit_total_w, args.limit_temp_c, args.limit_imbalance),
                  test_fault=args.test_fault)


# -- children -------------------------------------------------------------------
def _run_role(args) -> None:
    os.umask(0o077)
    cfg = _config(args)
    rt = args.runtime_dir
    if args.role == "recorder":
        from .recorder import main as recorder_main

        sys.exit(recorder_main(cfg, rt))

    import uvicorn

    if args.role == "api":
        from .api import create_app

        uvicorn.Server(uvicorn.Config(create_app(cfg, rt), uds=os.path.join(rt, bus.API_SOCKET),
                                      log_level=args.log_level.lower(), access_log=False,
                                      timeout_graceful_shutdown=3)).run()
    else:
        from .front import create_front_app

        # Open streams would otherwise hold a stop for long.
        uvicorn.Server(uvicorn.Config(create_front_app(cfg, rt), host=cfg.host, port=cfg.port,
                                      log_level=args.log_level.lower(), access_log=False, ws_ping_interval=20,
                                      timeout_graceful_shutdown=3)).run()


def _die_with_parent() -> None:
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except OSError:
        pass


# -- supervisor -----------------------------------------------------------------
class Supervisor:
    def __init__(self, argv: list[str], args):
        self.log = logging.getLogger("wvd")
        self.args = args
        self.own_rt = False
        rt = os.environ.get("RUNTIME_DIRECTORY", "").split(":")[0]
        if not rt:
            rt = tempfile.mkdtemp(prefix="wvd-", dir=os.environ.get("XDG_RUNTIME_DIR"))
            self.own_rt = True
        os.chmod(rt, 0o700)
        self.rt = rt
        db = args.db
        if not db or db == ":memory:":  # children must share it: a throwaway file instead
            db = os.path.join(rt, "memory.db")
        self.base = [sys.executable, "-m", "wvd", *argv, "--runtime-dir", rt, "--db", db]
        self.procs: dict[str, subprocess.Popen | None] = dict.fromkeys(ROLES)
        self.info = {r: {"pid": None, "restarts": 0, "started_at": None, "last_exit": None} for r in ROLES}
        self.history: dict[str, list[float]] = {r: [] for r in ROLES}
        self.stopping = False

    def _write_procs(self) -> None:
        path = os.path.join(self.rt, bus.PROCS_FILE)
        with open(path + ".tmp", "w") as f:
            json.dump({"supervisor": {"pid": os.getpid()}, **self.info}, f)
        os.replace(path + ".tmp", path)

    def _start(self, role: str) -> None:
        p = subprocess.Popen(self.base + ["--role", role], preexec_fn=_die_with_parent)
        self.procs[role] = p
        self.info[role].update(pid=p.pid, started_at=time.time())
        self._write_procs()

    def _wait_ready(self, role: str) -> None:
        sock = {"recorder": bus.FEED_SOCKET, "api": bus.API_SOCKET}.get(role)
        if not sock:
            return
        deadline = time.monotonic() + START_TIMEOUT_S
        path = os.path.join(self.rt, sock)
        while time.monotonic() < deadline and not os.path.exists(path):
            if self.procs[role].poll() is not None:
                return  # died while starting: the main loop handles it
            time.sleep(0.05)

    def _stop_all(self) -> None:
        self.stopping = True
        for role in reversed(ROLES):  # front first, the recorder last so it flushes everything
            p = self.procs[role]
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=STOP_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    self.log.warning("%s did not stop in %d s, killing it", role, STOP_TIMEOUT_S)
                    p.kill()
                    p.wait()

    def run(self) -> int:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: setattr(self, "stopping", True))
        for role in ROLES:
            try:
                os.unlink(os.path.join(self.rt, {"recorder": bus.FEED_SOCKET, "api": bus.API_SOCKET}.get(role, "-")))
            except OSError:
                pass
        code = 0
        try:
            for role in ROLES:
                self._start(role)
                self._wait_ready(role)
            pending: dict[str, float] = {}
            while not self.stopping:
                time.sleep(0.2)
                now = time.monotonic()
                for role, p in self.procs.items():
                    if role in pending:
                        if now >= pending[role]:
                            del pending[role]
                            self._start(role)
                        continue
                    rc = p.poll() if p else None
                    if rc is None:
                        continue
                    if self.stopping:
                        break
                    self.info[role]["last_exit"] = rc
                    hist = [t for t in self.history[role] if now - t < RESTART_WINDOW_S] + [now]
                    self.history[role] = hist
                    if len(hist) > MAX_RESTARTS:
                        self.log.critical("%s exited %d times within %d s (last code %s): giving up",
                                          role, len(hist), RESTART_WINDOW_S, rc)
                        self.stopping = True
                        code = EXIT_FATAL
                        break
                    self.info[role]["restarts"] += 1
                    self.log.error("%s exited with code %s: restarting in %.0f s", role, rc, RESTART_DELAY_S)
                    pending[role] = now + RESTART_DELAY_S
                    self._write_procs()
        finally:
            self._stop_all()
            if self.own_rt:
                shutil.rmtree(self.rt, ignore_errors=True)
        return code


def _supervisor_argv(argv: list[str]) -> list[str]:
    """argv for the children: everything the user gave, minus what the supervisor sets."""
    out, skip = [], False
    for a in argv:
        if skip:
            skip = False
            continue
        if a in ("--db", "--runtime-dir", "--role"):
            skip = True
            continue
        if a.startswith(("--db=", "--runtime-dir=", "--role=")):
            continue
        out.append(a)
    return out


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    args = build_parser().parse_args(argv)
    role = args.role or "wvd"
    logging.basicConfig(level=args.log_level.upper(),
                        format=f"%(asctime)s %(levelname)s [{role}] %(name)s: %(message)s")
    if not 1 <= args.rate <= 50:
        sys.exit("wvd: --rate must be between 1 and 50")
    open_bind = not _is_loopback(args.host) and not args.token
    if open_bind and args.allow_write:
        sys.exit(f"wvd: --allow-write on {args.host} needs --token: anyone on the network could send device commands")
    if args.role:
        if not args.runtime_dir:
            sys.exit("wvd: --role needs --runtime-dir")
        _run_role(args)
        return

    log = logging.getLogger("wvd")
    if args.test_fault:
        log.warning("test fault injection on: %s", args.test_fault)
    if open_bind:
        log.warning("no --token: anyone who can reach %s:%d can read the data", args.host, args.port)
    log.info("serving on http://%s:%d (db %s, %.0f Hz%s)", args.host, args.port, args.db, args.rate,
             f", simulate={args.simulate}" if args.simulate else "")
    sys.exit(Supervisor(_supervisor_argv(argv), args).run())


if __name__ == "__main__":
    main()
