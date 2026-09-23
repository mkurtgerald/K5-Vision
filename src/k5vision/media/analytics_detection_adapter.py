"""Bounded product-owned adapter from analytics tracking output to operator overlays.

Analytics remains an untrusted optional producer. This boundary accepts only the
small normalized geometry needed for transient presentation; producer track IDs,
model identifiers, source identity, credentials, paths, and media are not retained
or propagated into the overlay contract.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from k5vision.media.detection_overlay import (
    DetectionOverlayError,
    DetectionOverlayObservation,
)

_MAX_TRACKED_OBSERVATIONS = 512


class AnalyticsDetectionAdapterError(DetectionOverlayError):
    """Sanitized failure at the optional analytics-to-presentation boundary."""


def adapt_analytics_tracked_detections(
    values: Iterable[Any],
    *,
    max_observations: int = 128,
) -> tuple[DetectionOverlayObservation, ...]:
    """Convert bounded Analytics-lab tracked detections into transient boxes.

    The adapter intentionally uses structural validation instead of importing the
    analytics package at product runtime. Exact producer compatibility is qualified
    separately against a pinned Analytics-lab revision. Producer scope is never
    trusted here; site/source/session authority remains product-owned upstream.
    """
    if type(max_observations) is not int or not 1 <= max_observations <= _MAX_TRACKED_OBSERVATIONS:
        raise ValueError(f"max_observations must be between 1 and {_MAX_TRACKED_OBSERVATIONS}")

    try:
        selected = tuple(values)
    except TypeError as exc:
        raise AnalyticsDetectionAdapterError("analytics detections must be iterable") from exc
    if len(selected) > max_observations:
        raise AnalyticsDetectionAdapterError("analytics detection count exceeds configured bound")

    observations: list[DetectionOverlayObservation] = []
    for value in selected:
        try:
            track_id = value.track_id
            category = value.category
            confidence = value.confidence
            box = value.box
            coordinates = (box.x_min, box.y_min, box.x_max, box.y_max)
        except Exception as exc:
            raise AnalyticsDetectionAdapterError("analytics detection value is invalid") from exc

        if not isinstance(track_id, str) or not track_id or len(track_id) > 128:
            raise AnalyticsDetectionAdapterError("analytics detection value is invalid")

        try:
            observation = DetectionOverlayObservation(
                category=category,
                confidence=confidence,
                x_min=coordinates[0],
                y_min=coordinates[1],
                x_max=coordinates[2],
                y_max=coordinates[3],
            )
        except (DetectionOverlayError, TypeError, ValueError) as exc:
            raise AnalyticsDetectionAdapterError("analytics detection value is invalid") from exc
        observations.append(observation)

    return tuple(observations)
