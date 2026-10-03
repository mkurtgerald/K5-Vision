"""Portable regressions for installed analytics through the normal human-session app."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

import k5vision.analytics_config as config_module
import k5vision.analytics_runtime as analytics_runtime
import k5vision.stage_one_app as app_module
from k5vision.analytics_config import AnalyticsConfiguration, AnalyticsConfigurationError
from k5vision.domain.devices import Device
from k5vision.media.live_presentation import LivePresentationSnapshot, LivePresentationState
from k5vision.media.mixed_presentation import MixedLiveStream
from k5vision.media.presentation_frame import PixelFormat, PresentationVideoFrame
from k5vision.media.windows_operator_runtime import (
    WindowsOperatorRuntimeSnapshot,
    WindowsOperatorRuntimeState,
)
from k5vision.operator_launch import ResolvedLiveSource
from k5vision.operator_runtime import WindowsSingleLiveOperatorLauncher

_ADMIN_TOKEN = "disposable-fixture-user-admin-token"
_DEVICE_TOKEN = "disposable-fixture-device-service-token"
_PASSWORD = "disposable-fixture-password-12345"


def _config_file(tmp_path: Path) -> Path:
    root = tmp_path / "models"
    root.mkdir()
    path = tmp_path / "analytics.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "provider": config_module.PROVIDER,
                "source_revision": config_module.ANALYTICS_REVISION,
                "artifact_root": str(root),
            }
        ),
        encoding="utf-8",
    )
    return path


def _unexpected(*_args: object, **_kwargs: object) -> Any:
    raise AssertionError("disabled or unadmitted analytics reached an expensive boundary")


def test_fresh_process_rejects_config_before_importing_main_or_opening_databases(
    tmp_path: Path,
) -> None:
    invalid_config = tmp_path / "invalid-analytics.json"
    invalid_config.write_text("{synthetic-private-config-marker", encoding="utf-8")
    source_root = Path(__file__).resolve().parents[1] / "src"
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}
    }
    environment.update(
        {
            "PYTHONPATH": str(source_root),
            "K5_ANALYTICS_CONFIG": str(invalid_config),
            "K5_CONTROL_PLANE_SITE_ID": "synthetic-import-admission-site",
            "K5_CONTROL_PLANE_TOKEN": _DEVICE_TOKEN,
            "K5_CONTROL_PLANE_ADMIN_TOKEN": _ADMIN_TOKEN,
            "K5_DEVICE_DB_PATH": str(tmp_path / "devices.sqlite3"),
            "K5_USER_DB_PATH": str(tmp_path / "users.sqlite3"),
        }
    )
    script = """
import os
import sys
from pathlib import Path

root = Path(os.environ["K5_ANALYTICS_CONFIG"]).parent
assert "k5vision.main" not in sys.modules
assert not tuple(root.glob("*.sqlite3*"))

import k5vision.stage_one_app as stage_one_app

assert Path(stage_one_app.__file__).resolve().parent == (
    Path(os.environ["PYTHONPATH"]) / "k5vision"
)
assert "k5vision.main" not in sys.modules
assert not tuple(root.glob("*.sqlite3*"))

try:
    stage_one_app.create_stage_one_app()
except stage_one_app.AnalyticsConfigurationError as error:
    assert str(error) == "Configured analytics input is invalid."
    assert str(root) not in str(error)
    assert "synthetic-private-config-marker" not in str(error)
else:
    raise AssertionError("invalid explicit analytics was accepted")

assert "k5vision.main" not in sys.modules
assert not tuple(root.glob("*.sqlite3*"))
print("admission-before-app-ok")
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "admission-before-app-ok"
    assert not tuple(tmp_path.glob("*.sqlite3*"))


@pytest.mark.parametrize("injected", [False, True])
def test_no_opt_in_preserves_normal_app_without_dependency_model_or_factory_work(
    monkeypatch: pytest.MonkeyPatch,
    injected: bool,
) -> None:
    monkeypatch.setattr(app_module, "environ", {})
    monkeypatch.setattr(config_module, "validate_analytics_runtime", _unexpected)
    monkeypatch.setattr(config_module.metadata, "version", _unexpected)
    monkeypatch.setattr(app_module, "AnalyticsProviderFactory", _unexpected)
    monkeypatch.setattr(analytics_runtime, "_DetectorTracker", _unexpected)
    supplied: dict[str, object] = {}
    application = FastAPI()

    async def provider(_frame: PresentationVideoFrame) -> tuple[object, ...]:
        return ()

    def build(environment: object, **kwargs: object) -> tuple[None, None]:
        assert environment == {}
        supplied.update(kwargs)
        return None, None

    monkeypatch.setattr(app_module, "build_environment_operator_runtime", build)
    monkeypatch.setattr(app_module, "create_app", lambda **_kwargs: application)
    assert (
        app_module.create_stage_one_app(
            detection_provider=provider if injected else None,
        )
        is application
    )
    assert supplied == {
        "detection_provider": provider if injected else None,
        "detection_provider_factory": None,
    }


