"""Device registration service.

The in-memory implementation is intentionally replaceable. Persistence will be
introduced behind this interface rather than leaking database concerns into API
routes or device adapters.
"""

from uuid import UUID

from k5vision.domain.devices import Device, DeviceCreate

DEFAULT_DEVICE_CAPACITY = 1024
MAX_DEVICE_CAPACITY = 100_000
MAX_DEVICE_PAGE_SIZE = 100


class DeviceRegistryCapacityError(RuntimeError):
    """Raised when a bounded registry cannot accept another device."""


class DeviceRegistry:
    """Register and retrieve canonical devices within explicit resource bounds."""

    def __init__(self, *, capacity: int = DEFAULT_DEVICE_CAPACITY) -> None:
        if not 1 <= capacity <= MAX_DEVICE_CAPACITY:
            raise ValueError(f"capacity must be between 1 and {MAX_DEVICE_CAPACITY}")
        self._capacity = capacity
        self._devices: dict[UUID, Device] = {}

    @property
    def capacity(self) -> int:
        return self._capacity

    def register(self, request: DeviceCreate) -> Device:
        """Create and store a canonical device record when capacity is available."""
        if len(self._devices) >= self._capacity:
            raise DeviceRegistryCapacityError("device registry capacity reached")
        device = Device(**request.model_dump())
        self._devices[device.id] = device
        return device

    def list(self, *, offset: int = 0, limit: int = MAX_DEVICE_PAGE_SIZE) -> list[Device]:
        """Return one bounded page of registered devices in insertion order."""
        if offset < 0:
            raise ValueError("offset must be non-negative")
        if not 1 <= limit <= MAX_DEVICE_PAGE_SIZE:
            raise ValueError(f"limit must be between 1 and {MAX_DEVICE_PAGE_SIZE}")
        devices = list(self._devices.values())
        return devices[offset : offset + limit]

    def get(self, device_id: UUID) -> Device | None:
        """Return one device when present."""
        return self._devices.get(device_id)
