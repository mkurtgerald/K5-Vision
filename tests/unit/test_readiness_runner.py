from __future__ import annotations

import asyncio

from k5vision.media.readiness import ReadinessOutcome, ReadinessPlan
from k5vision.media.readiness_runner import qualify_readiness


class FakeRuntime:
    def __init__(
        self,
        *,
        fail_start: bool = False,
        fail_stop: bool = False,
        fail_close: bool = False,
    ) -> None:
        self.fail_start = fail_start
        self.fail_stop = fail_stop
        self.fail_close = fail_close
        self.starts = 0
        self.stops = 0
        self.closes = 0

    async def start(self, source_uri: str) -> None:
        self.starts += 1
        if self.fail_start:
            raise RuntimeError(f"unsafe source detail: {source_uri}")

    async def stop(self) -> None:
        self.stops += 1
        if self.fail_stop:
            raise RuntimeError("unsafe stop detail")

    async def close(self) -> None:
        self.closes += 1
        if self.fail_close:
            raise RuntimeError("unsafe close detail")


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def test_runner_executes_complete_increasing_workload_without_source_retention() -> None:
    created: list[FakeRuntime] = []

    def factory() -> FakeRuntime:
        runtime = FakeRuntime()
        created.append(runtime)
        return runtime

    async def scenario() -> None:
        source = "rtsp://username:password@example.invalid/stream"
        plan = ReadinessPlan(cycles_per_level=2, concurrency_ladder=(1, 2))
        evidence = await qualify_readiness(
            factory,
            source,
            revision="a" * 40,
            plan=plan,
        )
        assert evidence.accepted
        assert [item.level for item in evidence.observations] == [1, 1, 2, 2]
        assert all(item.outcome == ReadinessOutcome.PASS for item in evidence.observations)
        payload = evidence.model_dump_json()
        assert source not in payload
        assert "username" not in payload
        assert "password" not in payload

    run(scenario())
    assert len(created) == 6
    assert all(runtime.starts == 1 for runtime in created)
    assert all(runtime.stops == 1 for runtime in created)
    assert all(runtime.closes == 1 for runtime in created)


def test_runner_normalizes_start_failure_and_recovers_failed_session() -> None:
    created: list[FakeRuntime] = []

    def factory() -> FakeRuntime:
        runtime = FakeRuntime(fail_start=len(created) == 0)
        created.append(runtime)
        return runtime

    async def scenario() -> None:
        evidence = await qualify_readiness(
            factory,
            "rtsp://secret@example.invalid/stream",
            revision="b" * 40,
            plan=ReadinessPlan(cycles_per_level=2, concurrency_ladder=(1, 2)),
        )
        assert not evidence.accepted
        assert evidence.observations[0].outcome == ReadinessOutcome.START_FAILURE
        assert evidence.observations[0].completed_sessions == 0

    run(scenario())
    assert created[0].closes == 2


def test_runner_normalizes_stop_failure_and_performs_recovery_cleanup() -> None:
    created: list[FakeRuntime] = []

    def factory() -> FakeRuntime:
        runtime = FakeRuntime(fail_stop=len(created) == 0)
        created.append(runtime)
        return runtime

    async def scenario() -> None:
        evidence = await qualify_readiness(
            factory,
            "rtsp://secret@example.invalid/stream",
            revision="c" * 40,
            plan=ReadinessPlan(cycles_per_level=2, concurrency_ladder=(1, 2)),
        )
        assert not evidence.accepted
        assert evidence.observations[0].outcome == ReadinessOutcome.STOP_FAILURE
        assert evidence.observations[0].completed_sessions == 1

    run(scenario())
    assert created[0].closes == 2


def test_runner_reports_recovery_failure_without_raw_runtime_detail() -> None:
    created: list[FakeRuntime] = []

    def factory() -> FakeRuntime:
        runtime = FakeRuntime(
            fail_start=len(created) == 0,
            fail_close=len(created) == 0,
        )
        created.append(runtime)
        return runtime

    async def scenario() -> None:
        evidence = await qualify_readiness(
            factory,
            "rtsp://username:password@example.invalid/stream",
            revision="d" * 40,
            plan=ReadinessPlan(cycles_per_level=2, concurrency_ladder=(1, 2)),
        )
        assert evidence.observations[0].outcome == ReadinessOutcome.RECOVERY_FAILURE
        payload = evidence.model_dump_json()
        assert "unsafe" not in payload
        assert "username" not in payload
        assert "password" not in payload

    run(scenario())


def test_runner_rejects_empty_source_before_creating_runtime() -> None:
    created = 0

    def factory() -> FakeRuntime:
        nonlocal created
        created += 1
        return FakeRuntime()

    async def scenario() -> None:
        try:
            await qualify_readiness(factory, "   ", revision="e" * 40)
        except ValueError as exc:
            assert str(exc) == "source_uri must not be empty"
        else:
            raise AssertionError("empty source should be rejected")

    run(scenario())
    assert created == 0
