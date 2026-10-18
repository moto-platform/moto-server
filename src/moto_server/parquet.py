"""Builds telemetry.parquet and (if present) imu.parquet / gps.parquet for a session.

Column names: physical signals as `<defs signal>_<unit>` (e.g. engine_speed_rpm),
ages as `<signal>_age_ms`, validity as `<signal>_valid` -- see signals.py for the
naming rules, driven by moto_defs.vehicle_cl250.DIDS units. The version 4 tester
statistics keep their telemetry.csv names (step_gap_max_ms, ..., rtt_nrc78_count); the
version 3 CAN health fields use the schema field names in snake_case (can_bus_state,
can_tx_error_count, ..., can_flags; decode.CAN_HEALTH_FIELDS).
gps.parquet keeps the gps.csv column names, decoded from raw_hex (D-060: speed and
heading only, no position).
signals.parquet (D-066 item 4) has one row per vehicle-signal record of signals.csv:
the wire values (did, state, age_ms, raw), the catalog signal name and unit, and `value`
recomputed from did + raw with the generated vehicle catalog for state `valid` only (a
stale value is shown as unavailable); `sample_device_time_ms` is the node time of the
DID's last positive response, so a record repeated in several blocks can be deduplicated.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from moto_server import ble_schema, signals
from moto_server.decode import (
    CAN_HEALTH_FIELDS,
    GPS_FLAG_COLUMNS,
    GPS_RAW_COLUMNS,
    GPS_SCALED_COLUMNS,
    V4_EXTRA_HEADER,
    VSIG_STALE,
    VSIG_VALID,
    DecodedTelemetryRow,
    csv_column_for_field,
    decode_gps_csv,
    decode_imu_csv,
    decode_signals_csv,
    decode_telemetry_csv,
    decoded_can_health_columns,
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
    # Version 3 CAN health, also from the re-decode: null for version 2 rows.
    health = [decoded_can_health_columns(row.decoded) for row in rows]
    for col in (csv_column_for_field(f) for f in CAN_HEALTH_FIELDS):
        arrays[col] = pa.array([h[col] for h in health], type=pa.int64())
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


_UINT32_MAX = 2**32
_VSIG_VALID = VSIG_VALID
_VSIG_STALE = VSIG_STALE


def build_signals_table(session_id: str, csv_path: Path, schema: dict[str, Any]) -> pa.Table:
    """One row per vehicle-signal record, every decoded column from the did + raw
    re-derivation. A row that failed validation keeps its receive time with nulls."""
    rows = decode_signals_csv(csv_path, schema)
    age_max = schema["age"]["max"]

    def sample_time(row: Any) -> int | None:
        # The last positive response arrived at deviceTimeMs - ageMs while ageMs < max.
        if row.state in (_VSIG_VALID, _VSIG_STALE) and row.age_ms < age_max:
            return (row.device_time_ms - row.age_ms) % _UINT32_MAX
        return None

    def column(attr: str, typ: pa.DataType) -> pa.Array:
        return pa.array([getattr(row, attr) for row in rows], type=typ)

    return pa.table(
        {
            "session_id": pa.array([session_id] * len(rows), type=pa.string()),
            "rx_utc": column("rx_utc_iso", pa.string()),
            "rx_mono_ms": column("rx_mono_ms", pa.int64()),
            "block_seq": column("block_seq", pa.int64()),
            "device_time_ms": column("device_time_ms", pa.int64()),
            "total_records": column("total_records", pa.int64()),
            "first_record": column("first_record", pa.int64()),
            "did": column("did", pa.int64()),
            "state": column("state", pa.int64()),
            "state_name": column("state_name", pa.string()),
            "age_ms": column("age_ms", pa.int64()),
            "raw": column("raw", pa.int64()),
            "sample_device_time_ms": pa.array(
                [None if row.decode_error else sample_time(row) for row in rows], type=pa.int64()
            ),
            "signal": column("signal", pa.string()),
            "unit": column("unit", pa.string()),
            "value": pa.array(
                [row.value if row.state == _VSIG_VALID else None for row in rows],
                type=pa.float64(),
            ),
            "decode_error": column("decode_error", pa.string()),
        }
    )


def write_session_parquet(
    session_dir: Path, session_id: str, schema: dict[str, Any] | None = None
) -> list[Path]:
    """Writes telemetry.parquet (and imu.parquet / gps.parquet / signals.parquet, if imu.csv /
    gps.csv / signals.csv exist) under <session_dir>/parquet/. Returns the paths written."""
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

    signals_csv = session_dir / "raw" / "signals.csv"
    if signals_csv.exists():
        signals_table = build_signals_table(session_id, signals_csv, schema)
        signals_path = parquet_dir / "signals.parquet"
        pq.write_table(signals_table, signals_path)
        written.append(signals_path)

    return written
