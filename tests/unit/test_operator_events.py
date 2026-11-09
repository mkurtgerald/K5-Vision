from __future__ import annotations

import asyncio
import hashlib
import io
import os
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import Future
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
    camera = uuid4()
    store.configure(camera, enabled=True, confidence_threshold=0.7)
    assert _record(store, camera, 1)
    original = identity.directory / "operator-events.sqlite3"
    replaced = identity.directory / "old-events.sqlite3"
    before = original.stat()
    try:
        original.rename(replaced)
    except PermissionError as error:
        # Windows denies replacing an open SQLite file before K5's inode
        # revalidation is reached. Prove that actual OS boundary, retaining
        # the replacement-detection path on platforms which allow the rename.
        assert os.name == "nt" and error.winerror == 32
        assert original.exists() and not replaced.exists()
        after = original.stat()
        assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
        assert store.settings(camera) == {"enabled": True, "confidence_threshold": 0.7}
        assert len(store.events(camera)["events"]) == 1
        return
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
            assert service.status(source.binding.device_id)["sample_phase"] == "person_detected"
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
            assert service.status(source.binding.device_id)["sample_attempts"] == 3
        finally:
            await service.aclose()

    asyncio.run(run())
    assert provider.closed


@pytest.mark.parametrize(("cycle_seconds", "expected_sleep"), [(0.5, 1.5), (5.0, 0.0)])
def test_recorded_person_sampler_does_not_wait_again_after_slow_native_cycle(
    store, monkeypatch, cycle_seconds, expected_sleep
):
    # Synthetic elapsed time tests the production start-to-start cadence
    # without sleeping or depending on hosted runner CPU timing.
    import k5vision.operator_events as module

    clock = [100.0]
    service, _, _ = _service(store, _source(), clock=clock)
    sleeps = []

    async def simulated_cycle():
        clock[0] += cycle_seconds
        service._closing = True

    async def record_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(service, "cycle", simulated_cycle)
    monkeypatch.setattr(module.asyncio, "sleep", record_sleep)

    async def run():
        try:
            await service._run()
        finally:
            await service.aclose()

    asyncio.run(run())
    assert sleeps == [expected_sleep]


def test_pending_native_capture_reports_finite_stage_without_exposing_source(store):
    source = _source()
    service, sampler, _ = _service(store, source)
    camera_id = source.binding.device_id
    service.configure(camera_id, enabled=True, confidence_threshold=0.5)
    service._states[camera_id] = {"sample_phase": "capture_pending"}
    service._native_pending = Future()
    sampler._capture_outcome = "native_request"
    assert service.status(camera_id)["sample_phase"] == "native_request"
    assert service.status(camera_id)["last_completed_phase"] == "not_started"
    sampler._capture_outcome = "private-sensitive-media-path"
    assert service.status(camera_id)["sample_phase"] == "capture_pending"
    service._native_pending.set_result(None)
    assert service.status(camera_id)["sample_phase"] == "capture_pending"
    asyncio.run(service.aclose())


@pytest.mark.parametrize(
    ("worker_phase", "expected"),
    [
        ("native_launch_failed", "native_worker_starting"),
        ("native_write_failed", "native_request_writing"),
        ("native_child_exited_before_request", "native_worker_exited"),
        ("native_reply_missing", "native_reply_waiting"),
        ("native_header_invalid", "native_header_reading"),
        ("native_payload_incomplete", "native_payload_reading"),
        ("native_reply_invalid", "native_reply_validating"),
        ("native_decoder_active", "native_decoder_active"),
        ("native_model_active", "native_model_active"),
        ("native_inference_active", "native_inference_active"),
        ("private-rtsp-uri-or-other-untrusted-phase", "native_request"),
    ],
)
def test_pending_native_worker_distinguishes_bounded_inflight_stage(store, worker_phase, expected):
    source = _source()
    service, sampler, _ = _service(store, source)
    camera_id = source.binding.device_id
    service.configure(camera_id, enabled=True, confidence_threshold=0.5)
    service._states[camera_id] = {"sample_phase": "capture_pending"}
    service._native_pending = Future()
    sampler._capture_outcome = "native_request"
    sampler._worker = SimpleNamespace(_failure_phase=worker_phase)
    try:
        state = service.status(camera_id)
        assert state["sample_phase"] == expected
        assert state["last_completed_phase"] == "not_started"
        assert "private" not in state["sample_phase"]
    finally:
        service._native_pending.set_result(None)
        asyncio.run(service.aclose())


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


