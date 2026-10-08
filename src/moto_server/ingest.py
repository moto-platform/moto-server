"""Accepts a session upload (folder or zip), validates it against the session
contract, stores it, and produces report.json + parquet.

Storage layout under $MOTO_DATA_DIR/sessions/<session_id>/:
    raw/       the four contract files, copied verbatim (plus imu.csv / gps.csv if present)
    parquet/   telemetry.parquet (and imu.parquet / gps.parquet)
    report.json
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from moto_server import ble_schema, index, parquet, report
from moto_server.config import Settings

SESSION_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")
REQUIRED_FILES = {"meta.json", "telemetry.csv", "events.csv", "summary.json"}
OPTIONAL_FILES = {"imu.csv", "gps.csv"}
ALLOWED_FILES = REQUIRED_FILES | OPTIONAL_FILES


class InvalidSessionError(ValueError):
    """The uploaded session does not satisfy the session contract."""


class SessionConflictError(ValueError):
    """A session with this id already exists with different content."""

    def __init__(self, session_id: str):
        super().__init__(f"session {session_id} already exists with different content")
        self.session_id = session_id


@dataclass
class IngestResult:
    session_id: str
    created: bool  # False when an identical session already existed (idempotent replay)
    report: dict[str, Any]
    content_hash: str


def _reject_traversal(name: str) -> None:
    parts = Path(name).parts
    if Path(name).is_absolute() or ".." in parts:
        raise InvalidSessionError(f"unsafe path in archive: {name!r}")


def _extract_zip_safely(zip_path: Path, dest: Path) -> None:
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            _reject_traversal(info.filename)
            mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                raise InvalidSessionError(f"symlink in archive is not allowed: {info.filename!r}")
            target = dest / info.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)


def _find_session_base(root: Path) -> Path:
    """Returns the directory that directly holds meta.json: `root` itself, or
    root's single subdirectory (the zip-with-top-folder / <session_id>/ case)."""
    if (root / "meta.json").is_file():
        return root
    entries = [p for p in root.iterdir() if not p.name.startswith(".")]
    subdirs = [p for p in entries if p.is_dir()]
    if len(entries) == 1 and len(subdirs) == 1 and (subdirs[0] / "meta.json").is_file():
        return subdirs[0]
    raise InvalidSessionError(
        "meta.json not found at the archive root or inside a single top-level folder"
    )


def _validate_files(base: Path) -> None:
    entries = [p for p in base.iterdir() if not p.name.startswith(".")]
    subdirs = [p.name for p in entries if p.is_dir()]
    if subdirs:
        raise InvalidSessionError(f"unexpected subdirectory(ies) in session: {sorted(subdirs)}")

    present = {p.name for p in entries if p.is_file()}
    missing = REQUIRED_FILES - present
    if missing:
        raise InvalidSessionError(f"missing required file(s): {sorted(missing)}")
    unexpected = present - ALLOWED_FILES
    if unexpected:
        raise InvalidSessionError(f"unexpected file(s) in session: {sorted(unexpected)}")
    for name in present:
        if (base / name).is_symlink():
            raise InvalidSessionError(f"symlink is not allowed: {name!r}")


def _content_hash(base: Path) -> str:
    """sha256 over (filename, content) pairs, sorted by filename -- order-independent."""
    digest = hashlib.sha256()
    for name in sorted(ALLOWED_FILES):
        path = base / name
        if not path.is_file():
            continue
        digest.update(name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _load_meta(base: Path) -> dict[str, Any]:
    meta = json.loads((base / "meta.json").read_text())
    session_id = meta.get("session_id")
    if not isinstance(session_id, str) or not SESSION_ID_RE.match(session_id):
        raise InvalidSessionError(
            f"meta.json session_id {session_id!r} does not match ^\\d{{8}}-\\d{{6}}-[0-9a-f]{{4}}$"
        )
    return meta


def ingest_path(
    source: Path, settings: Settings, schema: dict[str, Any] | None = None
) -> IngestResult:
    """Ingests a session from a folder or a .zip file (files at the root or
    inside one top-level folder, per the session contract)."""
    schema = schema or ble_schema.load_default_schema()

    with tempfile.TemporaryDirectory(prefix="moto-server-ingest-") as tmp:
        tmp_path = Path(tmp)
        if source.is_file() and source.suffix == ".zip":
            extract_dir = tmp_path / "extracted"
            extract_dir.mkdir()
            _extract_zip_safely(source, extract_dir)
            base = _find_session_base(extract_dir)
        elif source.is_dir():
            base = _find_session_base(source)
        else:
            raise InvalidSessionError(f"source is neither a directory nor a .zip file: {source}")

        _validate_files(base)
        meta = _load_meta(base)
        session_id = meta["session_id"]
        content_hash = _content_hash(base)

        existing = index.get_session(settings.index_db_path, session_id)
        if existing is not None:
            if existing.content_hash == content_hash:
                existing_report_path = settings.sessions_dir / session_id / "report.json"
                return IngestResult(
                    session_id=session_id,
                    created=False,
                    report=json.loads(existing_report_path.read_text()),
                    content_hash=content_hash,
                )
            raise SessionConflictError(session_id)

        summary = json.loads((base / "summary.json").read_text())

        session_dir = settings.sessions_dir / session_id
        raw_dir = session_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        for name in ALLOWED_FILES:
            src_path = base / name
            if src_path.is_file():
                shutil.copy2(src_path, raw_dir / name)

        report_dict = report.build_report(raw_dir, session_id, meta, summary, schema)
        (session_dir / "report.json").write_text(json.dumps(report_dict, indent=2) + "\n")

        parquet.write_session_parquet(session_dir, session_id, schema)

        index.upsert_session(
            settings.index_db_path,
            index.SessionIndexRow(
                session_id=session_id,
                created_utc=meta.get("created_utc"),
                imported_utc=datetime.datetime.now(datetime.UTC).isoformat(),
                content_hash=content_hash,
                app_version=meta.get("app_version"),
                ble_schema_version=meta.get("ble_schema_version"),
                packet_count=report_dict["packet_count"],
                loss_percent=report_dict["loss"]["loss_percent"],
                status=report_dict["status"],
                has_imu=(raw_dir / "imu.csv").is_file(),
                has_gps=(raw_dir / "gps.csv").is_file(),
                defs_version=report_dict["defs_version"],
            ),
        )

        return IngestResult(
            session_id=session_id, created=True, report=report_dict, content_hash=content_hash
        )
