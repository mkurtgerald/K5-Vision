"""Authenticated bounded recording for the Stage-One operator workflow.

This boundary reuses the enrolled-device authority, private operator source resolver,
ephemeral RTP delivery, framed recording sink, and replay descriptor contracts. Public
receipts contain only opaque recording identity and bounded counters; source URIs,
credentials, filesystem paths, and media payloads remain execution-private.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from k5vision.domain.devices import Device, DeviceKind, DeviceProtocol
from k5vision.domain.users import UserAccount, UserRole
from k5vision.media.framed_recording import FramedAtomicRecordingSink, FramedRecordingState
from k5vision.media.recording import BoundedRtpRecorder, RecordingState
from k5vision.media.recording_descriptor import RecordingStreamDescriptor, VideoCodec
from k5vision.media.rtp_delivery import EphemeralRtpDelivery
from k5vision.operator_launch import (
    OperatorLaunchError,
    OperatorSourceResolver,
    ResolvedLiveSource,
    StreamToken,
)
from k5vision.services.device_registry import DeviceRegistry, DeviceRegistryStorageError

STAGE_ONE_RECORDING_ROOT_ENV = "K5_STAGE_ONE_RECORDING_ROOT"
_MAX_ACTIVE_RECORDINGS = 8
_MAX_PACKET_GOAL = 65_536
_MAX_RECORDING_BYTES = 512 * 1024 * 1024
_MAX_SEGMENTS = 32
_CLOCK_RATE_HZ = 90_000


class OperatorRecordingErrorCode(StrEnum):
    UNAUTHORIZED = "unauthorized"
    INSUFFICIENT_PERMISSION = "insufficient_permission"
    DEVICE_NOT_FOUND = "device_not_found"
    UNSUPPORTED_DEVICE = "unsupported_device"
    SOURCE_UNAVAILABLE = "source_unavailable"
    SOURCE_SCOPE_MISMATCH = "source_scope_mismatch"
    RECORDING_BUSY = "recording_busy"
    RECORDING_FAILURE = "recording_failure"
    REGISTRY_UNAVAILABLE = "registry_unavailable"


class OperatorRecordingError(RuntimeError):
    """Sanitized recording failure without source, credential, path, or payload detail."""

    def __init__(self, code: OperatorRecordingErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class OperatorRecordingRequest(BaseModel):
    """Credential-free request for one bounded recording."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    device_id: UUID
    stream_token: StreamToken


class OperatorRecordingReceipt(BaseModel):
    """Source-free result for one finalized replay-ready recording."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1"
    recording_id: UUID
    packet_count: int = Field(ge=1, le=_MAX_PACKET_GOAL)
    payload_bytes: int = Field(ge=12, le=_MAX_RECORDING_BYTES)
    duration_ms: int = Field(ge=0, le=7 * 24 * 60 * 60 * 1000)


class OperatorRecordingSegmentReceipt(BaseModel):
    """Source-free receipt for one finalized segment in a continuous session."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1"
    recording_id: UUID
    packet_count: int = Field(ge=1, le=_MAX_PACKET_GOAL)
    payload_bytes: int = Field(ge=12, le=_MAX_RECORDING_BYTES)
    duration_ms: int = Field(ge=0, le=7 * 24 * 60 * 60 * 1000)