def test_recorded_person_native_cold_budget_becomes_steady_only_after_valid_ack(
    monkeypatch, tmp_path
):
    import io
    import json

    import k5vision.operator_events as module

    worker = module._RecordedNativeWorker(SimpleNamespace(artifact_root=tmp_path / "private-model"))
    assert worker._request_timeout() == 18.0
    assert module._NATIVE_REQUEST_SECONDS == 8.0
    assert module._NATIVE_COLD_PERSON_REQUEST_SECONDS < 20

    # A complete real-shaped native response, including zero detections, proves
    # that the owned child reached native model inference. No camera media is used.
    pixels = bytes(544 * 320 * 4)
    header = (
        json.dumps(
            {"status": "ready", "elapsed": 1.5, "bytes": len(pixels), "detections": []}
        ).encode("ascii")
        + b"\n"
    )

    class Process:
        stdin = io.BytesIO()
        stdout = io.BytesIO(header + pixels)

        def poll(self):
            return None

        def kill(self):
            pass

        def wait(self, timeout=2):
            return 0

    process = Process()
    monkeypatch.setattr(worker, "_start", lambda: setattr(worker, "_process", process))
    elapsed, output, detections = worker.request(tmp_path / "private-snapshot.mp4")
    assert (elapsed, len(output), detections) == (1.5, len(pixels), ())
    assert worker._request_timeout() == module._NATIVE_REQUEST_SECONDS
    worker.close()

    # Source-only decoding has no model-compilation exception.
    assert module._RecordedNativeWorker(None)._request_timeout() == 8.0


def test_owned_native_child_launch_uses_isolated_embedded_python(monkeypatch):
    import k5vision.operator_events as module
    import k5vision.operator_native_process as native_process

    captured = []

    class Child:
        stdin = io.BytesIO()
        stdout = io.BytesIO()

        def poll(self):
            return None

        def kill(self):
            pass

        def wait(self, timeout=2):
            return 0

    monkeypatch.setattr(
        native_process,
        "_WindowsJob",
        lambda: SimpleNamespace(assign=lambda child: None, close=lambda: None),
    )
    monkeypatch.setattr(
        module.subprocess,
        "Popen",
        lambda arguments, **options: captured.append(arguments) or Child(),
    )
    worker = module._RecordedNativeWorker(None)
    try:
        worker._start()
        assert captured == [[sys.executable, "-I", "-B", "-m", "k5vision.operator_events_decoder"]]
    finally:
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
            assert service.status(source.binding.device_id)["sample_phase"] == "recording_paused"
            assert service.status(source.binding.device_id)["sample_attempts"] == 0
        finally:
            await service.aclose()

    asyncio.run(run())


def test_empty_native_sample_reports_only_bounded_source_free_reason(store):
    class EmptySampler:
        # A malicious or broken donor must not turn a diagnostic into a
        # path, camera URI or arbitrary upstream payload echo.
        _capture_outcome = "untrusted-private-string"

        def capture(self, _source):
            return None

    source = _source()
    service, _sampler, _provider = _service(store, source, sampler=EmptySampler())

    async def run():
        try:
            service.configure(source.binding.device_id, enabled=True, confidence_threshold=0.5)
            await service.cycle()
            status = service.status(source.binding.device_id)
            assert status["status"] == "waiting"
            assert status["sample_phase"] == "no_frame"
            assert status["last_completed_phase"] == "no_frame"
            assert status["sample_attempts"] == 1
            assert status["processed_frames"] == 0
        finally:
            await service.aclose()

    asyncio.run(run())


