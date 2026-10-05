#!/usr/bin/env python3
"""Observe actual installed Test/Run facades in a disposable, offline layout.

This distinct engineering witness does not replace Start-v1, installer/upgrade/
shortcut acceptance, person-box evidence, beta acceptance, or public RTSP tests.
The facade process inventories are observations, not prequalified exact profiles.
"""

from __future__ import annotations

import argparse
import ctypes
import importlib.util
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace

_SPEC = importlib.util.spec_from_file_location(
    "_facade_alpha", Path(__file__).with_name("installed_alpha_launcher_witness.py")
)
assert _SPEC is not None and _SPEC.loader is not None
alpha = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(alpha)
common, boundary = alpha.common, alpha.boundary
need = boundary.need

SCHEMA = "installed-alpha-facades-v1"
SCOPE = "owned-installed-test-run-facades-engineering-only"
RECEIPT_NAME = "installed-alpha-facades-witness.json"
EXPECTATIONS_NAME = "installed-alpha-facades-expectations.json"
IDENTITIES = alpha.IDENTITIES | {"facade_controller_sha256"}
INPUT_IDENTITIES = IDENTITIES - {"runtime_identity_sha256"}
MODES = {"test_invalid", "test_valid", "run_1", "run_2"}
BASE_IMAGES = {"base_python", "venv_python", "powershell", "console_host"}
RUN_IMAGES = {"gst_launch", "gst_inspect", "gst_scanner", "mediamtx"}
PROCESS_KINDS = BASE_IMAGES | RUN_IMAGES | {"unknown"}
# Resource ceilings, never assertions about a native success inventory.
MAX_PROCESSES = boundary.MAX_PROCESSES
MAX_EVENTS = boundary.MAX_EVENTS
TEMP_COUNTS = {
    "temp_" + kind + "_" + action
    for kind in boundary.TEMP_KINDS
    for action in boundary.ACTIONS.values()
}
OBS_COUNTS = (
    {
        "job_total",
        "job_active",
        "distinct_births",
        "temp_events",
        "policy_ps1_files",
        "policy_psm1_files",
    }
    | {"process_" + kind for kind in PROCESS_KINDS}
    | TEMP_COUNTS
)
OBS_BOOLEANS = {
    "process_coverage_complete",
    "cleanup_complete",
    "temporary_root_empty",
    "policy_lifecycles_complete",
    "temp_drain_complete",
}
OBS_FIELDS = OBS_COUNTS | OBS_BOOLEANS | {"schema_version", "mode", "error"}
FLAGS = {
    "completed",
    "cleanup_complete",
    "test_valid_completed",
    "test_valid_no_session",
    "test_invalid_refused",
    "test_invalid_no_session",
    "test_invalid_no_media",
    "person_box_acceptance",
    "installer_acceptance",
}
FIELDS = (
    IDENTITIES
    | FLAGS
    | {
        "schema_version",
        "revision",
        "run_nonce",
        "analytics_revision",
        "acceptance_scope",
        "fixture",
    }
    | {mode + "_observation" for mode in MODES}
    | {
        f"run_{attempt}_{name}"
        for attempt in (1, 2)
        for name in alpha.COUNTERS | alpha.RUN_BOOLEANS
    }
)
STAGES = {"admission", "build", "install", "probe", "verify", "cleanup", "complete"} | MODES


# Failure-only evidence is separate from acceptance and contains no identities.
EVIDENCE_PARTS = {"observer", "job", "collectors", "stdout", "exit", "exit_before_observer_abort"}
EVIDENCE_PHASES = {
    "not_started",
    "open_process",
    "job_membership",
    "process_times",
    "birth_limit",
    "image_query",
    "image_admission",
    "duplicate_birth",
    "admitted_birth",
    "captured_birth",
    "capture_publication",
    "notification",
    "startup",
    "finish",
}
TEST_MARKERS = {"admitted", "version", "pass", "revision", "gstreamer", "notice"}
EVIDENCE_COLLECTORS = {
    "process_capture_stopped",
    "process_admission_stopped",
    "process_handles_released",
    "temp_stopped",
    "temp_handles_released",
    "temp_drain_complete",
    "stdout_stopped",
    "stderr_stopped",
}


def evidence_need(ok):
    if not ok:
        raise ValueError("invalid facade failure evidence")


def evidence_fields(value, names):
    evidence_need(type(value) is dict and value.keys() == names)


def evidence_integer(value, maximum, *, nullable=False, minimum=0):
    evidence_need(nullable and value is None or type(value) is int and minimum <= value <= maximum)


def validate_exit_evidence(value):
    evidence_fields(
        value,
        {
            "stderr_stopped",
            "stderr_read_failed",
            "gate_state",
            "child_exit_code",
            "relay_exit_code",
        },
    )
    evidence_need(value["stderr_stopped"] is None or type(value["stderr_stopped"]) is bool)
    evidence_need(value["stderr_read_failed"] is None or type(value["stderr_read_failed"]) is bool)
    gate = value["gate_state"]
    evidence_need(gate is None or type(gate) is str and gate in common.GATE_STATES)
    for key in ("child_exit_code", "relay_exit_code"):
        evidence_integer(value[key], 2**32 - 1, nullable=True, minimum=-(2**31))
    if value["stderr_stopped"] is not True:
        evidence_need(
            all(
                value[key] is None
                for key in ("stderr_read_failed", "gate_state", "child_exit_code")
            )
        )


