from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from k5vision.domain.devices import Device, DeviceProtocol
from k5vision.identity_state import initialize_identity_state, load_identity_state
from k5vision.media.mediamtx_config import MediaMTXSource
from k5vision.media.mediamtx_provider import MediaMTXBinding
from k5vision.operator_product_service import (
    OperatorProductError,
    OperatorProductService,
    admit_native_executable,
)
from k5vision.operator_profile_lock import OperatorProfileLock, OperatorProfileLockError


class _Process:
    def __init__(self, arguments, *, cwd):
        self.arguments = arguments
        self.cwd = cwd
        self.returncode = None
        self.stopped = 0
        self.fail_stop = False
        self.paths = {}
        if arguments[0].endswith("media.exe"):
            self.paths = json.loads(Path(arguments[1]).read_text())["paths"]

    def poll(self):
        return self.returncode

    def stop(self):
        self.stopped += 1
        if self.fail_stop:
            raise RuntimeError("Owned process cleanup failed")
        self.returncode = 0


class _Provider:
    async def info(self):
        return None


class _Storage:
    allowed = True

    def __init__(self, _root, *, max_bytes):
        self.max_bytes = max_bytes

    def inspect(self):
        return SimpleNamespace(recording_allowed=self.allowed)

    prune = inspect


@pytest.fixture
def native(tmp_path, monkeypatch):
    import k5vision.operator_native_process as process_module
    import k5vision.operator_product_service as module
    import k5vision.operator_recording_storage as storage_module

    original_lstat = Path.lstat

    def trusted_system_ancestor(path, *args, **kwargs):
        info = original_lstat(path, *args, **kwargs)
        if os.name == "posix" and path in (Path("/"), Path("/tmp")) and info.st_uid == 65534:
            values = list(info)
            values[4] = 0
            return os.stat_result(values)
        return info

    monkeypatch.setattr(Path, "lstat", trusted_system_ancestor)
    monkeypatch.setattr(module, "admit_native_executable", lambda path, _component: path)
    monkeypatch.setattr(module, "MediaMTXProvider", lambda **_kwargs: _Provider())
    monkeypatch.setattr(module, "generate_operator_gateway_configuration", lambda **_kwargs: "{}")
    monkeypatch.setattr(storage_module, "RecordingStorage", _Storage)
    monkeypatch.setattr(_Storage, "allowed", True)
    processes = []

    def process_factory(arguments, *, cwd):
        process = _Process(arguments, cwd=cwd)
        processes.append(process)
        return process

    monkeypatch.setattr(process_module, "OwnedNativeProcess", process_factory)
    service = OperatorProductService(
        site_id="pilot-site",
        state_root=tmp_path / "state",
        recording_root=tmp_path / "recordings",
        media_executable=tmp_path / "media.exe",
        gateway_executable=tmp_path / "gateway.exe",
        public_port=18080,
        control_port=18081,
        api_port=18082,
        playback_port=18083,
        rtsp_port=18084,
        hls_port=18085,
    )
    return SimpleNamespace(
        service=service,
        processes=processes,
        process_factory=process_factory,
        process_module=process_module,
        module=module,
        storage_module=storage_module,
    )


def _source(index):
    device = Device(
        id=UUID(int=index),
        name=f"Camera {index}",
        host=f"192.0.2.{index}",
        protocols={DeviceProtocol.RTSP},
    )
    return MediaMTXSource(
        MediaMTXBinding("pilot-site", device.id), device, f"rtsp://{device.host}/live"
    )