def test_normal_app_validates_config_before_wiring_lazy_source_free_factory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = _config_file(tmp_path)
    environment = {"K5_ANALYTICS_CONFIG": str(path)}
    monkeypatch.setattr(app_module, "environ", environment)
    events: list[str] = []
    captured: list[AnalyticsConfiguration] = []
    resolver, launcher, factory = object(), object(), object()
    application = FastAPI()

    def admit(config: AnalyticsConfiguration) -> None:
        captured.append(config)
        events.append("admit")

    def create_factory(config: AnalyticsConfiguration) -> object:
        assert config is captured[0]
        events.append("factory")
        return factory

    def build(env: object, **kwargs: object) -> tuple[object, object]:
        assert env is environment
        assert kwargs == {"detection_provider": None, "detection_provider_factory": factory}
        events.append("runtime")
        return resolver, launcher

    def create(**kwargs: object) -> FastAPI:
        assert kwargs == {
            "operator_source_resolver": resolver,
            "operator_launcher": launcher,
            "operator_recording_root": None,
        }
        events.append("app")
        return application

    monkeypatch.setattr(config_module, "validate_analytics_runtime", admit)
    monkeypatch.setattr(app_module, "AnalyticsProviderFactory", create_factory)
    monkeypatch.setattr(app_module, "build_environment_operator_runtime", build)
    monkeypatch.setattr(app_module, "create_app", create)
    assert app_module.create_stage_one_app() is application
    assert events == ["admit", "factory", "runtime", "app"]


@pytest.mark.parametrize("failure", ["malformed", "dependency", "model"])
def test_explicit_analytics_failure_precedes_app_state_or_media_creation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
) -> None:
    path = _config_file(tmp_path)
    monkeypatch.setattr(app_module, "environ", {"K5_ANALYTICS_CONFIG": str(path)})
    monkeypatch.setattr(app_module, "create_app", _unexpected)
    monkeypatch.setattr(app_module, "build_environment_operator_runtime", _unexpected)
    monkeypatch.setattr(app_module, "AnalyticsProviderFactory", _unexpected)
    monkeypatch.setattr(analytics_runtime, "_DetectorTracker", _unexpected)
    if failure == "malformed":
        path.write_text("{not-json", encoding="utf-8")
    else:
        monkeypatch.setattr(config_module, "validate_installed_analytics", lambda: None)
        if failure == "dependency":
            monkeypatch.setattr(config_module.metadata, "version", lambda _package: "0.0")
        else:
            monkeypatch.setattr(
                config_module.metadata,
                "version",
                config_module.RUNTIME_VERSIONS.__getitem__,
            )
            artifacts = ModuleType("analytics_lab.artifacts")
            artifacts.OPENVINO_OMZ_2023_FP16 = ()  # type: ignore[attr-defined]

            def invalid_model(*_args: object) -> None:
                raise ValueError("synthetic-model-mismatch")

            artifacts.verify_artifact_set = invalid_model  # type: ignore[attr-defined]
            monkeypatch.setitem(sys.modules, "analytics_lab.artifacts", artifacts)

    with pytest.raises(AnalyticsConfigurationError) as caught:
        app_module.create_stage_one_app()
    assert str(tmp_path) not in str(caught.value)
    assert "synthetic-model-mismatch" not in str(caught.value)
    assert not tuple(tmp_path.glob("*.sqlite3"))


def test_explicit_config_conflicts_with_injected_provider_before_app_creation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(app_module, "environ", {"K5_ANALYTICS_CONFIG": str(_config_file(tmp_path))})
    monkeypatch.setattr(config_module, "validate_analytics_runtime", lambda _config: None)
    monkeypatch.setattr(app_module, "AnalyticsProviderFactory", _unexpected)
    monkeypatch.setattr(app_module, "build_environment_operator_runtime", _unexpected)
    monkeypatch.setattr(app_module, "create_app", _unexpected)

    async def injected(_frame: PresentationVideoFrame) -> tuple[object, ...]:
        return ()

    with pytest.raises(AnalyticsConfigurationError, match="conflicts"):
        app_module.create_stage_one_app(detection_provider=injected)


class _SyntheticBackend:
    """Only state/counters are retained, never the synthetic frame payload."""

    def __init__(self, config: AnalyticsConfiguration) -> None:
        assert isinstance(config, AnalyticsConfiguration)
        self.indices: list[int] = []

    def infer(
        self, _payload: bytes, width: int, height: int, stride: int, _elapsed_ms: int
    ) -> tuple[object, ...]:
        assert (width, height, stride) == (8, 8, 32)
        self.indices.append(len(self.indices))
        return ()


