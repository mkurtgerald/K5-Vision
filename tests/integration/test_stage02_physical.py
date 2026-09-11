import asyncio
import os

import pytest

from k5vision.adapters.base import DeviceCredentials
from k5vision.adapters.stage02_client import Stage02Adapter
from k5vision.domain.devices import Device, DeviceProtocol


def test_stage02_physical_endpoint() -> None:
    host = os.getenv("K5_STAGE02_HOST")
    if not host:
        pytest.skip("K5_STAGE02_HOST is not configured")

    port = int(os.getenv("K5_STAGE02_PORT", "80"))
    secure = os.getenv("K5_STAGE02_HTTPS", "0") == "1"
    username = os.getenv("K5_STAGE02_USER", "")
    password = os.getenv("K5_STAGE02_PASSWORD", "")
    protocols = {DeviceProtocol.ONVIF}
    if secure:
        protocols.add(DeviceProtocol.HTTPS)

    credentials = None
    if username:
        credentials = DeviceCredentials(username=username, password=password)

    device = Device(
        name="physical-endpoint",
        host=host,
        management_port=port,
        protocols=protocols,
    )
    adapter = Stage02Adapter(timeout_seconds=10)

    result = asyncio.run(adapter.probe(device, credentials))

    assert result.stream_profiles
    assert any(profile.connection_uri for profile in result.stream_profiles)
    assert result.identity.model or result.identity.manufacturer
