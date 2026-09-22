"""Human-session API surface for bounded visible Stage-One playback."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, status

from k5vision.auth_sessions import UserSessionManager
from k5vision.operator_launch import OperatorSourceResolver
from k5vision.operator_playback import (
    BoundedOperatorPlaybackCoordinator,
    OperatorPlaybackError,
    OperatorPlaybackErrorCode,
    OperatorPlaybackLauncher,
    OperatorPlaybackReceipt,
    OperatorPlaybackRequest,
)
from k5vision.services.device_registry import DeviceRegistry

MAX_OPERATOR_PLAYBACK_BEARER_LENGTH = 512

_ERROR_STATUS = {
    OperatorPlaybackErrorCode.UNAUTHORIZED: status.HTTP_401_UNAUTHORIZED,
    OperatorPlaybackErrorCode.DEVICE_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    OperatorPlaybackErrorCode.UNSUPPORTED_DEVICE: status.HTTP_422_UNPROCESSABLE_CONTENT,
    OperatorPlaybackErrorCode.SOURCE_UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
    OperatorPlaybackErrorCode.SOURCE_SCOPE_MISMATCH: status.HTTP_422_UNPROCESSABLE_CONTENT,
    OperatorPlaybackErrorCode.RECORDING_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    OperatorPlaybackErrorCode.RECORDING_INVALID: status.HTTP_422_UNPROCESSABLE_CONTENT,
    OperatorPlaybackErrorCode.RECORDING_SCOPE_MISMATCH: status.HTTP_422_UNPROCESSABLE_CONTENT,
    OperatorPlaybackErrorCode.PLAYBACK_BUSY: status.HTTP_429_TOO_MANY_REQUESTS,
    OperatorPlaybackErrorCode.PLAYBACK_FAILURE: status.HTTP_503_SERVICE_UNAVAILABLE,
    OperatorPlaybackErrorCode.REGISTRY_UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
}


def _extract_bearer(authorization: list[str] | None) -> str | None:
    header = authorization[0] if authorization and len(authorization) == 1 else ""
    scheme, separator, credential = header.partition(" ")
    if not (
        scheme.casefold() == "bearer"
        and separator
        and credential
        and credential.isascii()
        and len(credential) <= MAX_OPERATOR_PLAYBACK_BEARER_LENGTH
    ):
        return None
    return credential


def install_operator_playback_api(
    application: FastAPI,
    *,
    registry: DeviceRegistry | None,
    session_manager: UserSessionManager,
    source_resolver: OperatorSourceResolver | None,
    recording_root: str | Path | None,
    launcher: OperatorPlaybackLauncher | None = None,
) -> BoundedOperatorPlaybackCoordinator | None:
    """Install playback only when the existing source and recording boundaries exist."""
    coordinator: BoundedOperatorPlaybackCoordinator | None = None
    if registry is not None and source_resolver is not None and recording_root is not None:
        try:
            coordinator = BoundedOperatorPlaybackCoordinator(
                registry,
                source_resolver,
                recording_root,
                launcher=launcher,
            )
        except (TypeError, ValueError):
            coordinator = None

    application.state.operator_playback_coordinator = coordinator

    @application.post(
        "/api/v1/operator/playback",
        response_model=OperatorPlaybackReceipt,
        tags=["operator"],
    )
    async def launch_operator_playback(
        payload: OperatorPlaybackRequest,
        authorization: Annotated[list[str] | None, Header()] = None,
    ) -> OperatorPlaybackReceipt:
        credential = _extract_bearer(authorization)
        principal = await session_manager.resolve(credential or "")
        if principal is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if coordinator is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Operator playback is not configured",
            )

        try:
            return await coordinator.play(principal, payload)
        except OperatorPlaybackError as exc:
            response_status = _ERROR_STATUS[exc.code]
            headers = {"WWW-Authenticate": "Bearer"} if response_status == 401 else None
            raise HTTPException(
                status_code=response_status,
                detail=str(exc),
                headers=headers,
            ) from None

    return coordinator
