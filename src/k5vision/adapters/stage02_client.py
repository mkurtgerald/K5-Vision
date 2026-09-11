"""Stage 02 network integration behind project-owned contracts."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, Field

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
from k5vision.domain.devices import Device, DeviceProtocol

ClientFactory = Callable[..., Any]
DiscoveryFactory = Callable[..., Any]


class DiscoveredEndpoint(BaseModel):
    """Minimal discovery result retained by K5."""

    host: str = Field(min_length=1)
    management_port: int = Field(default=80, ge=1, le=65535)
    secure: bool = False
    xaddrs: list[str] = Field(default_factory=list)


def _default_client_factory(**kwargs: Any) -> Any:
    from onvif import ONVIFClient

    return ONVIFClient(**kwargs)


def _default_discovery_factory(**kwargs: Any) -> Any:
    from onvif import ONVIFDiscovery

    return ONVIFDiscovery(**kwargs)


def _get(value: Any, *names: str) -> Any:
    if value is None:
        return None
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    return None


def _positive_number(value: Any, cast: Callable[[Any], Any]) -> Any:
    if value is None:
        return None
    try:
        converted = cast(value)
    except (TypeError, ValueError):
        return None
    return converted if converted > 0 else None


def _sanitize_uri(value: Any) -> str | None:
    if not value:
        return None
    try:
        parsed = urlsplit(str(value))
        if not parsed.scheme or parsed.hostname is None:
            return None
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        return urlunsplit(
            (parsed.scheme, f"{host}{port}", parsed.path, parsed.query, parsed.fragment)
        )
    except ValueError:
        return None


def _map_exception(exc: Exception) -> DeviceProbeError:
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts.append(f"{type(current).__name__} {current}".lower())
        current = current.__cause__ or current.__context__
    text = " ".join(parts)

    if isinstance(exc, TimeoutError) or "timed out" in text or "timeout" in text:
        code = ProbeErrorCode.TIMEOUT
    elif any(term in text for term in ("unauthorized", "notauthorized", "authentication", "401")):
        code = ProbeErrorCode.AUTHENTICATION_FAILED
    elif any(
        term in text
        for term in (
            "connection refused",
            "connection error",
            "max retries exceeded",
            "network is unreachable",
            "no route to host",
            "name or service not known",
        )
    ):
        code = ProbeErrorCode.UNREACHABLE
    elif "not supported" in text or "actionnotsupported" in text:
        code = ProbeErrorCode.UNSUPPORTED
    else:
        code = ProbeErrorCode.INVALID_RESPONSE

    return DeviceProbeError(code, f"Stage 02 probe failed: {code.value}")


class Stage02Adapter:
    """Discovery/probe implementation isolated from the rest of the platform."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 8.0,
        interface: str | None = None,
        verify_ssl: bool = False,
        client_factory: ClientFactory | None = None,
        discovery_factory: DiscoveryFactory | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.interface = interface
        self.verify_ssl = verify_ssl
        self._client_factory = client_factory or _default_client_factory
        self._discovery_factory = discovery_factory or _default_discovery_factory

    async def discover(self) -> list[DiscoveredEndpoint]:
        """Discover compatible endpoints without leaking third-party result objects."""
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._discover_sync), timeout=self.timeout_seconds + 1
            )
        except DeviceProbeError:
            raise
        except Exception as exc:
            raise _map_exception(exc) from exc

    def _discover_sync(self) -> list[DiscoveredEndpoint]:
        kwargs: dict[str, Any] = {"timeout": self.timeout_seconds}
        if self.interface:
            kwargs["interface"] = self.interface
        raw = self._discovery_factory(**kwargs).discover()
        results: list[DiscoveredEndpoint] = []
        for item in raw or []:
            host = str(_get(item, "host") or "").strip()
            if not host:
                continue
            port = _positive_number(_get(item, "port"), int) or 80
            xaddrs = [str(value) for value in (_get(item, "xaddrs") or [])]
            secure = any(address.lower().startswith("https://") for address in xaddrs)
            results.append(
                DiscoveredEndpoint(
                    host=host,
                    management_port=port,
                    secure=secure,
                    xaddrs=xaddrs,
                )
            )
        return results

    async def probe(
        self,
        device: Device,
        credentials: DeviceCredentials | None = None,
    ) -> DeviceCapabilities:
        """Probe one endpoint and return canonical capabilities."""
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._probe_sync, device, credentials),
                timeout=self.timeout_seconds + 2,
            )
        except DeviceProbeError:
            raise
        except Exception as exc:
            raise _map_exception(exc) from exc

    def _probe_sync(
        self,
        device: Device,
        credentials: DeviceCredentials | None,
    ) -> DeviceCapabilities:
        username = credentials.username if credentials else ""
        password = credentials.password.get_secret_value() if credentials else ""
        secure = DeviceProtocol.HTTPS in device.protocols
        client = self._client_factory(
            host=str(device.host),
            port=device.management_port,
            username=username,
            password=password,
            timeout=self.timeout_seconds,
            use_https=secure,
            verify_ssl=self.verify_ssl,
            capture_xml=False,
        )

        management = client.devicemgmt()
        info = management.GetDeviceInformation()
        try:
            raw_capabilities = management.GetCapabilities()
        except Exception:
            raw_capabilities = None

        media = client.media()
        raw_profiles = media.GetProfiles() or []
        profiles: list[OnvifProfileSnapshot] = []
        stream_error: Exception | None = None
        supports_audio = False

        for raw_profile in raw_profiles:
            token = str(_get(raw_profile, "token", "Token", "_token") or "").strip()
            encoder = _get(raw_profile, "VideoEncoderConfiguration", "video_encoder_configuration")
            resolution = _get(encoder, "Resolution", "resolution")
            width = _positive_number(_get(resolution, "Width", "width"), int)
            height = _positive_number(_get(resolution, "Height", "height"), int)
            if not token or width is None or height is None:
                continue

            rate = _get(encoder, "RateControl", "rate_control")
            connection_uri: str | None = None
            try:
                stream = media.GetStreamUri(
                    ProfileToken=token,
                    StreamSetup={"Stream": "RTP-Unicast", "Transport": {"Protocol": "RTSP"}},
                )
                connection_uri = _sanitize_uri(_get(stream, "Uri", "URI", "uri"))
            except Exception as exc:
                stream_error = stream_error or exc

            supports_audio = supports_audio or bool(
                _get(raw_profile, "AudioEncoderConfiguration", "audio_encoder_configuration")
            )
            profiles.append(
                OnvifProfileSnapshot(
                    token=token,
                    name=str(_get(raw_profile, "Name", "name") or token),
                    encoding=str(_get(encoder, "Encoding", "encoding") or "unknown"),
                    width=width,
                    height=height,
                    fps=_positive_number(_get(rate, "FrameRateLimit", "frame_rate_limit"), float),
                    bitrate_kbps=_positive_number(
                        _get(rate, "BitrateLimit", "bitrate_limit"), int
                    ),
                    connection_uri=connection_uri,
                )
            )

        if not profiles:
            raise DeviceProbeError(
                ProbeErrorCode.INVALID_RESPONSE,
                "Stage 02 probe returned no usable profiles",
            )
        if not any(profile.connection_uri for profile in profiles):
            if stream_error is not None:
                raise _map_exception(stream_error) from stream_error
            raise DeviceProbeError(
                ProbeErrorCode.INVALID_RESPONSE,
                "Stage 02 probe returned no usable connection metadata",
            )

        device_caps = _get(raw_capabilities, "Device", "device")
        snapshot = OnvifDeviceSnapshot(
            manufacturer=_get(info, "Manufacturer", "manufacturer"),
            model=_get(info, "Model", "model"),
            firmware=_get(info, "FirmwareVersion", "firmware", "Firmware"),
            serial_number=_get(info, "SerialNumber", "serial_number"),
            hardware_id=_get(info, "HardwareId", "HardwareID", "hardware_id"),
            profiles=profiles,
            supports_ptz=bool(_get(raw_capabilities, "PTZ", "ptz")),
            supports_events=bool(_get(raw_capabilities, "Events", "events")),
            supports_audio=supports_audio,
            supports_digital_io=bool(_get(device_caps, "IO", "io")),
        )
        return normalize_onvif_snapshot(snapshot)