def validate_failure_evidence(value):
    evidence_fields(value, EVIDENCE_PARTS | {"schema_version", "mode", "snapshot_state"})
    evidence_need(
        type(value["schema_version"]) is str
        and value["schema_version"] == "installed-alpha-facade-evidence-v2"
    )
    evidence_need(type(value["mode"]) is str and value["mode"] in MODES)
    evidence_need(
        type(value["snapshot_state"]) is str
        and value["snapshot_state"] in {"quiescent", "not_quiescent", "unavailable"}
    )
    if value["snapshot_state"] != "quiescent":
        evidence_need(all(value[key] is None for key in EVIDENCE_PARTS))
        return
    observer = value["observer"]
    if observer is not None:
        evidence_fields(
            observer,
            {
                "capture_ordinal",
                "admission_ordinal",
                "failure_ordinal",
                "captured_count",
                "pending_count",
                "admitted_count",
                "validating_count",
                "failure_actor",
                "capture_error",
                "admission_error",
                "notifications_capped",
                "duplicates_capped",
                "capture_phase",
                "admission_phase",
                "failure_phase",
                "error",
                "primary_error",
                "cleanup_error",
                "admitted_classes",
                "admitted_counts",
            },
        )
        for key in ("capture_ordinal", "admission_ordinal", "failure_ordinal"):
            evidence_integer(observer[key], MAX_PROCESSES + 1)
        for key in ("captured_count", "pending_count", "admitted_count"):
            evidence_integer(observer[key], MAX_PROCESSES)
        evidence_integer(observer["validating_count"], 1)
        evidence_need(observer["validating_count"] == 0)
        evidence_need(
            type(observer["failure_actor"]) is str
            and observer["failure_actor"] in {"none", "capture", "admission", "lifecycle"}
        )
        for key in ("notifications_capped", "duplicates_capped"):
            evidence_integer(observer[key], MAX_EVENTS)
        for key in ("capture_phase", "admission_phase", "failure_phase"):
            evidence_need(type(observer[key]) is str and observer[key] in EVIDENCE_PHASES)
        for key in ("error", "primary_error", "cleanup_error", "capture_error", "admission_error"):
            evidence_need(type(observer[key]) is str and observer[key] in boundary.ERRORS)
        classes = observer["admitted_classes"]
        evidence_need(type(classes) is list and len(classes) <= MAX_PROCESSES)
        evidence_need(observer["admitted_count"] == len(classes))
        evidence_need(
            observer["captured_count"] == observer["pending_count"] + observer["admitted_count"]
        )
        evidence_need(
            all(type(kind) is str and kind in allowed_images(value["mode"]) for kind in classes)
        )
        evidence_fields(observer["admitted_counts"], PROCESS_KINDS)
        for kind in PROCESS_KINDS:
            count = observer["admitted_counts"][kind]
            evidence_integer(count, MAX_PROCESSES)
            evidence_need(count == classes.count(kind))
    job = value["job"]
    if job is not None:
        evidence_fields(
            job,
            {
                "total",
                "active_before_close",
                "aborted",
                "observer_abort_requested",
                "closed",
                "resources_closed",
            },
        )
        for key in ("total", "active_before_close"):
            evidence_integer(job[key], 2**32 - 1, nullable=True)
        for key in ("aborted", "observer_abort_requested", "closed", "resources_closed"):
            evidence_need(type(job[key]) is bool)
    evidence_fields(value["collectors"], EVIDENCE_COLLECTORS)
    for key, item in value["collectors"].items():
        evidence_need(item is None or type(item) is bool)
        if key.endswith("_stopped"):
            evidence_need(item is None or item is True)
    stdout = value["stdout"]
    if stdout is not None:
        evidence_fields(
            stdout,
            {
                "invalid",
                "start_diagnostic_invalid",
                "health_confirmed",
                "operator_request_observed",
                "returned",
                "refused",
                "failed",
                "test_counts",
                "run_markers",
                "start_milestones",
                "run_counters",
            },
        )
        for key in (
            "invalid",
            "start_diagnostic_invalid",
            "health_confirmed",
            "operator_request_observed",
        ):
            evidence_need(type(stdout[key]) is bool)
        for key in ("returned", "refused", "failed"):
            evidence_integer(stdout[key], 65536)
        for key, names, maximum in (
            ("test_counts", TEST_MARKERS, 65536),
            ("run_markers", alpha.MARKERS, 65536),
            ("start_milestones", alpha.START_MILESTONES.keys(), 255),
            ("run_counters", alpha.COUNTERS, 9_999_999),
        ):
            evidence_fields(stdout[key], names)
            for item in stdout[key].values():
                evidence_integer(item, maximum, nullable=key == "run_counters")
    for key in ("exit", "exit_before_observer_abort"):
        if value[key] is not None:
            validate_exit_evidence(value[key])
    if value["exit"] is not None:
        evidence_need(value["exit"]["stderr_stopped"] is value["collectors"]["stderr_stopped"])
    evidence_need(len(common.canonical(value)) <= 8192)


def stopped(collector):
    return None if collector is None else not collector.thread.is_alive()


def exit_evidence(owned, relay_exit_code):
    """Cached facts only; an unavailable collector never means no child failure."""
    summary = getattr(owned, "stderr_summary", None)
    quiet = stopped(summary)
    return {
        "stderr_stopped": quiet,
        "stderr_read_failed": summary.read_failed if quiet else None,
        "gate_state": summary.gate_state if quiet else None,
        "child_exit_code": summary.child_exit_code if quiet else None,
        "relay_exit_code": relay_exit_code,
    }


def failure_evidence(observation, mode):
    value = {
        "schema_version": "installed-alpha-facade-evidence-v2",
        "mode": mode,
        "snapshot_state": "not_quiescent",
        **dict.fromkeys(EVIDENCE_PARTS),
    }
    # Never walk a live collector's mutable maps or stream state. They never restart.
    if not observation.quiescent():
        return value
    value["snapshot_state"] = "quiescent"
    job = observation.jobs[0] if len(observation.jobs) == 1 else None
    observer = getattr(job, "observer", None)
    watcher, stdout, owned = observation.watcher, observation.stdout_summary, observation.owned
    if observer is not None:
        classes = list(observer.births.values())
        value["observer"] = {
            "capture_ordinal": observer.capture_ordinal,
            "admission_ordinal": observer.admission_ordinal,
            "failure_ordinal": observer.failure_ordinal,
            "captured_count": len(observer.reservations),
            "pending_count": len(observer.reservations) - len(observer.births),
            "admitted_count": len(observer.births),
            "validating_count": int(observer.validating is not None),
            "failure_actor": observer.failure_actor,
            "capture_error": observer.capture_error,
            "admission_error": observer.admission_error,
            "notifications_capped": observer.notification_count,
            "duplicates_capped": observer.duplicate_count,
            "capture_phase": observer.capture_phase,
            "admission_phase": observer.admission_phase,
            "failure_phase": observer.failure_phase,
            "error": observer.error,
            "primary_error": observer.primary_error,
            "cleanup_error": observer.cleanup_error,
            "admitted_classes": classes,
            "admitted_counts": {kind: classes.count(kind) for kind in PROCESS_KINDS},
        }
    if job is not None:
        value["job"] = {
            "total": job.final_total if job.accounting_observed else None,
            "active_before_close": job.final_active if job.accounting_observed else None,
            "aborted": job.aborted,
            "observer_abort_requested": job.observer_abort_requested,
            "closed": not job.observation_open,
            "resources_closed": job.resources_closed,
        }
    value["collectors"] = {
        "process_capture_stopped": stopped(observer),
        "process_admission_stopped": (
            None if observer is None else not observer.admission_thread.is_alive()
        ),
        "process_handles_released": getattr(observer, "release_complete", None),
        "temp_stopped": stopped(watcher),
        "temp_handles_released": getattr(watcher, "closed", None),
        "temp_drain_complete": getattr(watcher, "drain_complete", None),
        "stdout_stopped": stopped(stdout),
        "stderr_stopped": stopped(getattr(owned, "stderr_summary", None)),
    }
    if stdout is not None:
        value["stdout"] = {
            **{
                key: getattr(stdout, key)
                for key in (
                    "invalid",
                    "start_diagnostic_invalid",
                    "health_confirmed",
                    "operator_request_observed",
                    "returned",
                    "refused",
                    "failed",
                )
            },
            "test_counts": dict(stdout.test_counts),
            "run_markers": dict(stdout.counts),
            "start_milestones": dict(stdout.start_milestones),
            "run_counters": {key: stdout.scalars.get(key) for key in alpha.COUNTERS},
        }
    value["exit"] = exit_evidence(owned, observation.relay_exit_code)
    value["exit_before_observer_abort"] = observation.exit_before_observer_abort
    validate_failure_evidence(value)
    return value


