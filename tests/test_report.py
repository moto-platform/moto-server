from __future__ import annotations

from pathlib import Path

import pytest

from moto_server.config import Settings
from moto_server.ingest import ingest_path


def test_v2_report_exact_loss_and_stale_value(settings: Settings, fixtures_dir: Path):
    result = ingest_path(fixtures_dir / "v2_session", settings)
    report = result.report

    assert report["packet_count"] == 7
    assert report["loss"]["lost_count"] == 2
    assert report["loss"]["loss_percent"] == pytest.approx(2 / 9 * 100)
    assert report["telemetry_layout"] == "v2"
    assert report["consistency"]["decode_error_rows"] == []
    assert report["consistency"]["mismatch_rows"] == []

    battery = report["valid_ratios"]["battery_voltage"]
    assert battery["total_count"] == 7
    assert battery["valid_count"] == 6
    assert battery["valid_percent"] == pytest.approx(6 / 7 * 100)


def test_v3_report_exact_can_health_and_imu_gap(settings: Settings, fixtures_dir: Path):
    result = ingest_path(fixtures_dir / "v3_session", settings)
    report = result.report

    assert report["packet_count"] == 5
    assert report["loss"]["lost_count"] == 1
    assert report["loss"]["loss_percent"] == pytest.approx(1 / 6 * 100)

    coolant = report["range_checks"]["coolant_temp"]
    assert coolant["out_of_range_count"] == 1
    assert coolant["checked_count"] == 5

    can = report["can_health"]
    assert can["max_tec"] == 255
    assert can["max_rec"] == 255
    assert can["max_bus_off_count"] == 1
    assert can["max_unanswered_did_count"] == 2
    assert can["latched_bus_off"] is True
    assert can["latched_foreign_tester"] is False

    imu = report["imu"]
    assert imu["sample_count"] == 10
    assert imu["missing_samples"] == 4
    assert imu["loss_percent"] == pytest.approx(4 / 14 * 100)
    assert imu["scale_mismatch_rows"] == []
    assert imu["block_count"] == 2
    assert imu["blocks_with_sensor_reconfigured"] == 1
    assert imu["blocks_with_device_overflow"] == 0
    assert imu["blocks_with_read_error"] == 0

    assert report["status"] == "warn"
    assert "one or more signals had out-of-range values" in report["reasons"]
    assert "4 missing IMU sample(s)" in report["reasons"]
    assert any("sensor reconfiguration" in reason for reason in report["reasons"])


def test_report_fails_on_decode_error(settings: Settings, fixtures_dir: Path, tmp_path: Path):
    from .conftest import copy_fixture

    session_dir = copy_fixture(fixtures_dir / "v2_session", tmp_path / "session")
    telemetry_path = session_dir / "telemetry.csv"
    lines = telemetry_path.read_text().splitlines()
    # Corrupt the raw_hex of the first data row so it fails to decode.
    header, first, *rest = lines
    fields = first.split(",")
    fields[4] = "00"  # too short to be a valid v2/v3 payload
    lines = [header, ",".join(fields), *rest]
    telemetry_path.write_text("\n".join(lines) + "\n")

    result = ingest_path(session_dir, settings)
    assert result.report["status"] == "fail"
    assert len(result.report["consistency"]["decode_error_rows"]) == 1
