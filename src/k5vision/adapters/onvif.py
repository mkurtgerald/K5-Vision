"""Concrete ONVIF probe implementation behind project-owned contracts."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from k5vision.adapters.base import (
    DeviceCredentials,
    DeviceProbeError,
    ProbeErrorCode,
)
from k5vision.adapters.onvif_contracts import (
    OnvifDeviceSnapshot,
    OnvifProfileSnapshot,
    normalize_onvif_snapshot,
)
from k5vision.domain.capabilities import DeviceCapabilities
from k5vision.domain.devices import Device

ClientFactory = Callable[..., Any]


class OnvifAdapter:
    """Probe one registered endpoint without leaking client-library objects."""

    def __init__(
        self,
        *,
        port: int = 80,
        timeout_seconds: float = 10.0,
        use_https: bool = False,
        client_factory: ClientFactory | None = None,
    ) -> None:
        if not 1 <= port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        self._port = port
        self._timeout_seconds = timeout_seconds
        self._use_https = use_https
        self._client_factory = client_factory or _default_client_factory

    async def probe(
        self,
        device: Device,
        credentials: DeviceCredentials | None = None,
    ) -> DeviceCapabilities:
        """Run blocking SOAP work off the event loop and normalize failures."""
        try:
            return await asyncio.to_thread(self._probe_sync, device, credentials)
        except DeviceProbeError:
            raise
        except Exception as exc:
            raise _probe_error(exc) from exc

    def _probe_sync(
        self,
        device: Device,
        credentials: DeviceCredentials | None,
    ) -> DeviceCapabilities:
        username = credentials.username if credentials else None
        password = credentials.password.get_secret_value() if credentials else None

        client = self._client_factory(
            host=str(device.host),
            port=self._port,
            username=username,
            password=password,
            timeout=self._timeout_seconds,
            use_https=self._use_https,
        )

        device_service = client.devicemgmt()
        media_service = client.media()

        info = device_service.GetDeviceInformation()
        raw_profiles = list(media_service.GetProfiles() or [])
        capabilities = _optional_capabilities(device_service)

        snapshots = [_profile_snapshot(profile) for profile in raw_profiles]
        for profile in snapshots:
            _validate_stream_uri(media_service, profile.token)

        supports_audio = any(
            _value(profile, "AudioEncoderConfiguration") is not None
            for profile in raw_profiles
        )
        snapshot = OnvifDeviceSnapshot(
            manufacturer=_text(info, "Manufacturer"),
            model=_text(info, "Model"),
            firmware=_text(info, "FirmwareVersion"),
            serial_number=_text(info, "SerialNumber"),
            hardware_id=_text(info, "HardwareId"),
            profiles=snapshots,
            supports_ptz=_has_service(capabilities, "PTZ"),
            supports_events=_has_service(capabilities, "Events"),
            supports_audio=supports_audio,
            supports_digital_io=_has_device_io(capabilities),
        )
        return normalize_onvif_snapshot(snapshot)


def _default_client_factory(**kwargs: Any) -> Any:
    from onvif import CacheMode, ONVIFClient

    return ONVIFClient(cache=CacheMode.MEM, **kwargs)


def _optional_capabilities(device_service: Any) -> Any | None:
    try:
        return device_service.GetCapabilities(Category="All")
    except Exception as exc:
        if _is_unsupported(exc):
            return None
        raise


def _profile_snapshot(profile: Any) -> OnvifProfileSnapshot:
    token = _text(profile, "token") or _text(profile, "_token") or _text(profile, "Token")
    encoder = _value(profile, "VideoEncoderConfiguration")
    resolution = _value(encoder, "Resolution")
    rate_control = _value(encoder, "RateControl")

    if not token or encoder is None or resolution is None:
        raise ValueError("profile response is missing required fields")

    width = _positive_int(_value(resolution, "Width"), "profile width")
    height = _positive_int(_value(resolution, "Height"), "profile height")
    fps = _optional_positive_float(_value(rate_control, "FrameRateLimit"))
    bitrate = _optional_positive_int(_value(rate_control, "BitrateLimit"))

    return OnvifProfileSnapshot(
        token=token,
        name=_text(profile, "Name") or token,
        encoding=_text(encoder, "Encoding") or "unknown",
        width=width,
        height=height,
        fps=fps,
        bitrate_kbps=bitrate,
    )


def _validate_stream_uri(media_service: Any, profile_token: str) -> None:
    response = media_service.GetStreamUri(
        ProfileToken=profile_token,
        StreamSetup={"Stream": "RTP-Unicast", "Transport": {"Protocol": "RTSP"}},
    )
    uri = _text(response, "Uri")
    if not uri:
        raise ValueError("stream metadata response is missing a URI")


def _value(obj: Any, name: str) -> Any | None:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _text(obj: Any, name: str) -> str | None:
    value = _value(obj, name)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _positive_int(value: Any, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} is invalid") from exc
    if parsed <= 0:
        raise ValueError(f"{field} must be positive")
    return parsed


def _optional_positive_int(value: Any) -> int | None:
    if value is None:
        return None
    parsed = int(value)
    return parsed if parsed > 0 else None


def _optional_positive_float(value: Any) -> float | None:
    if value is None:
        return None
    parsed = float(value)
    return parsed if parsed > 0 else None


def _has_service(capabilities: Any, name: str) -> bool:
    service = _value(capabilities, name)
    return bool(_text(service, "XAddr"))


def _has_device_io(capabilities: Any) -> bool:
    extension = _value(capabilities, "Extension")
    return bool(_value(extension, "DeviceIO"))


def _probe_error(exc: Exception) -> DeviceProbeError:
    message = str(exc).lower()
    class_name = type(exc).__name__.lower()

    if isinstance(exc, TimeoutError) or "timeout" in class_name or "timed out" in message:
        return DeviceProbeError(ProbeErrorCode.TIMEOUT, "endpoint probe timed out")

    unreachable_markers = (
        "connection refused",
        "no route to host",
        "network is unreachable",
        "name or service not known",
    )
    if isinstance(exc, ConnectionError) or any(
        marker in message for marker in unreachable_markers
    ):
        return DeviceProbeError(ProbeErrorCode.UNREACHABLE, "endpoint is unreachable")

    auth_markers = (
        "notauthorized",
        "not authorized",
        "unauthorized",
        "authentication",
        "http 401",
        "http 403",
    )
    if any(marker in message for marker in auth_markers):
        return DeviceProbeError(
            ProbeErrorCode.AUTHENTICATION_FAILED,
            "endpoint authentication failed",
        )

    if _is_unsupported(exc):
        return DeviceProbeError(
            ProbeErrorCode.UNSUPPORTED,
            "endpoint operation is unsupported",
        )

    return DeviceProbeError(
        ProbeErrorCode.INVALID_RESPONSE,
        "endpoint returned an invalid response",
    )


def _is_unsupported(exc: Exception) -> bool:
    message = str(exc).lower()
    return "actionnotsupported" in message or "not supported" in message
