from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import k5vision.operator_motion as motion
from k5vision import identity_state
from k5vision.domain.devices import Device
from k5vision.media.mediamtx_config import MediaMTXSource
from k5vision.media.mediamtx_provider import MediaMTXBinding
from k5vision.operator_motion import MotionUnavailable, SceneMotionDetector, allowed_mask
from k5vision.operator_motion_process import MotionSample
from k5vision.operator_recording_policy import (
    MotionPolicy,
    RecordingPolicyConflict,
    RecordingPolicyStore,
)

_SPEC = importlib.util.spec_from_file_location(
    "motion_service_harness", Path(__file__).with_name("test_operator_product_service.py")
)
_HARNESS = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_HARNESS)
native = _HARNESS.native


def _wire(**changes):
    return {
        "revision": 1,
        "source_revision": "1" * 64,
        "sensitivity": 50,
        "minimum_area_percent": 0.5,
        "include": [],
        "exclude": [],
        **changes,
    }


@pytest.fixture
def pixels():
    pytest.importorskip("cv2")
    return pytest.importorskip("numpy")


def test_real_pixels_need_baseline_and_trigger_included_movement(pixels):
    detector = SceneMotionDetector()
    still = pixels.zeros((240, 320, 3), dtype=pixels.uint8)
    moved = still.copy()
    moved[60:160, 80:180] = 220
    assert detector.observe(still, _wire()) is False
    assert detector.observe(still, _wire()) is False
    assert detector.observe(moved, _wire()) is True


@pytest.mark.parametrize("dimensions", [(320, 240), (641, 359), (257, 513)])
def test_excluded_only_pixels_cannot_bleed_through_resize_or_blur(pixels, dimensions):
    width, height = dimensions
    detector = SceneMotionDetector()
    policy = _wire(exclude=[[[0.5, 0], [1, 0], [1, 1], [0.5, 1]]])
    still = pixels.full((height, width, 3), 80, dtype=pixels.uint8)
    assert detector.observe(still, policy) is False
    for value in (0, 255, 15, 210):
        changed = still.copy()
        changed[:, round((width - 1) * 0.5) :] = value
        assert detector.observe(changed, policy) is False


def test_inclusions_union_and_exclusions_win_on_real_pixels(pixels):
    detector = SceneMotionDetector()
    policy = _wire(
        include=[[[0, 0], [0.4, 0], [0.4, 1], [0, 1]], [[0.6, 0], [1, 0], [1, 1], [0.6, 1]]],
        exclude=[[[0.6, 0], [1, 0], [1, 1], [0.6, 1]]],
    )
    still = pixels.zeros((240, 320, 3), dtype=pixels.uint8)
    assert detector.observe(still, policy) is False
    outside = still.copy()
    outside[30:180, 150:310] = 255
    assert detector.observe(outside, policy) is False
    inside = still.copy()
    inside[40:150, 20:100] = 255
    assert detector.observe(inside, policy) is True


def test_geometry_source_and_policy_generations_rebuild_the_baseline(pixels):
    detector = SceneMotionDetector()
    first = pixels.zeros((240, 320, 3), dtype=pixels.uint8)
    changed = pixels.full_like(first, 255)
    assert detector.observe(first, _wire()) is False
    assert detector.observe(changed, _wire(revision=2)) is False
    assert detector.observe(first, _wire(revision=2, source_revision="2" * 64)) is False
    portrait = pixels.full((320, 240, 3), 255, dtype=pixels.uint8)
    assert detector.observe(portrait, _wire(revision=2, source_revision="2" * 64)) is False


def test_higher_sensitivity_detects_smaller_brightness_changes(pixels):
    still = pixels.zeros((240, 320, 3), dtype=pixels.uint8)
    moved = still.copy()
    moved[40:180, 40:180] = 15
    low, high = SceneMotionDetector(), SceneMotionDetector()
    assert low.observe(still, _wire(sensitivity=1)) is False
    assert high.observe(still, _wire(sensitivity=100)) is False
    assert low.observe(moved, _wire(sensitivity=1)) is False
    assert high.observe(moved, _wire(sensitivity=100)) is True


