from __future__ import annotations

import asyncio
import struct
import zlib

import pytest

from k5vision.media.framed_recording import (
    FramedAtomicRecordingSink,
    FramedRecordingError,
    FramedRecordingErrorCode,
    FramedRecordingReader,
    FramedRecordingState,
)

_MAGIC = b"K5RTPF\x00\x01"
_RECORD_HEADER = struct.Struct(">II")


def _rtp(payload: bytes = b"x", *, sequence: int = 1) -> bytes:
    return bytes(
        [
            0x80,
            96,
            (sequence >> 8) & 0xFF,
            sequence & 0xFF,
            0,
            0,
            0,
            1,
            0,
            0,
            0,
            1,
        ]
    ) + payload


def _record(packet: bytes, *, checksum: int | None = None) -> bytes:
    crc = zlib.crc32(packet) & 0xFFFFFFFF if checksum is None else checksum
    return _RECORD_HEADER.pack(len(packet), crc) + packet


def test_zero_packet_recording_is_well_formed_and_readable(tmp_path) -> None:
    async def exercise() -> None:
        sink = FramedAtomicRecordingSink(tmp_path, "zero")
        await sink.open()
        await sink.finalize()
        assert sink.snapshot.state == FramedRecordingState.FINALIZED

    asyncio.run(exercise())
    path = tmp_path / "zero.k5r"
    assert path.read_bytes() == _MAGIC

    reader = FramedRecordingReader(path)
    assert list(reader.iter_packets()) == []
    assert reader.snapshot.state == FramedRecordingState.COMPLETE
    assert reader.snapshot.packets == 0
    assert reader.snapshot.payload_bytes == 0


def test_round_trip_preserves_packet_boundaries_exactly(tmp_path) -> None:
    packets = [_rtp(b"a", sequence=1), _rtp(b"bc", sequence=2), _rtp(b"def", sequence=3)]

    async def exercise() -> None:
        sink = FramedAtomicRecordingSink(tmp_path, "round-trip")
        await sink.open()
        for packet in packets:
            await sink.write(memoryview(packet))
        await sink.finalize()
        assert sink.snapshot.packets == 3
        assert sink.snapshot.payload_bytes == sum(map(len, packets))
        assert sink.snapshot.file_bytes == len(_MAGIC) + sum(
            _RECORD_HEADER.size + len(packet) for packet in packets
        )

    asyncio.run(exercise())

    reader = FramedRecordingReader(tmp_path / "round-trip.k5r")
    assert list(reader.iter_packets()) == packets
    assert reader.snapshot.state == FramedRecordingState.COMPLETE
    assert reader.snapshot.packets == 3
    assert reader.snapshot.payload_bytes == sum(map(len, packets))


def test_writer_rejects_invalid_identifier_packet_and_limits(tmp_path) -> None:
    with pytest.raises(FramedRecordingError) as invalid_id:
        FramedAtomicRecordingSink(tmp_path, "../escape")
    assert invalid_id.value.code == FramedRecordingErrorCode.INVALID_RECORDING_ID

    async def invalid_packet() -> None:
        sink = FramedAtomicRecordingSink(tmp_path, "invalid")
        await sink.open()
        with pytest.raises(FramedRecordingError) as exc:
            await sink.write(memoryview(b"not-rtp"))
        assert exc.value.code == FramedRecordingErrorCode.INVALID_RECORD
        assert sink.snapshot.state == FramedRecordingState.FAILED
        await sink.abort()
        assert sink.snapshot.state == FramedRecordingState.ABORTED

    asyncio.run(invalid_packet())

    async def packet_limit() -> None:
        sink = FramedAtomicRecordingSink(tmp_path, "limit", max_packets=1)
        await sink.open()
        await sink.write(memoryview(_rtp(b"a")))
        with pytest.raises(FramedRecordingError) as exc:
            await sink.write(memoryview(_rtp(b"b", sequence=2)))
        assert exc.value.code == FramedRecordingErrorCode.LIMIT_EXCEEDED
        await sink.abort()

    asyncio.run(packet_limit())


