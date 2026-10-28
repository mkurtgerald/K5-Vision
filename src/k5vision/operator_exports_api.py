"""Current human authority for export preparation, polling and normal downloads."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import StreamingResponse

from k5vision.domain.users import UserRole
from k5vision.operator_exports import ExportError, ExportManager, export_start

_PREFIX = "/api/v1/operator/cameras/{device_id}/exports"
_COOKIE = "k5_operator_media"


class _ExportSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: str = Field(min_length=20, max_length=40)
    duration: float = Field(ge=1, le=3600, allow_inf_nan=False, strict=True)


def install_operator_exports_api(
    app: FastAPI,
    *,
    service,
    state_root: Path,
    require_session,
    authenticate,
    authorized_selection,
    admit_origin,
    manager: ExportManager | None = None,
) -> None:
    exports = manager or ExportManager(state_root / "exports", service=service)
    app.state.operator_exports = exports
    session_dependency = Depends(require_session)

    def permit(principal, camera_id):
        source = authorized_selection(principal, camera_id)
        if principal.role is UserRole.VIEWER:
            raise HTTPException(403, "MP4 export requires an operator or administrator")
        return source

    async def current(session, camera_id):
        principal, token = await authenticate(session[1])
        if principal.id != session[0].id:
            raise HTTPException(401, "Unauthorized")
        return principal, token, permit(principal, camera_id)

    def job_for(identifier, principal, camera_id):
        try:
            job = exports.get(identifier, owner=principal.id, camera=camera_id)
        except ExportError:
            raise HTTPException(503, "export_cache_unavailable") from None
        if job is None:
            raise HTTPException(404, "Export not found")
        return job

    def media_cookie(response, request, token):
        # Use the existing clock-safe media session cookie. Authentication
        # remains authoritative server-side on every poll/file request.
        response.set_cookie(
            _COOKIE,
            token,
            path="/media",
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
        )

    @app.post(_PREFIX, status_code=202, tags=["operator exports"])
    async def prepare_export(
        device_id: UUID,
        payload: _ExportSelection,
        request: Request,
        response: Response,
        session=session_dependency,
    ):
        response.headers["Cache-Control"] = "no-store"
        admit_origin(request)
        principal, token, source = await current(session, device_id)
        if service.status() not in {"ready", "disk_full"}:
            raise HTTPException(503, "export_unavailable")
        try:
            start = export_start(payload.start)
            job = await exports.create(
                owner=principal.id,
                binding=source.binding,
                start=start,
                duration=payload.duration,
                authorize=lambda: current(session, device_id),
            )
        except HTTPException:
            raise
        except (ValueError, OverflowError):
            raise HTTPException(422, "export_invalid_selection") from None
        except ExportError as error:
            status = 409 if str(error) in {"export_recording_gap", "export_busy"} else 503
            raise HTTPException(status, str(error)) from None
        except Exception:
            raise HTTPException(503, "export_unavailable") from None
        await current(session, device_id)
        media_cookie(response, request, token)
        return job.public()

    @app.get(_PREFIX + "/{export_id}", tags=["operator exports"])
    async def export_status(
        device_id: UUID,
        export_id: UUID,
        response: Response,
        session=session_dependency,
    ):
        response.headers["Cache-Control"] = "no-store"
        principal, _token, _source = await current(session, device_id)
        job = job_for(export_id, principal, device_id)
        try:
            await exports.verify_ready(job)
        except ExportError:
            raise HTTPException(503, "export_busy") from None
        await current(session, device_id)
        return job.public()

    async def file_session(request):
        if request.headers.getlist("authorization"):
            return await require_session(request)
        headers = request.headers.getlist("cookie")
        if len(headers) != 1 or len(headers[0]) > 4096:
            raise HTTPException(401, "Unauthorized")
        values = []
        for piece in headers[0].split(";"):
            name, separator, value = piece.strip().partition("=")
            if name == _COOKIE and separator:
                values.append(value)
        if len(values) != 1 or not 32 <= len(values[0]) <= 128:
            raise HTTPException(401, "Unauthorized")
        return await authenticate(values[0])

    @app.get(_PREFIX + "/{export_id}/file", tags=["operator exports"])
    @app.get("/media/exports/{device_id}/{export_id}.mp4", tags=["operator exports"])
    async def export_file(device_id: UUID, export_id: UUID, request: Request):
        session = await file_session(request)
        principal, _token, _source = await current(session, device_id)
        job = job_for(export_id, principal, device_id)
        try:
            source = await exports.open_file(job)
        except (ExportError, OSError):
            raise HTTPException(409, "export_file_unavailable") from None
        try:
            await current(session, device_id)
        except BaseException:
            exports.release_file(job, source)
            raise

        async def content():
            # Check authority again after every suspended send. Revocation
            # terminates a transfer instead of allowing an old media cookie to
            # finish downloading newly restricted footage.
            remaining = job.bytes
            while remaining:
                await current(session, device_id)
                piece = source.read(min(65536, remaining))
                if not piece:
                    raise RuntimeError("Export transfer interrupted")
                remaining -= len(piece)
                yield piece

        class OwnedFileResponse(StreamingResponse):
            async def __call__(self, scope, receive, send):
                try:
                    await super().__call__(scope, receive, send)
                finally:
                    exports.release_file(job, source)

        return OwnedFileResponse(
            content(),
            media_type="video/mp4",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": 'attachment; filename="' + job.public()["filename"] + '"',
                "Content-Length": str(job.bytes),
                "X-Content-Type-Options": "nosniff",
            },
        )

    previous_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with previous_lifespan(application):
            try:
                yield
            finally:
                await exports.aclose()

    app.router.lifespan_context = lifespan
