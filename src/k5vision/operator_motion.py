"""Bounded general-scene motion over trusted live camera pixels.

This detector owns no camera addresses, recordings, clocks or permissions. The
service supplies a current source/policy generation and applies its decision to
the independently owned recorder. Native decoding and image work run outside the
API process. No recording file is needed to discover movement.
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import json
import math
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote


class MotionUnavailable(RuntimeError):
    """A fixed, source-free motion failure suitable for an operator response."""

    def __init__(self, message, *, code="motion_policy_unavailable"):
        super().__init__(message)
        self.code = code


def analysis_dimensions(width: int, height: int) -> tuple[int, int]:
    if (
        type(width) is not int
        or type(height) is not int
        or not 1 <= width <= 8192
        or not 1 <= height <= 8192
        or width * height > 33_554_432
    ):
        raise MotionUnavailable("Motion frame geometry was refused")
    factor = min(1.0, 320 / max(width, height))
    return max(1, round(width * factor)), max(1, round(height * factor))


def _polygon(points, width: int, height: int):
    import numpy as np

    if type(points) is not list or not 3 <= len(points) <= 16:
        raise MotionUnavailable("Motion regions were refused")
    converted = []
    for point in points:
        if (
            type(point) is not list
            or len(point) != 2
            or any(type(value) not in (int, float) or not math.isfinite(value) for value in point)
            or any(not 0 <= value <= 1 for value in point)
        ):
            raise MotionUnavailable("Motion regions were refused")
        converted.append((round(point[0] * (width - 1)), round(point[1] * (height - 1))))
    return np.asarray(converted, dtype=np.int32)


def allowed_mask(width: int, height: int, include: list, exclude: list):
    """Union inclusions, then subtract exclusions in actual analysis coordinates."""
    import cv2
    import numpy as np

    if type(include) is not list or type(exclude) is not list or len(include) + len(exclude) > 8:
        raise MotionUnavailable("Motion regions were refused")
    mask = np.full((height, width), 255 if not include else 0, dtype=np.uint8)
    for points in include:
        cv2.fillPoly(mask, [_polygon(points, width, height)], 255)
    for points in exclude:
        cv2.fillPoly(mask, [_polygon(points, width, height)], 0)
    return mask


def worker_policy(policy: dict) -> dict:
    """Remove authoring metadata before sending an admitted policy to its child."""
    zones = policy["zones"]
    return {
        "revision": policy["revision"],
        "source_revision": policy["source_revision"],
        "sensitivity": policy["sensitivity"],
        "minimum_area_percent": policy["minimum_area_percent"],
        "include": [polygon["points"] for polygon in zones["include"]],
        "exclude": [polygon["points"] for polygon in zones["exclude"]],
    }


def validate_effective_mask(policy: dict) -> None:
    """Refuse a policy with no enabled detector pixels before saving it."""
    zones = policy["zones"]
    reference = zones["reference"]
    if reference is None:
        if zones["include"] or zones["exclude"]:
            raise MotionUnavailable("Motion regions require a current camera frame")
        return
    width, height = analysis_dimensions(reference["width"], reference["height"])
    selection = worker_policy(policy)
    mask = allowed_mask(width, height, selection["include"], selection["exclude"])
    if not bool(mask.any()):
        raise MotionUnavailable(
            "Motion regions exclude the entire camera frame", code="motion_zones_empty"
        )


class SceneMotionDetector:
    """Background-average motion with bounded pixels and exclusion-safe filtering.

    OpenCV/NumPy are already bundled with K5. A first image establishes the
    baseline; it never invents movement. Policy/source/geometry changes discard
    that baseline. The minimum changed area uses the full analysis-frame area,
    after filtering by the enabled regions.
    """

    def __init__(self):
        self._generation = None
        self._background = None
        self._mask = None
        self._native_mask = None

    def observe(self, frame, policy: dict) -> bool:
        import cv2
        import numpy as np

        if (
            not isinstance(frame, np.ndarray)
            or frame.dtype != np.uint8
            or frame.ndim != 3
            or frame.shape[2] != 3
        ):
            raise MotionUnavailable("Motion frame was refused")
        original_height, original_width = frame.shape[:2]
        width, height = analysis_dimensions(original_width, original_height)
        sensitivity = policy["sensitivity"]
        area = policy["minimum_area_percent"]
        if (
            type(sensitivity) is not int
            or not 1 <= sensitivity <= 100
            or type(area) not in (int, float)
            or not math.isfinite(area)
            or not 0.1 <= area <= 100
        ):
            raise MotionUnavailable("Motion sensitivity was refused")
        generation = (original_width, original_height, json.dumps(policy, sort_keys=True))
        if generation != self._generation:
            self._generation = generation
            self._background = None
            self._mask = allowed_mask(width, height, policy["include"], policy["exclude"])
            self._native_mask = allowed_mask(
                original_width, original_height, policy["include"], policy["exclude"]
            )
        if not bool(self._mask.any()):
            raise MotionUnavailable("Motion regions exclude the entire camera frame")
        # Mask before downsampling too: INTER_AREA must not blend excluded
        # native pixels into enabled analysis pixels along a polygon boundary.
        selected = cv2.bitwise_and(frame, frame, mask=self._native_mask)
        reduced = cv2.resize(selected, (width, height), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(reduced, cv2.COLOR_BGR2GRAY)
        # Excluded pixels must not affect neighboring pixels through blur or
        # background statistics. Global contrast normalization is deliberately
        # absent: excluded movement must not rescale included pixels either.
        gray = cv2.bitwise_and(gray, self._mask)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)
        gray = cv2.bitwise_and(gray, self._mask)
        if self._background is None:
            self._background = gray.astype(np.float32)
            return False
        difference = cv2.absdiff(gray, cv2.convertScaleAbs(self._background))
        threshold = round(4 + (100 - sensitivity) * 0.6)
        changed = cv2.threshold(difference, threshold, 255, cv2.THRESH_BINARY)[1]
        changed = cv2.bitwise_and(changed, self._mask)
        changed = cv2.dilate(changed, np.ones((3, 3), dtype=np.uint8), iterations=1)
        changed = cv2.bitwise_and(changed, self._mask)
        # Connected-component area counts enabled changed pixels, rather than a
        # contour's enclosing rectangle spanning an excluded hole.
        count, _labels, statistics, _centers = cv2.connectedComponentsWithStats(changed, 8)
        minimum = max(1, math.ceil(width * height * float(area) / 100))
        motion = any(
            int(statistics[index, cv2.CC_STAT_AREA]) >= minimum for index in range(1, count)
        )
        cv2.accumulateWeighted(gray, self._background, 0.05, mask=self._mask)
        return motion


class MotionRuntime:
    """Own independent live sampling and the recorder's ephemeral motion gate.

    Durable master intent remains in the camera store. Durable motion settings
    are supplied by the policy store. Every worker result is checked against
    the current source, policy and media generations under the service lock.
    """

    def __init__(self, service):
        self.service = service
        self.store = None
        self.policies = {}
        self.sources = {}
        self.workers = {}
        self.retired = []
        self.failed_workers = []
        self.cleanup_tasks = set()
        self.generations = {}
        self.holds = {}
        self.health = {}
        self.moving = {}
        self.samples = {}
        self.observed = {}
        self.reference_generations = {}
        self.gates = {}
        self.pending_gates = {}
        self.interest = {}
        self.inflight = {}
        self.mask_lock = asyncio.Lock()
        self.mask_tasks = set()
        self.mask_worker = None
        self.operations = set()
        self.last_sample = {}
        self.task = None
        self.closing = False
        # One request per enrolled camera plus two slots for policy/control
        # work. Native decode itself stays in isolated, single-thread children.
        # Camera outages must never occupy the operator's default I/O executor.
        self.executor = ThreadPoolExecutor(max_workers=66, thread_name_prefix="k5-live-motion")
        self.executor_closed = False

    def install(self, store):
        if (
            self.store is not None
            or self.service._media is not None
            or not all(
                callable(getattr(store, name, None)) for name in ("get", "save", "source_revision")
            )
        ):
            raise MotionUnavailable("Motion policy installation was refused")
        self.store = store

    @staticmethod
    def _same_source(first, second):
        return (
            first.binding == second.binding
            and first.device == second.device
            and first.source_uri == second.source_uri
            and first.recording_enabled == second.recording_enabled
            and getattr(first, "profile_key", "") == getattr(second, "profile_key", "")
        )

    def _submit(self, operation, *args):
        if self.executor_closed:
            raise MotionUnavailable("Motion workers are closed")
        context = contextvars.copy_context()
        return asyncio.get_running_loop().run_in_executor(
            self.executor, context.run, operation, *args
        )

    async def _run(self, operation, *args, cancel_result=None):
        task = self._submit(operation, *args)
        self.operations.add(task)
        cancelled = False
        try:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancelled = True
                except Exception:
                    break
            result = task.result()
            if cancelled:
                if cancel_result is not None:
                    cancel_result(result)
                raise asyncio.CancelledError
            return result
        finally:
            self.operations.discard(task)

    def _retire(self, path):
        worker = self.workers.pop(path, None)
        if worker is not None:
            worker.begin_close()
            self.retired.append(worker)

    def _retire_result(self, worker):
        worker.begin_close()
        self.retired.append(worker)

    def _forget_ephemeral(self, path):
        # Keep the epoch until the last old sample has drained. Once no task
        # can commit a late result, a removed camera need not occupy RAM maps.
        for values in (
            self.generations,
            self.holds,
            self.health,
            self.moving,
            self.samples,
            self.observed,
            self.reference_generations,
            self.gates,
            self.pending_gates,
            self.interest,
            self.last_sample,
        ):
            values.pop(path, None)

    def _sample_done(self, path, task):
        if self.inflight.get(path) is task:
            self.inflight.pop(path, None)
        if path not in self.sources and path not in self.inflight:
            self._forget_ephemeral(path)

    def invalidate(self, paths=None, *, clear_preview=True):
        for path in tuple(self.sources if paths is None else paths):
            self.generations[path] = self.generations.get(path, 0) + 1
            self.holds.pop(path, None)
            self.health[path] = "unavailable"
            self.moving.pop(path, None)
            self.gates.pop(path, None)
            self._retire(path)
            if clear_preview:
                self.samples.pop(path, None)
                self.observed.pop(path, None)
                self.reference_generations[path] = self.reference_generations.get(path, 0) + 1

    async def load(self, sources, *, prune=False):
        if self.store is None:
            file = self.service.state_root / "operator-recording-policy.json"
            if file.exists() or file.is_symlink():
                raise MotionUnavailable("Saved motion policies require their admitted owner")
            return
        if prune:
            await self._run(
                self.store.prune_existing, tuple(source.binding.device_id for source in sources)
            )
        policies = await self._run(
            lambda: {s.binding.path_name: self.store.get(s) for s in sources}
        )
        selections = {source.binding.path_name: source for source in sources}
        removed = set(self.sources) - set(selections)
        for path, previous in tuple(self.sources.items()):
            replacement = selections.get(path)
            if replacement is None or not self._same_source(previous, replacement):
                self.invalidate((path,))
        self.sources, self.policies = selections, policies
        for path in removed:
            if path not in self.inflight:
                self._forget_ephemeral(path)

    def selection_snapshot(self):
        return dict(self.sources), copy.deepcopy(self.policies)

    def restore_selection(self, snapshot):
        self.sources, self.policies = snapshot
        self.invalidate()

    def _generation(self, source, policy):
        path = source.binding.path_name
        return (
            self.generations.get(path, 0),
            self.service.media_generation,
            policy["revision"],
            policy["source_revision"],
        )

    def _current(self, source, policy, generation):
        path = source.binding.path_name
        current = self.sources.get(path)
        return (
            not self.closing
            and not self.service._closing
            and self.service._ready
            and current is not None
            and self._same_source(current, source)
            and any(self._same_source(value, source) for value in self.service._sources)
            and self.policies.get(path) == policy
            and self._generation(source, policy) == generation
        )

    def _zones_stale(self, path, policy):
        if policy["policy_source_revision"] != policy["source_revision"]:
            return True
        reference = policy["zones"]["reference"]
        if reference is None:
            return False
        observed = self.observed.get(path)
        return (
            reference["source_revision"] != policy["source_revision"]
            or reference["rotation"] != 0
            or (
                observed is not None
                and (reference["width"], reference["height"])
                != (observed["width"], observed["height"])
            )
        )

    def effective_record(self, source, *, now=None):
        if not source.recording_enabled or self.service._disk_full:
            return False
        path = source.binding.path_name
        policy = self.policies.get(path)
        if policy is None:
            return self.store is None
        if policy["mode"] == "continuous":
            return True
        now = time.monotonic() if now is None else now
        sampled = self.samples.get(path)
        return (
            self.service._ready
            and not self.service._needs_reconcile
            and self.health.get(path) == "ready"
            and not self._zones_stale(path, policy)
            and sampled is not None
            and 0 <= now - sampled["captured_at"] <= 2
            and self.holds.get(path, 0) > now
        )

    def status(self, binding):
        path = binding.path_name
        policy = self.policies.get(path)
        mode = "continuous" if policy is None else policy["mode"]
        source = self.sources.get(path)
        if source is None:
            source = next((s for s in self.service._sources if s.binding == binding), None)
        if source is not None and not source.recording_enabled:
            state = "paused"
        elif self.service._disk_full:
            state = "storage_blocked"
        elif policy is None and self.store is not None:
            state = "unavailable"
        elif mode == "continuous":
            state = "continuous"
        elif self._zones_stale(path, policy):
            state = "zones_need_review"
        elif (
            not self.service._ready
            or self.health.get(path) != "ready"
            or path not in self.samples
            or not (0 <= time.monotonic() - self.samples[path]["captured_at"] <= 2)
        ):
            state = "unavailable"
        elif self.gates.get(path, self.pending_gates.get(path)) is True:
            state = (
                "recording"
                if self.moving.get(path, False) and self.effective_record(source)
                else "post_capture"
            )
        else:
            state = "armed"
        return {
            "mode": mode,
            "state": state,
            "status": state,
            "revision": 0 if policy is None else policy["revision"],
            "post_capture_seconds": 10 if policy is None else policy["post_capture_seconds"],
            "pre_capture_seconds": 0,
            "trigger_source": "pixel_motion",
        }

    def current_reference(self, source):
        """Pure final preview fence, usable immediately after authorization."""
        current = self.sources.get(source.binding.path_name)
        if (
            self.closing
            or self.service._closing
            or current is None
            or not self._same_source(current, source)
        ):
            return None
        return copy.deepcopy(self.observed.get(source.binding.path_name))

    async def get_policy(self, source):
        if self.store is None or self.closing:
            raise MotionUnavailable("Motion policy is unavailable")
        async with self.service._lock:
            current = self.sources.get(source.binding.path_name)
            if current is None or not self._same_source(current, source):
                raise MotionUnavailable("Motion camera selection changed")
            policy = await self._run(self.store.get, source)
            return {
                **policy,
                **self.status(source.binding),
                "observed_reference": copy.deepcopy(self.observed.get(source.binding.path_name)),
            }

    async def forget(self, camera_id):
        if self.store is None or self.closing:
            raise MotionUnavailable("Motion policy is unavailable")
        async with self.service._lock:
            for path, source in tuple(self.sources.items()):
                if source.binding.device_id == camera_id:
                    self.invalidate((path,))
                    self.sources.pop(path, None)
                    self.policies.pop(path, None)
                    self.interest.pop(path, None)
            return await self._run(self.store.remove, camera_id)

    @staticmethod
    def _new_worker(service, source):
        from k5vision.operator_motion_process import LiveMotionWorker

        username = quote(service.credentials.username, safe="")
        password = quote(service.credentials.password, safe="")
        relay = f"rtsp://{username}:{password}@127.0.0.1:{service.ports['rtsp_port']}/{source.binding.path_name}"
        return LiveMotionWorker(relay)

    async def _gate(self, source, enabled, before_write=None):
        path = source.binding.path_name
        if self.gates.get(path) is enabled:
            return
        # A timeout/cancellation can arrive after MediaMTX applies the write.
        # Forget the old observation before dispatch so compensating False is
        # never skipped merely because the previous cached value was False.
        previous = self.gates.pop(path, None)
        self.pending_gates[path] = previous

        def dispatch():
            if before_write is not None:
                before_write()
            self.service._mutation("PATCH", f"/v3/config/paths/patch/{path}", {"record": enabled})

        try:
            await self._run(dispatch)
            self.gates[path] = enabled
        finally:
            self.pending_gates.pop(path, None)

    def note_gate(self, path, enabled):
        self.gates[path] = enabled

    def _cache_sample(self, source, policy, result, captured_at):
        path = source.binding.path_name
        analysis_dimensions(result.width, result.height)
        previous = self.observed.get(path)
        geometry = (result.width, result.height, policy["source_revision"])
        if previous is None or geometry != (
            previous["width"],
            previous["height"],
            previous["source_revision"],
        ):
            self.reference_generations[path] = self.reference_generations.get(path, 0) + 1
        reference = {
            "width": result.width,
            "height": result.height,
            "rotation": 0,
            "source_revision": policy["source_revision"],
            "generation": self.reference_generations[path],
        }
        self.observed[path] = reference
        self.samples[path] = {
            "jpeg": result.jpeg,
            "captured_at": captured_at,
            "reference": reference,
            "capture_id": secrets.token_urlsafe(24),
        }
        return captured_at

    async def _sample(self, source, policy, generation):
        path = source.binding.path_name
        worker = self.workers.get(path)
        try:
            if worker is None:
                worker = await self._run(
                    self._new_worker, self.service, source, cancel_result=self._retire_result
                )
                if not self._current(source, policy, generation):
                    worker.begin_close()
                    await self._run(worker.close)
                    return
                self.workers[path] = worker
            result = await self._run(worker.sample, worker_policy(policy))
            completed_at = time.monotonic()
            async with self.service._lock:
                if (
                    not self._current(source, policy, generation)
                    or self.workers.get(path) is not worker
                ):
                    return
                if not (
                    type(result.age_seconds) in (int, float)
                    and math.isfinite(result.age_seconds)
                    and 0 <= result.age_seconds + time.monotonic() - completed_at <= 2
                ):
                    raise MotionUnavailable("Motion frame is no longer current")
                captured_at = self._cache_sample(
                    source, policy, result, completed_at - result.age_seconds
                )
                if self._zones_stale(path, policy):
                    self.health[path] = "zones_need_review"
                    self.holds.pop(path, None)
                else:
                    self.health[path] = "ready"
                    self.moving[path] = result.motion
                    if policy["mode"] == "motion" and result.motion:
                        self.holds[path] = captured_at + policy["post_capture_seconds"]
                if policy["mode"] == "motion":
                    await self._gate(source, self.effective_record(source))
        except asyncio.CancelledError:
            raise
        except Exception:
            async with self.service._lock:
                if self._current(source, policy, generation):
                    self.health[path] = "unavailable"
                    self.holds.pop(path, None)
                    self._retire(path)
                    if policy["mode"] == "motion":
                        try:
                            await self._gate(source, False)
                        except Exception:
                            # A native write with unknown outcome cannot be
                            # represented as a safely armed camera. The existing
                            # worker recovery will regenerate every motion gate
                            # closed while retaining continuous camera intent.
                            self.service._needs_reconcile = True
                            self.service._ready = False

    def start(self):
        if self.store is not None and self.task is None:
            self.task = asyncio.create_task(self._loop(), name="operator-live-motion")

    async def _loop(self):
        while not self.closing and not self.service._closing:
            now = time.monotonic()
            for path, source in tuple(self.sources.items()):
                policy = self.policies[path]
                preview = self.interest.get(path, 0) > now
                enabled = (
                    policy["mode"] == "motion"
                    and source.recording_enabled
                    and not self.service._disk_full
                )
                if not (preview or enabled) or not self.service._ready:
                    self._retire(path)
                    continue
                if path not in self.inflight and now - self.last_sample.get(path, -1) >= 0.5:
                    self.last_sample[path] = now
                    task = asyncio.create_task(
                        self._sample(
                            source, copy.deepcopy(policy), self._generation(source, policy)
                        )
                    )
                    self.inflight[path] = task
                    task.add_done_callback(lambda done, key=path: self._sample_done(key, done))
            async with self.service._lock:
                if self.service._ready:
                    for path, source in tuple(self.sources.items()):
                        if self.policies[path]["mode"] == "motion" and path in self.service._paths:
                            try:
                                await self._gate(source, self.effective_record(source))
                            except Exception:
                                self.health[path] = "unavailable"
                                self.holds.pop(path, None)
                                self.service._needs_reconcile = True
                                self.service._ready = False
                                break
            retired, self.retired = self.retired, []
            for worker in retired:
                task = asyncio.create_task(self._finish_retired(worker))
                self.cleanup_tasks.add(task)
                task.add_done_callback(self.cleanup_tasks.discard)
            await asyncio.sleep(0.1)

    async def _finish_retired(self, worker):
        try:
            await self._run(worker.close)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.failed_workers.append(worker)

    async def preview(self, source):
        if self.store is None or self.closing or not self.service._ready:
            raise MotionUnavailable("Motion frame is unavailable")
        path = source.binding.path_name
        self.interest[path] = time.monotonic() + 125
        self.start()
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            async with self.service._lock:
                current = self.sources.get(path)
                sample = self.samples.get(path)
                if current is None or not self._same_source(current, source):
                    raise MotionUnavailable("Motion camera selection changed")
                if sample is not None and 0 <= time.monotonic() - sample["captured_at"] <= 2:
                    reference = sample["reference"]
                    return {
                        "jpeg": sample["jpeg"],
                        "width": reference["width"],
                        "height": reference["height"],
                        "rotation": 0,
                        "source_revision": reference["source_revision"],
                        "capture_id": sample["capture_id"],
                        "reference_generation": reference["generation"],
                    }
            await asyncio.sleep(0.05)
        raise MotionUnavailable("Motion frame is unavailable")

    @staticmethod
    def _new_mask_validator():
        from k5vision.operator_motion_process import MaskValidationWorker

        return MaskValidationWorker()

    async def _validate_mask(self, policy):
        # A whole-frame policy needs neither a native import nor a child. The
        # admitted DTO already rejects polygons without a reference image.
        if policy["zones"]["reference"] is None:
            return
        owner = asyncio.current_task()
        self.mask_tasks.add(owner)
        try:
            async with self.mask_lock:
                if self.closing or self.service._closing:
                    raise MotionUnavailable("Motion policy is stopping")
                created = []
                cancelled = False

                def create():
                    worker = self._new_mask_validator()
                    created.append(worker)
                    return worker

                try:
                    worker = await self._run(create)
                    self.mask_worker = worker
                    if self.closing or self.service._closing:
                        raise MotionUnavailable("Motion policy is stopping")
                    request = self._submit(worker.validate, policy)
                    self.operations.add(request)
                    try:
                        while not request.done():
                            try:
                                await asyncio.shield(request)
                            except asyncio.CancelledError:
                                cancelled = True
                                worker.begin_close()
                            except Exception:
                                break
                        if cancelled:
                            raise asyncio.CancelledError
                        allowed = request.result()
                    finally:
                        self.operations.discard(request)
                finally:
                    # This source-free validation child belongs to one request,
                    # including when the API owner later rejects a canceled or
                    # revoked operation. Never retain its native import in the
                    # API process, or return before its owned cleanup completes.
                    if created:
                        worker = created[0]
                        worker.begin_close()
                        try:
                            await self._run(worker.close)
                        except asyncio.CancelledError:
                            cancelled = True
                        except Exception:
                            self.failed_workers.append(worker)
                            raise MotionUnavailable(
                                "Motion validation cleanup was incomplete"
                            ) from None
                        finally:
                            if self.mask_worker is worker:
                                self.mask_worker = None
                if cancelled:
                    raise asyncio.CancelledError
                if type(allowed) is not bool:
                    raise MotionUnavailable("Motion validation was refused")
                if not allowed:
                    raise MotionUnavailable(
                        "Motion regions exclude the entire camera frame", code="motion_zones_empty"
                    )
        finally:
            self.mask_tasks.discard(owner)

    async def configure(
        self, source, expected_revision, policy, before_commit, reference_generation=None
    ):
        from k5vision.operator_recording_policy import (
            RecordingPolicyConflict,
            validate_motion_policy,
        )

        if self.store is None or self.closing:
            raise MotionUnavailable("Motion policy is unavailable")
        admitted = validate_motion_policy(policy)
        # Pure region admission can load a native image library. Keep it in its
        # killable child and outside the recorder lock; camera sampling and
        # post-capture deadlines must progress while it runs. All mutable state
        # and authority are rechecked below after the bounded result arrives.
        await self._validate_mask(admitted)
        async with self.service._lock:
            path = source.binding.path_name
            current = self.sources.get(path)
            if current is None or not self._same_source(source, current) or not self.service._ready:
                raise MotionUnavailable("Motion camera selection changed")
            previous = await self._run(self.store.get, source)
            if previous["revision"] != expected_revision:
                raise RecordingPolicyConflict("motion_policy_conflict")
            candidate = {
                **previous,
                **admitted,
                "revision": expected_revision + 1,
                "policy_source_revision": previous["source_revision"],
            }

            def check_reference():
                reference = admitted["zones"]["reference"]
                if reference_generation is None:
                    return
                observed = self.observed.get(path)
                if observed is None or observed["generation"] != reference_generation:
                    raise MotionUnavailable(
                        "Motion camera frame changed", code="motion_source_changed"
                    )
                if reference is not None and any(
                    observed[key] != reference[key]
                    for key in ("width", "height", "rotation", "source_revision")
                ):
                    raise MotionUnavailable(
                        "Motion camera frame changed", code="motion_source_changed"
                    )

            check_reference()
            # Capture fresh authority before any native effect, then fence both
            # the recorder dispatch and the final durable replacement with that
            # same pure current-session/policy/selection check.
            fence = None if before_commit is None else await before_commit()
            check_reference()

            def final_fence():
                if fence is not None:
                    fence()
                if self.closing or self.service._closing:
                    raise MotionUnavailable("Motion policy is stopping")
                current = self.sources.get(path)
                if current is None or not self._same_source(current, source):
                    raise MotionUnavailable("Motion camera selection changed")
                check_reference()

            self.invalidate((path,), clear_preview=False)
            self.policies[path] = candidate
            try:
                await self._gate(source, self.effective_record(source), before_write=final_fence)
                await self._run(self.service._write_media)
                check_reference()
                public = {
                    **candidate,
                    **self.status(source.binding),
                    "observed_reference": self.current_reference(source),
                }
                # Once save commits, do not await, reread, or perform fallible
                # work. Cancellation must not roll back a successfully saved
                # policy after the thread has completed its atomic commit.
                commit = self._submit(
                    lambda: self.store.save(
                        source, expected_revision, admitted, before_write=final_fence
                    )
                )
                self.operations.add(commit)
                cancelled = False
                try:
                    while not commit.done():
                        try:
                            await asyncio.shield(commit)
                        except asyncio.CancelledError:
                            cancelled = True
                    saved = commit.result()
                finally:
                    self.operations.discard(commit)
                self.policies[path] = saved
                if cancelled:
                    # State is committed; the response may be canceled, but its
                    # caller must not compensate a durable completed operation.
                    return public
                return public
            except BaseException:
                self.policies[path] = previous
                self.invalidate((path,), clear_preview=False)
                repair = asyncio.create_task(self._restore(source))
                while not repair.done():
                    try:
                        await asyncio.shield(repair)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if repair.exception() is not None:
                    self.service._needs_reconcile = True
                    self.service._ready = False
                raise

    async def _restore(self, source):
        await self._gate(source, self.effective_record(source))
        await self._run(self.service._write_media)

    async def close(self):
        self.closing = True
        if self.mask_worker is not None:
            self.mask_worker.begin_close()
        self.invalidate()
        tasks = [*self.inflight.values()]
        tasks.extend(self.mask_tasks)
        tasks.extend(self.cleanup_tasks)
        if self.task is not None:
            tasks.append(self.task)
        for task in tasks:
            task.cancel()
        cancelled = False
        for task in tasks:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancelled = cancelled or bool(asyncio.current_task().cancelling())
                except Exception:
                    break
        for operation in tuple(self.operations):
            while not operation.done():
                try:
                    await asyncio.shield(operation)
                except asyncio.CancelledError:
                    cancelled = True
                except Exception:
                    break
        failures = False
        retired, self.retired = self.retired + self.failed_workers, []
        self.failed_workers = []
        cleanup = [self._submit(worker.close) for worker in retired]
        for task in cleanup:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancelled = True
                except Exception:
                    break
            try:
                task.result()
            except BaseException:
                failures = True
                self.failed_workers.append(retired[cleanup.index(task)])
        self.samples.clear()
        self.observed.clear()
        if failures:
            raise MotionUnavailable("Motion worker cleanup was incomplete")
        if not self.executor_closed:
            shutdown = asyncio.create_task(
                asyncio.to_thread(self.executor.shutdown, wait=True, cancel_futures=False)
            )
            while not shutdown.done():
                try:
                    await asyncio.shield(shutdown)
                except asyncio.CancelledError:
                    cancelled = True
            shutdown.result()
            self.executor_closed = True
        if cancelled:
            raise asyncio.CancelledError