def test_finalize_and_abort_state_transitions_fail_closed(tmp_path) -> None:
    async def exercise() -> None:
        sink = FramedAtomicRecordingSink(tmp_path, "states")
        await sink.open()
        await sink.finalize()
        await sink.finalize()
        with pytest.raises(FramedRecordingError) as exc:
            await sink.abort()
        assert exc.value.code == FramedRecordingErrorCode.INVALID_STATE

        aborted = FramedAtomicRecordingSink(tmp_path, "aborted")
        await aborted.open()
        await aborted.abort()
        await aborted.abort()
        with pytest.raises(FramedRecordingError) as exc2:
            await aborted.finalize()
        assert exc2.value.code == FramedRecordingErrorCode.INVALID_STATE
        assert not (tmp_path / ".aborted.k5r.part").exists()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (b"bad", FramedRecordingErrorCode.INVALID_HEADER),
        (_MAGIC + b"\x00", FramedRecordingErrorCode.TRUNCATED_RECORD),
        (_MAGIC + _RECORD_HEADER.pack(0, 0), FramedRecordingErrorCode.INVALID_RECORD),
        (
            _MAGIC + _RECORD_HEADER.pack(12, 0) + b"\x80\x60",
            FramedRecordingErrorCode.TRUNCATED_RECORD,
        ),
        (
            _MAGIC + _record(_rtp(b"crc"), checksum=0),
            FramedRecordingErrorCode.INTEGRITY_FAILURE,
        ),
        (
            _MAGIC + _record(b"\x00" * 12),
            FramedRecordingErrorCode.INVALID_RECORD,
        ),
    ],
)
def test_reader_rejects_corruption_and_truncation(tmp_path, body: bytes, code) -> None:
    path = tmp_path / "corrupt.k5r"
    path.write_bytes(body)
    reader = FramedRecordingReader(path)
    with pytest.raises(FramedRecordingError) as exc:
        list(reader.iter_packets())
    assert exc.value.code == code
    assert reader.snapshot.state == FramedRecordingState.FAILED


def test_reader_enforces_packet_and_payload_limits(tmp_path) -> None:
    packets = [_rtp(b"a", sequence=1), _rtp(b"b", sequence=2)]
    path = tmp_path / "limits.k5r"
    path.write_bytes(_MAGIC + b"".join(_record(packet) for packet in packets))

    reader = FramedRecordingReader(path, max_packets=1)
    iterator = reader.iter_packets()
    assert next(iterator) == packets[0]
    with pytest.raises(FramedRecordingError) as exc:
        next(iterator)
    assert exc.value.code == FramedRecordingErrorCode.LIMIT_EXCEEDED
    assert reader.snapshot.state == FramedRecordingState.FAILED

    reader2 = FramedRecordingReader(path, max_payload_bytes=len(packets[0]))
    iterator2 = reader2.iter_packets()
    assert next(iterator2) == packets[0]
    with pytest.raises(FramedRecordingError) as exc2:
        next(iterator2)
    assert exc2.value.code == FramedRecordingErrorCode.LIMIT_EXCEEDED


def test_reader_is_one_pass_and_early_close_aborts_state(tmp_path) -> None:
    packet = _rtp(b"x")
    path = tmp_path / "single.k5r"
    path.write_bytes(_MAGIC + _record(packet))

    reader = FramedRecordingReader(path)
    assert list(reader.iter_packets()) == [packet]
    with pytest.raises(FramedRecordingError) as exc:
        list(reader.iter_packets())
    assert exc.value.code == FramedRecordingErrorCode.INVALID_STATE

    early = FramedRecordingReader(path)
    iterator = early.iter_packets()
    assert next(iterator) == packet
    iterator.close()
    assert early.snapshot.state == FramedRecordingState.ABORTED


def test_failures_do_not_expose_path_or_payload(tmp_path) -> None:
    secret_dir = tmp_path / "SECRET_PATH_MARKER"
    secret_dir.mkdir()
    path = secret_dir / "private.k5r"
    payload_marker = b"SECRET_PAYLOAD_MARKER"
    packet = _rtp(payload_marker)
    path.write_bytes(_MAGIC + _record(packet, checksum=0))

    reader = FramedRecordingReader(path)
    with pytest.raises(FramedRecordingError) as exc:
        list(reader.iter_packets())
    text = str(exc.value)
    assert "SECRET_PATH_MARKER" not in text
    assert "SECRET_PAYLOAD_MARKER" not in text
    assert "rtsp://" not in text.casefold()
    assert "credential" not in text.casefold()
    assert "runner" not in text.casefold()
