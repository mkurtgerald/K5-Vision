"""Bounded authenticated export of validated Stage-One recording pairs."""

from __future__ import annotations

import asyncio
import enum
import os
import stat
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from k5vision.domain.users import UserAccount
from k5vision.media.framed_recording import (
    FramedRecordingError,
    FramedRecordingReader,
    FramedRecordingState,
)
from k5vision.media.recording_descriptor import RecordingStreamDescriptor
from k5vision.operator_playback import (
    BoundedOperatorPlaybackCoordinator,
    OperatorPlaybackError,
    OperatorPlaybackErrorCode,
    _load_recording_pair,
)
from k5vision.services.device_registry import DeviceRegistry, DeviceRegistryStorageError

_DEFAULT_MAX_EXPORT_BYTES = 64 * 1024 * 1024
_MAX_EXPORT_BYTES = 512 * 1024 * 1024
_MAX_ACTIVE_EXPORTS = 8
_EXPORT_CHUNK_BYTES = 64 * 1024

FileIdentity = tuple[int, int, int, int]


class OperatorExportErrorCode(enum.StrEnum):
    EXPORT_TOO_LARGE = "export_too_large"
    EXPORT_BUSY = "export_busy"
    EXPORT_INVALID = "export_invalid"


class OperatorExportError(RuntimeError):
    """Sanitized export failure without source, path, credential, or media details."""

    def __init__(self, code: OperatorExportErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class OperatorExportHandle:
    """Execution-private export lease; never serialize this object."""

    recording_id: UUID
    recording_path: Path
    descriptor_bytes: bytes
    file_bytes: int
    file_identity: FileIdentity
    boundary: str
    response_bytes: int
    _reservation: object = field(repr=False, compare=False)


def _identity_from_stat(result: os.stat_result) -> FileIdentity:
    return (
        int(result.st_dev),
        int(result.st_ino),
        int(result.st_size),
        int(result.st_mtime_ns),
    )


def _multipart_sections(
    recording_id: UUID,
    boundary: str,
    descriptor_bytes: bytes,
    file_bytes: int,
) -> tuple[bytes, bytes, bytes]:
    descriptor_header = (
        f"--{boundary}\r\n"
        "Content-Type: application/json\r\n"
        f'Content-Disposition: attachment; filename="{recording_id}.k5d"\r\n'
        "\r\n"
    ).encode("ascii")
    recording_header = (
        f"\r\n--{boundary}\r\n"
        "Content-Type: application/vnd.k5.recording\r\n"
        f'Content-Disposition: attachment; filename="{recording_id}.k5r"\r\n'
        f"Content-Length: {file_bytes}\r\n"
        "\r\n"
    ).encode("ascii")
    footer = f"\r\n--{boundary}--\r\n".encode("ascii")
    return descriptor_header, recording_header, footer


def _verify_recording_for_export(
    recording_path: Path,
    descriptor: RecordingStreamDescriptor,
) -> FileIdentity:
    try:
        before = recording_path.lstat()
    except OSError:
        raise OperatorExportError(
            OperatorExportErrorCode.EXPORT_INVALID,
            "recording export could not be validated",
        ) from None
    if not stat.S_ISREG(before.st_mode) or int(before.st_size) != descriptor.file_bytes:
        raise OperatorExportError(
            OperatorExportErrorCode.EXPORT_INVALID,
            "recording export could not be validated",
        )

    reader = FramedRecordingReader(
        recording_path,
        max_packets=max(1, descriptor.packet_count),
        max_payload_bytes=max(1, descriptor.payload_bytes),
    )
    try:
        for _packet in reader.iter_packets():
            pass
    except FramedRecordingError:
        raise OperatorExportError(
            OperatorExportErrorCode.EXPORT_INVALID,
            "recording export could not be validated",
        ) from None

    snapshot = reader.snapshot
    if (
        snapshot.state != FramedRecordingState.COMPLETE
        or snapshot.packets != descriptor.packet_count
        or snapshot.payload_bytes != descriptor.payload_bytes
        or snapshot.file_bytes != descriptor.file_bytes
    ):
        raise OperatorExportError(
            OperatorExportErrorCode.EXPORT_INVALID,
            "recording export could not be validated",
        )

    try:
        after = recording_path.lstat()
    except OSError:
        raise OperatorExportError(
            OperatorExportErrorCode.EXPORT_INVALID,
            "recording export could not be validated",
        ) from None
    if not stat.S_ISREG(after.st_mode) or _identity_from_stat(before) != _identity_from_stat(after):
        raise OperatorExportError(
            OperatorExportErrorCode.EXPORT_INVALID,
            "recording export changed during validation",
        )
    return _identity_from_stat(after)


class BoundedOperatorExportCoordinator:
    """Authorize, verify, capacity-bound, and stream one recording export."""

    def __init__(
        self,
        registry: DeviceRegistry,
        recording_root: str | Path,
        *,
        max_export_bytes: int = _DEFAULT_MAX_EXPORT_BYTES,
        max_active_exports: int = 2,
    ) -> None:
        if not isinstance(registry, DeviceRegistry):
            raise TypeError("registry must be a DeviceRegistry")
        root_text = str(recording_root).strip()
        if not root_text:
            raise ValueError("recording_root is required")
        if not 1 <= max_export_bytes <= _MAX_EXPORT_BYTES:
            raise ValueError("max_export_bytes must be between 1 and 536870912")
        if not 1 <= max_active_exports <= _MAX_ACTIVE_EXPORTS:
            raise ValueError("max_active_exports must be between 1 and 8")

        self._registry = registry
        self._recording_root = Path(recording_root).expanduser().resolve(strict=False)
        self._max_export_bytes = max_export_bytes
        self._max_active_exports = max_active_exports
        self._reservations: set[object] = set()
        self._state_lock = asyncio.Lock()

    @property
    def active_exports(self) -> int:
        return len(self._reservations)

    async def _reserve(self) -> object:
        async with self._state_lock:
            if len(self._reservations) >= self._max_active_exports:
                raise OperatorExportError(
                    OperatorExportErrorCode.EXPORT_BUSY,
                    "operator export capacity is currently exhausted",
                )
            reservation = object()
            self._reservations.add(reservation)
            return reservation

    def release(self, handle: OperatorExportHandle) -> None:
        """Release only this lease, once, without a cancellation checkpoint.

        Reservation mutations are confined to the coordinator's event loop and
        never suspend. A late body finalizer cannot release another response's slot.
        """
        self._reservations.discard(handle._reservation)

    def _validation_finished(self, reservation: object, worker: asyncio.Task) -> None:
        self._reservations.discard(reservation)
        if not worker.cancelled():
            worker.exception()  # Retrieve a failure after the request was cancelled.

    async def begin_export(
        self,
        principal: UserAccount,
        recording_id: UUID,
    ) -> OperatorExportHandle:
        """Return one verified execution-private lease for a bounded export response."""
        BoundedOperatorPlaybackCoordinator._validate_principal(principal)
        if not isinstance(recording_id, UUID):
            raise TypeError("recording_id must be a UUID")

        recording_path, descriptor = await asyncio.to_thread(
            _load_recording_pair,
            self._recording_root,
            recording_id,
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
        BoundedOperatorPlaybackCoordinator._validate_device(device)

        descriptor_bytes = descriptor.to_json_bytes()
        boundary = f"k5-{recording_id.hex}"
        descriptor_header, recording_header, footer = _multipart_sections(
            recording_id,
            boundary,
            descriptor_bytes,
            descriptor.file_bytes,
        )
        response_bytes = (
            len(descriptor_header)
            + len(descriptor_bytes)
            + len(recording_header)
            + descriptor.file_bytes
            + len(footer)
        )
        if response_bytes > self._max_export_bytes:
            raise OperatorExportError(
                OperatorExportErrorCode.EXPORT_TOO_LARGE,
                "recording export exceeds the configured byte limit",
            )

        reservation = await self._reserve()
        worker: asyncio.Task | None = None
        try:
            validation = asyncio.to_thread(_verify_recording_for_export, recording_path, descriptor)
            try:
                worker = asyncio.create_task(validation)
            except BaseException:
                validation.close()
                raise
            identity = await asyncio.shield(worker)
            return OperatorExportHandle(
                recording_id=recording_id,
                recording_path=recording_path,
                descriptor_bytes=descriptor_bytes,
                file_bytes=descriptor.file_bytes,
                file_identity=identity,
                boundary=boundary,
                response_bytes=response_bytes,
                _reservation=reservation,
            )
        except BaseException as exc:
            if worker is not None and not worker.done():
                # Cancellation cannot stop to_thread validation. Keep its capacity
                # reserved until it settles, even if the request is cancelled again.
                worker.add_done_callback(
                    lambda finished: self._validation_finished(reservation, finished)
                )
            else:
                self._reservations.discard(reservation)
            if isinstance(exc, (asyncio.CancelledError, OperatorExportError)):
                raise
            if not isinstance(exc, Exception):
                raise
            raise OperatorExportError(
                OperatorExportErrorCode.EXPORT_INVALID,
                "recording export could not be validated",
            ) from None

    async def stream(self, handle: OperatorExportHandle) -> AsyncGenerator[bytes, None]:
        """Stream the canonical descriptor and immutable recording without retaining a copy."""
        if not isinstance(handle, OperatorExportHandle):
            raise TypeError("handle must be an OperatorExportHandle")
        descriptor_header, recording_header, footer = _multipart_sections(
            handle.recording_id,
            handle.boundary,
            handle.descriptor_bytes,
            handle.file_bytes,
        )
        try:
            yield descriptor_header
            yield handle.descriptor_bytes
            yield recording_header

            try:
                current = handle.recording_path.lstat()
                if (
                    not stat.S_ISREG(current.st_mode)
                    or _identity_from_stat(current) != handle.file_identity
                ):
                    raise OSError
                file = handle.recording_path.open("rb", buffering=0)
            except OSError:
                raise RuntimeError("recording export became unavailable") from None

            try:
                if _identity_from_stat(os.fstat(file.fileno())) != handle.file_identity:
                    raise RuntimeError("recording export became unavailable")
                remaining = handle.file_bytes
                while remaining:
                    chunk = file.read(min(_EXPORT_CHUNK_BYTES, remaining))
                    if not chunk:
                        raise RuntimeError("recording export became unavailable")
                    remaining -= len(chunk)
                    yield chunk
                    await asyncio.sleep(0)
                if file.read(1):
                    raise RuntimeError("recording export became unavailable")
            finally:
                file.close()

            yield footer
        finally:
            self.release(handle)
