from __future__ import annotations

import array
import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

import k5vision.operator_analytics as module
from k5vision.media.presentation_frame import PixelFormat, PresentationVideoFrame
from k5vision.operator_analytics_person import OperatorPersonConfiguration


def frame(marker=1, *, width=4, height=4, elapsed=999):
    return PresentationVideoFrame(
        memoryview(bytearray([marker] * width * height * 4)),
        width,
        height,
        width * 4,
        PixelFormat.BGRX,
        elapsed,
    )


def detection(*, category="person", confidence=0.9, track_id="private-tracker-id"):
    return SimpleNamespace(
        track_id=track_id,
        category=category,
        confidence=confidence,
        box=SimpleNamespace(x_min=0.1, y_min=0.2, x_max=0.7, y_max=0.9),
    )


@pytest.fixture
def harness(monkeypatch, tmp_path):
    calls, providers = [], []
    now = [0.0]
    gate = [None]
    values = [(detection(),)]

    class Provider:
        def __init__(self):
            self.closed = 0

        async def __call__(self, value):
            calls.append((self, bytes(value.payload), value.source_elapsed_ms))
            if gate[0] is not None:
                await gate[0].wait()
            result = values[0]
            if isinstance(result, Exception):
                raise result
            return result

        async def aclose(self):
            self.closed += 1

    class Factory:
        def __init__(self, config):
            assert config.artifact_root == tmp_path

        async def __call__(self):
            provider = Provider()
            providers.append(provider)
            return provider

    monkeypatch.setattr(module, "validate_person_runtime", lambda _config: None)
    monkeypatch.setattr(module, "PersonProviderFactory", Factory)

    def make(**kwargs):
        broker = module.OperatorAnalyticsBroker(OperatorPersonConfiguration(tmp_path), **kwargs)
        broker._clock = lambda: now[0]
        return broker

    return SimpleNamespace(
        calls=calls,
        providers=providers,
        now=now,
        gate=gate,
        values=values,
        make=make,
    )


def test_person_boxes_only_and_no_producer_identity_or_media_retained(harness):
    harness.values[0] = (
        detection(),
        detection(category="vehicle"),
        detection(confidence=0.49),
    )

    async def run():
        broker = harness.make()
        owner, camera = uuid4(), uuid4()
        result = await broker.infer(owner_id=owner, camera_id=camera, frame_id=17, frame=frame(7))
        assert result.status == "active" and len(result.observations) == 1
        assert result.as_dict()["frame_id"] == 17
        retained = json.dumps(result.as_dict())
        for forbidden in ("track", "payload", "credential", "source", str(owner), str(camera)):
            assert forbidden not in retained
        assert harness.calls[0][1] == bytes([7] * 64)
        assert harness.calls[0][2] == 0
        assert not hasattr(next(iter(broker._views.values())), "result")
        await broker.aclose()

    asyncio.run(run())
    assert harness.providers[0].closed == 1


def test_tracker_time_is_server_owned_and_views_do_not_share_state(harness):
    async def run():
        broker = harness.make()
        owner, other, camera, second = uuid4(), uuid4(), uuid4(), uuid4()
        await broker.infer(owner_id=owner, camera_id=camera, frame_id=0, frame=frame(elapsed=99999))
        harness.now[0] = 1
        await broker.infer(owner_id=owner, camera_id=camera, frame_id=1, frame=frame(elapsed=1))
        await broker.infer(owner_id=other, camera_id=camera, frame_id=2, frame=frame())
        await broker.infer(owner_id=owner, camera_id=second, frame_id=3, frame=frame())
        assert len(harness.providers) == 3
        assert harness.calls[0][0] is harness.calls[1][0]
        assert [value[2] for value in harness.calls] == [0, 1000, 0, 0]
        await broker.aclose()

    asyncio.run(run())


def test_busy_view_drops_frame_without_native_queue(harness):
    async def run():
        broker = harness.make()
        owner, camera = uuid4(), uuid4()
        harness.gate[0] = asyncio.Event()
        pending = asyncio.create_task(
            broker.infer(owner_id=owner, camera_id=camera, frame_id=1, frame=frame())
        )
        while not harness.calls:
            await asyncio.sleep(0)
        busy = await broker.infer(owner_id=owner, camera_id=camera, frame_id=2, frame=frame(8))
        assert busy.as_dict() == {"status": "busy", "frame_id": 2, "boxes": []}
        assert len(harness.calls) == 1
        harness.gate[0].set()
        assert (await pending).status == "active"
        assert (
            await broker.infer(owner_id=owner, camera_id=camera, frame_id=3, frame=frame())
        ).status == "busy"
        harness.now[0] = 0.5
        assert (
            await broker.infer(owner_id=owner, camera_id=camera, frame_id=4, frame=frame())
        ).status == "active"
        await broker.aclose()

    asyncio.run(run())


