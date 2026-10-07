"""WireView Pro II serial protocol: commands, frame layouts, decoding.

Ported from WireViewDeviceLib (WireViewPro2Device*.cs) and verified against a
real device. The link has no framing or CRC: flush input, write one command
byte, read a fixed-size reply.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

CMD_WELCOME = 0x00
CMD_READ_VENDOR_DATA = 0x01
CMD_READ_UID = 0x02
CMD_READ_SENSOR_VALUES = 0x04
CMD_READ_BUILD_INFO = 0x0D
CMD_CLEAR_FAULTS = 0x0E

VENDOR_SIZE = 3
UID_SIZE = 12
BUILD_SIZE = 68  # VendorData(3) + ProductName(32) + BuildInfo(32) + ProductNameLength(1)

# SensorStruct, Pack=4 (100 bytes, little endian):
#   Ts[4] int16 0.1 C | Vdd u16 mV | FanDuty u8 % | pad
#   6 x { Voltage int16 mV | pad2 | Current u32 mA | Power u32 mW }
#   TotalPower u32 mW | TotalCurrent u32 mA | AvgVoltage u16 mV | HpwrCapability u8 | pad
#   FaultStatus u16 | FaultLog u16
SENSOR_FMT = "<4hHBx" + "h2xII" * 6 + "IIHBxHH"
SENSOR_SIZE = struct.calcsize(SENSOR_FMT)
assert SENSOR_SIZE == 100

PIN_COUNT = 6
PSU_CAP_W = {0: 600, 1: 450, 2: 300, 3: 150}
PSU_CAP_CODE = {w: c for c, w in PSU_CAP_W.items()}

# Bit positions of FaultStatus / FaultLog (WireViewPro2Device.FAULT).
FAULTS = ["OTP_TCHIP", "OTP_TS", "OCP", "WIRE_OCP", "OPP", "CURRENT_IMBALANCE"]
FAULT_LABELS = {
    "OTP_TCHIP": "Chip over-temperature",
    "OTP_TS": "Sensor over-temperature",
    "OCP": "Over-current",
    "WIRE_OCP": "Wire over-current",
    "OPP": "Over-power",
    "CURRENT_IMBALANCE": "Current imbalance",
}

VENDOR_THERMAL_GRIZZLY = 0xEF
EDITIONS = {0x05: "WireView Pro II", 0x06: "WireView Pro II Noctua Edition"}

# An absent temperature sensor reads about -3276.8 C (TemperatureSensorPresence.cs).
TEMP_VALID_RANGE_C = (-100.0, 200.0)

# Below this total current the per-pin ratio is noise, not imbalance.
IMBALANCE_MIN_LOAD_A = 1.0


def fault_names(mask: int) -> list[str]:
    return [name for bit, name in enumerate(FAULTS) if mask & (1 << bit)]


@dataclass(frozen=True)
class VendorData:
    vendor_id: int
    product_id: int
    fw_version: int

    @property
    def supported(self) -> bool:
        return self.vendor_id == VENDOR_THERMAL_GRIZZLY and self.product_id in EDITIONS

    @property
    def edition(self) -> str:
        return EDITIONS.get(self.product_id, f"Unknown {self.vendor_id:02X}{self.product_id:02X}")

    @property
    def hw_rev(self) -> str:
        return f"{self.vendor_id:02X}{self.product_id:02X}"


def decode_vendor(buf: bytes) -> VendorData:
    if len(buf) != VENDOR_SIZE:
        raise ValueError(f"vendor data must be {VENDOR_SIZE} bytes, got {len(buf)}")
    return VendorData(buf[0], buf[1], buf[2])


def decode_build(buf: bytes) -> str:
    if len(buf) != BUILD_SIZE:
        raise ValueError(f"build info must be {BUILD_SIZE} bytes, got {len(buf)}")
    return buf[35:67].split(b"\0", 1)[0].decode("ascii", "replace")


def is_corrupt(buf: bytes) -> bool:
    """Same rule as the official client: real frames carry a fan duty <= 100
    and zero padding at offsets 11 and 95. Anything else is a desynced read."""
    return len(buf) != SENSOR_SIZE or buf[10] > 100 or buf[11] != 0 or buf[95] != 0


def _temp(raw: int) -> float | None:
    c = raw / 10
    lo, hi = TEMP_VALID_RANGE_C
    return c if lo < c < hi else None


def decode_sensors(buf: bytes) -> dict:
    """Decode a 100-byte sensor frame into engineering units."""
    if len(buf) != SENSOR_SIZE:
        raise ValueError(f"sensor frame must be {SENSOR_SIZE} bytes, got {len(buf)}")
    v = struct.unpack(SENSOR_FMT, buf)
    ts, vdd, fan, raw_pins = v[0:4], v[4], v[5], v[6:24]
    total_mw, total_ma, avg_mv, cap, fault_status, fault_log = v[24:30]
    pins = [
        {"v": raw_pins[i * 3] / 1000, "a": raw_pins[i * 3 + 1] / 1000, "w": raw_pins[i * 3 + 2] / 1000}
        for i in range(PIN_COUNT)
    ]
    return {
        "pins": pins,
        "total_w": total_mw / 1000,
        "total_a": total_ma / 1000,
        "avg_v": avg_mv / 1000,
        "vdd_v": vdd / 1000,
        "temps_c": dict(zip(("in", "out", "ext1", "ext2"), map(_temp, ts))),
        "fan_pct": fan,
        "psu_cap_w": PSU_CAP_W.get(cap, 0),
        "fault_status": fault_status,
        "fault_log": fault_log,
    }


def encode_sensors(
    pins: list[tuple[float, float]],
    temps_c: tuple[float | None, float | None, float | None, float | None] = (30.0, 30.0, 30.0, 30.0),
    vdd_v: float = 3.3,
    fan_pct: int = 0,
    psu_cap_w: int = 600,
    fault_status: int = 0,
    fault_log: int = 0,
) -> bytes:
    """Build a sensor frame from (volts, amps) per pin. Used by the simulator
    and tests so they exercise the same decode path as real hardware."""
    if len(pins) != PIN_COUNT:
        raise ValueError(f"need {PIN_COUNT} pins")
    flat: list[int] = []
    for volts, amps in pins:
        mv, ma = round(volts * 1000), round(amps * 1000)
        flat += [mv, ma, round(mv * ma / 1000)]
    total_mw = sum(flat[2::3])
    total_ma = sum(flat[1::3])
    avg_mv = round(sum(flat[0::3]) / PIN_COUNT)
    return struct.pack(
        SENSOR_FMT,
        *(-32768 if t is None else round(t * 10) for t in temps_c),
        round(vdd_v * 1000),
        fan_pct,
        *flat,
        total_mw,
        total_ma,
        avg_mv,
        PSU_CAP_CODE.get(psu_cap_w, 0),
        fault_status,
        fault_log,
    )


def clear_faults_payload(keep_status_mask: int = 0, keep_log_mask: int = 0) -> bytes:
    return bytes([CMD_CLEAR_FAULTS]) + struct.pack("<HH", keep_status_mask & 0xFFFF, keep_log_mask & 0xFFFF)


def derive(reading: dict) -> dict:
    """Values computed from one reading: hottest pin and pin imbalance
    (max pin current / mean pin current, 1.0 = perfectly balanced)."""
    amps = [p["a"] for p in reading["pins"]]
    max_a = max(amps)
    total_a = sum(amps)
    imbalance = None
    if total_a >= IMBALANCE_MIN_LOAD_A:
        imbalance = round(max_a / (total_a / PIN_COUNT), 4)
    temps = [t for t in reading["temps_c"].values() if t is not None]
    return {"max_pin_a": max_a, "pin_imbalance": imbalance, "max_temp_c": max(temps) if temps else None}
