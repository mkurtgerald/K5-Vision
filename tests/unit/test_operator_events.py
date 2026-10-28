from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from k5vision import identity_state
from k5vision.media.presentation_frame import PixelFormat, PresentationVideoFrame
from k5vision.operator_events import (
    OperatorEventsError,
    OperatorEventService,
    OperatorEventStore,
    RecordedFrameSampler,
    RecordedPersonFrame,
)


@pytest.fixture
def identity(tmp_path, monkeypatch):
    if os.name == "posix":
        original = Path.lstat

        def system_ancestors(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path in (Path("/"), Path("/tmp")) and info.st_uid == 65534:
                fields = list(info)
                fields[4] = 0
                return os.stat_result(fields)
            return info

        monkeypatch.setattr(Path, "lstat", system_ancestors)
    return identity_state.initialize_identity_state(
        tmp_path / "identity", username="event-admin", password="test-event-password"
    )


@pytest.fixture
def store(identity):
    value = OperatorEventStore(identity.directory, identity.site_id)
    yield value
    value.close()


def _record(store, camera, index, when=None, **changes):
    arguments = {
        "frame_key": hashlib.sha256(str(index).encode()).hexdigest(),
        "occurred_at": when or datetime(2026, 10, 28, 12, 0, index, tzinfo=UTC),
        "confidence": 0.9,
        "person_count": 2,
        "emit": True,
    }
    arguments.update(changes)
    return store.record(camera, **arguments)


def test_settings_and_events_survive_restart_with_recorded_time_and_cursor(identity, store):
    camera = uuid4()
    assert store.settings(camera) == {"enabled": False, "confidence_threshold": 0.65}
    store.configure(camera, enabled=True, confidence_threshold=0.7)
    for index in range(1, 5):
        assert _record(store, camera, index)
    first = store.events(camera, limit=2)
    assert [item["id"] for item in first["events"]] == [4, 3]
    assert first["next_before"] == 3 and first["has_more"]
    store.close()
    reopened = OperatorEventStore(identity.directory, identity.site_id)
    try:
        assert reopened.settings(camera) == {"enabled": True, "confidence_threshold": 0.7}
        last = reopened.events(camera, limit=2, before=first["next_before"])
        assert [item["id"] for item in last["events"]] == [2, 1]
        assert last["next_before"] is None and not last["has_more"]
        event = last["events"][0]
        assert event["occurred_at"] == event["playback_at"] == "2026-10-28T12:00:02.000Z"
        assert event["camera_id"] == str(camera) and event["person_count"] == 2
        assert not _record(reopened, camera, 4)
        assert reopened.summary(camera) == {
            "event_count": 4,
            "last_event_at": "2026-10-28T12:00:04.000Z",
        }
    finally:
        reopened.close()


def test_disable_and_suppressed_frames_never_create_events(store):
    camera = uuid4()
    assert not _record(store, camera, 1)
    store.configure(camera, enabled=True, confidence_threshold=0.5)
    assert not _record(store, camera, 1, emit=False)
    assert not _record(store, camera, 1)
    assert not _record(store, camera, 2, person_count=0, confidence=0.0)
    store.configure(camera, enabled=False, confidence_threshold=0.5)
    assert not _record(store, camera, 3)
    assert store.events(camera)["events"] == []


@pytest.mark.parametrize(
    "enabled,confidence", [(1, 0.5), (True, True), (True, 0.49), (True, 1), (True, float("nan"))]
)
def test_settings_reject_coercion_and_invalid_threshold(store, enabled, confidence):
    with pytest.raises(ValueError):
        store.configure(uuid4(), enabled=enabled, confidence_threshold=confidence)


@pytest.mark.parametrize(
    "change",
    [
        {"frame_key": "private-source"},
        {"person_count": 33},
        {"person_count": True},
        {"confidence": float("inf")},
        {"emit": 1},
        {"occurred_at": datetime(2026, 10, 28)},
    ],
)
def test_event_boundary_rejects_invalid_or_unaware_metadata(store, change):
    with pytest.raises(ValueError):
        _record(store, uuid4(), 1, **change)


def test_events_range_and_camera_boundaries_and_durable_append_order_survive_utc_rollback(store):
    camera, foreign = uuid4(), uuid4()
    store.configure(camera, enabled=True, confidence_threshold=0.5)
    now = datetime(2026, 10, 28, 12, tzinfo=UTC)
    assert _record(store, camera, 1, now)
    assert _record(store, camera, 2, now - timedelta(hours=48))
    assert store.events(foreign)["events"] == []
    assert [item["id"] for item in store.events(camera)["events"]] == [2, 1]
    selected = store.events(
        camera, start=now - timedelta(minutes=1), end=now + timedelta(minutes=1)
    )
    assert len(selected["events"]) == 1 and selected["events"][0]["id"] == 1
    assert store.summary(camera)["last_event_at"] == "2026-10-26T12:00:00.000Z"


@pytest.mark.parametrize(
    "options", [{"limit": 0}, {"limit": 101}, {"before": 0}, {"start": datetime.now(UTC)}]
)
def test_event_query_bounds(store, options):
    with pytest.raises(ValueError):
        store.events(uuid4(), **options)


def test_bounded_event_retention_uses_append_identity_not_wall_age(store, monkeypatch):
    import k5vision.operator_events as module

    monkeypatch.setattr(module, "MAX_EVENT_ROWS", 2)
    camera = uuid4()
    store.configure(camera, enabled=True, confidence_threshold=0.5)
    for index in range(1, 5):
        _record(store, camera, index)
    assert [event["id"] for event in store.events(camera)["events"]] == [4, 3]


def test_site_mismatch_and_replaced_private_database_fail_closed(identity, store):
    with pytest.raises(OperatorEventsError):
        OperatorEventStore(identity.directory, "foreign-site")
    original = identity.directory / "operator-events.sqlite3"
    replaced = identity.directory / "old-events.sqlite3"
    original.rename(replaced)
    original.write_bytes(replaced.read_bytes())
    original.chmod(0o600)
    with pytest.raises(OperatorEventsError, match="state unavailable"):
        store.settings(uuid4())


def test_corrupt_site_header_is_refused_on_reopen(identity, store):
    store.close()
    with sqlite3.connect(identity.directory / "operator-events.sqlite3") as connection:
        connection.execute("UPDATE event_state SET site_id='foreign'")
    with pytest.raises(OperatorEventsError, match="admission"):
        OperatorEventStore(identity.directory, identity.site_id)


def _source(camera=None, *, enabled=True):
    return SimpleNamespace(
        binding=SimpleNamespace(device_id=camera or uuid4(), path_name="k5_" + "a" * 64),
        recording_enabled=enabled,
    )


def _sample(index=1, when=None):
    return RecordedPersonFrame(
        PresentationVideoFrame(memoryview(bytes(16)), 2, 2, 8, PixelFormat.BGRX, index),
        when or datetime(2026, 10, 28, 12, tzinfo=UTC),
        hashlib.sha256(str(index).encode()).hexdigest(),
    )


class _Sampler:
    def __init__(self):
        self.index = 0
        self.when = datetime(2026, 10, 28, 12, tzinfo=UTC)

    def capture(self, _source):
        self.index += 1
        return _sample(self.index, self.when)


class _Provider:
    closed = False
    callback = None

    async def __call__(self, _frame):
        if self.callback:
            self.callback()
        return (
            SimpleNamespace(category="person", confidence=0.9),
            SimpleNamespace(category="person", confidence=0.6),
        )

    async def aclose(self):
        self.closed = True


def _service(store, source, sampler=None, provider=None, clock=None):
    sampler, provider = sampler or _Sampler(), provider or _Provider()

    async def factory():
        return provider

    service = OperatorEventService(
        store=store,
        cameras=SimpleNamespace(list_sources=lambda: (source,)),
        recording_root=Path("/unused-test-boundary"),
        configuration=None,
        sampler=sampler,
        provider_factory=factory,
        monotonic=lambda: clock[0] if clock is not None else 100.0,
    )
    return service, sampler, provider


def test_background_sampling_persists_events_without_browser_and_cooldown_is_monotonic(store):
    source, clock = _source(), [100.0]
    service, sampler, provider = _service(store, source, clock=clock)

    async def run():
        try:
            service.configure(source.binding.device_id, enabled=True, confidence_threshold=0.7)
            await service.cycle()
            assert service.status(source.binding.device_id)["status"] == "active"
            assert store.summary(source.binding.device_id)["event_count"] == 1
            clock[0] += 5
            sampler.when -= timedelta(hours=48)
            await service.cycle()
            assert store.summary(source.binding.device_id)["event_count"] == 1
            clock[0] += 5
            await service.cycle()
            rows = store.events(source.binding.device_id)["events"]
            assert len(rows) == 2 and rows[0]["occurred_at"] == "2026-10-26T12:00:00.000Z"
            assert rows[0]["person_count"] == 1
            assert service.status(source.binding.device_id)["processed_frames"] == 3
        finally:
            await service.aclose()

    asyncio.run(run())
    assert provider.closed


def test_reconfigure_during_inference_discards_result(store):
    source = _source()
    service, _sampler, provider = _service(store, source)

    async def run():
        try:
            service.configure(source.binding.device_id, enabled=True, confidence_threshold=0.5)
            provider.callback = lambda: service.configure(
                source.binding.device_id, enabled=False, confidence_threshold=0.5
            )
            await service.cycle()
            assert store.summary(source.binding.device_id)["event_count"] == 0
            assert service.status(source.binding.device_id)["status"] == "disabled"
        finally:
            await service.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("change", ["remove", "pause", "disable_reenable"])
def test_inflight_native_result_rechecks_camera_and_configuration_generation(store, change):
    source = _source()
    service, _sampler, provider = _service(store, source)
    sources = [source]
    service._cameras = SimpleNamespace(list_sources=lambda: tuple(sources))

    def during_inference():
        if change == "remove":
            sources.clear()
        elif change == "pause":
            source.recording_enabled = False
        else:
            service.configure(source.binding.device_id, enabled=False, confidence_threshold=0.5)
            service.configure(source.binding.device_id, enabled=True, confidence_threshold=0.5)

    provider.callback = during_inference

    async def run():
        try:
            service.configure(source.binding.device_id, enabled=True, confidence_threshold=0.5)
            await service.cycle()
            assert store.summary(source.binding.device_id)["event_count"] == 0
        finally:
            await service.aclose()

    asyncio.run(run())


def test_failed_native_provider_cleanup_retains_owner_and_refuses_another_launch(store):
    class StalledProvider:
        closes = 0

        async def __call__(self, _frame):
            raise RuntimeError("native-call-failed")

        async def aclose(self):
            self.closes += 1
            raise RuntimeError("native-cleanup-incomplete")

    source, provider = _source(), StalledProvider()
    service, _sampler, _ = _service(store, source, provider=provider)

    async def run():
        service.configure(source.binding.device_id, enabled=True, confidence_threshold=0.5)
        await service.cycle()
        assert service._provider is provider
        assert service._provider_failed
        await service.cycle()
        assert provider.closes == 1
        with pytest.raises(OperatorEventsError, match="cleanup is incomplete"):
            await service.aclose()
        assert service._provider is provider

    asyncio.run(run())


def test_stalled_owned_native_worker_is_killed_and_reaped_before_next_launch(monkeypatch, tmp_path):
    import k5vision.operator_events as module

    monkeypatch.setattr(module, "_NATIVE_REQUEST_SECONDS", 0.2)
    worker = module._RecordedNativeWorker(None)
    processes = []

    def start():
        process = subprocess.Popen(
            [sys.executable, "-c", "import sys,time;sys.stdin.readline();time.sleep(30)"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        worker._process = process
        processes.append(process)

    monkeypatch.setattr(worker, "_start", start)
    with pytest.raises(OperatorEventsError, match="native frame unavailable"):
        worker.request(tmp_path / "unused-private-snapshot.mp4")
    assert worker._process is None
    assert processes[0].poll() is not None
    worker.close()


def test_closed_native_worker_permanently_refuses_another_birth(monkeypatch, tmp_path):
    import k5vision.operator_events as module

    worker = module._RecordedNativeWorker(None)
    worker.close()
    monkeypatch.setattr(worker, "_start", lambda: pytest.fail("late native child"))
    with pytest.raises(OperatorEventsError, match="worker is closed"):
        worker.request(tmp_path / "unused.mp4")


def test_shutdown_closes_admission_before_pending_capture_reaches_native_launch(
    identity, store, monkeypatch
):
    root = identity.directory / "recordings"
    root.mkdir(mode=0o700)
    sampler = RecordedFrameSampler(root)
    entered, release = threading.Event(), threading.Event()
    source = _source()

    def delayed_capture(_source):
        entered.set()
        assert release.wait(3)
        return sampler.decode(b"not-yet-decoded", datetime.now(UTC), "delayed-snapshot")

    monkeypatch.setattr(sampler, "capture", delayed_capture)
    service, _sampler, _provider = _service(store, source, sampler=sampler)

    async def run():
        cycle = None
        closing = None
        try:
            service.configure(source.binding.device_id, enabled=True, confidence_threshold=0.5)
            cycle = asyncio.create_task(service.cycle())
            while not entered.is_set():
                await asyncio.sleep(0.01)
            closing = asyncio.create_task(service.aclose())
            await asyncio.sleep(0)
            assert sampler._closing.is_set()
            release.set()
            await closing
            await cycle
            assert not hasattr(sampler, "_worker")
        finally:
            release.set()
            if closing is None:
                await service.aclose()

    asyncio.run(run())


def test_paused_recording_and_disabled_detector_do_not_start_native_work(store):
    source = _source(enabled=False)
    service, sampler, _provider = _service(store, source)

    async def run():
        try:
            await service.cycle()
            assert sampler.index == 0
            service.configure(source.binding.device_id, enabled=True, confidence_threshold=0.5)
            await service.cycle()
            assert sampler.index == 0
            assert service.status(source.binding.device_id)["status"] == "waiting"
        finally:
            await service.aclose()

    asyncio.run(run())


def test_decoder_failure_is_isolated_and_closes_cleanly(store):
    class FailedSampler:
        def capture(self, _source):
            raise RuntimeError("private-recording-path")

    source = _source()
    service, _sampler, _provider = _service(store, source, sampler=FailedSampler())

    async def run():
        try:
            service.configure(source.binding.device_id, enabled=True, confidence_threshold=0.5)
            await service.start()
            await asyncio.sleep(0.1)
            assert service.status(source.binding.device_id)["status"] == "unavailable"
            assert store.events(source.binding.device_id)["events"] == []
        finally:
            await service.aclose()

    asyncio.run(run())


def test_recorder_growth_after_utc_rollback_selects_new_file_not_future_filename(
    identity, monkeypatch
):
    root = identity.directory / "recordings"
    root.mkdir(mode=0o700)
    source = _source()
    directory = root / source.binding.path_name
    directory.mkdir(mode=0o700)
    old = directory / "2026-10-28_12-00-00-000000_+0000.mp4"
    old.write_bytes(b"old")
    sampler = RecordedFrameSampler(root)
    captured = []

    def decode(raw, when, key):
        captured.append((raw, when, key))
        return _sample(1, when)

    monkeypatch.setattr(sampler, "decode", decode)
    assert sampler.capture(source) is None  # Cold baseline cannot replay retained footage.
    new = directory / "2026-10-26_12-00-00-000000_+0000.mp4"
    new.write_bytes(b"new-recorded-frames")
    result = sampler.capture(source)
    assert result.occurred_at == datetime(2026, 10, 26, 12, tzinfo=UTC)
    assert captured[0][0] == b"new-recorded-frames"
    assert sampler.capture(source) is None


def test_sampler_rejects_linked_source_and_oversized_scans(identity, monkeypatch):
    import k5vision.operator_events as module

    root = identity.directory / "recordings"
    root.mkdir(mode=0o700)
    source = _source()
    directory = root / source.binding.path_name
    directory.mkdir(mode=0o700)
    sampler = RecordedFrameSampler(root)
    for second in range(2):
        (directory / f"2026-10-28_12-00-0{second}-000000_+0000.mp4").write_bytes(b"test")
    monkeypatch.setattr(module, "MAX_RECORDING_ENTRIES", 1)
    with pytest.raises(OperatorEventsError, match="scan limit"):
        sampler.capture(source)
    if os.name == "posix":
        for entry in directory.iterdir():
            entry.unlink()
        directory.rmdir()
        directory.symlink_to(identity.directory, target_is_directory=True)
        with pytest.raises(OperatorEventsError, match="source refused"):
            sampler.capture(source)