def test_contour_bounds_cannot_count_a_large_excluded_hole(pixels):
    detector = SceneMotionDetector()
    policy = _wire(
        minimum_area_percent=25,
        exclude=[[[0.2, 0], [0.8, 0], [0.8, 1], [0.2, 1]]],
    )
    still = pixels.zeros((240, 320, 3), dtype=pixels.uint8)
    assert detector.observe(still, policy) is False
    moved = pixels.full_like(still, 255)
    assert detector.observe(moved, policy) is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("sensitivity", True),
        ("sensitivity", 0),
        ("minimum_area_percent", 0.01),
        ("minimum_area_percent", float("nan")),
    ],
)
def test_detector_refuses_unsafe_numeric_controls(pixels, field, value):
    with pytest.raises(MotionUnavailable):
        SceneMotionDetector().observe(
            pixels.zeros((100, 100, 3), dtype=pixels.uint8), _wire(**{field: value})
        )


def test_effective_mask_rejects_full_exclusion(pixels):
    whole = [[0, 0], [1, 0], [1, 1], [0, 1]]
    assert not allowed_mask(320, 240, [], [whole]).any()
    with pytest.raises(MotionUnavailable, match="entire camera frame"):
        SceneMotionDetector().observe(
            pixels.zeros((240, 320, 3), dtype=pixels.uint8), _wire(exclude=[whole])
        )


class _Policies:
    def __init__(self):
        self.values = {}
        self.writing = threading.Event()
        self.release_write = None

    @staticmethod
    def source_revision(source):
        value = source.source_uri + ":" + getattr(source, "profile_key", "")
        return hashlib.sha256(value.encode()).hexdigest()

    def get(self, source):
        current = self.source_revision(source)
        stored = self.values.get(source.binding.device_id)
        return {
            "camera_id": str(source.binding.device_id),
            "revision": 0 if stored is None else stored["revision"],
            "source_revision": current,
            "policy_source_revision": current if stored is None else stored["source_revision"],
            "pre_capture_seconds": 0,
            **copy.deepcopy(
                MotionPolicy().model_dump(mode="json") if stored is None else stored["policy"]
            ),
        }

    def save(self, source, expected_revision, policy, *, before_write=None):
        previous = self.get(source)
        if previous["revision"] != expected_revision:
            raise RecordingPolicyConflict("motion_policy_conflict")
        self.writing.set()
        if self.release_write is not None:
            assert self.release_write.wait(2)
        if before_write is not None:
            before_write()
        self.values[source.binding.device_id] = {
            "revision": expected_revision + 1,
            "source_revision": self.source_revision(source),
            "policy": copy.deepcopy(policy),
        }
        return self.get(source)

    def prune_existing(self, camera_ids):
        removed = set(self.values) - set(camera_ids)
        for identifier in removed:
            del self.values[identifier]
        return len(removed)

    def remove(self, camera_id):
        return self.values.pop(camera_id, None) is not None


class _Worker:
    def __init__(self):
        self.moving = False
        self.width, self.height = 320, 240
        self.age = 0.0
        self.closed = False
        self.reaped = False
        self.started = threading.Event()
        self.release = None
        self.sequence = 0

    def sample(self, _policy):
        self.started.set()
        if self.release is not None:
            assert self.release.wait(2)
        self.sequence += 1
        return MotionSample(
            self.width,
            self.height,
            self.width,
            self.height,
            self.sequence,
            self.moving,
            self.age,
            b"generated-jpeg",
            motion.time.monotonic() - self.age,
        )

    def begin_close(self):
        self.closed = True

    def close(self):
        self.begin_close()
        self.reaped = True


class _MaskValidator:
    def __init__(self):
        self.started = threading.Event()
        self.release = None
        self.allowed = True
        self.closed = False
        self.reaped = False

    def validate(self, _policy):
        self.started.set()
        if self.release is not None:
            assert self.release.wait(2), "Mask validation control was not released"
        return self.allowed

    def begin_close(self):
        self.closed = True
        if self.release is not None:
            self.release.set()

    def close(self):
        self.begin_close()
        self.reaped = True


