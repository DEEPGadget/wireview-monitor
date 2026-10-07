from wvd import protocol as P
from wvd.samples import build_sample, compute_stats, downsample

# Captured from a real WireView Pro II (FW 20260430_1838) at ~17 W idle load.
REAL_SENSOR = bytes.fromhex(
    "220129012b012b01830d0000ff2f0000d80000005d0a000005300000fb0000000d0c0000ff2f0000e8000000"
    "220b0000ff2f000000010000490c000005300000e2000000da0a00000b300000c200000052090000014200005f05"
    "00000330000000000000")
REAL_BUILD = bytes.fromhex(
    "ef0505546865726d616c204772697a7a6c792057697265566965772050726f2049490054472d57562d50524f322d"
    "46575f32303236303433305f31383338000000000020")


def test_real_frame_decodes():
    assert len(REAL_SENSOR) == P.SENSOR_SIZE and not P.is_corrupt(REAL_SENSOR)
    r = P.decode_sensors(REAL_SENSOR)
    assert r["temps_c"] == {"in": 29.0, "out": 29.7, "ext1": 29.9, "ext2": 29.9}
    assert r["pins"][0] == {"v": 12.287, "a": 0.216, "w": 2.653}
    assert r["psu_cap_w"] == 600 and r["fault_status"] == 0
    # Per-pin power and the device totals agree.
    assert abs(sum(p["w"] for p in r["pins"]) - r["total_w"]) < 0.01
    assert abs(sum(p["a"] for p in r["pins"]) - r["total_a"]) < 0.002


def test_real_build_and_vendor():
    assert P.decode_build(REAL_BUILD) == "TG-WV-PRO2-FW_20260430_1838"
    vd = P.decode_vendor(REAL_BUILD[:3])
    assert vd.supported and vd.hw_rev == "EF05" and vd.edition == "WireView Pro II"
    assert not P.decode_vendor(bytes([0xEF, 0x07, 1])).supported  # WireView II: not this protocol


def test_corrupt_detection():
    bad = bytearray(REAL_SENSOR)
    bad[11] = 1
    assert P.is_corrupt(bytes(bad))
    bad = bytearray(REAL_SENSOR)
    bad[10] = 101
    assert P.is_corrupt(bytes(bad))
    assert P.is_corrupt(REAL_SENSOR[:99])


def test_encode_roundtrip_and_absent_sensor():
    frame = P.encode_sensors([(12.1, 8.0)] * 5 + [(12.1, 2.0)], (40.0, 41.5, None, None),
                             fan_pct=55, fault_status=0b100, fault_log=0b110)
    assert not P.is_corrupt(frame)
    r = P.decode_sensors(frame)
    assert r["temps_c"] == {"in": 40.0, "out": 41.5, "ext1": None, "ext2": None}
    assert P.fault_names(r["fault_status"]) == ["OCP"]
    d = P.derive(r)
    assert d["max_pin_a"] == 8.0 and d["max_temp_c"] == 41.5
    assert d["pin_imbalance"] == round(8.0 / (42.0 / 6), 4)


def test_imbalance_needs_load():
    r = P.decode_sensors(P.encode_sensors([(12.0, 0.1)] * 6))
    assert P.derive(r)["pin_imbalance"] is None


def test_clear_faults_payload():
    assert P.clear_faults_payload(0xFFFB, 0x0001) == bytes([0x0E, 0xFB, 0xFF, 0x01, 0x00])


def _series(watts_list, t0=1000.0, dt=0.1, status=0):
    out = []
    for i, w in enumerate(watts_list):
        amps = w / 12.0 / 6
        frame = P.encode_sensors([(12.0, amps)] * 6, fault_status=status if i == 1 else 0)
        out.append(build_sample(i + 1, t0 + i * dt, "X", frame))
    return out


def test_stats_energy_and_faults():
    s = _series([120.0] * 11, status=1 << 2)  # 1 s at 120 W
    st = compute_stats(s)
    assert st["count"] == 11 and st["gaps"] == 0
    assert abs(st["energy_wh"] - 120 / 3600) < 1e-4
    assert st["faults_seen"] == ["OCP"]
    assert abs(st["fields"]["total_w"]["max"] - 120.0) < 0.05


def test_stats_skip_gaps():
    s = _series([100.0] * 4)
    s[2]["ts"] += 10
    s[3]["ts"] += 10
    assert compute_stats(s)["gaps"] == 1


def test_downsample_keeps_peak_and_faults():
    s = _series([100.0, 100.0, 400.0, 100.0], dt=0.1, status=1)
    ds = downsample(s, 10.0)
    assert len(ds) == 1 and ds[0]["n"] == 4
    assert ds[0]["derived"]["max_pin_a"] == max(x["derived"]["max_pin_a"] for x in s)
    assert ds[0]["faults"] == ["OTP_TCHIP"]
