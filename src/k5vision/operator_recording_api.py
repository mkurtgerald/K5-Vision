"""Human-session API surface for bounded Stage-One recording."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, status

from k5vision.auth_sessions import UserSessionManager
from k5vision.operator_launch import OperatorSourceResolver
from k5vision.operator_recording import (
    BoundedOperatorRecordingCoordinator,
    OperatorRecordingError,
    OperatorRecordingErrorCode,
    OperatorRecordingReceipt,
    OperatorRecordingRequest,
)
from k5vision.services.device_registry import DeviceRegistry

MAX_OPERATOR_RECORDING_BEARER_LENGTH = 512

_ERROR_STATUS = {
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
    """Install recording only when the existing private source and storage root are configured."""
    coordinator: BoundedOperatorRecordingCoordinator | None = None
    if registry is not None and source_resolver is not None and recording_root is not None:
        try:
            coordinator = BoundedOperatorRecordingCoordinator(
                registry,
                source_resolver,
                recording_root,
            )
        except (TypeError, ValueError):
            coordinator = None

    application.state.operator_recording_coordinator = coordinator

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
        if coordinator is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Operator recording is not configured",
            )

        try:
            return await coordinator.record(principal, payload)
        except OperatorRecordingError as exc:
            response_status = _ERROR_STATUS[exc.code]
            headers = {"WWW-Authenticate": "Bearer"} if response_status == 401 else None
            raise HTTPException(
                status_code=response_status,
                detail=str(exc),
                headers=headers,
            ) from None

    return coordinator
