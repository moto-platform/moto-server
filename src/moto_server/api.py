"""FastAPI application: session ingestion + read API.

Bearer token auth (MOTO_API_TOKEN) guards every endpoint except GET /health.
If MOTO_API_TOKEN is not set, protected endpoints return 503 (the API does
not silently run open).
"""

from __future__ import annotations

import hmac
import json
import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse

from moto_server import index, report
from moto_server.config import Settings, load_settings
from moto_server.ingest import InvalidSessionError, SessionConflictError, ingest_path

_UPLOAD_CHUNK_BYTES = 1024 * 1024


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


SettingsDep = Annotated[Settings, Depends(get_settings)]


def require_token(request: Request, settings: SettingsDep) -> None:
    if settings.api_token is None:
        raise HTTPException(status_code=503, detail="MOTO_API_TOKEN is not configured")
    auth = request.headers.get("authorization", "")
    # Constant-time comparison so response timing does not leak the token.
    if not hmac.compare_digest(auth.encode(), f"Bearer {settings.api_token}".encode()):
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")


AuthDep = Depends(require_token)


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="moto-server", version="0.2.0")
    app.state.settings = settings or load_settings()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/sessions", dependencies=[AuthDep], status_code=201)
    async def create_session(archive: UploadFile, settings: SettingsDep) -> JSONResponse:
        with tempfile.TemporaryDirectory(prefix="moto-server-upload-") as tmp:
            tmp_zip = Path(tmp) / "upload.zip"
            total = 0
            with tmp_zip.open("wb") as out:
                while chunk := await archive.read(_UPLOAD_CHUNK_BYTES):
                    total += len(chunk)
                    if total > settings.max_upload_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=f"upload exceeds MOTO_MAX_UPLOAD_MB={settings.max_upload_mb}",
                        )
                    out.write(chunk)

            try:
                result = ingest_path(tmp_zip, settings)
            except InvalidSessionError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            except SessionConflictError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

            status_code = 201 if result.created else 200
            return JSONResponse(status_code=status_code, content=result.report)

    @app.get("/sessions", dependencies=[AuthDep])
    def list_sessions(settings: SettingsDep) -> list[dict]:
        rows = index.list_sessions(settings.index_db_path)
        return [index.as_dict(row) for row in rows]

    @app.get("/sessions/{session_id}/report", dependencies=[AuthDep])
    def get_session_report(session_id: str, settings: SettingsDep, format: str = "json"):
        report_path = settings.sessions_dir / session_id / "report.json"
        if not report_path.is_file():
            raise HTTPException(status_code=404, detail=f"unknown session: {session_id}")

        report_dict = json.loads(report_path.read_text())
        if format == "text":
            return PlainTextResponse(report.render_text(report_dict))
        return JSONResponse(content=report_dict)

    return app


app = create_app()
