"""A slow or canceled native mask must not hold the recorder's service lock."""

from __future__ import annotations

import asyncio
import builtins
import copy
import importlib.util
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import k5vision.operator_motion as motion
from k5vision.operator_motion import MotionUnavailable

_SPEC = importlib.util.spec_from_file_location(
    "motion_mask_service_harness", Path(__file__).with_name("test_operator_motion.py")
)
_HARNESS = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_HARNESS)
native = _HARNESS.native
engine = _HARNESS.engine


def _policy(engine):
    source = engine.sources[0]
    return _HARNESS._body(
        zones={
            "reference": {
                "width": 320,
                "height": 240,
                "rotation": 0,
                "source_revision": engine.policies.source_revision(source),
            },
            "include": [],
            "exclude": [],
        }
    )


def _blocked_validator(engine, monkeypatch):
    validator = _HARNESS._MaskValidator()
    validator.release = threading.Event()
    monkeypatch.setattr(engine.service._motion, "_new_mask_validator", lambda: validator)
    return validator


def test_pending_validation_never_imports_native_libraries_or_blocks_other_camera(
    engine, monkeypatch
):
    async def scenario():
        service, first, other = engine.service, *engine.sources
        clock = SimpleNamespace(value=100.0)
        monkeypatch.setattr(motion, "time", SimpleNamespace(monotonic=lambda: clock.value))
        await service.start(engine.sources)
        validator = _blocked_validator(engine, monkeypatch)
        original_import = builtins.__import__
        attempted = []

        def no_native(name, *args, **kwargs):
            if name.split(".", 1)[0] in {"cv2", "numpy"}:
                attempted.append(name)
                raise AssertionError("The API parent attempted a native image import")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_native)
        try:
            await service.apply_motion_policy(first, 0, _HARNESS._body())
            await service.apply_motion_policy(other, 0, _HARNESS._body())
            task = asyncio.create_task(service.apply_motion_policy(first, 1, _policy(engine)))
            assert await asyncio.to_thread(validator.started.wait, 1)
            assert service._lock.locked() is False
            runtime = service._motion
            policy = copy.deepcopy(runtime.policies[other.binding.path_name])

            async def sample_other():
                await runtime._sample(other, policy, runtime._generation(other, policy))

            await sample_other()
            worker = runtime.workers[other.binding.path_name]
            worker.moving = True
            await sample_other()
            assert service._media.paths[other.binding.path_name]["record"] is True
            worker.moving = False
            clock.value += 3
            await sample_other()
            assert service._media.paths[other.binding.path_name]["record"] is False
            assert not task.done()
            validator.release.set()
            result = await task
            assert result["revision"] == 2 and validator.reaped
            assert attempted == []
        finally:
            validator.release.set()
            await service.stop()

    asyncio.run(scenario())


def test_canceling_validation_kills_and_reaps_before_return_without_mutation(engine, monkeypatch):
    async def scenario():
        service, source = engine.service, engine.sources[0]
        await service.start(engine.sources)
        validator = _blocked_validator(engine, monkeypatch)
        try:
            task = asyncio.create_task(service.apply_motion_policy(source, 0, _policy(engine)))
            assert await asyncio.to_thread(validator.started.wait, 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert validator.closed and validator.reaped
            assert service._motion.mask_worker is None and not service._motion.mask_tasks
            assert not service._motion.operations
            assert engine.policies.get(source)["revision"] == 0
            assert service._media.paths[source.binding.path_name]["record"] is True
            assert engine.attempts == []
        finally:
            validator.release.set()
            await service.stop()

    asyncio.run(scenario())


def test_service_shutdown_closes_inflight_mask_before_its_executor(engine, monkeypatch):
    async def scenario():
        service, source = engine.service, engine.sources[0]
        await service.start(engine.sources)
        validator = _blocked_validator(engine, monkeypatch)
        task = asyncio.create_task(service.apply_motion_policy(source, 0, _policy(engine)))
        assert await asyncio.to_thread(validator.started.wait, 1)
        await service.stop()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert validator.reaped and service._motion.executor_closed
        assert not service._motion.mask_tasks and not service._motion.operations

    asyncio.run(scenario())


def test_geometry_is_rechecked_after_isolated_validation_finishes(engine, monkeypatch):
    async def scenario():
        service, source = engine.service, engine.sources[0]
        await service.start(engine.sources)
        validator = _blocked_validator(engine, monkeypatch)
        try:
            await service.apply_motion_policy(source, 0, _HARNESS._body())
            await _HARNESS._sample(engine)
            reference = service.current_motion_reference(source)
            task = asyncio.create_task(
                service.apply_motion_policy(
                    source, 1, _policy(engine), reference_generation=reference["generation"]
                )
            )
            assert await asyncio.to_thread(validator.started.wait, 1)
            worker = engine.workers[-1]
            worker.width, worker.height = 640, 360
            await _HARNESS._sample(engine)
            validator.release.set()
            with pytest.raises(MotionUnavailable) as error:
                await task
            assert error.value.code == "motion_source_changed"
            assert engine.policies.get(source)["revision"] == 1
            assert validator.reaped
        finally:
            validator.release.set()
            await service.stop()

    asyncio.run(scenario())


def test_wholeframe_policy_needs_no_validation_child(engine, monkeypatch):
    async def scenario():
        service, source = engine.service, engine.sources[0]
        await service.start(engine.sources)

        def refuse():
            raise AssertionError("A whole-frame policy started a native validator")

        monkeypatch.setattr(service._motion, "_new_mask_validator", refuse)
        try:
            result = await service.apply_motion_policy(source, 0, _HARNESS._body())
            assert result["revision"] == 1
            assert service._motion.mask_worker is None
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_empty_isolated_mask_retains_422_and_leaves_recording_unchanged(engine, monkeypatch):
    async def scenario():
        service, source = engine.service, engine.sources[0]
        await service.start(engine.sources)
        validator = _HARNESS._MaskValidator()
        validator.allowed = False
        monkeypatch.setattr(service._motion, "_new_mask_validator", lambda: validator)
        try:
            with pytest.raises(MotionUnavailable) as error:
                await service.apply_motion_policy(source, 0, _policy(engine))
            assert error.value.code == "motion_zones_empty"
            assert validator.reaped
            assert engine.policies.get(source)["revision"] == 0 and engine.attempts == []
            assert service._media.paths[source.binding.path_name]["record"] is True
        finally:
            await service.stop()

    asyncio.run(scenario())
