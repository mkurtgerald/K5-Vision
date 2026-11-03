"""Make generated H.264 recordings readable by the native donor's legacy reader.

FFmpeg stores repeated fragment sample properties as TFHD defaults. MediaMTX's
recording reader expects those properties explicitly in TRUN entries. Expanding
these equivalent container fields preserves every encoded sample and timestamp;
it does not mock history, alter the product decoder, or claim recording endurance.
"""

from __future__ import annotations

import struct

_MAX_BYTES = 2 * 1024 * 1024


def _boxes(data: bytes):
    cursor = 0
    while cursor < len(data):
        if len(data) - cursor < 8:
            raise ValueError("Incomplete recording fixture box")
        size, kind = struct.unpack_from(">I4s", data, cursor)
        header = 8
        if size == 1:
            if len(data) - cursor < 16:
                raise ValueError("Incomplete recording fixture box")
            size = struct.unpack_from(">Q", data, cursor + 8)[0]
            header = 16
        elif size == 0:
            size = len(data) - cursor
        if size < header or cursor + size > len(data):
            raise ValueError("Invalid recording fixture box")
        yield kind, data[cursor + header : cursor + size], data[cursor : cursor + size]
        cursor += size


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def _flags(payload: bytes) -> int:
    if len(payload) < 4:
        raise ValueError("Invalid recording fixture full box")
    return int.from_bytes(payload[1:4], "big")


def _defaults(tfhd: bytes) -> tuple[int, int, int]:
    flags = _flags(tfhd)
    if len(tfhd) < 8 or flags & 1 or not flags & 0x020000:
        raise ValueError("Recording fixture needs fragment-relative offsets")
    offset = 8
    if flags & 2:
        offset += 4
    result = []
    for flag in (8, 16, 32):
        if flags & flag:
            if offset + 4 > len(tfhd):
                raise ValueError("Invalid recording fixture sample defaults")
            result.append(struct.unpack_from(">I", tfhd, offset)[0])
            offset += 4
        else:
            result.append(0)
    if offset != len(tfhd):
        raise ValueError("Unexpected recording fixture sample defaults")
    return tuple(result)


def _samples(payload: bytes, defaults: tuple[int, int, int], delta: int) -> bytes:
    flags = _flags(payload)
    if payload[0] not in (0, 1) or not flags & 1 or len(payload) < 12:
        raise ValueError("Invalid recording fixture sample table")
    count, data_offset = struct.unpack_from(">Ii", payload, 4)
    if not 1 <= count <= 100000 or data_offset <= 0:
        raise ValueError("Invalid recording fixture sample table")
    offset = 12
    first_flags = None
    if flags & 4:
        if offset + 4 > len(payload):
            raise ValueError("Invalid recording fixture first sample")
        first_flags = struct.unpack_from(">I", payload, offset)[0]
        offset += 4
    expanded_flags = (flags | 0x100 | 0x200 | 0x400) & ~4
    output = bytearray(bytes((payload[0],)) + expanded_flags.to_bytes(3, "big"))
    output.extend(struct.pack(">Ii", count, data_offset + delta))
    for index in range(count):
        values = []
        for field, flag in enumerate((0x100, 0x200, 0x400)):
            if flags & flag:
                if offset + 4 > len(payload):
                    raise ValueError("Incomplete recording fixture sample")
                values.append(struct.unpack_from(">I", payload, offset)[0])
                offset += 4
            else:
                values.append(
                    first_flags
                    if field == 2 and index == 0 and first_flags is not None
                    else defaults[field]
                )
        if not values[0] or not values[1]:
            raise ValueError("Recording fixture sample has no duration or payload")
        output.extend(struct.pack(">III", *values))
        if flags & 0x800:
            if offset + 4 > len(payload):
                raise ValueError("Incomplete recording fixture composition time")
            output.extend(payload[offset : offset + 4])
            offset += 4
    if offset != len(payload):
        raise ValueError("Unexpected recording fixture sample fields")
    return bytes(output)


def _track(payload: bytes, delta: int) -> bytes:
    children = list(_boxes(payload))
    headers = [body for kind, body, _raw in children if kind == b"tfhd"]
    if len(headers) != 1:
        raise ValueError("Recording fixture needs one fragment track header")
    defaults = _defaults(headers[0])
    output = []
    for kind, body, raw in children:
        if kind == b"trun":
            output.append(_box(kind, _samples(body, defaults, delta)))
        elif kind == b"tfdt":
            if body[0] == 0 and len(body) == 8:
                body = b"\x01" + body[1:4] + struct.pack(">Q", struct.unpack_from(">I", body, 4)[0])
            elif body[0] != 1 or len(body) != 12:
                raise ValueError("Invalid recording fixture decode time")
            output.append(_box(kind, body))
        else:
            output.append(raw)
    return _box(b"traf", b"".join(output))


def _fragment(payload: bytes, delta: int) -> bytes:
    return _box(
        b"moof",
        b"".join(
            _track(body, delta) if kind == b"traf" else raw for kind, body, raw in _boxes(payload)
        ),
    )


def explicit_recording_samples(payload: bytes) -> bytes:
    """Expand FFmpeg defaults into equivalent native-reader-compatible samples."""
    if not isinstance(payload, bytes) or not 32 <= len(payload) <= _MAX_BYTES:
        raise ValueError("Invalid recording fixture size")
    output = []
    fragments = 0
    for kind, body, raw in _boxes(payload):
        if kind == b"moof":
            rewritten = _fragment(body, 0)
            delta = len(rewritten) - len(raw)
            output.append(_fragment(body, delta))
            fragments += 1
        elif kind != b"mfra":
            # FFmpeg's optional trailing random-access index names old offsets.
            # The recording reader scans fragments and does not require it.
            output.append(raw)
    result = b"".join(output)
    if not fragments or len(result) > _MAX_BYTES:
        raise ValueError("Invalid recording fixture fragments")
    return result
