from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import pytest

from moto_server.config import Settings

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        api_token="test-token",
        max_upload_mb=100,
    )


def zip_fixture(src_dir: Path, dest_zip: Path, wrap_in_folder: bool = False) -> Path:
    """Zips a fixture session directory. With wrap_in_folder, entries are
    nested under one top-level folder named after the source directory
    (the "zip-with-top-folder" case the contract also accepts)."""
    with zipfile.ZipFile(dest_zip, "w") as zf:
        for path in sorted(src_dir.iterdir()):
            if not path.is_file():
                continue
            arcname = f"{src_dir.name}/{path.name}" if wrap_in_folder else path.name
            zf.write(path, arcname)
    return dest_zip


def copy_fixture(src_dir: Path, dest_dir: Path) -> Path:
    shutil.copytree(src_dir, dest_dir)
    return dest_dir
