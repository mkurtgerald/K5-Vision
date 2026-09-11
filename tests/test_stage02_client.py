import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from k5vision.adapters.base import DeviceCredentials, DeviceProbeError, ProbeErrorCode
from k5vision.adapters.stage02_client import Stage02Adapter, _map_exception
from k5vision.domain.devices import Device, DeviceProtocol
from k5vision.domain.streams import StreamProfile, StreamRole


class FakeManagement:
    def __init__(self, *, fail_capabilities: bool = False) -> None:
        self.fail_capabilities = fail_capabilities

    def GetDeviceInformation(self):
        return {
            "Manufacturer": "Example",
            "Model": "X1",
            "FirmwareVersion": "1.2.3",
            "SerialNumber": "ABC123",
            "HardwareId": "HW-1",
        }

    def GetCapabilities(self, **kwargs):
        assert kwargs == {"Category": "All"}
        if self.fail_capabilities:
            raise RuntimeError("optional capability query unsupported")
        return {
            "PTZ": {"XAddr": "http://example/ptz"},
            "Events": {"XAddr": "http://example/events"},
            "Device": {"IO": {"InputConnectors": 1}},
        }


class FakeMedia:
    def __init__(self, *, stream_error: BaseException | None = None, no_profiles: bool = False):
        self.stream_error = stream_error
        self.no_profiles = no_profiles

    def GetProfiles(self):
        if self.no_profiles:
            return []
        return [
            SimpleNamespace(
                token="sub",
                Name="Sub",
                VideoEncoderConfiguration=SimpleNamespace(
                    Encoding="H264",
                    Resolution=SimpleNamespace(Width=640, Height=360),
                    RateControl=SimpleNamespace(FrameRateLimit=10, BitrateLimit=512),
                ),
                AudioEncoderConfiguration=None,
            ),
            SimpleNamespace(
                token="main",
                Name="Main",
                VideoEncoderConfiguration=SimpleNamespace(
                    Encoding="H265",
                    Resolution=SimpleNamespace(Width=3840, Height=2160),
                    RateControl=SimpleNamespace(FrameRateLimit=30, BitrateLimit=8192),
                ),
                AudioEncoderConfiguration=SimpleNamespace(token="audio"),
            ),
        ]

    def GetStreamUri(self, **kwargs):
        if self.stream_error is not None:
            raise self.stream_error
        token = kwargs["ProfileToken"]
        return {"Uri": f"rtsp://user:secret@10.0.0.9:8554/{token}?transport=tcp"}


class FakeClient:
    def __init__(
        self,
        *,
        stream_error: BaseException | None = None,
        no_profiles: bool = False,
        fail_capabilities: bool = False,
    ) -> None:
        self.management = FakeManagement(fail_capabilities=fail_capabilities)
        self.media_service = FakeMedia(stream_error=stream_error, no_profiles=no_profiles)

    def devicemgmt(self):
        return self.management

    def media(self):
        return self.media_service


def test_discovery_normalizes_results_and_skips_empty_hosts() -> None:
    captured = {}

    def discovery_factory(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            discover=lambda: [
                {
                    "host": "10.0.0.9",
                    "port": 8899,
                    "xaddrs": ["https://10.0.0.9:8899/service"],
                },
                {"host": "", "port": 80, "xaddrs": []},
            ]
        )

    adapter = Stage02Adapter(
        timeout_seconds=2,
        interface="10.0.0.5",
        discovery_factory=discovery_factory,
    )
    results = asyncio.run(adapter.discover())

    assert captured == {"timeout": 2, "interface": "10.0.0.5"}
    assert len(results) == 1
    assert results[0].host == "10.0.0.9"
    assert results[0].management_port == 8899
    assert results[0].secure is True


def test_probe_returns_canonical_capabilities_and_strips_uri_credentials() -> None:
    captured = {}

    def client_factory(**kwargs):
        captured.update(kwargs)
        return FakeClient()

    adapter = Stage02Adapter(timeout_seconds=3, verify_ssl=True, client_factory=client_factory)
    device = Device(
        name="endpoint",
        host="10.0.0.9",
        management_port=8899,
        protocols={DeviceProtocol.ONVIF, DeviceProtocol.HTTPS},
    )
    credentials = DeviceCredentials(username="admin", password="secret")

    result = asyncio.run(adapter.probe(device, credentials))

    assert captured["host"] == "10.0.0.9"
    assert captured["port"] == 8899
    assert captured["use_https"] is True
    assert captured["verify_ssl"] is True
    assert captured["capture_xml"] is False
    assert result.identity.manufacturer == "Example"
    assert result.supports_ptz is True
    assert result.supports_events is True
    assert result.supports_audio is True
    assert result.supports_digital_io is True
    assert result.stream_profiles[0].role is StreamRole.MAIN
    assert result.stream_profiles[0].connection_uri == (
        "rtsp://10.0.0.9:8554/main?transport=tcp"
    )
    assert "secret" not in result.model_dump_json()


def test_optional_capability_failure_does_not_hide_core_probe_success() -> None:
    adapter = Stage02Adapter(
        client_factory=lambda **kwargs: FakeClient(fail_capabilities=True),
    )
    device = Device(name="endpoint", host="10.0.0.9")

    result = asyncio.run(adapter.probe(device))

    assert result.supports_ptz is False
    assert result.supports_events is False
    assert result.supports_digital_io is False
    assert len(result.stream_profiles) == 2


def test_probe_rejects_endpoint_without_usable_profiles() -> None:
    adapter = Stage02Adapter(client_factory=lambda **kwargs: FakeClient(no_profiles=True))
    device = Device(name="endpoint", host="10.0.0.9")

    with pytest.raises(DeviceProbeError) as caught:
        asyncio.run(adapter.probe(device))

    assert caught.value.code is ProbeErrorCode.INVALID_RESPONSE


def test_probe_maps_stream_failure_when_no_connection_metadata_succeeds() -> None:
    adapter = Stage02Adapter(
        client_factory=lambda **kwargs: FakeClient(stream_error=RuntimeError("401 Unauthorized"))
    )
    device = Device(name="endpoint", host="10.0.0.9")

    with pytest.raises(DeviceProbeError) as caught:
        asyncio.run(adapter.probe(device))

    assert caught.value.code is ProbeErrorCode.AUTHENTICATION_FAILED


def test_discovery_failure_is_normalized() -> None:
    def discovery_factory(**kwargs):
        raise TimeoutError("timed out")

    adapter = Stage02Adapter(discovery_factory=discovery_factory)

    with pytest.raises(DeviceProbeError) as caught:
        asyncio.run(adapter.discover())

    assert caught.value.code is ProbeErrorCode.TIMEOUT


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (TimeoutError("timed out"), ProbeErrorCode.TIMEOUT),
        (RuntimeError("401 Unauthorized"), ProbeErrorCode.AUTHENTICATION_FAILED),
        (RuntimeError("connection refused"), ProbeErrorCode.UNREACHABLE),
        (RuntimeError("ActionNotSupported"), ProbeErrorCode.UNSUPPORTED),
        (RuntimeError("malformed payload"), ProbeErrorCode.INVALID_RESPONSE),
    ],
)
def test_exception_mapping_is_stable(error: BaseException, expected: ProbeErrorCode) -> None:
    assert _map_exception(error).code is expected


def test_stream_profile_rejects_embedded_credentials() -> None:
    with pytest.raises(ValidationError):
        StreamProfile(
            token="main",
            name="Main",
            role=StreamRole.MAIN,
            width=1920,
            height=1080,
            connection_uri="rtsp://admin:secret@10.0.0.9/live",
        )
