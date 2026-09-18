"""Regression coverage for the bounded re-entrant presentation host."""

from __future__ import annotations

import asyncio

import pytest

from k5vision.media.presentation_host import (
    BoundedPresentationHost,
    PresentationHostError,
    PresentationHostErrorCode,
    PresentationHostState,
)
from k5vision.media.presentation_runtime import (
    PresentationRuntimeSnapshot,
    PresentationRuntimeState,
)


def _snapshot(
    state: PresentationRuntimeState,
    *,
    frames: int = 0,
    frame_bytes: int = 0,
) -> PresentationRuntimeSnapshot:
    return PresentationRuntimeSnapshot(
        state=state,
        stream_count=2 if state != PresentationRuntimeState.CREATED else 0,
        live_streams=1 if state != PresentationRuntimeState.CREATED else 0,
        playback_streams=1 if state != PresentationRuntimeState.CREATED else 0,
        viewport_count=2 if state != PresentationRuntimeState.CREATED else 0,
        completed_streams=2 if state == PresentationRuntimeState.COMPLETE else 0,
        delivered_frames=frames,
        delivered_frame_bytes=frame_bytes,
        max_source_span_ms=250 if frames else 0,
    )


class _FakeRuntime:
    def __init__(
        self,
        *,
        fail_start: bool = False,
        fail_wait: bool = False,
        fail_stop: bool = False,
        fail_close: bool = False,
    ) -> None:
        self._snapshot = _snapshot(PresentationRuntimeState.CREATED)
        self.fail_start = fail_start
        self.fail_wait = fail_wait
        self.fail_stop = fail_stop
        self.fail_close = fail_close
        self.close_calls = 0

    @property
    def snapshot(self) -> PresentationRuntimeSnapshot:
        return self._snapshot

    async def start(self, streams: object) -> PresentationRuntimeSnapshot:
        del streams
        if self.fail_start:
            raise RuntimeError("rtsp://private password=secret")
        self._snapshot = _snapshot(PresentationRuntimeState.RUNNING, frames=1, frame_bytes=1024)
        return self._snapshot

    async def wait(self) -> PresentationRuntimeSnapshot:
        if self.fail_wait:
            raise RuntimeError("private-path credential secret")
        self._snapshot = _snapshot(PresentationRuntimeState.COMPLETE, frames=4, frame_bytes=4096)
        return self._snapshot

    async def stop(self) -> PresentationRuntimeSnapshot:
        if self.fail_stop:
            raise RuntimeError("runner_name=private")
        self._snapshot = _snapshot(PresentationRuntimeState.STOPPED, frames=2, frame_bytes=2048)
        return self._snapshot

    async def close(self) -> PresentationRuntimeSnapshot:
        self.close_calls += 1
        if self.fail_close:
            raise RuntimeError("C:\\Users\\private")
        current = self._snapshot
        self._snapshot = PresentationRuntimeSnapshot(
            state=PresentationRuntimeState.CLOSED,
            stream_count=current.stream_count,
            live_streams=current.live_streams,
            playback_streams=current.playback_streams,
            viewport_count=current.viewport_count,
            completed_streams=current.completed_streams,
            delivered_frames=current.delivered_frames,
            delivered_frame_bytes=current.delivered_frame_bytes,
            max_source_span_ms=current.max_source_span_ms,
        )
        return self._snapshot


def test_host_reenters_with_fresh_runtime_and_aggregates_counters() -> None:
    created: list[_FakeRuntime] = []

    def factory() -> _FakeRuntime:
        runtime = _FakeRuntime()
        created.append(runtime)
        return runtime

    async def exercise() -> None:
        host = BoundedPresentationHost(factory, max_generations=2)
        first = await host.start(())
        assert first.state == PresentationHostState.RUNNING
        assert first.generation == 1
        assert (await host.wait()).state == PresentationHostState.COMPLETE

        second = await host.start(())
        assert second.state == PresentationHostState.RUNNING
        assert second.generation == 2
        done = await host.wait()
        assert done.completed_generations == 2
        assert done.delivered_frames == 8
        assert done.delivered_frame_bytes == 8192
        assert done.max_source_span_ms == 250
        assert len(created) == 2
        assert created[0].close_calls == 1

        closed = await host.close()
        assert closed.state == PresentationHostState.CLOSED
        assert closed.completed_generations == 2
        assert closed.delivered_frames == 8
        assert created[1].close_calls == 1
        assert (await host.close()) == closed

    asyncio.run(exercise())