@pytest.fixture
def engine(native, monkeypatch):
    service = native.service
    policies = _Policies()
    service.set_recording_policy_store(policies)
    monkeypatch.setattr(service._motion, "start", lambda: None)
    workers = []

    def worker(_service, _source):
        value = _Worker()
        workers.append(value)
        return value

    monkeypatch.setattr(service._motion, "_new_worker", worker)
    validators = []

    def mask_validator():
        value = _MaskValidator()
        validators.append(value)
        return value

    monkeypatch.setattr(service._motion, "_new_mask_validator", mask_validator)
    attempts = _HARNESS._fake_mutations(native, monkeypatch)
    return SimpleNamespace(
        service=service,
        policies=policies,
        workers=workers,
        validators=validators,
        attempts=attempts,
        sources=(_HARNESS._source(1), _HARNESS._source(2)),
    )


def _body(**changes):
    return {
        **MotionPolicy(mode="motion", post_capture_seconds=2).model_dump(mode="json"),
        **changes,
    }


async def _sample(engine):
    runtime = engine.service._motion
    source = engine.sources[0]
    policy = runtime.policies[source.binding.path_name]
    await runtime._sample(source, copy.deepcopy(policy), runtime._generation(source, policy))


async def _durable_runtime(tmp_path):
    identity = identity_state.initialize_identity_state(
        tmp_path / "identity", username="motion-admin", password="motion-test-private-password"
    )
    camera = Device(name="Generated motion camera", host="127.0.0.1")
    source = MediaMTXSource(
        MediaMTXBinding(identity.site_id, camera.id), camera, "rtsp://127.0.0.1/live"
    )
    store = RecordingPolicyStore(identity.directory, identity.site_id)
    store.save(source, 0, MotionPolicy(mode="motion").model_dump(mode="json"))
    service = SimpleNamespace(
        _media=None,
        _lock=asyncio.Lock(),
        _closing=False,
        _ready=True,
        _needs_reconcile=False,
        _disk_full=False,
        _sources=(source,),
        media_generation=1,
        state_root=identity.directory,
    )
    runtime = motion.MotionRuntime(service)
    runtime.install(store)
    await runtime.load((source,))
    worker = _Worker()
    path = source.binding.path_name
    runtime.workers[path], runtime.gates[path] = worker, False
    return runtime, source, store


def _pause_private_admission(store, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    private = store._private
    paused = False

    def blocked(path):
        nonlocal paused
        if not paused:
            paused = True
            entered.set()
            assert release.wait(3), "Controlled private admission was not released"
        return private(path)

    monkeypatch.setattr(store, "_private", blocked)
    return entered, release


def test_durable_policy_read_does_not_block_fresh_camera_sample(tmp_path, monkeypatch):
    async def scenario():
        runtime, source, store = await _durable_runtime(tmp_path)
        path = source.binding.path_name
        generation = runtime._generation(source, runtime.policies[path])
        entered, release = _pause_private_admission(store, monkeypatch)
        getter = sample = None
        try:
            getter = asyncio.create_task(runtime.get_policy(source))
            assert await asyncio.to_thread(entered.wait, 1)
            sample = asyncio.create_task(
                runtime._sample(source, runtime.policies[path], generation)
            )
            done, _ = await asyncio.wait({sample}, timeout=0.5)
            assert sample in done, "Private policy admission blocked camera sampling"
            assert not getter.done()
            assert runtime.status(source.binding)["state"] == "armed"
            release.set()
            assert (await getter)["status"] == "armed"
        finally:
            release.set()
            await asyncio.gather(
                *(task for task in (getter, sample) if task), return_exceptions=True
            )
            await runtime.close()
            store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["source", "service_source", "policy", "media", "closing"])
