"""Port-free, mocked ownership and exact-frame regressions; no native admission."""

import asyncio
from datetime import UTC, datetime

import pytest

from k5vision.media.presentation_frame import PixelFormat, PresentationVideoFrame
from k5vision.operator_product_analytics import AnalyzedSampleCoordinator, SampleError, SampleOwner


def frame():
    return PresentationVideoFrame(memoryview(bytes(range(64))), 4, 4, 16, PixelFormat.BGRX, 12)


class Runner:
    def __init__(self, gate=None, error=False):
        self.gate, self.error, self.closed = gate, error, False
        self.frame = frame()

    async def run(self, source, consumer):
        if self.gate:
            await self.gate.wait()
        await consumer(self.frame)
        self.closed = True
        if self.error:
            raise RuntimeError("rtsp://secret:password@private/source")


class Provider:
    def __init__(self, runner, gate=None, close_error=False):
        self.runner, self.gate, self.close_error = runner, gate, close_error
        self.seen = None
        self.closed = False

    async def __call__(self, value):
        assert self.runner.closed
        self.seen = value
        if self.gate:
            await self.gate.wait()
        return ()

    async def aclose(self):
        if self.close_error:
            raise RuntimeError("private/model/path")
        self.closed = True


def setup(*, runner=None, gate=None, close_error=False, clock=None):
    runner = runner or Runner()
    provider = Provider(runner, gate, close_error)

    async def factory():
        return provider

    coordinator = AnalyzedSampleCoordinator(
        factory, runner_factory=lambda: runner, clock=clock or __import__("time").monotonic
    )
    return coordinator, runner, provider


OWNER = SampleOwner("account", "session-digest", "camera", 1)


async def finish(coordinator):
    await asyncio.shield(coordinator._task)


def test_same_immutable_frame_after_decoder_cleanup_and_empty_detection_success():
    async def check():
        c, r, p = setup()
        ticket = c.admit(OWNER, "rtsp://secret@127.0.0.1/private")
        await finish(c)
        result = c.poll(ticket["ticket"], OWNER)
        assert result["status"] == "ready"
        assert p.seen.payload.readonly and bytes(p.seen.payload) == bytes(r.frame.payload)
        assert p.closed and result["rendered_boxes"] == 0
        assert result["pixel_format"] == "BGRx"
        assert datetime.fromisoformat(result["received_at"]).tzinfo == UTC
        assert "secret" not in str(result) and "source_elapsed_ms" not in result

    asyncio.run(check())


@pytest.mark.parametrize(
    "owner",
    [
        SampleOwner("other", "session-digest", "camera", 1),
        SampleOwner("account", "other", "camera", 1),
        SampleOwner("account", "session-digest", "other", 1),
        SampleOwner("account", "session-digest", "camera", 2),
    ],
)
def test_ticket_binds_account_session_camera_generation(owner):
    async def check():
        c, _, _ = setup()
        t = c.admit(OWNER, "private")
        await finish(c)
        with pytest.raises(SampleError) as error:
            c.poll(t["ticket"], owner)
        assert error.value.status_code == 404

    asyncio.run(check())


def test_cancelled_http_waiter_does_not_cancel_owned_capture_or_release_capacity():
    async def check():
        gate = asyncio.Event()
        c, r, _ = setup(runner=Runner(gate))
        ticket = c.admit(OWNER, "private")

        async def waiter():
            await asyncio.shield(c._task)

        request = asyncio.create_task(waiter())
        await asyncio.sleep(0)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        c.invalidate_session(OWNER.session)
        with pytest.raises(SampleError) as error:
            c.admit(OWNER, "private")
        assert error.value.status_code == 409 and not r.closed
        gate.set()
        await finish(c)
        with pytest.raises(SampleError):
            c.poll(ticket["ticket"], OWNER)
        assert c.status() == "sample_available"

    asyncio.run(check())


def test_expiry_erases_payload_but_does_not_release_blocked_inference():
    async def check():
        now = [10.0]
        gate = asyncio.Event()
        c, _, _ = setup(gate=gate, clock=lambda: now[0])
        t = c.admit(OWNER, "private")
        await asyncio.sleep(0)
        now[0] += 61
        with pytest.raises(SampleError) as error:
            c.poll(t["ticket"], OWNER)
        assert error.value.status_code == 410
        assert c.status() == "busy"
        gate.set()
        await finish(c)
        assert c._result is None

    asyncio.run(check())


