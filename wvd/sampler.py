"""The sampler thread: sole owner of the device.

Connects (and reconnects), polls sensor frames on a fixed monotonic schedule,
hands them to the store, runs the event engine and publishes to subscribers.
Device commands from the API are queued here so they never interleave with a
read. Nothing on this path waits for the DB or for a query: the store only
appends to its ring and its writer queue.

The loop survives any exception (it logs, drops the connection and starts
over). If the thread still ends while the daemon runs, on_fatal is called so
the process can exit and be restarted, instead of serving a frozen value.
"""
from __future__ import annotations

import concurrent.futures as cf
import logging
import queue
import termios
import threading
import time
from collections import deque
from typing import Callable

from . import protocol as P
from .events import EventEngine
from .samples import build_sample
from .store import Store
from .transport import DeviceError, DeviceInfo, SerialDevice, SimulatedDevice, find_ports

log = logging.getLogger("wvd.sampler")

# Same rule as WireViewPro2Device: give up after 6 consecutive failed reads,
# at once if the port node has vanished.
MAX_FAILED_READS = 6
RECONNECT_INTERVAL_S = 1.0
# A pause longer than this many periods counts as a gap; from GAP_EVENT_S on
# it is also logged as a sampler.gap event.
GAP_PERIODS = 2.0
GAP_EVENT_S = 0.5
GAP_WINDOW_S = 300

Device = SerialDevice | SimulatedDevice


class InjectedKill(BaseException):
    """Test fault: escapes the loop's exception guard and ends the thread."""


class FaultInjector:
    """Test-only faults, from --test-fault / WVD_TEST_FAULT:

    read-termios:N  every Nth read raises termios.error (EIO), like a USB re-enumeration
    loop-error:N    every Nth loop pass raises RuntimeError outside the read
    kill:N          the Nth read ends the sampler thread
    db-slow:S       every DB write batch first sleeps S seconds (applied by the recorder)
    """

    KINDS = ("read-termios", "loop-error", "kill", "db-slow")

    def __init__(self, spec: str | None):
        self.kind, self.every, self.n = None, 0, 0
        if spec:
            kind, _, arg = spec.partition(":")
            if kind not in self.KINDS:
                raise ValueError(f"unknown test fault {spec!r}, one of {self.KINDS}")
            if kind == "db-slow":
                return  # not a sampler fault
            self.kind, self.every = kind, max(1, int(arg or 1))

    def tick(self, where: str) -> None:
        if self.kind is None or (where == "loop") != (self.kind == "loop-error"):
            return
        self.n += 1
        if self.n % self.every:
            return
        if self.kind == "read-termios":
            raise termios.error(5, "Input/output error (injected)")
        if self.kind == "loop-error":
            raise RuntimeError("injected loop error")
        if self.kind == "kill" and self.n == self.every:
            raise InjectedKill("injected sampler kill")