class _ObservedProvider:
    def __init__(self, inner: analytics_runtime.OwnedAnalyticsProvider) -> None:
        self.inner = inner
        self.observed = asyncio.Event()
        self.calls = 0
        self.close_calls = 0

    async def __call__(self, frame: PresentationVideoFrame) -> tuple[object, ...]:
        result = await self.inner(frame)
        self.calls += 1
        self.observed.set()
        return result

    async def aclose(self) -> None:
        await self.inner.aclose()
        self.close_calls += 1


class _SyntheticDelivery:
    def __init__(self, provider: _ObservedProvider) -> None:
        self.provider = provider

    async def run(
        self,
        _source: str,
        consumer: Callable[[PresentationVideoFrame], Awaitable[None]],
    ) -> LivePresentationSnapshot:
        frame = PresentationVideoFrame(
            memoryview(bytes(256)),
            8,
            8,
            32,
            PixelFormat.BGRX,
            0,
        )
        await consumer(frame)
        await asyncio.wait_for(self.provider.observed.wait(), 2)
        # Let the real overlay collect the completed provider before teardown.
        for _ in range(5):
            await asyncio.sleep(0)
        return LivePresentationSnapshot(
            state=LivePresentationState.COMPLETE,
            decoder_initialized=True,
            accepted_packets=1,
            rtp_valid_packets=1,
            rtp_invalid_packets=0,
            rtp_delivered_bytes=256,
            delivered_frames=1,
            delivered_frame_bytes=256,
            source_span_ms=0,
        )


class _SyntheticWindowsRuntime:
    def __init__(self) -> None:
        self.closed = False
        self.stream: MixedLiveStream | None = None
        self.frames = 0

    @property
    def snapshot(self) -> WindowsOperatorRuntimeSnapshot:
        return WindowsOperatorRuntimeSnapshot(
            state=WindowsOperatorRuntimeState.COMPLETE,
            viewport_count=1,
            open_surface_count=0 if self.closed else 1,
            stream_count=1,
            delivered_frames=self.frames,
            presentations=self.frames,
        )

    async def start(self, streams: Sequence[MixedLiveStream]) -> WindowsOperatorRuntimeSnapshot:
        assert len(streams) == 1
        self.stream = streams[0]
        return self.snapshot

    async def wait(self) -> WindowsOperatorRuntimeSnapshot:
        assert self.stream is not None

        async def consume(_frame: PresentationVideoFrame) -> None:
            self.frames += 1

        await self.stream.delivery.run(self.stream.source_uri, consume)
        return self.snapshot

    async def close(self) -> WindowsOperatorRuntimeSnapshot:
        self.closed = True
        self.stream = None
        return self.snapshot


class _AppHarness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.providers: list[_ObservedProvider] = []
        self.backends: list[_SyntheticBackend] = []
        self.runtimes: list[_SyntheticWindowsRuntime] = []
        self.resolver_calls = 0
        self.factory_calls = 0
        for key in ("K5_CONTROL_PLANE_READ_TOKEN", "K5_STAGE_ONE_RECORDING_ROOT"):
            monkeypatch.delenv(key, raising=False)
        for key, value in {
            "K5_CONTROL_PLANE_ADMIN_TOKEN": _ADMIN_TOKEN,
            "K5_USER_DB_PATH": str(tmp_path / "users.sqlite3"),
            "K5_CONTROL_PLANE_TOKEN": _DEVICE_TOKEN,
            "K5_CONTROL_PLANE_SITE_ID": "synthetic-site",
            "K5_DEVICE_DB_PATH": str(tmp_path / "devices.sqlite3"),
            "K5_ANALYTICS_CONFIG": str(_config_file(tmp_path)),
        }.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setattr(config_module, "validate_analytics_runtime", lambda _config: None)
        harness = self

        class Backend(_SyntheticBackend):
            def __init__(self, config: AnalyticsConfiguration) -> None:
                super().__init__(config)
                harness.backends.append(self)

        class Factory(analytics_runtime.AnalyticsProviderFactory):
            async def __call__(self) -> _ObservedProvider:
                harness.factory_calls += 1
                provider = _ObservedProvider(await super().__call__())
                harness.providers.append(provider)
                return provider

        class Resolver:
            async def resolve(self, device: Device, token: str) -> ResolvedLiveSource:
                assert token == "synthetic-profile"
                harness.resolver_calls += 1
                return ResolvedLiveSource(f"rtsp://{device.host}/synthetic", 96)

        def runtime_factory(_layout: object) -> _SyntheticWindowsRuntime:
            runtime = _SyntheticWindowsRuntime()
            self.runtimes.append(runtime)
            return runtime

        def build(_environment: object, **kwargs: Any) -> tuple[Resolver, object]:
            return Resolver(), WindowsSingleLiveOperatorLauncher(
                delivery_factory=lambda _payload: _SyntheticDelivery(self.providers[-1]),
                runtime_factory=runtime_factory,
                **kwargs,
            )

        monkeypatch.setattr(analytics_runtime, "_DetectorTracker", Backend)
        monkeypatch.setattr(app_module, "AnalyticsProviderFactory", Factory)
        monkeypatch.setattr(app_module, "build_environment_operator_runtime", build)
        self.app = app_module.create_stage_one_app()


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _human_session(client: httpx.AsyncClient) -> str:
    response = await client.post(
        "/api/v1/users",
        headers=_headers(_ADMIN_TOKEN),
        json={
            "username": "synthetic-operator",
            "display_name": "Synthetic operator",
            "role": "operator",
            "enabled": True,
        },
    )
    assert response.status_code == 201
    bootstrap = await client.post(
        "/api/v1/auth/bootstrap-password",
        json={
            "username": "synthetic-operator",
            "temporary_credential": response.json()["temporary_credential"],
            "new_password": _PASSWORD,
        },
    )
    assert bootstrap.status_code == 204
    logged_in = await client.post(
        "/api/v1/auth/login",
        json={
            "username": "synthetic-operator",
            "password": _PASSWORD,
        },
    )
    assert logged_in.status_code == 200
    return logged_in.json()["session_token"]