def test_last_completed_sample_phase_survives_an_inflight_capture(store):
    entered = threading.Event()
    release = threading.Event()

    class TwoPhaseSampler:
        def __init__(self):
            self.calls = 0
            self._capture_outcome = "not_started"

        def capture(self, _source):
            self.calls += 1
            if self.calls == 1:
                self._capture_outcome = "no_growth"
                return None
            entered.set()
            if not release.wait(timeout=2):
                raise AssertionError("Timed out releasing test capture")
            self._capture_outcome = "no_decoded_frame"
            return None

    source = _source()
    service, sampler, _provider = _service(store, source, sampler=TwoPhaseSampler())

    async def run():
        try:
            service.configure(source.binding.device_id, enabled=True, confidence_threshold=0.5)
            await service.cycle()
            initial = service.status(source.binding.device_id)
            assert initial["sample_phase"] == "no_growth"
            assert initial["last_completed_phase"] == "no_growth"

            pending_cycle = asyncio.create_task(service.cycle())
            for _ in range(50):
                if entered.is_set():
                    break
                await asyncio.sleep(0.02)
            assert entered.is_set()
            pending = service.status(source.binding.device_id)
            assert pending["sample_phase"] == "capture_pending"
            assert pending["last_completed_phase"] == "no_growth"

            release.set()
            await pending_cycle
            completed = service.status(source.binding.device_id)
            assert completed["sample_phase"] == "no_decoded_frame"
            assert completed["last_completed_phase"] == "no_decoded_frame"
            assert completed["sample_attempts"] == 2
            assert sampler.calls == 2
        finally:
            release.set()
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
            status = service.status(source.binding.device_id)
            assert status["sample_phase"] == "sample_failed"
            assert status["last_completed_phase"] == "sample_failed"
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


def test_cached_inventory_detects_new_recorder_file_without_replaying_older_media(
    identity, monkeypatch
):
    root = identity.directory / "recordings"
    root.mkdir(mode=0o700)
    source = _source()
    directory = root / source.binding.path_name
    directory.mkdir(mode=0o700)
    retained = directory / "2026-10-28_12-00-00-000000_+0000.mp4"
    retained.write_bytes(b"retained-before-first-sample")

    # A valid cached storage scan may precede the sampler's first observation.
    # Its empty result must not authorize replay of already-retained files.
    sampler = RecordedFrameSampler(root, recording_files=lambda _binding: {})
    decoded = []

    def decode(raw, when, key):
        decoded.append((raw, when, key))
        return _sample(index=len(decoded), when=when)

    monkeypatch.setattr(sampler, "decode", decode)
    assert sampler.capture(source) is None
    assert sampler._capture_outcome == "baseline"
    assert sampler.capture(source) is None
    assert decoded == []

    # The active recorder rolls a new file before the 6-second maintenance
    # inventory refresh. Directory creation, not UTC filename ordering or an
    # old cached high-water, makes this source eligible for a real read.
    fresh = directory / "2026-10-27_12-00-00-000000_+0000.mp4"
    fresh.write_bytes(b"new-native-segment")
    sample = sampler.capture(source)
    assert sample is not None
    assert decoded == [(b"new-native-segment", datetime(2026, 10, 27, 12, tzinfo=UTC), fresh.name)]
    assert sampler.capture(source) is None
    assert len(decoded) == 1

    # An existing active segment continues from actual byte growth even when
    # the background storage inventory has not yet caught up.
    with fresh.open("ab") as output:
        output.write(b"-next-fragment")
    assert sampler.capture(source) is not None
    assert decoded[-1][0] == b"new-native-segment-next-fragment"
    assert len(decoded) == 2


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


def test_native_worker_reaps_pre_response_broken_pipe_then_retries_once(monkeypatch, tmp_path):
    import k5vision.operator_events as module

    class BrokenInput(io.BytesIO):
        def write(self, _payload):
            raise BrokenPipeError

    class Child:
        def __init__(self, broken):
            self.broken = broken
            self.stdin = BrokenInput() if broken else io.BytesIO()
            self.stdout = io.BytesIO(b'{"status":"empty"}\n')
            self.killed = False

        def poll(self):
            return 1 if self.broken or self.killed else None

        def kill(self):
            self.killed = True

        def wait(self, timeout=2):
            return 1 if self.broken or self.killed else 0

    first, replacement = Child(True), Child(False)
    births = iter((first, replacement))
    worker = module._RecordedNativeWorker(None)
    monkeypatch.setattr(worker, "_start", lambda: setattr(worker, "_process", next(births)))
    try:
        assert worker.request(tmp_path / "private-snapshot.mp4") is None
        assert first.stdin.closed and first.stdout.closed
        assert replacement.stdin.getvalue().endswith(b"\n")
        assert worker._process is replacement
    finally:
        worker.close()


