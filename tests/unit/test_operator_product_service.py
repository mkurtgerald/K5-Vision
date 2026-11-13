from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
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
from k5vision.operator_role_policy import PERMISSIONS
from k5vision.operator_time import HostClockObserver


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

    async def aclose(self):
        return None


class _Storage:
    allowed = True

    def __init__(self, _root, *, max_bytes, min_free_bytes=1024**3, retention_seconds=86400):
        self.max_bytes = max_bytes
        self.min_free_bytes = min_free_bytes
        self.retention_seconds = retention_seconds

    def configure_policy(self, *, max_bytes, min_free_bytes, retention_seconds):
        self.max_bytes = max_bytes
        self.min_free_bytes = min_free_bytes
        self.retention_seconds = retention_seconds

    def inspect(self):
        return SimpleNamespace(
            recording_allowed=self.allowed, total_bytes=100, free_bytes=2 * 1024**3
        )

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
    # This native-process harness uses a synthetic site and no user database.
    # Real storage-policy tests exercise the durable component anchor separately.
    import k5vision.operator_storage_settings as settings_module

    class SyntheticIdentityAnchor:
        def __init__(self, *_args, **_options):
            pass

        def seal(self):
            pass

    monkeypatch.setattr(settings_module, "ComponentStateAnchor", SyntheticIdentityAnchor)
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
    monkeypatch.setattr(service, "_check_gateway", lambda: None)
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


async def _until(predicate, *, timeout=3):
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
            # Preserve logical cadence without a busy loop that starves the
            # native-thread wakeup on Windows' coarser event-loop clock.
            await original_sleep(0.001)
        else:
            await original_sleep(delay)

    monkeypatch.setattr(native.module.asyncio, "sleep", sleep)


def _fake_mutations(native, monkeypatch, *, fail_once=None):
    attempts = []
    failed = False

    def mutation(method, route, payload=None, *, timeout=3.0):
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


@pytest.mark.parametrize("same_uri", [False, True])
@pytest.mark.parametrize("recording_enabled", [False, True])
def test_source_refresh_restarts_only_selected_camera_and_preserves_workers(
    native, monkeypatch, same_uri, recording_enabled
):
    attempts = _fake_mutations(native, monkeypatch)

    async def scenario():
        service = native.service
        first = replace(_source(1), recording_enabled=recording_enabled)
        second = _source(2)
        replacement = first if same_uri else replace(first, source_uri="rtsp://192.0.2.1/new")
        await service.start((first, second))
        workers = service._media, service._gateway
        generation = service.media_generation
        before = json.loads(service._media_file.read_bytes())
        commits = []

        async def persist():
            saved = json.loads(service._media_file.read_bytes())
            assert saved["paths"][first.binding.path_name]["source"] == replacement.source_uri
            commits.append(replacement)

        try:
            await service.refresh_camera_source(first, replacement, persist=persist)
            assert attempts == [("POST", f"/v3/config/paths/replace/{first.binding.path_name}")] * (
                2 if same_uri else 1
            )
            assert service._sources == (replacement, second)
            assert commits == [replacement]
            assert (service._media, service._gateway) == workers
            assert service.media_generation == generation and len(native.processes) == 2
            assert all(process.stopped == 0 for process in workers)
            saved = json.loads(service._media_file.read_bytes())
            assert saved["paths"] == service._media.paths
            assert (
                saved["paths"][second.binding.path_name]
                == before["paths"][second.binding.path_name]
            )
            assert saved["paths"][first.binding.path_name]["record"] is recording_enabled
            assert saved["paths"][first.binding.path_name]["sourceOnDemand"] is False
            assert service.status() == "ready" and not service._needs_reconcile
            assert service.camera_source_refresh_status(first.binding) == "ready"
        finally:
            await service.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure", ["remote_before", "remote_after", "timeout", "config", "persist"]
)
def test_source_refresh_compensates_target_without_disturbing_other_camera(
    native, monkeypatch, failure
):
    attempts = _fake_mutations(native, monkeypatch)

    async def scenario():
        service = native.service
        first, second = _source(1), _source(2)
        replacement = replace(first, source_uri="rtsp://user:private@192.0.2.1/changed")
        await service.start((first, second))
        workers = service._media, service._gateway
        original = service._media_file.read_bytes()
        original_paths = json.loads(original)["paths"]
        original_mutation, original_write = service._mutation, service._write_media
        calls = 0
        committed = []

        def mutation(*args, **kwargs):
            nonlocal calls
            calls += 1
            assert kwargs.get("timeout", 3) == 3
            if calls == 1 and failure in {"remote_before", "timeout"}:
                if failure == "timeout":
                    raise TimeoutError("private native URL")
                raise RuntimeError("private native URL")
            original_mutation(*args, **kwargs)
            if calls == 1 and failure == "remote_after":
                raise RuntimeError("private native URL")

        writes = 0

        def write():
            nonlocal writes
            writes += 1
            original_write()
            if writes == 1 and failure == "config":
                raise OSError("private configuration path")

        async def persist():
            if failure == "persist":
                raise RuntimeError("private database path")
            committed.append(True)

        monkeypatch.setattr(service, "_mutation", mutation)
        monkeypatch.setattr(service, "_write_media", write)
        try:
            with pytest.raises(OperatorProductError) as error:
                await service.refresh_camera_source(first, replacement, persist=persist)
            assert "private" not in str(error.value)
            assert not committed
            assert service._sources == (first, second)
            assert service._media_file.read_bytes() == original
            assert service._media.paths == original_paths
            assert all(route.endswith(first.binding.path_name) for _, route in attempts)
            assert all(method == "POST" for method, _ in attempts)
            assert (service._media, service._gateway) == workers
            assert len(native.processes) == 2 and all(p.stopped == 0 for p in workers)
            assert service.status() == "ready" and not service._needs_reconcile
            assert service.camera_source_refresh_status(first.binding) == "ready"
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_uncertain_source_rollback_reports_target_recovery_then_repairs_without_restart(
    native, monkeypatch
):
    _fake_mutations(native, monkeypatch)
    _fast_maintenance(native, monkeypatch)

    async def scenario():
        service = native.service
        first, second = _source(1), _source(2)
        await service.start((first, second))
        workers = service._media, service._gateway
        original = service._media_file.read_bytes()
        original_mutation = service._mutation
        calls = []

        def ambiguous(*args, **kwargs):
            calls.append(args)
            if len(calls) == 2:
                raise TimeoutError("rollback did not acknowledge")
            original_mutation(*args, **kwargs)
            if len(calls) == 1:
                raise TimeoutError("update applied without acknowledgement")

        monkeypatch.setattr(service, "_mutation", ambiguous)

        async def persist():
            pytest.fail("Failed native update must not commit")

        try:
            with pytest.raises(OperatorProductError):
                await service.refresh_camera_source(
                    first, replace(first, source_uri="rtsp://192.0.2.1/new"), persist=persist
                )
            assert service.camera_source_refresh_status(first.binding) == "recovery_required"
            assert service.camera_source_refresh_status(second.binding) == "ready"
            assert service.status() == "ready" and not service._needs_reconcile
            assert service._sources == (first, second)
            await _until(lambda: service.camera_source_refresh_status(first.binding) == "ready")
            assert len(calls) == 3
            assert all(args[1].endswith(first.binding.path_name) for args in calls)
            assert service._media_file.read_bytes() == original
            assert service._media.paths == json.loads(original)["paths"]
            assert (service._media, service._gateway) == workers and len(native.processes) == 2
        finally:
            await service.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["remote", "config"])
