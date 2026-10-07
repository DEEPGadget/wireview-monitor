"""Sample construction, flattening, statistics and downsampling.

A sample is the JSON shape served on every channel (REST, WS/SSE, CLI):
the decoded frame plus ts, seq, device, active fault names and derived values.
"""
from __future__ import annotations

import math

from . import protocol as P

TEMP_KEYS = ("in", "out", "ext1", "ext2")



def build_sample(seq: int, ts: float, device: str, frame: bytes, gap_s: float | None = None) -> dict:
    """gap_s: seconds since the previous sample (None for the first one)."""
    reading = P.decode_sensors(frame)
    return {
        "ts": ts,
        "seq": seq,
        "gap_s": gap_s,
        "device": device,
        **reading,
        "faults": P.fault_names(reading["fault_status"]),
        "derived": P.derive(reading),
    }


def flatten(s: dict) -> dict:
    """One flat row per sample: CSV columns and stats field names."""
    row = {
        "ts": s["ts"], "seq": s["seq"],
        "total_w": s["total_w"], "total_a": s["total_a"], "avg_v": s["avg_v"], "vdd_v": s["vdd_v"],
        "fan_pct": s["fan_pct"], "psu_cap_w": s["psu_cap_w"],
        "max_pin_a": s["derived"]["max_pin_a"], "pin_imbalance": s["derived"]["pin_imbalance"],
        "max_temp_c": s["derived"]["max_temp_c"],
    }
    for k in TEMP_KEYS:
        row[f"temp_{k}_c"] = s["temps_c"][k]
    for i, p in enumerate(s["pins"], 1):
        row[f"pin{i}_v"], row[f"pin{i}_a"], row[f"pin{i}_w"] = p["v"], p["a"], p["w"]
    row["fault_status"] = s["fault_status"]
    row["fault_log"] = s["fault_log"]
    row["gap_s"] = s.get("gap_s")
    return row


STAT_FIELDS = (
    ["total_w", "total_a", "avg_v", "max_pin_a", "pin_imbalance", "max_temp_c", "fan_pct", "vdd_v"]
    + [f"temp_{k}_c" for k in TEMP_KEYS]
    + [f"pin{i}_{q}" for i in range(1, P.PIN_COUNT + 1) for q in ("v", "a", "w")]
)

# Consecutive samples further apart than this are a gap: not integrated, counted.
GAP_S = 2.0


def _percentile(sorted_vals: list[float], q: float) -> float:
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


# Values kept per field for p95; longer windows keep every n-th value.
P95_KEEP = 200_000