@pytest.mark.parametrize(
    ("winerror", "child_exited", "retry"),
    [
        (109, True, True),
        (109, False, True),
        (232, True, True),
        (232, False, True),
        (233, True, True),
        (233, False, True),
        (5, False, False),
        (5, True, True),
    ],
)
def test_recorded_native_worker_retries_windows_disconnected_or_exited_children(
    monkeypatch, tmp_path, winerror, child_exited, retry
):
    import k5vision.operator_events as module

    class ClosedPipe(io.BytesIO):
        def write(self, _payload):
            error = OSError("private pipe failure")
            error.winerror = winerror
            raise error

    class Child:
        def __init__(self, broken):
            self.broken = broken
            self.stdin = ClosedPipe() if broken else io.BytesIO()
            self.stdout = io.BytesIO(b'{"status":"empty"}\n')
            self.killed = False

        def poll(self):
            # An unrelated access error must not be conflated with child exit.
            return 1 if (self.broken and child_exited) or self.killed else None

        def kill(self):
            self.killed = True

        def wait(self, timeout=2):
            return 1 if self.broken or self.killed else 0

    first, replacement = Child(True), Child(False)
    children = iter((first, replacement))
    worker = module._RecordedNativeWorker(None)
    starts = []

    def start():
        worker._process = next(children)
        starts.append(True)

    monkeypatch.setattr(worker, "_start", start)
    try:
        if retry:
            assert worker.request(tmp_path / "private-snapshot.mp4") is None
            assert len(starts) == 2
            assert replacement.stdin.getvalue().endswith(b"\n")
        else:
            with pytest.raises(OperatorEventsError, match="native frame unavailable"):
                worker.request(tmp_path / "private-snapshot.mp4")
            assert len(starts) == 1
            assert worker._failure_phase == "native_write_failed"
            assert worker._native_write_cause == "live_child_write_error"
        assert first.stdin.closed
    finally:
        worker.close()


@pytest.mark.parametrize("late_exit", [True, False])
def test_recorded_native_worker_only_recovers_ambiguous_write_after_confirmed_exit(
    monkeypatch, tmp_path, late_exit
):
    import k5vision.operator_events as module

    class AmbiguousPipe(io.BytesIO):
        def write(self, _payload):
            error = OSError("opaque native write failure")
            error.winerror = 5
            raise error

    class Child:
        def __init__(self, broken):
            self.broken = broken
            self.exited = False
            self.killed = False
            self.stdin = AmbiguousPipe() if broken else io.BytesIO()
            self.stdout = io.BytesIO(b'{"status":"empty"}\n')

        def poll(self):
            return 1 if self.exited or self.killed else None

        def kill(self):
            self.killed = True

        def wait(self, timeout=2):
            if self.broken and not self.exited and not self.killed:
                if timeout != 0.1 or not late_exit:
                    raise subprocess.TimeoutExpired("native-child", timeout)
                self.exited = True
            return 1 if self.exited or self.killed else 0

    first, replacement = Child(True), Child(False)
    births = iter((first, replacement))
    worker = module._RecordedNativeWorker(None)
    starts = []

    def start():
        worker._process = next(births)
        starts.append(True)

    monkeypatch.setattr(worker, "_start", start)
    try:
        if late_exit:
            assert worker.request(tmp_path / "private-snapshot.mp4") is None
            assert len(starts) == 2
            assert replacement.stdin.getvalue().endswith(b"\n")
            assert worker._native_write_cause == "write_accepted"
        else:
            with pytest.raises(OperatorEventsError, match="native frame unavailable"):
                worker.request(tmp_path / "private-snapshot.mp4")
            assert len(starts) == 1
            assert worker._failure_phase == "native_write_failed"
        assert first.stdin.closed
    finally:
        worker.close()


def test_native_worker_tracks_bounded_child_phases_without_source_content(monkeypatch, tmp_path):
    import k5vision.operator_events as module

    worker = module._RecordedNativeWorker(None)
    expected = [
        ("native_reply_missing", b'{"status":"phase","phase":"decode"}\n'),
        ("native_decoder_active", b'{"status":"phase","phase":"model"}\n'),
        ("native_model_active", b'{"status":"phase","phase":"inference"}\n'),
        ("native_inference_active", b'{"status":"empty"}\n'),
    ]
    observed = []

    class Output:
        def readline(self, _limit):
            phase, response = expected[len(observed)]
            observed.append(worker._failure_phase)
            assert worker._failure_phase == phase
            return response

        def close(self):
            pass

    class Child:
        stdin = io.BytesIO()
        stdout = Output()

        def poll(self):
            return None

        def kill(self):
            pass

        def wait(self, timeout=2):
            return 0

    monkeypatch.setattr(worker, "_start", lambda: setattr(worker, "_process", Child()))
    try:
        assert worker.request(tmp_path / "private-snapshot.mp4") is None
        assert observed == [item[0] for item in expected]
        assert len(observed) == 4
    finally:
        worker.close()


