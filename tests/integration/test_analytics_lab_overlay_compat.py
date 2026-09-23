"""Exact-revision compatibility witness for Analytics-lab tracking output.

The external repository is supplied only by the dedicated compatibility workflow.
K5 runtime does not import or track Analytics-lab source. This test deliberately
uses the lab's actual tracking contracts and simple-IoU producer implementation,
then crosses only the product-owned normalized overlay adapter.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("K5_ANALYTICS_COMPAT") != "1",
    reason="exact Analytics-lab compatibility checkout is not present",
)


def test_pinned_analytics_tracking_output_renders_visible_box() -> None:
    from analytics_lab.iou_tracker import SimpleIoUAssociationBackend
    from analytics_lab.tracking import (
        DetectionCandidate,
        NormalizedBox,
        TrackingSession,
    )

    from k5vision.media.analytics_detection_adapter import (
        adapt_analytics_tracked_detections,
    )
    from k5vision.media.detection_overlay import BoundedDetectionOverlayRenderer
    from k5vision.media.presentation_frame import PixelFormat, PresentationVideoFrame

    session = TrackingSession(SimpleIoUAssociationBackend())
    tracks = session.update(
        0,
        1_000,
        (
            DetectionCandidate(
                category="person",
                confidence=0.93,
                box=NormalizedBox(0.25, 0.20, 0.75, 0.85),
                model_class_id=0,
            ),
        ),
    )
    assert len(tracks) == 1
    assert tracks[0].track_id.startswith("iou-")

    observations = adapt_analytics_tracked_detections(tracks, max_observations=4)
    frame = PresentationVideoFrame(
        payload=memoryview(bytes(16 * 12 * 4)),
        width=16,
        height=12,
        stride_bytes=16 * 4,
        pixel_format=PixelFormat.BGRX,
        source_elapsed_ms=1_000,
    )
    result = BoundedDetectionOverlayRenderer(
        max_boxes=4,
        border_width=1,
        minimum_confidence=0.5,
    ).render(frame, observations)

    assert result.snapshot.input_observations == 1
    assert result.snapshot.rendered_boxes == 1
    assert result.snapshot.pixel_writes > 0
    assert bytes(result.frame.payload) != bytes(frame.payload)
    retained = result.snapshot.model_dump_json().casefold()
    assert tracks[0].track_id.casefold() not in retained
    assert "person" not in retained
    assert "payload" not in retained
    assert "source" not in retained
