"""Bounded local persistence sink for the K5 recording boundary.

The sink is intentionally small and project-owned. It persists opaque packet bytes
behind the Stage-07 ``RecordingSink`` protocol while keeping paths and payloads out
of observable state and exception text. Playback/container interpretation remains
outside this module.
"""

from __future__ import annotations

import asyncio
import enum
import os
import pathlib
import re
import typing

from pydantic import BaseModel, ConfigDict, Field


_RECORDING_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class FileSinkState(enum.StrEnum):
    CREATED = "created"
    OPEN = "open"
    FINALIZED = "finalized"
    ABORTED = "aborted"
    FAILED = "failed"


class FileSinkErrorCode(enum.StrEnum):
    INVALID_RECORDING_ID = "invalid_recording_id"
    INVALID_STATE = "invalid_state"
    CONFLICT = "conflict"
    LIMIT_EXCEEDED = "limit_exceeded"
    IO_FAILURE = "io_failure"


class FileSinkError(RuntimeError):
    """Sanitized local-persistence failure."""

    def __init__(self, code: FileSinkErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class FileSinkSnapshot(BaseModel):
    """Path-free local-persistence observability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    state: FileSinkState
    bytes_written: int = Field(ge=0)


class AtomicLocalRecordingSink:
    """Persist one bounded opaque recording using temp-then-atomic-finalize semantics."""

    def __init__(
        self,
        root: pathlib.Path,
        recording_id: str,
        *,
        max_bytes: int = 8 * 1024 * 1024 * 1024,
    ) -> None:
        if not _RECORDING_ID.fullmatch(recording_id) or recording_id in {".", ".."}:
            raise FileSinkError(
                FileSinkErrorCode.INVALID_RECORDING_ID,
                "recording identifier is invalid",
            )
        if not 1 <= max_bytes <= 8 * 1024 * 1024 * 1024:
            raise ValueError("max_bytes must be between 1 and 8589934592")

        self._root = pathlib.Path(root).expanduser().resolve(strict=False)
        self._recording_id = recording_id
        self._max_bytes = max_bytes
        self._part_path = self._root / f".{recording_id}.part"
        self._final_path = self._root / f"{recording_id}.rtp"
        self._file: typing.BinaryIO | None = None
        self._state = FileSinkState.CREATED
        self._bytes_written = 0

    @property
    def snapshot(self) -> FileSinkSnapshot:
        return FileSinkSnapshot(state=self._state, bytes_written=self._bytes_written)

    def _open_sync(self) -> typing.BinaryIO:
        self._root.mkdir(parents=True, exist_ok=True)
        if self._final_path.exists():
            raise FileExistsError
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        fd = os.open(self._part_path, flags, 0o600)
        try:
            return os.fdopen(fd, "wb", buffering=0)
        except BaseException:
            os.close(fd)
            raise

    async def open(self) -> None:
        if self._state == FileSinkState.OPEN:
            return
        if self._state != FileSinkState.CREATED:
            raise FileSinkError(
                FileSinkErrorCode.INVALID_STATE,
                "local recording cannot open from current state",
            )
        try:
            self._file = await asyncio.to_thread(self._open_sync)
        except FileExistsError:
            self._state = FileSinkState.FAILED
            raise FileSinkError(
                FileSinkErrorCode.CONFLICT,
                "local recording identifier already exists",
            ) from None
        except OSError:
            self._state = FileSinkState.FAILED
            raise FileSinkError(
                FileSinkErrorCode.IO_FAILURE,
                "local recording failed to open",
            ) from None
        self._state = FileSinkState.OPEN

    def _write_sync(self, packet: memoryview) -> None:
        if self._file is None:
            raise OSError
        written = self._file.write(packet)
        if written != len(packet):
            raise OSError

    async def write(self, packet: memoryview) -> None:
        if self._state != FileSinkState.OPEN:
            raise FileSinkError(
                FileSinkErrorCode.INVALID_STATE,
                "local recording is not open for writes",
            )
        packet_bytes = len(packet)
        if self._bytes_written + packet_bytes > self._max_bytes:
            raise FileSinkError(
                FileSinkErrorCode.LIMIT_EXCEEDED,
                "local recording byte limit exceeded",
            )
        try:
            await asyncio.to_thread(self._write_sync, packet)
        except OSError:
            self._state = FileSinkState.FAILED
            raise FileSinkError(
                FileSinkErrorCode.IO_FAILURE,
                "local recording write failed",
            ) from None
        self._bytes_written += packet_bytes

    def _finalize_sync(self) -> None:
        if self._file is None:
            raise OSError
        self._file.flush()
        os.fsync(self._file.fileno())
        self._file.close()
        self._file = None
        if self._final_path.exists():
            raise FileExistsError
        os.replace(self._part_path, self._final_path)

    async def finalize(self) -> None:
        if self._state == FileSinkState.FINALIZED:
            return
        if self._state != FileSinkState.OPEN:
            raise FileSinkError(
                FileSinkErrorCode.INVALID_STATE,
                "local recording cannot finalize from current state",
            )
        try:
            await asyncio.to_thread(self._finalize_sync)
        except FileExistsError:
            self._state = FileSinkState.FAILED
            await self._delete_part_best_effort()
            raise FileSinkError(
                FileSinkErrorCode.CONFLICT,
                "local recording identifier already exists",
            ) from None
        except OSError:
            self._state = FileSinkState.FAILED
            await self._delete_part_best_effort()
            raise FileSinkError(
                FileSinkErrorCode.IO_FAILURE,
                "local recording failed to finalize",
            ) from None
        self._state = FileSinkState.FINALIZED

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
        if self._state == FileSinkState.ABORTED:
            return
        if self._state == FileSinkState.FINALIZED:
            raise FileSinkError(
                FileSinkErrorCode.INVALID_STATE,
                "finalized local recording cannot be aborted",
            )
        try:
            await asyncio.to_thread(self._abort_sync)
        except OSError:
            self._state = FileSinkState.FAILED
            raise FileSinkError(
                FileSinkErrorCode.IO_FAILURE,
                "local recording cleanup failed",
            ) from None
        self._state = FileSinkState.ABORTED
