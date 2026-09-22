"""Authenticated playback into the accepted Windows Stage-One operator surface.

A playback request names only an enrolled device, opaque live-stream token, and opaque
recording UUID. The coordinator resolves the live source privately, validates the
recording descriptor against the selected device, and launches existing live and
playback presentation boundaries side-by-side in the accepted Windows runtime.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from k5vision.domain.devices import Device, DeviceKind, DeviceProtocol
from k5vision.domain.users import UserAccount
from k5vision.media.live_presentation import BoundedLivePresentationDelivery
from k5vision.media.mixed_presentation import MixedLiveStream, MixedPlaybackStream
from k5vision.media.playback_schedule import PlaybackRate
from k5vision.media.presentation_playback import BoundedPresentationPlaybackDelivery
from k5vision.media.recording_descriptor import (
    RecordingDescriptorError,
    RecordingStreamDescriptor,
    parse_recording_descriptor,
)
from k5vision.media.viewport_geometry import (
    ViewportGeometry,
    ViewportLayout,
    ViewportPlacement,
)
from k5vision.media.windows_operator_runtime import (
    BoundedWindowsOperatorRuntime,
    WindowsOperatorRuntimeSnapshot,
    WindowsOperatorRuntimeState,
)
from k5vision.operator_launch import (
    OperatorLaunchError,
    OperatorSourceResolver,
    ResolvedLiveSource,
    StreamToken,
)
from k5vision.services.device_registry import DeviceRegistry, DeviceRegistryStorageError

_MAX_ACTIVE_PLAYBACKS = 8
_MAX_DIMENSION = 16_384
_MAX_COUNT = 1_000_000


class OperatorPlaybackErrorCode(StrEnum):
    UNAUTHORIZED = "unauthorized"
    DEVICE_NOT_FOUND = "device_not_found"
    UNSUPPORTED_DEVICE = "unsupported_device"
    SOURCE_UNAVAILABLE = "source_unavailable"
    SOURCE_SCOPE_MISMATCH = "source_scope_mismatch"
    RECORDING_NOT_FOUND = "recording_not_found"
    RECORDING_INVALID = "recording_invalid"
    RECORDING_SCOPE_MISMATCH = "recording_scope_mismatch"
    PLAYBACK_BUSY = "playback_busy"
    PLAYBACK_FAILURE = "playback_failure"
    REGISTRY_UNAVAILABLE = "registry_unavailable"


class OperatorPlaybackError(RuntimeError):
    """Sanitized playback failure without source, credential, path, or payload detail."""

    def __init__(self, code: OperatorPlaybackErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class OperatorPlaybackRequest(BaseModel):
    """Bounded source-safe request for one recorded clip."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    device_id: UUID
    stream_token: StreamToken
    recording_id: UUID
    width: int = Field(default=1280, ge=640, le=_MAX_DIMENSION)
    height: int = Field(default=720, ge=240, le=_MAX_DIMENSION)
    rate: PlaybackRate = PlaybackRate.NORMAL


class OperatorPlaybackMetrics(BaseModel):
    """Aggregate execution result from one visible playback launch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    delivered_frames: int = Field(ge=0, le=_MAX_COUNT)
    presentations: int = Field(ge=0, le=_MAX_COUNT)


class OperatorPlaybackReceipt(BaseModel):
    """Identifier/path/source-free public result for one completed playback."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1"
    completed: bool = True
    delivered_frames: int = Field(ge=0, le=_MAX_COUNT)
    presentations: int = Field(ge=0, le=_MAX_COUNT)
    playback_duration_ms: int = Field(ge=0, le=7 * 24 * 60 * 60 * 1000)


class OperatorPlaybackLauncher(Protocol):
    async def run(
        self,
        live_source: ResolvedLiveSource,
        recording_path: Path,
        descriptor: RecordingStreamDescriptor,
        *,
        width: int,
        height: int,
        rate: PlaybackRate,
    ) -> OperatorPlaybackMetrics: ...


class _WindowsPlaybackRuntime(Protocol):
    @property
    def snapshot(self) -> WindowsOperatorRuntimeSnapshot: ...

    async def start(self, streams) -> WindowsOperatorRuntimeSnapshot: ...

    async def wait(self) -> WindowsOperatorRuntimeSnapshot: ...

    async def close(self) -> WindowsOperatorRuntimeSnapshot: ...