def emit_failure_evidence(observation, mode):
    # Diagnostic construction/output must not mask primary or cleanup failures.
    try:
        value = failure_evidence(observation, mode)
        validate_failure_evidence(value)
    except BaseException:
        value = {
            "schema_version": "installed-alpha-facade-evidence-v2",
            "mode": mode,
            "snapshot_state": "unavailable",
            **dict.fromkeys(EVIDENCE_PARTS),
        }
    try:
        validate_failure_evidence(value)
        print("K5_FACADE_EVIDENCE=" + common.canonical(value).decode("ascii"))
    except BaseException:
        pass


def allowed_images(mode: str) -> set[str]:
    need(type(mode) is str and mode in MODES)
    if mode == "test_invalid":
        return BASE_IMAGES
    if mode == "test_valid":
        return BASE_IMAGES | {"gst_launch"}
    return BASE_IMAGES | RUN_IMAGES


def validate_expectations(value: object, *, installed=True) -> dict[str, str]:
    names = IDENTITIES if installed else INPUT_IDENTITIES
    common.require(type(value) is dict and value.keys() == names | {"revision", "run_nonce"})
    for name, size in [("revision", 40), ("run_nonce", 32), *[(name, 64) for name in names]]:
        common.require(
            type(value[name]) is str
            and re.fullmatch(r"[0-9a-f]{" + str(size) + "}", value[name]) is not None
            and value[name] != "0" * size
        )
    return value


def alpha_expectations(value: dict[str, str]) -> dict[str, str]:
    return {key: item for key, item in value.items() if key != "facade_controller_sha256"}


def validate_observation(value: object, mode: str) -> None:
    need(type(value) is dict and value.keys() == OBS_FIELDS)
    for key, exact in {
        "schema_version": "owned-facade-observation-v1",
        "mode": mode,
        "error": "none",
    }.items():
        need(type(value[key]) is str and value[key] == exact)
    allowed = allowed_images(mode)
    for key in OBS_COUNTS:
        need(type(value[key]) is int and 0 <= value[key] <= MAX_EVENTS)
    for key in OBS_BOOLEANS:
        need(value[key] is True, "incomplete")
    total = value["job_total"]
    need(0 < total <= MAX_PROCESSES and value["distinct_births"] == total, "incomplete")
    need(value["job_active"] == 0, "cleanup_incomplete")
    need(sum(value["process_" + kind] for kind in PROCESS_KINDS) == total, "incomplete")
    need(
        all(value["process_" + kind] == 0 for kind in PROCESS_KINDS - allowed), "unexpected_process"
    )
    need(all(value["process_" + kind] > 0 for kind in BASE_IMAGES - {"console_host"}), "incomplete")
    if mode == "test_valid":
        need(value["process_gst_launch"] > 0, "incomplete")
    if mode.startswith("run_"):
        need(
            all(value["process_" + kind] > 0 for kind in RUN_IMAGES - {"gst_scanner"}), "incomplete"
        )
    need(value["temp_events"] == sum(value[key] for key in TEMP_COUNTS), "incomplete")
    need(
        all(value["temp_other_owned_temp_" + action] == 0 for action in boundary.ACTIONS.values()),
        "unexpected_temp",
    )
    if mode.startswith("test_"):
        need(
            all(value["temp_alpha_session_" + action] == 0 for action in boundary.ACTIONS.values()),
            "unexpected_temp",
        )
    else:
        need(
            value["temp_alpha_session_added"] > 0 and value["temp_alpha_session_removed"] > 0,
            "incomplete",
        )
    policy = [value["temp_policy_probe_" + action] for action in ("added", "modified", "removed")]
    ps1, psm1 = value["policy_ps1_files"], value["policy_psm1_files"]
    need((ps1, psm1, ps1 + psm1, sum(policy)) in boundary.POLICY_PROFILES, "policy_contract")
    need(policy == [ps1 + psm1] * 3, "policy_lifecycle")
    need(
        value["temp_policy_probe_renamed_from"] == value["temp_policy_probe_renamed_to"] == 0,
        "policy_lifecycle",
    )


def new_receipt(expected):
    return {
        "schema_version": SCHEMA,
        "acceptance_scope": SCOPE,
        "fixture": "generated-ball",
        "analytics_revision": common.ANALYTICS_REVISION,
        **expected,
        **dict.fromkeys(FLAGS, False),
    }


def validate_receipt(value: object, expected: dict[str, str]) -> None:
    validate_expectations(expected)
    common.require(type(value) is dict and value.keys() == FIELDS, "receipt_invalid")
    for key, exact in {
        "schema_version": SCHEMA,
        "acceptance_scope": SCOPE,
        "fixture": "generated-ball",
        "analytics_revision": common.ANALYTICS_REVISION,
        **expected,
    }.items():
        common.require(type(value[key]) is str and value[key] == exact, "receipt_invalid")
    for key in FLAGS:
        common.require(
            value[key] is (key not in {"person_box_acceptance", "installer_acceptance"}),
            "receipt_invalid",
        )
    for mode in MODES:
        validate_observation(value[mode + "_observation"], mode)
    for attempt in (1, 2):
        alpha.validate_run(
            {name: value[f"run_{attempt}_{name}"] for name in alpha.COUNTERS | alpha.RUN_BOOLEANS}
        )


