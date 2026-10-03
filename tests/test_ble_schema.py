from __future__ import annotations

import pytest

from moto_server import ble_schema
from moto_server.decode import V4_EXTRA_HEADER, csv_column_for_field
from moto_server.defs import ble as defs_ble

SCHEMA = ble_schema.load_default_schema()

V3_BYTES = defs_ble.TOTAL_BYTES_BY_VERSION[3]
V4_BYTES = defs_ble.TOTAL_BYTES_BY_VERSION[4]


def _pack_v3(**overrides) -> bytes:
    values = {
        "version": 3,
        "seq": 5,
        "deviceTimeMs": 123456,
        "rpm": 3000,
        "speed": 80,
        "coolantTemp": 90,
        "throttlePos": 45,
        "batteryVolt": 12400,
        "leanAngle": -32768,
        "maxLeanRight": -32768,
        "maxLeanLeft": -32768,
        "flags": 0b11011111,
        "rpmAgeMs": 10,
        "speedAgeMs": 20,
        "coolantTempAgeMs": 30,
        "throttlePosAgeMs": 40,
        "batteryVoltAgeMs": 50,
        "canBusState": 1,
        "canTxErrorCount": 2,
        "canRxErrorCount": 3,
        "canBusOffCount": 0,
        "unansweredDidCount": 1,
        "canFlags": 1,
    }
    values.update(overrides)
    return ble_schema.pack_fields(values, defs_ble.telemetry_fields(3), V3_BYTES)


def _pack_v4(**overrides) -> bytes:
    values = {
        "stepGapMaxMs": 12,
        "stepGapOverCount": 1,
        "rttDid": 0xF40C,
        "rttMinMs": 4,
        "rttMaxMs": 11,
        "rttSumMs": 100,
        "rttCount": 14,
        "rttNrc78Count": 2,
    }
    # Everything else is the v3 body, so v3/v4 differ only in the version byte and the tail.
    values.update({"version": 4, "seq": 6, "deviceTimeMs": 123556, "rpm": 3100, **overrides})
    base = {
        "speed": 80,
        "coolantTemp": 90,
        "throttlePos": 45,
        "batteryVolt": 12400,
        "leanAngle": -32768,
        "maxLeanRight": -32768,
        "maxLeanLeft": -32768,
        "flags": 0b11011111,
        "rpmAgeMs": 10,
        "speedAgeMs": 20,
        "coolantTempAgeMs": 30,
        "throttlePosAgeMs": 40,
        "batteryVoltAgeMs": 50,
        "canBusState": 1,
        "canTxErrorCount": 2,
        "canRxErrorCount": 3,
        "canBusOffCount": 0,
        "unansweredDidCount": 1,
        "canFlags": 1,
    }
    return ble_schema.pack_fields({**base, **values}, defs_ble.telemetry_fields(4), V4_BYTES)


def _pack_v2(**overrides) -> bytes:
    values = {
        "version": 2,
        "seq": 7,
        "rpm": 2500,
        "speed": 50,
        "coolantTemp": 80,
        "throttlePos": 20,
        "batteryVolt": 12000,
        "leanAngle": -32768,
        "maxLeanRight": -32768,
        "maxLeanLeft": -32768,
        "flags": 0b00111111,
    }
    values.update(overrides)
    return ble_schema.pack_fields(
        values, SCHEMA["lowMtuFallback"]["fields"], SCHEMA["lowMtuFallback"]["totalBytes"]
    )


def test_decode_telemetry_v3_physical_values():
    decoded = ble_schema.decode_telemetry(_pack_v3())
    assert decoded["version"] == 3
    assert decoded["seq"] == 5
    assert decoded["device_time_ms"] == 123456
    assert decoded["physical"]["ENGINE_SPEED"] == 3000
    assert decoded["physical"]["VEHICLE_SPEED"] == 80
    assert decoded["physical"]["COOLANT_TEMP"] == 90
    assert decoded["physical"]["THROTTLE_POS"] == 45
    assert decoded["physical"]["BATTERY_VOLTAGE"] == pytest.approx(12.4)
    assert decoded["ages_ms"]["ENGINE_SPEED"] == 10
    assert decoded["flags"]["rpmValid"] is True
    assert decoded["flags"]["ecuPresent"] is True
    assert decoded["can_health"]["bus_state"] == "running"
    assert decoded["can_health"]["tec"] == 2


