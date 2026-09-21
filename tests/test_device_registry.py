from pathlib import Path

import pytest

from k5vision.domain.devices import DeviceCreate, DeviceProtocol
from k5vision.services.device_registry import (
    DeviceRegistry,
    DeviceRegistryConflictError,
    DeviceRegistryStorageError,
)


def test_registry_defaults_to_onvif() -> None:
    registry = DeviceRegistry()
    request = DeviceCreate(name="Camera 1", host="10.0.0.10")

    device = registry.register(request)

    assert DeviceProtocol.ONVIF in device.protocols
    assert registry.get(device.id) == device
    registry.close()


def test_registry_persists_idempotent_identity_and_conflict_state(tmp_path: Path) -> None:
    database = tmp_path / "devices.sqlite3"
    request = DeviceCreate(name="Camera 1", host="10.0.0.10")

    first_registry = DeviceRegistry(database_path=database, site_id="site-a")
    first = first_registry.enroll(request)
    repeated = first_registry.enroll(request)
    first_registry.close()

    second_registry = DeviceRegistry(database_path=database, site_id="site-a")
    restored = second_registry.get(first.device.id)
    with pytest.raises(DeviceRegistryConflictError):
        second_registry.enroll(DeviceCreate(name="Renamed", host="10.0.0.10"))
    listed = second_registry.list()
    second_registry.close()

    assert first.created is True
    assert repeated.created is False
    assert repeated.device.id == first.device.id
    assert restored == first.device
    assert listed == [first.device]


def test_registry_scopes_identical_endpoint_to_site(tmp_path: Path) -> None:
    database = tmp_path / "devices.sqlite3"
    request = DeviceCreate(name="Camera 1", host="10.0.0.10")

    site_a = DeviceRegistry(database_path=database, site_id="site-a")
    site_a_device = site_a.register(request)
    site_a.close()

    site_b = DeviceRegistry(database_path=database, site_id="site-b")
    assert site_b.get(site_a_device.id) is None
    site_b_device = site_b.register(request)
    assert site_b_device.id != site_a_device.id
    site_b.close()


def test_registry_rejects_invalid_scope_and_storage_configuration(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        DeviceRegistry(site_id="site with spaces")
    with pytest.raises(ValueError):
        DeviceRegistry(database_path=" ")
    with pytest.raises(DeviceRegistryStorageError):
        DeviceRegistry(database_path=tmp_path / "missing" / "devices.sqlite3")


def test_closed_registry_fails_without_deleting_durable_state(tmp_path: Path) -> None:
    database = tmp_path / "devices.sqlite3"
    registry = DeviceRegistry(database_path=database, site_id="site-a")
    device = registry.register(DeviceCreate(name="Camera 1", host="10.0.0.10"))
    registry.close()

    with pytest.raises(DeviceRegistryStorageError):
        registry.get(device.id)

    reopened = DeviceRegistry(database_path=database, site_id="site-a")
    assert reopened.get(device.id) == device
    reopened.close()
