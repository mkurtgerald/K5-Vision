"""Canonical device identity and capability contracts."""

from pydantic import BaseModel

from k5vision.domain.streams import StreamProfile


class DeviceIdentity(BaseModel):
    """Normalized hardware identity reported by a device adapter."""

    manufacturer: str | None = None
    model: str | None = None
    firmware: str | None = None
    serial_number: str | None = None
    hardware_id: str | None = None


class DeviceCapabilities(BaseModel):
    """Normalized capabilities exposed to the rest of K5 Vision."""

    identity: DeviceIdentity
    stream_profiles: list[StreamProfile]
    supports_ptz: bool = False
    supports_events: bool = False
    supports_audio: bool = False
    supports_digital_io: bool = False
