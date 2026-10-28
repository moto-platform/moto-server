"""Vehicle-signal block (D-066 item 4): signals.csv contract, catalog re-decode,
report, parquet and index."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from vehicle_server import ble_schema, index
from vehicle_server.config import Settings
from vehicle_server.decode import (
    SIGNALS_HEADER,
    ContractError,
    csv_column_for_field,
    decode_signals_csv,
    group_signal_blocks,
)
from vehicle_server.defs import ble as defs_ble
from vehicle_server.defs import vehicle_catalog
from vehicle_server.ingest import ingest_path
from vehicle_server.report import render_text

from .conftest import copy_fixture, zip_fixture

SCHEMA = ble_schema.load_default_schema()
VSIG = SCHEMA["vehicleSignalBlock"]
FIXTURE = "v4_vsig_session"

ENGINE_LOAD_DID = vehicle_catalog.SIGNALS["ENGINE_LOAD"]["did"]  # outside the sender table (D-066)
UNKNOWN_DID = max(vehicle_catalog.BY_DID) + 1


def _rewrite_cell(session_dir: Path, row: int, column: str, value: str) -> None:
    path = session_dir / "signals.csv"
    header, *rows = path.read_text().splitlines()
    fields = rows[row].split(",")
    fields[header.split(",").index(column)] = value
    rows[row] = ",".join(fields)
    path.write_text("\n".join([header, *rows]) + "\n")


def _row_of(rows: list, did: int, nth: int = 0):
    return [r for r in rows if r.did == did][nth]


def _table(settings: Settings, session_id: str):
    return pq.read_table(settings.sessions_dir / session_id / "parquet" / "signals.parquet")


def test_signals_header_matches_the_schema_and_the_catalog():
    wire_fields = [f["name"] for f in (*VSIG["headerFields"], *VSIG["recordFields"])]
    columns = {csv_column_for_field(n) for n in wire_fields} - {
        "version",
        "seq",
        "reserved",
        "record_count",
    }
    assert columns <= set(SIGNALS_HEADER)
    assert {"block_seq", "lost_since_prev", "signal", "value"} <= set(SIGNALS_HEADER)
    assert {s["name"]: s["value"] for s in VSIG["state"]["values"]} == {
        name: value for value, name in defs_ble.VSIG_STATES.items()
    }


def test_fixture_covers_the_d066_cases(fixtures_dir: Path):
    rows = decode_signals_csv(fixtures_dir / FIXTURE / "signals.csv")
    assert len(rows) == 16
    assert len(group_signal_blocks(rows)) == 4
    assert all(r.decode_error is None and not r.mismatches for r in rows)
    # A DID outside the sender's table (D-066 item 3 candidate; D-068 promoted every catalog
    # DID to polled) in state valid: it is decoded generically from the catalog, whatever
    # its status, so the status itself is not asserted here.
    assert vehicle_catalog.SIGNALS["ENGINE_LOAD"]["status"] in (
        vehicle_catalog.STATUS_POLLED,
        vehicle_catalog.STATUS_CANDIDATE,
    )
    assert _row_of(rows, ENGINE_LOAD_DID).state_name == "valid"
    assert {r.state_name for r in rows} == {"valid", "stale", "skipped", "negativeResponse"}
    assert _row_of(rows, UNKNOWN_DID).signal is None


def test_decode_recomputes_value_from_did_and_raw(fixtures_dir: Path):
    rows = decode_signals_csv(fixtures_dir / FIXTURE / "signals.csv")
    rpm = _row_of(rows, vehicle_catalog.SIGNALS["ENGINE_SPEED"]["did"])
    assert (rpm.signal, rpm.unit, rpm.raw, rpm.value) == ("ENGINE_SPEED", "rpm", 12_000, 3000.0)
    load = _row_of(rows, ENGINE_LOAD_DID, 1)
    assert load.signal == "ENGINE_LOAD" and load.unit == "%"
    assert load.value == pytest.approx(128 * 100 / 255)
    stale = _row_of(rows, vehicle_catalog.SIGNALS["COOLANT_TEMP"]["did"])
    assert stale.state_name == "stale" and stale.value == 80.0  # stale rows keep the value
    nrc = _row_of(rows, vehicle_catalog.SIGNALS["FUEL_LEVEL"]["did"])
    assert nrc.state_name == "negativeResponse" and nrc.raw == 0x31 and nrc.value is None
    skipped = _row_of(rows, vehicle_catalog.SIGNALS["THROTTLE_POS"]["did"], 1)
    assert skipped.state_name == "skipped" and skipped.value is None


@pytest.mark.parametrize("extra", ["raw_hex", "latitude", "unit"])
def test_signals_csv_with_another_header_is_rejected(
    settings: Settings, fixtures_dir: Path, tmp_path: Path, extra: str
):
    session_dir = copy_fixture(fixtures_dir / FIXTURE, tmp_path / "session")
    path = session_dir / "signals.csv"
    lines = path.read_text().splitlines()
    lines = [lines[0] + f",{extra}"] + [line + ",0" for line in lines[1:]]
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(ContractError, match="unrecognized signals.csv header"):
        ingest_path(session_dir, settings)


def test_signals_csv_with_a_missing_column_is_rejected(
    settings: Settings, fixtures_dir: Path, tmp_path: Path
):
    session_dir = copy_fixture(fixtures_dir / FIXTURE, tmp_path / "session")
    path = session_dir / "signals.csv"
    lines = path.read_text().splitlines()
    path.write_text("\n".join(",".join(line.split(",")[:-1]) for line in lines) + "\n")
    with pytest.raises(ContractError, match="unrecognized signals.csv header"):
        ingest_path(session_dir, settings)


def test_report_of_the_fixture_session(settings: Settings, fixtures_dir: Path):
    report = ingest_path(fixtures_dir / FIXTURE, settings).report
    vsig = report["vehicle_signals"]
    assert vsig["block_count"] == 4
    assert vsig["record_count"] == 16
    assert vsig["lost_blocks"] == 2  # seq 12, 13
    assert vsig["lost_percent"] == pytest.approx(2 / 6 * 100)
    assert vsig["total_records"] == 8
    assert vsig["decode_error_rows"] == vsig["mismatch_rows"] == vsig["malformed_rows"] == []
    assert vsig["unknown_dids"] == [f"0x{UNKNOWN_DID:04X}"]
    assert vsig["meta_block_version"] == VSIG["version"]

    load = vsig["signals"][f"0x{ENGINE_LOAD_DID:04X}"]
    assert load["name"] == "ENGINE_LOAD" and load["unit"] == "%"
    assert load["states"] == {"valid": 2}
    assert (load["min"], load["max"]) == (pytest.approx(40.0), pytest.approx(128 * 100 / 255))
    fuel = vsig["signals"][f"0x{vehicle_catalog.SIGNALS['FUEL_LEVEL']['did']:04X}"]
    assert fuel["states"] == {"negativeResponse": 2} and fuel["nrcs"] == ["0x31"]
    coolant = vsig["signals"][f"0x{vehicle_catalog.SIGNALS['COOLANT_TEMP']['did']:04X}"]
    assert coolant["states"] == {"stale": 1, "valid": 1}
    assert coolant["valid_count"] == 1 and coolant["last_value"] == 85.0

    # Only the lost blocks make the session warn; NRC, stale, skipped and an unknown DID do not.
    assert report["status"] == "warn"
    assert report["reasons"] == ["2 vehicle-signal block(s) lost"]
    text = render_text(report)
    assert "vehicle signals: 16 record(s) in 4 block(s), 2 lost (33.33%), 8 of 8" in text
    assert "(ENGINE_LOAD): states {'valid': 2}, valid 40.0 .. 50.19" in text
    assert "(unknown DID): states {'valid': 2}" in text
    assert "NRC ['0x31']" in text


def test_sessions_without_signals_have_no_section(settings: Settings, fixtures_dir: Path):
    for name in ("v2_session", "v3_session", "v4_session", "v4_gps_session"):
        result = ingest_path(fixtures_dir / name, settings)
        assert result.report["vehicle_signals"] is None
        assert "vehicle signals:" not in render_text(result.report)
        row = index.get_session(settings.index_db_path, result.session_id)
        assert row is not None and row.has_vehicle_signals is False
        assert not (
            settings.sessions_dir / result.session_id / "parquet" / "signals.parquet"
        ).exists()


def test_ingest_zip_stores_raw_parquet_and_index(
    settings: Settings, fixtures_dir: Path, tmp_path: Path
):
    zip_path = zip_fixture(fixtures_dir / FIXTURE, tmp_path / "vsig.zip")
    result = ingest_path(zip_path, settings)
    session_dir = settings.sessions_dir / result.session_id
    assert (session_dir / "raw" / "signals.csv").read_bytes() == (
        fixtures_dir / FIXTURE / "signals.csv"
    ).read_bytes()
    row = index.get_session(settings.index_db_path, result.session_id)
    assert row is not None and row.has_vehicle_signals is True and row.has_gps is False
    assert index.as_dict(row)["has_vehicle_signals"] is True
    # Identical re-upload stays idempotent with signals.csv in the content hash.
    assert ingest_path(zip_path, settings).created is False


def test_parquet_has_one_channel_per_catalog_signal_with_its_unit(
    settings: Settings, fixtures_dir: Path
):
    result = ingest_path(fixtures_dir / FIXTURE, settings)
    table = _table(settings, result.session_id)
    assert table.num_rows == 16
    assert set(table.column("session_id").to_pylist()) == {result.session_id}
    data = table.to_pydict()

    load = [i for i, s in enumerate(data["signal"]) if s == "ENGINE_LOAD"]
    assert len(load) == 2  # a catalog DID is a channel like any other
    assert {data["unit"][i] for i in load} == {"%"}
    assert [data["value"][i] for i in load] == [pytest.approx(40.0), pytest.approx(128 * 100 / 255)]
    assert [data["did"][i] for i in load] == [ENGINE_LOAD_DID] * 2
    assert [data["state_name"][i] for i in load] == ["valid", "valid"]
    # Receive time and node time of the block, and the time of the DID's last answer.
    assert data["device_time_ms"][load[0]] == 9_200
    assert data["sample_device_time_ms"][load[0]] == 9_200 - 90

    assert {s for s in data["signal"] if s} >= {"ENGINE_SPEED", "VEHICLE_SPEED", "ENGINE_LOAD"}
    # Valid rows only carry a value: stale, skipped and NRC rows are null, raw is kept.
    by_state = {}
    for state, value in zip(data["state_name"], data["value"], strict=True):
        by_state.setdefault(state, []).append(value)
    assert by_state["stale"] == [None]
    assert by_state["skipped"] == [None]
    assert by_state["negativeResponse"] == [None, None]
    assert by_state["valid"].count(None) == 2  # only the unknown DID has no value
    nrc = data["state_name"].index("negativeResponse")
    assert data["raw"][nrc] == 0x31 and data["sample_device_time_ms"][nrc] is None


def test_unknown_did_is_kept_with_an_empty_value(settings: Settings, fixtures_dir: Path):
    result = ingest_path(fixtures_dir / FIXTURE, settings)
    data = _table(settings, result.session_id).to_pydict()
    unknown = [i for i, did in enumerate(data["did"]) if did == UNKNOWN_DID]
    assert len(unknown) == 2
    assert [data["signal"][i] for i in unknown] == [None, None]
    assert [data["unit"][i] for i in unknown] == [None, None]
    assert [data["value"][i] for i in unknown] == [None, None]
    assert [data["raw"][i] for i in unknown] == [77, 78]  # the wire values survive
    assert [data["state_name"][i] for i in unknown] == ["valid", "valid"]
    assert result.report["status"] == "warn"  # only the lost blocks, not the unknown DID


def test_app_value_disagreeing_with_the_catalog_is_flagged(
    settings: Settings, fixtures_dir: Path, tmp_path: Path
):
    session_dir = copy_fixture(fixtures_dir / FIXTURE, tmp_path / "session")
    _rewrite_cell(session_dir, 0, "value", "3050.0")  # did + raw say 3000.0
    _rewrite_cell(session_dir, 1, "signal", "OTHER_SIGNAL")
    report = ingest_path(session_dir, settings).report
    assert report["vehicle_signals"]["mismatch_rows"] == [0, 1]
    assert (
        "2 vehicle-signal row(s) disagree with the app's own decoded columns" in (report["reasons"])
    )
    assert report["status"] == "warn"  # a consistency warning, not a failure
    data = _table(settings, report["session_id"]).to_pydict()
    assert data["value"][0] == 3000.0  # the recomputed value is what gets stored
    assert data["signal"][1] == "VEHICLE_SPEED"


def test_app_value_for_an_unknown_did_is_flagged(
    settings: Settings, fixtures_dir: Path, tmp_path: Path
):
    session_dir = copy_fixture(fixtures_dir / FIXTURE, tmp_path / "session")
    _rewrite_cell(session_dir, 7, "value", "77.0")  # the catalog has no DID to decode with
    report = ingest_path(session_dir, settings).report
    assert report["vehicle_signals"]["mismatch_rows"] == [7]


def test_state_nrc_value_must_stay_empty(settings: Settings, fixtures_dir: Path, tmp_path: Path):
    session_dir = copy_fixture(fixtures_dir / FIXTURE, tmp_path / "session")
    _rewrite_cell(session_dir, 6, "value", "19.0")  # negative response: no value
    report = ingest_path(session_dir, settings).report
    assert report["vehicle_signals"]["mismatch_rows"] == [6]


def test_raw_that_does_not_fit_the_did_length_is_malformed(
    settings: Settings, fixtures_dir: Path, tmp_path: Path
):
    session_dir = copy_fixture(fixtures_dir / FIXTURE, tmp_path / "session")
    # ENGINE_LOAD has a 1-byte data field; 300 does not fit. The app writes no value.
    _rewrite_cell(session_dir, 5, "raw", "300")
    _rewrite_cell(session_dir, 5, "value", "")
    report = ingest_path(session_dir, settings).report
    assert report["vehicle_signals"]["malformed_rows"] == [5]
    assert report["vehicle_signals"]["mismatch_rows"] == []
    assert any("does not fit the DID's length" in reason for reason in report["reasons"])
    data = _table(settings, report["session_id"]).to_pydict()
    assert data["value"][5] is None and data["raw"][5] == 300


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("state", "9", "state: unknown value 9"),
        ("state", "-1", "state: -1 is out of range"),
        ("did", "65536", "did: 65536 is out of range"),
        ("age_ms", "65536", "age_ms: 65536 is out of range"),
        ("raw", "4294967296", "raw: 4294967296 is out of range"),
        ("raw", "12.5", "raw: not an integer"),
        ("block_seq", "256", "block_seq: 256 is out of range"),
    ],
)
def test_invalid_wire_values_fail_the_session(
    settings: Settings, fixtures_dir: Path, tmp_path: Path, column: str, value: str, message: str
):
    session_dir = copy_fixture(fixtures_dir / FIXTURE, tmp_path / "session")
    _rewrite_cell(session_dir, 2, column, value)
    report = ingest_path(session_dir, settings).report
    vsig = report["vehicle_signals"]
    assert vsig["decode_error_rows"] == [2]
    assert report["status"] == "fail"
    assert "1 vehicle-signal row(s) failed validation" in report["reasons"]
    data = _table(settings, report["session_id"]).to_pydict()
    assert data["decode_error"][2] is not None and message in data["decode_error"][2]
    assert data["did"][2] is None and data["value"][2] is None
    assert data["rx_mono_ms"][2] == 9_100  # the receive time survives


def test_wire_range_limits_are_accepted(settings: Settings, fixtures_dir: Path, tmp_path: Path):
    session_dir = copy_fixture(fixtures_dir / FIXTURE, tmp_path / "session")
    _rewrite_cell(session_dir, 7, "did", "65535")
    _rewrite_cell(session_dir, 7, "age_ms", "65535")
    _rewrite_cell(session_dir, 7, "raw", "4294967295")
    report = ingest_path(session_dir, settings).report
    assert report["vehicle_signals"]["decode_error_rows"] == []


def test_meta_block_version_is_optional_and_checked(
    settings: Settings, fixtures_dir: Path, tmp_path: Path
):
    older = copy_fixture(fixtures_dir / FIXTURE, tmp_path / "older")
    meta_path = older / "meta.json"
    meta = json.loads(meta_path.read_text())
    del meta["vehicle_signal_block_version"]
    meta_path.write_text(json.dumps(meta))
    report = ingest_path(older, settings).report
    assert report["vehicle_signals"]["meta_block_version"] is None
    assert report["reasons"] == ["2 vehicle-signal block(s) lost"]

    newer = copy_fixture(fixtures_dir / FIXTURE, tmp_path / "newer")
    meta = json.loads((newer / "meta.json").read_text())
    meta["vehicle_signal_block_version"] = defs_ble.VSIG_BLOCK_VERSION + 1
    meta["session_id"] = "20260115-160001-c9d1"
    (newer / "meta.json").write_text(json.dumps(meta))
    report = ingest_path(newer, settings).report
    assert report["status"] == "fail"
    assert any("vehicle_signal_block_version" in reason for reason in report["reasons"])


def test_index_without_has_vehicle_signals_column_is_migrated(
    settings: Settings, fixtures_dir: Path
):
    db_path = settings.index_db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY, created_utc TEXT, imported_utc TEXT NOT NULL,
                content_hash TEXT NOT NULL, app_version TEXT, ble_schema_version INTEGER,
                packet_count INTEGER, loss_percent REAL, status TEXT,
                has_imu INTEGER NOT NULL DEFAULT 0, defs_version TEXT,
                has_gps INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            "INSERT INTO sessions (session_id, imported_utc, content_hash) VALUES (?, ?, ?)",
            ("20250101-000000-0000", "2025-01-01T00:00:00+00:00", "abc"),
        )

    old = index.get_session(db_path, "20250101-000000-0000")
    assert old is not None and old.has_vehicle_signals is False
    result = ingest_path(fixtures_dir / FIXTURE, settings)
    row = index.get_session(db_path, result.session_id)
    assert row is not None and row.has_vehicle_signals is True