def test_durable_policy_read_rechecks_generation_after_private_admission(
    tmp_path, monkeypatch, change
):
    async def scenario():
        runtime, source, store = await _durable_runtime(tmp_path)
        path = source.binding.path_name
        entered, release = _pause_private_admission(store, monkeypatch)
        getter = None
        try:
            getter = asyncio.create_task(runtime.get_policy(source))
            assert await asyncio.to_thread(entered.wait, 1)
            async with runtime.service._lock:
                if change == "source":
                    runtime.sources[path] = replace(source, profile_key="a" * 64)
                    runtime.service._sources = (runtime.sources[path],)
                elif change == "service_source":
                    runtime.service._sources = (replace(source, profile_key="a" * 64),)
                elif change == "policy":
                    runtime.policies[path] = {**runtime.policies[path], "revision": 2}
                elif change == "media":
                    runtime.service.media_generation += 1
                else:
                    runtime.closing = True
                selected = copy.deepcopy(runtime.policies[path])
            release.set()
            with pytest.raises(MotionUnavailable, match="selection changed"):
                await getter
            assert runtime.policies[path] == selected
        finally:
            release.set()
            if getter is not None:
                await asyncio.gather(getter, return_exceptions=True)
            await runtime.close()
            store.close()

    asyncio.run(scenario())


def test_durable_policy_read_returns_unavailable_status_when_backend_is_down(tmp_path):
    async def scenario():
        runtime, source, store = await _durable_runtime(tmp_path)
        try:
            runtime.service._ready = False
            result = await runtime.get_policy(source)
            assert result["status"] == "unavailable" and result["revision"] == 1
        finally:
            await runtime.close()
            store.close()

    asyncio.run(scenario())


def test_real_service_motion_arms_triggers_holds_and_closes_only_one_path(engine, monkeypatch):
    async def scenario():
        service, source = engine.service, engine.sources[0]
        clock = SimpleNamespace(value=100.0)
        monkeypatch.setattr(motion, "time", SimpleNamespace(monotonic=lambda: clock.value))
        await service.start(engine.sources)
        try:
            result = await service.apply_motion_policy(source, 0, _body())
            assert result["status"] == "unavailable" and result["revision"] == 1
            assert "observed_reference" in result
            assert service._media.paths[source.binding.path_name]["record"] is False
            await _sample(engine)
            assert service.recording_policy_status(source.binding)["state"] == "armed"
            engine.workers[-1].moving = True
            await _sample(engine)
            assert service._media.paths[source.binding.path_name]["record"] is True
            assert service.recording_policy_status(source.binding)["state"] == "recording"
            engine.workers[-1].moving = False
            await _sample(engine)
            assert service.recording_policy_status(source.binding)["state"] == "post_capture"
            clock.value += 1
            await _sample(engine)
            assert service._motion.effective_record(source) is True
            clock.value += 1.1
            await _sample(engine)
            assert service._media.paths[source.binding.path_name]["record"] is False
            assert service.recording_policy_status(source.binding)["state"] == "armed"
            assert service._media.paths[engine.sources[1].binding.path_name]["record"] is True
        finally:
            await service.stop()
        assert all(worker.reaped for worker in engine.workers)

    asyncio.run(scenario())


def test_storage_recovery_never_starts_an_idle_motion_path(engine):
    async def scenario():
        service, source = engine.service, engine.sources[0]
        await service.start(engine.sources)
        try:
            await service.apply_motion_policy(source, 0, _body())
            await _sample(engine)
            blocked = SimpleNamespace(recording_allowed=False)
            recovered = SimpleNamespace(recording_allowed=True)
            async with service._lock:
                await service._apply_storage_status(blocked)
                assert not any(path["record"] for path in service._media.paths.values())
                await service._apply_storage_status(recovered)
            assert service._media.paths[source.binding.path_name]["record"] is False
            assert service._media.paths[engine.sources[1].binding.path_name]["record"] is True
            assert service._motion.effective_record(source) is False
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_master_pause_and_resume_require_a_new_motion_baseline(engine):
    async def scenario():
        service, source = engine.service, engine.sources[0]
        await service.start(engine.sources)
        try:
            await service.apply_motion_policy(source, 0, _body())
            await _sample(engine)
            engine.workers[-1].moving = True
            await _sample(engine)
            assert service._motion.effective_record(source)
            paused = replace(source, recording_enabled=False)
            await service.apply_recording_preferences((paused, engine.sources[1]))
            assert service.recording_policy_status(source.binding)["state"] == "paused"
            await service.apply_recording_preferences(engine.sources)
            assert service._media.paths[source.binding.path_name]["record"] is False
            assert service._motion.effective_record(source) is False
            assert not service._motion.holds
        finally:
            await service.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["uri", "profile"])
