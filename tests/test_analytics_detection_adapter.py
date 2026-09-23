import math
from dataclasses import dataclass

import pytest

from k5vision.media.analytics_detection_adapter import (
    AnalyticsDetectionAdapterError,
    adapt_analytics_tracked_detections,
)


@dataclass(frozen=True)
class _Box:
    x_min: float
    y_min: float
    x_max: float
    y_max: float


@dataclass(frozen=True)
class _Tracked:
    track_id: str
    category: str
    confidence: float
    box: _Box
    model_class_id: str | int | None = None


def _tracked(*, track_id: str = "session-track-1") -> _Tracked:
    return _Tracked(
        track_id=track_id,
        category="person",
        confidence=0.91,
        box=_Box(0.1, 0.2, 0.8, 0.9),
        model_class_id=0,
    )


def test_adapter_converts_only_transient_overlay_fields() -> None:
    source = _tracked(track_id="private-session-local-track")

    observations = adapt_analytics_tracked_detections((source,))

    assert len(observations) == 1
    observation = observations[0]
    assert observation.category == "person"
    assert observation.confidence == 0.91
    assert (observation.x_min, observation.y_min, observation.x_max, observation.y_max) == (
        0.1,
        0.2,
        0.8,
        0.9,
    )
    assert not hasattr(observation, "track_id")
    assert not hasattr(observation, "model_class_id")


def test_adapter_bounds_input_count_before_conversion() -> None:
    with pytest.raises(AnalyticsDetectionAdapterError, match="count"):
        adapt_analytics_tracked_detections((_tracked(), _tracked()), max_observations=1)


def test_adapter_fails_closed_with_sanitized_invalid_value() -> None:
    marker = "private-track-marker"
    invalid = _Tracked(
        track_id=marker,
        category="person",
        confidence=math.nan,
        box=_Box(0.1, 0.1, 0.9, 0.9),
    )

    with pytest.raises(AnalyticsDetectionAdapterError) as caught:
        adapt_analytics_tracked_detections((invalid,))

    assert str(caught.value) == "analytics detection value is invalid"
    assert marker not in str(caught.value)


def test_adapter_configuration_is_bounded() -> None:
    with pytest.raises(ValueError, match="max_observations"):
        adapt_analytics_tracked_detections((), max_observations=0)
    with pytest.raises(ValueError, match="max_observations"):
        adapt_analytics_tracked_detections((), max_observations=513)
