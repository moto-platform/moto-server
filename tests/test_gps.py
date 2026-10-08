"""GPS block (D-060): decoder, gps.csv contract, report, parquet and index."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from moto_server import ble_schema, index
from moto_server.config import Settings
from moto_server.decode import (
    GPS_FLAG_COLUMNS,
    GPS_HEADER,
    GPS_RAW_COLUMNS,
    ContractError,
    csv_column_for_field,
)
from moto_server.defs import ble as defs_ble
from moto_server.ingest import ingest_path
from moto_server.report import render_text

from .conftest import copy_fixture, zip_fixture

SCHEMA = ble_schema.load_default_schema()
GPS = SCHEMA["gpsBlock"]
POSITION_WORDS = ("lat", "lon", "height", "alt", "hmsl", "ecef", "pos")


def _block(**overrides: int) -> bytes:
    values = {f["name"]: 0 for f in GPS["fields"]}
    values.update({"version": GPS["version"], **overrides})
    return ble_schema.pack_fields(values, GPS["fields"], GPS["totalBytes"])


def _rewrite_gps_cell(session_dir: Path, row: int, column: str, value: str) -> None:
    path = session_dir / "gps.csv"
    header, *rows = path.read_text().splitlines()
    fields = rows[row].split(",")
    fields[header.split(",").index(column)] = value
    rows[row] = ",".join(fields)
    path.write_text("\n".join([header, *rows]) + "\n")


def test_gps_csv_columns_come_from_the_schema():
    schema_columns = [csv_column_for_field(f["name"]) for f in GPS["fields"]]
    # Every gps.csv raw column is a schema field; version / reserved are not stored.
    assert GPS_RAW_COLUMNS == [c for c in schema_columns if c not in ("version", "seq", "reserved")]
    flag_names = [b["name"] for b in GPS["flags"]["bits"] if b["name"] != "reserved"]
    assert GPS_FLAG_COLUMNS == [csv_column_for_field(n) for n in flag_names]
    assert defs_ble.GPS_TOTAL_BYTES == GPS["totalBytes"]


def test_gps_contract_has_no_position_column():
    for column in GPS_HEADER:
        assert not any(word in column.lower() for word in POSITION_WORDS), column


def test_decode_gps_block_scales_and_flags():
    decoded = ble_schema.decode_gps_block(
        _block(
            seq=7,
            deviceTimeMs=123_456,
            groundSpeed=27_778,
            headingOfMotion=35_999_999,
            speedAccuracy=250,
            headingAccuracy=100_000,
            fixType=3,
            numSv=12,
            flags=0b101,
        )
    )
    assert decoded["seq"] == 7
    assert decoded["device_time_ms"] == 123_456
    assert decoded["scaled"]["ground_speed_mps"] == pytest.approx(27.778)
    assert decoded["scaled"]["heading_of_motion_deg"] == pytest.approx(359.99999)
    assert decoded["scaled"]["speed_accuracy_mps"] == pytest.approx(0.25)
    assert decoded["scaled"]["heading_accuracy_deg"] == pytest.approx(1.0)
    assert decoded["fix_type_name"] == "fix3d"
    assert decoded["flags"] == {"gnssFixOk": True, "parseError": False, "uartOverflow": True}


def test_decode_gps_block_signed_int32_fields():
    decoded = ble_schema.decode_gps_block(_block(groundSpeed=-5))
    assert decoded["raw"]["groundSpeed"] == -5


def test_decode_gps_block_rejects_wrong_size_and_version():
    with pytest.raises(ble_schema.DecodeError, match="expected 26 bytes"):
        ble_schema.decode_gps_block(_block()[:-1])
    with pytest.raises(ble_schema.DecodeError, match="expected 26 bytes"):
        ble_schema.decode_gps_block(_block() + b"\x00")
    with pytest.raises(ble_schema.DecodeError, match="unsupported GPS block version"):
        ble_schema.decode_gps_block(_block(version=GPS["version"] + 1))


def test_v4_gps_report(settings: Settings, fixtures_dir: Path):
    report = ingest_path(fixtures_dir / "v4_gps_session", settings).report
    gps = report["gps"]
    assert gps["block_count"] == 7
    assert gps["lost_or_skipped_blocks"] == 2  # 253, 254; the 255 -> 0 wrap is no gap
    assert gps["lost_or_skipped_percent"] == pytest.approx(2 / 9 * 100)
    assert gps["effective_rate_hz"] == pytest.approx(6 / 0.8)
    assert gps["decode_error_rows"] == []
    assert gps["mismatch_rows"] == []
    assert gps["fix_types"] == {"fix2d": 1, "fix3d": 4, "gnssDeadReckoning": 1, "noFix": 1}
    assert gps["speed_check_usable_blocks"] == 4
    assert gps["speed_check_usable_percent"] == pytest.approx(4 / 7 * 100)
    assert gps["blocks_with_parse_error"] == 1
    assert gps["blocks_with_uart_overflow"] == 1

    assert report["status"] == "warn"
    assert "2 GPS block(s) lost or MTU-skipped" in report["reasons"]
    assert (
        "GPS node errors: parse error in 1 block(s), UART overflow in 1 block(s)"
        in report["reasons"]
    )
    text = render_text(report)
    assert "GPS: 7 block(s), 2 lost or MTU-skipped (22.22%), effective rate 7.5 Hz" in text
    assert "usable for the speed check 57.1%" in text


def test_sessions_without_gps_have_no_gps_section(settings: Settings, fixtures_dir: Path):
    for name in ("v2_session", "v3_session", "v4_session"):
        result = ingest_path(fixtures_dir / name, settings)
        assert result.report["gps"] is None
        assert "GPS:" not in render_text(result.report)
        row = index.get_session(settings.index_db_path, result.session_id)
        assert row is not None and row.has_gps is False
        session_dir = settings.sessions_dir / result.session_id
        assert not (session_dir / "parquet" / "gps.parquet").exists()


def test_gps_ingest_zip_stores_raw_parquet_and_index(
    settings: Settings, fixtures_dir: Path, tmp_path: Path
):
    zip_path = zip_fixture(fixtures_dir / "v4_gps_session", tmp_path / "gps.zip")
    result = ingest_path(zip_path, settings)
    session_dir = settings.sessions_dir / result.session_id
    assert (session_dir / "raw" / "gps.csv").read_bytes() == (
        fixtures_dir / "v4_gps_session" / "gps.csv"
    ).read_bytes()
    row = index.get_session(settings.index_db_path, result.session_id)
    assert row is not None and row.has_gps is True and row.has_imu is False
    assert index.as_dict(row)["has_gps"] is True

    table = pq.read_table(session_dir / "parquet" / "gps.parquet")
    assert table.num_rows == 7
    assert set(table.column("session_id").to_pylist()) == {result.session_id}
    assert table.column("seq").to_pylist() == [250, 251, 252, 255, 0, 1, 2]
    assert table.column("ground_speed_mps").to_pylist()[2] == pytest.approx(11.111)
    assert table.column("fix_type_name").to_pylist()[0] == "noFix"
    assert table.column("uart_overflow").to_pylist() == [False] * 4 + [True] + [False] * 2
    for column in table.column_names:
        assert not any(word in column.lower() for word in POSITION_WORDS), column

    # Identical re-upload stays idempotent with gps.csv in the content hash.
    assert ingest_path(zip_path, settings).created is False


@pytest.mark.parametrize("extra", ["latitude", "lon_e7", "height_mm"])
def test_gps_csv_with_a_position_column_is_rejected(
    settings: Settings, fixtures_dir: Path, tmp_path: Path, extra: str
):
    session_dir = copy_fixture(fixtures_dir / "v4_gps_session", tmp_path / "session")
    path = session_dir / "gps.csv"
    lines = path.read_text().splitlines()
    lines = [lines[0] + f",{extra}"] + [line + ",0" for line in lines[1:]]
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(ContractError, match="unrecognized gps.csv header"):
        ingest_path(session_dir, settings)


def test_gps_app_columns_disagreeing_with_raw_hex_are_flagged(
    settings: Settings, fixtures_dir: Path, tmp_path: Path
):
    session_dir = copy_fixture(fixtures_dir / "v4_gps_session", tmp_path / "session")
    _rewrite_gps_cell(session_dir, 2, "ground_speed_mps", "12.0")  # raw_hex says 11.111
    report = ingest_path(session_dir, settings).report
    assert report["gps"]["mismatch_rows"] == [2]
    assert "1 GPS row(s) disagree with the app's own decoded columns" in report["reasons"]
    session_dir_out = settings.sessions_dir / report["session_id"]
    table = pq.read_table(session_dir_out / "parquet" / "gps.parquet")
    # The authoritative value (raw_hex) is what gets stored.
    assert table.column("ground_speed_mps").to_pylist()[2] == pytest.approx(11.111)


def test_gps_raw_hex_that_does_not_decode_fails_the_session(
    settings: Settings, fixtures_dir: Path, tmp_path: Path
):
    session_dir = copy_fixture(fixtures_dir / "v4_gps_session", tmp_path / "session")
    _rewrite_gps_cell(session_dir, 1, "raw_hex", "01fb")
    report = ingest_path(session_dir, settings).report
    assert report["status"] == "fail"
    assert report["gps"]["decode_error_rows"] == [1]
    assert "1 GPS row(s) failed to decode from raw_hex" in report["reasons"]
    table = pq.read_table(settings.sessions_dir / report["session_id"] / "parquet" / "gps.parquet")
    assert table.column("ground_speed").to_pylist()[1] is None


def test_index_without_has_gps_column_is_migrated(settings: Settings, fixtures_dir: Path):
    db_path = settings.index_db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY, created_utc TEXT, imported_utc TEXT NOT NULL,
                content_hash TEXT NOT NULL, app_version TEXT, ble_schema_version INTEGER,
                packet_count INTEGER, loss_percent REAL, status TEXT,
                has_imu INTEGER NOT NULL DEFAULT 0, defs_version TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO sessions (session_id, imported_utc, content_hash) VALUES (?, ?, ?)",
            ("20250101-000000-0000", "2025-01-01T00:00:00+00:00", "abc"),
        )

    old = index.get_session(db_path, "20250101-000000-0000")
    assert old is not None and old.has_gps is False
    result = ingest_path(fixtures_dir / "v4_gps_session", settings)
    row = index.get_session(db_path, result.session_id)
    assert row is not None and row.has_gps is True