def test_new_source_generation_never_reuses_saved_motion_authority(engine, change):
    async def scenario():
        service, source = engine.service, engine.sources[0]
        await service.start(engine.sources)
        try:
            await service.apply_motion_policy(source, 0, _body())
            await _sample(engine)
            replacement = replace(
                source,
                **(
                    {"source_uri": source.source_uri + "/new"}
                    if change == "uri"
                    else {"profile_key": "f" * 64}
                ),
            )
            await service.apply_sources((replacement, engine.sources[1]))
            assert service.recording_policy_status(source.binding)["state"] == "zones_need_review"
            assert service._media.paths[source.binding.path_name]["record"] is False
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_changed_native_geometry_keeps_recording_closed_until_redraw(engine):
    async def scenario():
        service, source = engine.service, engine.sources[0]
        await service.start(engine.sources)
        try:
            reference = {
                "width": 320,
                "height": 240,
                "rotation": 0,
                "source_revision": engine.policies.source_revision(source),
            }
            await service.apply_motion_policy(
                source, 0, _body(zones={"reference": reference, "include": [], "exclude": []})
            )
            await _sample(engine)
            worker = engine.workers[-1]
            worker.width, worker.height, worker.moving = 640, 360, True
            await _sample(engine)
            assert service.recording_policy_status(source.binding)["state"] == "zones_need_review"
            assert service._media.paths[source.binding.path_name]["record"] is False
            assert service._motion.observed[source.binding.path_name]["width"] == 640
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_policy_change_discards_a_late_positive_worker_result(engine):
    async def scenario():
        service, source = engine.service, engine.sources[0]
        await service.start(engine.sources)
        try:
            await service.apply_motion_policy(source, 0, _body())
            await _sample(engine)
            worker = engine.workers[-1]
            worker.moving, worker.release = True, threading.Event()
            worker.started.clear()
            pending = asyncio.create_task(_sample(engine))
            await asyncio.to_thread(worker.started.wait, 1)
            await service.apply_motion_policy(source, 1, _body(sensitivity=80))
            worker.release.set()
            await pending
            assert service._media.paths[source.binding.path_name]["record"] is False
            assert not service._motion.holds
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_frame_that_ages_while_waiting_for_service_lock_cannot_trigger(engine, monkeypatch):
    async def scenario():
        service, source = engine.service, engine.sources[0]
        clock = SimpleNamespace(value=100.0)
        monkeypatch.setattr(motion, "time", SimpleNamespace(monotonic=lambda: clock.value))
        await service.start(engine.sources)
        try:
            await service.apply_motion_policy(source, 0, _body())
            await _sample(engine)
            worker = engine.workers[-1]
            worker.moving = True
            worker.started.clear()
            async with service._lock:
                pending = asyncio.create_task(_sample(engine))
                await asyncio.to_thread(worker.started.wait, 1)
                await asyncio.sleep(0.03)
                clock.value += 3
            await pending
            assert service._media.paths[source.binding.path_name]["record"] is False
            assert service.recording_policy_status(source.binding)["state"] == "unavailable"
        finally:
            await service.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("handoff_seconds", [0.25, 2.25])