@pytest.mark.parametrize("decoder_failure", [True, False])
def test_unresolved_cleanup_fails_closed_and_never_exposes_errors(decoder_failure):
    async def check():
        c, _, _ = setup(runner=Runner(error=decoder_failure), close_error=not decoder_failure)
        t = c.admit(OWNER, "rtsp://secret:password@private/source")
        await finish(c)
        assert c.poll(t["ticket"], OWNER)["status"] == "failed"
        assert c.status() == "unavailable"
        with pytest.raises(SampleError) as error:
            c.admit(OWNER, "private")
        assert "private" not in str(error.value) and c._result is None

    asyncio.run(check())


def test_renderer_receives_the_identical_frame_used_by_inference(monkeypatch):
    from types import SimpleNamespace

    import k5vision.operator_product_analytics as module

    original = module.BoundedDetectionOverlayRenderer

    async def check():
        c, r, p = setup()
        captured = []

        class Renderer(original):
            def render(self, value, observations):
                assert value is p.seen
                captured.append(value)
                return super().render(value, observations)

        monkeypatch.setattr(module, "BoundedDetectionOverlayRenderer", Renderer)

        async def infer(value):
            p.seen = value
            return (
                SimpleNamespace(
                    track_id="never-expose-track",
                    category="person",
                    confidence=0.9,
                    box=SimpleNamespace(x_min=0, y_min=0, x_max=1, y_max=1),
                ),
            )

        class DetectProvider:
            __call__ = staticmethod(infer)
            aclose = p.aclose

        async def factory():
            return DetectProvider()

        c._provider_factory = factory
        t = c.admit(OWNER, "private")
        await finish(c)
        result = c.poll(t["ticket"], OWNER)
        assert captured and result["rendered_boxes"] == 1
        assert result["pixels"][:4] == b"\x00\xff\x00\x00"
        assert bytes(p.seen.payload) == bytes(r.frame.payload)
        assert "never-expose-track" not in str(result)

    asyncio.run(check())


@pytest.mark.parametrize("phase", ["startup", "inference", "close"])
def test_each_native_phase_stays_owned_after_request_cancel_and_invalidation(phase):
    async def check():
        entered, release = asyncio.Event(), asyncio.Event()
        r = Runner()
        p = Provider(r)

        async def pause():
            entered.set()
            await release.wait()

        class GatedProvider:
            async def __call__(self, value):
                if phase == "inference":
                    await pause()
                return await p(value)

            async def aclose(self):
                if phase == "close":
                    await pause()
                await p.aclose()

        async def factory():
            if phase == "startup":
                await pause()
            return GatedProvider()

        c = AnalyzedSampleCoordinator(factory, runner_factory=lambda: r)
        t = c.admit(OWNER, "private")
        await entered.wait()

        async def http_waiter():
            await asyncio.shield(c._task)

        request = asyncio.create_task(http_waiter())
        await asyncio.sleep(0)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        c.invalidate()
        assert c.status() == "busy"
        with pytest.raises(SampleError):
            c.admit(OWNER, "private")
        release.set()
        await finish(c)
        assert p.closed and c.status() == "sample_available"
        with pytest.raises(SampleError):
            c.poll(t["ticket"], OWNER)

    asyncio.run(check())


def test_factory_timeout_latches_capacity_without_claiming_native_drained():
    async def check():
        async def factory():
            raise TimeoutError("private-runtime/model")

        c = AnalyzedSampleCoordinator(factory, runner_factory=Runner)
        t = c.admit(OWNER, "private")
        await finish(c)
        assert c.poll(t["ticket"], OWNER)["status"] == "failed"
        assert c.status() == "unavailable"
        with pytest.raises(SampleError):
            await c.aclose()

    asyncio.run(check())


def test_invalid_and_oversized_sample_never_reaches_provider():
    async def check():
        for value in (
            object(),
            PresentationVideoFrame(
                memoryview(b"x" * (16 * 1024 * 1024 + 4)),
                1,
                1,
                16 * 1024 * 1024 + 4,
                PixelFormat.BGRX,
                0,
            ),
        ):

            class InvalidRunner:
                async def run(self, source, consumer, value=value):
                    await consumer(value)

            c, _, p = setup(runner=InvalidRunner())
            t = c.admit(OWNER, "private")
            await finish(c)
            assert c.poll(t["ticket"], OWNER)["status"] == "failed"
            assert p.seen is None

    asyncio.run(check())


def test_timer_expires_unpolled_result_and_shutdown_retains_owned_work():
    async def check():
        gate = asyncio.Event()
        c, _, _ = setup(runner=Runner(gate))
        c.admit(OWNER, "private")
        closing = asyncio.create_task(c.aclose())
        await asyncio.sleep(0)
        assert not closing.done() and c.status() == "unavailable"
        gate.set()
        await closing
        assert c._result is None and c._expiry_handle is None
        c, _, _ = setup()
        c.admit(OWNER, "private")
        await finish(c)
        assert c._result
        c._expiry_handle._run()
        assert c._result is None

    asyncio.run(check())


