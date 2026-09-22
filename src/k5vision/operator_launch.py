"""Authenticated source-safe handoff into the accepted live operator runtime.

This boundary deliberately keeps camera connection material execution-only. Callers
select an already-enrolled device and a credential-free stream token; a private
resolver may turn that selection into a transient authenticated RTSP URI, but that
URI never appears in the request, receipt, retained coordinator state, or errors.
"""

from __future__ import annotations

import asyncio
import enum
from dataclasses import dataclass
from typing import Annotated, Protocol
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from k5vision.domain.devices import Device, DeviceKind, DeviceProtocol
from k5vision.domain.users import UserAccount
from k5vision.services.device_registry import DeviceRegistry, DeviceRegistryStorageError

_MAX_ACTIVE_LAUNCHES = 16
_MAX_STREAM_TOKEN_LENGTH = 256
_MAX_DIMENSION = 16_384
_MAX_COUNT = 1_000_000

StreamToken = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=_MAX_STREAM_TOKEN_LENGTH,
        pattern=r"^[A-Za-z0-9._:-]{1,256}$",
    ),
]


class OperatorLaunchErrorCode(enum.StrEnum):
    """Stable source-free failure classes for the Stage-One launch seam."""

    UNAUTHORIZED = "unauthorized"
    DEVICE_NOT_FOUND = "device_not_found"
    UNSUPPORTED_DEVICE = "unsupported_device"
    SOURCE_UNAVAILABLE = "source_unavailable"
    SOURCE_SCOPE_MISMATCH = "source_scope_mismatch"
    LAUNCH_BUSY = "launch_busy"
    LAUNCH_FAILURE = "launch_failure"
    REGISTRY_UNAVAILABLE = "registry_unavailable"


