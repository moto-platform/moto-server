from __future__ import annotations

from pathlib import Path

import pyarrow.parquet as pq

from moto_server.config import Settings
from moto_server.ingest import ingest_path


def test_telemetry_parquet_columns_and_session_id(settings: Settings, fixtures_dir: Path):
    result = ingest_path(fixtures_dir / "v3_session", settings)
    session_dir = settings.sessions_dir / result.session_id
    table = pq.read_table(session_dir / "parquet" / "telemetry.parquet")

    expected_columns = {
        "session_id",
        "rx_utc",
        "rx_mono_ms",
        "device_time_ms",
        "engine_speed_rpm",
        "vehicle_speed_kmh",
        "coolant_temp_degc",
        "throttle_pos_pct",
        "battery_voltage_v",
        "engine_speed_age_ms",
        "vehicle_speed_age_ms",
        "coolant_temp_age_ms",
        "throttle_pos_age_ms",
        "battery_voltage_age_ms",
        "engine_speed_valid",
        "vehicle_speed_valid",
        "coolant_temp_valid",
        "throttle_pos_valid",
        "battery_voltage_valid",
    }
    assert expected_columns.issubset(set(table.column_names))
    assert table.num_rows == 5
    assert set(table.column("session_id").to_pylist()) == {result.session_id}
    assert table.column("engine_speed_rpm").to_pylist() == [4000, 4005, 4010, 4015, 4020]


def test_imu_parquet_columns_and_session_id(settings: Settings, fixtures_dir: Path):
    result = ingest_path(fixtures_dir / "v3_session", settings)
    session_dir = settings.sessions_dir / result.session_id
    table = pq.read_table(session_dir / "parquet" / "imu.parquet")

    expected_columns = {
        "session_id",
        "device_time_ms",
        "sample_index",
        "ax_g",
        "ay_g",
        "az_g",
        "gx_dps",
        "gy_dps",
        "gz_dps",
        "block_missing_samples",
    }
    assert expected_columns.issubset(set(table.column_names))
    assert table.num_rows == 10
    assert set(table.column("session_id").to_pylist()) == {result.session_id}
    assert table.column("block_missing_samples").to_pylist()[5] == 4


def test_v2_session_has_no_imu_parquet(settings: Settings, fixtures_dir: Path):
    result = ingest_path(fixtures_dir / "v2_session", settings)
    session_dir = settings.sessions_dir / result.session_id
    assert not (session_dir / "parquet" / "imu.parquet").exists()
    table = pq.read_table(session_dir / "parquet" / "telemetry.parquet")
    # v2 sessions have no age/device-time data: those columns must be null, not absent.
    assert all(v is None for v in table.column("device_time_ms").to_pylist())
    assert all(v is None for v in table.column("engine_speed_age_ms").to_pylist())
