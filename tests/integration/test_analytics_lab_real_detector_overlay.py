"""Hosted exact-revision witness for a real reviewed Analytics-lab detector.

This test is opt-in and runs only in the dedicated Stage One analytics compatibility
workflow. It downloads no assets itself. The workflow prepares the Analytics-lab
rights-reviewed validation seed and exact reviewed Open Model Zoo artifacts in
RUNNER_TEMP, then this witness crosses only transient decoded pixels and normalized
tracked detections into the product-owned K5 overlay boundary.

No source URI, credential, filesystem path, media payload, or private/home-camera
material is retained by K5 or uploaded as workflow evidence.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("K5_ANALYTICS_REAL_DETECTOR") != "1",
    reason="exact Analytics-lab detector evidence is not prepared",
)


def _normalized(value: float, extent: int) -> float:
    return min(1.0, max(0.0, float(value) / float(extent)))


def test_pinned_real_detector_tracking_output_renders_visible_box() -> None:
    import cv2

    from analytics_lab.iou_tracker import SimpleIoUAssociationBackend
    from analytics_lab.openvino_omz import OpenVINOOMZPoseBackend
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

    root_value = os.getenv("K5_ANALYTICS_EVIDENCE_ROOT")
    assert root_value
    root = Path(root_value).resolve(strict=True)
    manifest_path = (root / "validation-manifest.json").resolve(strict=True)
    manifest_path.relative_to(root)
    document = json.loads(manifest_path.read_text(encoding="utf-8"))

    samples = document.get("samples")
    assert isinstance(samples, list) and samples
    sample = samples[0]
    assert isinstance(sample, dict)
    relative_video = sample.get("video_path")
    assert isinstance(relative_video, str) and relative_video
    video_path = (root / relative_video).resolve(strict=True)
    video_path.relative_to(root)

    artifact_root = (root / str(document.get("artifact_root", "artifacts"))).resolve(strict=True)
    artifact_root.relative_to(root)

    detector = OpenVINOOMZPoseBackend(artifact_root)
    tracker = TrackingSession(SimpleIoUAssociationBackend())
    capture = cv2.VideoCapture(str(video_path))
    assert capture.isOpened()

    selected_image = None
    selected_tracks = ()
    selected_timestamp_ms = 0
    try:
        for frame_index in range(60):
            ok, image = capture.read()
            if not ok:
                break
            height, width = image.shape[:2]
            timestamp_ms = frame_index * 40 + 1
            candidates = []
            for pose in detector(image, frame_index, timestamp_ms):
                box = pose.bbox
                x_min = _normalized(box.x1, width)
                y_min = _normalized(box.y1, height)
                x_max = _normalized(box.x2, width)
                y_max = _normalized(box.y2, height)
                if x_max <= x_min or y_max <= y_min:
                    continue
                candidates.append(
                    DetectionCandidate(
                        category="person",
                        confidence=pose.confidence,
                        box=NormalizedBox(x_min, y_min, x_max, y_max),
                        model_class_id=1,
                    )
                )
            tracks = tracker.update(frame_index, timestamp_ms, tuple(candidates))
            if tracks:
                selected_image = image
                selected_tracks = tracks
                selected_timestamp_ms = timestamp_ms
                break
    finally:
        capture.release()

    assert selected_image is not None, "reviewed detector produced no person within bounded witness"
    assert selected_tracks

    height, width = selected_image.shape[:2]
    bgrx = cv2.cvtColor(selected_image, cv2.COLOR_BGR2BGRA)
    bgrx[:, :, 3] = 0
    frame = PresentationVideoFrame(
        payload=memoryview(bytearray(bgrx.tobytes())),
        width=width,
        height=height,
        stride_bytes=width * 4,
        pixel_format=PixelFormat.BGRX,
        source_elapsed_ms=selected_timestamp_ms,
    )

    observations = adapt_analytics_tracked_detections(
        selected_tracks,
        max_observations=32,
    )
    result = BoundedDetectionOverlayRenderer(
        max_boxes=32,
        border_width=2,
        minimum_confidence=0.5,
    ).render(frame, observations)

    assert result.snapshot.input_observations >= 1
    assert result.snapshot.rendered_boxes >= 1
    assert result.snapshot.pixel_writes > 0
    assert bytes(result.frame.payload) != bytes(frame.payload)

    retained = result.snapshot.model_dump_json().casefold()
    for forbidden in (
        "payload",
        "source",
        "credential",
        "password",
        "rtsp",
        str(video_path).casefold(),
        str(artifact_root).casefold(),
    ):
        assert forbidden not in retained
