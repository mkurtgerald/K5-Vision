"""K5 Vision control-plane API."""

from contextlib import asynccontextmanager
from hmac import compare_digest
from os import environ
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response, status
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from k5vision import __version__
from k5vision.domain.devices import Device, DeviceCreate
from k5vision.services.device_registry import (
    DEFAULT_DEVICE_CAPACITY,
    MAX_DEVICE_CAPACITY,
    MAX_DEVICE_PAGE_SIZE,
    DeviceRegistry,
    DeviceRegistryCapacityError,
    DeviceRegistryConflictError,
    DeviceRegistryStorageError,
)

CONTROL_PLANE_TOKEN_ENV = "K5_CONTROL_PLANE_TOKEN"
CONTROL_PLANE_SITE_ENV = "K5_CONTROL_PLANE_SITE_ID"
DEVICE_DB_PATH_ENV = "K5_DEVICE_DB_PATH"
MAX_DEVICE_REQUEST_BYTES = 16_384


class _RequestBodyTooLarge(Exception):
    pass


class _BoundedDeviceRequestBody:
    """Reject oversized device-registration bodies before application parsing."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or scope.get("path") != "/api/v1/devices"
        ):
            await self.app(scope, receive, send)
            return

        for name, value in scope.get("headers", []):
            if name.lower() != b"content-length":
                continue
            try:
                declared = int(value)
            except ValueError:
                await self._reject(send, status.HTTP_400_BAD_REQUEST, "Invalid request length")
                return
            if declared < 0:
                await self._reject(send, status.HTTP_400_BAD_REQUEST, "Invalid request length")
                return
            if declared > self.max_bytes:
                await self._reject(
                    send,
                    status.HTTP_413_CONTENT_TOO_LARGE,
                    "Request body too large",
                )
                return

        received_bytes = 0
        response_started = False

        async def bounded_receive() -> Message:
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > self.max_bytes:
                    raise _RequestBodyTooLarge
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, bounded_receive, tracking_send)
        except _RequestBodyTooLarge:
            if response_started:
                raise
            await self._reject(
                send,
                status.HTTP_413_CONTENT_TOO_LARGE,
                "Request body too large",
            )

    @staticmethod
    async def _reject(send: Send, response_status: int, detail: str) -> None:
        body = (f'{{"detail":"{detail}"}}').encode()
        await send(
            {
                "type": "http.response.start",
                "status": response_status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def _resolve_control_plane_token(explicit_token: str | None) -> str | None:
    token = explicit_token if explicit_token is not None else environ.get(CONTROL_PLANE_TOKEN_ENV)
    if token is None:
        return None
    token = token.strip()
    return token if token and token.isascii() else None


def _resolve_control_plane_site(explicit_site: str | None) -> str | None:
    site = explicit_site if explicit_site is not None else environ.get(CONTROL_PLANE_SITE_ENV)
    if site is None:
        return None
    site = site.strip()
    allowed = all(character.isalnum() or character in "._-" for character in site)
    return site if site and len(site) <= 128 and site.isascii() and allowed else None


def _resolve_device_db_path(explicit_path: str | Path | None) -> str | None:
    raw_path = explicit_path if explicit_path is not None else environ.get(DEVICE_DB_PATH_ENV)
    if raw_path is None:
        return None
    path = str(raw_path).strip()
    return path if path and path != ":memory:" else None


def create_app(
    *,
    control_plane_token: str | None = None,
    control_plane_site_id: str | None = None,
    device_db_path: str | Path | None = None,
    device_capacity: int = DEFAULT_DEVICE_CAPACITY,
    max_device_request_bytes: int = MAX_DEVICE_REQUEST_BYTES,
) -> FastAPI:
    """Build the control plane with fail-closed authenticated durable device state."""
    if not 1 <= device_capacity <= MAX_DEVICE_CAPACITY:
        raise ValueError(f"device_capacity must be between 1 and {MAX_DEVICE_CAPACITY}")
    if not 1024 <= max_device_request_bytes <= 1_048_576:
        raise ValueError("max_device_request_bytes must be between 1024 and 1048576")

    expected_token = _resolve_control_plane_token(control_plane_token)
    site_id = _resolve_control_plane_site(control_plane_site_id)
    database_path = _resolve_device_db_path(device_db_path)
    registry: DeviceRegistry | None = None
    if site_id is not None and database_path is not None:
        try:
            registry = DeviceRegistry(
                capacity=device_capacity,
                database_path=database_path,
                site_id=site_id,
            )
        except DeviceRegistryStorageError:
            registry = None

    @asynccontextmanager
    async def lifespan(_application: FastAPI):
        try:
            yield
        finally:
            if registry is not None:
                registry.close()

    application = FastAPI(
        title="K5 Vision Control Plane",
        version=__version__,
        description="Vendor-neutral security sensor management control plane.",
        lifespan=lifespan,
    )
    application.add_middleware(
        _BoundedDeviceRequestBody,
        max_bytes=max_device_request_bytes,
    )
    application.state.device_registry = registry

    async def require_control_plane_auth(
        authorization: Annotated[list[str] | None, Header()] = None,
    ) -> None:
        if expected_token is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Control-plane authentication is not configured",
            )

        header = authorization[0] if authorization and len(authorization) == 1 else ""
        scheme, separator, credential = header.partition(" ")
        if (
            scheme.lower() != "bearer"
            or not separator
            or not credential
            or not credential.isascii()
            or not compare_digest(credential, expected_token)
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if registry is None or site_id is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Control-plane device state is not configured",
            )

    def require_registry() -> DeviceRegistry:
        if registry is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Device registry unavailable",
            )
        return registry

    device_auth = [Depends(require_control_plane_auth)]

    @application.get("/api/v1/health", tags=["system"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @application.get(
        "/api/v1/devices",
        response_model=list[Device],
        tags=["devices"],
        dependencies=device_auth,
    )
    async def list_devices(
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_DEVICE_PAGE_SIZE)] = MAX_DEVICE_PAGE_SIZE,
    ) -> list[Device]:
        try:
            return require_registry().list(offset=offset, limit=limit)
        except DeviceRegistryStorageError:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Device registry unavailable",
            ) from None

    @application.post(
        "/api/v1/devices",
        response_model=Device,
        status_code=status.HTTP_201_CREATED,
        responses={status.HTTP_200_OK: {"description": "Existing identical enrollment"}},
        tags=["devices"],
        dependencies=device_auth,
    )
    async def register_device(payload: DeviceCreate, response: Response) -> Device:
        try:
            enrollment = require_registry().enroll(payload)
        except DeviceRegistryCapacityError:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Device registry capacity reached",
            ) from None
        except DeviceRegistryConflictError:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Device endpoint already enrolled with different metadata",
            ) from None
        except DeviceRegistryStorageError:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Device registry unavailable",
            ) from None
        if not enrollment.created:
            response.status_code = status.HTTP_200_OK
        return enrollment.device

    @application.get(
        "/api/v1/devices/{device_id}",
        response_model=Device,
        tags=["devices"],
        dependencies=device_auth,
    )
    async def get_device(device_id: UUID) -> Device:
        try:
            device = require_registry().get(device_id)
        except DeviceRegistryStorageError:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Device registry unavailable",
            ) from None
        if device is None:
            raise HTTPException(status_code=404, detail="Device not found")
        return device

    return application


app = create_app()
