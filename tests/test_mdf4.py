from __future__ import annotations

from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from asammdf import MDF

from vehicle_server import cli, mdf4


def _write_session(tmp_path: Path) -> Path:
    session_dir = tmp_path / "session"
    parquet_dir = session_dir / "parquet"
    parquet_dir.mkdir(parents=True)

    pq.write_table(
        pa.table(
            {
                "session_id": ["session"] * 3,
                "rx_utc": ["2026-01-01T00:00:00Z"] * 3,
                "rx_mono_ms": [1_000, 1_200, 1_400],
                "seq": [0, 1, 2],
                "packet_version": [3, 4, 4],
                "device_time_ms": [10_000, 10_200, 10_400],
                "decode_error": [None, None, None],
                "engine_speed_rpm": [3_000, 3_100, 3_200],
                "engine_speed_age_ms": [50, 60, 70],
                "engine_speed_valid": [True, False, True],
                "step_gap_max_ms": [12, None, 15],
                "mystery_count": [1, 2, 3],
            }
        ),
        parquet_dir / "telemetry.parquet",
    )
    pq.write_table(
        pa.table(
            {
                "session_id": ["session"] * 2,
                "rx_mono_ms": [1_000, 1_010],
                "device_time_ms": [10_000, 10_010],
                "sample_index": [0, 1],
                "ax_raw": [409, 819],
                "ax_g": [0.1, 0.2],
                "gx_dps": [1.5, 2.5],
                "block_missing_samples": [0, 0],
            }
        ),
        parquet_dir / "imu.parquet",
    )
    pq.write_table(
        pa.table(
            {
                "rx_mono_ms": [1_000, 1_200],
                "device_time_ms": [10_000, 10_200],
                "seq": [0, 1],
                "ground_speed_mps": [10.0, 11.0],
                "heading_of_motion_deg": [90.0, 91.0],
                "gnss_fix_ok": [True, True],
                "latitude": [41.0, 41.1],
                "longitude": [29.0, 29.1],
                "altitude_m": [100.0, 101.0],
            }
        ),
        parquet_dir / "gps.parquet",
    )
    pq.write_table(
        pa.table(
            {
                "signal": [
                    "ENGINE_SPEED",
                    "ENGINE_SPEED",
                    "ENGINE_SPEED",
                    "COOLANT_TEMP",
                    "COOLANT_TEMP",
                    "MYSTERY",
                    None,
                ],
                "unit": ["rpm", "rpm", "rpm", "degC", "degC", None, None],
                "value": [3_000.0, 3_100.0, 3_200.0, 80.0, None, 1.5, None],
                "sample_device_time_ms": [9_900, 10_000, 10_000, 9_950, 9_960, 9_970, 9_980],
                "state_name": ["valid", "valid", "valid", "valid", "stale", "valid", "valid"],
            }
        ),
        parquet_dir / "signals.parquet",
    )
    return session_dir


def test_export_round_trip(tmp_path: Path):
    session_dir = _write_session(tmp_path)
    output = mdf4.export_mdf4(session_dir)

    assert output == session_dir / "parquet" / "session.mf4"
    assert output.is_file()
    with MDF(output) as mdf:
        expected = {
            "engine_speed_rpm",
            "engine_speed_age_ms",
            "engine_speed_valid",
            "step_gap_max_ms",
            "mystery_count",
            "ax_g",
            "gx_dps",
            "ax_raw",
            "sample_index",
            "ground_speed_mps",
            "heading_of_motion_deg",
            "gnss_fix_ok",
            "ENGINE_SPEED",
            "COOLANT_TEMP",
            "MYSTERY",
        }
        assert expected <= set(mdf.channels_db)
        assert {
            "session_id",
            "rx_utc",
            "rx_mono_ms",
            "device_time_ms",
            "decode_error",
            "state_name",
        }.isdisjoint(mdf.channels_db)

        speed = mdf.get("engine_speed_rpm")
        np.testing.assert_allclose(speed.timestamps, [10.0, 10.2, 10.4])
        np.testing.assert_allclose(speed.samples, [3_000, 3_100, 3_200])
        assert speed.unit == "rpm"

        age = mdf.get("engine_speed_age_ms")
        assert age.unit == "ms"
        np.testing.assert_allclose(age.samples, [50, 60, 70])

        valid = mdf.get("engine_speed_valid")
        assert valid.unit == ""
        np.testing.assert_array_equal(valid.samples, [1, 0, 1])

        gap = mdf.get("step_gap_max_ms")
        assert gap.unit == "ms"
        assert np.isnan(gap.samples[1])
        assert gap.samples[0] == 12 and gap.samples[2] == 15

        unknown = mdf.get("mystery_count")
        assert unknown.unit == ""

        assert mdf.get("ax_g").unit == "g"
        assert mdf.get("gx_dps").unit == "deg/s"
        assert mdf.get("ax_raw").unit == ""
        assert mdf.get("sample_index").unit == ""
        assert mdf.get("ground_speed_mps").unit == "m/s"
        assert mdf.get("heading_of_motion_deg").unit == "deg"
        assert mdf.get("gnss_fix_ok").unit == ""

        engine = mdf.get("ENGINE_SPEED")
        np.testing.assert_allclose(engine.timestamps, [9.9, 10.0])
        np.testing.assert_allclose(engine.samples, [3_000, 3_200])
        assert engine.unit == "rpm"
        assert mdf.get("COOLANT_TEMP").unit == "degC"
        assert mdf.get("MYSTERY").unit == ""

        group_names = [group.channel_group.acq_name for group in mdf.groups]
        assert group_names.count("telemetry") == 1
        assert group_names.count("imu") == 1
        assert group_names.count("gps") == 1
        assert group_names.count("vehicle signals") == 3