@pytest.mark.parametrize(
    "replies",
    [
        b'{"status":"phase","phase":"rtsp://private"}\n',
        b'{"status":"phase","phase":"decode"}\n' * 4,
    ],
)
def test_native_worker_rejects_unbounded_or_untrusted_progress(monkeypatch, tmp_path, replies):
    import k5vision.operator_events as module

    worker = module._RecordedNativeWorker(None)

    class Child:
        stdin = io.BytesIO()

        def __init__(self):
            self.stdout = io.BytesIO(replies)

        def poll(self):
            return None

        def kill(self):
            pass

        def wait(self, timeout=2):
            return 0

    monkeypatch.setattr(worker, "_start", lambda: setattr(worker, "_process", Child()))
    try:
        with pytest.raises(OperatorEventsError, match="native frame unavailable"):
            worker.request(tmp_path / "private-snapshot.mp4")
        assert worker._failure_phase == "native_header_invalid"
    finally:
        worker.close()


@pytest.mark.parametrize(
    "reply,write_fails,child_exited,expected",
    [
        (b"", True, True, "native_child_exited_before_request"),
        (b"", True, False, "native_write_failed"),
        (b"not-json\n", False, True, "native_header_invalid"),
        (b'{"status":"ready","bytes":1}\n', False, True, "native_header_invalid"),
        (
            b'{"status":"ready","bytes":696320,"elapsed":1.0,"detections":[]}\n',
            False,
            True,
            "native_payload_incomplete",
        ),
    ],
)
def test_native_worker_distinguishes_transport_failure_boundaries(
    monkeypatch, tmp_path, reply, write_fails, child_exited, expected
):
    import k5vision.operator_events as module

    class PrivateStdin(io.BytesIO):
        def write(self, payload):
            if write_fails:
                raise BrokenPipeError
            return super().write(payload)

    class Process:
        def __init__(self):
            self.stdin = PrivateStdin()
            self.stdout = io.BytesIO(reply)

        def poll(self):
            return 1 if child_exited else None

        def kill(self):
            pass

        def wait(self, timeout=2):
            return 1

    worker = module._RecordedNativeWorker(None)
    monkeypatch.setattr(worker, "_start", lambda: setattr(worker, "_process", Process()))
    with pytest.raises(OperatorEventsError, match="native frame unavailable"):
        worker.request(tmp_path / "private-snapshot.mp4")
    assert worker._failure_phase == expected
    assert worker._process is None
    worker.close()


@pytest.mark.parametrize(
    "child_phase,expected",
    [
        ("model", "native_model_failed"),
        ("decode", "native_decode_failed"),
        ("inference", "native_inference_failed"),
        ("rtsp://private-source", "native_transport_failed"),
    ],
)
def test_native_worker_failure_phase_is_finite_and_source_free(
    monkeypatch, tmp_path, child_phase, expected
):
    import json

    import k5vision.operator_events as module

    worker = module._RecordedNativeWorker(None)

    class Process:
        stdin = io.BytesIO()
        stdout = io.BytesIO(
            (json.dumps({"status": "failed", "phase": child_phase}) + "\n").encode()
        )

        def poll(self):
            return 1

        def kill(self):
            pass

        def wait(self, timeout=2):
            return 1

    monkeypatch.setattr(worker, "_start", lambda: setattr(worker, "_process", Process()))
    with pytest.raises(OperatorEventsError, match="native frame unavailable"):
        worker.request(tmp_path / "private-snapshot.mp4")
    assert worker._failure_phase == expected
    assert worker._process is None
    worker.close()