class FacadeProcessObserver:
    """One prompt capture actor and one ordered, per-birth hash admission actor.

    Every reserved handle stays in one registry until both actors stop using it.
    The queue is private, bounded by the unchanged distinct-birth ceiling, and
    never locked across hashing. Faster capture is not a native timing guarantee.
    """

    def __init__(self, job, admitted, resources):
        from ctypes import wintypes as w

        class Association(ctypes.Structure):
            _fields_ = [("key", ctypes.c_void_p), ("port", w.HANDLE)]

        self._initialize_evidence()
        self.job, self.admitted, self.api = job, admitted, boundary.api()
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.admission_thread = threading.Thread(target=self._admit, daemon=True)
        # Both lifetimes are visible before allocating/starting anything. Shared
        # quiescence sees the admission actor too, including constructor failure.
        # The second entry exposes lifetime only; this observer owns all errors.
        resources.extend([self, SimpleNamespace(thread=self.admission_thread, error="none")])
        job.observer = self
        try:
            duplicate = w.HANDLE()
            current = self.api.GetCurrentProcess()
            boundary.native_need(
                self.api.DuplicateHandle(
                    current, job.handle, current, ctypes.byref(duplicate), 0, False, 2
                )
            )
            self.job_query_handle = duplicate.value
            self.port = self.api.CreateIoCompletionPort(ctypes.c_void_p(-1), None, 0, 1)
            boundary.native_need(self.port)
            association = Association(1, self.port)
            boundary.native_need(
                job.api.SetInformationJobObject(
                    job.handle, 7, ctypes.byref(association), ctypes.sizeof(association)
                )
            )
            # Startup gate prevents either actor working before both starts pass.
            self.admission_thread.start()
            self.thread.start()
            self.begin.set()
        except BaseException as error:
            self._record_failure("lifecycle", error, phase="startup")
            self.stop.set()
            self.begin.set()
            with self.condition:
                self.condition.notify_all()
            for actor, done in (
                (self.thread, self.capture_done),
                (self.admission_thread, self.admission_done),
            ):
                if actor.ident is None:
                    done.set()
            self._join_actors(time.monotonic() + 3)
            self._release()
            raise

    def _initialize_evidence(self):
        self.notification_count = self.duplicate_count = 0
        self.capture_ordinal = self.admission_ordinal = self.failure_ordinal = 0
        self.capture_phase = self.admission_phase = self.failure_phase = "not_started"
        self.capture_error = self.admission_error = "none"
        self.failure_actor = "none"
        self.error = self.primary_error = self.cleanup_error = "none"
        self.condition = threading.Condition()
        self.resource_lock = threading.Lock()
        self.stop, self.begin = threading.Event(), threading.Event()
        self.capture_done, self.admission_done = threading.Event(), threading.Event()
        self.births, self.reservations, self.pending = {}, {}, deque()
        self.validating = None
        self.handles = []
        self.port = self.job_query_handle = None
        self.closed = self.release_complete = self.handle_release_failed = False

    @staticmethod
    def _error_code(error):
        return str(error) if isinstance(error, boundary.ObservationFailure) else "native_error"

    def _record_failure(self, actor, error, *, phase=None):
        """Publish terminal state before abort, without holding locks on abort."""
        code = self._error_code(error)
        with self.condition:
            if actor in {"capture", "admission"}:
                if getattr(self, actor + "_error") == "none":
                    setattr(self, actor + "_error", code)
            if self.primary_error == "none":
                self.primary_error = code
                self.failure_actor = actor
                self.failure_phase = phase or getattr(self, actor + "_phase")
                self.failure_ordinal = (
                    getattr(self, actor + "_ordinal") if actor != "lifecycle" else 0
                )
            if self.error == "none":
                self.error = code
            self.stop.set()
            self.condition.notify_all()

    def _cleanup_failed(self, *, handle=False):
        with self.condition:
            self.handle_release_failed |= handle
            self.cleanup_error = "cleanup_incomplete"
            self.error = "cleanup_incomplete"
            self.stop.set()
            self.condition.notify_all()

    def _fail(self, actor, error):
        self._record_failure(actor, error)
        try:
            self.job.abort()
        except BaseException:
            self._cleanup_failed()
        self._wake()

    def _wake(self):
        # A late wake cannot touch a released completion port.
        with self.resource_lock:
            if self.port and not self.closed:
                try:
                    if not self.api.PostQueuedCompletionStatus(self.port, 0, 2, None):
                        self._cleanup_failed()
                except BaseException:
                    self._cleanup_failed()
        with self.condition:
            self.condition.notify_all()

    def _observe(self, pid):
        with self.condition:
            self.notification_count = min(MAX_EVENTS, self.notification_count + 1)
            self.capture_ordinal = min(MAX_PROCESSES + 1, len(self.reservations) + 1)
        try:
            self._observe_birth(pid)
        except BaseException as error:
            self._record_failure("capture", error)
            raise

    def _observe_birth(self, pid):
        from ctypes import wintypes as w

        self.capture_phase = "open_process"
        handle = self.api.OpenProcess(0x1000, False, pid)
        boundary.native_need(handle, missing=True)
        try:
            self.capture_phase = "job_membership"
            belongs = w.BOOL()
            boundary.native_need(
                self.api.IsProcessInJob(handle, self.job_query_handle, ctypes.byref(belongs))
            )
            need(bool(belongs.value), "ownership_unproven")
            self.capture_phase = "process_times"
            created, exited, kernel, user = (ctypes.c_uint64() for _ in range(4))
            boundary.native_need(
                self.api.GetProcessTimes(
                    handle,
                    ctypes.byref(created),
                    ctypes.byref(exited),
                    ctypes.byref(kernel),
                    ctypes.byref(user),
                )
            )
            need(created.value > 0, "native_error")
            key = (pid, created.value)
            with self.condition:
                if key in self.reservations:
                    self.duplicate_count = min(MAX_EVENTS, self.duplicate_count + 1)
                    self.capture_phase = "duplicate_birth"
                    return
                self.capture_phase = "birth_limit"
                need(len(self.reservations) < MAX_PROCESSES, "limit")
            self.capture_phase = "image_query"
            buffer, count = ctypes.create_unicode_buffer(32768), w.DWORD(32768)
            boundary.native_need(
                self.api.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(count))
            )
            self.capture_phase = "capture_publication"
            record = (key, buffer.value, self.capture_ordinal)
            with self.condition:
                if self.stop.is_set():
                    return
                # Transfer ownership to the sole registry before publishing work.
                # If queue insertion fails, that registry still owns this handle.
                self.handles.append(handle)
                handle = None
                self.reservations[key] = record
                self.pending.append(record)
                self.capture_phase = "captured_birth"
                self.condition.notify_all()
        except BaseException as error:
            # Capture primary before transient CloseHandle can fail and mask it.
            self._record_failure("capture", error)
            raise
        finally:
            if handle is not None:
                try:
                    need(bool(self.api.CloseHandle(handle)), "cleanup_incomplete")
                except BaseException:
                    self._cleanup_failed(handle=True)
                    raise

    def _admit_one(self, record):
        key, image, ordinal = record
        self.admission_ordinal, self.admission_phase = ordinal, "image_admission"
        # Unchanged raw per-birth hash. No state/resource lock spans this call.
        kind = boundary.process_class(image, self.admitted)
        need(kind in self.admitted and kind != "unknown", "unexpected_process")
        with self.condition:
            if not self.stop.is_set():
                self.births[key] = kind
                self.admission_phase = "admitted_birth"
            self.condition.notify_all()

    def _admit(self):
        self.begin.wait()
        try:
            while True:
                with self.condition:
                    while not self.pending and not self.stop.is_set():
                        if self.capture_done.is_set():
                            return
                        self.condition.wait()
                    if self.stop.is_set():
                        return
                    record = self.pending.popleft()
                    self.validating = record
                try:
                    self._admit_one(record)
                finally:
                    with self.condition:
                        self.validating = None
                        self.condition.notify_all()
        except BaseException as error:
            self._fail("admission", error)
        finally:
            self.admission_done.set()
            with self.condition:
                self.condition.notify_all()
            self._release()

    def _read(self):
        from ctypes import wintypes as w

        self.begin.wait()
        try:
            while not self.stop.is_set():
                self.capture_phase, self.capture_ordinal = "notification", 0
                message, key, value = w.DWORD(), ctypes.c_size_t(), ctypes.c_void_p()
                ready = self.api.GetQueuedCompletionStatus(
                    self.port, ctypes.byref(message), ctypes.byref(key), ctypes.byref(value), 100
                )
                if not ready:
                    if ctypes.get_last_error() == 258:
                        continue
                    boundary.native_need(False)
                if key.value == 2:
                    return
                need(key.value == 1, "native_error")
                if message.value == 6:
                    need(value.value is not None and 0 < value.value < 2**32, "native_error")
                    self._observe(value.value)
        except BaseException as error:
            self._fail("capture", error)
        finally:
            self.capture_done.set()
            with self.condition:
                self.condition.notify_all()
            self._release()

    def _release(self):
        # Done means no further native access by that actor. The last actor may
        # release in its own finalizer; external quiescence still requires joins.
        with self.resource_lock:
            if self.closed or not (self.capture_done.is_set() and self.admission_done.is_set()):
                return
            complete = not self.handle_release_failed
            for handle in [*self.handles, self.port, self.job_query_handle]:
                if handle is None:
                    continue
                try:
                    complete = bool(self.api.CloseHandle(handle)) and complete
                except BaseException:
                    complete = False
            self.handles.clear()
            self.closed, self.release_complete = True, complete
            if not complete:
                self._cleanup_failed()

    def _join_actors(self, deadline):
        for actor in (self.thread, self.admission_thread):
            if actor.ident is not None:
                actor.join(max(0, deadline - time.monotonic()))
        if self.thread.is_alive() or self.admission_thread.is_alive():
            self._cleanup_failed()
            raise boundary.ObservationFailure("cleanup_incomplete")

    def reconciled(self, expected_total):
        return (
            self.error == "none"
            and 0 < expected_total <= MAX_PROCESSES
            and len(self.reservations) == len(self.births) == expected_total
            and not self.pending
            and self.validating is None
        )

    def cancel(self):
        self._record_failure("lifecycle", boundary.ObservationFailure("incomplete"), phase="finish")
        self._wake()

    def finish(self, expected_total):
        # Preserve the original 2s collection + 3s join budgets, shared by both.
        deadline = time.monotonic() + 2
        with self.condition:
            while not self.reconciled(expected_total) and self.error == "none":
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self.capture_done.is_set() and self.admission_done.is_set():
                    break
                self.condition.wait(remaining)
            if self.error == "none" and not self.reconciled(expected_total):
                self._record_failure(
                    "lifecycle", boundary.ObservationFailure("incomplete"), phase="finish"
                )
            self.stop.set()
            self.condition.notify_all()
        self._wake()
        self._join_actors(time.monotonic() + 3)
        self._release()
        need(self.release_complete and self.cleanup_error == "none", "cleanup_incomplete")