LiveDeliveryFactory = Callable[[int], object]
PlaybackDeliveryFactory = Callable[[Path, RecordingStreamDescriptor, PlaybackRate], object]
WindowsRuntimeFactory = Callable[[ViewportLayout], _WindowsPlaybackRuntime]


def _default_live_delivery(payload_type: int) -> object:
    return BoundedLivePresentationDelivery(
        payload_type,
        packet_goal=2048,
        delivery_timeout_seconds=30.0,
        packet_consumer_timeout_seconds=4.0,
        decoder_timeout_seconds=2.0,
        frame_consumer_timeout_seconds=2.0,
        cleanup_timeout_seconds=2.0,
        relay_startup_probe_seconds=0.5,
    )


def _default_playback_delivery(
    path: Path,
    descriptor: RecordingStreamDescriptor,
    rate: PlaybackRate,
) -> object:
    return BoundedPresentationPlaybackDelivery(
        path,
        descriptor,
        0,
        descriptor.duration_ms,
        rate,
        decoder_timeout_seconds=2.0,
        frame_consumer_timeout_seconds=2.0,
        pump_consumer_timeout_seconds=4.0,
        cleanup_timeout_seconds=2.0,
    )


def _default_windows_runtime(layout: ViewportLayout) -> _WindowsPlaybackRuntime:
    return BoundedWindowsOperatorRuntime(layout)


class WindowsLivePlaybackOperatorLauncher:
    """Present current live video beside recorded playback in the accepted Windows shell."""

    def __init__(
        self,
        *,
        live_delivery_factory: LiveDeliveryFactory = _default_live_delivery,
        playback_delivery_factory: PlaybackDeliveryFactory = _default_playback_delivery,
        runtime_factory: WindowsRuntimeFactory = _default_windows_runtime,
    ) -> None:
        if not callable(live_delivery_factory):
            raise TypeError("live_delivery_factory must be callable")
        if not callable(playback_delivery_factory):
            raise TypeError("playback_delivery_factory must be callable")
        if not callable(runtime_factory):
            raise TypeError("runtime_factory must be callable")
        self._live_delivery_factory = live_delivery_factory
        self._playback_delivery_factory = playback_delivery_factory
        self._runtime_factory = runtime_factory

    async def run(
        self,
        live_source: ResolvedLiveSource,
        recording_path: Path,
        descriptor: RecordingStreamDescriptor,
        *,
        width: int,
        height: int,
        rate: PlaybackRate,
    ) -> OperatorPlaybackMetrics:
        if not isinstance(live_source, ResolvedLiveSource):
            raise OperatorPlaybackError(
                OperatorPlaybackErrorCode.PLAYBACK_FAILURE,
                "operator playback source is invalid",
            )
        if not isinstance(descriptor, RecordingStreamDescriptor):
            raise OperatorPlaybackError(
                OperatorPlaybackErrorCode.PLAYBACK_FAILURE,
                "operator playback descriptor is invalid",
            )
        if not isinstance(rate, PlaybackRate):
            raise OperatorPlaybackError(
                OperatorPlaybackErrorCode.PLAYBACK_FAILURE,
                "operator playback rate is invalid",
            )

        left_width = width // 2
        right_width = width - left_width
        try:
            layout = ViewportLayout(
                placements=(
                    ViewportPlacement(
                        logical_slot=0,
                        geometry=ViewportGeometry(
                            x=0,
                            y=0,
                            width=left_width,
                            height=height,
                        ),
                    ),
                    ViewportPlacement(
                        logical_slot=1,
                        geometry=ViewportGeometry(
                            x=left_width,
                            y=0,
                            width=right_width,
                            height=height,
                        ),
                    ),
                )
            )
            live_delivery = self._live_delivery_factory(live_source.payload_type)
            playback_delivery = self._playback_delivery_factory(
                recording_path,
                descriptor,
                rate,
            )
            streams = (
                MixedLiveStream(
                    slot=0,
                    source_uri=live_source.source_uri,
                    delivery=live_delivery,
                ),
                MixedPlaybackStream(slot=1, delivery=playback_delivery),
            )
            runtime = self._runtime_factory(layout)
        except Exception:
            raise OperatorPlaybackError(
                OperatorPlaybackErrorCode.PLAYBACK_FAILURE,
                "operator playback could not be assembled",
            ) from None

        primary_error: BaseException | None = None
        try:
            await runtime.start(streams)
            final = await runtime.wait()
            if final.state is not WindowsOperatorRuntimeState.COMPLETE:
                raise OperatorPlaybackError(
                    OperatorPlaybackErrorCode.PLAYBACK_FAILURE,
                    "operator playback did not complete",
                )
            return OperatorPlaybackMetrics(
                delivered_frames=final.delivered_frames,
                presentations=final.presentations,
            )
        except asyncio.CancelledError as exc:
            primary_error = exc
            raise
        except OperatorPlaybackError as exc:
            primary_error = exc
            raise
        except Exception as exc:
            primary_error = exc
            raise OperatorPlaybackError(
                OperatorPlaybackErrorCode.PLAYBACK_FAILURE,
                "operator playback failed",
            ) from None
        finally:
            try:
                await runtime.close()
            except asyncio.CancelledError:
                if primary_error is None:
                    raise
            except Exception:
                if primary_error is None:
                    raise OperatorPlaybackError(
                        OperatorPlaybackErrorCode.PLAYBACK_FAILURE,
                        "operator playback cleanup failed",
                    ) from None


