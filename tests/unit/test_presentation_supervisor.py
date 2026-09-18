"""Regression coverage for bounded operator presentation replacement control."""

from __future__ import annotations

import asyncio

import pytest

from k5vision.media.presentation_host import (
    PresentationHostSnapshot,
    PresentationHostState,
)
from k5vision.media.presentation_supervisor import (
    BoundedPresentationSupervisor,
    PresentationSupervisorError,
    PresentationSupervisorErrorCode,
    PresentationSupervisorState,
)


def _snapshot(
    state: PresentationHostState,
    *,
    generation: int = 0,
    completed: int = 0,
    stopped: int = 0,
    failures: int = 0,
    frames: int = 0,
) -> PresentationHostSnapshot:
    active = generation > 0
    return PresentationHostSnapshot(
        state=state,
        generation=generation,
        generation_limit=8,
        completed_generations=completed,
        stopped_generations=stopped,
        failures=failures,
        stream_count=2 if active else 0,
        live_streams=1 if active else 0,
        playback_streams=1 if active else 0,
        viewport_count=2 if active else 0,
        delivered_frames=frames,
        delivered_frame_bytes=frames * 1024,
        max_source_span_ms=250 if frames else 0,
    )


class _FakeHost:
    def __init__(
        self,
        *,
        fail_start_on: int | None = None,
        fail_wait: bool = False,
        fail_stop: bool = False,
        fail_close: bool = False,
    ) -> None:
        self._snapshot = _snapshot(PresentationHostState.READY)
        self.fail_start_on = fail_start_on
        self.fail_wait = fail_wait
        self.fail_stop = fail_stop
        self.fail_close = fail_close
        self.events: list[str] = []

    @property
    def snapshot(self) -> PresentationHostSnapshot:
        return self._snapshot

    async def start(self, streams: object) -> PresentationHostSnapshot:
        del streams
        next_generation = self._snapshot.generation + 1
        self.events.append(f"start:{next_generation}")
        if self.fail_start_on == next_generation:
            raise RuntimeError("rtsp://private password=secret")
        self._snapshot = _snapshot(
            PresentationHostState.RUNNING,
            generation=next_generation,
            completed=self._snapshot.completed_generations,
            stopped=self._snapshot.stopped_generations,
            failures=self._snapshot.failures,
            frames=self._snapshot.delivered_frames,
        )
        return self._snapshot

    async def wait(self) -> PresentationHostSnapshot:
        self.events.append(f"wait:{self._snapshot.generation}")
        if self.fail_wait:
            raise RuntimeError("credential private-path")
        self._snapshot = _snapshot(
            PresentationHostState.COMPLETE,
            generation=self._snapshot.generation,
            completed=self._snapshot.completed_generations + 1,
            stopped=self._snapshot.stopped_generations,
            failures=self._snapshot.failures,
            frames=self._snapshot.delivered_frames + 4,
        )
        return self._snapshot

    async def stop(self) -> PresentationHostSnapshot:
        self.events.append(f"stop:{self._snapshot.generation}")
        if self.fail_stop:
            raise RuntimeError("runner_name=private")
        self._snapshot = _snapshot(
            PresentationHostState.STOPPED,
            generation=self._snapshot.generation,
            completed=self._snapshot.completed_generations,
            stopped=self._snapshot.stopped_generations + 1,
            failures=self._snapshot.failures,
            frames=self._snapshot.delivered_frames + 1,
        )
        return self._snapshot

    async def close(self) -> PresentationHostSnapshot:
        self.events.append("close")
        if self.fail_close:
            raise RuntimeError("C:\\Users\\private")
        current = self._snapshot
        self._snapshot = _snapshot(
            PresentationHostState.CLOSED,
            generation=current.generation,
            completed=current.completed_generations,
            stopped=current.stopped_generations,
            failures=current.failures,
            frames=current.delivered_frames,
        )
        return self._snapshot


def test_present_then_active_replace_stops_before_fresh_start() -> None:
    async def exercise() -> None:
        host = _FakeHost()
        supervisor = BoundedPresentationSupervisor(host, max_replacements=2)
        first = await supervisor.present(())
        assert first.state == PresentationSupervisorState.RUNNING
        assert first.generation == 1

        replaced = await supervisor.replace(())
        assert replaced.state == PresentationSupervisorState.RUNNING
        assert replaced.generation == 2
        assert replaced.replacements == 1
        assert replaced.stopped_generations == 1
        assert host.events[:3] == ["start:1", "stop:1", "start:2"]

        done = await supervisor.wait()
        assert done.state == PresentationSupervisorState.COMPLETE
        assert done.completed_generations == 1
        assert done.delivered_frames == 5
        assert (await supervisor.close()).state == PresentationSupervisorState.CLOSED

    asyncio.run(exercise())


