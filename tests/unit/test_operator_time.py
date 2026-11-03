"""Clock-step evidence must be bounded, session-local, and never assert NTP sync."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from k5vision.operator_time import (
    MAX_CLOCK_CHANGE_COUNT,
    MAX_CLOCK_STEP_SECONDS,
    HostClockObserver,
)


def _clock():
    samples = SimpleNamespace(wall=datetime(2026, 10, 18, tzinfo=UTC), monotonic=100.0)
    observer = HostClockObserver(utc_now=lambda: samples.wall, monotonic=lambda: samples.monotonic)
    return observer, samples


def _advance(samples, *, elapsed=2.0, step=0.0):
    samples.wall += timedelta(seconds=elapsed + step)
    samples.monotonic += elapsed


def test_default_observer_reports_current_aware_host_utc():
    before = datetime.now(UTC)
    result = HostClockObserver().snapshot()
    after = datetime.now(UTC)
    observed = datetime.fromisoformat(result["server_utc"].replace("Z", "+00:00"))
    assert observed.utcoffset() == timedelta(0)
    assert before - timedelta(milliseconds=1) <= observed <= after
    assert result["clock"]["sync_verified"] is False


def test_elapsed_time_and_timezone_do_not_establish_ntp_synchronization():
    observer, samples = _clock()
    _advance(samples, elapsed=3600)
    samples.wall = samples.wall.astimezone(timezone(timedelta(hours=9)))
    result = observer.snapshot()
    assert result == {
        "server_utc": "2026-10-18T01:00:00.000Z",
        "clock": {
            "status": "observing",
            "last_step_seconds": 0.0,
            "change_count": 0,
            "last_change_at": None,
            "sync_verified": False,
        },
    }


@pytest.mark.parametrize("step", [-30, -2, 2, 30])
def test_forward_and_backward_steps_are_latched_until_process_restart(step):
    observer, samples = _clock()
    _advance(samples, step=step)
    changed = observer.snapshot()
    assert changed["clock"]["status"] == "changed"
    assert changed["clock"]["last_step_seconds"] == step
    assert changed["clock"]["change_count"] == 1
    assert changed["clock"]["last_change_at"] == changed["server_utc"]
    _advance(samples, elapsed=120)
    assert observer.snapshot()["clock"] == changed["clock"]
    restarted = HostClockObserver(utc_now=lambda: samples.wall, monotonic=lambda: samples.monotonic)
    assert restarted.snapshot()["clock"]["status"] == "observing"


def test_subthreshold_drift_is_not_mislabeled_as_a_clock_step():
    observer, samples = _clock()
    for step in (-1.999, 0, 1.999):
        _advance(samples, step=step)
        assert observer.snapshot()["clock"]["status"] == "observing"


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), True, "100", 99.0])
def test_invalid_or_rewound_monotonic_sample_is_unavailable_and_can_recover(invalid):
    observer, samples = _clock()
    samples.monotonic = invalid
    result = observer.snapshot()
    assert result["clock"]["status"] == "unavailable"
    assert result["clock"]["sync_verified"] is False
    assert result["server_utc"] == "2026-10-18T00:00:00.000Z"
    json.dumps(result, allow_nan=False)
    samples.monotonic = 1.0
    assert observer.snapshot()["clock"]["status"] == "observing"


@pytest.mark.parametrize("invalid", [None, "private-clock-fixture", datetime(2026, 10, 18)])
def test_invalid_wall_time_is_not_invented_or_exposed(invalid):
    observer, samples = _clock()
    samples.wall = invalid
    result = observer.snapshot()
    assert result["server_utc"] is None
    assert result["clock"]["status"] == "unavailable"
    assert "private-clock-fixture" not in json.dumps(result)


def test_failed_observation_preserves_prior_change_evidence():
    observer, samples = _clock()
    _advance(samples, step=-5)
    evidence = observer.snapshot()["clock"]
    wall = samples.wall
    samples.wall = None
    failed = observer.snapshot()["clock"]
    assert failed == {**evidence, "status": "unavailable"}
    samples.wall = wall
    assert observer.snapshot()["clock"] == evidence


def test_sampler_exceptions_are_finite_unavailable_diagnostics():
    def unavailable():
        raise RuntimeError("private-clock-fixture")

    observer = HostClockObserver(utc_now=unavailable)
    result = observer.snapshot()
    assert result["server_utc"] is None
    assert result["clock"]["status"] == "unavailable"
    assert "private-clock-fixture" not in json.dumps(result, allow_nan=False)


def test_extreme_steps_and_change_count_are_bounded_without_losing_alarm():
    observer, samples = _clock()
    observer._change_count = MAX_CLOCK_CHANGE_COUNT - 1
    samples.wall = datetime.min.replace(tzinfo=UTC)
    samples.monotonic += 2
    negative = observer.snapshot()
    assert negative["clock"]["last_step_seconds"] == -MAX_CLOCK_STEP_SECONDS
    samples.wall = datetime.max.replace(tzinfo=UTC)
    samples.monotonic += 2
    positive = observer.snapshot()
    assert positive["clock"]["last_step_seconds"] == MAX_CLOCK_STEP_SECONDS
    assert positive["clock"]["change_count"] == MAX_CLOCK_CHANGE_COUNT
    assert positive["clock"]["status"] == "changed"
    json.dumps(positive, allow_nan=False)


def test_concurrent_samples_are_serialized_and_return_independent_snapshots():
    samples = SimpleNamespace(counter=0)
    epoch = datetime(2026, 10, 18, tzinfo=UTC)

    def utc_now():
        samples.counter += 1
        return epoch + timedelta(seconds=samples.counter)

    observer = HostClockObserver(utc_now=utc_now, monotonic=lambda: samples.counter)
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: observer.snapshot(), range(128)))
    assert samples.counter == 129
    assert all(result["clock"]["status"] == "observing" for result in results)
    assert len({result["server_utc"] for result in results}) == 128
    results[0]["clock"]["change_count"] = MAX_CLOCK_CHANGE_COUNT
    assert observer.snapshot()["clock"]["change_count"] == 0