class Sampler:
    def __init__(self, store: Store, engine: EventEngine, publish: Callable[[dict], None],
                 rate_hz: float = 10.0, port: str | None = None, simulate: str | None = None,
                 test_fault: str | None = None, on_fatal: Callable[[str], None] | None = None):
        self.store = store
        self.engine = engine
        self.publish = publish
        self.period = 1.0 / rate_hz
        self.rate_hz = rate_hz
        self.port = port
        self.simulate = simulate
        self.on_fatal = on_fatal
        self.device: Device | None = None
        self.info: DeviceInfo | None = None
        self.counters = {"ok": 0, "corrupt": 0, "failed": 0, "connects": 0, "disconnects": 0, "loop_errors": 0}
        self.last_error: str | None = None
        self.started_at = time.time()
        self.last_sample_wall: float | None = None
        self.gaps_total = 0
        self.last_gap: dict | None = None
        self._gaps: deque[tuple[float, float]] = deque()
        self._prev_ts: float | None = None
        self._fault = FaultInjector(test_fault)
        self._commands: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._main, name="wvd-sampler", daemon=True)
        self._recent_ts: deque[float] = deque(maxlen=50)

    # -- public -----------------------------------------------------------
    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3)
        self._disconnect(reason="shutdown", emit=False)

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    @property
    def connected(self) -> bool:
        return self.device is not None and self.alive

    def max_gap_s(self, window_s: float = GAP_WINDOW_S) -> float:
        cutoff = time.monotonic() - window_s
        return max((g for t, g in list(self._gaps) if t >= cutoff), default=0.0)

    @property
    def measured_hz(self) -> float:
        ts = list(self._recent_ts)
        if len(ts) < 2 or ts[-1] == ts[0]:
            return 0.0
        return round((len(ts) - 1) / (ts[-1] - ts[0]), 2)

    def run_command(self, fn: Callable[[Device], object], timeout: float = 5.0):
        """Run fn(device) on the sampler thread between two reads."""
        fut: cf.Future = cf.Future()
        self._commands.put((fn, fut))
        return fut.result(timeout=timeout)

    # -- thread -----------------------------------------------------------
    def _candidates(self) -> list[Device]:
        if self.simulate:
            return [SimulatedDevice(self.simulate)]
        ports = [self.port] if self.port else find_ports()
        return [SerialDevice(p) for p in ports]

    def _connect(self) -> bool:
        for dev in self._candidates():
            try:
                dev.open()
                info = dev.identify()
            except Exception as e:  # termios.error is not an OSError
                self.last_error = f"{dev.port}: {e}"
                dev.close()
                continue
            self.device, self.info = dev, info
            self.counters["connects"] += 1
            self.last_error = None
            self.engine.reset()
            log.info("connected %s on %s (uid %s, %s)", info.edition, info.port, info.uid, info.build)
            self._emit({"ts": time.time(), "type": "device.connected", **info.as_dict()})
            return True
        return False

    def _disconnect(self, reason: str, emit: bool = True) -> None:
        if self.device is None:
            return
        port = self.device.port
        try:
            self.device.close()
        except Exception:
            log.exception("closing %s failed", port)
        self.device = None
        self.counters["disconnects"] += 1
        log.warning("disconnected from %s: %s", port, reason)
        if emit:
            self._emit({"ts": time.time(), "type": "device.disconnected", "port": port, "reason": reason})

    def _emit(self, event: dict) -> None:
        stored = self.store.add_event(event)
        self.publish({"kind": "event", "data": stored})

    def _drain_commands(self) -> None:
        while True:
            try:
                fn, fut = self._commands.get_nowait()
            except queue.Empty:
                return
            if self.device is None:
                fut.set_exception(DeviceError("device not connected"))
                continue
            try:
                fut.set_result(fn(self.device))
            except Exception as e:  # handed back to the API caller
                fut.set_exception(e)

    def _main(self) -> None:
        try:
            self._run()
        except BaseException as e:
            log.critical("sampler thread died", exc_info=True)
            self.last_error = f"sampler thread died: {e!r}"
            if self.on_fatal:
                self.on_fatal(self.last_error)
            return
        if not self._stop.is_set():  # returned without being asked to: same as dying
            if self.on_fatal:
                self.on_fatal("sampler thread exited")

    def _run(self) -> None:
        state = {"failures": 0, "next_tick": time.monotonic()}
        while not self._stop.is_set():
            try:
                self._step(state)
            except Exception as e:
                # Anything unexpected: log, drop the connection, start over.
                self.counters["loop_errors"] += 1
                self.last_error = f"sampler error: {e!r}"
                log.exception("sampler loop error")
                self._disconnect(reason=f"sampler error: {e!r}")
                self._stop.wait(RECONNECT_INTERVAL_S)
                state["next_tick"] = time.monotonic()

    def _step(self, state: dict) -> None:
        if self.device is None:
            if not self._connect():
                self._drain_commands()
                self._stop.wait(RECONNECT_INTERVAL_S)
                return
            state["failures"] = 0
            state["next_tick"] = time.monotonic()

        self._fault.tick("loop")
        self._drain_commands()
        frame = b""
        broken = False
        try:
            self._fault.tick("read")
            frame = self.device.read_sensor_frame()
        except DeviceError as e:
            self.last_error = str(e)
        except Exception as e:  # OSError, termios.error: the port itself is gone or broken
            self.last_error = f"{type(e).__name__}: {e}"
            broken = True
        if len(frame) == P.SENSOR_SIZE and not P.is_corrupt(frame):
            state["failures"] = 0
            self.counters["ok"] += 1
            self._on_frame(frame)
        else:
            state["failures"] += 1
            corrupt = len(frame) == P.SENSOR_SIZE
            self.counters["corrupt" if corrupt else "failed"] += 1
            if broken or (not corrupt and not self.device.node_exists()) or state["failures"] >= MAX_FAILED_READS:
                self._disconnect(reason=self.last_error if broken else f"{state['failures']} failed read(s)")
                return

        now = time.monotonic()
        state["next_tick"] += self.period
        if state["next_tick"] < now:  # fell behind (slow read): resync, don't burst
            state["next_tick"] = now
        self._stop.wait(max(0.0, state["next_tick"] - time.monotonic()))

    def _on_frame(self, frame: bytes) -> None:
        ts = time.time()
        gap = None if self._prev_ts is None else round(ts - self._prev_ts, 4)
        self._prev_ts = ts
        sample = build_sample(self.store.next_seq(), ts, self.info.uid, frame, gap)
        self.store.add(sample, frame)
        self.last_sample_wall = ts
        self._recent_ts.append(ts)
        self.publish({"kind": "sample", "data": sample})
        if gap is not None and gap > GAP_PERIODS * self.period:
            self._note_gap(sample, gap)
        for ev in self.engine.check(sample):
            self._emit(ev)

    def _note_gap(self, sample: dict, gap: float) -> None:
        now = time.monotonic()
        self.gaps_total += 1
        self._gaps.append((now, gap))
        while self._gaps and self._gaps[0][0] < now - GAP_WINDOW_S:
            self._gaps.popleft()
        self.last_gap = {"ts": sample["ts"], "gap_s": gap, "seq": sample["seq"]}
        if gap >= GAP_EVENT_S:
            log.warning("sampling gap of %.2f s before seq %d", gap, sample["seq"])
            self._emit({"ts": sample["ts"], "type": "sampler.gap", "gap_s": gap, "from_ts": sample["ts"] - gap,
                        "seq": sample["seq"]})
