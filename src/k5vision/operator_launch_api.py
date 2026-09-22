"""Human-session API surface for the bounded Stage-One operator launch seam."""

from __future__ import annotations

from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, status

from k5vision.auth_sessions import UserSessionManager
from k5vision.operator_launch import (
    BoundedOperatorLaunchCoordinator,
    OperatorLauncher,
    OperatorLaunchError,
    OperatorLaunchErrorCode,
    OperatorLaunchReceipt,
    OperatorLaunchRequest,
    OperatorSourceResolver,
)
from k5vision.services.device_registry import DeviceRegistry

MAX_OPERATOR_BEARER_LENGTH = 512

_ERROR_STATUS = {
    OperatorLaunchErrorCode.UNAUTHORIZED: status.HTTP_401_UNAUTHORIZED,
    OperatorLaunchErrorCode.DEVICE_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    OperatorLaunchErrorCode.UNSUPPORTED_DEVICE: status.HTTP_422_UNPROCESSABLE_CONTENT,
    OperatorLaunchErrorCode.SOURCE_UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
    OperatorLaunchErrorCode.SOURCE_SCOPE_MISMATCH: status.HTTP_422_UNPROCESSABLE_CONTENT,
    OperatorLaunchErrorCode.LAUNCH_BUSY: status.HTTP_429_TOO_MANY_REQUESTS,
    OperatorLaunchErrorCode.LAUNCH_FAILURE: status.HTTP_503_SERVICE_UNAVAILABLE,
    OperatorLaunchErrorCode.REGISTRY_UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
}


def _extract_bearer(authorization: list[str] | None) -> str | None:
    header = authorization[0] if authorization and len(authorization) == 1 else ""
    scheme, separator, credential = header.partition(" ")
    if not (
        scheme.casefold() == "bearer"
        and separator
        and credential
        and credential.isascii()
        and len(credential) <= MAX_OPERATOR_BEARER_LENGTH
    ):
        return None
    return credential


def install_operator_launch_api(
    application: FastAPI,
    *,
    registry: DeviceRegistry | None,
    session_manager: UserSessionManager,
    source_resolver: OperatorSourceResolver | None,
    launcher: OperatorLauncher | None,
    max_active_launches: int = 4,
) -> BoundedOperatorLaunchCoordinator | None:
    """Install a human-session-only launch route with fail-closed runtime wiring."""
    coordinator: BoundedOperatorLaunchCoordinator | None = None
    if registry is not None and source_resolver is not None and launcher is not None:
        coordinator = BoundedOperatorLaunchCoordinator(
            registry,
            source_resolver,
            launcher,
            max_active_launches=max_active_launches,
        )

    application.state.operator_launch_coordinator = coordinator

    @application.post(
        "/api/v1/operator/live",
        response_model=OperatorLaunchReceipt,
        tags=["operator"],
    )
    async def launch_live_operator(
        payload: OperatorLaunchRequest,
        authorization: Annotated[list[str] | None, Header()] = None,
    ) -> OperatorLaunchReceipt:
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
                detail="Live operator runtime is not configured",
            )

        try:
            return await coordinator.launch(principal, payload)
        except OperatorLaunchError as exc:
            response_status = _ERROR_STATUS[exc.code]
            headers = {"WWW-Authenticate": "Bearer"} if response_status == 401 else None
            raise HTTPException(
                status_code=response_status,
                detail=str(exc),
                headers=headers,
            ) from None

    return coordinator