def test_global_view_capacity_and_idle_disposal(harness):
    async def run():
        broker = harness.make(max_views=2, idle_seconds=1)
        owner, camera = uuid4(), uuid4()
        for _ in range(2):
            assert (
                await broker.infer(owner_id=uuid4(), camera_id=camera, frame_id=1, frame=frame())
            ).status == "active"
        assert (
            await broker.infer(owner_id=owner, camera_id=camera, frame_id=2, frame=frame())
        ).status == "busy"
        assert len(harness.providers) == 2
        harness.now[0] = 1
        await broker.expire_idle()
        assert not broker._views and all(provider.closed == 1 for provider in harness.providers)
        assert (
            await broker.infer(owner_id=owner, camera_id=camera, frame_id=3, frame=frame())
        ).status == "active"
        await broker.aclose()

    asyncio.run(run())


def test_owner_close_cancels_inflight_and_leaves_other_session(harness):
    async def run():
        broker = harness.make()
        owner, other, camera = uuid4(), uuid4(), uuid4()
        await broker.infer(owner_id=other, camera_id=camera, frame_id=1, frame=frame())
        harness.gate[0] = asyncio.Event()
        pending = asyncio.create_task(
            broker.infer(owner_id=owner, camera_id=camera, frame_id=2, frame=frame())
        )
        while len(harness.calls) < 2:
            await asyncio.sleep(0)
        await broker.close_owner(owner)
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert (other, camera) in broker._views and (owner, camera) not in broker._views
        assert harness.providers[0].closed == 0 and harness.providers[1].closed == 1
        await broker.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("reason", ["failure", "timeout", "malformed", "overflow"])
def test_native_failure_returns_fixed_unavailable_and_closes_provider(harness, reason):
    if reason == "failure":
        harness.values[0] = RuntimeError("private-model-path-frame-source")
    elif reason == "malformed":
        harness.values[0] = (object(),)
    elif reason == "overflow":
        harness.values[0] = tuple(detection() for _ in range(33))

    async def run():
        broker = harness.make(inference_timeout_seconds=0.05)
        if reason == "timeout":
            harness.gate[0] = asyncio.Event()
        result = await broker.infer(owner_id=uuid4(), camera_id=uuid4(), frame_id=9, frame=frame())
        assert result.as_dict() == {"status": "unavailable", "frame_id": 9, "boxes": []}
        assert not broker._views and harness.providers[0].closed == 1
        await broker.aclose()

    asyncio.run(run())


def test_caller_cancellation_closes_owned_provider(harness):
    async def run():
        broker = harness.make()
        harness.gate[0] = asyncio.Event()
        pending = asyncio.create_task(
            broker.infer(owner_id=uuid4(), camera_id=uuid4(), frame_id=1, frame=frame())
        )
        while not harness.calls:
            await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert not broker._views and harness.providers[0].closed == 1
        await broker.aclose()

    asyncio.run(run())


def test_shutdown_is_idempotent_and_refuses_later_frames(harness):
    async def run():
        broker = harness.make()
        args = {"owner_id": uuid4(), "camera_id": uuid4(), "frame_id": 1, "frame": frame()}
        await broker.infer(**args)
        await broker.aclose()
        await broker.aclose()
        assert harness.providers[0].closed == 1
        assert (await broker.infer(**args)).status == "unavailable"
        assert len(harness.calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize("invalid", ["owner", "camera", "frame_id", "bool_id", "frame"])
def test_invalid_input_never_creates_native_worker(harness, invalid):
    async def run():
        broker = harness.make()
        args = {"owner_id": uuid4(), "camera_id": uuid4(), "frame_id": 1, "frame": frame()}
        args[
            {"owner": "owner_id", "camera": "camera_id", "bool_id": "frame_id"}.get(
                invalid, invalid
            )
        ] = True if invalid == "bool_id" else "private-invalid-value"
        with pytest.raises(ValueError) as error:
            await broker.infer(**args)
        assert "private-invalid-value" not in str(error.value)
        assert not broker._views and not harness.providers
        await broker.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["typed", "strided", "oversized", "bool_geometry"])
def test_frame_byte_and_geometry_bounds_are_checked_before_work(harness, kind):
    if kind == "typed":
        value = PresentationVideoFrame(
            memoryview(array.array("I", [1] * 64)), 4, 4, 16, PixelFormat.BGRX, 1
        )
    elif kind == "strided":
        value = PresentationVideoFrame(memoryview(bytes(128))[::2], 4, 4, 16, PixelFormat.BGRX, 1)
    elif kind == "oversized":
        value = frame(width=640, height=641)
    else:
        value = frame(width=True, height=4)

    async def run():
        broker = harness.make()
        with pytest.raises(ValueError, match="frame was refused"):
            await broker.infer(owner_id=uuid4(), camera_id=uuid4(), frame_id=1, frame=value)
        assert not harness.providers
        await broker.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_views": True},
        {"max_views": 5},
        {"minimum_interval_seconds": 0},
        {"minimum_interval_seconds": float("nan")},
        {"inference_timeout_seconds": 0},
        {"idle_seconds": float("inf")},
        {"idle_seconds": 0.1},
    ],
)
def test_invalid_capacity_or_timing_is_refused(harness, kwargs):
    with pytest.raises(ValueError, match="refused"):
        harness.make(**kwargs)