async def _enroll(client: httpx.AsyncClient, host: str) -> str:
    response = await client.post(
        "/api/v1/devices",
        headers=_headers(_DEVICE_TOKEN),
        json={
            "name": "Synthetic camera",
            "host": host,
            "kind": "camera",
            "protocols": ["rtsp"],
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


def test_normal_app_rejects_missing_invalid_and_service_auth_before_source_or_inference(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    harness = _AppHarness(monkeypatch, tmp_path)

    async def scenario() -> None:
        async with harness.app.router.lifespan_context(harness.app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(harness.app),
                base_url="http://test",
            ) as client:
                device = await _enroll(client, "192.0.2.40")
                for token in (None, "invalid-session", _DEVICE_TOKEN, _ADMIN_TOKEN):
                    response = await client.post(
                        "/api/v1/operator/live",
                        json={
                            "device_id": device,
                            "stream_token": "synthetic-profile",
                        },
                        headers={} if token is None else _headers(token),
                    )
                    assert response.status_code == 401
                    assert response.json() == {"detail": "Unauthorized"}
                    assert response.headers["www-authenticate"] == "Bearer"
                    assert harness.resolver_calls == harness.factory_calls == 0
                    assert not harness.providers and not harness.backends and not harness.runtimes

    asyncio.run(scenario())


def test_normal_authenticated_app_owns_fresh_sessions_for_two_cameras_and_relaunch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    harness = _AppHarness(monkeypatch, tmp_path)

    async def scenario() -> None:
        async with harness.app.router.lifespan_context(harness.app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(harness.app),
                base_url="http://test",
            ) as client:
                session = await _human_session(client)
                cameras = [await _enroll(client, host) for host in ("192.0.2.40", "192.0.2.41")]
                for camera in (*cameras, cameras[0]):
                    response = await client.post(
                        "/api/v1/operator/live",
                        headers=_headers(session),
                        json={"device_id": camera, "stream_token": "synthetic-profile"},
                    )
                    assert response.status_code == 200
                    assert response.json() == {
                        "schema_version": "2",
                        "completed": True,
                        "delivered_frames": 1,
                        "presentations": 1,
                        "processed_controls": 0,
                        "analytics_enabled": True,
                        "analytics_provider_submissions": 1,
                        "analytics_provider_completions": 1,
                        "analytics_failures": 0,
                        "analytics_rendered_boxes": 0,
                    }
                    for marker in (
                        "rtsp://",
                        "192.0.2.",
                        "synthetic-profile",
                        session,
                        "source_uri",
                        "device_id",
                        "payload",
                        str(tmp_path),
                    ):
                        assert marker not in response.text
                    assert harness.providers[-1].close_calls == 1
                    assert harness.runtimes[-1].closed
                    assert harness.app.state.operator_launch_coordinator.active_launches == 0

    asyncio.run(scenario())
    assert harness.resolver_calls == harness.factory_calls == 3
    assert len({id(provider.inner) for provider in harness.providers}) == 3
    assert len({id(backend) for backend in harness.backends}) == 3
    assert [backend.indices for backend in harness.backends] == [[0], [0], [0]]
    assert all(provider.close_calls == 1 for provider in harness.providers)