class FacadeTempLifecycle:
    """Observe every event; only Run can create the known owned session subtree.

    Policy lifecycle remains the separately qualified finite profile. Session
    events are bounded observations, not writer attribution or media semantics.
    """

    def __init__(self, mode):
        self.mode = mode
        self.policy = boundary.PolicyLifecycle()
        self.extensions = self.policy.extensions
        self.invalid = False

    def observe(self, name, action):
        try:
            need(not self.invalid, "unexpected_temp")
            kind = boundary.classify_temp(name)
            need(
                kind == "policy_probe" or self.mode.startswith("run_") and kind == "alpha_session",
                "unexpected_temp",
            )
            if kind == "policy_probe":
                self.policy.observe(name, action)
        except BaseException:
            self.invalid = True
            raise

    def complete(self):
        return not self.invalid and self.policy.complete()


class FacadeTempObserver(boundary.TempObserver):
    def __init__(self, root, abort, resources, mode):
        self.mode = mode
        super().__init__(root, abort, resources)

    def _read(self):
        # Install the per-instance lifecycle before this reader consumes events.
        # Native I/O, cancellation-race draining and resource ownership are reused.
        self.lifecycle = FacadeTempLifecycle(self.mode)
        super()._read()


class FacadeObservation(boundary.PreflightObservation):
    def __init__(self, *, temp_root, admitted_images, mode):
        super().__init__(common, temp_root=temp_root, admitted_images=admitted_images)
        self.mode = mode
        self.collectors = []
        self.owned = self.relay_exit_code = self.stdout_summary = None
        self.exit_before_observer_abort = None

    def quiescent(self):
        return super().quiescent() and all(
            not collector.thread.is_alive() for collector in self.collectors
        )

    def start(self):
        need(not self.started)
        self.temp_root = common.local_path(self.temp_root, directory=True)
        need(not any(self.temp_root.iterdir()))
        need(type(self.admitted) is dict and self.admitted.keys() == allowed_images(self.mode))
        for path, identity in self.admitted.values():
            need(common.local_path(path) == path)
            need(type(identity) is str and re.fullmatch(r"[0-9a-f]{64}", identity) is not None)
            need(boundary.file_hash(path) == identity)
        need(
            len({os.path.normcase(str(path)) for path, _ in self.admitted.values()})
            == len(self.admitted)
        )
        self.watcher = FacadeTempObserver(self.temp_root, self.abort, self.resources, self.mode)
        self.started = True

    def job_factory(self):
        need(self.started and not self.finished and not self.jobs)
        observation, original_job = self, self.base_job

        class ObservedJob(original_job):
            def __init__(self):
                super().__init__()
                self.abort_lock = threading.Lock()
                self.aborted = False
                self.observation_open = True
                self.resources_closed = False
                self.observer = None
                self.final_total = self.final_active = 0
                self.accounting_observed = self.observer_abort_requested = False
                observation.jobs.append(self)
                try:
                    self.observer = FacadeProcessObserver(
                        self, observation.admitted, observation.resources
                    )
                    need(observation.watcher.error == "none", observation.watcher.error)
                except BaseException:
                    self.close()
                    raise

            def abort(self):
                with self.abort_lock:
                    self.aborted = True
                    if self.observer is not None and threading.current_thread() in (
                        self.observer.thread,
                        self.observer.admission_thread,
                    ):
                        self.observer_abort_requested = True
                        # Only cached exit facts; no new query, wait, or process lookup.
                        # Failure to collect diagnostics must never prevent the abort.
                        try:
                            observation.exit_before_observer_abort = exit_evidence(
                                observation.owned,
                                getattr(
                                    getattr(observation.owned, "process", None), "returncode", None
                                ),
                            )
                        except BaseException:
                            observation.exit_before_observer_abort = None
                    if self.observation_open:
                        self.api.TerminateJobObject(self.handle, 1)

            def assign(self, process):
                boundary.guarded_assignment(
                    self,
                    process,
                    lambda: (observation.watcher.error, self.observer.error),
                    lambda child: original_job.assign(self, child),
                )

            def close(self):
                if not self.observation_open:
                    need(self.resources_closed, "cleanup_incomplete")
                    return
                failure = None
                try:
                    # Preserve pre-termination active count; killing a survivor
                    # cannot convert an incomplete facade into successful evidence.
                    state = self.accounting()
                    self.final_total, self.final_active = (
                        state.total_processes,
                        state.active_processes,
                    )
                    self.accounting_observed = True
                    boundary.native_need(self.api.TerminateJobObject(self.handle, 1))
                    deadline = time.monotonic() + 5
                    while self.accounting().active_processes:
                        need(time.monotonic() < deadline, "cleanup_incomplete")
                        time.sleep(0.01)
                except BaseException as error:
                    failure = error
                finally:
                    # Job accounting/termination failure must not strand either
                    # actor. Stop independently, then spend the single observer
                    # finish/join budget even when the main close already failed.
                    if self.observer is not None:
                        try:
                            if failure is not None:
                                self.observer.cancel()
                            self.observer.finish(self.final_total)
                        except BaseException as error:
                            if failure is None:
                                failure = error
                    try:
                        with self.abort_lock:
                            self.observation_open = False
                            super().close()
                    except BaseException as error:
                        if failure is None:
                            failure = error
                    self.resources_closed = failure is None
                if failure is not None:
                    raise boundary.ObservationFailure("cleanup_incomplete") from None

        return ObservedJob()

    def finish(self):
        need(self.started and not self.finished)
        need(len(self.jobs) == 1 and not self.jobs[0].observation_open, "incomplete")
        self.watcher.finish()
        need(self.quiescent(), "cleanup_incomplete")
        self.finished = True
        watcher, job = self.watcher, self.jobs[0]
        births = list(job.observer.births.values())
        record = {
            "schema_version": "owned-facade-observation-v1",
            "mode": self.mode,
            "error": watcher.error if watcher.error != "none" else job.observer.error,
            "job_total": job.final_total,
            "job_active": job.final_active,
            "distinct_births": len(births),
            "temp_events": watcher.total,
            **{"process_" + kind: births.count(kind) for kind in PROCESS_KINDS},
            **watcher.counts,
            **{
                "policy_" + key + "_files": count
                for key, count in watcher.lifecycle.extensions.items()
            },
            "process_coverage_complete": job.observer.reconciled(job.final_total)
            and job.final_active == 0
            and "unknown" not in births,
            "temporary_root_empty": not any(self.temp_root.iterdir()),
            "temp_drain_complete": watcher.drain_complete,
            "policy_lifecycles_complete": watcher.lifecycle.complete(),
            "cleanup_complete": watcher.closed and job.resources_closed and self.quiescent(),
        }
        for path, identity in self.admitted.values():
            need(common.local_path(path) == path and boundary.file_hash(path) == identity)
        validate_observation(record, self.mode)
        return record


