"""K5 Vision control-plane API."""

from hmac import compare_digest
from os import environ
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status

from k5vision import __version__
from k5vision.domain.devices import Device, DeviceCreate
from k5vision.services.device_registry import DeviceRegistry

CONTROL_PLANE_TOKEN_ENV = "K5_CONTROL_PLANE_TOKEN"


def _resolve_control_plane_token(explicit_token: str | None) -> str | None:
    token = explicit_token if explicit_token is not None else environ.get(CONTROL_PLANE_TOKEN_ENV)
    if token is None:
        return None
    token = token.strip()
    return token or None


def create_app(*, control_plane_token: str | None = None) -> FastAPI:
    """Build an isolated K5 Vision API application instance."""
    application = FastAPI(
        title="K5 Vision Control Plane",
        version=__version__,
        description="Vendor-neutral security sensor management control plane.",
    )
    application.state.device_registry = DeviceRegistry()
    expected_token = _resolve_control_plane_token(control_plane_token)

    async def require_control_plane_auth(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        if expected_token is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Control-plane authentication is not configured",
            )

        scheme, separator, credential = (authorization or "").partition(" ")
        if (
            scheme.lower() != "bearer"
            or not separator
            or not credential
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
    async def list_devices(request: Request) -> list[Device]:
        return request.app.state.device_registry.list()

    @application.post(
        "/api/v1/devices",
        response_model=Device,
        status_code=status.HTTP_201_CREATED,
        tags=["devices"],
        dependencies=device_auth,
    )
    async def register_device(payload: DeviceCreate, request: Request) -> Device:
        return request.app.state.device_registry.register(payload)

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
