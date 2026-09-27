"""Runtime configuration, read from the environment (12-factor style)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    api_token: str | None
    max_upload_mb: int

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def index_db_path(self) -> Path:
        return self.data_dir / "index.sqlite"


def load_settings() -> Settings:
    data_dir = Path(os.environ.get("MOTO_DATA_DIR", "./data")).resolve()
    api_token = os.environ.get("MOTO_API_TOKEN") or None
    max_upload_mb = int(os.environ.get("MOTO_MAX_UPLOAD_MB", "100"))
    return Settings(data_dir=data_dir, api_token=api_token, max_upload_mb=max_upload_mb)