def test_decode_telemetry_v3_flags_and_can_flags_decode_bits():
    decoded = ble_schema.decode_telemetry(_pack_v3(flags=0b10000001, canFlags=0b00000101))
    assert decoded["flags"]["rpmValid"] is True
    assert decoded["flags"]["speedValid"] is False
    assert decoded["flags"]["imuActive"] is True
    assert decoded["can_health"]["flags"]["pollerEnabled"] is True
    assert decoded["can_health"]["flags"]["latchedBusOff"] is True
    assert decoded["can_health"]["flags"]["latchedForeignTester"] is False


def test_decode_telemetry_v2_layout_has_no_device_time_or_can_health():
    decoded = ble_schema.decode_telemetry(_pack_v2())
    assert decoded["version"] == 2
    assert decoded["device_time_ms"] is None
    assert decoded["can_health"] is None
    assert decoded["ages_ms"] == {}
    assert decoded["physical"]["VEHICLE_SPEED"] == 50
    assert decoded["physical"]["BATTERY_VOLTAGE"] == pytest.approx(12.0)


def test_decode_telemetry_unknown_version_rejected():
    raw = bytearray(_pack_v2())
    raw[0] = 99
    with pytest.raises(ble_schema.DecodeError, match="unsupported telemetry packet version"):
        ble_schema.decode_telemetry(bytes(raw))


def test_decode_telemetry_v3_has_no_tester_stats():
    # v3 uses only the fields without sinceVersion 4: a 37-byte packet decodes.
    assert len(_pack_v3()) == V3_BYTES == 37
    decoded = ble_schema.decode_telemetry(_pack_v3())
    assert decoded["tester_stats"] is None
    assert not any(name in decoded["raw"] for name in ("stepGapMaxMs", "rttDid", "rttCount"))


def test_decode_telemetry_v4_round_trip():
    raw = _pack_v4()
    assert len(raw) == V4_BYTES == 57
    decoded = ble_schema.decode_telemetry(raw)
    assert decoded["version"] == 4
    assert decoded["seq"] == 6
    assert decoded["device_time_ms"] == 123556
    assert decoded["physical"]["ENGINE_SPEED"] == 3100
    assert decoded["can_health"]["bus_state"] == "running"
    assert decoded["tester_stats"] == {
        "stepGapMaxMs": 12,
        "stepGapOverCount": 1,
        "rttDid": 0xF40C,
        "rttMinMs": 4,
        "rttMaxMs": 11,
        "rttSumMs": 100,
        "rttCount": 14,
        "rttNrc78Count": 2,
    }


def test_decode_telemetry_v4_uint32_fields_hold_full_range():
    decoded = ble_schema.decode_telemetry(_pack_v4(rttSumMs=0xFFFFFFFF, rttCount=0xFFFFFFFE))
    assert decoded["tester_stats"]["rttSumMs"] == 0xFFFFFFFF
    assert decoded["tester_stats"]["rttCount"] == 0xFFFFFFFE


def test_decode_telemetry_wrong_length_rejected():
    raw = _pack_v3()[:-1]
    with pytest.raises(ble_schema.DecodeError, match="expected 37 bytes"):
        ble_schema.decode_telemetry(raw)


def test_decode_telemetry_v4_wrong_length_rejected():
    with pytest.raises(ble_schema.DecodeError, match="telemetry v4: expected 57 bytes, got 56"):
        ble_schema.decode_telemetry(_pack_v4()[:-1])
    with pytest.raises(ble_schema.DecodeError, match="expected 57 bytes, got 58"):
        ble_schema.decode_telemetry(_pack_v4() + b"\x00")
    # A v4 version byte on a v3-sized payload is also a length error, not a v3 decode.
    raw = bytearray(_pack_v3())
    raw[0] = 4
    with pytest.raises(ble_schema.DecodeError, match="expected 57 bytes, got 37"):
        ble_schema.decode_telemetry(bytes(raw))


def test_decode_telemetry_v2_wrong_length_rejected():
    with pytest.raises(ble_schema.DecodeError, match="telemetry v2: expected 16 bytes"):
        ble_schema.decode_telemetry(_pack_v2() + b"\x00")


