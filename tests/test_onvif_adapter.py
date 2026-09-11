import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from k5vision.adapters.base import DeviceCredentials, DeviceProbeError, ProbeErrorCode
from k5vision.adapters.onvif import OnvifAdapter
from k5vision.domain.devices import Device
from k5vision.domain.streams import StreamRole, VideoCodec


def _device() -> Device:
    return Device(name="node-a", host="192.168.10.20")


def _profile(
    token: str,
    *,
    width: int,
    height: int,
    encoding: str = "H264",
    audio: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        token=token,
        Name=token,
        VideoEncoderConfiguration=SimpleNamespace(
            Encoding=encoding,
            Resolution=SimpleNamespace(Width=width, Height=height),
            RateControl=SimpleNamespace(FrameRateLimit=30, BitrateLimit=2048),
        ),
        AudioEncoderConfiguration=object() if audio else None,
    )


class _DeviceService:
    def __init__(self, *, capabilities: Any | None = None, capability_error: Exception | None = None) -> None:
        self._capabilities = capabilities
        self._capability_error = capability_error

    def GetDeviceInformation(self) -> dict[str, str]:
        return {
            "Manufacturer": "Example",
            "Model": "Model-A",
            "FirmwareVersion": "1.2.3",
            "SerialNumber": "SN-1",
            "HardwareId": "HW-1",
        }

    def GetCapabilities(self, **_: Any) -> Any:
        if self._capability_error is not None:
            raise self._capability_error
        return self._capabilities


class _MediaService:
    def __init__(self, profiles: list[Any]) -> None:
        self._profiles = profiles
        self.uri_tokens: list[str] = []

    def GetProfiles(self) -> list[Any]:
        return self._profiles

    def GetStreamUri(self, *, ProfileToken: str, StreamSetup: dict[str, Any]) -> dict[str, str]:
        assert StreamSetup["Transport"]["Protocol"] == "RTSP"
        self.uri_tokens.append(ProfileToken)
        return {"Uri": f"rtsp://user:secret@192.168.10.20/{ProfileToken}"}


class _Client:
    def __init__(self, device_service: Any, media_service: Any) -> None:
        self._device_service = device_service
        self._media_service = media_service

    def devicemgmt(self) -> Any:
        return self._device_service

    def media(self) -> Any:
        return self._media_service


def test_probe_normalizes_real_client_boundary_without_leaking_client_objects() -> None:
    capabilities = SimpleNamespace(
        PTZ=SimpleNamespace(XAddr="http://node/ptz"),
        Events=SimpleNamespace(XAddr="http://node/events"),
        Extension=SimpleNamespace(DeviceIO=object()),
    )
    media = _MediaService(
        [
            _profile("sub", width=640, height=360),
            _profile("main", width=1920, height=1080, encoding="H265", audio=True),
        ]
    )
    client = _Client(_DeviceService(capabilities=capabilities), media)
    captured: dict[str, Any] = {}

    def factory(**kwargs: Any) -> _Client:
        captured.update(kwargs)
        return client

    adapter = OnvifAdapter(port=8080, timeout_seconds=4.5, client_factory=factory)
    result = asyncio.run(
        adapter.probe(
            _device(),
            DeviceCredentials(username="operator", password="do-not-log"),
        )
    )

    assert captured == {
        "host": "192.168.10.20",
        "port": 8080,
        "username": "operator",
        "password": "do-not-log",
        "timeout": 4.5,
        "use_https": False,
    }
    assert result.identity.manufacturer == "Example"
    assert result.identity.hardware_id == "HW-1"
    assert result.supports_ptz is True
    assert result.supports_events is True
    assert result.supports_audio is True
    assert result.supports_digital_io is True
    assert media.uri_tokens == ["sub", "main"]
    assert result.stream_profiles[0].token == "main"
    assert result.stream_profiles[0].role is StreamRole.MAIN
    assert result.stream_profiles[0].codec is VideoCodec.H265
    assert result.stream_profiles[1].role is StreamRole.SUBSTREAM
    assert "secret" not in result.model_dump_json()


def test_probe_allows_missing_optional_capabilities() -> None:
    media = _MediaService([_profile("only", width=1280, height=720)])
    client = _Client(
        _DeviceService(capability_error=RuntimeError("ActionNotSupported")),
        media,
    )
    adapter = OnvifAdapter(client_factory=lambda **_: client)

    result = asyncio.run(adapter.probe(_device()))

    assert result.supports_ptz is False
    assert result.supports_events is False
    assert result.supports_digital_io is False


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (TimeoutError("timed out"), ProbeErrorCode.TIMEOUT),
        (ConnectionError("connection refused"), ProbeErrorCode.UNREACHABLE),
        (RuntimeError("NotAuthorized"), ProbeErrorCode.AUTHENTICATION_FAILED),
        (RuntimeError("operation not supported"), ProbeErrorCode.UNSUPPORTED),
        (RuntimeError("bad payload"), ProbeErrorCode.INVALID_RESPONSE),
    ],
)
def test_probe_maps_failures_to_stable_error_codes(raised: Exception, expected: ProbeErrorCode) -> None:
    def factory(**_: Any) -> Any:
        raise raised

    adapter = OnvifAdapter(client_factory=factory)

    with pytest.raises(DeviceProbeError) as caught:
        asyncio.run(adapter.probe(_device()))

    assert caught.value.code is expected
    assert "secret" not in str(caught.value).lower()


def test_probe_rejects_malformed_profile_as_invalid_response() -> None:
    malformed = SimpleNamespace(
        token="bad",
        Name="bad",
        VideoEncoderConfiguration=SimpleNamespace(Encoding="H264", Resolution=None),
    )
    client = _Client(_DeviceService(), _MediaService([malformed]))
    adapter = OnvifAdapter(client_factory=lambda **_: client)

    with pytest.raises(DeviceProbeError) as caught:
        asyncio.run(adapter.probe(_device()))

    assert caught.value.code is ProbeErrorCode.INVALID_RESPONSE


def test_probe_rejects_missing_stream_metadata_as_invalid_response() -> None:
    class MissingUriMedia(_MediaService):
        def GetStreamUri(self, *, ProfileToken: str, StreamSetup: dict[str, Any]) -> dict[str, str]:
            return {}

    client = _Client(
        _DeviceService(),
        MissingUriMedia([_profile("only", width=1280, height=720)]),
    )
    adapter = OnvifAdapter(client_factory=lambda **_: client)

    with pytest.raises(DeviceProbeError) as caught:
        asyncio.run(adapter.probe(_device()))

    assert caught.value.code is ProbeErrorCode.INVALID_RESPONSE


def test_adapter_rejects_invalid_configuration() -> None:
    with pytest.raises(ValueError, match="port"):
        OnvifAdapter(port=0)
    with pytest.raises(ValueError, match="timeout"):
        OnvifAdapter(timeout_seconds=0)
