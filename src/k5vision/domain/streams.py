"""Canonical media profile contracts independent of camera vendors."""

from enum import StrEnum
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel, Field, field_validator

_SAFE_CONNECTION_QUERY_VALUES = {"transport": frozenset({"tcp", "udp"})}


class VideoCodec(StrEnum):
    """Video codecs understood by K5's canonical stream model."""

    H264 = "h264"
    H265 = "h265"
    MJPEG = "mjpeg"
    UNKNOWN = "unknown"


class StreamRole(StrEnum):
    """Logical use of a camera media profile."""

    MAIN = "main"
    SUBSTREAM = "substream"
    AUXILIARY = "auxiliary"


class StreamProfile(BaseModel):
    """Vendor-neutral description of a camera media profile."""

    token: str = Field(min_length=1)
    name: str = Field(min_length=1)
    role: StreamRole
    codec: VideoCodec = VideoCodec.UNKNOWN
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps: float | None = Field(default=None, gt=0)
    bitrate_kbps: int | None = Field(default=None, gt=0)
    connection_uri: str | None = None

    @field_validator("connection_uri")
    @classmethod
    def reject_sensitive_connection_metadata(cls, value: str | None) -> str | None:
        """Keep credentials and secret-bearing URI components out of canonical metadata."""
        if value is None:
            return None
        parsed = urlsplit(value)
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("connection_uri must not contain embedded credentials")
        if parsed.fragment:
            raise ValueError("connection_uri must not contain a fragment")
        for key, query_value in parse_qsl(parsed.query, keep_blank_values=True):
            normalized_key = key.casefold()
            normalized_value = query_value.casefold()
            allowed_values = _SAFE_CONNECTION_QUERY_VALUES.get(normalized_key)
            if allowed_values is None or normalized_value not in allowed_values:
                raise ValueError("connection_uri contains non-allowlisted query metadata")
        return value

    @property
    def pixel_count(self) -> int:
        """Return frame pixel count for deterministic profile ranking."""
        return self.width * self.height
