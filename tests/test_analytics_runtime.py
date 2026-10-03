from __future__ import annotations

import array
import asyncio
import gc
import sys
import threading
import weakref
from types import SimpleNamespace

import pytest

import k5vision.analytics_runtime as runtime
from k5vision.analytics_config import AnalyticsConfiguration
from k5vision.media.presentation_frame import PixelFormat, PresentationVideoFrame


def frame(marker=1):
    return PresentationVideoFrame(
        memoryview(bytearray([marker] * 64)), 4, 4, 16, PixelFormat.BGRX, marker
    )


@pytest.fixture
def fake_backend(monkeypatch):
    instances = []

    class Backend:
        def __init__(self, config):
            self.calls = []
            instances.append(self)

        def infer(self, payload, width, height, stride, elapsed):
            self.calls.append((payload, width, height, stride, elapsed))
            return (len(self.calls), payload[0])

    monkeypatch.setattr(runtime, "_DetectorTracker", Backend)
    return instances


def test_owned_session_uses_transient_copy_and_fresh_tracker_per_launch(tmp_path, fake_backend):
    factory = runtime.AnalyticsProviderFactory(AnalyticsConfiguration(tmp_path))

    async def run():
        one, two = await factory(), await factory()
        assert one is not two
        assert await one(frame(3)) == (1, 3)
        assert await one(frame(4)) == (2, 4)
        assert await two(frame(9)) == (1, 9)
        assert isinstance(fake_backend[0].calls[0][0], bytes)
        await one.aclose()
        await one.aclose()
        await two.aclose()
        with pytest.raises(RuntimeError, match="unavailable"):
            await one(frame())
        three = await factory()
        assert await three(frame(7)) == (1, 7)
        await three.aclose()

    asyncio.run(run())
    assert len(fake_backend) == 3


def test_native_sessions_are_bounded_and_close_releases_capacity(tmp_path, fake_backend):
    factory = runtime.AnalyticsProviderFactory(AnalyticsConfiguration(tmp_path))

    async def run():
        sessions = [await factory() for _ in range(runtime._MAX_NATIVE_SESSIONS)]
        with pytest.raises(RuntimeError, match="capacity"):
            await factory()
        for session in sessions:
            await session.aclose()
        next_session = await factory()
        await next_session.aclose()

    asyncio.run(run())


def test_invalid_frame_does_not_submit_work(tmp_path, monkeypatch, fake_backend):
    factory = runtime.AnalyticsProviderFactory(AnalyticsConfiguration(tmp_path))

    async def run():
        session = await factory()
        for value in (object(), frame()):
            monkeypatch.setattr(runtime, "_MAX_FRAME_BYTES", 1)
            with pytest.raises(ValueError, match="boundary"):
                await session(value)
        assert fake_backend[0].calls == []
        await session.aclose()

    asyncio.run(run())


def test_typed_or_noncontiguous_memoryview_cannot_bypass_native_byte_limit(tmp_path, fake_backend):
    factory = runtime.AnalyticsProviderFactory(AnalyticsConfiguration(tmp_path))
    payloads = (memoryview(array.array("I", [1] * 64)), memoryview(bytes(128))[::2])

    async def run():
        session = await factory()
        for payload in payloads:
            value = PresentationVideoFrame(payload, 4, 4, 16, PixelFormat.BGRX, 1)
            with pytest.raises(ValueError, match="boundary"):
                await session(value)
        assert fake_backend[0].calls == []
        await session.aclose()

    asyncio.run(run())


def test_failed_inference_is_sanitized_and_session_closes(tmp_path, monkeypatch):
    class Backend:
        def __init__(self, _):
            pass

        def infer(self, *args):
            raise RuntimeError("private-frame-and-model-path")

    monkeypatch.setattr(runtime, "_DetectorTracker", Backend)
    factory = runtime.AnalyticsProviderFactory(AnalyticsConfiguration(tmp_path))

    async def run():
        session = await factory()
        with pytest.raises(RuntimeError) as error:
            await session(frame())
        assert str(error.value) == "Analytics inference failed."
        await session.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("phase", ["initialize", "infer"])