class BoundedOperatorPlaybackCoordinator:
    """Authorize one recorded clip and present it beside its enrolled live source."""

    def __init__(
        self,
        registry: DeviceRegistry,
        source_resolver: OperatorSourceResolver,
        recording_root: str | Path,
        *,
        launcher: OperatorPlaybackLauncher | None = None,
        max_active_playbacks: int = 2,
    ) -> None:
        if not isinstance(registry, DeviceRegistry):
            raise TypeError("registry must be a DeviceRegistry")
        if not callable(getattr(source_resolver, "resolve", None)):
            raise TypeError("source_resolver must implement resolve")
        root_text = str(recording_root).strip()
        if not root_text:
            raise ValueError("recording_root is required")
        if not 1 <= max_active_playbacks <= _MAX_ACTIVE_PLAYBACKS:
            raise ValueError(
                f"max_active_playbacks must be between 1 and {_MAX_ACTIVE_PLAYBACKS}"
            )
        selected_launcher = launcher or WindowsLivePlaybackOperatorLauncher()
        if not callable(getattr(selected_launcher, "run", None)):
            raise TypeError("launcher must implement run")

        self._registry = registry
        self._source_resolver = source_resolver
        self._recording_root = Path(recording_root).expanduser().resolve(strict=False)
        self._launcher = selected_launcher
        self._max_active_playbacks = max_active_playbacks
        self._active_playbacks = 0
        self._state_lock = asyncio.Lock()

    @property
    def active_playbacks(self) -> int:
        return self._active_playbacks

    async def _reserve(self) -> None:
        async with self._state_lock:
            if self._active_playbacks >= self._max_active_playbacks:
                raise OperatorPlaybackError(
                    OperatorPlaybackErrorCode.PLAYBACK_BUSY,
                    "operator playback capacity is currently exhausted",
                )
            self._active_playbacks += 1

    async def _release(self) -> None:
        async with self._state_lock:
            self._active_playbacks = max(0, self._active_playbacks - 1)

    @staticmethod
    def _validate_principal(principal: UserAccount) -> None:
        if not isinstance(principal, UserAccount) or not principal.enabled:
            raise OperatorPlaybackError(
                OperatorPlaybackErrorCode.UNAUTHORIZED,
                "an enabled human session is required",
            )

    @staticmethod
    def _validate_device(device: Device) -> None:
        video_kinds = {DeviceKind.CAMERA, DeviceKind.BODY_CAMERA, DeviceKind.ENCODER}
        video_protocols = {DeviceProtocol.ONVIF, DeviceProtocol.RTSP}
        if device.kind not in video_kinds or not device.protocols.intersection(video_protocols):
            raise OperatorPlaybackError(
                OperatorPlaybackErrorCode.UNSUPPORTED_DEVICE,
                "the selected device does not expose a supported video capability",
            )

    @staticmethod
    def _validate_live_source(device: Device, source: ResolvedLiveSource) -> None:
        try:
            parsed = urlsplit(source.source_uri)
            host = parsed.hostname
        except ValueError:
            parsed = None
            host = None
        if (
            parsed is None
            or parsed.scheme.casefold() not in {"rtsp", "rtsps"}
            or host is None
            or host.casefold() != str(device.host).casefold()
        ):
            raise OperatorPlaybackError(
                OperatorPlaybackErrorCode.SOURCE_SCOPE_MISMATCH,
                "resolved live source is outside the selected device scope",
            )

    def _load_recording(
        self,
        recording_id: UUID,
        device: Device,
    ) -> tuple[Path, RecordingStreamDescriptor]:
        recording_path = self._recording_root / f"{recording_id}.k5r"
        descriptor_path = self._recording_root / f"{recording_id}.k5d"
        if not recording_path.is_file() or not descriptor_path.is_file():
            raise OperatorPlaybackError(
                OperatorPlaybackErrorCode.RECORDING_NOT_FOUND,
                "selected recording was not found",
            )
        try:
            descriptor = parse_recording_descriptor(descriptor_path.read_bytes())
            file_bytes = recording_path.stat().st_size
        except (OSError, RecordingDescriptorError):
            raise OperatorPlaybackError(
                OperatorPlaybackErrorCode.RECORDING_INVALID,
                "selected recording metadata is invalid",
            ) from None
        if descriptor.recording_id != recording_id:
            raise OperatorPlaybackError(
                OperatorPlaybackErrorCode.RECORDING_INVALID,
                "selected recording metadata is invalid",
            )
        if descriptor.source_id != device.id:
            raise OperatorPlaybackError(
                OperatorPlaybackErrorCode.RECORDING_SCOPE_MISMATCH,
                "selected recording is outside the selected device scope",
            )
        if file_bytes != descriptor.file_bytes:
            raise OperatorPlaybackError(
                OperatorPlaybackErrorCode.RECORDING_INVALID,
                "selected recording failed size validation",
            )
        return recording_path, descriptor

    async def play(
        self,
        principal: UserAccount,
        request: OperatorPlaybackRequest,
    ) -> OperatorPlaybackReceipt:
        self._validate_principal(principal)
        if not isinstance(request, OperatorPlaybackRequest):
            raise TypeError("request must be an OperatorPlaybackRequest")
        try:
            device = self._registry.get(request.device_id)
        except DeviceRegistryStorageError:
            raise OperatorPlaybackError(
                OperatorPlaybackErrorCode.REGISTRY_UNAVAILABLE,
                "device registry is unavailable",
            ) from None
        if device is None:
            raise OperatorPlaybackError(
                OperatorPlaybackErrorCode.DEVICE_NOT_FOUND,
                "selected device was not found",
            )
        self._validate_device(device)
        recording_path, descriptor = self._load_recording(request.recording_id, device)

        await self._reserve()
        try:
            try:
                live_source = await self._source_resolver.resolve(device, request.stream_token)
            except OperatorLaunchError:
                raise OperatorPlaybackError(
                    OperatorPlaybackErrorCode.SOURCE_UNAVAILABLE,
                    "selected live source could not be resolved",
                ) from None
            except Exception:
                raise OperatorPlaybackError(
                    OperatorPlaybackErrorCode.SOURCE_UNAVAILABLE,
                    "selected live source could not be resolved",
                ) from None
            if not isinstance(live_source, ResolvedLiveSource):
                raise OperatorPlaybackError(
                    OperatorPlaybackErrorCode.SOURCE_UNAVAILABLE,
                    "selected live source could not be resolved",
                )
            self._validate_live_source(device, live_source)

            try:
                metrics = await self._launcher.run(
                    live_source,
                    recording_path,
                    descriptor,
                    width=request.width,
                    height=request.height,
                    rate=request.rate,
                )
            except OperatorPlaybackError:
                raise
            except Exception:
                raise OperatorPlaybackError(
                    OperatorPlaybackErrorCode.PLAYBACK_FAILURE,
                    "operator playback failed",
                ) from None
            if not isinstance(metrics, OperatorPlaybackMetrics):
                raise OperatorPlaybackError(
                    OperatorPlaybackErrorCode.PLAYBACK_FAILURE,
                    "operator playback returned an invalid result",
                )
            return OperatorPlaybackReceipt(
                delivered_frames=metrics.delivered_frames,
                presentations=metrics.presentations,
                playback_duration_ms=descriptor.duration_ms,
            )
        finally:
            await self._release()
