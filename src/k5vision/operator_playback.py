"""Authenticated bounded playback for the Stage-One operator workflow.

This boundary binds an opaque recording UUID back to its replay descriptor and enrolled
source, then composes the accepted live and playback presentation paths into the
existing Windows operator runtime. Public state remains source/path/credential/media
free; recording paths and resolved live connection material remain execution-private.
"""

from __future__ import annotations

import asyncio
import stat
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from k5vision.domain.devices import Device, DeviceKind, DeviceProtocol
from k5vision.domain.users import UserAccount, UserRole
from k5vision.media.mixed_presentation import MixedLiveStream, MixedPlaybackStream
from k5vision.media.pausable_presentation_playback import PausablePresentationPlaybackDelivery
from k5vision.media.playback_control import (
    PlaybackControlSnapshot,
    PlaybackControlState,
    PlaybackPauseControl,
)
from k5vision.media.playback_schedule import PlaybackRate
from k5vision.media.recording_descriptor import (
    RecordingDescriptorError,
    RecordingStreamDescriptor,
    parse_recording_descriptor,
)
from k5vision.media.viewport_geometry import ViewportGeometry, ViewportLayout, ViewportPlacement
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
from k5vision.operator_runtime import (
    _default_delivery_factory,
    _stage_one_presentation_runtime_factory,
)
from k5vision.services.device_registry import DeviceRegistry, DeviceRegistryStorageError

_MAX_ACTIVE_PLAYBACKS = 8
_MAX_DIMENSION = 16_384
_MAX_DESCRIPTOR_BYTES = 4096
_MAX_COUNT = 1_000_000


class OperatorPlaybackErrorCode(StrEnum):
    UNAUTHORIZED = "unauthorized"
    RECORDING_NOT_FOUND = "recording_not_found"
    RECORDING_INVALID = "recording_invalid"
    DEVICE_NOT_FOUND = "device_not_found"
    UNSUPPORTED_DEVICE = "unsupported_device"
    SOURCE_UNAVAILABLE = "source_unavailable"
    SOURCE_SCOPE_MISMATCH = "source_scope_mismatch"
    WINDOW_INVALID = "window_invalid"
    PLAYBACK_BUSY = "playback_busy"
    CONTROL_CONFLICT = "control_conflict"
    CONTROL_NOT_FOUND = "control_not_found"
    CONTROL_FORBIDDEN = "control_forbidden"
    PLAYBACK_FAILURE = "playback_failure"
    REGISTRY_UNAVAILABLE = "registry_unavailable"


