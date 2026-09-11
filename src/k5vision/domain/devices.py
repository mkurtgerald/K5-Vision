"""Canonical device models used across vendor adapters."""

from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, IPvAnyAddress


class DeviceProtocol(StrEnum):
    """Protocols a K5-managed device may expose."""

    ONVIF = "onvif"
    RTSP = "rtsp"
    HTTP = "http"
    HTTPS = "https"
    VENDOR = "vendor"


class DeviceKind(StrEnum):
    """High-level sensor/device categories."""

    CAMERA = "camera"
    BODY_CAMERA = "body_camera"
    DRONE = "drone"
    ENCODER = "encoder"
    SENSOR = "sensor"


class DeviceCreate(BaseModel):
    """Data accepted when registering a device with the control plane."""

    name: str = Field(min_length=1, max_length=128)
    host: IPvAnyAddress
    management_port: int = Field(default=80, ge=1, le=65535)
    kind: DeviceKind = DeviceKind.CAMERA
    protocols: set[DeviceProtocol] = Field(default_factory=lambda: {DeviceProtocol.ONVIF})
    tags: set[str] = Field(default_factory=set)


class Device(DeviceCreate):
    """Canonical registered device."""

    id: UUID = Field(default_factory=uuid4)
