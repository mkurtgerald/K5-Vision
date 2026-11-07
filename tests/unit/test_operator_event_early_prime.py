"""The installed person model prepares before camera analytics is enabled.

Only a sealed model (no recording bytes or camera source) is eligible to prime.
Neither a disabled model nor a refused native runtime may be started.
"""

from __future__ import annotations

import asyncio

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
