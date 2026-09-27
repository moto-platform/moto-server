from __future__ import annotations

import io
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from moto_server.api import create_app
from moto_server.config import Settings

from .conftest import zip_fixture

AUTH = {"Authorization": "Bearer test-token"}


def _zip_bytes(fixtures_dir: Path, name: str, tmp_path: Path) -> bytes:
    zip_path = zip_fixture(fixtures_dir / name, tmp_path / f"{name}.zip")
    return zip_path.read_bytes()


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


def test_health_requires_no_auth(client: TestClient):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_protected_endpoint_without_token_is_401(client: TestClient):
    resp = client.get("/sessions")
    assert resp.status_code == 401


def test_protected_endpoint_with_wrong_token_is_401(client: TestClient):
    resp = client.get("/sessions", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


def test_api_returns_503_when_token_not_configured(tmp_path: Path):
    settings = Settings(data_dir=tmp_path / "data", api_token=None, max_upload_mb=100)
    client = TestClient(create_app(settings))
    resp = client.get("/sessions", headers=AUTH)
    assert resp.status_code == 503


def test_create_session_then_list_and_report(
    client: TestClient, fixtures_dir: Path, tmp_path: Path
):
    body = _zip_bytes(fixtures_dir, "v2_session", tmp_path)
    resp = client.post(
        "/sessions",
        headers=AUTH,
        files={"archive": ("session.zip", io.BytesIO(body), "application/zip")},
    )
    assert resp.status_code == 201
    report = resp.json()
    assert report["session_id"] == "20260115-120000-a1b2"

    listed = client.get("/sessions", headers=AUTH)
    assert listed.status_code == 200
    assert any(row["session_id"] == "20260115-120000-a1b2" for row in listed.json())

    got_report = client.get("/sessions/20260115-120000-a1b2/report", headers=AUTH)
    assert got_report.status_code == 200
    assert got_report.json()["session_id"] == "20260115-120000-a1b2"

    text_report = client.get(
        "/sessions/20260115-120000-a1b2/report", headers=AUTH, params={"format": "text"}
    )
    assert text_report.status_code == 200
    assert "20260115-120000-a1b2" in text_report.text


def test_report_unknown_session_is_404(client: TestClient):
    resp = client.get("/sessions/00000000-000000-ffff/report", headers=AUTH)
    assert resp.status_code == 404


def test_create_session_idempotent_replay_is_200(
    client: TestClient, fixtures_dir: Path, tmp_path: Path
):
    body = _zip_bytes(fixtures_dir, "v3_session", tmp_path)
    files = {"archive": ("session.zip", io.BytesIO(body), "application/zip")}
    first = client.post("/sessions", headers=AUTH, files=files)
    assert first.status_code == 201

    files = {"archive": ("session.zip", io.BytesIO(body), "application/zip")}
    second = client.post("/sessions", headers=AUTH, files=files)
    assert second.status_code == 200


def test_create_session_conflict_is_409(client: TestClient, fixtures_dir: Path, tmp_path: Path):
    body = _zip_bytes(fixtures_dir, "v3_session", tmp_path)
    files = {"archive": ("session.zip", io.BytesIO(body), "application/zip")}
    first = client.post("/sessions", headers=AUTH, files=files)
    assert first.status_code == 201

    mutated_dir = tmp_path / "mutated"
    shutil.copytree(fixtures_dir / "v3_session", mutated_dir)
    events_path = mutated_dir / "events.csv"
    events_path.write_text(events_path.read_text() + "2026-01-15T13:00:05.000Z,5500,note,changed\n")
    mutated_zip = zip_fixture(mutated_dir, tmp_path / "mutated.zip")

    files = {"archive": ("session.zip", io.BytesIO(mutated_zip.read_bytes()), "application/zip")}
    resp = client.post("/sessions", headers=AUTH, files=files)
    assert resp.status_code == 409


def test_create_session_invalid_is_422(client: TestClient, fixtures_dir: Path, tmp_path: Path):
    mutated_dir = tmp_path / "mutated"
    shutil.copytree(fixtures_dir / "v2_session", mutated_dir)
    (mutated_dir / "summary.json").unlink()
    mutated_zip = zip_fixture(mutated_dir, tmp_path / "mutated.zip")

    files = {"archive": ("session.zip", io.BytesIO(mutated_zip.read_bytes()), "application/zip")}
    resp = client.post("/sessions", headers=AUTH, files=files)
    assert resp.status_code == 422


def test_create_session_too_large_is_413(fixtures_dir: Path, tmp_path: Path):
    settings = Settings(data_dir=tmp_path / "data", api_token="test-token", max_upload_mb=0)
    client = TestClient(create_app(settings))
    body = _zip_bytes(fixtures_dir, "v2_session", tmp_path)
    resp = client.post(
        "/sessions",
        headers=AUTH,
        files={"archive": ("session.zip", io.BytesIO(body), "application/zip")},
    )
    assert resp.status_code == 413
