"""Loads a session's raw CSV files and re-decodes every telemetry/IMU/GPS/signals row.

The app's decoded CSV columns exist for convenience only; per the session
contract, `raw_hex` is authoritative and the server always re-decodes it with
`ble_schema` (a consistency check against the app's own decode happens in
report.py). This module owns the CSV <-> decoded-row conversion shared by
report.py (validation) and parquet.py (columnar export).
"""

from __future__ import annotations

import csv
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from moto_server import ble_schema, signals
from moto_server.defs import ble as defs_ble

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
# Telemetry version 4 (D-058) appends the tester statistics (schema `testerStats`).
# The names are the schema field names in snake_case (test_ble_schema checks this).
V4_EXTRA_HEADER = [
    "step_gap_max_ms",
    "step_gap_over_count",
    "rtt_did",
    "rtt_min_ms",
    "rtt_max_ms",
    "rtt_sum_ms",
    "rtt_count",
    "rtt_nrc78_count",
]
V4_HEADER = V3_HEADER + V4_EXTRA_HEADER

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

# gps.csv (D-060): one row per GPS block. The raw columns are the schema's
# gpsBlock field names in snake_case (test_ble_schema checks this); the exact
# header check below also means no other column (e.g. a position) is accepted.
GPS_RAW_COLUMNS = [
    "device_time_ms",
    "ground_speed",
    "heading_of_motion",
    "speed_accuracy",
    "heading_accuracy",
    "fix_type",
    "num_sv",
    "flags",
]
GPS_SCALED_COLUMNS = [
    "ground_speed_mps",
    "heading_of_motion_deg",
    "speed_accuracy_mps",
    "heading_accuracy_deg",
]
GPS_FLAG_COLUMNS = ["gnss_fix_ok", "parse_error", "uart_overflow"]
GPS_HEADER = [
    "rx_utc_iso",
    "rx_mono_ms",
    "seq",
    "lost_since_prev",
    "raw_hex",
    *GPS_RAW_COLUMNS,
    *GPS_SCALED_COLUMNS,
    *GPS_FLAG_COLUMNS,
]

# signals.csv (D-066 item 4): one row per vehicle-signal *record*; the block header
# fields are repeated on every row of a block. did / state / age_ms / raw are the wire
# values and authoritative; signal / value are the app's catalog decode (a check only).
SIGNALS_HEADER = [
    "rx_utc_iso",
    "rx_mono_ms",
    "block_seq",
    "lost_since_prev",
    "device_time_ms",
    "total_records",
    "first_record",
    "did",
    "state",
    "age_ms",
    "raw",
    "signal",
    "value",
]
# Columns that hold a wire field -> the field name in schema vehicleSignalBlock
# (headerFields / recordFields); the value range comes from that field's size.
_SIGNALS_WIRE_FIELDS = {
    "block_seq": "seq",
    "device_time_ms": "deviceTimeMs",
    "total_records": "totalRecords",
    "first_record": "firstRecord",
    "did": "did",
    "state": "state",
    "age_ms": "ageMs",
    "raw": "raw",
}


# States whose raw is the DID's data bytes (schema vehicleSignalBlock.rawRule), by name
# from the generated state table, never as literal numbers.
_VALUE_STATES = frozenset(
    value for value, name in defs_ble.VSIG_STATES.items() if name in ("valid", "stale")
)


class ContractError(ValueError):
    """A CSV header does not match its known session contract."""


def detect_telemetry_layout(header: list[str]) -> str:
    """Returns "v2", "v3" or "v4" (the column set of the CSV) for a known header,
    else raises ContractError. A session recorded before the version 4 columns
    existed simply has the shorter header."""
    if header == V4_HEADER:
        return "v4"
    if header == V3_HEADER:
        return "v3"
    if header == V2_HEADER:
        return "v2"
    raise ContractError(f"unrecognized telemetry.csv header: {header!r}")


