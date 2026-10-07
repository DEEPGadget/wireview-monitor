"""Turns the sample stream into events: fault bits, limit crossings, connection."""
from __future__ import annotations

from dataclasses import asdict, dataclass

from . import protocol as P

# A limit clears once the value falls this far below it, so a value hovering
# at the limit does not flood the event log.
HYSTERESIS = 0.95


@dataclass
class Limits:
    """Warning thresholds shared by the dashboard, the event log and `wvctl assert`.
    None disables a limit."""
    pin_a: float | None = 9.5
    total_w: float | None = 600.0
    temp_c: float | None = 80.0
    imbalance: float | None = 1.5

    def as_dict(self) -> dict:
        return asdict(self)


# limit name -> how to read the value from a sample
LIMIT_SOURCES = {
    "pin_a": lambda s: s["derived"]["max_pin_a"],
    "total_w": lambda s: s["total_w"],
    "temp_c": lambda s: s["derived"]["max_temp_c"],
    "imbalance": lambda s: s["derived"]["pin_imbalance"],
}


class EventEngine:
    def __init__(self, limits: Limits):
        self.limits = limits
        self._status: int | None = None
        self._log: int | None = None
        self._over: set[str] = set()

    def reset(self) -> None:
        """Forget the previous sample (new connection): the next one is a baseline."""
        self._status = self._log = None
        self._over.clear()

    def check(self, s: dict) -> list[dict]:
        ev: list[dict] = []
        ts = s["ts"]
        status, log = s["fault_status"], s["fault_log"]
        if self._status is not None:
            for bit, name in enumerate(P.FAULTS):
                m = 1 << bit
                if status & m and not self._status & m:
                    ev.append({"ts": ts, "type": "fault.set", "fault": name, "label": P.FAULT_LABELS[name],
                               "seq": s["seq"]})
                elif self._status & m and not status & m:
                    ev.append({"ts": ts, "type": "fault.clear", "fault": name, "label": P.FAULT_LABELS[name],
                               "seq": s["seq"]})
        elif status:
            for name in P.fault_names(status):
                ev.append({"ts": ts, "type": "fault.active", "fault": name, "label": P.FAULT_LABELS[name],
                           "seq": s["seq"]})
        if self._log is not None and log != self._log:
            ev.append({"ts": ts, "type": "fault.log", "fault_log": P.fault_names(log), "seq": s["seq"]})
        self._status, self._log = status, log

        for name, read in LIMIT_SOURCES.items():
            limit = getattr(self.limits, name)
            value = read(s)
            if limit is None or value is None:
                continue
            if name not in self._over and value > limit:
                self._over.add(name)
                ev.append({"ts": ts, "type": "limit.exceeded", "limit": name, "value": value,
                           "threshold": limit, "seq": s["seq"]})
            elif name in self._over and value < limit * HYSTERESIS:
                self._over.discard(name)
                ev.append({"ts": ts, "type": "limit.normal", "limit": name, "value": value,
                           "threshold": limit, "seq": s["seq"]})
        return ev