ENVELOPE = r"""param([string]$Facade, [string]$InstallRoot, [string]$Mode, [int]$Port)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
try {
    if ($Mode -ceq 'test_invalid' -or $Mode -ceq 'test_valid') {
        & $Facade -InstallRoot $InstallRoot
    } elseif ($Mode -ceq 'run_1' -or $Mode -ceq 'run_2') {
        & $Facade -InstallRoot $InstallRoot -Port $Port -ExitAfterPublicTest
    } else { throw 'refused' }
    if (-not $?) { throw 'refused' }
    Write-Output 'K5_FACADE_RETURNED'
    exit 0
} catch {
    if ($Mode -ceq 'test_invalid' -and
        $_.Exception.Message -ceq 'K5 analytics preflight failed. No alpha session was started.') {
        Write-Output 'K5_FACADE_EXPECTED_REFUSAL'
        exit 23
    }
    Write-Output 'K5_FACADE_FAILED'
    exit 24
}
"""


def facade_command(powershell, envelope, installed, mode, port):
    allowed_images(mode)
    common.require(type(port) is int and 1024 <= port <= 65535, "admission_failed")
    facade = "Test" if mode.startswith("test_") else "Run"
    return [
        str(powershell),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(envelope),
        "-Facade",
        str(installed / (facade + "-K5VisionAlpha.ps1")),
        "-InstallRoot",
        str(installed),
        "-Mode",
        mode,
        "-Port",
        str(port),
    ]


class FacadeSummary(alpha.LaunchSummary):
    def __init__(self, stream, mode, revision):
        self.mode, self.revision = mode, revision
        self.returned = self.refused = self.failed = 0
        self.test_counts = dict.fromkeys(
            ("admitted", "version", "pass", "revision", "gstreamer", "notice"), 0
        )
        super().__init__(stream)

    def _line(self, line):
        line = line.rstrip(b"\r")
        for marker, key in (
            (b"K5_FACADE_RETURNED", "returned"),
            (b"K5_FACADE_EXPECTED_REFUSAL", "refused"),
            (b"K5_FACADE_FAILED", "failed"),
        ):
            if line == marker:
                setattr(self, key, getattr(self, key) + 1)
                return
        if self.mode.startswith("run_"):
            super()._line(line)
            return
        # Strict full Test stdout grammar; unknown lines are not ignored. The
        # no-camera text is checked as output, never used to infer no media.
        lines = {
            "admitted": (
                b"K5 analytics configuration admitted; live provider acceptance is pending."
            ),
            "version": b"k5-vision 0.1.0",
            "pass": b"K5 Vision Alpha preflight PASS",
            "revision": b"Reviewed K5 revision: " + self.revision.encode("ascii"),
            "gstreamer": b"Reviewed GStreamer: " + alpha.GSTREAMER_VERSION.encode("ascii"),
            "notice": (
                b"No camera was contacted and no camera media was read "
                b"or written by this preflight."
            ),
        }
        for key, exact in lines.items():
            if line == exact:
                self.test_counts[key] += 1
                return
        self.invalid = True

    def result(self):
        common.require(not self.invalid and self.failed == 0, "output_invalid")
        common.require(
            self.returned == int(self.mode != "test_invalid")
            and self.refused == int(self.mode == "test_invalid"),
            "receipt_invalid",
        )
        if self.mode.startswith("test_"):
            common.require(
                all(count == int(self.mode == "test_valid") for count in self.test_counts.values()),
                "receipt_invalid",
            )
            return {}
        return super().result(stage="launch_1" if self.mode == "run_1" else "launch_2")


def admitted_images(env, installed, powershell, mode):
    result = alpha.admitted_preflight_images(env, installed, powershell)
    gst, mtx = alpha.native_roots(Path(env["LOCALAPPDATA"]))
    extra = {
        "gst_launch": gst / "bin/gst-launch-1.0.exe",
        "gst_inspect": gst / "bin/gst-inspect-1.0.exe",
        "gst_scanner": gst / "libexec/gstreamer-1.0/gst-plugin-scanner.exe",
        "mediamtx": mtx / "mediamtx.exe",
    }
    for kind in allowed_images(mode) - BASE_IMAGES:
        path = common.local_path(extra[kind])
        result[kind] = (path, common.file_hash(path))
    return result


