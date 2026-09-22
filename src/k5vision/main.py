"""K5 Vision control-plane API."""

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from enum import StrEnum
from hmac import compare_digest
from math import ceil
from os import environ
from pathlib import Path
from time import monotonic
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response, status
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from k5vision import __version__
from k5vision.domain.devices import Device, DeviceCreate
from k5vision.domain.users import UserRole
from k5vision.operator_launch import OperatorLauncher, OperatorSourceResolver
from k5vision.operator_launch_api import install_operator_launch_api
from k5vision.operator_recording_api import install_operator_recording_api
from k5vision.services.device_registry import (
    DEFAULT_DEVICE_CAPACITY,
    MAX_DEVICE_CAPACITY,
    MAX_DEVICE_PAGE_SIZE,
    DeviceRegistry,
    DeviceRegistryCapacityError,
    DeviceRegistryConflictError,
    DeviceRegistryStorageError,
)
from k5vision.user_admin_api import install_user_admin_api

CONTROL_PLANE_TOKEN_ENV = "K5_CONTROL_PLANE_TOKEN"
CONTROL_PLANE_READ_TOKEN_ENV = "K5_CONTROL_PLANE_READ_TOKEN"
CONTROL_PLANE_SITE_ENV = "K5_CONTROL_PLANE_SITE_ID"
DEVICE_DB_PATH_ENV = "K5_DEVICE_DB_PATH"
MAX_DEVICE_REQUEST_BYTES = 16_384
MAX_CONTROL_PLANE_BEARER_LENGTH = 512
DEFAULT_DEVICE_READ_RATE_LIMIT = 240
DEFAULT_DEVICE_WRITE_RATE_LIMIT = 60
DEFAULT_DEVICE_RATE_WINDOW_SECONDS = 60.0
MAX_DEVICE_RATE_LIMIT = 10_000
MAX_DEVICE_RATE_WINDOW_SECONDS = 3_600.0


class _DeviceAccess(StrEnum):
    READ = "read"
    WRITE = "write"


class _DeviceRequestClass(StrEnum):
    READ = "read"
    WRITE = "write"


class _RequestBodyTooLarge(Exception):
    pass


class _BoundedDeviceRequestBody:
    """Reject oversized device/operator mutation bodies before application parsing."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or scope.get("path")
            not in {"/api/v1/devices", "/api/v1/operator/live", "/api/v1/operator/recordings"}
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


class _DeviceRequestRateLimiter:
    """Bound authenticated device requests without retaining credentials or client addresses."""

    def __init__(
        self,
        *,
        read_limit: int,
        write_limit: int,
        window_seconds: float,
    ) -> None:
        self._limits = {
            _DeviceRequestClass.READ: read_limit,
            _DeviceRequestClass.WRITE: write_limit,
        }
        self._window_seconds = window_seconds
        self._events: dict[tuple[_DeviceRequestClass, str], deque[float]] = {}
        self._lock = asyncio.Lock()

    async def consume(
        self,
        request_class: _DeviceRequestClass,
        principal_key: str,
    ) -> int | None:
        """Return Retry-After seconds when one principal's bounded budget is exhausted."""
        now = monotonic()
        cutoff = now - self._window_seconds
        key = (request_class, principal_key)
        async with self._lock:
            events = self._events.setdefault(key, deque())
            while events and events[0] <= cutoff:
                events.popleft()
            limit = self._limits[request_class]
            if len(events) >= limit:
                return max(1, ceil(self._window_seconds - (now - events[0])))
            events.append(now)
        return None


