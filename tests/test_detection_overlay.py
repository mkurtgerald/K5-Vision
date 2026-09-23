import math

import pytest

from k5vision.media.detection_overlay import (
    BoundedDetectionOverlayRenderer,
    DetectionOverlayError,
    DetectionOverlayObservation,
)
from k5vision.media.presentation_frame import PixelFormat, PresentationVideoFrame


def _frame(width: int = 8, height: int = 6) -> PresentationVideoFrame:
    stride = width * 4
    return PresentationVideoFrame(
        payload=memoryview(bytes(stride * height)),
        width=width,
        height=height,
        stride_bytes=stride,
        pixel_format=PixelFormat.BGRX,
        source_elapsed_ms=125,
    )


def test_overlay_draws_visible_box_without_mutating_source_frame() -> None:
    frame = _frame()
    source_bytes = bytes(frame.payload)
    renderer = BoundedDetectionOverlayRenderer(
        max_boxes=4,
        border_width=1,
        minimum_confidence=0.5,
    )
    observation = DetectionOverlayObservation(
        category="person",
        confidence=0.9,
        x_min=0.25,
        y_min=1 / 6,
        x_max=0.75,
        y_max=5 / 6,
    )

    result = renderer.render(frame, (observation,))

    assert bytes(frame.payload) == source_bytes
    assert result.frame is not frame
    assert bytes(result.frame.payload) != source_bytes
    assert result.frame.width == frame.width
    assert result.frame.height == frame.height
    assert result.frame.stride_bytes == frame.stride_bytes
    assert result.frame.pixel_format is PixelFormat.BGRX
    assert result.frame.source_elapsed_ms == frame.source_elapsed_ms
    assert result.snapshot.input_observations == 1
    assert result.snapshot.rendered_boxes == 1
    assert result.snapshot.confidence_filtered == 0
    assert result.snapshot.pixel_writes > 0

    left = 2
    top = 1
    offset = top * frame.stride_bytes + left * 4
    assert bytes(result.frame.payload[offset : offset + 4]) == b"\x00\xff\x00\x00"


def test_overlay_filters_low_confidence_without_copying_payload() -> None:
    frame = _frame()
    renderer = BoundedDetectionOverlayRenderer(minimum_confidence=0.8)
    observation = DetectionOverlayObservation(
        category="vehicle",
        confidence=0.5,
        x_min=0.1,
        y_min=0.1,
        x_max=0.9,
        y_max=0.9,
    )

    result = renderer.render(frame, (observation,))

    assert result.frame is frame
    assert result.snapshot.input_observations == 1
    assert result.snapshot.rendered_boxes == 0
    assert result.snapshot.confidence_filtered == 1
    assert result.snapshot.pixel_writes == 0


def test_overlay_snapshot_retains_no_box_label_or_media_payload() -> None:
    frame = _frame()
    renderer = BoundedDetectionOverlayRenderer()
    observation = DetectionOverlayObservation(
        category="private-category-marker",
        confidence=1.0,
        x_min=0.0,
        y_min=0.0,
        x_max=1.0,
        y_max=1.0,
    )

    payload = renderer.render(frame, (observation,)).snapshot.model_dump_json().casefold()

    assert "private-category-marker" not in payload
    assert "payload" not in payload
    assert "source" not in payload
    assert "credential" not in payload
    assert "path" not in payload
    assert "x_min" not in payload
    assert "y_min" not in payload


def test_overlay_rejects_invalid_observations_and_count_overflow() -> None:
    with pytest.raises(DetectionOverlayError, match="finite"):
        DetectionOverlayObservation(
            category="person",
            confidence=math.nan,
            x_min=0.1,
            y_min=0.1,
            x_max=0.9,
            y_max=0.9,
        )
    with pytest.raises(DetectionOverlayError, match="positive normalized area"):
        DetectionOverlayObservation(
            category="person",
            confidence=0.9,
            x_min=0.5,
            y_min=0.1,
            x_max=0.5,
            y_max=0.9,
        )

    frame = _frame()
    renderer = BoundedDetectionOverlayRenderer(max_boxes=1)
    observation = DetectionOverlayObservation(
        category="person",
        confidence=0.9,
        x_min=0.1,
        y_min=0.1,
        x_max=0.9,
        y_max=0.9,
    )
    with pytest.raises(DetectionOverlayError, match="count"):
        renderer.render(frame, (observation, observation))


def test_overlay_configuration_is_bounded() -> None:
    with pytest.raises(ValueError, match="max_boxes"):
        BoundedDetectionOverlayRenderer(max_boxes=0)
    with pytest.raises(ValueError, match="border_width"):
        BoundedDetectionOverlayRenderer(border_width=0)
    with pytest.raises(ValueError, match="minimum_confidence"):
        BoundedDetectionOverlayRenderer(minimum_confidence=math.inf)