async def _until(predicate, *, timeout=1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    assert predicate(), "Owned worker did not reconcile its desired state"


def _fast_maintenance(native, monkeypatch):
    original_sleep = asyncio.sleep
    clock = SimpleNamespace(value=100.0)
    monkeypatch.setattr(native.module, "time", SimpleNamespace(monotonic=lambda: clock.value))

    async def sleep(delay):
        if delay >= 0.1:
            clock.value += delay
            await original_sleep(0)
        else:
            await original_sleep(delay)

    monkeypatch.setattr(native.module.asyncio, "sleep", sleep)


def _fake_mutations(native, monkeypatch, *, fail_once=None):
    attempts = []
    failed = False

    def mutation(method, route, payload=None):
        nonlocal failed
        attempts.append((method, route))
        if fail_once is not None and not failed and fail_once(method, route, len(attempts)):
            failed = True
            raise OperatorProductError("Camera configuration could not be applied")
        path = route.rsplit("/", 1)[1]
        paths = native.service._media.paths
        if "/add/" in route:
            if path in paths:
                raise OperatorProductError("Camera configuration could not be applied")
            paths[path] = dict(payload)
        elif "/replace/" in route:
            if path not in paths:
                raise OperatorProductError("Camera configuration could not be applied")
            paths[path] = dict(payload)
        elif "/delete/" in route:
            if path not in paths:
                raise OperatorProductError("Camera configuration could not be applied")
            del paths[path]
        elif "/patch/" in route:
            if path not in paths:
                raise OperatorProductError("Camera configuration could not be applied")
            paths[path].update(payload)
        else:
            pytest.fail("Unexpected configuration route")

    monkeypatch.setattr(native.service, "_mutation", mutation)
    return attempts


def test_empty_start_then_camera_add_keeps_existing_native_workers(native, monkeypatch):
    _fake_mutations(native, monkeypatch)

    async def scenario():
        service = native.service
        try:
            await service.start(())
            media, gateway = service._media, service._gateway
            assert service.status() == "ready"
            assert media.paths == {}
            await service.apply_sources((_source(1),))
            await service.apply_sources((_source(1), _source(2)))
            assert service._media is media and service._gateway is gateway
            assert set(media.paths) == {_source(1).binding.path_name, _source(2).binding.path_name}
            config = json.loads(service._media_file.read_text())
            permissions = config["authInternalUsers"][0]["permissions"]
            assert all(item["action"] != "publish" for item in permissions)
            assert config["hlsCDNSecret"] == service.cdn_secret
            assert all(
                "rtsp://" not in argument for p in native.processes for argument in p.arguments
            )
        finally:
            await service.stop()
        assert service.status() == "unavailable"
        assert all(process.stopped == 1 for process in native.processes)

    asyncio.run(scenario())


def test_gateway_start_failure_cleans_up_already_started_media(native, monkeypatch):
    def fail_gateway(arguments, *, cwd):
        if arguments[0].endswith("gateway.exe"):
            raise RuntimeError("Native gateway startup failed")
        return native.process_factory(arguments, cwd=cwd)

    monkeypatch.setattr(native.process_module, "OwnedNativeProcess", fail_gateway)

    async def scenario():
        with pytest.raises(OperatorProductError, match="startup"):
            await native.service.start((_source(1),))
        assert native.service.status() == "unavailable"
        assert native.processes and all(process.stopped == 1 for process in native.processes)

    asyncio.run(scenario())


def test_dead_media_worker_restarts_from_saved_sources(native, monkeypatch):
    _fast_maintenance(native, monkeypatch)

    async def scenario():
        service = native.service
        try:
            await service.start((_source(1),))
            media = service._media
            media.returncode = 17
            await _until(lambda: service._media is not media and service.status() == "ready")
            assert media.stopped == 1
            assert set(service._media.paths) == {_source(1).binding.path_name}
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_partial_camera_update_is_reconciled_without_a_manual_process_crash(native, monkeypatch):
    _fast_maintenance(native, monkeypatch)
    _fake_mutations(native, monkeypatch, fail_once=lambda _method, _route, attempt: attempt == 2)

    async def scenario():
        service = native.service
        try:
            await service.start(())
            with pytest.raises(OperatorProductError):
                await service.apply_sources((_source(1), _source(2)))
            desired = {_source(1).binding.path_name, _source(2).binding.path_name}
            await _until(
                lambda: set(service._media.paths) == desired and service.status() == "ready"
            )
            assert service._paths == desired
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_partial_disk_pause_retries_until_every_camera_stops_recording(native, monkeypatch):
    _fast_maintenance(native, monkeypatch)
    _fake_mutations(
        native,
        monkeypatch,
        fail_once=lambda method, _route, attempt: method == "PATCH" and attempt == 2,
    )

    async def scenario():
        service = native.service
        try:
            await service.start((_source(1), _source(2)))
            _Storage.allowed = False
            await _until(
                lambda: (
                    service.status() == "disk_full"
                    and all(not path["record"] for path in service._media.paths.values())
                )
            )
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_stop_attempts_media_cleanup_when_gateway_cleanup_fails(native):
    async def scenario():
        service = native.service
        await service.start(())
        media, gateway = service._media, service._gateway
        gateway.fail_stop = True
        try:
            with pytest.raises(RuntimeError):
                await service.stop()
            assert media.stopped == 1
        finally:
            gateway.fail_stop = False
            await service.stop()

    asyncio.run(scenario())


def test_cancelled_start_waits_for_inflight_spawn_before_cleanup(native, monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def gated_spawn(arguments, *, cwd):
        if arguments[0].endswith("media.exe"):
            entered.set()
            assert release.wait(3)
        return native.process_factory(arguments, cwd=cwd)

    monkeypatch.setattr(native.process_module, "OwnedNativeProcess", gated_spawn)

    async def scenario():
        service = native.service
        startup = asyncio.create_task(service.start(()))
        await _until(entered.is_set)
        startup.cancel()
        cleanup = asyncio.create_task(service.stop())
        try:
            await asyncio.sleep(0.02)
            assert not cleanup.done(), "Cleanup returned while a native spawn was still running"
        finally:
            release.set()
            await asyncio.gather(startup, cleanup, return_exceptions=True)
            await asyncio.sleep(0.02)
            await service.stop()
        assert native.processes and all(process.stopped >= 1 for process in native.processes)

    asyncio.run(scenario())


def test_storage_admission_failure_cannot_leave_a_ready_recording_worker(native, monkeypatch):
    class RefusedStorage:
        def __init__(self, *_args, **_kwargs):
            raise RuntimeError("Recording storage root admission failed")

    monkeypatch.setattr(native.storage_module, "RecordingStorage", RefusedStorage)

    async def scenario():
        service = native.service
        try:
            with pytest.raises(OperatorProductError):
                await service.start((_source(1),))
            assert service.status() == "unavailable"
            assert all(process.stopped >= 1 for process in native.processes)
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_executable_admission_refuses_changed_bytes_and_linked_paths(native, tmp_path, monkeypatch):
    executable = tmp_path / "admitted.exe"
    content = b"immutable-native-fixture"
    executable.write_bytes(content)
    executable.chmod(0o600)
    manifest = {
        "artifacts": [
            {
                "platform": "windows_amd64" if os.name == "nt" else "linux_amd64",
                "executable_sha256": hashlib.sha256(content).hexdigest(),
                "executable_bytes": len(content),
            }
        ]
    }
    original_read = Path.read_text

    def manifest_read(path, *args, **kwargs):
        if path.name == "mediamtx-donor-manifest.json":
            return json.dumps(manifest)
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", manifest_read)
    assert admit_native_executable(executable, "mediamtx") == executable
    executable.write_bytes(b"changed-native-fixture")
    with pytest.raises(OperatorProductError, match="identity was refused"):
        admit_native_executable(executable, "mediamtx")
    executable.write_bytes(content)
    linked = tmp_path / "linked.exe"
    os.link(executable, linked)
    with pytest.raises(OperatorProductError, match="identity was refused"):
        admit_native_executable(linked, "mediamtx")


def _product_application(
    native,
    tmp_path,
    monkeypatch,
    *,
    fail_stop=False,
    fail_start=False,
    identity_directory=None,
):
    import k5vision.operator_product as product_module

    identity = (
        initialize_identity_state(
            tmp_path / "identity", username="pilot-admin", password="local-test-password"
        )
        if identity_directory is None
        else load_identity_state(identity_directory)
    )

    class Cameras:
        closed = False

        def __init__(self, *_args, **_kwargs):
            pass

        def list_sources(self):
            return ()

        def close(self):
            self.closed = True

    monkeypatch.setattr(product_module, "OperatorCameraStore", Cameras)
    calls = []

    async def start(sources):
        calls.append(("start", sources))
        if fail_start:
            raise OperatorProductError("Native startup was refused")

    async def stop():
        calls.append(("stop", ()))
        if fail_stop:
            raise OperatorProductError("Native operator cleanup was incomplete")

    service = SimpleNamespace(
        site_id=identity.site_id,
        public_port=8080,
        provider=_Provider(),
        start=start,
        stop=stop,
        status=lambda: "ready",
    )
    application = product_module.create_operator_product_app(
        identity_directory=identity.directory, service=service
    )
    return application, calls


def _assert_profile_lease_available(directory):
    lease = OperatorProfileLock(directory)
    try:
        lease.acquire()
    finally:
        lease.close()


def test_product_lifespan_uses_existing_human_identity_and_closes_its_camera_store(
    native, tmp_path, monkeypatch
):
    app, calls = _product_application(native, tmp_path, monkeypatch)
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "pilot-admin", "password": "local-test-password"},
        )
        assert login.status_code == 200
        result = client.get(
            "/api/v1/operator/cameras",
            headers={"Authorization": "Bearer " + login.json()["session_token"]},
        )
        assert result.status_code == 200
        assert result.json() == {"cameras": [], "worker_status": "ready"}
    assert calls == [("start", ()), ("stop", ())]
    assert app.state.operator_camera_store.closed
    _assert_profile_lease_available(tmp_path / "identity")