class FacadeOwnedProcess(common.OwnedProcess):
    """Retain stderr collector even when base construction/assignment fails."""

    def __init__(self, *args, observation, **kwargs):
        self.facade_observation = observation
        observation.owned = self
        try:
            super().__init__(*args, **kwargs)
        finally:
            collector = getattr(self, "stderr_summary", None)
            if collector is not None:
                observation.collectors.append(collector)

    def close(self):
        # Copy only the cached scalar after the original wait/kill/close. Do not
        # retain Popen or extend its Windows process-handle lifetime for diagnostics.
        process = getattr(self, "process", None)
        try:
            super().close()
        finally:
            if process is not None:
                self.facade_observation.relay_exit_code = process.returncode


def invoke_facade(command, *, work, env, mode, expected, images, observations):
    observation = FacadeObservation(temp_root=Path(env["TEMP"]), admitted_images=images, mode=mode)
    observations.append(observation)  # Even partial setup must block unsafe cleanup.
    owned = summary = original = result = record = None
    try:
        observation.start()
        owned = FacadeOwnedProcess(
            command,
            observation=observation,
            cwd=work,
            env=env,
            operation="probe_admission"
            if mode.startswith("test_")
            else "launch_1"
            if mode == "run_1"
            else "launch_2",
            stdout=subprocess.PIPE,
            job_factory=observation.job_factory,
        )
        summary = FacadeSummary(owned.process.stdout, mode, expected["revision"])
        observation.collectors.append(summary)
        observation.stdout_summary = summary
        try:
            owned.wait(60 if mode.startswith("test_") else 150)
        except common.WitnessError as error:
            detail = error.diagnostic or {}
            if not (
                mode == "test_invalid"
                and str(error) == "child_failed"
                and detail.get("gate_state") == "exited"
                and detail.get("child_exit_code") == detail.get("relay_exit_code") == 23
                and detail.get("timed_out") is False
            ):
                raise
        summary.finish()
        expected_exit = 23 if mode == "test_invalid" else 0
        common.require(
            owned.process.returncode == expected_exit
            and owned.stderr_summary.gate_state == "exited"
            and owned.stderr_summary.child_exit_code == expected_exit,
            "child_failed",
        )
        common.require(alpha.job_accounting(owned)[1] == 0, "cleanup_incomplete")
        result = summary.result()
    except BaseException as error:
        original = error
        raise
    finally:
        cleanup_errors = []
        if owned is not None:
            try:
                common.close_after_failure(owned, original)
            except BaseException as error:
                cleanup_errors.append(error)
        if summary is not None:
            try:
                summary.finish()
            except BaseException as error:
                cleanup_errors.append(error)
        try:
            if (
                original is None
                and observation.started
                and len(observation.jobs) == 1
                and not observation.jobs[0].observation_open
            ):
                record = observation.finish()
        except BaseException as error:
            cleanup_errors.append(error)
        finally:
            try:
                observation.close()
            except BaseException as error:
                cleanup_errors.append(error)
        if (
            original is not None
            or cleanup_errors
            or any(resource.error != "none" for resource in observation.resources)
        ):
            emit_failure_evidence(observation, mode)
        for resource in observation.resources:
            if resource.error != "none":
                emit_failure(mode, boundary.ObservationFailure(resource.error))
        if cleanup_errors:
            if original is not None:
                emit_failure(mode, original)
            for cleanup_error in cleanup_errors:
                emit_failure("cleanup", cleanup_error)
            raise common.WitnessError("cleanup_incomplete") from None
    validate_observation(record, mode)
    return result, record


