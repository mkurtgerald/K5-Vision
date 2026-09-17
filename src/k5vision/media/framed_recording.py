"""Versioned replay-ready local recording format.

Stage 09 adds deterministic packet framing and integrity checks above the accepted
recording-ingest boundary. Paths and payloads are intentionally excluded from
observable snapshots and exception text.
"""

from __future__ import annotations

import asyncio
import enum
import os
import pathlib
import re
import struct
import typing
import zlib

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.rtp_delivery import is_rtp_v2

_MAGIC = b"K5RTPF\x00\x01"
_RECORD_HEADER = struct.Struct(">II")
_RECORDING_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_PACKET_BYTES = 65_535
_MAX_RECORDING_BYTES = 8 * 1024 * 1024 * 1024


class FramedRecordingState(enum.StrEnum):
    CREATED = "created"
    OPEN = "open"
    FINALIZED = "finalized"
    COMPLETE = "complete"
    ABORTED = "aborted"
    FAILED = "failed"


class FramedRecordingErrorCode(enum.StrEnum):
    INVALID_RECORDING_ID = "invalid_recording_id"
    INVALID_STATE = "invalid_state"
    CONFLICT = "conflict"
    LIMIT_EXCEEDED = "limit_exceeded"
    IO_FAILURE = "io_failure"
    INVALID_HEADER = "invalid_header"
    INVALID_RECORD = "invalid_record"
    TRUNCATED_RECORD = "truncated_record"
    INTEGRITY_FAILURE = "integrity_failure"


