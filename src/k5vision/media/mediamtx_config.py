"""Generate a private, enrolled-only native MediaMTX worker configuration.

The returned JSON is valid YAML for MediaMTX. It contains execution credentials
and source URIs: callers must write it into protected local state, never an argv,
public artifact or log. This module does not create files or expose config APIs.
"""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit

from k5vision.domain.devices import Device, DeviceKind, DeviceProtocol
from k5vision.media.mediamtx_provider import MediaMTXBinding, MediaMTXCredentials


@dataclass(frozen=True, slots=True)
class MediaMTXSource:
    """An enrolled camera selection with execution-only source material."""

    binding: MediaMTXBinding
    device: Device = field(repr=False)
    source_uri: str = field(repr=False)

    def __post_init__(self) -> None:
        valid = False
        try:
            source = urlsplit(self.source_uri)
            host = ipaddress.ip_address(source.hostname or "")
            port = 554 if source.port is None else source.port
            userinfo = source.netloc.rsplit("@", 1)[0] if "@" in source.netloc else None
            valid = (
                isinstance(self.binding, MediaMTXBinding)
                and isinstance(self.device, Device)
                and self.device.id == self.binding.device_id
                and self.device.kind
                in {DeviceKind.CAMERA, DeviceKind.BODY_CAMERA, DeviceKind.ENCODER}
                and bool(self.device.protocols & {DeviceProtocol.RTSP, DeviceProtocol.ONVIF})
                and source.scheme in {"rtsp", "rtsps"}
                and self.source_uri.startswith(("rtsp://", "rtsps://"))
                and re.search(r"%(?![0-9A-Fa-f]{2})", self.source_uri) is None
                and (
                    userinfo is None
                    or (
                        re.fullmatch(r"[A-Za-z0-9._~!$&'()*+,;=:%@-]+", userinfo) is not None
                        and bool(source.username)
                        and source.password is not None
                    )
                )
                and host == ipaddress.ip_address(str(self.device.host))
                and 1 <= port <= 65535
                and not source.fragment
                and len(self.source_uri) <= 4096
                and not any(
                    ord(character) <= 32 or ord(character) == 127 for character in self.source_uri
                )
            )
        except (TypeError, ValueError, AttributeError):
            pass
        if not valid:
            raise ValueError("private media source must match its enrolled camera selection")


def _recording_directory(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError("recordings require an absolute local directory")
    windows = PureWindowsPath(value)
    posix = PurePosixPath(value)
    if (
        value != value.strip()
        or any(ord(character) < 32 for character in value)
        or "%" in value
        or ".." in windows.parts
        or ".." in posix.parts
        or value.startswith(("\\\\", "//"))
        or not (windows.is_absolute() or posix.is_absolute())
        or value in ("/", "\\")
        or (windows.is_absolute() and len(windows.parts) == 1)
        or any(":" in part for part in windows.parts[1:])
    ):
        raise ValueError("recordings require an absolute local directory")
    return value.rstrip("/\\") + "/%path/%Y-%m-%d_%H-%M-%S-%f_%z"


def generate_mediamtx_configuration(
    *,
    site_id: str,
    sources: Sequence[MediaMTXSource],
    credentials: MediaMTXCredentials,
    recording_directory: str,
    api_port: int = 9997,
    playback_port: int = 9996,
    rtsp_port: int = 8554,
    hls_port: int = 8888,
    segment_seconds: int = 60,
    retention_seconds: int = 0,
    max_readers: int = 16,
    allow_empty: bool = False,
) -> str:
    """Return deterministic private config, with no anonymous or wildcard paths.

    This first service composition keeps all interfaces on loopback. Browser
    access must pass through K5 authorization; native-worker credentials remain
    internal. The installer separately admits filesystem ownership and the exact
    executable version before starting this configuration.

    Native filename/wall-clock age deletion is always disabled; the retained
    ``retention_seconds`` argument accepts only zero. Configure elapsed retention
    on ``RecordingStorage`` in the owning K5 service. Standalone donor users must
    supply their own recording maintenance rather than relying on this config.
    """
    if not isinstance(credentials, MediaMTXCredentials):
        raise TypeError("private media credentials are required")
    ports = (api_port, playback_port, rtsp_port, hls_port)
    if any(type(port) is not int or not 1 <= port <= 65535 for port in ports):
        raise ValueError("media listener ports are invalid")
    if len(set(ports)) != len(ports):
        raise ValueError("media listeners require distinct ports")
    if type(retention_seconds) is not int or retention_seconds != 0:
        raise ValueError(
            "Native age deletion must remain disabled; configure RecordingStorage retention instead"
        )
    if (
        type(segment_seconds) is not int
        or not 1 <= segment_seconds <= 3600
        or type(max_readers) is not int
        or not 1 <= max_readers <= 64
        or type(allow_empty) is not bool
        or not (0 if allow_empty else 1) <= len(sources) <= 64
    ):
        raise ValueError("media recording and capacity bounds are invalid")
    recording_path = _recording_directory(recording_directory)
    paths = {}
    for source in sources:
        if not isinstance(source, MediaMTXSource) or source.binding.site_id != site_id:
            raise ValueError("media source belongs to a different site")
        source.__post_init__()
        path = source.binding.path_name
        if path in paths:
            raise ValueError("media configuration contains a duplicate selection")
        paths[path] = {
            "source": source.source_uri,
            "sourceOnDemand": False,
            "rtspTransport": "tcp",
            "useAbsoluteTimestamp": False,
            "record": True,
            "recordPath": recording_path,
            "recordFormat": "fmp4",
            "recordPartDuration": "1s",
            "recordSegmentDuration": f"{segment_seconds}s",
            # Native cleanup ages filenames against the adjustable wall clock.
            # K5 owns retention using observed monotonic elapsed time instead.
            "recordDeleteAfter": "0s",
            "maxReaders": max_readers,
        }
    permissions = [{"action": "api"}]
    permissions.extend(
        {"action": action, "path": path}
        for path in sorted(paths)
        for action in ("read", "playback")
    )
    config = {
        "logLevel": "warn",
        "authMethod": "internal",
        "authInternalUsers": [
            {
                "user": credentials.username,
                "pass": credentials.password,
                "ips": ["127.0.0.1", "::1"],
                "permissions": permissions,
            }
        ],
        "api": True,
        "apiAddress": f"127.0.0.1:{api_port}",
        "apiAllowOrigins": [],
        "playback": True,
        "playbackAddress": f"127.0.0.1:{playback_port}",
        "playbackAllowOrigins": [],
        "rtsp": True,
        "rtspAddress": f"127.0.0.1:{rtsp_port}",
        "rtspTransports": ["tcp"],
        "hls": True,
        "hlsAddress": f"127.0.0.1:{hls_port}",
        "hlsAllowOrigins": [],
        "webrtc": False,
        "rtmp": False,
        "srt": False,
        "moq": False,
        "metrics": False,
        "pprof": False,
        "paths": dict(sorted(paths.items())),
    }
    return json.dumps(config, sort_keys=True, indent=2) + "\n"