def test_executor_handoff_preserves_capture_freshness_and_hold_deadline(
    engine, monkeypatch, handoff_seconds
):
    async def scenario():
        service, source = engine.service, engine.sources[0]
        runtime, path = service._motion, source.binding.path_name
        clock = SimpleNamespace(value=100.0)
        monkeypatch.setattr(motion, "time", SimpleNamespace(monotonic=lambda: clock.value))
        await service.start(engine.sources)
        try:
            await service.apply_motion_policy(source, 0, _body())
            await _sample(engine)
            worker = engine.workers[-1]
            worker.moving = True
            run = runtime._run

            async def delayed_handoff(operation, *args, **kwargs):
                result = await run(operation, *args, **kwargs)
                if operation == worker.sample:
                    # The executor has finished. Delay delivery to the service
                    # coroutine without changing its decoded capture timestamp.
                    clock.value += handoff_seconds
                return result

            monkeypatch.setattr(runtime, "_run", delayed_handoff)
            await _sample(engine)
            if handoff_seconds > 2:
                assert service.recording_policy_status(source.binding)["state"] == "unavailable"
                assert service._media.paths[path]["record"] is False
                assert path not in runtime.holds
                assert worker.closed
            else:
                assert service.recording_policy_status(source.binding)["state"] == "recording"
                assert runtime.samples[path]["captured_at"] == 100.0
                assert runtime.holds[path] == 102.0
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_revocation_during_offloop_save_is_fenced_and_runtime_compensated(engine):
    async def scenario():
        service, source = engine.service, engine.sources[0]
        await service.start(engine.sources)
        engine.policies.release_write = threading.Event()
        revoked = False

        async def before_commit():
            def guard():
                if revoked:
                    raise HTTPException(403, "Permission revoked")

            return guard

        try:
            task = asyncio.create_task(
                service.apply_motion_policy(source, 0, _body(), before_commit)
            )
            assert await asyncio.to_thread(engine.policies.writing.wait, 1)
            revoked = True
            engine.policies.release_write.set()
            with pytest.raises(HTTPException) as error:
                await task
            assert error.value.status_code == 403
            assert engine.policies.get(source)["revision"] == 0
            assert service.recording_policy_status(source.binding)["mode"] == "continuous"
            assert service._media.paths[source.binding.path_name]["record"] is True
        finally:
            engine.policies.release_write.set()
            await service.stop()

    asyncio.run(scenario())