def test_model_priming_is_owned_and_nonblocking_when_recorded_person_is_enabled(
    identity, store, monkeypatch
):
    root = identity.directory / "recordings"
    root.mkdir(mode=0o700)
    sampler = RecordedFrameSampler(root)
    source = _source()
    entered, release = threading.Event(), threading.Event()

    def prime_without_media():
        entered.set()
        assert release.wait(3)

    monkeypatch.setattr(sampler, "prime", prime_without_media)
    service = OperatorEventService(
        store=store,
        cameras=SimpleNamespace(list_sources=lambda: (source,)),
        recording_root=root,
        configuration=SimpleNamespace(artifact_root=identity.directory / "model"),
        sampler=sampler,
    )
    try:
        service.configure(source.binding.device_id, enabled=True, confidence_threshold=0.5)
        assert entered.wait(2)
        assert service._native_pending is not None
        assert not service._native_pending.done()
        assert sampler._observations == {}
    finally:
        release.set()
        asyncio.run(service.aclose())


@pytest.mark.parametrize("prime_fails", [False, True])
def test_startup_prime_is_reused_only_when_native_worker_is_ready(
    identity, store, monkeypatch, prime_fails
):
    # Healthy native worker readiness prevents duplicate compilation; a failed
    # or exited preparation must be retried before enabling recorded detection.
    root = identity.directory / "recordings"
    root.mkdir(mode=0o700)
    sampler = RecordedFrameSampler(root)
    source = _source()
    requests = []

    def prime_once():
        requests.append("prime")
        if prime_fails:
            raise OperatorEventsError("synthetic model preparation unavailable")

    monkeypatch.setattr(sampler, "prime", prime_once)
    monkeypatch.setattr(sampler, "native_model_ready", lambda: not prime_fails)
    service = OperatorEventService(
        store=store,
        cameras=SimpleNamespace(list_sources=lambda: (source,)),
        recording_root=root,
        configuration=SimpleNamespace(artifact_root=identity.directory / "model"),
        sampler=sampler,
    )

    async def run():
        try:
            await service.start()
            first = service._native_pending
            assert first is not None
            if prime_fails:
                with pytest.raises(
                    OperatorEventsError, match="synthetic model preparation unavailable"
                ):
                    await asyncio.wrap_future(first)
            else:
                await asyncio.wrap_future(first)
            service.configure(source.binding.device_id, enabled=True, confidence_threshold=0.5)
            if prime_fails:
                retried = service._native_pending
                assert retried is not first
                with pytest.raises(
                    OperatorEventsError, match="synthetic model preparation unavailable"
                ):
                    await asyncio.wrap_future(retried)
                assert requests == ["prime", "prime"]
            else:
                assert service._native_pending is first
                assert requests == ["prime"]
        finally:
            await service.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(("child_status", "expected_requests"), [(None, 0), (1, 1)])
def test_model_prime_rechecks_child_liveness_before_trusting_cached_readiness(
    identity, monkeypatch, child_status, expected_requests
):
    import k5vision.operator_events as module

    root = identity.directory / "recordings"
    root.mkdir(mode=0o700)
    sampler = RecordedFrameSampler(root)
    sampler._configuration = SimpleNamespace(artifact_root=identity.directory / "model")
    worker = module._RecordedNativeWorker(sampler._configuration)
    worker._person_ready = True

    class Child:
        def poll(self):
            return child_status

    worker._process = Child()
    requests = []
    monkeypatch.setattr(worker, "request", lambda path: requests.append(path))
    sampler._worker = worker
    sampler.prime()
    assert requests == [None] * expected_requests


def test_recorded_native_model_prime_requires_matching_reply_and_reuses_child(
    monkeypatch, tmp_path
):
    import json

    import k5vision.operator_events as module

    worker = module._RecordedNativeWorker(
        SimpleNamespace(artifact_root=tmp_path / "sealed-person-model")
    )
    assert worker._request_timeout() == 18.0

    class Process:
        stdin = io.BytesIO()
        stdout = io.BytesIO(b'{"status":"primed"}\n')

        def poll(self):
            return None

        def kill(self):
            pass

        def wait(self, timeout=2):
            return 0

    process = Process()
    monkeypatch.setattr(worker, "_start", lambda: setattr(worker, "_process", process))
    assert worker.request(None) is None
    assert worker._person_ready
    assert worker._request_timeout() == module._NATIVE_REQUEST_SECONDS
    assert json.loads(process.stdin.getvalue()) == {
        "path": "",
        "artifact_root": str(tmp_path / "sealed-person-model"),
    }
    worker.close()