def test_worker_failure_does_not_retain_native_traceback_frames_or_backend(
    tmp_path, monkeypatch, phase
):
    references = []

    class Backend:
        def __init__(self, _):
            references.append(weakref.ref(self))
            if phase == "initialize":
                partial_frame = b"private-partial-native-frame"  # noqa: F841
                raise RuntimeError("private-initialization-error")

        def infer(self, payload, *args):
            decoded_frame = bytes(payload)  # noqa: F841
            raise RuntimeError("private-native-error")

    monkeypatch.setattr(runtime, "_DetectorTracker", Backend)

    async def run():
        released = []
        session = runtime._AnalyticsSession(
            AnalyticsConfiguration(tmp_path), lambda: released.append(1)
        )
        if phase == "initialize":
            with pytest.raises(RuntimeError, match="initialization failed"):
                await session.start()
        else:
            await session.start()
            with pytest.raises(RuntimeError, match="inference failed"):
                await session(frame())
        old_future = session._pending
        assert old_future.exception() is None
        assert old_future.result() is (False if phase == "initialize" else None)
        await session.aclose()
        assert session._pending.result() is None
        assert session._backend is None
        assert released == [1]
        gc.collect()
        assert all(reference() is None for reference in references)

    asyncio.run(run())


def test_cancelled_native_call_stays_single_flight_and_reserves_capacity(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []

    class Backend:
        def __init__(self, _):
            pass

        def infer(self, *args):
            calls.append(1)
            entered.set()
            assert release.wait(5)
            return ()

    monkeypatch.setattr(runtime, "_DetectorTracker", Backend)
    monkeypatch.setattr(runtime, "_MAX_NATIVE_SESSIONS", 1)
    monkeypatch.setattr(runtime, "_CLOSE_TIMEOUT_SECONDS", 0.01)
    factory = runtime.AnalyticsProviderFactory(AnalyticsConfiguration(tmp_path))

    async def run():
        session = await factory()
        task = asyncio.create_task(session(frame()))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            with pytest.raises(RuntimeError, match="unavailable"):
                await session(frame())
            with pytest.raises(RuntimeError, match="cleanup is incomplete"):
                await session.aclose()
            with pytest.raises(RuntimeError, match="capacity"):
                await factory()
            assert calls == [1]
        finally:
            release.set()
            await asyncio.wrap_future(session._pending)
        fresh = await factory()
        await fresh.aclose()

    asyncio.run(run())


def test_start_timeout_retains_capacity_until_owned_worker_finishes(tmp_path, monkeypatch):
    release = threading.Event()

    class Backend:
        def __init__(self, _):
            assert release.wait(5)

    monkeypatch.setattr(runtime, "_DetectorTracker", Backend)
    monkeypatch.setattr(runtime, "_MAX_NATIVE_SESSIONS", 1)
    monkeypatch.setattr(runtime, "_START_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(runtime, "_CLOSE_TIMEOUT_SECONDS", 0.01)
    factory = runtime.AnalyticsProviderFactory(AnalyticsConfiguration(tmp_path))

    async def run():
        try:
            with pytest.raises(RuntimeError, match="cleanup is incomplete"):
                await factory()
            with pytest.raises(RuntimeError, match="capacity"):
                await factory()
        finally:
            release.set()
        # Event completion callback releases the slot on the worker thread.
        for _ in range(100):
            if factory._capacity.acquire(blocking=False):
                factory._capacity.release()
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("owned initialization worker did not release capacity")
        monkeypatch.setattr(runtime, "_START_TIMEOUT_SECONDS", 1)
        session = await factory()
        await session.aclose()

    asyncio.run(run())


def test_start_failure_releases_capacity_and_no_backend_survives(tmp_path, monkeypatch):
    def fail(_):
        raise RuntimeError("invalid model fixture")

    monkeypatch.setattr(runtime, "_DetectorTracker", fail)
    factory = runtime.AnalyticsProviderFactory(AnalyticsConfiguration(tmp_path))

    async def run():
        for _ in range(6):
            with pytest.raises(RuntimeError, match="session initialization failed"):
                await factory()

    asyncio.run(run())


def test_executor_creation_failure_releases_reserved_slot(tmp_path, monkeypatch):
    def fail(*a, **kw):
        raise RuntimeError("private details")

    monkeypatch.setattr(runtime, "ThreadPoolExecutor", fail)
    factory = runtime.AnalyticsProviderFactory(AnalyticsConfiguration(tmp_path))

    async def run():
        for _ in range(6):
            with pytest.raises(RuntimeError, match="session initialization failed"):
                await factory()

    asyncio.run(run())


def test_detector_adapter_preserves_padded_bgrx_normalization_and_monotonic_track_time(monkeypatch):
    observed = []

    class Array:
        def reshape(self, *shape):
            observed.append(("reshape", shape))
            return self

        def __getitem__(self, key):
            return self

    class Numpy:
        uint8 = "uint8"

        @staticmethod
        def frombuffer(payload, **kwargs):
            observed.append(("buffer", payload, kwargs))
            return Array()

        @staticmethod
        def ascontiguousarray(value):
            return value

    class Tracker:
        def update(self, index, timestamp, candidates):
            observed.append(("tracks", index, timestamp, candidates))
            return candidates

    backend = object.__new__(runtime._DetectorTracker)
    backend._numpy = Numpy()
    backend._candidate = lambda **kwargs: kwargs
    backend._box = lambda *args: args
    backend._tracker = Tracker()
    backend._index, backend._timestamp = 0, -1
    good = SimpleNamespace(bbox=SimpleNamespace(x1=-2, y1=1, x2=15, y2=5), confidence=0.9)
    empty = SimpleNamespace(bbox=SimpleNamespace(x1=8, y1=1, x2=7, y2=2), confidence=0.9)
    backend._detector = lambda image, index, timestamp: (good, empty)
    result = backend.infer(b"\0" * 288, 10, 6, 48, 0)
    assert result == (
        {
            "category": "person",
            "confidence": 0.9,
            "box": (0.0, 1 / 6, 1.0, 5 / 6),
            "model_class_id": 1,
        },
    )
    backend.infer(b"\0" * 288, 10, 6, 48, 0)
    tracks = [value for value in observed if value[0] == "tracks"]
    assert [(value[1], value[2]) for value in tracks] == [(0, 0), (1, 1)]
    assert ("reshape", (6, 48)) in observed
    assert ("reshape", (6, 10, 4)) in observed


def test_detector_constructs_only_reviewed_cpu_modules_after_admission(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(runtime, "validate_analytics_runtime", lambda c: calls.append("admitted"))
    monkeypatch.setitem(sys.modules, "numpy", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "analytics_lab.iou_tracker",
        SimpleNamespace(SimpleIoUAssociationBackend=lambda: "association"),
    )
    monkeypatch.setitem(
        sys.modules,
        "analytics_lab.openvino_omz",
        SimpleNamespace(
            OpenVINOOMZConfig=lambda **kw: kw,
            OpenVINOOMZPoseBackend=lambda root, **kw: calls.append((root, kw)),
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "analytics_lab.tracking",
        SimpleNamespace(
            DetectionCandidate=object,
            NormalizedBox=object,
            TrackingSession=lambda backend: calls.append(backend),
        ),
    )
    runtime._DetectorTracker(AnalyticsConfiguration(tmp_path))
    assert calls == [
        "admitted",
        (tmp_path, {"config": {"max_people": 4, "device": "CPU"}}),
        "association",
    ]