class OperatorPlaybackError(RuntimeError):
    """Sanitized playback failure without source, credential, path, or payload detail."""

    def __init__(self, code: OperatorPlaybackErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class OperatorPlaybackRequest(BaseModel):
    """Bounded public request for one recorded clip in the existing operator surface."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    recording_id: UUID
    control_id: UUID | None = None
    stream_token: StreamToken = "main"
    start_ms: int = Field(default=0, ge=0, le=7 * 24 * 60 * 60 * 1000)
    end_ms: int | None = Field(default=None, ge=0, le=7 * 24 * 60 * 60 * 1000)
    rate: PlaybackRate = PlaybackRate.NORMAL
    width: int = Field(default=1280, ge=640, le=_MAX_DIMENSION)
    height: int = Field(default=720, ge=240, le=_MAX_DIMENSION)

    @model_validator(mode="after")
    def validate_requested_window(self) -> OperatorPlaybackRequest:
        if self.end_ms is not None and self.end_ms < self.start_ms:
            raise ValueError("playback end must not precede start")
        return self


class OperatorPlaybackMetrics(BaseModel):
    """Aggregate result from the accepted mixed live/playback operator runtime."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    delivered_frames: int = Field(default=0, ge=0, le=_MAX_COUNT)
    presentations: int = Field(default=0, ge=0, le=_MAX_COUNT)
    processed_controls: int = Field(default=0, ge=0, le=_MAX_COUNT)
    descriptor_verified: bool = False


class OperatorPlaybackReceipt(BaseModel):
    """Path/source/media-free result for one completed operator playback."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1"
    recording_id: UUID
    completed: bool = True
    delivered_frames: int = Field(ge=0, le=_MAX_COUNT)
    presentations: int = Field(ge=0, le=_MAX_COUNT)
    processed_controls: int = Field(ge=0, le=_MAX_COUNT)
    descriptor_verified: bool


class OperatorPlaybackControlAction(StrEnum):
    PAUSE = "pause"
    RESUME = "resume"


class OperatorPlaybackControlReceipt(BaseModel):
    """Source-free status for one authorized active playback control."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1"
    control_id: UUID
    state: PlaybackControlState
    pause_count: int = Field(ge=0, le=_MAX_COUNT)
    resume_count: int = Field(ge=0, le=_MAX_COUNT)
    paused_total_ms: int = Field(ge=0, le=2_147_483_647)


class OperatorPlaybackLauncher(Protocol):
    async def run(
        self,
        source: ResolvedLiveSource,
        recording_path: Path,
        descriptor: RecordingStreamDescriptor,
        *,
        start_ms: int,
        end_ms: int,
        rate: PlaybackRate,
        width: int,
        height: int,
        pause_control: PlaybackPauseControl | None = None,
    ) -> OperatorPlaybackMetrics: ...


class _WindowsOperatorBoundary(Protocol):
    async def start(self, streams: tuple[MixedLiveStream | MixedPlaybackStream, ...]) -> object: ...

    async def wait(self) -> WindowsOperatorRuntimeSnapshot: ...

    async def close(self) -> WindowsOperatorRuntimeSnapshot: ...


class WindowsMixedOperatorPlaybackLauncher:
    """Present the current live source beside one bounded recorded playback."""

    def __init__(self, *, runtime_factory=None, live_delivery_factory=None, playback_factory=None):
        self._runtime_factory = runtime_factory or self._default_runtime_factory
        self._live_delivery_factory = live_delivery_factory or _default_delivery_factory
        self._playback_factory = playback_factory or PausablePresentationPlaybackDelivery
        if not callable(self._runtime_factory):
            raise TypeError("runtime_factory must be callable")
        if not callable(self._live_delivery_factory):
            raise TypeError("live_delivery_factory must be callable")
        if not callable(self._playback_factory):
            raise TypeError("playback_factory must be callable")

    @staticmethod
    def _default_runtime_factory(layout: ViewportLayout) -> BoundedWindowsOperatorRuntime:
        return BoundedWindowsOperatorRuntime(
            layout,
            presentation_runtime_factory=_stage_one_presentation_runtime_factory,
        )

    async def run(
        self,
        source: ResolvedLiveSource,
        recording_path: Path,
        descriptor: RecordingStreamDescriptor,
        *,
        start_ms: int,
        end_ms: int,
        rate: PlaybackRate,
        width: int,
        height: int,
        pause_control: PlaybackPauseControl | None = None,
    ) -> OperatorPlaybackMetrics:
        if not isinstance(source, ResolvedLiveSource):
            raise OperatorPlaybackError(
                OperatorPlaybackErrorCode.PLAYBACK_FAILURE,
                "operator playback failed",
            )
        if not isinstance(descriptor, RecordingStreamDescriptor):
            raise OperatorPlaybackError(
                OperatorPlaybackErrorCode.PLAYBACK_FAILURE,
                "operator playback failed",
            )

        left_width = width // 2
        right_width = width - left_width
        try:
            layout = ViewportLayout(
                placements=(
                    ViewportPlacement(
                        logical_slot=0,
                        geometry=ViewportGeometry(x=0, y=0, width=left_width, height=height),
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
            live_delivery = self._live_delivery_factory(source.payload_type)
            if pause_control is None:
                playback_delivery = self._playback_factory(
                    recording_path,
                    descriptor,
                    start_ms,
                    end_ms,
                    rate,
                )
            else:
                playback_delivery = self._playback_factory(
                    recording_path,
                    descriptor,
                    start_ms,
                    end_ms,
                    rate,
                    pause_control=pause_control,
                )
            streams = (
                MixedLiveStream(slot=0, source_uri=source.source_uri, delivery=live_delivery),
                MixedPlaybackStream(slot=1, delivery=playback_delivery),
            )
            runtime: _WindowsOperatorBoundary = self._runtime_factory(layout)
        except Exception:
            raise OperatorPlaybackError(
                OperatorPlaybackErrorCode.PLAYBACK_FAILURE,
                "operator playback failed",
            ) from None

        primary_error: BaseException | None = None
        try:
            await runtime.start(streams)
            final = await runtime.wait()
            if final.state is not WindowsOperatorRuntimeState.COMPLETE:
                raise OperatorPlaybackError(
                    OperatorPlaybackErrorCode.PLAYBACK_FAILURE,
                    "operator playback failed",
                )
            return OperatorPlaybackMetrics(
                delivered_frames=final.delivered_frames,
                presentations=final.presentations,
                processed_controls=0,
                descriptor_verified=True,
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


def _load_recording_pair(root: Path, recording_id: UUID) -> tuple[Path, RecordingStreamDescriptor]:
    recording_path = root / f"{recording_id}.k5r"
    descriptor_path = root / f"{recording_id}.k5d"
    try:
        recording_stat = recording_path.lstat()
        descriptor_stat = descriptor_path.lstat()
    except FileNotFoundError:
        raise OperatorPlaybackError(
            OperatorPlaybackErrorCode.RECORDING_NOT_FOUND,
            "recording was not found",
        ) from None
    except OSError:
        raise OperatorPlaybackError(
            OperatorPlaybackErrorCode.RECORDING_INVALID,
            "recording metadata is unavailable",
        ) from None

    if not stat.S_ISREG(recording_stat.st_mode) or not stat.S_ISREG(descriptor_stat.st_mode):
        raise OperatorPlaybackError(
            OperatorPlaybackErrorCode.RECORDING_INVALID,
            "recording metadata is unavailable",
        )
    if not 1 <= descriptor_stat.st_size <= _MAX_DESCRIPTOR_BYTES:
        raise OperatorPlaybackError(
            OperatorPlaybackErrorCode.RECORDING_INVALID,
            "recording metadata is invalid",
        )

    try:
        descriptor = parse_recording_descriptor(descriptor_path.read_bytes())
    except (OSError, RecordingDescriptorError):
        raise OperatorPlaybackError(
            OperatorPlaybackErrorCode.RECORDING_INVALID,
            "recording metadata is invalid",
        ) from None

    if descriptor.recording_id != recording_id or recording_stat.st_size != descriptor.file_bytes:
        raise OperatorPlaybackError(
            OperatorPlaybackErrorCode.RECORDING_INVALID,
            "recording metadata is invalid",
        )
    return recording_path, descriptor


class BoundedOperatorPlaybackCoordinator:
    """Authorize one persisted recording and present it in the existing operator runtime."""

    def __init__(
        self,
        registry: DeviceRegistry,
        source_resolver: OperatorSourceResolver,
        recording_root: str | Path,
        playback_launcher: OperatorPlaybackLauncher,
        *,
        max_active_playbacks: int = 2,
    ) -> None:
        if not isinstance(registry, DeviceRegistry):
            raise TypeError("registry must be a DeviceRegistry")
        if not callable(getattr(source_resolver, "resolve", None)):
            raise TypeError("source_resolver must implement resolve")
        root_text = str(recording_root).strip()
        if not root_text:
            raise ValueError("recording_root is required")
        if not callable(getattr(playback_launcher, "run", None)):
            raise TypeError("playback_launcher must implement run")
        if not 1 <= max_active_playbacks <= _MAX_ACTIVE_PLAYBACKS:
            raise ValueError("max_active_playbacks must be between 1 and 8")

        self._registry = registry
        self._source_resolver = source_resolver
        self._recording_root = Path(recording_root).expanduser().resolve(strict=False)
        self._playback_launcher = playback_launcher
        self._max_active_playbacks = max_active_playbacks
        self._active_playbacks = 0
        self._controls: dict[UUID, tuple[UUID, UUID, PlaybackPauseControl]] = {}
        self._state_lock = asyncio.Lock()

    @property
    def active_playbacks(self) -> int:
        return self._active_playbacks

    async def _reserve(
        self,
        principal: UserAccount,
        request: OperatorPlaybackRequest,
    ) -> PlaybackPauseControl | None:
        async with self._state_lock:
            if self._active_playbacks >= self._max_active_playbacks:
                raise OperatorPlaybackError(
                    OperatorPlaybackErrorCode.PLAYBACK_BUSY,
                    "operator playback capacity is currently exhausted",
                )
            pause_control: PlaybackPauseControl | None = None
            if request.control_id is not None:
                if request.control_id in self._controls:
                    raise OperatorPlaybackError(
                        OperatorPlaybackErrorCode.CONTROL_CONFLICT,
                        "playback control identifier is already active",
                    )
                pause_control = PlaybackPauseControl()
                self._controls[request.control_id] = (
                    principal.id,
                    request.recording_id,
                    pause_control,
                )
            self._active_playbacks += 1
            return pause_control

    async def _release(self, control_id: UUID | None) -> None:
        async with self._state_lock:
            if control_id is not None:
                self._controls.pop(control_id, None)
            self._active_playbacks = max(0, self._active_playbacks - 1)

    @staticmethod
    def _control_receipt(
        control_id: UUID,
        snapshot: PlaybackControlSnapshot,
    ) -> OperatorPlaybackControlReceipt:
        return OperatorPlaybackControlReceipt(
            control_id=control_id,
            state=snapshot.state,
            pause_count=snapshot.pause_count,
            resume_count=snapshot.resume_count,
            paused_total_ms=snapshot.paused_total_ms,
        )

    async def control(
        self,
        principal: UserAccount,
        control_id: UUID,
        action: OperatorPlaybackControlAction,
    ) -> OperatorPlaybackControlReceipt:
        """Apply one bounded control to an active playback owned by the same principal."""
        self._validate_principal(principal)
        if not isinstance(control_id, UUID):
            raise TypeError("control_id must be a UUID")
        if not isinstance(action, OperatorPlaybackControlAction):
            raise TypeError("action must be an OperatorPlaybackControlAction")

        async with self._state_lock:
            active = self._controls.get(control_id)
            if active is None:
                raise OperatorPlaybackError(
                    OperatorPlaybackErrorCode.CONTROL_NOT_FOUND,
                    "active playback control was not found",
                )
            owner_id, _recording_id, pause_control = active
            if owner_id != principal.id:
                raise OperatorPlaybackError(
                    OperatorPlaybackErrorCode.CONTROL_FORBIDDEN,
                    "active playback control belongs to another principal",
                )
            if action is OperatorPlaybackControlAction.PAUSE:
                snapshot = await pause_control.pause()
            else:
                snapshot = await pause_control.resume()
            return self._control_receipt(control_id, snapshot)

    @staticmethod
    def _validate_principal(principal: UserAccount) -> None:
        if (
            not isinstance(principal, UserAccount)
            or not principal.enabled
            or principal.role not in {UserRole.VIEWER, UserRole.OPERATOR, UserRole.ADMINISTRATOR}
        ):
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
                "the recording source is not a supported video device",
            )

    @staticmethod
    def _validate_source(device: Device, source: ResolvedLiveSource) -> None:
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
                "resolved live source is outside the recorded device scope",
            )

    async def play(
        self,
        principal: UserAccount,
        request: OperatorPlaybackRequest,
    ) -> OperatorPlaybackReceipt:
        self._validate_principal(principal)
        if not isinstance(request, OperatorPlaybackRequest):
            raise TypeError("request must be an OperatorPlaybackRequest")

        recording_path, descriptor = await asyncio.to_thread(
            _load_recording_pair,
            self._recording_root,
            request.recording_id,
        )
        try:
            device = self._registry.get(descriptor.source_id)
        except DeviceRegistryStorageError:
            raise OperatorPlaybackError(
                OperatorPlaybackErrorCode.REGISTRY_UNAVAILABLE,
                "device registry is unavailable",
            ) from None
        if device is None:
            raise OperatorPlaybackError(
                OperatorPlaybackErrorCode.DEVICE_NOT_FOUND,
                "recorded device was not found",
            )
        self._validate_device(device)

        end_ms = descriptor.duration_ms if request.end_ms is None else request.end_ms
        if request.start_ms > end_ms or end_ms > descriptor.duration_ms:
            raise OperatorPlaybackError(
                OperatorPlaybackErrorCode.WINDOW_INVALID,
                "playback window is outside the recording duration",
            )

        pause_control = await self._reserve(principal, request)
        try:
            try:
                source = await self._source_resolver.resolve(device, request.stream_token)
            except OperatorLaunchError:
                raise OperatorPlaybackError(
                    OperatorPlaybackErrorCode.SOURCE_UNAVAILABLE,
                    "current live source could not be resolved",
                ) from None
            except Exception:
                raise OperatorPlaybackError(
                    OperatorPlaybackErrorCode.SOURCE_UNAVAILABLE,
                    "current live source could not be resolved",
                ) from None
            if not isinstance(source, ResolvedLiveSource):
                raise OperatorPlaybackError(
                    OperatorPlaybackErrorCode.SOURCE_UNAVAILABLE,
                    "current live source could not be resolved",
                )
            self._validate_source(device, source)

            try:
                if pause_control is None:
                    metrics = await self._playback_launcher.run(
                        source,
                        recording_path,
                        descriptor,
                        start_ms=request.start_ms,
                        end_ms=end_ms,
                        rate=request.rate,
                        width=request.width,
                        height=request.height,
                    )
                else:
                    metrics = await self._playback_launcher.run(
                        source,
                        recording_path,
                        descriptor,
                        start_ms=request.start_ms,
                        end_ms=end_ms,
                        rate=request.rate,
                        width=request.width,
                        height=request.height,
                        pause_control=pause_control,
                    )
            except OperatorPlaybackError:
                raise
            except Exception:
                raise OperatorPlaybackError(
                    OperatorPlaybackErrorCode.PLAYBACK_FAILURE,
                    "operator playback failed",
                ) from None
            if not isinstance(metrics, OperatorPlaybackMetrics) or not metrics.descriptor_verified:
                raise OperatorPlaybackError(
                    OperatorPlaybackErrorCode.PLAYBACK_FAILURE,
                    "operator playback did not verify the recording",
                )
            return OperatorPlaybackReceipt(
                recording_id=request.recording_id,
                delivered_frames=metrics.delivered_frames,
                presentations=metrics.presentations,
                processed_controls=metrics.processed_controls,
                descriptor_verified=True,
            )
        finally:
            await self._release(request.control_id)
