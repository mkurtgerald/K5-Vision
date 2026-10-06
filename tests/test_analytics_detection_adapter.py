import math
from collections.abc import Iterator
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


@pytest.mark.parametrize("count", (0, 1, 3))
@pytest.mark.parametrize("container", ("tuple", "list", "iterator", "generator"))
def test_adapter_accepts_finite_inputs_through_exact_limit(count: int, container: str) -> None:
    items = [_tracked() for _ in range(count)]
    values = {
        "tuple": tuple(items),
        "list": items,
        "iterator": iter(items),
        "generator": (item for item in items),
    }[container]

    observations = adapt_analytics_tracked_detections(values, max_observations=3)

    assert len(observations) == count
    assert all(observation.category == "person" for observation in observations)


@pytest.mark.parametrize("limit", (1, 128, 512))
def test_adapter_stops_at_overflow_before_converting_any_detection(limit: int) -> None:
    consumed = 0

    class UnconvertedDetection:
        @property
        def track_id(self) -> str:
            raise AssertionError("overflow must be rejected before detection conversion")

    def values() -> Iterator[UnconvertedDetection]:
        nonlocal consumed
        for _ in range(limit + 1):
            consumed += 1
            yield UnconvertedDetection()
        raise AssertionError("producer advanced beyond the overflow sentinel")

    with pytest.raises(AnalyticsDetectionAdapterError, match="count"):
        adapt_analytics_tracked_detections(values(), max_observations=limit)

    assert consumed == limit + 1


def test_adapter_does_not_consult_producer_length() -> None:
    class UntrustedLength:
        def __iter__(self) -> Iterator[_Tracked]:
            return iter((_tracked(),))

        def __len__(self) -> int:
            raise AssertionError("untrusted producer length must not be requested")

    assert len(adapt_analytics_tracked_detections(UntrustedLength(), max_observations=1)) == 1


def test_adapter_does_not_consult_producer_length_hint() -> None:
    class UntrustedLengthHint:
        def __init__(self) -> None:
            self._values = iter((_tracked(),))

        def __iter__(self) -> Iterator[_Tracked]:
            return self

        def __next__(self) -> _Tracked:
            return next(self._values)

        def __length_hint__(self) -> int:
            raise AssertionError("untrusted producer length hint must not be requested")

    assert len(adapt_analytics_tracked_detections(UntrustedLengthHint(), max_observations=1)) == 1


@pytest.mark.parametrize("values", (None, 1, object()))
def test_adapter_preserves_sanitized_noniterable_error(values: object) -> None:
    with pytest.raises(AnalyticsDetectionAdapterError, match="must be iterable"):
        adapt_analytics_tracked_detections(values)  # type: ignore[arg-type]


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