def test_product_shutdown_closes_camera_store_even_when_native_cleanup_fails(
    native, tmp_path, monkeypatch
):
    app, calls = _product_application(native, tmp_path, monkeypatch, fail_stop=True)
    with pytest.raises(OperatorProductError, match="cleanup"):
        with TestClient(app):
            pass
    assert calls == [("start", ()), ("stop", ())]
    assert app.state.operator_camera_store.closed
    # Incomplete native stop must retain exclusivity, despite store cleanup.
    import k5vision.operator_product as product_module

    lease = OperatorProfileLock(tmp_path / "identity")
    try:
        for _ in range(2):
            with pytest.raises(OperatorProfileLockError):
                lease.acquire()
        assert app.state.operator_profile_lock in product_module._PRODUCT_LEASES.values()
    finally:
        lease.close()
        # Test-only disposal: the service above is a mock with no native worker.
        retained = product_module._PRODUCT_LEASES.pop(id(app.state.operator_profile_lock))
        retained.close()


def test_product_startup_failure_closes_camera_store_and_attempts_native_cleanup(
    native, tmp_path, monkeypatch
):
    app, calls = _product_application(native, tmp_path, monkeypatch, fail_start=True)
    with pytest.raises(OperatorProductError, match="startup"):
        with TestClient(app):
            pass
    assert calls == [("start", ()), ("stop", ())]
    assert app.state.operator_camera_store.closed
    _assert_profile_lease_available(tmp_path / "identity")