def test_first_recorded_person_event_has_priority_over_speculative_view_warmup(store):
    camera = uuid4()
    source = SimpleNamespace(binding=SimpleNamespace(device_id=camera))
    cameras = SimpleNamespace(list_sources=lambda: (source,))
    store.configure(camera, enabled=True, confidence_threshold=0.5)
    assert _record(store, camera, 1)
    service = OperatorEventService(
        store=store,
        cameras=cameras,
        recording_root=Path("."),
        configuration=None,
        sampler=SimpleNamespace(),
        provider_factory=object(),
    )
    try:
        # Historical rows cannot prove a newly started native worker has
        # detected a real event in this service lifetime.
        assert service.awaiting_first_recorded_person_event()
        service._last_events[camera] = 1.0
        assert not service.awaiting_first_recorded_person_event()
        service._last_events.clear()
        store.configure(camera, enabled=False, confidence_threshold=0.5)
        assert not service.awaiting_first_recorded_person_event()
    finally:
        asyncio.run(service.aclose())


def test_first_recorded_person_event_releases_warmup_with_other_cameras_empty(store):
    first, second, unrelated = uuid4(), uuid4(), uuid4()
    sources = tuple(
        SimpleNamespace(binding=SimpleNamespace(device_id=camera)) for camera in (first, second)
    )
    cameras = SimpleNamespace(list_sources=lambda: sources)
    for camera in (first, second):
        store.configure(camera, enabled=True, confidence_threshold=0.5)
    service = OperatorEventService(
        store=store,
        cameras=cameras,
        recording_root=Path("."),
        configuration=None,
        sampler=SimpleNamespace(),
        provider_factory=object(),
    )
    try:
        assert service.awaiting_first_recorded_person_event()
        service._last_events[unrelated] = 1.0
        assert service.awaiting_first_recorded_person_event()
        service._last_events[first] = 2.0
        assert not service.awaiting_first_recorded_person_event()
        service._last_events.clear()
        store.configure(first, enabled=False, confidence_threshold=0.5)
        assert service.awaiting_first_recorded_person_event()
        store.configure(second, enabled=False, confidence_threshold=0.5)
        assert not service.awaiting_first_recorded_person_event()
    finally:
        asyncio.run(service.aclose())


def test_first_verified_recorded_person_event_starts_optional_preparation_once(store):
    source = _source()

    class InitiallyEmptyProvider:
        def __init__(self):
            self.calls = 0

        async def __call__(self, _frame):
            self.calls += 1
            if self.calls == 1:
                return ()
            return (SimpleNamespace(category="person", confidence=0.9),)

        async def aclose(self):
            pass

    service, _, provider = _service(store, source, provider=InitiallyEmptyProvider())
    calls = []
    service.notify_on_first_recorded_person_event(lambda: calls.append("first_event"))

    async def run():
        try:
            service.configure(source.binding.device_id, enabled=True, confidence_threshold=0.5)
            await service.cycle()
            assert provider.calls == 1
            assert calls == []
            assert service.awaiting_first_recorded_person_event()
            await service.cycle()
            assert calls == ["first_event"]
            assert not service.awaiting_first_recorded_person_event()
            await service.cycle()
            assert calls == ["first_event"]
        finally:
            await service.aclose()

    asyncio.run(run())


def test_verified_live_recorder_prime_releases_optional_view_warmup_without_event(store):
    """A sealed primed worker may warm the view; it never admits a person event."""
    camera = uuid4()
    source = SimpleNamespace(binding=SimpleNamespace(device_id=camera))
    cameras = SimpleNamespace(list_sources=lambda: (source,))
    store.configure(camera, enabled=True, confidence_threshold=0.5)

    class NativeProcess:
        exit_code = None

        def poll(self):
            return self.exit_code

    process = NativeProcess()
    worker = SimpleNamespace(
        _person_ready=False,
        _process=process,
        begin_close=lambda: None,
        close=lambda: None,
    )
    sampler = object.__new__(RecordedFrameSampler)
    sampler._closing = threading.Event()
    sampler._worker_lock = threading.RLock()
    sampler._worker = worker
    service = OperatorEventService(
        store=store,
        cameras=cameras,
        recording_root=Path("."),
        configuration=None,
        sampler=sampler,
        provider_factory=object(),
    )
    try:
        assert service.awaiting_first_recorded_person_event()
        assert not sampler.native_model_ready()
        worker._person_ready = True
        assert sampler.native_model_ready()
        assert not service.awaiting_first_recorded_person_event()
        assert service._last_events == {}
        process.exit_code = 1
        assert not sampler.native_model_ready()
        assert service.awaiting_first_recorded_person_event()
        worker._process = None
        assert not sampler.native_model_ready()
    finally:
        asyncio.run(service.aclose())
    assert not sampler.native_model_ready()


