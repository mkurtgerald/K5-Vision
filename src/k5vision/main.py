"""K5 Vision control-plane API."""

from hmac import compare_digest
from os import environ
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from k5vision import __version__
from k5vision.domain.devices import Device, DeviceCreate
from k5vision.services.device_registry import (
    DEFAULT_DEVICE_CAPACITY,
    MAX_DEVICE_CAPACITY,
    MAX_DEVICE_PAGE_SIZE,
    DeviceRegistry,
    DeviceRegistryCapacityError,
)

CONTROL_PLANE_TOKEN_ENV = "K5_CONTROL_PLANE_TOKEN"
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
        body = (f'{{"detail":"{detail}"}}').encode("utf-8")
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


def create_app(
    *,
    control_plane_token: str | None = None,
    device_capacity: int = DEFAULT_DEVICE_CAPACITY,
    max_device_request_bytes: int = MAX_DEVICE_REQUEST_BYTES,
) -> FastAPI:
    """Build an isolated API requiring one ASCII bearer credential for device operations.

    Missing or non-ASCII token configuration leaves device operations unavailable.
    """
    if not 1 <= device_capacity <= MAX_DEVICE_CAPACITY:
        raise ValueError(f"device_capacity must be between 1 and {MAX_DEVICE_CAPACITY}")
    if not 1024 <= max_device_request_bytes <= 1_048_576:
        raise ValueError("max_device_request_bytes must be between 1024 and 1048576")

    application = FastAPI(
        title="K5 Vision Control Plane",
        version=__version__,
        description="Vendor-neutral security sensor management control plane.",
    )
    application.add_middleware(
        _BoundedDeviceRequestBody,
        max_bytes=max_device_request_bytes,
    )
    application.state.device_registry = DeviceRegistry(capacity=device_capacity)
    expected_token = _resolve_control_plane_token(control_plane_token)

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
        request: Request,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_DEVICE_PAGE_SIZE)] = MAX_DEVICE_PAGE_SIZE,
    ) -> list[Device]:
        return request.app.state.device_registry.list(offset=offset, limit=limit)

    @application.post(
        "/api/v1/devices",
        response_model=Device,
        status_code=status.HTTP_201_CREATED,
        tags=["devices"],
        dependencies=device_auth,
    )
    async def register_device(payload: DeviceCreate, request: Request) -> Device:
        try:
            return request.app.state.device_registry.register(payload)
        except DeviceRegistryCapacityError:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Device registry capacity reached",
            ) from None

    @application.get(
        "/api/v1/devices/{device_id}",
        response_model=Device,
        tags=["devices"],
        dependencies=device_auth,
    )
    async def get_device(device_id: UUID, request: Request) -> Device:
        device = request.app.state.device_registry.get(device_id)
        if device is None:
            raise HTTPException(status_code=404, detail="Device not found")
        return device

    return application


app = create_app()