def test_accepted_versions_come_from_the_schema():
    assert ble_schema.accepted_versions(SCHEMA) == list(defs_ble.ACCEPTED_VERSIONS) == [2, 3, 4]
    raw = bytearray(_pack_v4())
    raw[0] = 5
    with pytest.raises(ble_schema.DecodeError, match="unsupported telemetry packet version: 5"):
        ble_schema.decode_telemetry(bytes(raw))


def test_v4_csv_columns_match_the_schema_tester_fields():
    names = [f["name"] for f in ble_schema.tester_stat_fields(SCHEMA)]
    assert [csv_column_for_field(n) for n in names] == V4_EXTRA_HEADER


def test_decode_telemetry_empty_payload_rejected():
    with pytest.raises(ble_schema.DecodeError, match="empty"):
        ble_schema.decode_telemetry(b"")


def _pack_imu_block(sample_count: int = 2, first_sample_index: int = 65534) -> bytes:
    imu = SCHEMA["imuBlock"]
    header = {
        "version": 1,
        "seq": 3,
        "deviceTimeMs": 1000,
        "firstSampleIndex": first_sample_index,
        "sampleCount": sample_count,
        "samplePeriodMs": 10,
        "flags": 0b00000001,
        "reserved": 0,
    }
    buf = bytearray(imu["headerBytes"] + sample_count * imu["sampleBytes"])
    buf[: imu["headerBytes"]] = ble_schema.pack_fields(
        header, imu["headerFields"], imu["headerBytes"]
    )
    for i in range(sample_count):
        sample = {"ax": 100 + i, "ay": -50, "az": 4096, "gx": 10, "gy": -10, "gz": 5}
        offset = imu["headerBytes"] + i * imu["sampleBytes"]
        buf[offset : offset + imu["sampleBytes"]] = ble_schema.pack_fields(
            sample, imu["sampleFields"], imu["sampleBytes"]
        )
    return bytes(buf)


def test_decode_imu_block_samples_and_scale_and_wrap():
    raw = _pack_imu_block(sample_count=3, first_sample_index=65534)
    decoded = ble_schema.decode_imu_block(raw)
    assert decoded["sample_count"] == 3
    assert decoded["flags"]["deviceOverflow"] is True
    assert decoded["flags"]["readError"] is False
    indices = [s["sample_index"] for s in decoded["samples"]]
    # wraps past 65535 back to 0, 1
    assert indices == [65534, 65535, 0]
    first = decoded["samples"][0]
    assert first["ax_g"] == pytest.approx(100 / 4096)
    assert first["gx_dps"] == pytest.approx(10 / 65.5)


def test_decode_imu_block_sensor_reconfigured_flag():
    imu = SCHEMA["imuBlock"]
    header = {
        "version": 1,
        "seq": 1,
        "deviceTimeMs": 0,
        "firstSampleIndex": 0,
        "sampleCount": 1,
        "samplePeriodMs": 10,
        "flags": 0b00000100,
        "reserved": 0,
    }
    buf = bytearray(imu["headerBytes"] + imu["sampleBytes"])
    buf[: imu["headerBytes"]] = ble_schema.pack_fields(
        header, imu["headerFields"], imu["headerBytes"]
    )
    sample = {"ax": 0, "ay": 0, "az": 4096, "gx": 0, "gy": 0, "gz": 0}
    buf[imu["headerBytes"] :] = ble_schema.pack_fields(
        sample, imu["sampleFields"], imu["sampleBytes"]
    )

    decoded = ble_schema.decode_imu_block(bytes(buf))
    assert decoded["flags"]["sensorReconfigured"] is True
    assert decoded["flags"]["deviceOverflow"] is False


def test_decode_imu_block_wrong_length_rejected():
    raw = _pack_imu_block(sample_count=2)[:-1]
    with pytest.raises(ble_schema.DecodeError, match="imu block"):
        ble_schema.decode_imu_block(raw)


def test_decode_imu_block_unknown_version_rejected():
    raw = bytearray(_pack_imu_block())
    raw[0] = 9
    with pytest.raises(ble_schema.DecodeError, match="unsupported IMU block version"):
        ble_schema.decode_imu_block(bytes(raw))
