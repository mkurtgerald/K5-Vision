"""Bounded read boundary over accepted recording artifacts.

The boundary composes the accepted framed-recording reader with the accepted
source-free stream descriptor. Observable state and failures never include
filesystem paths, source details, credentials, or media payloads.
"""

from __future__ import annotations

import enum
import pathlib
import typing

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.framed_recording import (
    FramedRecordingError,
    FramedRecordingErrorCode,
    FramedRecordingReader,
)
from k5vision.media.recording_descriptor import RecordingStreamDescriptor

_MAX_PACKETS = 1_000_000
_MAX_PAYLOAD_BYTES = 8 * 1024 * 1024 * 1024
_MAX_PACKET_BYTES = 65_535


class PlaybackState(enum.StrEnum):
    CREATED = "created"
    READING = "reading"
    COMPLETE = "complete"
    ABORTED = "aborted"
    FAILED = "failed"


class PlaybackErrorCode(enum.StrEnum):
    INVALID_STATE = "invalid_state"
    RECORDING_FAILURE = "recording_failure"
    DESCRIPTOR_MISMATCH = "descriptor_mismatch"
    RTP_MISMATCH = "rtp_mismatch"


class PlaybackError(RuntimeError):
    """Sanitized playback-boundary failure."""

    def __init__(
        self,
        code: PlaybackErrorCode,
        message: str,
        *,
        recording_error_code: FramedRecordingErrorCode | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.recording_error_code = recording_error_code


class PlaybackSnapshot(BaseModel):
    """Source-free playback observability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    state: PlaybackState
    packets: int = Field(ge=0, le=_MAX_PACKETS)
    payload_bytes: int = Field(ge=0, le=_MAX_PAYLOAD_BYTES)
    file_bytes: int = Field(ge=0)
    descriptor_verified: bool = False


class BoundedRecordingPlayback:
    """Read one finalized recording through a validated descriptor exactly once."""

    def __init__(
        self,
        path: pathlib.Path,
        descriptor: RecordingStreamDescriptor,
        *,
        max_packets: int = _MAX_PACKETS,
        max_payload_bytes: int = _MAX_PAYLOAD_BYTES,
        max_packet_bytes: int = _MAX_PACKET_BYTES,
    ) -> None:
        if not 1 <= max_packets <= _MAX_PACKETS:
            raise ValueError("max_packets must be between 1 and 1000000")
        if not 1 <= max_payload_bytes <= _MAX_PAYLOAD_BYTES:
            raise ValueError("max_payload_bytes must be between 1 and 8589934592")
        if not 12 <= max_packet_bytes <= _MAX_PACKET_BYTES:
            raise ValueError("max_packet_bytes must be between 12 and 65535")
        if descriptor.packet_count > max_packets:
            raise ValueError("descriptor packet count exceeds playback limit")
        if descriptor.payload_bytes > max_payload_bytes:
            raise ValueError("descriptor payload bytes exceed playback limit")

        self._path = pathlib.Path(path).expanduser().resolve(strict=False)
        self._descriptor = descriptor
        self._max_packets = max_packets
        self._max_payload_bytes = max_payload_bytes
        self._max_packet_bytes = max_packet_bytes
        self._state = PlaybackState.CREATED
        self._packets = 0
        self._payload_bytes = 0
        self._file_bytes = 0
        self._descriptor_verified = False

    @property
    def snapshot(self) -> PlaybackSnapshot:
        return PlaybackSnapshot(
            state=self._state,
            packets=self._packets,
            payload_bytes=self._payload_bytes,
            file_bytes=self._file_bytes,
            descriptor_verified=self._descriptor_verified,
        )

    @staticmethod
    def _payload_type(packet: bytes) -> int:
        return packet[1] & 0x7F

    @staticmethod
    def _timestamp(packet: bytes) -> int:
        return int.from_bytes(packet[4:8], "big")

    def _validate_packet(self, packet: bytes) -> None:
        if self._payload_type(packet) != self._descriptor.payload_type:
            raise PlaybackError(
                PlaybackErrorCode.RTP_MISMATCH,
                "playback RTP metadata does not match descriptor",
            )
        if self._packets == 0 and self._timestamp(packet) != self._descriptor.rtp_timestamp_origin:
            raise PlaybackError(
                PlaybackErrorCode.RTP_MISMATCH,
                "playback RTP metadata does not match descriptor",
            )

    def _validate_progress(self) -> None:
        if (
            self._packets > self._descriptor.packet_count
            or self._payload_bytes > self._descriptor.payload_bytes
        ):
            raise PlaybackError(
                PlaybackErrorCode.DESCRIPTOR_MISMATCH,
                "playback recording does not match descriptor",
            )

    def _validate_complete(self) -> None:
        if (
            self._packets != self._descriptor.packet_count
            or self._payload_bytes != self._descriptor.payload_bytes
            or self._file_bytes != self._descriptor.file_bytes
        ):
            raise PlaybackError(
                PlaybackErrorCode.DESCRIPTOR_MISMATCH,
                "playback recording does not match descriptor",
            )

    def iter_packets(self) -> typing.Iterator[bytes]:
        """Yield validated packets once while retaining only source-free counters."""
        if self._state != PlaybackState.CREATED:
            raise PlaybackError(
                PlaybackErrorCode.INVALID_STATE,
                "playback boundary cannot be reused",
            )

        reader = FramedRecordingReader(
            self._path,
            max_packets=self._max_packets,
            max_payload_bytes=self._max_payload_bytes,
            max_packet_bytes=self._max_packet_bytes,
        )
        iterator = reader.iter_packets()
        self._state = PlaybackState.READING
        completed = False
        try:
            for packet in iterator:
                self._validate_packet(packet)
                self._packets += 1
                self._payload_bytes += len(packet)
                self._file_bytes = reader.snapshot.file_bytes
                self._validate_progress()
                yield packet

            self._file_bytes = reader.snapshot.file_bytes
            self._validate_complete()
            self._descriptor_verified = True
            self._state = PlaybackState.COMPLETE
            completed = True
        except PlaybackError:
            self._state = PlaybackState.FAILED
            raise
        except FramedRecordingError as exc:
            self._state = PlaybackState.FAILED
            raise PlaybackError(
                PlaybackErrorCode.RECORDING_FAILURE,
                "playback recording read failed",
                recording_error_code=exc.code,
            ) from None
        finally:
            if not completed:
                iterator.close()
                self._file_bytes = reader.snapshot.file_bytes
                if self._state == PlaybackState.READING:
                    self._state = PlaybackState.ABORTED