class StatsAccumulator:
    """compute_stats over a window fed chunk by chunk, in constant memory apart
    from the p95 reservoir. min/max/avg/energy are exact; p95 is exact up to
    P95_KEEP samples and taken from evenly strided values above that."""

    def __init__(self, expected: int = 0):
        self.stride = max(1, -(-expected // P95_KEEP))
        self.count = 0
        self.first = self.last = None
        self.n = dict.fromkeys(STAT_FIELDS, 0)
        self.sum = dict.fromkeys(STAT_FIELDS, 0.0)
        self.min: dict = dict.fromkeys(STAT_FIELDS)
        self.max: dict = dict.fromkeys(STAT_FIELDS)
        self.lastv: dict = dict.fromkeys(STAT_FIELDS)
        self.keep: dict[str, list] = {f: [] for f in STAT_FIELDS}
        self.energy_j = 0.0
        self.gaps = 0
        self.max_gap_s = 0.0
        self.seen = 0

    def add(self, samples: list[dict]) -> None:
        for s in samples:
            r = flatten(s)
            keep = self.count % self.stride == 0
            for f in STAT_FIELDS:
                v = r[f]
                if v is None:
                    continue
                if self.n[f] == 0 or v < self.min[f]:
                    self.min[f] = v
                if self.n[f] == 0 or v > self.max[f]:
                    self.max[f] = v
                self.n[f] += 1
                self.sum[f] += v
                self.lastv[f] = v
                if keep:
                    self.keep[f].append(v)
            if self.last is not None:
                dt = r["ts"] - self.last["ts"]
                self.max_gap_s = max(self.max_gap_s, dt)
                if dt > GAP_S:
                    self.gaps += 1
                else:
                    self.energy_j += (self.last["total_w"] + r["total_w"]) / 2 * dt
            else:
                self.first = r
            self.seen |= r["fault_status"]
            self.last = r
            self.count += 1

    def result(self) -> dict:
        if not self.count:
            return {"count": 0, "from": None, "to": None, "duration_s": 0.0, "rate_hz": 0.0,
                    "energy_wh": 0.0, "gaps": 0, "max_gap_s": 0.0, "faults_seen": [], "fault_log_end": [],
                    "fields": {}}
        fields = {}
        for f in STAT_FIELDS:
            if not self.n[f]:
                fields[f] = None
                continue
            fields[f] = {
                "min": self.min[f], "max": self.max[f], "avg": round(self.sum[f] / self.n[f], 6),
                "p95": round(_percentile(sorted(self.keep[f]) or [self.lastv[f]], 0.95), 6), "last": self.lastv[f],
            }
        duration = self.last["ts"] - self.first["ts"]
        out = {
            "count": self.count,
            "from": self.first["ts"],
            "to": self.last["ts"],
            "duration_s": round(duration, 3),
            "rate_hz": round((self.count - 1) / duration, 2) if duration > 0 else 0.0,
            "energy_wh": round(self.energy_j / 3600, 6),
            "gaps": self.gaps,
            "max_gap_s": round(self.max_gap_s, 4),
            "faults_seen": P.fault_names(self.seen),
            "fault_log_end": P.fault_names(self.last["fault_log"]),
            "fields": fields,
        }
        if self.stride > 1:
            out["p95_stride"] = self.stride
        return out


def compute_stats(samples: list[dict]) -> dict:
    """min/max/avg/p95/last per field, energy, faults and gaps over a window."""
    acc = StatsAccumulator(len(samples))
    acc.add(samples)
    return acc.result()


def _avg(vals: list) -> float | None:
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


def downsample(samples: list[dict], step_s: float) -> list[dict]:
    """Average samples into step_s buckets, keeping the sample shape. Fault
    masks are OR-ed so a short fault inside a bucket is not lost."""
    if step_s <= 0 or not samples:
        return samples
    out: list[dict] = []
    bucket: list[dict] = []
    key = None
    for s in samples:
        k = math.floor(s["ts"] / step_s)
        if key is not None and k != key:
            out.append(_merge(bucket))
            bucket = []
        key = k
        bucket.append(s)
    if bucket:
        out.append(_merge(bucket))
    return out


def _merge(b: list[dict]) -> dict:
    if len(b) == 1:
        return b[0]
    last = b[-1]
    status = log = 0
    for s in b:
        status |= s["fault_status"]
        log |= s["fault_log"]
    reading = {
        "pins": [{q: _avg([s["pins"][i][q] for s in b]) for q in ("v", "a", "w")} for i in range(P.PIN_COUNT)],
        "total_w": _avg([s["total_w"] for s in b]),
        "total_a": _avg([s["total_a"] for s in b]),
        "avg_v": _avg([s["avg_v"] for s in b]),
        "vdd_v": _avg([s["vdd_v"] for s in b]),
        "temps_c": {k: _avg([s["temps_c"][k] for s in b]) for k in TEMP_KEYS},
        "fan_pct": round(_avg([s["fan_pct"] for s in b])),
        "psu_cap_w": last["psu_cap_w"],
        "fault_status": status,
        "fault_log": log,
    }
    derived = P.derive(reading)
    # Peak, not average, of the hottest pin: a bucket must not hide a spike.
    derived["max_pin_a"] = max(s["derived"]["max_pin_a"] for s in b)
    return {"ts": last["ts"], "seq": last["seq"], "gap_s": max((s.get("gap_s") or 0) for s in b) or None,
            "device": last["device"], **reading,
            "faults": P.fault_names(status), "derived": derived, "n": len(b)}
