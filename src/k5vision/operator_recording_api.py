"""Human-session API surfaces for bounded Stage-One recording and playback."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from k5vision.auth_sessions import UserSessionManager
from k5vision.media.playback_schedule import PlaybackRate
from k5vision.operator_export import (
    BoundedOperatorExportCoordinator,
    OperatorExportError,
    OperatorExportErrorCode,
)
from k5vision.operator_launch import OperatorSourceResolver
from k5vision.operator_playback import (
    BoundedOperatorPlaybackCoordinator,
    OperatorPlaybackError,
    OperatorPlaybackControlAction,
    OperatorPlaybackControlReceipt,
    OperatorPlaybackErrorCode,
    OperatorPlaybackReceipt,
    OperatorPlaybackRequest,
    WindowsMixedOperatorPlaybackLauncher,
)
from k5vision.operator_playback_timeline import (
    BoundedOperatorPlaybackTimeline,
    OperatorPlaybackTimelineReceipt,
)
from k5vision.operator_recording import (
    BoundedOperatorRecordingCoordinator,
    OperatorRecordingError,
    OperatorRecordingErrorCode,
    OperatorRecordingReceipt,
    OperatorRecordingRequest,
)
from k5vision.services.device_registry import DeviceRegistry

MAX_OPERATOR_RECORDING_BEARER_LENGTH = 512

_RECORDING_ERROR_STATUS = {
    OperatorRecordingErrorCode.UNAUTHORIZED: status.HTTP_401_UNAUTHORIZED,
    OperatorRecordingErrorCode.INSUFFICIENT_PERMISSION: status.HTTP_403_FORBIDDEN,
    OperatorRecordingErrorCode.DEVICE_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    OperatorRecordingErrorCode.UNSUPPORTED_DEVICE: status.HTTP_422_UNPROCESSABLE_CONTENT,
    OperatorRecordingErrorCode.SOURCE_UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
    OperatorRecordingErrorCode.SOURCE_SCOPE_MISMATCH: status.HTTP_422_UNPROCESSABLE_CONTENT,
    OperatorRecordingErrorCode.RECORDING_BUSY: status.HTTP_429_TOO_MANY_REQUESTS,
    OperatorRecordingErrorCode.RECORDING_FAILURE: status.HTTP_503_SERVICE_UNAVAILABLE,
    OperatorRecordingErrorCode.REGISTRY_UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
}

_PLAYBACK_ERROR_STATUS = {
    OperatorPlaybackErrorCode.UNAUTHORIZED: status.HTTP_401_UNAUTHORIZED,
    OperatorPlaybackErrorCode.RECORDING_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    OperatorPlaybackErrorCode.RECORDING_INVALID: status.HTTP_503_SERVICE_UNAVAILABLE,
    OperatorPlaybackErrorCode.DEVICE_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    OperatorPlaybackErrorCode.UNSUPPORTED_DEVICE: status.HTTP_422_UNPROCESSABLE_CONTENT,
    OperatorPlaybackErrorCode.SOURCE_UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
    OperatorPlaybackErrorCode.SOURCE_SCOPE_MISMATCH: status.HTTP_422_UNPROCESSABLE_CONTENT,
    OperatorPlaybackErrorCode.WINDOW_INVALID: status.HTTP_422_UNPROCESSABLE_CONTENT,
    OperatorPlaybackErrorCode.PLAYBACK_BUSY: status.HTTP_429_TOO_MANY_REQUESTS,
    OperatorPlaybackErrorCode.CONTROL_CONFLICT: status.HTTP_409_CONFLICT,
    OperatorPlaybackErrorCode.CONTROL_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    OperatorPlaybackErrorCode.CONTROL_FORBIDDEN: status.HTTP_403_FORBIDDEN,
    OperatorPlaybackErrorCode.PLAYBACK_FAILURE: status.HTTP_503_SERVICE_UNAVAILABLE,
    OperatorPlaybackErrorCode.REGISTRY_UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
}

_EXPORT_ERROR_STATUS = {
    OperatorExportErrorCode.EXPORT_TOO_LARGE: status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
    OperatorExportErrorCode.EXPORT_BUSY: status.HTTP_429_TOO_MANY_REQUESTS,
    OperatorExportErrorCode.EXPORT_INVALID: status.HTTP_503_SERVICE_UNAVAILABLE,
}


def _extract_bearer(authorization: list[str] | None) -> str | None:
    header = authorization[0] if authorization and len(authorization) == 1 else ""
    scheme, separator, credential = header.partition(" ")
    if not (
        scheme.casefold() == "bearer"
        and separator
        and credential
        and credential.isascii()
        and len(credential) <= MAX_OPERATOR_RECORDING_BEARER_LENGTH
    ):
        return None
    return credential


def install_operator_recording_api(
    application: FastAPI,
    *,
    registry: DeviceRegistry | None,
    session_manager: UserSessionManager,
    source_resolver: OperatorSourceResolver | None,
    recording_root: str | Path | None,
) -> BoundedOperatorRecordingCoordinator | None:
    """Install recording/playback only when private source and storage are configured."""
    recording_coordinator: BoundedOperatorRecordingCoordinator | None = None
    playback_coordinator: BoundedOperatorPlaybackCoordinator | None = None
    timeline_coordinator: BoundedOperatorPlaybackTimeline | None = None
    export_coordinator: BoundedOperatorExportCoordinator | None = None
    if registry is not None and recording_root is not None:
        try:
            timeline_coordinator = BoundedOperatorPlaybackTimeline(registry, recording_root)
            export_coordinator = BoundedOperatorExportCoordinator(registry, recording_root)
        except (TypeError, ValueError):
            timeline_coordinator = None
            export_coordinator = None
    if registry is not None and source_resolver is not None and recording_root is not None:
        try:
            recording_coordinator = BoundedOperatorRecordingCoordinator(
                registry,
                source_resolver,
                recording_root,
            )
            playback_coordinator = BoundedOperatorPlaybackCoordinator(
                registry,
                source_resolver,
                recording_root,
                WindowsMixedOperatorPlaybackLauncher(),
            )
        except (TypeError, ValueError):
            recording_coordinator = None
            playback_coordinator = None

    application.state.operator_recording_coordinator = recording_coordinator
    application.state.operator_playback_coordinator = playback_coordinator
    application.state.operator_playback_timeline = timeline_coordinator
    application.state.operator_export_coordinator = export_coordinator

    @application.post(
        "/api/v1/operator/recordings",
        response_model=OperatorRecordingReceipt,
        status_code=status.HTTP_201_CREATED,
        tags=["operator"],
    )
    async def create_operator_recording(
        payload: OperatorRecordingRequest,
        authorization: Annotated[list[str] | None, Header()] = None,
    ) -> OperatorRecordingReceipt:
        credential = _extract_bearer(authorization)
        principal = await session_manager.resolve(credential or "")
        if principal is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if recording_coordinator is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Operator recording is not configured",
            )

        try:
            return await recording_coordinator.record(principal, payload)
        except OperatorRecordingError as exc:
            response_status = _RECORDING_ERROR_STATUS[exc.code]
            headers = {"WWW-Authenticate": "Bearer"} if response_status == 401 else None
            raise HTTPException(
                status_code=response_status,
                detail=str(exc),
                headers=headers,
            ) from None

    @application.get(
        "/api/v1/operator/recordings/{recording_id}/timeline",
        response_model=OperatorPlaybackTimelineReceipt,
        tags=["operator"],
    )
    async def get_operator_recording_timeline(
        recording_id: UUID,
        authorization: Annotated[list[str] | None, Header()] = None,
    ) -> OperatorPlaybackTimelineReceipt:
        credential = _extract_bearer(authorization)
        principal = await session_manager.resolve(credential or "")
        if principal is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if timeline_coordinator is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Operator playback timeline is not configured",
            )

        try:
            return await timeline_coordinator.inspect(principal, recording_id)
        except OperatorPlaybackError as exc:
            response_status = _PLAYBACK_ERROR_STATUS[exc.code]
            headers = {"WWW-Authenticate": "Bearer"} if response_status == 401 else None
            raise HTTPException(
                status_code=response_status,
                detail=str(exc),
                headers=headers,
            ) from None

    @application.get(
        "/api/v1/operator/recordings/{recording_id}/export",
        tags=["operator"],
    )
    async def export_operator_recording(
        recording_id: UUID,
        authorization: Annotated[list[str] | None, Header()] = None,
    ) -> StreamingResponse:
        credential = _extract_bearer(authorization)
        principal = await session_manager.resolve(credential or "")
        if principal is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if export_coordinator is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Operator export is not configured",
            )

        try:
            handle = await export_coordinator.begin_export(principal, recording_id)
        except OperatorPlaybackError as exc:
            response_status = _PLAYBACK_ERROR_STATUS[exc.code]
            headers = {"WWW-Authenticate": "Bearer"} if response_status == 401 else None
            raise HTTPException(
                status_code=response_status,
                detail=str(exc),
                headers=headers,
            ) from None
        except OperatorExportError as exc:
            raise HTTPException(
                status_code=_EXPORT_ERROR_STATUS[exc.code],
                detail=str(exc),
            ) from None

        return StreamingResponse(
            export_coordinator.stream(handle),
            media_type=f"multipart/mixed; boundary={handle.boundary}",
            headers={
                "Cache-Control": "no-store",
                "Content-Length": str(handle.response_bytes),
                "X-Content-Type-Options": "nosniff",
                "X-K5-Recording-Id": str(recording_id),
                "X-K5-Descriptor-Validated": "true",
            },
        )

    @application.post(
        "/api/v1/operator/recordings/{recording_id}/playback",
        response_model=OperatorPlaybackReceipt,
        tags=["operator"],
    )
    async def play_operator_recording(
        recording_id: UUID,
        authorization: Annotated[list[str] | None, Header()] = None,
        stream_token: Annotated[
            str,
            Query(
                min_length=1,
                max_length=256,
                pattern=r"^[A-Za-z0-9._:-]{1,256}$",
            ),
        ] = "main",
        start_ms: Annotated[int, Query(ge=0, le=7 * 24 * 60 * 60 * 1000)] = 0,
        end_ms: Annotated[int | None, Query(ge=0, le=7 * 24 * 60 * 60 * 1000)] = None,
        rate: PlaybackRate = PlaybackRate.NORMAL,
        width: Annotated[int, Query(ge=640, le=16_384)] = 1280,
        height: Annotated[int, Query(ge=240, le=16_384)] = 720,
        control_id: UUID | None = None,
    ) -> OperatorPlaybackReceipt:
        credential = _extract_bearer(authorization)
        principal = await session_manager.resolve(credential or "")
        if principal is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if playback_coordinator is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Operator playback is not configured",
            )

        request = OperatorPlaybackRequest(
            recording_id=recording_id,
            control_id=control_id,
            stream_token=stream_token,
            start_ms=start_ms,
            end_ms=end_ms,
            rate=rate,
            width=width,
            height=height,
        )
        try:
            return await playback_coordinator.play(principal, request)
        except OperatorPlaybackError as exc:
            response_status = _PLAYBACK_ERROR_STATUS[exc.code]
            headers = {"WWW-Authenticate": "Bearer"} if response_status == 401 else None
            raise HTTPException(
                status_code=response_status,
                detail=str(exc),
                headers=headers,
            ) from None

    async def _apply_playback_control(
        control_id: UUID,
        action: OperatorPlaybackControlAction,
        authorization: list[str] | None,
    ) -> OperatorPlaybackControlReceipt:
        credential = _extract_bearer(authorization)
        principal = await session_manager.resolve(credential or "")
        if principal is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if playback_coordinator is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Operator playback is not configured",
            )
        try:
            return await playback_coordinator.control(principal, control_id, action)
        except OperatorPlaybackError as exc:
            response_status = _PLAYBACK_ERROR_STATUS[exc.code]
            headers = {"WWW-Authenticate": "Bearer"} if response_status == 401 else None
            raise HTTPException(
                status_code=response_status,
                detail=str(exc),
                headers=headers,
            ) from None

    @application.post(
        "/api/v1/operator/playback-controls/{control_id}/pause",
        response_model=OperatorPlaybackControlReceipt,
        tags=["operator"],
    )
    async def pause_operator_playback(
        control_id: UUID,
        authorization: Annotated[list[str] | None, Header()] = None,
    ) -> OperatorPlaybackControlReceipt:
        return await _apply_playback_control(
            control_id,
            OperatorPlaybackControlAction.PAUSE,
            authorization,
        )

    @application.post(
        "/api/v1/operator/playback-controls/{control_id}/resume",
        response_model=OperatorPlaybackControlReceipt,
        tags=["operator"],
    )
    async def resume_operator_playback(
        control_id: UUID,
        authorization: Annotated[list[str] | None, Header()] = None,
    ) -> OperatorPlaybackControlReceipt:
        return await _apply_playback_control(
            control_id,
            OperatorPlaybackControlAction.RESUME,
            authorization,
        )

    return recording_coordinator
