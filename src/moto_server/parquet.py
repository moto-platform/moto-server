"""Builds telemetry.parquet and (if present) imu.parquet / gps.parquet for a session.

Column names: physical signals as `<defs signal>_<unit>` (e.g. engine_speed_rpm),
ages as `<signal>_age_ms`, validity as `<signal>_valid` -- see signals.py for the
naming rules, driven by moto_defs.vehicle_cl250.DIDS units. The version 4 tester
statistics keep their telemetry.csv names (step_gap_max_ms, ..., rtt_nrc78_count).
gps.parquet keeps the gps.csv column names, decoded from raw_hex (D-060: speed and
heading only, no position).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from moto_server import ble_schema, signals
from moto_server.decode import (
    GPS_FLAG_COLUMNS,
    GPS_RAW_COLUMNS,
    GPS_SCALED_COLUMNS,
    V4_EXTRA_HEADER,
    DecodedTelemetryRow,
    csv_column_for_field,
    decode_gps_csv,
    decode_imu_csv,
    decode_telemetry_csv,
    decoded_tester_columns,
)


def build_telemetry_table(
    session_id: str, rows: list[DecodedTelemetryRow], schema: dict[str, Any]
) -> pa.Table:
    defs_signals = signals.known_defs_signals(schema)

    columns: dict[str, list[Any]] = {
        "session_id": [session_id] * len(rows),
        "rx_utc": [row.rx_utc_iso for row in rows],
        "rx_mono_ms": [row.rx_mono_ms for row in rows],
        "seq": [row.seq for row in rows],
        "packet_version": [row.app_packet_version for row in rows],
        "device_time_ms": [row.decoded["device_time_ms"] if row.decoded else None for row in rows],
        "decode_error": [row.decode_error for row in rows],
    }

    for defs_signal in defs_signals:
        physical_col = signals.physical_column(defs_signal)
        age_col = signals.age_column(defs_signal)
        valid_col = signals.valid_column(defs_signal)
        flag_name = signals.flag_name_for(defs_signal)

        columns[physical_col] = [
            (row.decoded["physical"].get(defs_signal) if row.decoded else None) for row in rows
        ]
        columns[age_col] = [
            (row.decoded["ages_ms"].get(defs_signal) if row.decoded else None) for row in rows
        ]
        columns[valid_col] = [
            (bool(row.decoded["flags"].get(flag_name)) if row.decoded and flag_name else None)
            for row in rows
        ]

    # Version 4 tester statistics (D-058), taken from the raw_hex re-decode: nullable
    # integers, null for version 2/3 rows and for sessions recorded before they existed.
    tester = [decoded_tester_columns(row.decoded) for row in rows]
    arrays = {col: pa.array([t[col] for t in tester], type=pa.int64()) for col in V4_EXTRA_HEADER}
    return pa.table({**columns, **arrays})


def build_imu_table(session_id: str, csv_path: Path, schema: dict[str, Any]) -> pa.Table:
    imu_rows = decode_imu_csv(csv_path, schema)
    return pa.table(
        {
            "session_id": [session_id] * len(imu_rows),
            "rx_utc": [row.rx_utc_iso for row in imu_rows],
            "rx_mono_ms": [row.rx_mono_ms for row in imu_rows],
            "block_seq": [row.block_seq for row in imu_rows],
            "block_flags": [row.block_flags for row in imu_rows],
            "block_missing_samples": [row.block_missing_samples for row in imu_rows],
            "sample_index": [row.sample_index for row in imu_rows],
            "device_time_ms": [row.device_time_ms for row in imu_rows],
            "ax_raw": [row.raw["ax"] for row in imu_rows],
            "ay_raw": [row.raw["ay"] for row in imu_rows],
            "az_raw": [row.raw["az"] for row in imu_rows],
            "gx_raw": [row.raw["gx"] for row in imu_rows],
            "gy_raw": [row.raw["gy"] for row in imu_rows],
            "gz_raw": [row.raw["gz"] for row in imu_rows],
            "ax_g": [row.scaled_recomputed["ax_g"] for row in imu_rows],
            "ay_g": [row.scaled_recomputed["ay_g"] for row in imu_rows],
            "az_g": [row.scaled_recomputed["az_g"] for row in imu_rows],
            "gx_dps": [row.scaled_recomputed["gx_dps"] for row in imu_rows],
            "gy_dps": [row.scaled_recomputed["gy_dps"] for row in imu_rows],
            "gz_dps": [row.scaled_recomputed["gz_dps"] for row in imu_rows],
        }
    )


def build_gps_table(session_id: str, csv_path: Path, schema: dict[str, Any]) -> pa.Table:
    """One row per GPS block. Every decoded column comes from the raw_hex re-decode;
    a row that failed to decode keeps its receive time and seq with null values."""
    gps_rows = decode_gps_csv(csv_path, schema)
    raw = [
        {csv_column_for_field(k): v for k, v in row.decoded["raw"].items()} if row.decoded else {}
        for row in gps_rows
    ]
    flags = [
        {csv_column_for_field(k): v for k, v in row.decoded["flags"].items()} if row.decoded else {}
        for row in gps_rows
    ]
    columns: dict[str, pa.Array] = {
        "session_id": pa.array([session_id] * len(gps_rows), type=pa.string()),
        "rx_utc": pa.array([row.rx_utc_iso for row in gps_rows], type=pa.string()),
        "rx_mono_ms": pa.array([row.rx_mono_ms for row in gps_rows], type=pa.int64()),
        "seq": pa.array([row.seq for row in gps_rows], type=pa.int64()),
        "decode_error": pa.array([row.decode_error for row in gps_rows], type=pa.string()),
    }
    for col in GPS_RAW_COLUMNS:
        columns[col] = pa.array([r.get(col) for r in raw], type=pa.int64())
    for col in GPS_SCALED_COLUMNS:
        columns[col] = pa.array(
            [row.decoded["scaled"][col] if row.decoded else None for row in gps_rows],
            type=pa.float64(),
        )
    columns["fix_type_name"] = pa.array(
        [row.decoded["fix_type_name"] if row.decoded else None for row in gps_rows],
        type=pa.string(),
    )
    for col in GPS_FLAG_COLUMNS:
        columns[col] = pa.array([f.get(col) for f in flags], type=pa.bool_())
    return pa.table(columns)


def write_session_parquet(
    session_dir: Path, session_id: str, schema: dict[str, Any] | None = None
) -> list[Path]:
    """Writes telemetry.parquet (and imu.parquet / gps.parquet, if imu.csv / gps.csv exist) under
    <session_dir>/parquet/. Returns the paths written."""
    schema = schema or ble_schema.load_default_schema()
    parquet_dir = session_dir / "parquet"
    parquet_dir.mkdir(parents=True, exist_ok=True)

    _, rows = decode_telemetry_csv(session_dir / "raw" / "telemetry.csv", schema)
    telemetry_table = build_telemetry_table(session_id, rows, schema)
    telemetry_path = parquet_dir / "telemetry.parquet"
    pq.write_table(telemetry_table, telemetry_path)
    written = [telemetry_path]

    imu_csv = session_dir / "raw" / "imu.csv"
    if imu_csv.exists():
        imu_table = build_imu_table(session_id, imu_csv, schema)
        imu_path = parquet_dir / "imu.parquet"
        pq.write_table(imu_table, imu_path)
        written.append(imu_path)

    gps_csv = session_dir / "raw" / "gps.csv"
    if gps_csv.exists():
        gps_table = build_gps_table(session_id, gps_csv, schema)
        gps_path = parquet_dir / "gps.parquet"
        pq.write_table(gps_table, gps_path)
        written.append(gps_path)

    return written
