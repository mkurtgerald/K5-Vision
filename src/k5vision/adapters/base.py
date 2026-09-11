"""Stable device-adapter contracts used by discovery/probe implementations."""

from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, SecretStr

from k5vision.domain.capabilities import DeviceCapabilities
from k5vision.domain.devices import Device


class ProbeErrorCode(StrEnum):
    """Normalized probe failures presented to orchestration code."""

    AUTHENTICATION_FAILED = "authentication_failed"
    TIMEOUT = "timeout"
    UNREACHABLE = "unreachable"
    INVALID_RESPONSE = "invalid_response"
    UNSUPPORTED = "unsupported"


class DeviceCredentials(BaseModel):
    """Ephemeral credentials supplied to an adapter; never persisted by this model."""

    username: str
    password: SecretStr


class DeviceProbeError(RuntimeError):
    """Adapter failure with a stable machine-readable reason."""

    def __init__(self, code: ProbeErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class DeviceAdapter(Protocol):
    """Protocol implemented by ONVIF and future vendor-specific adapters."""

    async def probe(
        self,
        device: Device,
        credentials: DeviceCredentials | None = None,
    ) -> DeviceCapabilities:
        """Probe one device and return canonical capabilities."""
        ...
