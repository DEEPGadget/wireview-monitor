from wvd import protocol as P
from wvd.events import EventEngine, Limits
from wvd.samples import build_sample


def sample(seq, amps=1.0, status=0, log=0):
    return build_sample(seq, float(seq), "X", P.encode_sensors([(12.0, amps)] * 6, fault_status=status, fault_log=log))


def test_fault_transitions():
    e = EventEngine(Limits())
    assert e.check(sample(1)) == []
    ev = e.check(sample(2, status=1 << 2, log=1 << 2))
    assert [x["type"] for x in ev] == ["fault.set", "fault.log"] and ev[0]["fault"] == "OCP"
    ev = e.check(sample(3, status=0, log=1 << 2))
    assert [x["type"] for x in ev] == ["fault.clear"]


def test_fault_active_at_connect():
    e = EventEngine(Limits())
    assert [x["type"] for x in e.check(sample(1, status=1))] == ["fault.active"]


def test_limit_hysteresis():
    e = EventEngine(Limits(pin_a=9.5, total_w=None, temp_c=None, imbalance=None))
    assert e.check(sample(1, amps=9.0)) == []
    assert [x["type"] for x in e.check(sample(2, amps=9.6))] == ["limit.exceeded"]
    assert e.check(sample(3, amps=9.4)) == []  # above 95 % of the limit: still over
    assert [x["type"] for x in e.check(sample(4, amps=8.9))] == ["limit.normal"]
