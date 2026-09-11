"""Canonical media profile contracts independent of camera vendors."""

from enum import StrEnum
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator


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
    def reject_embedded_credentials(cls, value: str | None) -> str | None:
        """Keep credentials out of canonical connection metadata."""
        if value is None:
            return None
        parsed = urlsplit(value)
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("connection_uri must not contain embedded credentials")
        return value

    @property
    def pixel_count(self) -> int:
        """Return frame pixel count for deterministic profile ranking."""
        return self.width * self.height