def test_motion_media_restart_configuration_is_closed_until_fresh_sampling(engine):
    async def scenario():
        service, source = engine.service, engine.sources[0]
        await service.start(engine.sources)
        try:
            await service.apply_motion_policy(source, 0, _body())
            await _sample(engine)
            engine.workers[-1].moving = True
            await _sample(engine)
            assert service._motion.effective_record(source)
            service._motion.invalidate()
            configuration = service._configuration(engine.sources)
            assert configuration["paths"][source.binding.path_name]["record"] is False
            assert configuration["paths"][engine.sources[1].binding.path_name]["record"] is True
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_preview_is_trusted_native_geometry_and_keeps_observer_for_full_lease(engine):
    async def scenario():
        service, source = engine.service, engine.sources[0]
        await service.start(engine.sources)
        try:
            await _sample(engine)
            preview = await service.motion_preview(source)
            assert preview["width"] == 320 and preview["height"] == 240
            assert preview["source_revision"] == engine.policies.source_revision(source)
            assert preview["reference_generation"] > 0
            assert (
                service._motion.interest[source.binding.path_name] - motion.time.monotonic() > 120
            )
            value = await service.get_motion_policy(source)
            assert value["observed_reference"]["generation"] == preview["reference_generation"]
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_revoked_before_commit_cannot_enable_a_motion_recorder(engine):
    async def scenario():
        service, source = engine.service, engine.sources[0]
        await service.start(engine.sources)
        try:
            await service.apply_motion_policy(source, 0, _body())
            attempts = []
            original = service._mutation

            def observe(method, route, payload=None, **kwargs):
                attempts.append(payload)
                return original(method, route, payload, **kwargs)

            service._mutation = observe

            async def deny():
                raise HTTPException(403, "Permission revoked")

            with pytest.raises(HTTPException):
                await service.apply_motion_policy(source, 1, _body(mode="continuous"), deny)
            assert attempts == []
            assert engine.policies.get(source)["mode"] == "motion"
            assert service._media.paths[source.binding.path_name]["record"] is False
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_failed_master_preference_restores_the_motion_source_map(engine):
    async def scenario():
        service, source = engine.service, engine.sources[0]
        await service.start(engine.sources)
        try:
            await service.apply_motion_policy(source, 0, _body())
            original = service._write_media
            failed = False

            def refuse_once():
                nonlocal failed
                if not failed:
                    failed = True
                    raise OSError("Generated write refusal")
                original()

            service._write_media = refuse_once
            with pytest.raises(RuntimeError):
                await service.apply_recording_preferences(
                    (replace(source, recording_enabled=False), engine.sources[1])
                )
            assert service._sources == engine.sources
            assert service._motion.sources[source.binding.path_name] == source
            await _sample(engine)
            assert service.recording_policy_status(source.binding)["state"] == "armed"
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_ambiguous_native_enable_is_compensated_even_when_old_cache_was_false(engine):
    async def scenario():
        service, source = engine.service, engine.sources[0]
        await service.start(engine.sources)
        try:
            await service.apply_motion_policy(source, 0, _body())
            await _sample(engine)
            worker = engine.workers[-1]
            worker.moving = True
            original = service._mutation
            failed = False

            def ambiguous(method, route, payload=None, **kwargs):
                nonlocal failed
                result = original(method, route, payload, **kwargs)
                if payload == {"record": True} and not failed:
                    failed = True
                    raise RuntimeError("Generated response loss after application")
                return result

            service._mutation = ambiguous
            await _sample(engine)
            assert failed
            assert service._media.paths[source.binding.path_name]["record"] is False
            assert service._media.paths[engine.sources[1].binding.path_name]["record"] is True
            assert service.recording_policy_status(source.binding)["state"] == "unavailable"
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_clear_all_zones_accepts_the_current_owned_preview_generation(engine):
    async def scenario():
        service, source = engine.service, engine.sources[0]
        await service.start(engine.sources)
        try:
            await _sample(engine)
            reference = service.current_motion_reference(source)
            result = await service.apply_motion_policy(
                source,
                0,
                _body(zones={"reference": None, "include": [], "exclude": []}),
                reference_generation=reference["generation"],
            )
            assert result["status"] in {"unavailable", "armed"}
            assert result["zones"]["reference"] is None
            assert result["revision"] == 1
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_old_preview_epoch_returns_a_finite_source_conflict(engine):
    async def scenario():
        service, source = engine.service, engine.sources[0]
        await service.start(engine.sources)
        try:
            await _sample(engine)
            reference = service.current_motion_reference(source)
            with pytest.raises(MotionUnavailable) as error:
                await service.apply_motion_policy(
                    source, 0, _body(), reference_generation=reference["generation"] + 1
                )
            assert error.value.code == "motion_source_changed"
            assert engine.policies.get(source)["revision"] == 0
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_cancelled_worker_constructor_remains_owned_until_reaped(engine):
    async def scenario():
        service, source = engine.service, engine.sources[0]
        await service.start(engine.sources)
        worker = _Worker()
        started, release = threading.Event(), threading.Event()

        def create(_service, _source):
            started.set()
            assert release.wait(2)
            return worker

        service._motion._new_worker = create
        try:
            await service.apply_motion_policy(source, 0, _body())
            task = asyncio.create_task(_sample(engine))
            assert await asyncio.to_thread(started.wait, 1)
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert worker.closed and worker in service._motion.retired
        finally:
            release.set()
            await service.stop()
        assert worker.reaped

    asyncio.run(scenario())


def test_startup_prunes_only_orphaned_saved_cameras(engine):
    async def scenario():
        removed = _HARNESS._source(3)
        engine.policies.save(removed, 0, _body())
        assert removed.binding.device_id in engine.policies.values
        await engine.service.start(engine.sources)
        try:
            assert removed.binding.device_id not in engine.policies.values
            await engine.service.apply_motion_policy(engine.sources[0], 0, _body())
            assert engine.sources[0].binding.device_id in engine.policies.values
            await engine.service.forget_motion_policy(engine.sources[0].binding.device_id)
            assert engine.sources[0].binding.device_id not in engine.policies.values
        finally:
            await engine.service.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("hold_enable_reply", [False, True], ids=["immediate-ack", "held-ack"])