class OperatorLaunchError(RuntimeError):
    """Sanitized launch failure that never retains connection material."""

    def __init__(self, code: OperatorLaunchErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class OperatorLaunchRequest(BaseModel):
    """Bounded public request containing no URI or credential material."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    device_id: UUID
    stream_token: StreamToken
    width: int = Field(default=1280, ge=320, le=_MAX_DIMENSION)
    height: int = Field(default=720, ge=240, le=_MAX_DIMENSION)


class OperatorLaunchMetrics(BaseModel):
    """Aggregate execution result returned by the accepted operator launcher."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    delivered_frames: int = Field(default=0, ge=0, le=_MAX_COUNT)
    presentations: int = Field(default=0, ge=0, le=_MAX_COUNT)
    processed_controls: int = Field(default=0, ge=0, le=_MAX_COUNT)


class OperatorLaunchReceipt(BaseModel):
    """Credential/source/media-free retained result for one completed launch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1"
    completed: bool = True
    delivered_frames: int = Field(ge=0, le=_MAX_COUNT)
    presentations: int = Field(ge=0, le=_MAX_COUNT)
    processed_controls: int = Field(ge=0, le=_MAX_COUNT)


@dataclass(frozen=True, slots=True)
class ResolvedLiveSource:
    """Private execution-only live source resolved inside the trusted runtime."""

    source_uri: str
    payload_type: int

    def __post_init__(self) -> None:
        if not isinstance(self.source_uri, str) or not self.source_uri.strip():
            raise ValueError("resolved live source is invalid")
        if isinstance(self.payload_type, bool) or not 96 <= self.payload_type <= 127:
            raise ValueError("resolved live source payload type is invalid")


class OperatorSourceResolver(Protocol):
    """Resolve credential-free product selection to private runtime material."""

    async def resolve(self, device: Device, stream_token: str) -> ResolvedLiveSource: ...


class OperatorLauncher(Protocol):
    """Invoke the already-qualified visible operator runtime for one source."""

    async def run(
        self,
        source: ResolvedLiveSource,
        *,
        width: int,
        height: int,
    ) -> OperatorLaunchMetrics: ...


class BoundedOperatorLaunchCoordinator:
    """Authorize an enrolled source and hand it to a bounded private launcher."""

    def __init__(
        self,
        registry: DeviceRegistry,
        source_resolver: OperatorSourceResolver,
        launcher: OperatorLauncher,
        *,
        max_active_launches: int = 4,
    ) -> None:
        if not isinstance(registry, DeviceRegistry):
            raise TypeError("registry must be a DeviceRegistry")
        if not callable(getattr(source_resolver, "resolve", None)):
            raise TypeError("source_resolver must implement resolve")
        if not callable(getattr(launcher, "run", None)):
            raise TypeError("launcher must implement run")
        if not 1 <= max_active_launches <= _MAX_ACTIVE_LAUNCHES:
            raise ValueError(f"max_active_launches must be between 1 and {_MAX_ACTIVE_LAUNCHES}")

        self._registry = registry
        self._source_resolver = source_resolver
        self._launcher = launcher
        self._max_active_launches = max_active_launches
        self._active_launches = 0
        self._state_lock = asyncio.Lock()

    @property
    def active_launches(self) -> int:
        """Return only the aggregate active count; no source identity is retained."""
        return self._active_launches

    async def _reserve(self) -> None:
        async with self._state_lock:
            if self._active_launches >= self._max_active_launches:
                raise OperatorLaunchError(
                    OperatorLaunchErrorCode.LAUNCH_BUSY,
                    "operator launch capacity is currently exhausted",
                )
            self._active_launches += 1

    async def _release(self) -> None:
        async with self._state_lock:
            self._active_launches = max(0, self._active_launches - 1)

    @staticmethod
    def _validate_principal(principal: UserAccount) -> None:
        if not isinstance(principal, UserAccount) or not principal.enabled:
            raise OperatorLaunchError(
                OperatorLaunchErrorCode.UNAUTHORIZED,
                "an enabled human session is required",
            )

    @staticmethod
    def _validate_device(device: Device) -> None:
        video_kinds = {DeviceKind.CAMERA, DeviceKind.BODY_CAMERA, DeviceKind.ENCODER}
        video_protocols = {DeviceProtocol.ONVIF, DeviceProtocol.RTSP}
        if device.kind not in video_kinds or not device.protocols.intersection(video_protocols):
            raise OperatorLaunchError(
                OperatorLaunchErrorCode.UNSUPPORTED_DEVICE,
                "the selected device does not expose a supported live-video capability",
            )

    @staticmethod
    def _validate_resolved_source(device: Device, source: ResolvedLiveSource) -> None:
        try:
            parsed = urlsplit(source.source_uri)
            source_host = parsed.hostname
        except ValueError:
            source_host = None
            parsed = None
        if (
            parsed is None
            or parsed.scheme.casefold() not in {"rtsp", "rtsps"}
            or source_host is None
            or source_host.casefold() != str(device.host).casefold()
        ):
            raise OperatorLaunchError(
                OperatorLaunchErrorCode.SOURCE_SCOPE_MISMATCH,
                "resolved live source is outside the selected device scope",
            )

    async def launch(
        self,
        principal: UserAccount,
        request: OperatorLaunchRequest,
    ) -> OperatorLaunchReceipt:
        """Run one authorized source without exposing its private runtime URI."""
        self._validate_principal(principal)
        if not isinstance(request, OperatorLaunchRequest):
            raise TypeError("request must be an OperatorLaunchRequest")

        try:
            device = self._registry.get(request.device_id)
        except DeviceRegistryStorageError:
            raise OperatorLaunchError(
                OperatorLaunchErrorCode.REGISTRY_UNAVAILABLE,
                "device registry is unavailable",
            ) from None
        if device is None:
            raise OperatorLaunchError(
                OperatorLaunchErrorCode.DEVICE_NOT_FOUND,
                "selected device was not found",
            )
        self._validate_device(device)

        await self._reserve()
        try:
            try:
                source = await self._source_resolver.resolve(device, request.stream_token)
            except OperatorLaunchError:
                raise
            except Exception:
                raise OperatorLaunchError(
                    OperatorLaunchErrorCode.SOURCE_UNAVAILABLE,
                    "selected live source could not be resolved",
                ) from None

            if not isinstance(source, ResolvedLiveSource):
                raise OperatorLaunchError(
                    OperatorLaunchErrorCode.SOURCE_UNAVAILABLE,
                    "selected live source could not be resolved",
                )
            self._validate_resolved_source(device, source)

            try:
                metrics = await self._launcher.run(
                    source,
                    width=request.width,
                    height=request.height,
                )
            except OperatorLaunchError:
                raise
            except Exception:
                raise OperatorLaunchError(
                    OperatorLaunchErrorCode.LAUNCH_FAILURE,
                    "live operator runtime failed",
                ) from None
            if not isinstance(metrics, OperatorLaunchMetrics):
                raise OperatorLaunchError(
                    OperatorLaunchErrorCode.LAUNCH_FAILURE,
                    "live operator runtime returned an invalid result",
                )

            return OperatorLaunchReceipt(
                delivered_frames=metrics.delivered_frames,
                presentations=metrics.presentations,
                processed_controls=metrics.processed_controls,
            )
        finally:
            await self._release()
