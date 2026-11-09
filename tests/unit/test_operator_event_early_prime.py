"""The installed person model prepares before camera analytics is enabled.

Only a sealed model (no recording bytes or camera source) is eligible to prime.
Neither a disabled model nor a refused native runtime may be started.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
from uuid import uuid4

import pytest

from k5vision import operator_events


@pytest.mark.parametrize(
    ("configured", "unavailable", "expected_primes"),
    [(True, False, 1), (True, True, 0), (False, False, 0)],
)
def test_recorded_person_model_primes_on_service_start(
    monkeypatch, tmp_path, configured, unavailable, expected_primes
):
    primed = []
    closed = []
    configurations = []

    class StubSampler:
        def __init__(self):
            self._configuration = None

        def prime(self):
            # There are no cameras or recorded frames in this test.
            assert self._configuration is configurations[0]
            primed.append(True)

        def prune(self, active):
            assert active == set()

        def begin_close(self):
            pass

        def close(self):
            closed.append("sampler")

    class Cameras:
        def list_sources(self):
            return ()

    class Store:
        def close(self):
            closed.append("store")

    monkeypatch.setattr(operator_events, "RecordedFrameSampler", StubSampler)
    configuration = object() if configured else None
    configurations.append(configuration)
    service = operator_events.OperatorEventService(
        store=Store(),
        cameras=Cameras(),
        recording_root=tmp_path,
        configuration=configuration,
        unavailable=unavailable,
        sampler=StubSampler(),
    )

    async def exercise():
        try:
            await service.start()
            pending = service._native_pending
            assert (pending is not None) is bool(expected_primes)
            if pending is not None:
                await asyncio.wait_for(asyncio.wrap_future(pending), 5)
            # A duplicate start cannot launch another warmup or sampling task.
            await service.start()
        finally:
            await service.aclose()

    asyncio.run(exercise())
    assert len(primed) == expected_primes
    assert closed == ["sampler", "store"]


@pytest.mark.parametrize(
    ("prior_prime", "enabled", "worker_ready", "expected_primes"),
    [
        ("completed", True, False, 1),
        ("completed", True, True, 0),
        ("pending", True, False, 0),
        ("none", True, False, 1),
        ("completed", False, False, 0),
    ],
)
def test_enabling_analytics_retries_only_completed_failed_recorder_prime(
    monkeypatch, tmp_path, prior_prime, enabled, worker_ready, expected_primes
):
    """A failed startup prewarm cannot force the first recording into a cold path."""

    class Sampler:
        def __init__(self):
            self._configuration = None
            self.primes = 0

        def native_model_ready(self):
            return worker_ready

        def prime(self):
            # Only a model-preparation signal; no camera, frame, or recording.
            self.primes += 1

        def begin_close(self):
            pass

        def close(self):
            pass

    class Cameras:
        def list_sources(self):
            return ()

    class Store:
        def __init__(self):
            self.values = {"enabled": False, "confidence_threshold": 0.65}

        def configure(self, camera_id, *, enabled, confidence_threshold):
            self.values = {"enabled": enabled, "confidence_threshold": confidence_threshold}

        def settings(self, camera_id):
            return self.values

        def summary(self, camera_id):
            return {"event_count": 0, "last_event_at": None}

        def close(self):
            pass

    monkeypatch.setattr(operator_events, "RecordedFrameSampler", Sampler)
    sampler = Sampler()
    service = operator_events.OperatorEventService(
        store=Store(),
        cameras=Cameras(),
        recording_root=tmp_path,
        configuration=object(),
        sampler=sampler,
    )
    previous = Future()
    if prior_prime != "none":
        service._native_pending = previous
    if prior_prime == "completed":
        previous.set_result(None)
    try:
        status = service.configure(uuid4(), enabled=enabled, confidence_threshold=0.7)
        assert status["enabled"] is enabled
        if expected_primes:
            assert service._native_pending is not previous
            service._native_pending.result(timeout=5)
        else:
            assert (service._native_pending is previous) is (prior_prime != "none")
        assert sampler.primes == expected_primes
    finally:
        if prior_prime == "pending":
            previous.set_result(None)
        asyncio.run(service.aclose())