def bind_controller(args, work, expected):
    """Raw Git export binding before materialization, never checkout normalization."""
    archive, source = work / "facade-binding.zip", work / "facade-binding"
    env = alpha.clean_environment(dict(os.environ), work)
    for key in ("TEMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        Path(env[key]).mkdir(parents=True, exist_ok=True)
    command = common.archive_command(args.repo, archive, expected["revision"])
    command[0] = str(common.local_path(args.git.absolute()))
    common.run(command, cwd=work, env=env, operation="archive_candidate")
    common.extract_archive(archive, source)
    common.require(common.digest(alpha.tree_manifest(source)) == expected["source_tree_sha256"])
    exported = common.local_path(source / "scripts" / Path(__file__).name)
    current = common.local_path(Path(__file__).absolute())
    common.require(
        common.file_hash(exported)
        == expected["facade_controller_sha256"]
        == common.file_hash(current)
    )
    common.require(exported.read_bytes() == current.read_bytes())
    for name in (
        "installed_alpha_launcher_witness.py",
        "installed_analytics_witness.py",
        "windows_owned_preflight.py",
    ):
        common.require(
            (source / "scripts" / name).read_bytes() == Path(__file__).with_name(name).read_bytes()
        )
    # The accepted prepare() independently repeats its own helper checks.
    shutil.rmtree(source)
    archive.unlink()


def generated_authorities(args, work, installed):
    """Reconstruct probe authority from admitted raw source, never from probe output."""
    source = work / "source"
    root = common.local_path(args.evidence_root.absolute(), directory=True)
    versions = {
        **common.requirements(source / "scripts/windows-alpha/runtime-requirements.txt"),
        **common.RUNTIME_VERSIONS,
        **common.WINDOWS_RUNTIME_VERSIONS,
    }
    files = {
        work / "installed_analytics_witness.py": Path(__file__)
        .with_name("installed_analytics_witness.py")
        .read_bytes(),
        work / "probe-inputs.json": common.canonical(
            {
                "k5_payload": alpha.source_payload(source),
                "runtime_versions": versions,
                "evidence_root": str(root),
            }
        ),
        work / "analytics.json": common.canonical(
            {
                "schema_version": 1,
                "provider": "analytics-lab-omz-person-v1",
                "source_revision": common.ANALYTICS_REVISION,
                "artifact_root": str(root / "artifacts"),
            }
        ),
    }
    command = [
        str(installed / ".venv/Scripts/python.exe"),
        "-I",
        "-B",
        str(work / "installed_analytics_witness.py"),
        "--probe",
        "--inputs",
        str(work / "probe-inputs.json"),
        "--output",
        str(work / "probe-output.json"),
    ]
    return files, command


def verify_generated(files, command, expected_command, env, work):
    common.require(type(command) is list and command == expected_command)
    common.require(env.get("K5_ANALYTICS_CONFIG") == str(work / "analytics.json"))
    for path, content in files.items():
        checked = common.local_path(path)
        common.require(checked.stat().st_size == len(content))
        with checked.open("rb") as stream:
            common.require(stream.read(len(content) + 1) == content)


def verify_inputs(args, installed, env, expected):
    common.require(
        common.digest(alpha.tree_manifest(installed.parent / "source"))
        == expected["source_tree_sha256"]
    )
    alpha.verify_layout(installed, alpha_expectations(expected))
    common.require(
        common.file_hash(common.local_path(args.k5_wheel.absolute())) == expected["k5_wheel_sha256"]
    )
    common.require(
        common.digest(alpha.tree_manifest(args.wheelhouse.absolute(), maximum_files=256))
        == expected["wheelhouse_sha256"]
    )
    common.require(
        common.digest(alpha.native_manifest(Path(env["LOCALAPPDATA"])))
        == expected["native_cache_sha256"]
    )
    common.require(
        common.digest(alpha.native_manifest(args.local_appdata.absolute()))
        == expected["native_cache_sha256"]
    )
    common.require(
        common.file_hash(common.local_path(Path(__file__).absolute()))
        == expected["facade_controller_sha256"]
    )


def facade_sequence(
    *,
    args,
    work,
    installed,
    env,
    powershell,
    expected,
    document,
    observations,
    probe_command,
    state,
    authorities,
    expected_probe_command,
):
    envelope = work / "invoke-facade.ps1"
    envelope.write_text(ENVELOPE, encoding="ascii", newline="\n")
    invalid = work / "invalid-facade-analytics.json"
    invalid_bytes = b'{"schema_version":1,"provider":"invalid-selected-provider"}'
    invalid.write_bytes(invalid_bytes)
    phase_authorities = {**authorities, invalid: invalid_bytes, envelope: ENVELOPE.encode("ascii")}
    port = common.free_port()
    for mode in ("test_invalid", "test_valid", "run_1", "run_2"):
        state["stage"] = mode
        verify_generated(phase_authorities, probe_command, expected_probe_command, env, work)
        verify_inputs(args, installed, env, expected)
        alpha.probe(probe_command, work, env, alpha_expectations(expected), after=True)
        verify_generated(phase_authorities, probe_command, expected_probe_command, env, work)
        alpha.require_ports_free(port)
        selected_env = (
            {**env, "K5_ANALYTICS_CONFIG": str(invalid)} if mode == "test_invalid" else env
        )
        result, record = invoke_facade(
            facade_command(powershell, envelope, installed, mode, port),
            work=work,
            env=selected_env,
            mode=mode,
            expected=expected,
            images=admitted_images(env, installed, powershell, mode),
            observations=observations,
        )
        verify_generated(phase_authorities, probe_command, expected_probe_command, env, work)
        document[mode + "_observation"] = record
        if mode == "test_invalid":
            document.update(
                test_invalid_refused=True, test_invalid_no_session=True, test_invalid_no_media=True
            )
        elif mode == "test_valid":
            document.update(test_valid_completed=True, test_valid_no_session=True)
        else:
            document.update({mode + "_" + key: value for key, value in result.items()})
        verify_inputs(args, installed, env, expected)
        alpha.probe(probe_command, work, env, alpha_expectations(expected), after=True)
        verify_generated(phase_authorities, probe_command, expected_probe_command, env, work)
        alpha.require_ports_free(port)


def emit_failure(stage, error):
    code = str(error) if isinstance(error, common.WitnessError) else "unexpected"
    if isinstance(error, boundary.ObservationFailure):
        code = "receipt_invalid"
    common.require(type(stage) is str and stage in STAGES)
    common.require(code in common.FAILURES)
    print(
        "K5_FACADE_FAILURE="
        + common.canonical(
            {
                "schema_version": "installed-alpha-facade-failure-v1",
                "stage": stage,
                "failure_code": code,
                "observation_error": str(error)
                if isinstance(error, boundary.ObservationFailure)
                else "none",
            }
        ).decode("ascii")
    )


def execute(args):
    output = alpha.clear_output(args.output, RECEIPT_NAME)
    admitted = alpha.clear_output(args.admitted_expectations, EXPECTATIONS_NAME)
    expected = validate_expectations(common.read_json(args.expectations), installed=False)
    document, state = new_receipt(expected), {"stage": "admission"}
    work, error, observations = None, None, []
    try:
        alpha.admit_platform()
        repo = common.local_path(args.repo.absolute(), directory=True)
        work = common.adopt_work_root(args.work_root, args.temp_root, repo, output)
        common.require(not admitted.is_relative_to(work), "admission_failed")
        bind_controller(args, work, expected)
        installed, env, command = alpha.prepare(args, work, alpha_expectations(expected), state)
        state["stage"] = "probe"
        verify_inputs(args, installed, env, expected)
        authorities, expected_command = generated_authorities(args, work, installed)
        verify_generated(authorities, command, expected_command, env, work)
        checked = alpha.probe(command, work, env, alpha_expectations(expected))
        expected = {**expected, "runtime_identity_sha256": checked["runtime_identity_sha256"]}
        document.update(checked)
        validate_expectations(expected)
        with admitted.open("xb") as stream:
            stream.write(common.canonical(expected) + b"\n")
        alpha.verify_native(Path(env["LOCALAPPDATA"]), work, env)
        powershell = common.local_path(
            Path(env["SYSTEMROOT"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        )
        facade_sequence(
            args=args,
            work=work,
            installed=installed,
            env=env,
            powershell=powershell,
            expected=expected,
            document=document,
            observations=observations,
            probe_command=command,
            state=state,
            authorities=authorities,
            expected_probe_command=expected_command,
        )
        state["stage"] = "verify"
        verify_inputs(args, installed, env, expected)
        bind_controller(args, work, expected)
        document["completed"] = True
    except BaseException as caught:
        error = caught
    finally:
        if work is not None:
            try:
                common.require(
                    all(
                        observation.quiescent()
                        and all(job.resources_closed for job in observation.jobs)
                        for observation in observations
                    ),
                    "cleanup_incomplete",
                )
                shutil.rmtree(work)
                common.require(not work.exists(), "cleanup_incomplete")
            except BaseException:
                if error is not None:
                    emit_failure(state["stage"], error)
                state["stage"], error = "cleanup", common.WitnessError("cleanup_incomplete")
        document["cleanup_complete"] = error is None
    if error is not None:
        emit_failure(state["stage"], error)
        return 1
    validate_receipt(document, expected)
    with output.open("xb") as stream:
        stream.write(common.canonical(document) + b"\n")
    print("Installed Alpha facade witness passed")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate-receipt", action="store_true")
    parser.add_argument("--expectations", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--admitted-expectations", type=Path)
    inputs = (
        "repo",
        "analytics-source",
        "k5-wheel",
        "wheelhouse",
        "evidence-root",
        "local-appdata",
        "git",
        "temp-root",
        "work-root",
    )
    for name in inputs:
        parser.add_argument("--" + name, type=Path)
    args = parser.parse_args()
    try:
        if args.validate_receipt:
            common.require(args.output.name == RECEIPT_NAME, "receipt_invalid")
            validate_receipt(common.read_json(args.output), common.read_json(args.expectations))
            print("Installed Alpha facade receipt validation passed")
            return 0
        alpha.clear_output(args.output, RECEIPT_NAME)
        common.require(
            args.admitted_expectations is not None
            and all(getattr(args, name.replace("-", "_")) is not None for name in inputs),
            "admission_failed",
        )
        return execute(args)
    except BaseException as error:
        emit_failure("admission", error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