class FramedRecordingError(RuntimeError):
    """Sanitized recording-format failure."""

    def __init__(self, code: FramedRecordingErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class FramedRecordingSnapshot(BaseModel):
    """Path-free framed-recording observability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    state: FramedRecordingState
    packets: int = Field(ge=0)
    payload_bytes: int = Field(ge=0)
    file_bytes: int = Field(ge=0)


class FramedAtomicRecordingSink:
    """Persist RTP packets with deterministic framing and per-record integrity."""

    def __init__(
        self,
        root: pathlib.Path,
        recording_id: str,
        *,
        max_packets: int = 1_000_000,
        max_payload_bytes: int = _MAX_RECORDING_BYTES,
        max_packet_bytes: int = _MAX_PACKET_BYTES,
    ) -> None:
        if not _RECORDING_ID.fullmatch(recording_id) or recording_id in {".", ".."}:
            raise FramedRecordingError(
                FramedRecordingErrorCode.INVALID_RECORDING_ID,
                "recording identifier is invalid",
            )
        if not 1 <= max_packets <= 1_000_000:
            raise ValueError("max_packets must be between 1 and 1000000")
        if not 1 <= max_payload_bytes <= _MAX_RECORDING_BYTES:
            raise ValueError("max_payload_bytes must be between 1 and 8589934592")
        if not 12 <= max_packet_bytes <= _MAX_PACKET_BYTES:
            raise ValueError("max_packet_bytes must be between 12 and 65535")

        self._root = pathlib.Path(root).expanduser().resolve(strict=False)
        self._recording_id = recording_id
        self._max_packets = max_packets
        self._max_payload_bytes = max_payload_bytes
        self._max_packet_bytes = max_packet_bytes
        self._part_path = self._root / f".{recording_id}.k5r.part"
        self._final_path = self._root / f"{recording_id}.k5r"
        self._file: typing.BinaryIO | None = None
        self._state = FramedRecordingState.CREATED
        self._packets = 0
        self._payload_bytes = 0
        self._file_bytes = 0

    @property
    def snapshot(self) -> FramedRecordingSnapshot:
        return FramedRecordingSnapshot(
            state=self._state,
            packets=self._packets,
            payload_bytes=self._payload_bytes,
            file_bytes=self._file_bytes,
        )

    def _write_all_sync(self, data: bytes | memoryview) -> None:
        if self._file is None:
            raise OSError
        view = memoryview(data)
        while view:
            written = self._file.write(view)
            if written is None or written <= 0:
                raise OSError
            view = view[written:]

    def _open_sync(self) -> typing.BinaryIO:
        self._root.mkdir(parents=True, exist_ok=True)
        if self._final_path.exists():
            raise FileExistsError
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        fd = os.open(self._part_path, flags, 0o600)
        file: typing.BinaryIO | None = None
        try:
            file = os.fdopen(fd, "wb", buffering=0)
            self._file = file
            self._write_all_sync(_MAGIC)
            self._file_bytes = len(_MAGIC)
            return file
        except BaseException:
            if file is not None:
                try:
                    file.close()
                except OSError:
                    pass
            else:
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._file = None
            try:
                self._part_path.unlink()
            except OSError:
                pass
            raise

    async def open(self) -> None:
        if self._state == FramedRecordingState.OPEN:
            return
        if self._state != FramedRecordingState.CREATED:
            raise FramedRecordingError(
                FramedRecordingErrorCode.INVALID_STATE,
                "framed recording cannot open from current state",
            )
        try:
            self._file = await asyncio.to_thread(self._open_sync)
        except FileExistsError:
            self._state = FramedRecordingState.FAILED
            raise FramedRecordingError(
                FramedRecordingErrorCode.CONFLICT,
                "framed recording identifier already exists",
            ) from None
        except OSError:
            self._state = FramedRecordingState.FAILED
            raise FramedRecordingError(
                FramedRecordingErrorCode.IO_FAILURE,
                "framed recording failed to open",
            ) from None
        self._state = FramedRecordingState.OPEN

    def _write_record_sync(self, packet: memoryview) -> None:
        checksum = zlib.crc32(packet) & 0xFFFFFFFF
        header = _RECORD_HEADER.pack(len(packet), checksum)
        self._write_all_sync(header)
        self._write_all_sync(packet)

    async def write(self, packet: memoryview) -> None:
        if self._state != FramedRecordingState.OPEN:
            raise FramedRecordingError(
                FramedRecordingErrorCode.INVALID_STATE,
                "framed recording is not open for writes",
            )
        packet_bytes = len(packet)
        if packet_bytes > self._max_packet_bytes or not is_rtp_v2(packet):
            self._state = FramedRecordingState.FAILED
            raise FramedRecordingError(
                FramedRecordingErrorCode.INVALID_RECORD,
                "framed recording packet is invalid",
            )
        if (
            self._packets + 1 > self._max_packets
            or self._payload_bytes + packet_bytes > self._max_payload_bytes
        ):
            self._state = FramedRecordingState.FAILED
            raise FramedRecordingError(
                FramedRecordingErrorCode.LIMIT_EXCEEDED,
                "framed recording limit exceeded",
            )
        try:
            await asyncio.to_thread(self._write_record_sync, packet)
        except OSError:
            self._state = FramedRecordingState.FAILED
            raise FramedRecordingError(
                FramedRecordingErrorCode.IO_FAILURE,
                "framed recording write failed",
            ) from None
        self._packets += 1
        self._payload_bytes += packet_bytes
        self._file_bytes += _RECORD_HEADER.size + packet_bytes

    def _finalize_sync(self) -> None:
        if self._file is None:
            raise OSError
        self._file.flush()
        os.fsync(self._file.fileno())
        self._file.close()
        self._file = None

        os.link(self._part_path, self._final_path)
        try:
            self._part_path.unlink()
        except FileNotFoundError:
            return
        except OSError:
            try:
                self._final_path.unlink()
            except OSError:
                pass
            raise

    async def finalize(self) -> None:
        if self._state == FramedRecordingState.FINALIZED:
            return
        if self._state != FramedRecordingState.OPEN:
            raise FramedRecordingError(
                FramedRecordingErrorCode.INVALID_STATE,
                "framed recording cannot finalize from current state",
            )
        try:
            await asyncio.to_thread(self._finalize_sync)
        except FileExistsError:
            self._state = FramedRecordingState.FAILED
            await self._delete_part_best_effort()
            raise FramedRecordingError(
                FramedRecordingErrorCode.CONFLICT,
                "framed recording identifier already exists",
            ) from None
        except OSError:
            self._state = FramedRecordingState.FAILED
            await self._delete_part_best_effort()
            raise FramedRecordingError(
                FramedRecordingErrorCode.IO_FAILURE,
                "framed recording failed to finalize",
            ) from None
        self._state = FramedRecordingState.FINALIZED

    def _abort_sync(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            finally:
                self._file = None
        try:
            self._part_path.unlink()
        except FileNotFoundError:
            pass

    async def _delete_part_best_effort(self) -> None:
        try:
            await asyncio.to_thread(self._abort_sync)
        except OSError:
            pass

    async def abort(self) -> None:
        if self._state == FramedRecordingState.ABORTED:
            return
        if self._state == FramedRecordingState.FINALIZED:
            raise FramedRecordingError(
                FramedRecordingErrorCode.INVALID_STATE,
                "finalized framed recording cannot be aborted",
            )
        try:
            await asyncio.to_thread(self._abort_sync)
        except OSError:
            self._state = FramedRecordingState.FAILED
            raise FramedRecordingError(
                FramedRecordingErrorCode.IO_FAILURE,
                "framed recording cleanup failed",
            ) from None
        self._state = FramedRecordingState.ABORTED


class FramedRecordingReader:
    """One-pass bounded reader for the Stage-09 recording format."""

    def __init__(
        self,
        path: pathlib.Path,
        *,
        max_packets: int = 1_000_000,
        max_payload_bytes: int = _MAX_RECORDING_BYTES,
        max_packet_bytes: int = _MAX_PACKET_BYTES,
    ) -> None:
        if not 1 <= max_packets <= 1_000_000:
            raise ValueError("max_packets must be between 1 and 1000000")
        if not 1 <= max_payload_bytes <= _MAX_RECORDING_BYTES:
            raise ValueError("max_payload_bytes must be between 1 and 8589934592")
        if not 12 <= max_packet_bytes <= _MAX_PACKET_BYTES:
            raise ValueError("max_packet_bytes must be between 12 and 65535")
        self._path = pathlib.Path(path).expanduser().resolve(strict=False)
        self._max_packets = max_packets
        self._max_payload_bytes = max_payload_bytes
        self._max_packet_bytes = max_packet_bytes
        self._state = FramedRecordingState.CREATED
        self._packets = 0
        self._payload_bytes = 0
        self._file_bytes = 0

    @property
    def snapshot(self) -> FramedRecordingSnapshot:
        return FramedRecordingSnapshot(
            state=self._state,
            packets=self._packets,
            payload_bytes=self._payload_bytes,
            file_bytes=self._file_bytes,
        )

    @staticmethod
    def _read_exact(file: typing.BinaryIO, size: int) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            chunk = file.read(remaining)
            if not chunk:
                raise FramedRecordingError(
                    FramedRecordingErrorCode.TRUNCATED_RECORD,
                    "framed recording is truncated",
                )
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def iter_packets(self) -> typing.Iterator[bytes]:
        if self._state != FramedRecordingState.CREATED:
            raise FramedRecordingError(
                FramedRecordingErrorCode.INVALID_STATE,
                "framed recording reader cannot be reused",
            )
        try:
            file = self._path.open("rb")
        except OSError:
            self._state = FramedRecordingState.FAILED
            raise FramedRecordingError(
                FramedRecordingErrorCode.IO_FAILURE,
                "framed recording failed to open",
            ) from None

        self._state = FramedRecordingState.OPEN
        completed = False
        try:
            magic = file.read(len(_MAGIC))
            self._file_bytes += len(magic)
            if magic != _MAGIC:
                raise FramedRecordingError(
                    FramedRecordingErrorCode.INVALID_HEADER,
                    "framed recording header is invalid",
                )

            while True:
                header = file.read(_RECORD_HEADER.size)
                self._file_bytes += len(header)
                if not header:
                    completed = True
                    self._state = FramedRecordingState.COMPLETE
                    return
                if len(header) != _RECORD_HEADER.size:
                    raise FramedRecordingError(
                        FramedRecordingErrorCode.TRUNCATED_RECORD,
                        "framed recording is truncated",
                    )
                packet_bytes, expected_crc = _RECORD_HEADER.unpack(header)
                if not 12 <= packet_bytes <= self._max_packet_bytes:
                    raise FramedRecordingError(
                        FramedRecordingErrorCode.INVALID_RECORD,
                        "framed recording record is invalid",
                    )
                if (
                    self._packets + 1 > self._max_packets
                    or self._payload_bytes + packet_bytes > self._max_payload_bytes
                ):
                    raise FramedRecordingError(
                        FramedRecordingErrorCode.LIMIT_EXCEEDED,
                        "framed recording read limit exceeded",
                    )
                payload = self._read_exact(file, packet_bytes)
                self._file_bytes += packet_bytes
                if zlib.crc32(payload) & 0xFFFFFFFF != expected_crc:
                    raise FramedRecordingError(
                        FramedRecordingErrorCode.INTEGRITY_FAILURE,
                        "framed recording integrity check failed",
                    )
                if not is_rtp_v2(memoryview(payload)):
                    raise FramedRecordingError(
                        FramedRecordingErrorCode.INVALID_RECORD,
                        "framed recording record is invalid",
                    )
                self._packets += 1
                self._payload_bytes += packet_bytes
                yield payload
        except FramedRecordingError:
            self._state = FramedRecordingState.FAILED
            raise
        except OSError:
            self._state = FramedRecordingState.FAILED
            raise FramedRecordingError(
                FramedRecordingErrorCode.IO_FAILURE,
                "framed recording read failed",
            ) from None
        finally:
            try:
                file.close()
            except OSError:
                if self._state == FramedRecordingState.OPEN:
                    self._state = FramedRecordingState.FAILED
            if not completed and self._state == FramedRecordingState.OPEN:
                self._state = FramedRecordingState.ABORTED
