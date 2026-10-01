"""Physical Stage-One witness joining reviewed analytics to the Windows operator.

This opt-in witness deliberately avoids external RTSP availability. It streams only
rights-reviewed validation-video frames prepared by the pinned Analytics-lab revision
through the product's real analytics overlay wrapper and real Windows operator runtime.
No frame payloads, source paths, credentials, screenshots, or media are retained.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from k5vision.media.live_presentation import LivePresentationSnapshot, LivePresentationState
from k5vision.media.presentation_frame import PixelFormat, PresentationVideoFrame
from k5vision.operator_launch import ResolvedLiveSource
from k5vision.operator_runtime import WindowsSingleLiveOperatorLauncher

pytestmark = pytest.mark.skipif(
    os.getenv("K5_STAGE_ONE_JOINED_ANALYTICS_PHYSICAL") != "1",
    reason="Stage One joined analytics/Windows qualification is opt-in",
)

_MAX_FRAMES = 60


class _JoinedAnalyticsProvider:
    """Pinned Analytics-lab detector/tracker adapter for this physical witness."""

    def __init__(self, evidence_root: Path) -> None:
        from analytics_lab.iou_tracker import SimpleIoUAssociationBackend
        from analytics_lab.openvino_omz import OpenVINOOMZConfig, OpenVINOOMZPoseBackend
        from analytics_lab.tracking import TrackingSession

        manifest_path = (evidence_root / "validation-manifest.json").resolve(strict=True)
        manifest_path.relative_to(evidence_root)
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
        artifact_root = (evidence_root / str(document.get("artifact_root", "artifacts"))).resolve(
            strict=True
        )
        artifact_root.relative_to(evidence_root)

        self._detector = OpenVINOOMZPoseBackend(
            artifact_root,
            config=OpenVINOOMZConfig(max_people=4),
        )
        self._tracker = TrackingSession(SimpleIoUAssociationBackend())
        self._frame_index = 0
        self.provider_calls = 0
        self.tracked_detections = 0

    async def __call__(self, frame: PresentationVideoFrame) -> tuple[object, ...]:
        import numpy as np
        from analytics_lab.tracking import DetectionCandidate, NormalizedBox

        self.provider_calls += 1
        frame_index = self._frame_index
        self._frame_index += 1
        timestamp_ms = frame_index * 100 + 1

        raw = np.frombuffer(
            frame.payload,
            dtype=np.uint8,
            count=frame.height * frame.stride_bytes,
        )
        rows = raw.reshape(frame.height, frame.stride_bytes)
        bgrx = rows[:, : frame.width * 4].reshape(frame.height, frame.width, 4)
        bgr = np.ascontiguousarray(bgrx[:, :, :3])

        poses = await asyncio.to_thread(
            self._detector,
            bgr,
            frame_index,
            timestamp_ms,
        )
        candidates = []
        for pose in poses:
            box = pose.bbox
            x_min = min(1.0, max(0.0, float(box.x1) / frame.width))
            y_min = min(1.0, max(0.0, float(box.y1) / frame.height))
            x_max = min(1.0, max(0.0, float(box.x2) / frame.width))
            y_max = min(1.0, max(0.0, float(box.y2) / frame.height))
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

        tracks = self._tracker.update(frame_index, timestamp_ms, tuple(candidates))
        self.tracked_detections += len(tracks)
        return tracks


class _ReviewedVideoDelivery:
    """Emit only transient reviewed validation frames into the live-delivery seam."""

    def __init__(self, evidence_root: Path, provider: _JoinedAnalyticsProvider) -> None:
        manifest_path = (evidence_root / "validation-manifest.json").resolve(strict=True)
        manifest_path.relative_to(evidence_root)
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
        samples = document.get("samples")
        if not isinstance(samples, list) or not samples or not isinstance(samples[0], dict):
            raise ValueError("reviewed validation sample is unavailable")
        relative_video = samples[0].get("video_path")
        if not isinstance(relative_video, str) or not relative_video:
            raise ValueError("reviewed validation video is unavailable")
        self._video_path = (evidence_root / relative_video).resolve(strict=True)
        self._video_path.relative_to(evidence_root)
        self._provider = provider

    async def run(
        self,
        source_uri: str,
        consumer: Callable[[PresentationVideoFrame], Awaitable[None]],
    ) -> LivePresentationSnapshot:
        import cv2

        if not source_uri or not callable(consumer):
            raise ValueError("joined witness delivery input is invalid")

        capture = cv2.VideoCapture(str(self._video_path))
        if not capture.isOpened():
            raise RuntimeError("reviewed validation video could not be opened")

        delivered_frames = 0
        delivered_frame_bytes = 0
        source_span_ms = 0
        detection_seen_at: int | None = None
        try:
            for frame_index in range(_MAX_FRAMES):
                ok, image = capture.read()
                if not ok:
                    break
                height, width = image.shape[:2]
                bgrx = cv2.cvtColor(image, cv2.COLOR_BGR2BGRA)
                bgrx[:, :, 3] = 0
                payload = bytearray(bgrx.tobytes())
                source_elapsed_ms = frame_index * 100 + 1
                frame = PresentationVideoFrame(
                    payload=memoryview(payload),
                    width=width,
                    height=height,
                    stride_bytes=width * 4,
                    pixel_format=PixelFormat.BGRX,
                    source_elapsed_ms=source_elapsed_ms,
                )
                await consumer(frame)
                delivered_frames += 1
                delivered_frame_bytes += len(payload)
                source_span_ms = source_elapsed_ms

                await asyncio.sleep(0.1)
                if self._provider.tracked_detections > 0:
                    if detection_seen_at is None:
                        detection_seen_at = delivered_frames
                    elif delivered_frames >= detection_seen_at + 3:
                        break
        finally:
            capture.release()

        if delivered_frames < 1:
            raise RuntimeError("reviewed validation video emitted no frames")

        return LivePresentationSnapshot(
            state=LivePresentationState.COMPLETE,
            decoder_initialized=True,
            accepted_packets=0,
            rtp_valid_packets=0,
            rtp_invalid_packets=0,
            rtp_delivered_bytes=0,
            delivered_frames=delivered_frames,
            delivered_frame_bytes=delivered_frame_bytes,
            source_span_ms=source_span_ms,
        )


def test_reviewed_video_detector_tracker_overlay_reaches_windows_operator() -> None:
    root_value = os.getenv("K5_ANALYTICS_EVIDENCE_ROOT")
    assert root_value
    evidence_root = Path(root_value).resolve(strict=True)
    provider = _JoinedAnalyticsProvider(evidence_root)

    def delivery_factory(payload_type: int) -> _ReviewedVideoDelivery:
        assert 96 <= payload_type <= 127
        return _ReviewedVideoDelivery(evidence_root, provider)

    launcher = WindowsSingleLiveOperatorLauncher(
        delivery_factory=delivery_factory,
        detection_provider=provider,
    )
    metrics = asyncio.run(
        launcher.run(
            ResolvedLiveSource("rtsp://127.0.0.1/joined-analytics-witness", 96),
            width=1280,
            height=720,
        )
    )

    assert metrics.delivered_frames >= 1
    assert metrics.presentations >= 1
    assert metrics.analytics_enabled is True
    assert metrics.analytics_provider_submissions >= 1
    assert metrics.analytics_provider_completions >= 1
    assert metrics.analytics_failures == 0
    assert provider.tracked_detections >= 1
    assert metrics.analytics_rendered_boxes >= 1
