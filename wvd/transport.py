"""Device transports: the real serial port and a simulator with the same API."""
from __future__ import annotations

import fcntl
import glob
import math
import os
import random
import select
import struct
import termios
import threading
import time
from dataclasses import dataclass

from . import protocol as P

USB_VID, USB_PID = "0483", "5740"  # STM32 CDC/ACM; shared with other STM32 gadgets
READ_TIMEOUT_S = 1.0


class DeviceError(Exception):
    pass


@dataclass
class DeviceInfo:
    port: str
    uid: str
    vendor_id: int
    product_id: int
    fw_version: int
    edition: str
    hw_rev: str
    build: str

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _usb_ids(tty_sysfs: str) -> tuple[str, str] | None:
    # tty -> device is the USB interface; idVendor/idProduct live on its parent.
    dev = os.path.realpath(os.path.join(tty_sysfs, "device"))
    for d in (dev, os.path.dirname(dev)):
        try:
            with open(os.path.join(d, "idVendor")) as f, open(os.path.join(d, "idProduct")) as g:
                return f.read().strip(), g.read().strip()
        except OSError:
            continue
    return None


def find_ports() -> list[str]:
    """ttyACM nodes whose USB device is 0483:5740. The id is generic STM32, so
    each candidate is still identified by its vendor data before use."""
    return ["/dev/" + os.path.basename(tty) for tty in sorted(glob.glob("/sys/class/tty/ttyACM*"))
            if _usb_ids(tty) == (USB_VID, USB_PID)]


class SerialDevice:
    """One WireView on a tty, opened exclusively for the life of the connection."""

    def __init__(self, port: str):
        self.port = port
        self._fd: int | None = None

    # -- low level --------------------------------------------------------
    def open(self) -> None:
        fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            # Exclusive: no framing on the wire, so a second reader corrupts both.
            fcntl.ioctl(fd, termios.TIOCEXCL)
            attrs = termios.tcgetattr(fd)
            attrs[0] = 0  # iflag
            attrs[1] = 0  # oflag
            attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
            attrs[3] = 0  # lflag
            attrs[4] = attrs[5] = termios.B115200
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
        except Exception:
            os.close(fd)
            raise
        self._fd = fd

    def close(self) -> None:
        if self._fd is not None:
            try:
                fcntl.ioctl(self._fd, termios.TIOCNXCL)
            except OSError:
                pass
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    def node_exists(self) -> bool:
        return os.path.exists(self.port)

    def _read_exact(self, size: int) -> bytes:
        assert self._fd is not None
        buf = b""
        deadline = time.monotonic() + READ_TIMEOUT_S
        while len(buf) < size:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([self._fd], [], [], left)[0]:
                break
            chunk = os.read(self._fd, size - len(buf))
            if not chunk:
                break
            buf += chunk
        return buf

    def transact(self, payload: bytes, reply_size: int = 0) -> bytes:
        if self._fd is None:
            raise DeviceError("port not open")
        termios.tcflush(self._fd, termios.TCIFLUSH)
        os.write(self._fd, payload)
        if reply_size == 0:
            termios.tcdrain(self._fd)
            return b""
        return self._read_exact(reply_size)

    # -- commands ---------------------------------------------------------
    def identify(self) -> DeviceInfo:
        vd = P.decode_vendor(self._expect(P.CMD_READ_VENDOR_DATA, P.VENDOR_SIZE))
        if not vd.supported:
            raise DeviceError(f"{self.port}: unsupported device {vd.hw_rev}")
        uid = self._expect(P.CMD_READ_UID, P.UID_SIZE).hex().upper()
        build = P.decode_build(self._expect(P.CMD_READ_BUILD_INFO, P.BUILD_SIZE))
        return DeviceInfo(self.port, uid, vd.vendor_id, vd.product_id, vd.fw_version, vd.edition, vd.hw_rev, build)

    def _expect(self, cmd: int, size: int) -> bytes:
        buf = self.transact(bytes([cmd]), size)
        if len(buf) != size:
            raise DeviceError(f"{self.port}: command 0x{cmd:02X} returned {len(buf)}/{size} bytes")
        return buf

    def read_sensor_frame(self) -> bytes:
        return self.transact(bytes([P.CMD_READ_SENSOR_VALUES]), P.SENSOR_SIZE)

    def clear_faults(self, keep_status_mask: int = 0, keep_log_mask: int = 0) -> None:
        self.transact(P.clear_faults_payload(keep_status_mask, keep_log_mask))


SCENARIOS = ("idle", "load", "imbalance", "fault")


class SimulatedDevice:
    """Synthesizes sensor frames for a scenario; frames go through the real decoder.

    idle      ~20 W, all pins balanced
    load      300-450 W sine load, temperatures follow the load
    imbalance load, with pin 3 carrying ~2x its share
    fault     load, an over-current fault latches every 20 s and clears 5 s later
    """

    def __init__(self, scenario: str = "load", seed: int | None = None):
        if scenario not in SCENARIOS:
            raise ValueError(f"unknown scenario {scenario!r}, pick one of {SCENARIOS}")
        self.scenario = scenario
        self.port = f"sim:{scenario}"
        self._rng = random.Random(seed)
        self._t0 = time.monotonic()
        self._fault_log = 0
        self._lock = threading.Lock()
        self._open = False

    def open(self) -> None:
        self._open = True
        self._t0 = time.monotonic()

    def close(self) -> None:
        self._open = False

    def node_exists(self) -> bool:
        return True

    def identify(self) -> DeviceInfo:
        return DeviceInfo(self.port, "51D0000000000000000000" + format(SCENARIOS.index(self.scenario), "02X"),
                          0xEF, 0x05, 5, P.EDITIONS[0x05], "EF05", "SIMULATED_" + self.scenario.upper())

    def read_sensor_frame(self) -> bytes:
        if not self._open:
            raise DeviceError("simulator not open")
        t = time.monotonic() - self._t0
        rng = self._rng
        if self.scenario == "idle":
            total = 20.0
        else:
            total = 375 + 75 * math.sin(2 * math.pi * t / 30)
        share = [1.0] * P.PIN_COUNT
        if self.scenario == "imbalance":
            share[2] = 2.0
        norm = sum(share)
        volts = 12.25 - total / 600 * 0.15
        pins = []
        for s in share:
            amps = total / volts * s / norm * (1 + rng.uniform(-0.03, 0.03))
            pins.append((volts + rng.uniform(-0.006, 0.006), max(0.0, amps)))
        heat = total / 600
        temps = (30 + 25 * heat + rng.uniform(-0.2, 0.2), 31 + 28 * heat + rng.uniform(-0.2, 0.2),
                 29 + 15 * heat, None)
        status = 0
        if self.scenario == "fault" and (t % 20) < 5 and t > 5:
            status = 1 << P.FAULTS.index("OCP")
        with self._lock:
            self._fault_log |= status
            log = self._fault_log
        fan = min(100, int(40 + 60 * heat)) if self.scenario != "idle" else 0
        return P.encode_sensors(pins, temps, 3.45, fan, 600, status, log)

    def clear_faults(self, keep_status_mask: int = 0, keep_log_mask: int = 0) -> None:
        with self._lock:
            self._fault_log &= keep_log_mask