def csv_column_for_field(schema_field_name: str) -> str:
    """telemetry.csv / parquet column of a version 4 schema field: stepGapMaxMs ->
    step_gap_max_ms, rttNrc78Count -> rtt_nrc78_count."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", schema_field_name).lower()


def decoded_tester_columns(decoded: dict[str, Any] | None) -> dict[str, int | None]:
    """The version 4 tester columns of a decoded packet (all None when it has none)."""
    stats = {
        csv_column_for_field(k): v for k, v in ((decoded or {}).get("tester_stats") or {}).items()
    }
    return {col: stats.get(col) for col in V4_EXTRA_HEADER}


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
    app_tester_stats: dict[str, int | None]
    decode_error: str | None = None
    decoded: dict[str, Any] | None = None
    mismatches: list[str] = field(default_factory=list)


def decode_telemetry_csv(
    csv_path: Path, schema: dict[str, Any] | None = None
) -> tuple[str, list[DecodedTelemetryRow]]:
    """Parses telemetry.csv, detects the v2/v3/v4 column layout, re-decodes raw_hex per row.

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
            app_packet_version = _to_int(record["packet_version"]) if layout != "v2" else 2
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
            app_tester_stats: dict[str, int | None] = {}
            if layout == "v4":
                app_tester_stats = {col: _to_int(record[col]) for col in V4_EXTRA_HEADER}
            if layout != "v2":
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
                app_tester_stats=app_tester_stats,
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

            if layout == "v4":
                server_tester = decoded_tester_columns(row.decoded)
                row.mismatches.extend(
                    col for col in V4_EXTRA_HEADER if app_tester_stats[col] != server_tester[col]
                )

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


@dataclass
class DecodedGpsRow:
    row_index: int
    rx_utc_iso: str
    rx_mono_ms: int
    seq: int
    app_lost_since_prev: int
    raw_hex: str
    decode_error: str | None = None
    decoded: dict[str, Any] | None = None
    mismatches: list[str] = field(default_factory=list)


