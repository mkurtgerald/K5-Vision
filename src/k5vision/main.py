"""K5 Vision control-plane API."""

from uuid import UUID

from fastapi import FastAPI, HTTPException, Request, status

from k5vision import __version__
from k5vision.domain.devices import Device, DeviceCreate
from k5vision.services.device_registry import DeviceRegistry


def create_app() -> FastAPI:
    """Build an isolated K5 Vision API application instance."""
    application = FastAPI(
        title="K5 Vision Control Plane",
        version=__version__,
        description="Vendor-neutral security sensor management control plane.",
    )
    application.state.device_registry = DeviceRegistry()

    @application.get("/api/v1/health", tags=["system"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @application.get("/api/v1/devices", response_model=list[Device], tags=["devices"])
    async def list_devices(request: Request) -> list[Device]:
        return request.app.state.device_registry.list()

    @application.post(
        "/api/v1/devices",
        response_model=Device,
        status_code=status.HTTP_201_CREATED,
        tags=["devices"],
    )
    async def register_device(payload: DeviceCreate, request: Request) -> Device:
        return request.app.state.device_registry.register(payload)

    @application.get("/api/v1/devices/{device_id}", response_model=Device, tags=["devices"])
    async def get_device(device_id: UUID, request: Request) -> Device:
        device = request.app.state.device_registry.get(device_id)
        if device is None:
            raise HTTPException(status_code=404, detail="Device not found")
        return device

    return application


app = create_app()
