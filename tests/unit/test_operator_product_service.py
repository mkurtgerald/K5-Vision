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


class _Storage:
    allowed = True

    def __init__(self, _root, *, max_bytes):
        self.max_bytes = max_bytes

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
            assert media.stopped == 0
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

    monkeypatch.setattr(person_module, "load_person_configuration", refuse)
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

    class Broker:
        def __init__(self, _config):
            pass

        async def aclose(self):
            closed.append(True)

    monkeypatch.setattr(person_module, "load_person_configuration", lambda _environment: object())
    monkeypatch.setattr(broker_module, "OperatorAnalyticsBroker", Broker)
    app, calls = _product_application(native, tmp_path, monkeypatch, fail_start=True)
    with pytest.raises(OperatorProductError, match="startup"):
        with TestClient(app):
            pass
    assert closed == [True]
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