@pytest.mark.parametrize("same_uri", [False, True])
def test_cancelled_source_refresh_joins_late_mutation_before_target_compensation(
    native, monkeypatch, stage, same_uri
):
    attempts = _fake_mutations(native, monkeypatch)
    entered, release = threading.Event(), threading.Event()

    async def scenario():
        service = native.service
        first, second = _source(1), _source(2)
        await service.start((first, second))
        workers = service._media, service._gateway
        saved = service._media_file.read_bytes()
        original = service._mutation if stage == "remote" else service._write_media
        calls = 0

        def held(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                assert release.wait(3)
            return original(*args, **kwargs)

        monkeypatch.setattr(service, "_mutation" if stage == "remote" else "_write_media", held)

        async def persist():
            pytest.fail("Cancelled request must not reach persistence")

        try:
            replacement = first if same_uri else replace(first, source_uri="rtsp://192.0.2.1/new")
            refresh = asyncio.create_task(
                service.refresh_camera_source(first, replacement, persist=persist)
            )
            await _until(entered.is_set)
            refresh.cancel()
            await asyncio.sleep(0.02)
            refresh.cancel()
            assert not refresh.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await refresh
            assert service._sources == (first, second)
            assert service._media_file.read_bytes() == saved
            assert service._media.paths == json.loads(saved)["paths"]
            assert all(route.endswith(first.binding.path_name) for _, route in attempts)
            assert (service._media, service._gateway) == workers
            assert all(p.stopped == 0 for p in workers)
            assert service.status() == "ready" and not service._needs_reconcile
        finally:
            release.set()
            await service.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("mismatch", ["previous", "binding", "device", "recording", "private_uri"])
def test_source_refresh_refuses_stale_or_different_camera_identity(native, monkeypatch, mismatch):
    attempts = _fake_mutations(native, monkeypatch)

    async def scenario():
        service = native.service
        first, second = _source(1), _source(2)
        await service.start((first, second))
        saved = service._media_file.read_bytes()
        previous, replacement = first, replace(first, source_uri="rtsp://192.0.2.1/new")
        if mismatch == "previous":
            previous = replace(first, source_uri="rtsp://192.0.2.1/stale")
        elif mismatch == "binding":
            replacement = second
        elif mismatch == "device":
            replacement = replace(
                replacement, device=first.device.model_copy(update={"name": "Other"})
            )
        elif mismatch == "recording":
            replacement = replace(replacement, recording_enabled=False)
        else:
            object.__setattr__(replacement, "source_uri", "http://192.0.2.1/invalid")

        async def persist():
            pytest.fail("Unadmitted source must not commit")

        try:
            with pytest.raises(OperatorProductError, match="selection changed"):
                await service.refresh_camera_source(previous, replacement, persist=persist)
            assert attempts == []
            assert service._sources == (first, second)
            assert service._media_file.read_bytes() == saved
            assert service._media.paths == json.loads(saved)["paths"]
        finally:
            await service.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("same_uri", [False, True])
def test_stop_joins_cancelled_source_compensation_before_releasing_native_processes(
    native, monkeypatch, same_uri
):
    attempts = _fake_mutations(native, monkeypatch)
    update_entered, update_release = threading.Event(), threading.Event()
    rollback_entered, rollback_release = threading.Event(), threading.Event()

    async def scenario():
        service = native.service
        first, second = _source(1), _source(2)
        await service.start((first, second))
        workers = service._media, service._gateway
        saved = service._media_file.read_bytes()
        mutation = service._mutation
        calls = 0

        def held(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                update_entered.set()
                assert update_release.wait(3)
            elif calls == 2:
                rollback_entered.set()
                assert rollback_release.wait(3)
            mutation(*args, **kwargs)

        monkeypatch.setattr(service, "_mutation", held)

        async def persist():
            pytest.fail("Cancelled update must not persist")

        replacement = first if same_uri else replace(first, source_uri="rtsp://192.0.2.1/new")
        refresh = asyncio.create_task(
            service.refresh_camera_source(first, replacement, persist=persist)
        )
        cleanup = None
        try:
            await _until(update_entered.is_set)
            refresh.cancel()
            update_release.set()
            await _until(rollback_entered.is_set)
            refresh.cancel()
            cleanup = asyncio.create_task(service.stop())
            await asyncio.sleep(0.02)
            assert not cleanup.done() and not refresh.done()
            assert all(process.stopped == 0 for process in workers)
            assert service._sources == (first, second)
            rollback_release.set()
            with pytest.raises(asyncio.CancelledError):
                await refresh
            await cleanup
            assert service._media_file.read_bytes() == saved
            assert all(process.stopped == 1 for process in workers)
            replaces = [route for method, route in attempts if method == "POST"]
            assert replaces == [f"/v3/config/paths/replace/{first.binding.path_name}"] * 2
            assert not service._source_refreshes
        finally:
            update_release.set()
            rollback_release.set()
            await asyncio.gather(refresh, *([cleanup] if cleanup else []), return_exceptions=True)
            if service._media is not None:
                await service.stop()

    asyncio.run(scenario())


def test_stale_source_recovery_never_overwrites_a_newer_committed_source(native, monkeypatch):
    attempts = _fake_mutations(native, monkeypatch)

    async def scenario():
        service = native.service
        first, second = _source(1), _source(2)
        replacement = replace(first, source_uri="rtsp://192.0.2.1/new")
        await service.start((first, second))

        async def persist():
            return None

        try:
            await service.refresh_camera_source(first, replacement, persist=persist)
            saved = service._media_file.read_bytes()
            # Model an old queued recovery after a later successful intent.
            service._source_recovery[first.binding.path_name] = (first, 0)
            async with service._lock:
                await service._retry_camera_source()
            assert len(attempts) == 1
            assert service._sources == (replacement, second)
            assert service._media_file.read_bytes() == saved
            assert service._media.paths[first.binding.path_name]["source"] == replacement.source_uri
            assert not service._source_recovery
            assert service.camera_source_refresh_status(first.binding) == "ready"
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_source_refresh_respects_disk_pause_and_rejects_unavailable_worker(native, monkeypatch):
    attempts = _fake_mutations(native, monkeypatch)

    async def scenario():
        service = native.service
        first, second = _source(1), _source(2)
        _Storage.allowed = False
        await service.start((first, second))
        commits = []

        async def persist():
            commits.append(True)

        try:
            await service.refresh_camera_source(first, first, persist=persist)
            assert service.status() == "disk_full"
            assert not any(path["record"] for path in service._media.paths.values())
            assert len(attempts) == 2 and commits == [True]
            assert service._media.paths[first.binding.path_name]["sourceOnDemand"] is False
            saved = service._media_file.read_bytes()
            service._ready = False
            with pytest.raises(OperatorProductError, match="unavailable"):
                await service.refresh_camera_source(first, first, persist=persist)
            assert len(attempts) == 2 and commits == [True]
            assert service._media_file.read_bytes() == saved
            assert service.camera_source_refresh_status(first.binding) == "unavailable"
            with pytest.raises(ValueError):
                service.camera_source_refresh_status(MediaMTXBinding("other-site", first.device.id))
        finally:
            await service.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("failed_call", [1, 2])
@pytest.mark.parametrize("applied_before_failure", [False, True])
def test_unchanged_uri_refresh_restores_continuous_acquisition_after_either_post_fails(
    native, monkeypatch, failed_call, applied_before_failure
):
    attempts = _fake_mutations(native, monkeypatch)

    async def scenario():
        service = native.service
        first, second = _source(1), _source(2)
        await service.start((first, second))
        saved = service._media_file.read_bytes()
        workers = service._media, service._gateway
        mutation = service._mutation
        calls = 0

        def ambiguous(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == failed_call and not applied_before_failure:
                raise TimeoutError("private native timeout")
            mutation(*args, **kwargs)
            if calls == failed_call:
                raise TimeoutError("private native timeout")

        monkeypatch.setattr(service, "_mutation", ambiguous)

        async def persist():
            pytest.fail("Partial refresh must not persist")

        try:
            with pytest.raises(OperatorProductError):
                await service.refresh_camera_source(first, first, persist=persist)
            assert service._sources == (first, second)
            assert service._media_file.read_bytes() == saved
            assert service._media.paths == json.loads(saved)["paths"]
            assert service._media.paths[first.binding.path_name]["sourceOnDemand"] is False
            assert all(route.endswith(first.binding.path_name) for _, route in attempts)
            assert (service._media, service._gateway) == workers
            assert service.status() == "ready" and not service._needs_reconcile
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_stop_closes_each_active_writer_before_workers_without_rewriting_saved_config(
    native, monkeypatch
):
    mutations = _fake_mutations(native, monkeypatch)

    async def scenario():
        service = native.service
        await service.start((_source(1), _source(2)))
        media, gateway = service._media, service._gateway
        saved = service._media_file.read_bytes()
        original_sources, original_paths = service._sources, service._paths.copy()
        original_stop = gateway.stop

        def stop_gateway():
            assert len(mutations) == 2
            assert all(not path["record"] for path in media.paths.values())
            original_stop()

        monkeypatch.setattr(gateway, "stop", stop_gateway)
        await service.stop()
        assert set(mutations) == {
            ("PATCH", f"/v3/config/paths/patch/{path}") for path in original_paths
        }
        assert service._media_file.read_bytes() == saved
        assert all(settings["record"] for settings in json.loads(saved)["paths"].values())
        assert service._sources == original_sources and service._paths == original_paths
        assert media.stopped == gateway.stopped == 1

    asyncio.run(scenario())


def test_stop_joins_both_native_workers_in_parallel_after_recording_flush(native, monkeypatch):
    mutations = _fake_mutations(native, monkeypatch)

    async def scenario():
        service = native.service
        await service.start((_source(1), _source(2)))
        media, gateway = service._media, service._gateway
        barrier = threading.Barrier(2)

        def checked_stop(process):
            original = process.stop

            def stop():
                assert len(mutations) == 2
                assert all(not path["record"] for path in media.paths.values())
                # A serial stop would strand the first worker at this barrier.
                barrier.wait(timeout=6)
                original()

            return stop

        monkeypatch.setattr(gateway, "stop", checked_stop(gateway))
        monkeypatch.setattr(media, "stop", checked_stop(media))
        await service.stop()
        assert media.stopped == gateway.stopped == 1

    asyncio.run(scenario())


def test_live_storage_policy_updates_existing_manager_and_recorder_paths(native, monkeypatch):
    _fake_mutations(native, monkeypatch)

    async def scenario():
        service = native.service
        await service.start((_source(1), _source(2)))
        manager = service._storage
        try:
            monkeypatch.setattr(_Storage, "allowed", False)
            changed = await service.configure_storage(
                expected_revision=0,
                quota_bytes=30 * 1024**3,
                minimum_free_bytes=2 * 1024**3,
                retention_seconds=3 * 86400,
            )
            assert service._storage is manager
            assert manager.max_bytes == 30 * 1024**3
            assert manager.min_free_bytes == 2 * 1024**3
            assert manager.retention_seconds == 3 * 86400
            assert changed["inspection_available"] and not changed["recording_allowed"]
            assert all(not path["record"] for path in service._media.paths.values())
            assert service.status() == "disk_full"
            monkeypatch.setattr(_Storage, "allowed", True)
            changed = await service.configure_storage(expected_revision=1, retention_seconds=0)
            assert service._storage is manager
            assert changed["recording_allowed"]
            assert all(path["record"] for path in service._media.paths.values())
            assert service.status() == "ready"
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_cancelled_storage_change_and_stop_join_inflight_deletion(native, monkeypatch):
    _fake_mutations(native, monkeypatch)
    entered, release = threading.Event(), threading.Event()

    async def scenario():
        service = native.service
        await service.start((_source(1),))
        service._task.cancel()
        try:
            await service._task
        except asyncio.CancelledError:
            pass
        service._task = None
        manager = service._storage
        original_prune = manager.prune

        def held_prune():
            entered.set()
            assert release.wait(timeout=5)
            return original_prune()

        monkeypatch.setattr(manager, "prune", held_prune)
        update = asyncio.create_task(
            service.configure_storage(expected_revision=0, retention_seconds=0)
        )
        assert await asyncio.to_thread(entered.wait, 3)
        update.cancel()
        cleanup = asyncio.create_task(service.stop())
        await asyncio.sleep(0.05)
        assert not update.done() and not cleanup.done()
        assert service._media.poll() is None
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await update
        await cleanup
        assert not service._storage_operations
        assert service._media is None

    try:
        asyncio.run(scenario())
    finally:
        release.set()


def test_stop_skips_flush_when_media_has_already_exited(native, monkeypatch):
    async def scenario():
        service = native.service
        await service.start((_source(1),))
        media, gateway = service._media, service._gateway
        media.returncode = 19
        monkeypatch.setattr(service, "_mutation", lambda *_args, **_kwargs: pytest.fail("late API"))
        await service.stop()
        assert media.stopped == gateway.stopped == 1

    asyncio.run(scenario())


def test_stop_uses_one_shared_flush_budget_and_still_cleans_unresponsive_worker(
    native, monkeypatch
):
    async def scenario():
        service = native.service
        await service.start(tuple(_source(index) for index in range(1, 65)))
        media, gateway = service._media, service._gateway
        clock = SimpleNamespace(value=100.0)
        monkeypatch.setattr(native.module, "time", SimpleNamespace(monotonic=lambda: clock.value))
        attempts = []

        def unresponsive(_method, _route, _payload=None, *, timeout=3.0):
            attempts.append(timeout)
            clock.value += timeout
            raise OperatorProductError("Camera configuration could not be applied")

        monkeypatch.setattr(service, "_mutation", unresponsive)
        await service.stop()
        assert attempts == [native.module._RECORDING_FLUSH_SECONDS]
        assert media.stopped == gateway.stopped == 1

    asyncio.run(scenario())


def test_cancelled_stop_joins_flush_before_cleanup_or_return(native, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    async def scenario():
        service = native.service
        await service.start((_source(1),))
        media, gateway = service._media, service._gateway

        def held_request(_method, _route, _payload=None, *, timeout=3.0):
            entered.set()
            assert release.wait(3)
            assert media.stopped == gateway.stopped == 0

        monkeypatch.setattr(service, "_mutation", held_request)
        cleanup = asyncio.create_task(service.stop())
        await _until(entered.is_set)
        cleanup.cancel()
        try:
            await asyncio.sleep(0.02)
            assert not cleanup.done()
            assert media.stopped == gateway.stopped == 0
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await cleanup
        assert media.stopped == gateway.stopped == 1

    asyncio.run(scenario())


def test_cancelled_stop_joins_provider_before_authenticated_worker_is_released(native, monkeypatch):
    async def scenario():
        service = native.service
        await service.start(())
        media, gateway = service._media, service._gateway
        entered, release = asyncio.Event(), asyncio.Event()

        async def close_provider():
            entered.set()
            await release.wait()
            assert media.stopped == gateway.stopped == 0

        monkeypatch.setattr(service.provider, "aclose", close_provider)
        cleanup = asyncio.create_task(service.stop())
        await entered.wait()
        cleanup.cancel()
        await asyncio.sleep(0)
        assert not cleanup.done()
        assert media.stopped == gateway.stopped == 0
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await cleanup
        assert media.stopped == gateway.stopped == 1

    asyncio.run(scenario())


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


@pytest.mark.parametrize("component", ["media", "gateway"])
def test_alive_unresponsive_worker_is_recovered_after_repeated_probes(
    native, monkeypatch, component
):
    _fast_maintenance(native, monkeypatch)

    async def scenario():
        service = native.service
        try:
            await service.start((_source(1),))
            failed = getattr(service, "_" + component)
            other = service._gateway if component == "media" else service._media
            attempts = []
            observed_status = []

            def check():
                if getattr(service, "_" + component) is failed:
                    observed_status.append(service.status())
                    attempts.append(True)
                    raise OperatorProductError("Worker is unresponsive")

            async def info():
                check()

            if component == "media":
                monkeypatch.setattr(service.provider, "info", info)
            else:
                monkeypatch.setattr(service, "_check_gateway", check)
            await _until(
                lambda: (
                    getattr(service, "_" + component) is not failed and service.status() == "ready"
                )
            )
            assert len(attempts) == 3
            assert "unavailable" in observed_status
            assert failed.stopped == 1
            assert (service._gateway if component == "media" else service._media) is other
            assert set(service._media.paths) == {_source(1).binding.path_name}
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_transient_health_failure_marks_unavailable_without_restarting_workers(native, monkeypatch):
    _fast_maintenance(native, monkeypatch)

    async def scenario():
        service = native.service
        try:
            await service.start(())
            original = service._media, service._gateway
            attempts = []
            observed_status = []

            def probe():
                observed_status.append(service.status())
                attempts.append(True)
                if len(attempts) == 1:
                    raise OperatorProductError("Temporary listener delay")

            monkeypatch.setattr(service, "_check_gateway", probe)
            await _until(lambda: len(attempts) >= 2 and service.status() == "ready")
            assert observed_status[1] == "unavailable"
            assert (service._media, service._gateway) == original
            assert all(process.stopped == 0 for process in original)
        finally:
            await service.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("total", "free", "allowed", "reason"),
    [
        (100, 2 * 1024**3, True, "ready"),
        (100, 100, False, "free_space"),
        (21 * 1024**3, 2 * 1024**3, False, "capacity"),
        (100, 2 * 1024**3, False, "inspection"),
    ],
)
def test_diagnostics_projects_only_last_aggregate_storage_sample(
    native, total, free, allowed, reason
):
    service = native.service
    service._clock = HostClockObserver(
        utc_now=lambda: datetime(2026, 10, 18, tzinfo=UTC), monotonic=lambda: 100.0
    )
    assert service.diagnostics()["storage"] is None
    service._storage_status = SimpleNamespace(
        total_bytes=total, free_bytes=free, recording_allowed=allowed
    )
    result = service.diagnostics()
    assert result == {
        "worker_status": "unavailable",
        "max_recording_bytes": 20 * 1024**3,
        "server_utc": "2026-10-18T00:00:00.000Z",
        "clock": {
            "status": "observing",
            "last_step_seconds": 0.0,
            "change_count": 0,
            "last_change_at": None,
            "sync_verified": False,
        },
        "storage": {
            "recording_bytes": total,
            "free_bytes": free,
            "recording_allowed": allowed,
            "reason": reason,
        },
    }
    text = json.dumps(result)
    assert service.credentials.password not in text
    assert str(service.state_root) not in text
    assert "rtsp://" not in text


def test_active_recorded_person_inventory_interest_refreshes_private_storage_cache(
    native, monkeypatch
):
    _fast_maintenance(native, monkeypatch)
    sampled_paths = []
    scans = []
    original_prune = _Storage.prune

    def private_inventory(_storage, path):
        sampled_paths.append(path)
        return {}

    def tracked_prune(storage):
        scans.append(native.module.time.monotonic())
        return original_prune(storage)

    monkeypatch.setattr(_Storage, "recording_files", private_inventory, raising=False)
    monkeypatch.setattr(_Storage, "prune", tracked_prune)
    service = native.service
    source = _source(1)

    async def scenario():
        try:
            await service.start((source,))
            await _until(lambda: len(scans) >= 1)
            previous = len(scans)
            request_time = native.module.time.monotonic()
            assert service.recording_files(source.binding) == {}
            assert sampled_paths == [source.binding.path_name]
            # The recorder continues to use its bounded cache, but a recent
            # analytics consumer must see the next retention scan before the
            # idle 15-second cadence, without walking the directory itself.
            await _until(lambda: len(scans) > previous)
            assert 0 <= scans[previous] - request_time <= 10
            assert service.status() == "ready"
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_maintenance_observes_clock_steps_without_pausing_recording(native, monkeypatch):
    _fast_maintenance(native, monkeypatch)
    samples = SimpleNamespace(count=0, monotonic=0.0)
    epoch = datetime(2026, 10, 18, tzinfo=UTC)

    def utc_now():
        samples.count += 1
        samples.monotonic += 2
        step = 5 if samples.count >= 3 else 0
        return epoch + timedelta(seconds=samples.monotonic + step)

    service = native.service
    service._clock = HostClockObserver(utc_now=utc_now, monotonic=lambda: samples.monotonic)

    async def scenario():
        try:
            await service.start((_source(1),))
            original = service._media, service._gateway
            # No diagnostics call supplies the clock observations in this wait.
            await _until(lambda: service._clock._change_count == 1)
            assert samples.count >= 3
            result = service.diagnostics()
            assert result["clock"]["status"] == "changed"
            assert result["clock"]["last_step_seconds"] == 5
            assert result["clock"]["sync_verified"] is False
            assert service.status() == "ready"
            assert not service._disk_full
            assert (service._media, service._gateway) == original
            assert all(process.stopped == 0 for process in original)
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


def test_recording_preference_patches_only_writer_and_survives_worker_restart(native, monkeypatch):
    mutations = _fake_mutations(native, monkeypatch)
    _fast_maintenance(native, monkeypatch)

    async def scenario():
        service = native.service
        first, second = _source(1), _source(2)
        try:
            await service.start((first, second))
            media, gateway = service._media, service._gateway
            original = json.loads(service._media_file.read_text())
            paused = replace(first, recording_enabled=False)
            await service.apply_recording_preferences((paused, second))
            assert mutations == [("PATCH", f"/v3/config/paths/patch/{first.binding.path_name}")]
            assert (service._media, service._gateway) == (media, gateway)
            assert media.paths[first.binding.path_name] == {
                **original["paths"][first.binding.path_name],
                "record": False,
            }
            assert (
                media.paths[second.binding.path_name] == original["paths"][second.binding.path_name]
            )
            assert service._sources == (paused, second)
            generation = service.media_generation
            media.returncode = 19
            await _until(
                lambda: service.media_generation > generation and service.status() == "ready"
            )
            assert service._media.paths[first.binding.path_name]["record"] is False
            assert service._media.paths[second.binding.path_name]["record"] is True
            await service.apply_recording_preferences((first, second))
            assert service._media.paths[first.binding.path_name]["record"] is True
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_storage_recovery_never_resumes_an_operator_paused_camera(native, monkeypatch):
    _fast_maintenance(native, monkeypatch)
    _fake_mutations(native, monkeypatch)

    async def scenario():
        service = native.service
        first, second = replace(_source(1), recording_enabled=False), _source(2)
        try:
            await service.start((first, second))
            _Storage.allowed = False
            await _until(lambda: service.status() == "disk_full")
            assert all(not value["record"] for value in service._media.paths.values())
            _Storage.allowed = True
            await _until(lambda: service.status() == "ready")
            assert service._media.paths[first.binding.path_name]["record"] is False
            assert service._media.paths[second.binding.path_name]["record"] is True
        finally:
            await service.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("initially_allowed", [True, False])
def test_storage_policy_is_not_acknowledged_until_all_writer_patches_finish(
    native, monkeypatch, initially_allowed
):
    _fast_maintenance(native, monkeypatch)
    _fake_mutations(native, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    _Storage.allowed = initially_allowed

    async def scenario():
        service = native.service
        first, second = replace(_source(1), recording_enabled=False), _source(2)
        try:
            await service.start((first, second))
            mutation = service._mutation
            blocked = False

            def held_patch(*args, **kwargs):
                nonlocal blocked
                if not blocked:
                    blocked = True
                    entered.set()
                    assert release.wait(3)
                mutation(*args, **kwargs)

            monkeypatch.setattr(service, "_mutation", held_patch)
            _Storage.allowed = not initially_allowed
            await _until(entered.is_set)
            # The storage sample has changed, but a native writer still has
            # the previous policy. Neither ready nor disk_full is an ack yet.
            assert service.status() == "unavailable"
            assert service._needs_reconcile is True
            release.set()
            expected = "disk_full" if initially_allowed else "ready"
            await _until(lambda: service.status() == expected)
            assert service._needs_reconcile is False
            assert service._media.paths[first.binding.path_name]["record"] is False
            assert service._media.paths[second.binding.path_name]["record"] is (
                not initially_allowed
            )
            assert json.loads(service._media_file.read_text())["paths"] == service._media.paths
        finally:
            release.set()
            await service.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["ambiguous_patch", "configuration_write"])
def test_failed_recording_preference_compensates_old_writer_and_saved_configuration(
    native, monkeypatch, failure
):
    _fake_mutations(native, monkeypatch)

    async def scenario():
        service = native.service
        first, second = _source(1), _source(2)
        try:
            await service.start((first, second))
            original = service._media_file.read_bytes()
            original_operation = (
                service._mutation if failure == "ambiguous_patch" else service._write_media
            )
            failed = False

            def operation(*args, **kwargs):
                nonlocal failed
                result = original_operation(*args, **kwargs)
                if not failed:
                    failed = True
                    raise RuntimeError("private worker diagnostics")
                return result

            monkeypatch.setattr(
                service, "_mutation" if failure == "ambiguous_patch" else "_write_media", operation
            )
            with pytest.raises(OperatorProductError) as error:
                await service.apply_recording_preferences(
                    (replace(first, recording_enabled=False), second)
                )
            assert "private" not in str(error.value)
            assert service._sources == (first, second)
            assert all(value["record"] for value in service._media.paths.values())
            assert service._media_file.read_bytes() == original
            assert service.status() == "ready"
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_cancelled_recording_preference_joins_late_patch_and_restores_previous_intent(
    native, monkeypatch
):
    _fake_mutations(native, monkeypatch)
    entered, release = threading.Event(), threading.Event()

    async def scenario():
        service = native.service
        first = _source(1)
        try:
            await service.start((first,))
            mutation = service._mutation
            first_call = True

            def held_patch(*args, **kwargs):
                nonlocal first_call
                if first_call:
                    first_call = False
                    entered.set()
                    assert release.wait(3)
                mutation(*args, **kwargs)

            monkeypatch.setattr(service, "_mutation", held_patch)
            change = asyncio.create_task(
                service.apply_recording_preferences((replace(first, recording_enabled=False),))
            )
            await _until(entered.is_set)
            change.cancel()
            await asyncio.sleep(0.02)
            assert not change.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await change
            assert service._sources == (first,)
            assert service._media.paths[first.binding.path_name]["record"] is True
            assert (
                json.loads(service._media_file.read_text())["paths"][first.binding.path_name][
                    "record"
                ]
                is True
            )
        finally:
            release.set()
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

        def __init__(self, root, *_args, **_kwargs):
            self._root = root

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
        assert result.json() == {
            "cameras": [],
            "worker_status": "ready",
            "capabilities": {
                "account": {
                    **login.json()["account"],
                    "role_profile_id": None,
                    "effective_permissions": sorted(PERMISSIONS),
                },
                "effective_permissions": sorted(PERMISSIONS),
                "can_view_history": True,
                "can_export": True,
                "can_manage_cameras": True,
                "can_manage_users": True,
                "camera_access": {"mode": "all", "camera_ids": []},
            },
        }
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
    _assert_profile_lease_available(tmp_path / "identity")


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


def test_rejected_optional_person_configuration_does_not_prevent_recording_start(
    native, tmp_path, monkeypatch
):
    import k5vision.operator_analytics_person as person_module

    def refuse(_environment):
        raise person_module.OperatorPersonConfigurationError("Person configuration was refused.")

    monkeypatch.setattr(person_module, "load_installed_person_configuration", refuse)
    app, calls = _product_application(native, tmp_path, monkeypatch)
    with TestClient(app) as client:
        assert client.get("/api/v1/health").status_code == 200
        assert app.state.operator_person_status == "unavailable"
        assert calls == [("start", ())]
    assert calls == [("start", ()), ("stop", ())]
    _assert_profile_lease_available(tmp_path / "identity")


def test_configured_person_broker_closes_even_when_recording_startup_fails(
    native, tmp_path, monkeypatch
):
    import k5vision.operator_analytics as broker_module
    import k5vision.operator_analytics_person as person_module

    closed = []
    view_limits = []

    class Broker:
        def __init__(self, _config, *, max_views):
            view_limits.append(max_views)

        async def aclose(self):
            closed.append(True)

    monkeypatch.setattr(
        person_module, "load_installed_person_configuration", lambda _environment: object()
    )
    monkeypatch.setenv(person_module.PERSON_CONFIG_ENV, "explicit-engineering-test-configuration")
    monkeypatch.setattr(broker_module, "OperatorAnalyticsBroker", Broker)
    app, calls = _product_application(native, tmp_path, monkeypatch, fail_start=True)
    with pytest.raises(OperatorProductError, match="startup"):
        with TestClient(app):
            pass
    assert view_limits == [4]
    assert closed == [True]
    assert calls == [("start", ()), ("stop", ())]
    assert app.state.operator_camera_store.closed
    _assert_profile_lease_available(tmp_path / "identity")


def test_bundled_person_model_enables_bounded_sampled_view_without_override(
    native, tmp_path, monkeypatch
):
    import k5vision.operator_analytics as broker_module
    import k5vision.operator_analytics_person as person_module

    view_limits = []
    preparing = []
    closed = []

    class Broker:
        def __init__(self, _config, *, max_views):
            view_limits.append(max_views)

        def start_preparing(self):
            preparing.append(True)

        async def expire_idle(self):
            pass

        async def aclose(self):
            closed.append(True)

    monkeypatch.delenv(person_module.PERSON_CONFIG_ENV, raising=False)
    monkeypatch.setattr(
        person_module, "load_installed_person_configuration", lambda _environment: object()
    )
    monkeypatch.setattr(broker_module, "OperatorAnalyticsBroker", Broker)
    app, calls = _product_application(native, tmp_path, monkeypatch)
    with TestClient(app) as client:
        assert client.get("/api/v1/health").status_code == 200
        assert app.state.operator_person_status == "configured"
        assert calls == [("start", ())]
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "pilot-admin", "password": "local-test-password"},
        )
        assert login.status_code == 200
        result = client.get(
            "/api/v1/operator/cameras",
            headers={"Authorization": "Bearer " + login.json()["session_token"]},
        )
        assert result.status_code == 200 and result.json()["cameras"] == []
        # A fresh account has no camera to prepare. Preserve login/API
        # responsiveness while keeping the sealed native broker configured.
        assert preparing == []
    assert view_limits == [1]
    assert preparing == []
    assert closed == [True]
    assert calls == [("start", ()), ("stop", ())]
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
        assert response.json() == {
            "cameras": [],
            "worker_status": "ready",
            "capabilities": {
                "account": {
                    **login.json()["account"],
                    "role_profile_id": None,
                    "effective_permissions": sorted(PERMISSIONS),
                },
                "effective_permissions": sorted(PERMISSIONS),
                "can_view_history": True,
                "can_export": True,
                "can_manage_cameras": True,
                "can_manage_users": True,
                "camera_access": {"mode": "all", "camera_ids": []},
            },
        }
        probe = OperatorProfileLock(tmp_path / "identity")
        try:
            with pytest.raises(OperatorProfileLockError):
                probe.acquire()
        finally:
            probe.close()
    assert first_calls == [("start", ()), ("stop", ())]
    _assert_profile_lease_available(tmp_path / "identity")


def test_native_person_warmup_waits_for_quiet_camera_enrollment():
    from k5vision.operator_product_api import _NativePersonWarmupGate

    calls = []

    class Broker:
        def start_preparing(self):
            calls.append("prepare")

    async def exercise():
        gate = _NativePersonWarmupGate(Broker(), quiet_seconds=10)
        gate.schedule()
        first = gate._handle
        assert first is not None and not first.cancelled()
        gate.schedule()
        assert gate._handle is first
        gate.schedule(enrollment=True)
        second = gate._handle
        assert first.cancelled()
        assert second is not None and second is not first and not second.cancelled()
        assert not calls
        gate._begin()
        assert calls == ["prepare"]
        gate.schedule(enrollment=True)
        assert calls == ["prepare"]
        gate.close()
        assert gate._closed

    asyncio.run(exercise())


def test_native_person_warmup_cancelled_on_product_shutdown():
    from k5vision.operator_product_api import _NativePersonWarmupGate

    calls = []

    class Broker:
        def start_preparing(self):
            calls.append("prepare")

    async def exercise():
        gate = _NativePersonWarmupGate(Broker(), quiet_seconds=10)
        gate.schedule()
        pending = gate._handle
        assert pending is not None
        gate.close()
        assert pending.cancelled()
        assert gate._handle is None
        gate._begin()
        gate.schedule(enrollment=True)
        assert calls == []

    asyncio.run(exercise())


def test_native_view_warmup_yields_to_first_recorded_event_without_changing_view_path():
    from k5vision.operator_product_api import _NativePersonWarmupGate

    calls = []
    priority = {"pending": True, "unavailable": False}

    class Broker:
        def start_preparing(self):
            calls.append("prepare")

    def recorded_event_pending():
        if priority["unavailable"]:
            raise RuntimeError("Recorder readiness unavailable")
        return priority["pending"]

    async def exercise():
        gate = _NativePersonWarmupGate(
            Broker(), quiet_seconds=10, defer_preparation=recorded_event_pending
        )
        gate.schedule()
        initial = gate._handle
        gate._begin()
        assert initial.cancelled()
        assert calls == []
        deferred = gate._handle
        assert deferred is not None and not deferred.cancelled()

        priority["unavailable"] = True
        gate._begin()
        assert deferred.cancelled()
        assert calls == []
        assert gate._handle is not None

        priority["unavailable"] = False
        priority["pending"] = False
        pending = gate._handle
        gate._begin()
        assert pending.cancelled()
        assert calls == ["prepare"]
        assert gate._handle is None
        gate.schedule(enrollment=True)
        assert calls == ["prepare"]
        gate.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("previous_failures", [0, 1])
def test_local_provider_capacity_does_not_change_prior_worker_health(
    native, monkeypatch, previous_failures
):
    from k5vision.media.mediamtx_provider import (
        MEDIAMTX_VERSION,
        MediaMTXCredentials,
        MediaMTXProvider,
        MediaWorkerError,
        MediaWorkerErrorCode,
    )

    entered, release = threading.Event(), threading.Event()
    count = 0
    lock = threading.Lock()
    observations = []

    def worker(request, timeout, _limit):
        nonlocal count
        assert timeout == 3
        if request.full_url.endswith("/v3/info"):
            return json.dumps(
                {"version": MEDIAMTX_VERSION, "started": "2026-11-08T01:00:00Z"}
            ).encode()
        with lock:
            count += 1
            if count == 4:
                entered.set()
        assert release.wait(5), "Controlled worker reads were not released"
        return b"[]"

    class ObservedProvider(MediaMTXProvider):
        async def info(self):
            try:
                return await super().info()
            except MediaWorkerError as error:
                observations.append(error.code)
                raise

    async def scenario():
        service = native.service
        held = []
        original_sleep = asyncio.sleep
        ticks = asyncio.Queue()

        async def tick_sleep(delay):
            if delay == 2:
                await ticks.get()
            else:
                await original_sleep(delay)

        try:
            await service.start(())
            service._task.cancel()
            await asyncio.gather(service._task, return_exceptions=True)
            service._task = None
            provider = ObservedProvider(
                site_id=service.site_id,
                api_origin="http://127.0.0.1:18082",
                playback_origin="http://127.0.0.1:18083",
                credentials=MediaMTXCredentials("worker", "x" * 43),
                requester=worker,
            )
            service.provider = provider
            await provider.info()
            service._health_failures["media"] = previous_failures
            service._ready = previous_failures == 0
            held = [
                asyncio.create_task(provider.playback_spans(_source(1).binding)) for _ in range(4)
            ]
            await _until(entered.is_set)
            monkeypatch.setattr(native.module.asyncio, "sleep", tick_sleep)
            service._task = asyncio.create_task(service._maintain())
            ticks.put_nowait(None)
            await _until(lambda: bool(observations))
            # Wait until the observed health result has finished its iteration.
            await _until(lambda: service._lock.locked() is False)
            assert observations == [MediaWorkerErrorCode.BUSY]
            assert service._health_failures["media"] == previous_failures
            assert service.status() == ("ready" if previous_failures == 0 else "unavailable")
            assert provider._version_admitted
            assert all(
                process.poll() is None and not process.stopped for process in native.processes
            )
            release.set()
            assert await asyncio.gather(*held) == [(), (), (), ()]
            if previous_failures:
                ticks.put_nowait(None)
                await _until(lambda: service.status() == "ready")
                assert service._health_failures["media"] == 0
        finally:
            release.set()
            await asyncio.gather(*held, return_exceptions=True)
            await service.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure", ["timeout", "unavailable", "unauthorized", "version", "gateway_busy"]
)
def test_worker_observation_failures_still_block_media_until_success(native, monkeypatch, failure):
    from urllib.error import HTTPError

    from k5vision.media.mediamtx_provider import (
        MEDIAMTX_VERSION,
        MediaMTXCredentials,
        MediaMTXProvider,
        MediaWorkerError,
        MediaWorkerErrorCode,
    )

    counts = {"info": 0, "gateway": 0}

    def worker(_request, timeout, _limit):
        assert timeout == 3
        counts["info"] += 1
        version = MEDIAMTX_VERSION
        if counts["info"] == 2:
            if failure == "timeout":
                raise TimeoutError
            if failure == "unavailable":
                raise OSError
            if failure == "unauthorized":
                raise HTTPError("http://127.0.0.1/info", 401, "", None, None)
            if failure == "version":
                version = "v9"
        return json.dumps({"version": version, "started": "2026-11-08T01:00:00Z"}).encode()

    def gateway():
        counts["gateway"] += 1
        if failure == "gateway_busy" and counts["gateway"] == 1:
            raise MediaWorkerError(MediaWorkerErrorCode.BUSY)

    async def scenario():
        service = native.service
        original_sleep = asyncio.sleep
        ticks = asyncio.Queue()

        async def tick_sleep(delay):
            if delay == 2:
                await ticks.get()
            else:
                await original_sleep(delay)

        try:
            await service.start(())
            service._task.cancel()
            await asyncio.gather(service._task, return_exceptions=True)
            service._task = None
            service.provider = MediaMTXProvider(
                site_id=service.site_id,
                api_origin="http://127.0.0.1:18082",
                playback_origin="http://127.0.0.1:18083",
                credentials=MediaMTXCredentials("worker", "x" * 43),
                requester=worker,
            )
            await service.provider.info()
            monkeypatch.setattr(service, "_check_gateway", gateway)
            monkeypatch.setattr(native.module.asyncio, "sleep", tick_sleep)
            workers = service._media, service._gateway
            service._task = asyncio.create_task(service._maintain())
            ticks.put_nowait(None)
            component = "gateway" if failure == "gateway_busy" else "media"
            await _until(lambda: service._health_failures[component] == 1)
            await _until(lambda: not service._lock.locked())
            assert service.status() == "unavailable"
            assert service.provider._version_admitted is (failure == "gateway_busy")
            assert (service._media, service._gateway) == workers
            assert all(not process.stopped for process in workers)
            ticks.put_nowait(None)
            await _until(lambda: service.status() == "ready")
            assert service._health_failures[component] == 0
            assert service.provider._version_admitted
            assert (service._media, service._gateway) == workers
        finally:
            await service.stop()

    asyncio.run(scenario())