def decode_gps_csv(csv_path: Path, schema: dict[str, Any] | None = None) -> list[DecodedGpsRow]:
    """Parses gps.csv and re-decodes raw_hex per row with the schema.

    Like telemetry, raw_hex is authoritative; `mismatches` lists the columns
    where the app's own decoded values disagree with our decode.
    """
    schema = schema or ble_schema.load_default_schema()
    rows: list[DecodedGpsRow] = []
    with csv_path.open(newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        if header != GPS_HEADER:
            raise ContractError(f"unrecognized gps.csv header: {header!r}")
        for i, raw_row in enumerate(reader):
            record = dict(zip(header, raw_row, strict=True))
            row = DecodedGpsRow(
                row_index=i,
                rx_utc_iso=record["rx_utc_iso"],
                rx_mono_ms=int(record["rx_mono_ms"]),
                seq=int(record["seq"]),
                app_lost_since_prev=int(record["lost_since_prev"]),
                raw_hex=record["raw_hex"],
            )
            try:
                row.decoded = ble_schema.decode_gps_block(bytes.fromhex(record["raw_hex"]), schema)
            except (ble_schema.DecodeError, ValueError) as exc:
                row.decode_error = str(exc)
                rows.append(row)
                continue

            decoded = row.decoded
            if row.seq != decoded["seq"]:
                row.mismatches.append("seq")
            server_raw = {csv_column_for_field(k): v for k, v in decoded["raw"].items()}
            row.mismatches.extend(
                col for col in GPS_RAW_COLUMNS if _to_int(record[col]) != server_raw[col]
            )
            row.mismatches.extend(
                col
                for col in GPS_SCALED_COLUMNS
                if abs(float(record[col]) - decoded["scaled"][col]) > 1e-6
            )
            server_flags = {csv_column_for_field(k): v for k, v in decoded["flags"].items()}
            row.mismatches.extend(
                col for col in GPS_FLAG_COLUMNS if _to_bool(record[col]) != server_flags[col]
            )
            rows.append(row)
    return rows


@dataclass
class DecodedSignalRow:
    row_index: int
    rx_utc_iso: str
    rx_mono_ms: int
    # The block header cells as written, to group rows into blocks even when a
    # wire field of the row is invalid.
    block_key: tuple[str, str, str, str]
    app_lost_since_prev: int | None = None
    block_seq: int | None = None
    device_time_ms: int | None = None
    total_records: int | None = None
    first_record: int | None = None
    did: int | None = None
    state: int | None = None
    state_name: str | None = None
    age_ms: int | None = None
    raw: int | None = None
    # Our own catalog decode of did + raw (None: unknown DID, state without a value,
    # or a raw that does not fit the DID's length).
    signal: str | None = None
    unit: str | None = None
    value: float | None = None
    # State 1 or 2 with a known DID but a raw above 2^(8*length) - 1: malformed
    # (vehicleSignalBlock.decodeRule), kept out of the signal outputs.
    malformed: bool = False
    decode_error: str | None = None
    mismatches: list[str] = field(default_factory=list)


def _wire_int(record: dict[str, str], column: str, vsig: dict[str, Any]) -> int:
    """An unsigned integer cell, range-checked against its schema field's size."""
    field_name = _SIGNALS_WIRE_FIELDS[column]
    schema_field = next(
        f for f in (*vsig["headerFields"], *vsig["recordFields"]) if f["name"] == field_name
    )
    try:
        number = int(record[column])
    except ValueError:
        raise ble_schema.DecodeError(f"{column}: not an integer: {record[column]!r}") from None
    if not 0 <= number < 1 << (8 * schema_field["size"]):
        raise ble_schema.DecodeError(f"{column}: {number} is out of range for {field_name}")
    return number


def _same_value(app_cell: str, server_value: float | None) -> bool:
    if app_cell == "" or server_value is None:
        return app_cell == "" and server_value is None
    try:
        return math.isclose(float(app_cell), server_value, rel_tol=1e-9, abs_tol=1e-9)
    except ValueError:
        return False


def decode_signals_csv(
    csv_path: Path, schema: dict[str, Any] | None = None
) -> list[DecodedSignalRow]:
    """Parses signals.csv and re-derives `signal` / `value` from `did` + `raw`.

    The wire columns (did, state, age_ms, raw and the block header) are validated
    against the schema; a row that fails keeps its receive time and gets a
    `decode_error`. The app's `signal` / `value` columns are only compared with the
    recomputation from the generated vehicle catalog (`mismatches`).
    """
    schema = schema or ble_schema.load_default_schema()
    vsig = schema["vehicleSignalBlock"]
    rows: list[DecodedSignalRow] = []
    with csv_path.open(newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        if header != SIGNALS_HEADER:
            raise ContractError(f"unrecognized signals.csv header: {header!r}")
        for i, raw_row in enumerate(reader):
            record = dict(zip(header, raw_row, strict=True))
            row = DecodedSignalRow(
                row_index=i,
                rx_utc_iso=record["rx_utc_iso"],
                rx_mono_ms=int(record["rx_mono_ms"]),
                block_key=(
                    record["block_seq"],
                    record["rx_mono_ms"],
                    record["device_time_ms"],
                    record["first_record"],
                ),
            )
            rows.append(row)
            try:
                row.app_lost_since_prev = int(record["lost_since_prev"])
                for column in _SIGNALS_WIRE_FIELDS:
                    setattr(row, column, _wire_int(record, column, vsig))
                if row.state not in defs_ble.VSIG_STATES:
                    raise ble_schema.DecodeError(f"state: unknown value {row.state}")
            except (ble_schema.DecodeError, ValueError) as exc:
                row.decode_error = str(exc)
                for column in _SIGNALS_WIRE_FIELDS:
                    setattr(row, column, None)  # no half-parsed record
                continue

            assert row.did is not None and row.state is not None and row.raw is not None
            row.state_name = defs_ble.VSIG_STATES[row.state]
            found = signals.catalog_signal(row.did)
            if found is not None:
                name, entry = found
                row.signal = name
                row.unit = entry["unit"]
                if row.state in _VALUE_STATES:
                    if signals.catalog_raw_fits(entry, row.raw):
                        row.value = signals.catalog_value(name, row.raw)
                    else:
                        row.malformed = True

            if record["signal"] != (row.signal or ""):
                row.mismatches.append("signal")
            if not _same_value(record["value"], row.value):
                row.mismatches.append("value")
    return rows


def group_signal_blocks(rows: list[DecodedSignalRow]) -> list[list[DecodedSignalRow]]:
    """Consecutive rows with the same block header cells are one block (one notification)."""
    blocks: list[list[DecodedSignalRow]] = []
    for row in rows:
        if blocks and blocks[-1][0].block_key == row.block_key:
            blocks[-1].append(row)
        else:
            blocks.append([row])
    return blocks