@pytest.mark.parametrize("phase", ["construct", "pull", "close"])
def test_unchanged_direct_delivery_thread_work_is_never_cancelled_by_waiter(phase):
    import threading

    from k5vision.media.gstreamer_direct_frame_delivery import GStreamerDirectFrameDelivery
    from k5vision.media.presentation_decoder import _PresentationPayload

    entered, release = threading.Event(), threading.Event()
    closed = []

    def pause():
        entered.set()
        if not release.wait(5):
            raise RuntimeError("Test gate timeout")

    class Backend:
        def pull(self, timeout):
            if phase == "pull":
                pause()
            return _PresentationPayload(bytes(64), 4, 4, 16)

        def close(self):
            if phase == "close":
                pause()
            closed.append(True)

    def backend_factory(*args):
        if phase == "construct":
            pause()
        return Backend()

    async def check():
        async def forbidden():
            raise AssertionError("Invalidated work must not start inference")

        c = AnalyzedSampleCoordinator(
            forbidden,
            runner_factory=lambda: GStreamerDirectFrameDelivery(
                frame_goal=1, max_frame_bytes=16 * 1024 * 1024, backend_factory=backend_factory
            ),
        )
        c.admit(OWNER, "rtsp://private@127.0.0.1/path")
        try:
            for _ in range(500):
                if entered.is_set():
                    break
                await asyncio.sleep(0.001)
            assert entered.is_set()

            async def waiter():
                await asyncio.shield(c._task)

            waiting = asyncio.create_task(waiter())
            await asyncio.sleep(0)
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
            c.invalidate()
            assert c.status() == "busy" and not closed
            with pytest.raises(SampleError):
                c.admit(OWNER, "private")
        finally:
            release.set()
            await finish(c)
        assert closed == [True] and c.status() == "sample_available"

    asyncio.run(check())


def test_real_factory_timeout_retains_native_slot_and_coordinator_stays_latched(monkeypatch):
    import threading
    from pathlib import Path

    import k5vision.analytics_runtime as runtime
    from k5vision.analytics_config import AnalyticsConfiguration

    entered, release, completed = threading.Event(), threading.Event(), threading.Event()

    class BlockedTracker:
        def __init__(self, config):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Mock native startup gate timed out")
            completed.set()

    monkeypatch.setattr(runtime, "_DetectorTracker", BlockedTracker)
    monkeypatch.setattr(runtime, "_START_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(runtime, "_CLOSE_TIMEOUT_SECONDS", 0.02)
    factory = runtime.AnalyticsProviderFactory(AnalyticsConfiguration(Path("/unused-mocked-model")))

    def free_slots():
        held = 0
        try:
            while factory._capacity.acquire(blocking=False):
                held += 1
            return held
        finally:
            for _ in range(held):
                factory._capacity.release()

    async def check():
        c = AnalyzedSampleCoordinator(factory, runner_factory=Runner)
        ticket = c.admit(OWNER, "private")
        try:
            await finish(c)
            assert entered.is_set() and not completed.is_set()
            assert free_slots() == 3, (
                "Timed-out native startup still owns one of four factory slots"
            )
            assert c.status() == "unavailable"
            assert c.poll(ticket["ticket"], OWNER)["status"] == "failed"
            with pytest.raises(SampleError):
                c.admit(OWNER, "private")
        finally:
            release.set()
            for _ in range(500):
                if completed.is_set() and free_slots() == 4:
                    break
                await asyncio.sleep(0.001)
        assert completed.is_set() and free_slots() == 4
        # The coordinator has no public provider cleanup witness after failed startup.
        # Native completion does not silently reopen its conservative latch.
        assert c.status() == "unavailable" and c._result is None

    asyncio.run(check())


def test_repeated_cancelled_shutdown_waiters_cannot_escape_native_drain():
    async def check():
        gate = asyncio.Event()
        c, r, _ = setup(runner=Runner(gate))
        c.admit(OWNER, "private")
        await asyncio.sleep(0)
        closing = asyncio.create_task(c.aclose())
        await asyncio.sleep(0)
        for _ in range(3):
            closing.cancel()
            await asyncio.sleep(0)
            assert not closing.done() and not r.closed and not c._task.done()
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert r.closed and c._task.done()
        assert c.status() == "unavailable"

    asyncio.run(check())
