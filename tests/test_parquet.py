from __future__ import annotations

from pathlib import Path

import pyarrow as pa
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


TESTER_COLUMNS = [
    "step_gap_max_ms",
    "step_gap_over_count",
    "rtt_did",
    "rtt_min_ms",
    "rtt_max_ms",
    "rtt_sum_ms",
    "rtt_count",
    "rtt_nrc78_count",
]


def test_v4_session_parquet_has_nullable_integer_tester_columns(
    settings: Settings, fixtures_dir: Path
):
    result = ingest_path(fixtures_dir / "v4_session", settings)
    session_dir = settings.sessions_dir / result.session_id
    table = pq.read_table(session_dir / "parquet" / "telemetry.parquet")

    assert table.num_rows == 7
    for column in TESTER_COLUMNS:
        assert pa.types.is_integer(table.schema.field(column).type), column
    # Row 0 is a version 3 packet: no tester data. Rows 1..6 are version 4.
    assert all(table.column(c).to_pylist()[0] is None for c in TESTER_COLUMNS)
    assert table.column("step_gap_max_ms").to_pylist() == [None, 12, 12, 15, 15, 20, 20]
    assert table.column("rtt_did").to_pylist()[1:3] == [0xF40C, 0xF40D]
    # A uint32 value above the int32 range survives.
    assert table.column("rtt_count").to_pylist()[5] == 0xFFFFFFFF
    assert table.column("rtt_sum_ms").to_pylist()[5] == 0xFFFFFFFF
    # The v3 columns of the same rows are intact.
    assert table.column("engine_speed_rpm").to_pylist()[:2] == [2000, 2001]


def test_old_session_without_tester_columns_gets_null_columns(
    settings: Settings, fixtures_dir: Path
):
    # v3_session is a telemetry.csv recorded before the 8 tester columns existed.
    result = ingest_path(fixtures_dir / "v3_session", settings)
    session_dir = settings.sessions_dir / result.session_id
    table = pq.read_table(session_dir / "parquet" / "telemetry.parquet")
    for column in TESTER_COLUMNS:
        assert pa.types.is_integer(table.schema.field(column).type), column
        assert table.column(column).null_count == table.num_rows, column