def test_cold_recorder_prime_defers_view_without_enabled_analytics(store):
    """The camera-free recorder model gets CPU priority before UI configuration."""
    camera = uuid4()
    source = SimpleNamespace(binding=SimpleNamespace(device_id=camera))
    cameras = SimpleNamespace(list_sources=lambda: (source,))

    class NativeProcess:
        def poll(self):
            return None

    worker = SimpleNamespace(
        _person_ready=False,
        _process=NativeProcess(),
        begin_close=lambda: None,
        close=lambda: None,
    )
    sampler = object.__new__(RecordedFrameSampler)
    sampler._closing = threading.Event()
    sampler._worker_lock = threading.RLock()
    sampler._worker = worker
    service = OperatorEventService(
        store=store,
        cameras=cameras,
        recording_root=Path("."),
        configuration=object(),
        sampler=sampler,
    )
    try:
        assert not store.settings(camera)["enabled"]
        assert service.awaiting_first_recorded_person_event()
        worker._person_ready = True
        assert not service.awaiting_first_recorded_person_event()
        worker._person_ready = False
        store.configure(camera, enabled=True, confidence_threshold=0.5)
        assert service.awaiting_first_recorded_person_event()
        service._last_events[camera] = 1.0
        assert not service.awaiting_first_recorded_person_event()
    finally:
        asyncio.run(service.aclose())


@pytest.mark.parametrize(
    ("failed_stage", "expected"),
    [
        ("queued", "sample_queue_failed"),
        ("source_check", "sample_source_failed"),
        ("snapshot_read", "snapshot_read_failed"),
        ("decode_attempt", "snapshot_decode_failed"),
        ("frame_ready", "event_admission_failed"),
        ("native_request", "native_write_failed"),
        ("untrusted-private-value", "sample_failed"),
    ],
)
def test_current_recorded_sample_stage_owns_failure_attribution(
    identity, store, monkeypatch, failed_stage, expected
):
    """A stale native prime failure must not conceal the actual failing stage."""
    from k5vision.operator_events import _RecordedNativeWorker

    root = identity.directory / "recordings"
    root.mkdir(mode=0o700)
    sampler = RecordedFrameSampler(root)
    source = _source()
    service, _, _ = _service(store, source, sampler=sampler)
    worker = _RecordedNativeWorker(None)
    worker._failure_phase = "native_write_failed"
    worker._native_write_cause = "live_child_write_error"
    sampler._worker = worker

    async def fail_at_stage(_source, _settings):
        sampler._capture_outcome = failed_stage
        raise RuntimeError("private request error must not reach operator status")

    monkeypatch.setattr(service, "_sample", fail_at_stage)

    async def run():
        try:
            service.configure(source.binding.device_id, enabled=True, confidence_threshold=0.5)
            await service.cycle()
            status = service.status(source.binding.device_id)
            assert status["sample_phase"] == expected
            assert status["last_completed_phase"] == expected
            assert status["native_write_cause"] == (
                "live_child_write_error" if failed_stage == "native_request" else "not_applicable"
            )
            assert status["status"] == "unavailable"
            assert "private" not in str(status)

            # A later prime can begin before the status poll. Its changing
            # worker state must not erase the last completed failure's cause.
            worker._native_write_cause = "pending"
            service._states[source.binding.device_id]["sample_phase"] = "native_pending"
            pending_status = service.status(source.binding.device_id)
            assert pending_status["sample_phase"] == "native_pending"
            assert pending_status["last_completed_phase"] == expected
            assert pending_status["native_write_cause"] == (
                "live_child_write_error" if failed_stage == "native_request" else "not_applicable"
            )
            assert "private" not in str(pending_status)
        finally:
            await service.aclose()

    asyncio.run(run())
