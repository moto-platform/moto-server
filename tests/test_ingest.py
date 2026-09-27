from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from moto_server.config import Settings
from moto_server.ingest import InvalidSessionError, SessionConflictError, ingest_path

from .conftest import copy_fixture, zip_fixture


def test_ingest_folder(settings: Settings, fixtures_dir: Path):
    result = ingest_path(fixtures_dir / "v2_session", settings)
    assert result.created is True
    assert result.session_id == "20260115-120000-a1b2"
    assert result.report["status"] == "warn"

    session_dir = settings.sessions_dir / result.session_id
    assert (session_dir / "raw" / "telemetry.csv").is_file()
    assert (session_dir / "parquet" / "telemetry.parquet").is_file()
    assert (session_dir / "report.json").is_file()


def test_ingest_zip_at_root(settings: Settings, fixtures_dir: Path, tmp_path: Path):
    zip_path = zip_fixture(
        fixtures_dir / "v3_session", tmp_path / "session.zip", wrap_in_folder=False
    )
    result = ingest_path(zip_path, settings)
    assert result.created is True
    assert result.session_id == "20260115-130000-c3d4"
    assert (settings.sessions_dir / result.session_id / "parquet" / "imu.parquet").is_file()


def test_ingest_zip_with_top_level_folder(settings: Settings, fixtures_dir: Path, tmp_path: Path):
    zip_path = zip_fixture(
        fixtures_dir / "v2_session", tmp_path / "session.zip", wrap_in_folder=True
    )
    result = ingest_path(zip_path, settings)
    assert result.created is True
    assert result.session_id == "20260115-120000-a1b2"


def test_ingest_zip_path_traversal_rejected(settings: Settings, fixtures_dir: Path, tmp_path: Path):
    zip_path = tmp_path / "evil.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for name in ["meta.json", "telemetry.csv", "events.csv", "summary.json"]:
            zf.write(fixtures_dir / "v2_session" / name, name)
        zf.writestr("../../etc/evil.txt", "pwned")

    with pytest.raises(InvalidSessionError, match="unsafe path"):
        ingest_path(zip_path, settings)


def test_ingest_zip_absolute_path_rejected(settings: Settings, fixtures_dir: Path, tmp_path: Path):
    zip_path = tmp_path / "evil_abs.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for name in ["meta.json", "telemetry.csv", "events.csv", "summary.json"]:
            zf.write(fixtures_dir / "v2_session" / name, name)
        zf.writestr("/etc/evil.txt", "pwned")

    with pytest.raises(InvalidSessionError, match="unsafe path"):
        ingest_path(zip_path, settings)


def test_ingest_missing_required_file_rejected(
    settings: Settings, fixtures_dir: Path, tmp_path: Path
):
    session_dir = copy_fixture(fixtures_dir / "v2_session", tmp_path / "session")
    (session_dir / "summary.json").unlink()
    with pytest.raises(InvalidSessionError, match="missing required file"):
        ingest_path(session_dir, settings)


def test_ingest_unexpected_file_rejected(settings: Settings, fixtures_dir: Path, tmp_path: Path):
    session_dir = copy_fixture(fixtures_dir / "v2_session", tmp_path / "session")
    (session_dir / "not_allowed.bin").write_bytes(b"\x00\x01")
    with pytest.raises(InvalidSessionError, match="unexpected file"):
        ingest_path(session_dir, settings)


def test_ingest_bad_session_id_rejected(settings: Settings, fixtures_dir: Path, tmp_path: Path):
    session_dir = copy_fixture(fixtures_dir / "v2_session", tmp_path / "session")
    meta_path = session_dir / "meta.json"
    meta_path.write_text(meta_path.read_text().replace("20260115-120000-a1b2", "not-a-session-id"))
    with pytest.raises(InvalidSessionError, match="session_id"):
        ingest_path(session_dir, settings)


def test_ingest_idempotent_same_content_returns_existing(settings: Settings, fixtures_dir: Path):
    first = ingest_path(fixtures_dir / "v2_session", settings)
    second = ingest_path(fixtures_dir / "v2_session", settings)
    assert first.created is True
    assert second.created is False
    assert first.content_hash == second.content_hash
    assert first.report == second.report


def test_ingest_conflict_same_id_different_content(
    settings: Settings, fixtures_dir: Path, tmp_path: Path
):
    ingest_path(fixtures_dir / "v2_session", settings)

    session_dir = copy_fixture(fixtures_dir / "v2_session", tmp_path / "session")
    events_path = session_dir / "events.csv"
    events_path.write_text(events_path.read_text() + "2026-01-15T12:00:07.000Z,1700,note,changed\n")

    with pytest.raises(SessionConflictError):
        ingest_path(session_dir, settings)
