"""E-18 item 4: lost_since_prev, vsig_gap events and summary vehicle_signal_* keys must agree;
unknown gps / vehicle-signal block versions in meta.json fail the session."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from moto_server.config import Settings
from moto_server.defs import ble as defs_ble
from moto_server.ingest import ingest_path

from .conftest import copy_fixture

VSIG_FIXTURE = "v4_vsig_session"
GPS_FIXTURE = "v4_gps_session"


def _edit_json(path: Path, **changes) -> None:
    data = json.loads(path.read_text())
    data.update(changes)
    path.write_text(json.dumps(data))


def _replace(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new))


def _ingest(settings: Settings, fixtures_dir: Path, tmp_path: Path, fixture: str, edit=None):
    session_dir = copy_fixture(fixtures_dir / fixture, tmp_path / "session")
    if edit:
        edit(session_dir)
    return ingest_path(session_dir, settings).report


def test_fixture_figures_agree(settings: Settings, fixtures_dir: Path, tmp_path: Path):
    report = _ingest(settings, fixtures_dir, tmp_path, VSIG_FIXTURE)
    vsig = report["vehicle_signals"]
    assert vsig["consistency_issues"] == []
    assert vsig["lost_blocks"] == 2
    assert report["status"] == "warn"
    assert report["reasons"] == ["2 vehicle-signal block(s) lost"]


def test_lost_since_prev_must_match_the_block_seq_gap(settings, fixtures_dir, tmp_path):
    def edit(d: Path) -> None:
        _replace(d / "signals.csv", ",14,2,9500", ",14,3,9500")

    report = _ingest(settings, fixtures_dir, tmp_path, VSIG_FIXTURE, edit)
    issues = report["vehicle_signals"]["consistency_issues"]
    assert any("lost_since_prev 3 but block_seq gap is 2" in i for i in issues)
    assert report["status"] == "warn"


def test_lost_since_prev_on_a_later_row_of_a_block(settings, fixtures_dir, tmp_path):
    def edit(d: Path) -> None:
        _replace(d / "signals.csv", ",14,0,9500,8,0,62477", ",14,2,9500,8,0,62477")

    report = _ingest(settings, fixtures_dir, tmp_path, VSIG_FIXTURE, edit)
    assert report["vehicle_signals"]["consistency_issues"] == [
        "block seq 14: lost_since_prev is non-zero beyond the first row"
    ]


def test_missing_vsig_gap_event(settings, fixtures_dir, tmp_path):
    def edit(d: Path) -> None:
        _replace(d / "events.csv", "2026-01-15T16:00:03.000Z,9500,vsig_gap,missing=2;seq=14\n", "")

    report = _ingest(settings, fixtures_dir, tmp_path, VSIG_FIXTURE, edit)
    issues = report["vehicle_signals"]["consistency_issues"]
    assert issues == ["no vsig_gap event for block seq 14 (2 block(s) lost)"]


@pytest.mark.parametrize(
    ("new_detail", "expected"),
    [
        ("missing=5;seq=14", "says missing=5 but block_seq gap is 2"),
        ("missing=2;seq=12", "that is not in signals.csv"),
        ("garbage", "unparsable detail 'garbage'"),
    ],
)
def test_wrong_vsig_gap_event(settings, fixtures_dir, tmp_path, new_detail, expected):
    def edit(d: Path) -> None:
        _replace(d / "events.csv", "missing=2;seq=14", new_detail)

    report = _ingest(settings, fixtures_dir, tmp_path, VSIG_FIXTURE, edit)
    assert any(expected in i for i in report["vehicle_signals"]["consistency_issues"])
    assert report["status"] == "warn"


def test_duplicate_vsig_gap_event(settings, fixtures_dir, tmp_path):
    line = "2026-01-15T16:00:03.000Z,9500,vsig_gap,missing=2;seq=14\n"

    def edit(d: Path) -> None:
        _replace(d / "events.csv", line, line + line)

    report = _ingest(settings, fixtures_dir, tmp_path, VSIG_FIXTURE, edit)
    assert report["vehicle_signals"]["consistency_issues"] == [
        "more than one vsig_gap event for block seq 14"
    ]


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("vehicle_signal_block_count", 5, "summary vehicle_signal_block_count is 5"),
        ("vehicle_signal_record_count", 15, "summary vehicle_signal_record_count is 15"),
        ("vehicle_signal_lost_blocks", 3, "summary vehicle_signal_lost_blocks is 3"),
        ("vehicle_signal_lost_percent", 10.0, "summary vehicle_signal_lost_percent is 10.0"),
        ("vehicle_signal_lost_percent", "x", "summary vehicle_signal_lost_percent is 'x'"),
    ],
)
def test_summary_keys_must_match(settings, fixtures_dir, tmp_path, key, value, expected):
    def edit(d: Path) -> None:
        _edit_json(d / "summary.json", **{key: value})

    report = _ingest(settings, fixtures_dir, tmp_path, VSIG_FIXTURE, edit)
    assert any(expected in i for i in report["vehicle_signals"]["consistency_issues"])
    assert report["status"] == "warn"


def test_summary_without_vehicle_signal_keys_is_not_flagged(settings, fixtures_dir, tmp_path):
    def edit(d: Path) -> None:
        data = json.loads((d / "summary.json").read_text())
        for key in [k for k in data if k.startswith("vehicle_signal_")]:
            del data[key]
        (d / "summary.json").write_text(json.dumps(data))

    report = _ingest(settings, fixtures_dir, tmp_path, VSIG_FIXTURE, edit)
    assert report["vehicle_signals"]["consistency_issues"] == []


def test_summary_counts_blocks_but_no_signals_csv(settings, fixtures_dir, tmp_path):
    def edit(d: Path) -> None:
        (d / "signals.csv").unlink()

    report = _ingest(settings, fixtures_dir, tmp_path, VSIG_FIXTURE, edit)
    assert report["vehicle_signals"] is None
    assert (
        "summary.json counts vehicle-signal blocks but the session has no signals.csv"
        in (report["reasons"])
    )


@pytest.mark.parametrize(
    ("fixture", "key", "pinned"),
    [
        (VSIG_FIXTURE, "vehicle_signal_block_version", defs_ble.VSIG_BLOCK_VERSION),
        (GPS_FIXTURE, "gps_block_version", defs_ble.GPS_BLOCK_VERSION),
    ],
)
class TestBlockVersion:
    def test_pinned_version_is_accepted(
        self, settings, fixtures_dir, tmp_path, fixture, key, pinned
    ):
        report = _ingest(settings, fixtures_dir, tmp_path, fixture)
        assert not any(key in reason for reason in report["reasons"])

    @pytest.mark.parametrize("delta", [1, -1])
    def test_unknown_version_fails(
        self, settings, fixtures_dir, tmp_path, fixture, key, pinned, delta
    ):
        report = _ingest(
            settings,
            fixtures_dir,
            tmp_path,
            fixture,
            lambda d: _edit_json(d / "meta.json", **{key: pinned + delta}),
        )
        assert report["status"] == "fail"
        assert any(
            f"meta {key} {pinned + delta} is not supported" in r and f"version {pinned} only" in r
            for r in report["reasons"]
        )

    @pytest.mark.parametrize("bad", ["1", True, 1.0])
    def test_non_integer_version_fails(
        self, settings, fixtures_dir, tmp_path, fixture, key, pinned, bad
    ):
        report = _ingest(
            settings,
            fixtures_dir,
            tmp_path,
            fixture,
            lambda d: _edit_json(d / "meta.json", **{key: bad}),
        )
        assert report["status"] == "fail"
        assert any(f"meta {key} {bad!r} is not an integer" in r for r in report["reasons"])
