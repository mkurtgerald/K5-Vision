"""Device registration service.

The in-memory implementation is intentionally replaceable. Persistence will be
introduced behind this interface rather than leaking database concerns into API
routes or device adapters.
"""

from uuid import UUID

from k5vision.domain.devices import Device, DeviceCreate


class DeviceRegistry:
    """Register and retrieve canonical devices."""

    def __init__(self) -> None:
        self._devices: dict[UUID, Device] = {}

    def register(self, request: DeviceCreate) -> Device:
        """Create and store a canonical device record."""
        device = Device(**request.model_dump())
        self._devices[device.id] = device
        return device

    def list(self) -> list[Device]:
        """Return registered devices in insertion order."""
        return list(self._devices.values())

    def get(self, device_id: UUID) -> Device | None:
        """Return one device when present."""
        return self._devices.get(device_id)
