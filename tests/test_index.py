from __future__ import annotations

from pathlib import Path

from moto_server import index
from moto_server.config import Settings
from moto_server.ingest import ingest_path


def test_index_row_written_on_ingest(settings: Settings, fixtures_dir: Path):
    result = ingest_path(fixtures_dir / "v3_session", settings)

    row = index.get_session(settings.index_db_path, result.session_id)
    assert row is not None
    assert row.session_id == result.session_id
    assert row.content_hash == result.content_hash
    assert row.app_version == "2.0.0"
    assert row.ble_schema_version == 3
    assert row.packet_count == 5
    assert row.status == "warn"
    assert row.has_imu is True
    assert row.defs_version == result.report["defs_version"]


def test_index_list_sessions(settings: Settings, fixtures_dir: Path):
    ingest_path(fixtures_dir / "v2_session", settings)
    ingest_path(fixtures_dir / "v3_session", settings)

    rows = index.list_sessions(settings.index_db_path)
    ids = {row.session_id for row in rows}
    assert ids == {"20260115-120000-a1b2", "20260115-130000-c3d4"}
