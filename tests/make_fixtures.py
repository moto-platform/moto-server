#!/usr/bin/env python3
"""Generates the committed test fixture sessions under tests/fixtures/.

Builds raw_hex telemetry rows FROM THE SCHEMA (ble_schema.pack_fields), never
from hand-written byte offsets, so the fixtures exercise the same code path
the decoder uses. Run this again (and commit the result) whenever the fixture
scenarios need to change:

    uv run python tests/make_fixtures.py
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from moto_server import ble_schema

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
SCHEMA = ble_schema.load_default_schema()


def bits_to_int(bit_defs: list[dict], set_names: set[str]) -> int:
    value = 0
    for bit in bit_defs:
        if bit["name"] in set_names:
            value |= 1 << bit["bit"]
    return value


def write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# v2 session: 16-byte raw_hex rows, two lost-packet gaps, one stale (invalid) value.
# ---------------------------------------------------------------------------

V2_SESSION_ID = "20260115-120000-a1b2"

FLAG_BITS = SCHEMA["flags"]["bits"]
V2_ALL_VALID = {
    "rpmValid",
    "speedValid",
    "coolantTempValid",
    "throttlePosValid",
    "batteryVoltValid",
    "ecuPresent",
}


def make_v2_fixture() -> None:
    out_dir = FIXTURES_DIR / "v2_session"
    out_dir.mkdir(parents=True, exist_ok=True)
    fields = SCHEMA["lowMtuFallback"]["fields"]
    total_bytes = SCHEMA["lowMtuFallback"]["totalBytes"]

    # seq sequence has two single-packet gaps: 0,1,2,4,5,7,8 (lost after seq=2 and seq=5).
    seqs = [0, 1, 2, 4, 5, 7, 8]
    stale_row_index = 3  # seq=4: battery is stale (invalid), value kept from before.

    telemetry_rows = []
    for i, seq in enumerate(seqs):
        rpm = 3000 + i * 10
        speed = 60
        coolant = 85
        tps = 30
        battery_raw = 12400  # 12.4 V
        valid_names = set(V2_ALL_VALID)
        if i == stale_row_index:
            valid_names.discard("batteryVoltValid")
        flags_value = bits_to_int(FLAG_BITS, valid_names)

        values = {
            "version": 2,
            "seq": seq,
            "rpm": rpm,
            "speed": speed,
            "coolantTemp": coolant,
            "throttlePos": tps,
            "batteryVolt": battery_raw,
            "leanAngle": -32768,
            "maxLeanRight": -32768,
            "maxLeanLeft": -32768,
            "flags": flags_value,
        }
        raw = ble_schema.pack_fields(values, fields, total_bytes)
        rx_mono_ms = 1_000 + i * 100

        telemetry_rows.append(
            [
                f"2026-01-15T12:00:{i:02d}.000Z",
                str(rx_mono_ms),
                str(seq),
                "0" if i == 0 else str(seq - seqs[i - 1] - 1),
                raw.hex(),
                str(rpm),
                str(speed),
                str(coolant),
                str(tps),
                f"{battery_raw / 1000:.3f}",
                "",
                "",
                "",  # lean fields, deprecated, always empty for v2 fixture output
                "1",
                "1",
                "1",
                "1",
                "0" if i == stale_row_index else "1",
                "",
                "1",
                "",
            ]
        )

    write_csv(out_dir / "telemetry.csv", _v2_telemetry_header(), telemetry_rows)
    write_csv(
        out_dir / "events.csv",
        ["rx_utc_iso", "rx_mono_ms", "event", "detail"],
        [
            ["2026-01-15T12:00:00.000Z", "1000", "recording_started", ""],
            ["2026-01-15T12:00:02.200Z", "1200", "packet_gap", "lost=1"],
            ["2026-01-15T12:00:05.500Z", "1500", "packet_gap", "lost=1"],
            [
                f"2026-01-15T12:00:{len(seqs):02d}.000Z",
                str(1000 + len(seqs) * 100),
                "recording_stopped",
                "",
            ],
        ],
    )

    meta = {
        "session_id": V2_SESSION_ID,
        "created_utc": "2026-01-15T12:00:00.000Z",
        "rider_name": "test-rider",
        "rider_weight_kg": 75,
        "extra_load_kg": 0,
        "ambient_temp_c": 18,
        "weather": "dry",
        "tire_pressure_front_bar": 2.2,
        "tire_pressure_rear_bar": 2.5,
        "fuel_level": 0.8,
        "vehicle_config": "stock",
        "condition_label": "healthy",
        "route_type": "urban",
        "note": "v2 fixture: two single-packet gaps, one stale battery reading",
        "app_version": "1.4.0",
        "ble_schema_version": 2,
        "device_name": "CL250-fixture",
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")

    lost = sum((seqs[i] - seqs[i - 1] - 1) for i in range(1, len(seqs)))
    summary = {
        "duration_ms": 1000 + len(seqs) * 100,
        "packet_count": len(seqs),
        "lost_count": lost,
        "loss_percent": round(lost / (lost + len(seqs)) * 100, 4),
        "decode_error_count": 0,
        "disconnect_count": 0,
        "first_packet_utc": "2026-01-15T12:00:00.000Z",
        "last_packet_utc": f"2026-01-15T12:00:{len(seqs):02d}.000Z",
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")


def _v2_telemetry_header() -> list[str]:
    return [
        "rx_utc_iso",
        "rx_mono_ms",
        "seq",
        "lost_since_prev",
        "raw_hex",
        "rpm",
        "speed_kmh",
        "coolant_c",
        "tps_pct",
        "battery_v",
        "lean_deg",
        "max_lean_right_deg",
        "max_lean_left_deg",
        "rpm_valid",
        "speed_valid",
        "coolant_valid",
        "tps_valid",
        "battery_valid",
        "lean_valid",
        "ecu_present",
        "decode_error",
    ]


# ---------------------------------------------------------------------------
# v3 session: 37-byte raw_hex rows with changing CAN health, one out-of-range
# value, plus imu.csv with a missing-sample gap that wraps sample_index.
# ---------------------------------------------------------------------------

V3_SESSION_ID = "20260115-130000-c3d4"

CAN_STATE_BY_NAME = {v["name"]: v["value"] for v in SCHEMA["canHealth"]["busState"]["values"]}
CAN_FLAG_BITS = SCHEMA["canHealth"]["canFlags"]["bits"]


def make_v3_fixture() -> None:
    out_dir = FIXTURES_DIR / "v3_session"
    out_dir.mkdir(parents=True, exist_ok=True)
    fields = SCHEMA["fields"]
    total_bytes = SCHEMA["totalBytes"]

    # seq has one single-packet gap: 10,11,13,14,15 (lost after seq=11).
    seqs = [10, 11, 13, 14, 15]
    out_of_range_row_index = 2  # coolant below DIDS min (-40 degC)

    can_rows = [
        {
            "state": "running",
            "tec": 0,
            "rec": 0,
            "bus_off": 0,
            "unanswered": 0,
            "flags": {"pollerEnabled"},
        },
        {
            "state": "errorWarning",
            "tec": 100,
            "rec": 50,
            "bus_off": 0,
            "unanswered": 0,
            "flags": {"pollerEnabled"},
        },
        {
            "state": "busOff",
            "tec": 255,
            "rec": 255,
            "bus_off": 1,
            "unanswered": 2,
            "flags": {"pollerEnabled"},
        },
        {
            "state": "stopped",
            "tec": 255,
            "rec": 255,
            "bus_off": 1,
            "unanswered": 2,
            "flags": {"pollerEnabled", "latchedBusOff"},
        },
        {
            "state": "errorWarning",
            "tec": 20,
            "rec": 15,
            "bus_off": 1,
            "unanswered": 2,
            "flags": {"pollerEnabled"},
        },
    ]

    telemetry_rows = []
    for i, seq in enumerate(seqs):
        rpm = 4000 + i * 5
        speed = 90
        coolant = -100 if i == out_of_range_row_index else 88
        tps = 40
        battery_raw = 12600
        device_time_ms = 5_000 + i * 100
        can = can_rows[i]

        values = {
            "version": 3,
            "seq": seq,
            "deviceTimeMs": device_time_ms,
            "rpm": rpm,
            "speed": speed,
            "coolantTemp": coolant,
            "throttlePos": tps,
            "batteryVolt": battery_raw,
            "leanAngle": -32768,
            "maxLeanRight": -32768,
            "maxLeanLeft": -32768,
            "flags": bits_to_int(
                FLAG_BITS,
                {
                    "rpmValid",
                    "speedValid",
                    "coolantTempValid",
                    "throttlePosValid",
                    "batteryVoltValid",
                    "ecuPresent",
                    "imuActive",
                },
            ),
            "rpmAgeMs": 10,
            "speedAgeMs": 15,
            "coolantTempAgeMs": 200,
            "throttlePosAgeMs": 20,
            "batteryVoltAgeMs": 400,
            "canBusState": CAN_STATE_BY_NAME[can["state"]],
            "canTxErrorCount": can["tec"],
            "canRxErrorCount": can["rec"],
            "canBusOffCount": can["bus_off"],
            "unansweredDidCount": can["unanswered"],
            "canFlags": bits_to_int(CAN_FLAG_BITS, can["flags"]),
        }
        raw = ble_schema.pack_fields(values, fields, total_bytes)
        rx_mono_ms = 5_000 + i * 100

        telemetry_rows.append(
            [
                f"2026-01-15T13:00:{i:02d}.000Z",
                str(rx_mono_ms),
                str(seq),
                "0" if i == 0 else str(seq - seqs[i - 1] - 1),
                raw.hex(),
                str(rpm),
                str(speed),
                str(coolant),
                str(tps),
                f"{battery_raw / 1000:.3f}",
                "",
                "",
                "",  # deprecated lean columns
                "1",
                "1",
                "1",
                "1",
                "1",
                "",
                "1",
                "",
                "3",
                str(device_time_ms),
                "10",
                "15",
                "200",
                "20",
                "400",
                "1",
                str(values["canBusState"]),
                str(can["tec"]),
                str(can["rec"]),
                str(can["bus_off"]),
                str(can["unanswered"]),
                str(values["canFlags"]),
            ]
        )

    write_csv(out_dir / "telemetry.csv", _v3_telemetry_header(), telemetry_rows)
    write_csv(
        out_dir / "events.csv",
        ["rx_utc_iso", "rx_mono_ms", "event", "detail"],
        [
            ["2026-01-15T13:00:00.000Z", "5000", "recording_started", ""],
            ["2026-01-15T13:00:00.000Z", "5000", "packet_version", "version=3"],
            [
                "2026-01-15T13:00:01.000Z",
                "5100",
                "can_health",
                "state=errorWarning;tec=100;rec=50;bus_off=0;flags=1",
            ],
            [
                "2026-01-15T13:00:02.000Z",
                "5200",
                "can_health",
                "state=busOff;tec=255;rec=255;bus_off=1;flags=1",
            ],
            ["2026-01-15T13:00:02.000Z", "5200", "did_unanswered", "count=2;delta=2"],
            [
                "2026-01-15T13:00:03.000Z",
                "5300",
                "can_health",
                "state=stopped;tec=255;rec=255;bus_off=1;flags=5",
            ],
            ["2026-01-15T13:00:04.000Z", "5400", "recording_stopped", ""],
        ],
    )

    make_v3_imu_csv(out_dir / "imu.csv")

    meta = {
        "session_id": V3_SESSION_ID,
        "created_utc": "2026-01-15T13:00:00.000Z",
        "rider_name": "test-rider",
        "rider_weight_kg": 75,
        "extra_load_kg": 5,
        "ambient_temp_c": 20,
        "weather": "dry",
        "tire_pressure_front_bar": 2.2,
        "tire_pressure_rear_bar": 2.5,
        "fuel_level": 0.6,
        "vehicle_config": "stock",
        "condition_label": "fault:cornering-can-health",
        "route_type": "closed course",
        "note": "v3 fixture: CAN health changes, one out-of-range coolant reading, "
        "IMU block gap wrapping sample_index",
        "app_version": "2.0.0",
        "ble_schema_version": 3,
        "device_name": "CL250-fixture",
        "imu_block_version": 1,
        "requested_mtu": 185,
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")

    lost = sum(seqs[i] - seqs[i - 1] - 1 for i in range(1, len(seqs)))
    summary = {
        "duration_ms": 5000 + len(seqs) * 100,
        "packet_count": len(seqs),
        "lost_count": lost,
        "loss_percent": round(lost / (lost + len(seqs)) * 100, 4),
        "decode_error_count": 0,
        "disconnect_count": 0,
        "first_packet_utc": "2026-01-15T13:00:00.000Z",
        "last_packet_utc": f"2026-01-15T13:00:{len(seqs):02d}.000Z",
        "packet_versions": {"3": len(seqs)},
        "imu_block_count": 2,
        "imu_sample_count": 10,
        "imu_missing_samples": 4,
        "imu_loss_percent": round(4 / 14 * 100, 4),
        "mtu": 185,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")


def _v3_telemetry_header() -> list[str]:
    return _v2_telemetry_header() + [
        "packet_version",
        "device_time_ms",
        "rpm_age_ms",
        "speed_age_ms",
        "coolant_age_ms",
        "tps_age_ms",
        "battery_age_ms",
        "imu_active",
        "can_bus_state",
        "can_tec",
        "can_rec",
        "can_bus_off_count",
        "unanswered_did_count",
        "can_flags",
    ]


IMU_FLAG_BITS = SCHEMA["imuBlock"]["flags"]["bits"]


def make_v3_imu_csv(path: Path) -> None:
    """Two 5-sample blocks; block B starts after a 4-sample gap that wraps the
    uint16 sample_index counter (65530..65534, gap, then 3..7). Block B also
    carries `sensorReconfigured` (the node re-initialized the IMU after
    losing its configuration), to exercise the report's IMU flag counters."""
    accel_lsb = SCHEMA["imuBlock"]["scale"]["accel"]["lsbPerUnit"]
    gyro_lsb = SCHEMA["imuBlock"]["scale"]["gyro"]["lsbPerUnit"]
    sample_period_ms = SCHEMA["imuBlock"]["samplePeriodMs"]

    rows = []
    blocks = [
        {
            "block_seq": 0,
            "first_sample_index": 65530,
            "device_time_ms": 1000,
            "missing": 0,
            "flags": set(),
        },
        {
            "block_seq": 1,
            "first_sample_index": 3,
            "device_time_ms": 1090,
            "missing": 4,
            "flags": {"sensorReconfigured"},
        },
    ]
    for block in blocks:
        block_flags_value = bits_to_int(IMU_FLAG_BITS, block["flags"])
        for i in range(5):
            sample_index = (block["first_sample_index"] + i) % 65536
            device_time_ms = block["device_time_ms"] + i * sample_period_ms
            ax_raw, ay_raw, az_raw = 100 + i, -50 + i, 4096
            gx_raw, gy_raw, gz_raw = 10 + i, -10 - i, 5
            rows.append(
                [
                    f"2026-01-15T13:00:{block['block_seq']:02d}.{i:02d}0Z",
                    str(1000 + block["block_seq"] * 100 + i * sample_period_ms),
                    str(block["block_seq"]),
                    str(block_flags_value),
                    str(block["missing"] if i == 0 else 0),
                    str(sample_index),
                    str(device_time_ms),
                    str(ax_raw),
                    str(ay_raw),
                    str(az_raw),
                    str(gx_raw),
                    str(gy_raw),
                    str(gz_raw),
                    f"{ax_raw / accel_lsb:.6f}",
                    f"{ay_raw / accel_lsb:.6f}",
                    f"{az_raw / accel_lsb:.6f}",
                    f"{gx_raw / gyro_lsb:.6f}",
                    f"{gy_raw / gyro_lsb:.6f}",
                    f"{gz_raw / gyro_lsb:.6f}",
                ]
            )

    write_csv(
        path,
        [
            "rx_utc_iso",
            "rx_mono_ms",
            "block_seq",
            "block_flags",
            "block_missing_samples",
            "sample_index",
            "device_time_ms",
            "ax_raw",
            "ay_raw",
            "az_raw",
            "gx_raw",
            "gy_raw",
            "gz_raw",
            "ax_g",
            "ay_g",
            "az_g",
            "gx_dps",
            "gy_dps",
            "gz_dps",
        ],
        rows,
    )


def main() -> None:
    make_v2_fixture()
    make_v3_fixture()
    print(f"Wrote fixtures to {FIXTURES_DIR}")


if __name__ == "__main__":
    main()
