"""The sampler thread: sole owner of the device.

Connects (and reconnects), polls sensor frames on a fixed monotonic schedule,
stores them, runs the event engine and publishes to subscribers. Device
commands from the API are queued here so they never interleave with a read.
"""
from __future__ import annotations

import concurrent.futures as cf
import logging
import queue
import threading
import time
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
FLUSH_INTERVAL_S = 0.5

Device = SerialDevice | SimulatedDevice


class Sampler:
    def __init__(self, store: Store, engine: EventEngine, publish: Callable[[dict], None],
                 rate_hz: float = 10.0, port: str | None = None, simulate: str | None = None):
        self.store = store
        self.engine = engine
        self.publish = publish
        self.period = 1.0 / rate_hz
        self.rate_hz = rate_hz
        self.port = port
        self.simulate = simulate
        self.device: Device | None = None
        self.info: DeviceInfo | None = None
        self.counters = {"ok": 0, "corrupt": 0, "failed": 0, "connects": 0, "disconnects": 0}
        self.last_error: str | None = None
        self.started_at = time.time()
        self._commands: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="wvd-sampler", daemon=True)
        self._recent_ts: list[float] = []

    # -- public -----------------------------------------------------------
    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3)
        self._disconnect(reason="shutdown", emit=False)
        self.store.flush()

    @property
    def connected(self) -> bool:
        return self.device is not None

    @property
    def measured_hz(self) -> float:
        ts = self._recent_ts
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
            except (OSError, DeviceError, ValueError) as e:
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
        self.device.close()
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

    def _run(self) -> None:
        failures = 0
        next_tick = time.monotonic()
        last_flush = time.monotonic()
        while not self._stop.is_set():
            if self.device is None:
                if not self._connect():
                    self._drain_commands()
                    self._stop.wait(RECONNECT_INTERVAL_S)
                    continue
                failures = 0
                next_tick = time.monotonic()

            self._drain_commands()
            frame = b""
            try:
                frame = self.device.read_sensor_frame()
            except (OSError, DeviceError) as e:
                self.last_error = str(e)
            if len(frame) == P.SENSOR_SIZE and not P.is_corrupt(frame):
                failures = 0
                self.counters["ok"] += 1
                ts = time.time()
                sample = build_sample(self.store.next_seq(), ts, self.info.uid, frame)
                self.store.add(sample, frame)
                self._recent_ts = (self._recent_ts + [ts])[-50:]
                self.publish({"kind": "sample", "data": sample})
                for ev in self.engine.check(sample):
                    self._emit(ev)
            else:
                failures += 1
                corrupt = len(frame) == P.SENSOR_SIZE
                self.counters["corrupt" if corrupt else "failed"] += 1
                if (not corrupt and not self.device.node_exists()) or failures >= MAX_FAILED_READS:
                    self._disconnect(reason=f"{failures} failed read(s)")
                    continue

            now = time.monotonic()
            if now - last_flush >= FLUSH_INTERVAL_S:
                last_flush = now
                try:
                    self.store.flush()
                except Exception:
                    log.exception("store flush failed")
            next_tick += self.period
            if next_tick < now:  # fell behind (slow read/flush): resync, don't burst
                next_tick = now
            self._stop.wait(max(0.0, next_tick - time.monotonic()))
