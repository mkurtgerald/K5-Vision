from k5vision.domain.devices import DeviceCreate, DeviceProtocol
from k5vision.services.device_registry import DeviceRegistry


def test_registry_defaults_to_onvif() -> None:
    registry = DeviceRegistry()
    request = DeviceCreate(name="Camera 1", host="10.0.0.10")

    device = registry.register(request)

    assert DeviceProtocol.ONVIF in device.protocols
    assert registry.get(device.id) == device
