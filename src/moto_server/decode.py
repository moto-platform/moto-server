"""Loads a session's raw CSV files and re-decodes every telemetry/IMU row.

The app's decoded CSV columns exist for convenience only; per the session
contract, `raw_hex` is authoritative and the server always re-decodes it with
`ble_schema` (a consistency check against the app's own decode happens in
report.py). This module owns the CSV <-> decoded-row conversion shared by
report.py (validation) and parquet.py (columnar export).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from moto_server import ble_schema, signals

V2_HEADER = [
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
V3_EXTRA_HEADER = [
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
V3_HEADER = V2_HEADER + V3_EXTRA_HEADER

IMU_HEADER = [
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
]


class ContractError(ValueError):
    """The CSV header does not match either known telemetry.csv contract."""


def detect_telemetry_layout(header: list[str]) -> str:
    """Returns "v2" or "v3" for a known header, else raises ContractError."""
    if header == V3_HEADER:
        return "v3"
    if header == V2_HEADER:
        return "v2"
    raise ContractError(f"unrecognized telemetry.csv header: {header!r}")


def _to_int(value: str) -> int | None:
    return int(value) if value not in ("", None) else None


def _to_float(value: str) -> float | None:
    return float(value) if value not in ("", None) else None


def _to_bool(value: str) -> bool | None:
    if value in ("", None):
        return None
    return value == "1"


@dataclass
class DecodedTelemetryRow:
    row_index: int
    rx_utc_iso: str
    rx_mono_ms: int
    seq: int
    raw_hex: str
    app_packet_version: int
    app_decode_error: str | None
    app_physical: dict[str, float | None]
    app_valid: dict[str, bool | None]
    app_ages_ms: dict[str, int | None]
    app_can_health: dict[str, Any]
    decode_error: str | None = None
    decoded: dict[str, Any] | None = None
    mismatches: list[str] = field(default_factory=list)


def decode_telemetry_csv(
    csv_path: Path, schema: dict[str, Any] | None = None
) -> tuple[str, list[DecodedTelemetryRow]]:
    """Parses telemetry.csv, detects v2/v3 layout, and re-decodes raw_hex for every row.

    Returns (layout, rows). Each row's `decoded` holds our own ble_schema decode
    of raw_hex; `mismatches` lists field names where it disagrees with the
    app's own decoded columns (a consistency check, not the source of truth).
    """
    schema = schema or ble_schema.load_default_schema()
    with csv_path.open(newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        layout = detect_telemetry_layout(header)
        rows: list[DecodedTelemetryRow] = []
        for i, raw_row in enumerate(reader):
            record = dict(zip(header, raw_row, strict=True))
            app_packet_version = _to_int(record["packet_version"]) if layout == "v3" else 2
            app_physical = {
                "ENGINE_SPEED": _to_float(record["rpm"]),
                "VEHICLE_SPEED": _to_float(record["speed_kmh"]),
                "COOLANT_TEMP": _to_float(record["coolant_c"]),
                "THROTTLE_POS": _to_float(record["tps_pct"]),
                "BATTERY_VOLTAGE": _to_float(record["battery_v"]),
            }
            app_valid = {
                "ENGINE_SPEED": _to_bool(record["rpm_valid"]),
                "VEHICLE_SPEED": _to_bool(record["speed_valid"]),
                "COOLANT_TEMP": _to_bool(record["coolant_valid"]),
                "THROTTLE_POS": _to_bool(record["tps_valid"]),
                "BATTERY_VOLTAGE": _to_bool(record["battery_valid"]),
            }
            app_ages_ms: dict[str, int | None] = {}
            app_can_health: dict[str, Any] = {}
            if layout == "v3":
                app_ages_ms = {
                    "ENGINE_SPEED": _to_int(record["rpm_age_ms"]),
                    "VEHICLE_SPEED": _to_int(record["speed_age_ms"]),
                    "COOLANT_TEMP": _to_int(record["coolant_age_ms"]),
                    "THROTTLE_POS": _to_int(record["tps_age_ms"]),
                    "BATTERY_VOLTAGE": _to_int(record["battery_age_ms"]),
                }
                app_can_health = {
                    "bus_state_raw": _to_int(record["can_bus_state"]),
                    "tec": _to_int(record["can_tec"]),
                    "rec": _to_int(record["can_rec"]),
                    "bus_off_count": _to_int(record["can_bus_off_count"]),
                    "unanswered_did_count": _to_int(record["unanswered_did_count"]),
                    "flags_raw": _to_int(record["can_flags"]),
                }

            row = DecodedTelemetryRow(
                row_index=i,
                rx_utc_iso=record["rx_utc_iso"],
                rx_mono_ms=int(record["rx_mono_ms"]),
                seq=int(record["seq"]),
                raw_hex=record["raw_hex"],
                app_packet_version=app_packet_version,
                app_decode_error=record["decode_error"] or None,
                app_physical=app_physical,
                app_valid=app_valid,
                app_ages_ms=app_ages_ms,
                app_can_health=app_can_health,
            )

            try:
                raw_bytes = bytes.fromhex(record["raw_hex"])
                row.decoded = ble_schema.decode_telemetry(raw_bytes, schema)
            except (ble_schema.DecodeError, ValueError) as exc:
                row.decode_error = str(exc)
                rows.append(row)
                continue

            for defs_signal, app_value in app_physical.items():
                if app_value is None:
                    continue
                server_value = row.decoded["physical"].get(defs_signal)
                if server_value is None or abs(server_value - app_value) > 1e-6:
                    row.mismatches.append(signals.signal_key(defs_signal))

            rows.append(row)

    return layout, rows


@dataclass
class DecodedImuRow:
    row_index: int
    rx_utc_iso: str
    rx_mono_ms: int
    block_seq: int
    block_flags: int
    block_missing_samples: int
    sample_index: int
    device_time_ms: int
    raw: dict[str, int]
    scaled_app: dict[str, float]
    scaled_recomputed: dict[str, float]
    mismatches: list[str] = field(default_factory=list)


def decode_imu_csv(csv_path: Path, schema: dict[str, Any] | None = None) -> list[DecodedImuRow]:
    """Parses imu.csv and recomputes *_g/*_dps from *_raw for a consistency check."""
    schema = schema or ble_schema.load_default_schema()
    accel_lsb = schema["imuBlock"]["scale"]["accel"]["lsbPerUnit"]
    gyro_lsb = schema["imuBlock"]["scale"]["gyro"]["lsbPerUnit"]

    rows: list[DecodedImuRow] = []
    with csv_path.open(newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        if header != IMU_HEADER:
            raise ContractError(f"unrecognized imu.csv header: {header!r}")
        for i, raw_row in enumerate(reader):
            record = dict(zip(header, raw_row, strict=True))
            raw = {
                "ax": int(record["ax_raw"]),
                "ay": int(record["ay_raw"]),
                "az": int(record["az_raw"]),
                "gx": int(record["gx_raw"]),
                "gy": int(record["gy_raw"]),
                "gz": int(record["gz_raw"]),
            }
            scaled_app = {
                "ax_g": float(record["ax_g"]),
                "ay_g": float(record["ay_g"]),
                "az_g": float(record["az_g"]),
                "gx_dps": float(record["gx_dps"]),
                "gy_dps": float(record["gy_dps"]),
                "gz_dps": float(record["gz_dps"]),
            }
            scaled_recomputed = {
                "ax_g": raw["ax"] / accel_lsb,
                "ay_g": raw["ay"] / accel_lsb,
                "az_g": raw["az"] / accel_lsb,
                "gx_dps": raw["gx"] / gyro_lsb,
                "gy_dps": raw["gy"] / gyro_lsb,
                "gz_dps": raw["gz"] / gyro_lsb,
            }
            mismatches = [
                key for key in scaled_app if abs(scaled_app[key] - scaled_recomputed[key]) > 1e-6
            ]
            rows.append(
                DecodedImuRow(
                    row_index=i,
                    rx_utc_iso=record["rx_utc_iso"],
                    rx_mono_ms=int(record["rx_mono_ms"]),
                    block_seq=int(record["block_seq"]),
                    block_flags=int(record["block_flags"]),
                    block_missing_samples=int(record["block_missing_samples"]),
                    sample_index=int(record["sample_index"]),
                    device_time_ms=int(record["device_time_ms"]),
                    raw=raw,
                    scaled_app=scaled_app,
                    scaled_recomputed=scaled_recomputed,
                    mismatches=mismatches,
                )
            )
    return rows