def test_position_is_gated_by_the_flag(tmp_path: Path):
    session_dir = _write_session(tmp_path)

    default = mdf4.export_mdf4(session_dir)
    with MDF(default) as mdf:
        assert "ground_speed_mps" in mdf.channels_db
        for name in ("latitude", "longitude", "altitude_m"):
            assert name not in mdf.channels_db

    flagged = mdf4.export_mdf4(session_dir, include_position=True)
    with MDF(flagged) as mdf:
        latitude = mdf.get("latitude")
        assert latitude.unit == ""
        np.testing.assert_allclose(latitude.timestamps, [10.0, 10.2])
        np.testing.assert_allclose(latitude.samples, [41.0, 41.1])
        assert "longitude" in mdf.channels_db
        assert "altitude_m" in mdf.channels_db


def test_no_defs_column_is_mistaken_for_position():
    schema = mdf4.ble_schema.load_default_schema()
    columns = {
        **mdf4._known_units(schema),
        **mdf4._imu_units(schema),
        **mdf4._gps_units(schema),
    }
    assert "throttle_pos_pct" in columns
    assert [name for name in columns if mdf4._is_position_column(name)] == []


def test_missing_imu_is_skipped(tmp_path: Path):
    session_dir = _write_session(tmp_path)
    (session_dir / "parquet" / "imu.parquet").unlink()

    output = mdf4.export_mdf4(session_dir)
    with MDF(output) as mdf:
        assert "engine_speed_rpm" in mdf.channels_db
        assert "ax_g" not in mdf.channels_db
        assert "telemetry" in [group.channel_group.acq_name for group in mdf.groups]


def test_empty_session_raises_value_error(tmp_path: Path):
    empty = tmp_path / "empty"
    (empty / "parquet").mkdir(parents=True)
    with pytest.raises(ValueError, match="no MDF4 data"):
        mdf4.export_mdf4(empty)
    with pytest.raises(ValueError, match="no parquet directory"):
        mdf4.export_mdf4(tmp_path / "missing")


def test_v2_telemetry_falls_back_to_receive_time(tmp_path: Path):
    session_dir = tmp_path / "v2"
    parquet_dir = session_dir / "parquet"
    parquet_dir.mkdir(parents=True)
    pq.write_table(
        pa.table(
            {
                "rx_mono_ms": [1_000, 1_200],
                "device_time_ms": [None, None],
                "engine_speed_rpm": [3_000, 3_100],
                "engine_speed_age_ms": [None, None],
                "engine_speed_valid": [True, False],
            }
        ),
        parquet_dir / "telemetry.parquet",
    )

    output = mdf4.export_mdf4(session_dir)
    with MDF(output) as mdf:
        speed = mdf.get("engine_speed_rpm")
        np.testing.assert_allclose(speed.timestamps, [1.0, 1.2])
        np.testing.assert_allclose(speed.samples, [3_000, 3_100])
        assert "device_time_ms" not in mdf.channels_db


def _run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, argv: list[str]) -> int:
    monkeypatch.setenv("VEHICLE_DATA_DIR", str(tmp_path / "data"))
    with pytest.raises(SystemExit) as exc_info:
        cli.main(argv)
    return exc_info.value.code


def test_cli_export(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fixtures_dir: Path, capsys):
    assert _run(monkeypatch, tmp_path, ["import", str(fixtures_dir / "v3_session")]) == 0
    assert _run(monkeypatch, tmp_path, ["export", "20260115-130000-c3d4"]) == 0

    output = Path(capsys.readouterr().out.strip().splitlines()[-1])
    assert output.name == "20260115-130000-c3d4.mf4"
    assert output.is_file()
    with MDF(output) as mdf:
        assert "engine_speed_rpm" in mdf.channels_db

    args = cli.build_parser().parse_args(["export", "some-id", "--include-position"])
    assert args.session_id == "some-id" and args.include_position is True


def test_cli_export_unknown_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys):
    assert _run(monkeypatch, tmp_path, ["export", "00000000-000000-ffff"]) == 1
    assert "error:" in capsys.readouterr().err