def test_host_enforces_generation_limit() -> None:
    async def exercise() -> None:
        host = BoundedPresentationHost(_FakeRuntime, max_generations=1)
        await host.start(())
        await host.wait()
        with pytest.raises(PresentationHostError) as exc:
            await host.start(())
        assert exc.value.code == PresentationHostErrorCode.GENERATION_LIMIT

    asyncio.run(exercise())


def test_host_rejects_second_start_while_running() -> None:
    async def exercise() -> None:
        host = BoundedPresentationHost(_FakeRuntime)
        await host.start(())
        with pytest.raises(PresentationHostError) as exc:
            await host.start(())
        assert exc.value.code == PresentationHostErrorCode.INVALID_STATE
        await host.close()

    asyncio.run(exercise())


def test_host_stop_accounts_generation_and_allows_reentry() -> None:
    async def exercise() -> None:
        host = BoundedPresentationHost(_FakeRuntime, max_generations=2)
        await host.start(())
        stopped = await host.stop()
        assert stopped.state == PresentationHostState.STOPPED
        assert stopped.stopped_generations == 1
        assert stopped.delivered_frames == 2
        await host.start(())
        complete = await host.wait()
        assert complete.completed_generations == 1
        assert complete.stopped_generations == 1
        assert complete.delivered_frames == 6
        await host.close()

    asyncio.run(exercise())


def test_factory_failure_is_sanitized() -> None:
    def factory() -> _FakeRuntime:
        raise RuntimeError("rtsp://private username=admin password=secret")

    async def exercise() -> None:
        host = BoundedPresentationHost(factory)
        with pytest.raises(PresentationHostError) as exc:
            await host.start(())
        assert exc.value.code == PresentationHostErrorCode.ASSEMBLY_FAILURE
        message = str(exc.value).casefold()
        assert "rtsp" not in message
        assert "password" not in message
        assert host.snapshot.failures == 1

    asyncio.run(exercise())


def test_execution_failure_is_sanitized_and_recoverable() -> None:
    runtimes = iter([_FakeRuntime(fail_wait=True), _FakeRuntime()])

    async def exercise() -> None:
        host = BoundedPresentationHost(lambda: next(runtimes), max_generations=2)
        await host.start(())
        with pytest.raises(PresentationHostError) as exc:
            await host.wait()
        assert exc.value.code == PresentationHostErrorCode.EXECUTION_FAILURE
        assert "credential" not in str(exc.value).casefold()
        assert host.snapshot.state == PresentationHostState.FAILED
        assert host.snapshot.failures == 1

        await host.start(())
        done = await host.wait()
        assert done.state == PresentationHostState.COMPLETE
        assert done.generation == 2
        await host.close()

    asyncio.run(exercise())


def test_start_and_cleanup_failures_are_sanitized() -> None:
    async def start_failure() -> None:
        host = BoundedPresentationHost(lambda: _FakeRuntime(fail_start=True))
        with pytest.raises(PresentationHostError) as exc:
            await host.start(())
        assert exc.value.code == PresentationHostErrorCode.START_FAILURE
        assert "password" not in str(exc.value).casefold()

    async def cleanup_failure() -> None:
        host = BoundedPresentationHost(lambda: _FakeRuntime(fail_close=True))
        await host.start(())
        await host.wait()
        with pytest.raises(PresentationHostError) as exc:
            await host.close()
        assert exc.value.code == PresentationHostErrorCode.CLEANUP_FAILURE
        assert "users" not in str(exc.value).casefold()

    asyncio.run(start_failure())
    asyncio.run(cleanup_failure())


def test_constructor_and_idle_controls_fail_closed() -> None:
    with pytest.raises(PresentationHostError) as exc:
        BoundedPresentationHost(_FakeRuntime, max_generations=0)
    assert exc.value.code == PresentationHostErrorCode.INVALID_CONFIGURATION

    with pytest.raises(PresentationHostError):
        BoundedPresentationHost(_FakeRuntime, transition_timeout_seconds=0)

    async def exercise() -> None:
        host = BoundedPresentationHost(_FakeRuntime)
        with pytest.raises(PresentationHostError) as exc:
            await host.wait()
        assert exc.value.code == PresentationHostErrorCode.INVALID_STATE
        with pytest.raises(PresentationHostError) as exc:
            await host.stop()
        assert exc.value.code == PresentationHostErrorCode.INVALID_STATE
        assert (await host.close()).state == PresentationHostState.CLOSED
        with pytest.raises(PresentationHostError) as exc:
            await host.start(())
        assert exc.value.code == PresentationHostErrorCode.INVALID_STATE

    asyncio.run(exercise())
