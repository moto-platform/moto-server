"""Builds telemetry.parquet and (if present) imu.parquet for a session.

Column names: physical signals as `<defs signal>_<unit>` (e.g. engine_speed_rpm),
ages as `<signal>_age_ms`, validity as `<signal>_valid` -- see signals.py for the
naming rules, driven by moto_defs.vehicle_cl250.DIDS units.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from moto_server import ble_schema, signals
from moto_server.decode import DecodedTelemetryRow, decode_imu_csv, decode_telemetry_csv


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

    return pa.table(columns)


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


def write_session_parquet(
    session_dir: Path, session_id: str, schema: dict[str, Any] | None = None
) -> list[Path]:
    """Writes telemetry.parquet (and imu.parquet, if imu.csv exists) under
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

    return written