def _resolve_bearer_token(
    explicit_token: str | None,
    *,
    environment_name: str,
) -> tuple[str | None, bool]:
    raw_token = explicit_token if explicit_token is not None else environ.get(environment_name)
    if raw_token is None:
        return None, False
    token = raw_token.strip()
    valid = bool(token and token.isascii() and len(token) <= MAX_CONTROL_PLANE_BEARER_LENGTH)
    return (token if valid else None), True


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
    control_plane_read_token: str | None = None,
    control_plane_site_id: str | None = None,
    device_db_path: str | Path | None = None,
    device_capacity: int = DEFAULT_DEVICE_CAPACITY,
    max_device_request_bytes: int = MAX_DEVICE_REQUEST_BYTES,
    device_read_rate_limit: int = DEFAULT_DEVICE_READ_RATE_LIMIT,
    device_write_rate_limit: int = DEFAULT_DEVICE_WRITE_RATE_LIMIT,
    device_rate_window_seconds: float = DEFAULT_DEVICE_RATE_WINDOW_SECONDS,
    operator_source_resolver: OperatorSourceResolver | None = None,
    operator_launcher: OperatorLauncher | None = None,
    operator_recording_root: str | Path | None = None,
) -> FastAPI:
    """Build the control plane with fail-closed authenticated durable device state."""
    if not 1 <= device_capacity <= MAX_DEVICE_CAPACITY:
        raise ValueError(f"device_capacity must be between 1 and {MAX_DEVICE_CAPACITY}")
    if not 1024 <= max_device_request_bytes <= 1_048_576:
        raise ValueError("max_device_request_bytes must be between 1024 and 1048576")
    if not 1 <= device_read_rate_limit <= MAX_DEVICE_RATE_LIMIT:
        raise ValueError(f"device_read_rate_limit must be between 1 and {MAX_DEVICE_RATE_LIMIT}")
    if not 1 <= device_write_rate_limit <= MAX_DEVICE_RATE_LIMIT:
        raise ValueError(f"device_write_rate_limit must be between 1 and {MAX_DEVICE_RATE_LIMIT}")
    if not 1.0 <= device_rate_window_seconds <= MAX_DEVICE_RATE_WINDOW_SECONDS:
        raise ValueError(
            f"device_rate_window_seconds must be between 1.0 and {MAX_DEVICE_RATE_WINDOW_SECONDS}"
        )

    write_token, write_token_configured = _resolve_bearer_token(
        control_plane_token,
        environment_name=CONTROL_PLANE_TOKEN_ENV,
    )
    read_token, read_token_configured = _resolve_bearer_token(
        control_plane_read_token,
        environment_name=CONTROL_PLANE_READ_TOKEN_ENV,
    )
    auth_configuration_valid = write_token_configured and write_token is not None
    if read_token_configured:
        auth_configuration_valid = auth_configuration_valid and read_token is not None
    if (
        write_token is not None
        and read_token is not None
        and compare_digest(write_token, read_token)
    ):
        auth_configuration_valid = False

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

    request_limiter = _DeviceRequestRateLimiter(
        read_limit=device_read_rate_limit,
        write_limit=device_write_rate_limit,
        window_seconds=device_rate_window_seconds,
    )
    user_registry = None

    @asynccontextmanager
    async def lifespan(_application: FastAPI):
        try:
            yield
        finally:
            if user_registry is not None:
                user_registry.close()
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
    user_registry = install_user_admin_api(
        application,
        site_id=site_id,
        reserved_tokens=tuple(token for token in (write_token, read_token) if token is not None),
    )
    session_manager = application.state.user_session_manager
    install_operator_launch_api(
        application,
        registry=registry,
        session_manager=session_manager,
        source_resolver=operator_source_resolver,
        launcher=operator_launcher,
    )
    install_operator_recording_api(
        application,
        registry=registry,
        session_manager=session_manager,
        source_resolver=operator_source_resolver,
        recording_root=operator_recording_root,
    )

    async def authenticate_control_plane(
        authorization: Annotated[list[str] | None, Header()] = None,
    ) -> tuple[_DeviceAccess, str]:
        human_auth_available = user_registry is not None
        if not auth_configuration_valid and not human_auth_available:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Control-plane authentication is not configured",
            )

        header = authorization[0] if authorization and len(authorization) == 1 else ""
        scheme, separator, credential = header.partition(" ")
        credential_valid = bool(
            scheme.lower() == "bearer"
            and separator
            and credential
            and credential.isascii()
            and len(credential) <= MAX_CONTROL_PLANE_BEARER_LENGTH
        )
        if not credential_valid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized",
                headers={"WWW-Authenticate": "Bearer"},
            )

        access: _DeviceAccess | None = None
        principal_key: str | None = None
        if auth_configuration_valid and write_token is not None:
            write_match = compare_digest(credential, write_token)
            read_match = bool(read_token is not None and compare_digest(credential, read_token))
            if write_match:
                access = _DeviceAccess.WRITE
                principal_key = "service:write"
            elif read_match:
                access = _DeviceAccess.READ
                principal_key = "service:read"

        if access is None and human_auth_available:
            principal = await session_manager.resolve(credential)
            if principal is not None:
                access = (
                    _DeviceAccess.READ if principal.role is UserRole.VIEWER else _DeviceAccess.WRITE
                )
                principal_key = f"user:{principal.id}"

        if access is None or principal_key is None:
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
        return access, principal_key

    async def enforce_device_rate_limit(
        request_class: _DeviceRequestClass,
        principal_key: str,
    ) -> None:
        retry_after = await request_limiter.consume(request_class, principal_key)
        if retry_after is not None:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Device request rate limit exceeded",
                headers={"Retry-After": str(retry_after)},
            )

    async def require_device_read(
        principal: Annotated[tuple[_DeviceAccess, str], Depends(authenticate_control_plane)],
    ) -> None:
        _, principal_key = principal
        await enforce_device_rate_limit(_DeviceRequestClass.READ, principal_key)

    async def require_device_write(
        principal: Annotated[tuple[_DeviceAccess, str], Depends(authenticate_control_plane)],
    ) -> None:
        access, principal_key = principal
        if access is not _DeviceAccess.WRITE:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient device permission",
            )
        await enforce_device_rate_limit(_DeviceRequestClass.WRITE, principal_key)

    def require_registry() -> DeviceRegistry:
        if registry is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Device registry unavailable",
            )
        return registry

    device_read_auth = [Depends(require_device_read)]
    device_write_auth = [Depends(require_device_write)]

    @application.get("/api/v1/health", tags=["system"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @application.get(
        "/api/v1/devices",
        response_model=list[Device],
        tags=["devices"],
        dependencies=device_read_auth,
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
        dependencies=device_write_auth,
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
        dependencies=device_read_auth,
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
