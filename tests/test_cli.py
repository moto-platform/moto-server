from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from moto_server import cli


def _run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, argv: list[str]) -> int:
    monkeypatch.setenv("MOTO_DATA_DIR", str(tmp_path / "data"))
    with pytest.raises(SystemExit) as exc_info:
        cli.main(argv)
    return exc_info.value.code


def test_cli_import_ok_exits_zero_even_with_warnings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fixtures_dir: Path, capsys
):
    code = _run(monkeypatch, tmp_path, ["import", str(fixtures_dir / "v2_session")])
    assert code == 0
    out = capsys.readouterr().out
    assert "20260115-120000-a1b2" in out


def test_cli_import_conflict_exits_two(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fixtures_dir: Path
):
    _run(monkeypatch, tmp_path, ["import", str(fixtures_dir / "v2_session")])

    mutated_dir = tmp_path / "mutated"
    shutil.copytree(fixtures_dir / "v2_session", mutated_dir)
    events_path = mutated_dir / "events.csv"
    events_path.write_text(events_path.read_text() + "2026-01-15T12:00:07.000Z,1700,note,changed\n")

    code = _run(monkeypatch, tmp_path, ["import", str(mutated_dir)])
    assert code == 2


def test_cli_import_invalid_exits_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fixtures_dir: Path
):
    mutated_dir = tmp_path / "mutated"
    shutil.copytree(fixtures_dir / "v2_session", mutated_dir)
    (mutated_dir / "summary.json").unlink()

    code = _run(monkeypatch, tmp_path, ["import", str(mutated_dir)])
    assert code == 1


def test_cli_report_after_import(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fixtures_dir: Path, capsys
):
    _run(monkeypatch, tmp_path, ["import", str(fixtures_dir / "v3_session")])
    code = _run(monkeypatch, tmp_path, ["report", "20260115-130000-c3d4", "--format", "json"])
    assert code == 0
    out = capsys.readouterr().out
    assert "20260115-130000-c3d4" in out


def test_cli_report_unknown_session_exits_one(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    code = _run(monkeypatch, tmp_path, ["report", "00000000-000000-ffff"])
    assert code == 1


def test_cli_list_after_import(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fixtures_dir: Path, capsys
):
    _run(monkeypatch, tmp_path, ["import", str(fixtures_dir / "v2_session")])
    code = _run(monkeypatch, tmp_path, ["list"])
    assert code == 0
    out = capsys.readouterr().out
    assert "20260115-120000-a1b2" in out
