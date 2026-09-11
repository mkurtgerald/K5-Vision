"""ONVIF extraction snapshots and normalization helpers.

Network/SOAP handling belongs in an ONVIF client implementation. This module
accepts already-extracted values and converts them into K5's canonical domain,
keeping third-party ONVIF objects out of the rest of the platform.
"""

from pydantic import BaseModel, Field

from k5vision.domain.capabilities import DeviceCapabilities, DeviceIdentity
from k5vision.domain.streams import StreamProfile, StreamRole, VideoCodec


class OnvifProfileSnapshot(BaseModel):
    """Minimal fields extracted from an ONVIF media profile."""

    token: str = Field(min_length=1)
    name: str = Field(min_length=1)
    encoding: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps: float | None = Field(default=None, gt=0)
    bitrate_kbps: int | None = Field(default=None, gt=0)
    connection_uri: str | None = None


class OnvifDeviceSnapshot(BaseModel):
    """Library-neutral data extracted during an ONVIF probe."""

    manufacturer: str | None = None
    model: str | None = None
    firmware: str | None = None
    serial_number: str | None = None
    hardware_id: str | None = None
    profiles: list[OnvifProfileSnapshot] = Field(default_factory=list)
    supports_ptz: bool = False
    supports_events: bool = False
    supports_audio: bool = False
    supports_digital_io: bool = False


def _codec(encoding: str) -> VideoCodec:
    normalized = encoding.strip().lower().replace(".", "")
    return {
        "h264": VideoCodec.H264,
        "h265": VideoCodec.H265,
        "hevc": VideoCodec.H265,
        "mjpeg": VideoCodec.MJPEG,
        "jpeg": VideoCodec.MJPEG,
    }.get(normalized, VideoCodec.UNKNOWN)


def normalize_onvif_snapshot(snapshot: OnvifDeviceSnapshot) -> DeviceCapabilities:
    """Convert extracted ONVIF fields into stable K5 capability contracts."""
    ordered = sorted(
        snapshot.profiles,
        key=lambda profile: (profile.width * profile.height, profile.bitrate_kbps or 0),
        reverse=True,
    )

    profiles: list[StreamProfile] = []
    for index, profile in enumerate(ordered):
        if index == 0:
            role = StreamRole.MAIN
        elif index == len(ordered) - 1:
            role = StreamRole.SUBSTREAM
        else:
            role = StreamRole.AUXILIARY

        profiles.append(
            StreamProfile(
                token=profile.token,
                name=profile.name,
                role=role,
                codec=_codec(profile.encoding),
                width=profile.width,
                height=profile.height,
                fps=profile.fps,
                bitrate_kbps=profile.bitrate_kbps,
                connection_uri=profile.connection_uri,
            )
        )

    return DeviceCapabilities(
        identity=DeviceIdentity(
            manufacturer=snapshot.manufacturer,
            model=snapshot.model,
            firmware=snapshot.firmware,
            serial_number=snapshot.serial_number,
            hardware_id=snapshot.hardware_id,
        ),
        stream_profiles=profiles,
        supports_ptz=snapshot.supports_ptz,
        supports_events=snapshot.supports_events,
        supports_audio=snapshot.supports_audio,
        supports_digital_io=snapshot.supports_digital_io,
    )
