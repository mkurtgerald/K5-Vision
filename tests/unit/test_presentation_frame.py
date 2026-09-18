from __future__ import annotations

import pytest

from k5vision.media.presentation_frame import (
    PixelFormat,
    PresentationFrameError,
    PresentationFrameErrorCode,
    PresentationVideoFrame,
)


def _frame(**kwargs: object) -> PresentationVideoFrame:
    values: dict[str, object] = {
        "payload": memoryview(bytearray(16)),
        "width": 2,
        "height": 2,
        "stride_bytes": 8,
        "pixel_format": PixelFormat.BGRX,
        "source_elapsed_ms": 125,
    }
    values.update(kwargs)
    return PresentationVideoFrame(**values)  # type: ignore[arg-type]


def test_valid_frame_is_zero_copy_and_metadata_is_payload_free() -> None:
    raw = bytearray(range(16))
    frame = _frame(payload=memoryview(raw))

    assert frame.payload.obj is raw
    metadata = frame.metadata
    assert metadata.width == 2
    assert metadata.height == 2
    assert metadata.stride_bytes == 8
    assert metadata.pixel_format == PixelFormat.BGRX
    assert metadata.source_elapsed_ms == 125
    assert metadata.byte_length == 16
    serialized = str(metadata.model_dump())
    assert "bytearray" not in serialized
    assert "memoryview" not in serialized
    assert str(list(raw)) not in serialized


def test_padded_stride_is_allowed_when_payload_matches() -> None:
    frame = _frame(payload=memoryview(bytearray(24)), stride_bytes=12)

    assert frame.metadata.byte_length == 24
    assert frame.metadata.stride_bytes == 12


@pytest.mark.parametrize("width,height", [(0, 2), (2, 0), (16_385, 2), (2, 16_385)])
def test_invalid_geometry_fails_closed(width: int, height: int) -> None:
    with pytest.raises(PresentationFrameError) as caught:
        _frame(width=width, height=height)

    assert caught.value.code == PresentationFrameErrorCode.INVALID_GEOMETRY


def test_stride_smaller_than_bgrx_row_fails_closed() -> None:
    with pytest.raises(PresentationFrameError) as caught:
        _frame(stride_bytes=7)

    assert caught.value.code == PresentationFrameErrorCode.INVALID_STRIDE


def test_payload_length_must_match_stride_times_height() -> None:
    with pytest.raises(PresentationFrameError) as caught:
        _frame(payload=memoryview(bytearray(15)))

    assert caught.value.code == PresentationFrameErrorCode.INVALID_PAYLOAD


def test_payload_must_be_memoryview() -> None:
    with pytest.raises(PresentationFrameError) as caught:
        _frame(payload=b"0123456789abcdef")

    assert caught.value.code == PresentationFrameErrorCode.INVALID_PAYLOAD


def test_invalid_timing_fails_closed() -> None:
    with pytest.raises(PresentationFrameError) as caught:
        _frame(source_elapsed_ms=-1)

    assert caught.value.code == PresentationFrameErrorCode.INVALID_TIMING


def test_unsupported_pixel_format_fails_closed() -> None:
    with pytest.raises(PresentationFrameError) as caught:
        _frame(pixel_format="RGB")

    assert caught.value.code == PresentationFrameErrorCode.UNSUPPORTED_FORMAT


def test_oversized_geometry_payload_fails_closed_without_allocating_it() -> None:
    with pytest.raises(PresentationFrameError) as caught:
        _frame(width=16_384, height=16_384, stride_bytes=65_536)

    assert caught.value.code == PresentationFrameErrorCode.INVALID_PAYLOAD