class OperatorRecordingSessionReceipt(BaseModel):
    """Bounded aggregate result for one continuous rotating recording session."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1"
    session_id: UUID
    segments: tuple[OperatorRecordingSegmentReceipt, ...]
    segment_count: int = Field(ge=1, le=_MAX_SEGMENTS)
    packet_count: int = Field(ge=1, le=_MAX_PACKET_GOAL)
    payload_bytes: int = Field(ge=12)
    duration_ms: int = Field(ge=0, le=7 * 24 * 60 * 60 * 1000)


class _RtpDeliveryBoundary(Protocol):
    async def deliver(
        self,
        source_uri: str,
        consumer: Callable[[memoryview], Awaitable[None]],
    ) -> object: ...


DeliveryFactory = Callable[[], _RtpDeliveryBoundary]


def _default_delivery_factory() -> _RtpDeliveryBoundary:
    return EphemeralRtpDelivery(
        packet_goal=2048,
        delivery_timeout_seconds=30.0,
        consumer_timeout_seconds=3.0,
        relay_startup_probe_seconds=0.5,
    )


def _millisecond_utc_now() -> datetime:
    now = datetime.now(UTC)
    return now.replace(microsecond=(now.microsecond // 1000) * 1000)


def _rtp_timestamp(packet: memoryview) -> int:
    return int.from_bytes(packet[4:8], "big")


def _write_descriptor_sync(
    root: Path, recording_id: UUID, descriptor: RecordingStreamDescriptor
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    final_path = root / f"{recording_id}.k5d"
    stage_path = root / f"{recording_id}.k5d.stage"
    if final_path.exists() or stage_path.exists():
        raise FileExistsError
    payload = descriptor.to_json_bytes()
    try:
        with stage_path.open("xb") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.replace(stage_path, final_path)
    except BaseException:
        try:
            stage_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _delete_recording_pair_best_effort(root: Path, recording_id: UUID) -> None:
    for suffix in (".k5r", ".k5d", ".k5d.stage"):
        try:
            (root / f"{recording_id}{suffix}").unlink(missing_ok=True)
        except OSError:
            pass


class BoundedOperatorRecordingCoordinator:
    """Authorize one enrolled source and create bounded replay-ready local recordings."""

    def __init__(
        self,
        registry: DeviceRegistry,
        source_resolver: OperatorSourceResolver,
        recording_root: str | Path,
        *,
        delivery_factory: DeliveryFactory = _default_delivery_factory,
        packet_goal: int = 2048,
        max_recording_bytes: int = _MAX_RECORDING_BYTES,
        max_active_recordings: int = 2,
        segment_packet_goal: int | None = None,
        max_segments: int = 8,
    ) -> None:
        if not isinstance(registry, DeviceRegistry):
            raise TypeError("registry must be a DeviceRegistry")
        if not callable(getattr(source_resolver, "resolve", None)):
            raise TypeError("source_resolver must implement resolve")
        root_text = str(recording_root).strip()
        if not root_text:
            raise ValueError("recording_root is required")
        if not callable(delivery_factory):
            raise TypeError("delivery_factory must be callable")
        if not 1 <= packet_goal <= _MAX_PACKET_GOAL:
            raise ValueError(f"packet_goal must be between 1 and {_MAX_PACKET_GOAL}")
        if not 1 <= max_recording_bytes <= _MAX_RECORDING_BYTES:
            raise ValueError(f"max_recording_bytes must be between 1 and {_MAX_RECORDING_BYTES}")
        if not 1 <= max_active_recordings <= _MAX_ACTIVE_RECORDINGS:
            raise ValueError(
                f"max_active_recordings must be between 1 and {_MAX_ACTIVE_RECORDINGS}"
            )
        if not 1 <= max_segments <= _MAX_SEGMENTS:
            raise ValueError(f"max_segments must be between 1 and {_MAX_SEGMENTS}")
        resolved_segment_goal = segment_packet_goal
        if resolved_segment_goal is None:
            resolved_segment_goal = max(1, (packet_goal + max_segments - 1) // max_segments)
        if not 1 <= resolved_segment_goal <= packet_goal:
            raise ValueError("segment_packet_goal must be between 1 and packet_goal")

        self._registry = registry
        self._source_resolver = source_resolver
        self._recording_root = Path(recording_root).expanduser().resolve(strict=False)
        self._delivery_factory = delivery_factory
        self._packet_goal = packet_goal
        self._max_recording_bytes = max_recording_bytes
        self._max_active_recordings = max_active_recordings
        self._segment_packet_goal = resolved_segment_goal
        self._max_segments = max_segments
        self._active_recordings = 0
        self._state_lock = asyncio.Lock()

    @property
    def active_recordings(self) -> int:
        return self._active_recordings

    async def _reserve(self) -> None:
        async with self._state_lock:
            if self._active_recordings >= self._max_active_recordings:
                raise OperatorRecordingError(
                    OperatorRecordingErrorCode.RECORDING_BUSY,
                    "operator recording capacity is currently exhausted",
                )
            self._active_recordings += 1

    async def _release(self) -> None:
        async with self._state_lock:
            self._active_recordings = max(0, self._active_recordings - 1)

    @staticmethod
    def _validate_principal(principal: UserAccount) -> None:
        if not isinstance(principal, UserAccount) or not principal.enabled:
            raise OperatorRecordingError(
                OperatorRecordingErrorCode.UNAUTHORIZED,
                "an enabled human session is required",
            )
        if principal.role not in {UserRole.OPERATOR, UserRole.ADMINISTRATOR}:
            raise OperatorRecordingError(
                OperatorRecordingErrorCode.INSUFFICIENT_PERMISSION,
                "operator recording requires operator permission",
            )

    @staticmethod
    def _validate_device(device: Device) -> None:
        video_kinds = {DeviceKind.CAMERA, DeviceKind.BODY_CAMERA, DeviceKind.ENCODER}
        video_protocols = {DeviceProtocol.ONVIF, DeviceProtocol.RTSP}
        if device.kind not in video_kinds or not device.protocols.intersection(video_protocols):
            raise OperatorRecordingError(
                OperatorRecordingErrorCode.UNSUPPORTED_DEVICE,
                "the selected device does not expose a supported video capability",
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
            raise OperatorRecordingError(
                OperatorRecordingErrorCode.SOURCE_SCOPE_MISMATCH,
                "resolved recording source is outside the selected device scope",
            )

    def _load_device(self, device_id: UUID) -> Device:
        try:
            device = self._registry.get(device_id)
        except DeviceRegistryStorageError:
            raise OperatorRecordingError(
                OperatorRecordingErrorCode.REGISTRY_UNAVAILABLE,
                "device registry is unavailable",
            ) from None
        if device is None:
            raise OperatorRecordingError(
                OperatorRecordingErrorCode.DEVICE_NOT_FOUND,
                "selected device was not found",
            )
        self._validate_device(device)
        return device

    async def _resolve_source(
        self, device: Device, stream_token: StreamToken
    ) -> ResolvedLiveSource:
        try:
            source = await self._source_resolver.resolve(device, stream_token)
        except OperatorLaunchError:
            raise OperatorRecordingError(
                OperatorRecordingErrorCode.SOURCE_UNAVAILABLE,
                "selected recording source could not be resolved",
            ) from None
        except Exception:
            raise OperatorRecordingError(
                OperatorRecordingErrorCode.SOURCE_UNAVAILABLE,
                "selected recording source could not be resolved",
            ) from None
        if not isinstance(source, ResolvedLiveSource):
            raise OperatorRecordingError(
                OperatorRecordingErrorCode.SOURCE_UNAVAILABLE,
                "selected recording source could not be resolved",
            )
        self._validate_source(device, source)
        return source

    async def record(
        self,
        principal: UserAccount,
        request: OperatorRecordingRequest,
    ) -> OperatorRecordingReceipt:
        self._validate_principal(principal)
        if not isinstance(request, OperatorRecordingRequest):
            raise TypeError("request must be an OperatorRecordingRequest")

        device = self._load_device(request.device_id)
        await self._reserve()
        recording_id = uuid4()
        recorder: BoundedRtpRecorder | None = None
        sink: FramedAtomicRecordingSink | None = None
        finalized = False
        try:
            source = await self._resolve_source(device, request.stream_token)
            sink = FramedAtomicRecordingSink(
                self._recording_root,
                str(recording_id),
                max_packets=self._packet_goal,
                max_payload_bytes=self._max_recording_bytes,
            )
            recorder = BoundedRtpRecorder(
                sink,
                max_packets=self._packet_goal,
                max_bytes=self._max_recording_bytes,
                operation_timeout_seconds=3.0,
            )
            delivery = self._delivery_factory()
            first_timestamp: int | None = None
            last_timestamp: int | None = None

            await recorder.start()

            async def consume(packet: memoryview) -> None:
                nonlocal first_timestamp, last_timestamp
                if int(packet[1] & 0x7F) != source.payload_type:
                    raise OperatorRecordingError(
                        OperatorRecordingErrorCode.RECORDING_FAILURE,
                        "recording RTP payload type changed unexpectedly",
                    )
                timestamp = _rtp_timestamp(packet)
                if first_timestamp is None:
                    first_timestamp = timestamp
                last_timestamp = timestamp
                await recorder.consume(packet)

            try:
                await delivery.deliver(source.source_uri, consume)
                final = await recorder.finalize()
            except OperatorRecordingError:
                raise
            except Exception:
                raise OperatorRecordingError(
                    OperatorRecordingErrorCode.RECORDING_FAILURE,
                    "operator recording failed",
                ) from None

            if (
                final.state is not RecordingState.FINALIZED
                or sink.snapshot.state is not FramedRecordingState.FINALIZED
                or first_timestamp is None
                or last_timestamp is None
            ):
                raise OperatorRecordingError(
                    OperatorRecordingErrorCode.RECORDING_FAILURE,
                    "operator recording did not finalize",
                )

            counters = sink.snapshot
            duration_ms = (
                ((last_timestamp - first_timestamp) & 0xFFFFFFFF) * 1000
            ) // _CLOCK_RATE_HZ
            started = _millisecond_utc_now()
            descriptor = RecordingStreamDescriptor(
                recording_id=recording_id,
                source_id=device.id,
                codec=VideoCodec.H264,
                payload_type=source.payload_type,
                clock_rate_hz=_CLOCK_RATE_HZ,
                started_at_utc=started,
                ended_at_utc=started + timedelta(milliseconds=duration_ms),
                duration_ms=duration_ms,
                rtp_timestamp_origin=first_timestamp,
                packet_count=counters.packets,
                payload_bytes=counters.payload_bytes,
                file_bytes=counters.file_bytes,
            )
            try:
                await asyncio.to_thread(
                    _write_descriptor_sync,
                    self._recording_root,
                    recording_id,
                    descriptor,
                )
            except Exception:
                raise OperatorRecordingError(
                    OperatorRecordingErrorCode.RECORDING_FAILURE,
                    "recording descriptor could not be persisted",
                ) from None

            finalized = True
            return OperatorRecordingReceipt(
                recording_id=recording_id,
                packet_count=counters.packets,
                payload_bytes=counters.payload_bytes,
                duration_ms=duration_ms,
            )
        finally:
            if not finalized:
                if recorder is not None and recorder.snapshot.state not in {
                    RecordingState.FINALIZED,
                    RecordingState.ABORTED,
                }:
                    try:
                        await recorder.abort()
                    except Exception:
                        pass
                await asyncio.to_thread(
                    _delete_recording_pair_best_effort,
                    self._recording_root,
                    recording_id,
                )
            await self._release()

    async def record_continuous(
        self,
        principal: UserAccount,
        request: OperatorRecordingRequest,
    ) -> OperatorRecordingSessionReceipt:
        """Record one bounded delivery while atomically rotating finalized K5 segments."""
        self._validate_principal(principal)
        if not isinstance(request, OperatorRecordingRequest):
            raise TypeError("request must be an OperatorRecordingRequest")

        device = self._load_device(request.device_id)
        await self._reserve()
        session_id = uuid4()
        segment_receipts: list[OperatorRecordingSegmentReceipt] = []
        recorder: BoundedRtpRecorder | None = None
        sink: FramedAtomicRecordingSink | None = None
        recording_id: UUID | None = None
        first_timestamp: int | None = None
        last_timestamp: int | None = None
        session_timestamp_origin: int | None = None
        session_started_at: datetime | None = None
        total_packets = 0
        total_payload_bytes = 0

        async def abort_active_segment() -> None:
            nonlocal recorder, sink, recording_id
            if recorder is not None and recorder.snapshot.state not in {
                RecordingState.FINALIZED,
                RecordingState.ABORTED,
            }:
                try:
                    await recorder.abort()
                except Exception:
                    pass
            if recording_id is not None:
                await asyncio.to_thread(
                    _delete_recording_pair_best_effort,
                    self._recording_root,
                    recording_id,
                )
            recorder = None
            sink = None
            recording_id = None

        async def start_segment(timestamp: int) -> None:
            nonlocal recorder, sink, recording_id, first_timestamp, last_timestamp
            nonlocal session_timestamp_origin, session_started_at
            if len(segment_receipts) >= self._max_segments:
                raise OperatorRecordingError(
                    OperatorRecordingErrorCode.RECORDING_FAILURE,
                    "continuous recording segment capacity was exhausted",
                )
            recording_id = uuid4()
            sink = FramedAtomicRecordingSink(
                self._recording_root,
                str(recording_id),
                max_packets=self._segment_packet_goal,
                max_payload_bytes=self._max_recording_bytes,
            )
            recorder = BoundedRtpRecorder(
                sink,
                max_packets=self._segment_packet_goal,
                max_bytes=self._max_recording_bytes,
                operation_timeout_seconds=3.0,
            )
            await recorder.start()
            first_timestamp = timestamp
            last_timestamp = timestamp
            if session_timestamp_origin is None:
                session_timestamp_origin = timestamp
                session_started_at = _millisecond_utc_now()

        async def finalize_segment() -> None:
            nonlocal recorder, sink, recording_id, first_timestamp, last_timestamp
            if recorder is None or sink is None or recording_id is None:
                return
            if first_timestamp is None or last_timestamp is None:
                raise OperatorRecordingError(
                    OperatorRecordingErrorCode.RECORDING_FAILURE,
                    "continuous recording segment had no media",
                )
            try:
                final = await recorder.finalize()
                if (
                    final.state is not RecordingState.FINALIZED
                    or sink.snapshot.state is not FramedRecordingState.FINALIZED
                ):
                    raise OperatorRecordingError(
                        OperatorRecordingErrorCode.RECORDING_FAILURE,
                        "continuous recording segment did not finalize",
                    )
                counters = sink.snapshot
                duration_ms = (
                    ((last_timestamp - first_timestamp) & 0xFFFFFFFF) * 1000
                ) // _CLOCK_RATE_HZ
                assert session_timestamp_origin is not None
                assert session_started_at is not None
                segment_offset_ms = (
                    ((first_timestamp - session_timestamp_origin) & 0xFFFFFFFF) * 1000
                ) // _CLOCK_RATE_HZ
                started = session_started_at + timedelta(milliseconds=segment_offset_ms)
                descriptor = RecordingStreamDescriptor(
                    recording_id=recording_id,
                    source_id=device.id,
                    codec=VideoCodec.H264,
                    payload_type=source.payload_type,
                    clock_rate_hz=_CLOCK_RATE_HZ,
                    started_at_utc=started,
                    ended_at_utc=started + timedelta(milliseconds=duration_ms),
                    duration_ms=duration_ms,
                    rtp_timestamp_origin=first_timestamp,
                    packet_count=counters.packets,
                    payload_bytes=counters.payload_bytes,
                    file_bytes=counters.file_bytes,
                )
                await asyncio.to_thread(
                    _write_descriptor_sync,
                    self._recording_root,
                    recording_id,
                    descriptor,
                )
                segment_receipts.append(
                    OperatorRecordingSegmentReceipt(
                        recording_id=recording_id,
                        packet_count=counters.packets,
                        payload_bytes=counters.payload_bytes,
                        duration_ms=duration_ms,
                    )
                )
            except OperatorRecordingError:
                await abort_active_segment()
                raise
            except Exception:
                await abort_active_segment()
                raise OperatorRecordingError(
                    OperatorRecordingErrorCode.RECORDING_FAILURE,
                    "continuous recording segment could not be finalized",
                ) from None
            recorder = None
            sink = None
            recording_id = None
            first_timestamp = None
            last_timestamp = None

        try:
            source = await self._resolve_source(device, request.stream_token)
            delivery = self._delivery_factory()

            async def consume(packet: memoryview) -> None:
                nonlocal last_timestamp, total_packets, total_payload_bytes
                if int(packet[1] & 0x7F) != source.payload_type:
                    raise OperatorRecordingError(
                        OperatorRecordingErrorCode.RECORDING_FAILURE,
                        "recording RTP payload type changed unexpectedly",
                    )
                if total_packets >= self._packet_goal:
                    raise OperatorRecordingError(
                        OperatorRecordingErrorCode.RECORDING_FAILURE,
                        "continuous recording packet budget was exhausted",
                    )
                timestamp = _rtp_timestamp(packet)
                if recorder is None:
                    await start_segment(timestamp)
                assert recorder is not None
                assert sink is not None
                await recorder.consume(packet)
                last_timestamp = timestamp
                total_packets += 1
                total_payload_bytes += len(packet)
                if sink.snapshot.packets >= self._segment_packet_goal:
                    await finalize_segment()

            try:
                await delivery.deliver(source.source_uri, consume)
                await finalize_segment()
            except OperatorRecordingError:
                raise
            except Exception:
                raise OperatorRecordingError(
                    OperatorRecordingErrorCode.RECORDING_FAILURE,
                    "continuous operator recording failed",
                ) from None

            if not segment_receipts or total_packets < 1:
                raise OperatorRecordingError(
                    OperatorRecordingErrorCode.RECORDING_FAILURE,
                    "continuous operator recording produced no finalized media",
                )
            total_duration_ms = sum(segment.duration_ms for segment in segment_receipts)
            return OperatorRecordingSessionReceipt(
                session_id=session_id,
                segments=tuple(segment_receipts),
                segment_count=len(segment_receipts),
                packet_count=total_packets,
                payload_bytes=total_payload_bytes,
                duration_ms=total_duration_ms,
            )
        finally:
            if recorder is not None or recording_id is not None:
                await abort_active_segment()
            await self._release()