def test_second_product_application_cannot_start_workers_for_an_active_identity(
    native, tmp_path, monkeypatch
):
    first, first_calls = _product_application(native, tmp_path, monkeypatch)
    second, second_calls = _product_application(
        native, tmp_path, monkeypatch, identity_directory=tmp_path / "identity"
    )
    with TestClient(first) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "pilot-admin", "password": "local-test-password"},
        )
        assert login.status_code == 200
        headers = {"Authorization": "Bearer " + login.json()["session_token"]}
        with pytest.raises(OperatorProfileLockError):
            with TestClient(second):
                pytest.fail("A second operator application obtained the active profile")
        assert not any(operation == "start" for operation, _ in second_calls)
        assert second.state.operator_camera_store.closed
        assert first_calls == [("start", ())]
        assert not first.state.operator_camera_store.closed
        response = client.get("/api/v1/operator/cameras", headers=headers)
        assert response.status_code == 200
        assert response.json() == {"cameras": [], "worker_status": "ready"}
        probe = OperatorProfileLock(tmp_path / "identity")
        try:
            with pytest.raises(OperatorProfileLockError):
                probe.acquire()
        finally:
            probe.close()
    assert first_calls == [("start", ()), ("stop", ())]
    _assert_profile_lease_available(tmp_path / "identity")