def test_actual_background_loop_runs_without_browser_or_recording_files(
    engine, monkeypatch, hold_enable_reply
):
    async def scenario():
        service, source = engine.service, engine.sources[0]
        applied, release = threading.Event(), threading.Event()
        original_mutation = service._mutation

        def mutation(method, route, payload=None, **kwargs):
            result = original_mutation(method, route, payload, **kwargs)
            if (
                hold_enable_reply
                and route.endswith(source.binding.path_name)
                and payload == {"record": True}
            ):
                applied.set()
                assert release.wait(2), "Native reply control was not released"
            return result

        monkeypatch.setattr(service, "_mutation", mutation)
        await service.start(engine.sources)
        try:
            await service.apply_motion_policy(source, 0, _body(post_capture_seconds=1))
            motion.MotionRuntime.start(service._motion)
            await _HARNESS._until(
                lambda: service.recording_policy_status(source.binding)["state"] == "armed",
                timeout=2,
            )
            worker = engine.workers[-1]
            worker.moving = True
            if hold_enable_reply:
                assert await asyncio.to_thread(applied.wait, 1)
                # MediaMTX can apply a request before its HTTP reply returns.
                # That is still the original Armed stage until the service
                # observes the acknowledgment, not the next post-capture idle.
                assert service._media.paths[source.binding.path_name]["record"] is True
                assert service.recording_policy_status(source.binding)["state"] == "armed"
                assert service._motion.pending_gates[source.binding.path_name] is False
                release.set()
            await _HARNESS._until(
                lambda: (
                    service._media.paths[source.binding.path_name]["record"] is True
                    and service.recording_policy_status(source.binding)["state"] == "recording"
                ),
                timeout=2,
            )
            worker.moving = False
            await _HARNESS._until(
                lambda: service.recording_policy_status(source.binding)["state"] == "armed",
                timeout=3,
            )
            assert service._media.paths[source.binding.path_name]["record"] is False
            assert service._media.paths[engine.sources[1].binding.path_name]["record"] is True
        finally:
            release.set()
            await service.stop()
        assert all(worker.reaped for worker in engine.workers)
        assert service._motion.executor_closed
        assert not service._motion.operations

    asyncio.run(scenario())


def test_unloaded_policy_never_falls_back_to_continuous_when_store_is_installed(engine):
    async def scenario():
        service, source = engine.service, engine.sources[0]
        await service.start(engine.sources)
        try:
            service._motion.policies.pop(source.binding.path_name)
            assert service._motion.effective_record(source) is False
            assert service.recording_policy_status(source.binding)["state"] == "unavailable"
            assert (
                service._configuration(engine.sources)["paths"][source.binding.path_name]["record"]
                is False
            )
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_removed_camera_releases_ephemeral_sample_maps_after_its_last_task(engine):
    async def scenario():
        service, source = engine.service, engine.sources[0]
        await service.start(engine.sources)
        try:
            await service.apply_motion_policy(source, 0, _body())
            await _sample(engine)
            path = source.binding.path_name
            worker = engine.workers[-1]
            worker.release = threading.Event()
            worker.started.clear()
            policy = copy.deepcopy(service._motion.policies[path])
            generation = service._motion._generation(source, policy)
            pending = asyncio.create_task(service._motion._sample(source, policy, generation))
            service._motion.inflight[path] = pending
            pending.add_done_callback(lambda done: service._motion._sample_done(path, done))
            assert await asyncio.to_thread(worker.started.wait, 1)
            await service.apply_sources((engine.sources[1],))
            assert path in service._motion.generations
            worker.release.set()
            await pending
            await asyncio.sleep(0)
            for values in (
                service._motion.generations,
                service._motion.health,
                service._motion.samples,
                service._motion.observed,
                service._motion.reference_generations,
            ):
                assert path not in values
        finally:
            worker.release.set()
            await service.stop()

    asyncio.run(scenario())
