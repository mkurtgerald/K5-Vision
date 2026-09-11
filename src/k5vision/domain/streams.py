"""Canonical media profile contracts independent of camera vendors."""

from enum import StrEnum

from pydantic import BaseModel, Field


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

    @property
    def pixel_count(self) -> int:
        """Return frame pixel count for deterministic profile ranking."""
        return self.width * self.height
