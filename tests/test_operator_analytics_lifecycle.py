"""Per-launch ownership and stop/relaunch cleanup for installed analytics."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence

import pytest

import k5vision.operator_runtime as runtime_module
from k5vision.media.mixed_presentation import MixedLiveStream
from k5vision.media.presentation_frame import PixelFormat, PresentationVideoFrame
from k5vision.media.windows_operator_runtime import (
    WindowsOperatorRuntimeSnapshot,
    WindowsOperatorRuntimeState,
)
from k5vision.operator_launch import (
    OperatorLaunchError,
    OperatorLaunchErrorCode,
    ResolvedLiveSource,
)
from k5vision.operator_runtime import WindowsSingleLiveOperatorLauncher


class _OwnedProvider:
    def __init__(self, events: list[str], *, fail_close: bool = False) -> None:
        self.events = events
        self.fail_close = fail_close
        self.calls = 0
        self.close_calls = 0

    async def __call__(self, frame: PresentationVideoFrame) -> tuple[object, ...]:
        assert isinstance(frame, PresentationVideoFrame)
        assert self.close_calls == 0
        self.calls += 1
        return ()

    async def aclose(self) -> None:
        self.events.append("provider-close")
        self.close_calls += 1
        if self.fail_close:
            raise RuntimeError("synthetic-private-provider-diagnostic")


class _Delivery:
    def __init__(self, *, block: bool = False) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        if not block:
            self.release.set()

    async def run(
        self,
        _source: str,
        consumer: Callable[[PresentationVideoFrame], Awaitable[None]],
    ) -> None:
        await consumer(
            PresentationVideoFrame(
                memoryview(bytes(256)),
                8,
                8,
                32,
                PixelFormat.BGRX,
                0,
            )
        )
        for _ in range(5):
            await asyncio.sleep(0)
        self.started.set()
        await self.release.wait()


class _Runtime:
    def __init__(self, events: list[str], *, failure: str = "") -> None:
        self.events = events
        self.failure = failure
        self.stream: MixedLiveStream | None = None
        self.close_calls = 0

    @property
    def snapshot(self) -> WindowsOperatorRuntimeSnapshot:
        return WindowsOperatorRuntimeSnapshot(
            state=WindowsOperatorRuntimeState.COMPLETE,
            viewport_count=1,
            open_surface_count=0 if self.close_calls else 1,
            stream_count=1,
            delivered_frames=1,
            presentations=1,
        )

    async def start(self, streams: Sequence[MixedLiveStream]) -> WindowsOperatorRuntimeSnapshot:
        if self.failure == "start":
            raise RuntimeError("synthetic-private-native-diagnostic")
        self.stream = streams[0]
        return self.snapshot

    async def wait(self) -> WindowsOperatorRuntimeSnapshot:
        if self.failure == "wait":
            raise RuntimeError("synthetic-private-native-diagnostic")
        assert self.stream is not None

        async def consume(_frame: PresentationVideoFrame) -> None:
            pass

        await self.stream.delivery.run(self.stream.source_uri, consume)
        return self.snapshot

    async def close(self) -> WindowsOperatorRuntimeSnapshot:
        self.events.append("runtime-close")
        self.close_calls += 1
        self.stream = None
        if self.failure == "close":
            raise RuntimeError("synthetic-private-native-diagnostic")
        return self.snapshot


def test_concurrent_cameras_cancel_and_relaunch_own_distinct_providers_and_cleanup() -> None:
    async def scenario() -> None:
        providers: list[_OwnedProvider] = []
        deliveries: list[_Delivery] = []
        runtimes: list[_Runtime] = []

        async def provider_factory() -> _OwnedProvider:
            provider = _OwnedProvider([])
            providers.append(provider)
            return provider

        def delivery_factory(_payload: int) -> _Delivery:
            delivery = _Delivery(block=True)
            deliveries.append(delivery)
            return delivery

        def runtime_factory(_layout: object) -> _Runtime:
            runtime = _Runtime(providers[-1].events)
            runtimes.append(runtime)
            return runtime

        launcher = WindowsSingleLiveOperatorLauncher(
            detection_provider_factory=provider_factory,
            delivery_factory=delivery_factory,
            runtime_factory=runtime_factory,
        )

        async def start(host: str, ordinal: int) -> asyncio.Task:
            task = asyncio.create_task(
                launcher.run(
                    ResolvedLiveSource(f"rtsp://{host}/synthetic", 96),
                    width=1280,
                    height=720,
                )
            )
            # Factory/delivery construction are synchronous until runtime.wait.
            await asyncio.sleep(0)
            await asyncio.wait_for(deliveries[ordinal].started.wait(), 2)
            return task

        first = await start("192.0.2.40", 0)
        second = await start("192.0.2.41", 1)
        assert providers[0] is not providers[1]
        assert [provider.calls for provider in providers] == [1, 1]
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert providers[0].close_calls == runtimes[0].close_calls == 1
        assert providers[0].events == ["runtime-close", "provider-close"]
        assert providers[1].close_calls == runtimes[1].close_calls == 0
        assert not second.done()

        relaunched = await start("192.0.2.40", 2)
        assert len({id(provider) for provider in providers}) == 3
        assert [provider.calls for provider in providers] == [1, 1, 1]
        deliveries[1].release.set()
        deliveries[2].release.set()
        for metrics in await asyncio.gather(second, relaunched):
            assert metrics.analytics_enabled is True
            assert metrics.analytics_provider_submissions == 1
            assert metrics.analytics_provider_completions == 1
            assert metrics.analytics_failures == 0
        assert all(provider.close_calls == 1 for provider in providers)
        assert all(runtime.close_calls == 1 for runtime in runtimes)
        assert all(provider.events == ["runtime-close", "provider-close"] for provider in providers)

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["delivery", "runtime", "start", "wait", "close", "provider"])
def test_owned_provider_is_closed_once_across_media_start_run_and_cleanup_failures(
    failure: str,
) -> None:
    events: list[str] = []
    provider = _OwnedProvider(events, fail_close=failure == "provider")
    runtime = _Runtime(events, failure=failure)

    async def factory() -> _OwnedProvider:
        return provider

    def delivery_factory(_payload: int) -> _Delivery:
        if failure == "delivery":
            raise RuntimeError("synthetic-private-delivery-diagnostic")
        return _Delivery()

    def runtime_factory(_layout: object) -> _Runtime:
        if failure == "runtime":
            raise RuntimeError("synthetic-private-runtime-diagnostic")
        return runtime

    launcher = WindowsSingleLiveOperatorLauncher(
        detection_provider_factory=factory,
        delivery_factory=delivery_factory,
        runtime_factory=runtime_factory,
    )
    with pytest.raises(OperatorLaunchError) as caught:
        asyncio.run(
            launcher.run(
                ResolvedLiveSource("rtsp://192.0.2.40/synthetic", 96),
                width=1280,
                height=720,
            )
        )
    assert caught.value.code is OperatorLaunchErrorCode.LAUNCH_FAILURE
    assert "synthetic-private" not in str(caught.value)
    assert "rtsp://" not in str(caught.value)
    assert provider.close_calls == 1
    assert events == (
        ["provider-close"]
        if failure in {"delivery", "runtime"}
        else ["runtime-close", "provider-close"]
    )


def test_provider_start_failure_precedes_media_and_is_source_free() -> None:
    async def factory() -> _OwnedProvider:
        raise RuntimeError("synthetic-private-model-path")

    def unexpected(_argument: object) -> object:
        raise AssertionError("media must not start after provider initialization failure")

    launcher = WindowsSingleLiveOperatorLauncher(
        detection_provider_factory=factory,
        delivery_factory=unexpected,
        runtime_factory=unexpected,
    )
    with pytest.raises(OperatorLaunchError, match="configured analytics could not start") as caught:
        asyncio.run(
            launcher.run(
                ResolvedLiveSource("rtsp://192.0.2.40/synthetic", 96),
                width=1280,
                height=720,
            )
        )
    assert "synthetic-private" not in str(caught.value)


@pytest.mark.parametrize(
    "environment",
    [
        {"K5_LOCAL_TEST_RTSP_SOURCE": "rtsp://127.0.0.1/synthetic"},
        {
            "K5_PUBLIC_TEST_RTSP_SOURCE": "rtsp://93.184.216.34/synthetic",
            "K5_PUBLIC_TEST_SOURCE_IP": "93.184.216.34",
        },
        {
            "K5_STAGE03_SOURCE": "rtsp://192.0.2.40/synthetic",
            "K5_STAGE03_CAM_CRED": "disposable-user\ndisposable-fixture\n",
        },
    ],
)
def test_all_environment_runtime_paths_forward_factory_without_starting_it(
    monkeypatch: pytest.MonkeyPatch,
    environment: dict[str, str],
) -> None:
    supplied: list[object] = []

    async def factory() -> _OwnedProvider:
        raise AssertionError("factory must remain lazy until an authenticated launch")

    def launcher(**kwargs: object) -> object:
        supplied.append(kwargs.get("detection_provider_factory"))
        assert kwargs.get("detection_provider") is None
        return object()

    monkeypatch.setattr(runtime_module, "WindowsSingleLiveOperatorLauncher", launcher)
    resolver, result = runtime_module.build_environment_operator_runtime(
        environment,
        detection_provider_factory=factory,
    )
    assert resolver is not None and result is not None
    assert supplied == [factory]


def test_injected_provider_and_owned_factory_are_mutually_exclusive() -> None:
    async def injected(_frame: PresentationVideoFrame) -> tuple[object, ...]:
        return ()

    async def factory() -> _OwnedProvider:
        raise AssertionError("construction must not start analytics")

    with pytest.raises(ValueError, match="mutually exclusive"):
        WindowsSingleLiveOperatorLauncher(
            detection_provider=injected,
            detection_provider_factory=factory,
        )