def test_replace_after_terminal_generation_does_not_issue_redundant_stop() -> None:
    async def exercise() -> None:
        host = _FakeHost()
        supervisor = BoundedPresentationSupervisor(host)
        await supervisor.present(())
        await supervisor.wait()
        await supervisor.replace(())
        assert host.events[:3] == ["start:1", "wait:1", "start:2"]
        assert supervisor.snapshot.replacements == 1
        await supervisor.stop()
        await supervisor.close()

    asyncio.run(exercise())


def test_replacement_limit_fails_closed_without_starting_another_generation() -> None:
    async def exercise() -> None:
        host = _FakeHost()
        supervisor = BoundedPresentationSupervisor(host, max_replacements=1)
        await supervisor.present(())
        await supervisor.replace(())
        before = tuple(host.events)
        with pytest.raises(PresentationSupervisorError) as exc:
            await supervisor.replace(())
        assert exc.value.code == PresentationSupervisorErrorCode.REPLACEMENT_LIMIT
        assert tuple(host.events) == before
        await supervisor.close()

    asyncio.run(exercise())


def test_start_and_replacement_failures_are_sanitized() -> None:
    async def initial_failure() -> None:
        host = _FakeHost(fail_start_on=1)
        supervisor = BoundedPresentationSupervisor(host)
        with pytest.raises(PresentationSupervisorError) as exc:
            await supervisor.present(())
        assert exc.value.code == PresentationSupervisorErrorCode.START_FAILURE
        assert "password" not in str(exc.value).casefold()
        assert supervisor.snapshot.state == PresentationSupervisorState.FAILED

    async def replacement_failure() -> None:
        host = _FakeHost(fail_start_on=2)
        supervisor = BoundedPresentationSupervisor(host)
        await supervisor.present(())
        with pytest.raises(PresentationSupervisorError) as exc:
            await supervisor.replace(())
        assert exc.value.code == PresentationSupervisorErrorCode.REPLACEMENT_FAILURE
        assert "rtsp" not in str(exc.value).casefold()
        assert supervisor.snapshot.state == PresentationSupervisorState.FAILED

    asyncio.run(initial_failure())
    asyncio.run(replacement_failure())


def test_stop_failure_is_sanitized_during_replacement() -> None:
    async def exercise() -> None:
        host = _FakeHost(fail_stop=True)
        supervisor = BoundedPresentationSupervisor(host)
        await supervisor.present(())
        with pytest.raises(PresentationSupervisorError) as exc:
            await supervisor.replace(())
        assert exc.value.code == PresentationSupervisorErrorCode.REPLACEMENT_FAILURE
        assert "runner" not in str(exc.value).casefold()
        assert supervisor.snapshot.state == PresentationSupervisorState.FAILED

    asyncio.run(exercise())


def test_wait_failure_is_sanitized() -> None:
    async def exercise() -> None:
        host = _FakeHost(fail_wait=True)
        supervisor = BoundedPresentationSupervisor(host)
        await supervisor.present(())
        with pytest.raises(PresentationSupervisorError) as exc:
            await supervisor.wait()
        assert exc.value.code == PresentationSupervisorErrorCode.EXECUTION_FAILURE
        assert "credential" not in str(exc.value).casefold()
        assert supervisor.snapshot.state == PresentationSupervisorState.FAILED

    asyncio.run(exercise())


def test_stop_and_close_are_bounded_and_close_is_idempotent() -> None:
    async def exercise() -> None:
        host = _FakeHost()
        supervisor = BoundedPresentationSupervisor(host)
        await supervisor.present(())
        stopped = await supervisor.stop()
        assert stopped.state == PresentationSupervisorState.STOPPED
        assert stopped.stopped_generations == 1
        assert (await supervisor.stop()) == stopped
        closed = await supervisor.close()
        assert closed.state == PresentationSupervisorState.CLOSED
        assert (await supervisor.close()) == closed

    asyncio.run(exercise())


def test_cleanup_failure_and_invalid_configuration_are_sanitized() -> None:
    with pytest.raises(PresentationSupervisorError) as exc:
        BoundedPresentationSupervisor(object())
    assert exc.value.code == PresentationSupervisorErrorCode.INVALID_CONFIGURATION

    with pytest.raises(PresentationSupervisorError):
        BoundedPresentationSupervisor(_FakeHost(), max_replacements=0)

    with pytest.raises(PresentationSupervisorError):
        BoundedPresentationSupervisor(_FakeHost(), transition_timeout_seconds=0)

    async def exercise() -> None:
        host = _FakeHost(fail_close=True)
        supervisor = BoundedPresentationSupervisor(host)
        with pytest.raises(PresentationSupervisorError) as exc:
            await supervisor.stop()
        assert exc.value.code == PresentationSupervisorErrorCode.INVALID_STATE
        await supervisor.present(())
        with pytest.raises(PresentationSupervisorError) as exc:
            await supervisor.close()
        assert exc.value.code == PresentationSupervisorErrorCode.CLEANUP_FAILURE
        assert "users" not in str(exc.value).casefold()

    asyncio.run(exercise())
