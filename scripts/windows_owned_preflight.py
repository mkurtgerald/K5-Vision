"""Owned Windows preflight initialization observations, never media acceptance.

The policy-probe-shaped profile must be independently qualified by the hosted
Windows test before any installed witness uses it. Every owned process birth and
TEMP event must reconcile. Names/PIDs remain private and are never logged; these
notifications do not identify the files' writers.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import struct
import threading
import time
from pathlib import Path

MAX_PROCESSES = 32
MAX_EVENTS = 256
PROCESS_KINDS = {
    "base_python",
    "venv_python",
    "powershell",
    "console_host",
    "dotnet_compiler",
    "unknown",
}
TEMP_KINDS = {"policy_probe", "alpha_session", "other_owned_temp"}
ACTIONS = {1: "added", 2: "removed", 3: "modified", 4: "renamed_from", 5: "renamed_to"}
ERRORS = {
    "none",
    "access_denied",
    "process_unavailable",
    "ownership_unproven",
    "native_error",
    "limit",
    "incomplete",
    "fixture_admission",
    "launcher_rejected",
    "cleanup_incomplete",
    "unexpected_process",
    "unexpected_temp",
    "policy_lifecycle",
    "policy_contract",
}


class ObservationFailure(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code if code in ERRORS else "native_error")


def need(condition: bool, code="fixture_admission") -> None:
    if not condition:
        raise ObservationFailure(code)


def classify_temp(name: str) -> str:
    parts = name.replace("/", "\\").split("\\")
    if len(parts) == 1 and re.fullmatch(
        r"__PSScriptPolicyTest_[a-z0-9]{8}\.[a-z0-9]{3}\.(?:ps1|psm1)", name
    ):
        return "policy_probe"
    if parts and re.fullmatch(r"K5VisionAlpha-[0-9a-fA-F]{32}", parts[0]):
        return "alpha_session"
    return "other_owned_temp"


def _notification_events(data: bytes) -> list[tuple[str, str]]:
    """Decode bounded owned-directory names for private lifecycle checking only."""
    need(0 < len(data) <= 65_536, "limit")
    offset, result = 0, []
    while True:
        need(offset + 12 <= len(data), "native_error")
        following, action, length = struct.unpack_from("<III", data, offset)
        need(action in ACTIONS and length % 2 == 0 and 0 < length <= 4096, "native_error")
        end = offset + 12 + length
        need(end <= len(data), "native_error")
        try:
            name = data[offset + 12 : end].decode("utf-16-le", errors="strict")
        except UnicodeError:
            raise ObservationFailure("native_error") from None
        result.append((name, ACTIONS[action]))
        need(len(result) <= MAX_EVENTS, "limit")
        if following == 0:
            return result
        need(following >= 12 + length and following % 4 == 0, "native_error")
        offset += following


EXPECTED_PROCESSES = {
    "base_python": 2,
    "powershell": 1,
    "venv_python": 1,
    "console_host": 1,
    "dotnet_compiler": 0,
    "unknown": 0,
}


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def notification_categories(data: bytes) -> list[tuple[str, str]]:
    """Source-free public projection; full names never leave lifecycle checking."""
    return [(classify_temp(name), action) for name, action in _notification_events(data)]


class PolicyLifecycle:
    """Exact four-file observed initialization profile, with no ignored events.

    Microsoft's application-control documentation defines top-level random 8.3
    .ps1/.psm1 policy probes. GetAppLockerPolicy writes/deletes paired files.
    The qualification profile requires two pairs, one add/write/delete each. Its
    notification cardinality is intentionally fail-closed, not an OS-wide claim.
    The lowercase alphanumeric 8.3 alphabet is the approved documented shape,
    not an assertion of the exact .NET random-name generator.
    Names only establish a documented shape; the watcher does not identify writers.
    https://learn.microsoft.com/en-us/powershell/scripting/security/app-control/application-control
    https://github.com/PowerShell/PowerShell/blob/master/src/System.Management.Automation/security/wldpNativeMethods.cs
    """

    def __init__(self):
        self.states = {}
        self.extensions = {"ps1": 0, "psm1": 0}
        self.events = 0
        self.invalid = False

    def observe(self, name: str, action: str) -> None:
        try:
            need(not self.invalid, "policy_lifecycle")
            self._observe(name, action)
        except BaseException:
            self.invalid = True
            raise

    def _observe(self, name: str, action: str) -> None:
        need(type(name) is str and classify_temp(name) == "policy_probe", "unexpected_temp")
        need(type(action) is str and action in {"added", "modified", "removed"}, "policy_lifecycle")
        stage = self.states.get(name)
        expected = {None: "added", "added": "modified", "modified": "removed"}
        need(action == expected.get(stage), "policy_lifecycle")
        if action == "added":
            extension = name.rsplit(".", 1)[1]
            need(len(self.states) < 4 and self.extensions[extension] < 2, "policy_contract")
            self.extensions[extension] += 1
        self.states[name] = action
        self.events += 1
        need(self.events <= 12, "policy_contract")

    def complete(self) -> bool:
        return (
            not self.invalid
            and self.extensions == {"ps1": 2, "psm1": 2}
            and self.events == 12
            and len(self.states) == 4
            and all(v == "removed" for v in self.states.values())
        )


def validate_process_contract(counts, *, total, active, covered):
    need(type(counts) is dict and counts.keys() == EXPECTED_PROCESSES.keys(), "unexpected_process")
    need(all(type(v) is int for v in counts.values()), "unexpected_process")
    need(all(type(v) is int for v in (total, active, covered)), "unexpected_process")
    need(
        total == covered == 5 and active == 0 and counts == EXPECTED_PROCESSES, "unexpected_process"
    )


SUMMARY_COUNTS = (
    {
        "job_total",
        "job_active",
        "distinct_births",
        "temp_events",
        "policy_ps1_files",
        "policy_psm1_files",
    }
    | {"process_" + key for key in PROCESS_KINDS}
    | {"temp_" + kind + "_" + action for kind in TEMP_KINDS for action in ACTIONS.values()}
)
SUMMARY_BOOLEANS = {
    "process_coverage_complete",
    "cleanup_complete",
    "temporary_root_empty",
    "policy_lifecycles_complete",
    "temp_drain_complete",
}
SUMMARY_FIELDS = (
    SUMMARY_COUNTS
    | SUMMARY_BOOLEANS
    | {"schema_version", "error"}
    | {f"birth_{index}_class" for index in range(1, MAX_PROCESSES + 1)}
)


def empty_summary():
    return {
        "schema_version": "owned-preflight-initialization-v1",
        "error": "none",
        **dict.fromkeys(SUMMARY_COUNTS, 0),
        **dict.fromkeys(SUMMARY_BOOLEANS, False),
        **{f"birth_{index}_class": None for index in range(1, MAX_PROCESSES + 1)},
    }


def validate_summary(value):
    need(type(value) is dict and value.keys() == SUMMARY_FIELDS)
    need(
        type(value["schema_version"]) is str
        and value["schema_version"] == "owned-preflight-initialization-v1"
    )
    need(type(value["error"]) is str and value["error"] in ERRORS)
    for key in SUMMARY_BOOLEANS:
        need(type(value[key]) is bool)
    for key in SUMMARY_COUNTS:
        need(type(value[key]) is int and 0 <= value[key] <= MAX_EVENTS)
    for index in range(1, MAX_PROCESSES + 1):
        kind = value[f"birth_{index}_class"]
        need(kind is None or type(kind) is str and kind in PROCESS_KINDS)
    total = value["distinct_births"]
    need(total <= MAX_PROCESSES)
    kinds = [value[f"birth_{index}_class"] for index in range(1, total + 1)]
    need(all(kind is not None for kind in kinds))
    need(all(value[f"birth_{i}_class"] is None for i in range(total + 1, MAX_PROCESSES + 1)))
    need(all(value["process_" + kind] == kinds.count(kind) for kind in PROCESS_KINDS))
    if value["process_coverage_complete"]:
        need(
            0 < total == value["job_total"] <= MAX_PROCESSES
            and value["job_active"] == 0
            and value["process_unknown"] == 0
        )
    if value["policy_lifecycles_complete"]:
        need(
            value["policy_ps1_files"] == value["policy_psm1_files"] == 2
            and value["temp_events"] == 12
        )
        need(
            all(
                value["temp_policy_probe_" + action]
                == (4 if action in {"added", "modified", "removed"} else 0)
                for action in ACTIONS.values()
            )
        )
        need(
            all(
                value["temp_" + kind + "_" + action] == 0
                for kind in TEMP_KINDS - {"policy_probe"}
                for action in ACTIONS.values()
            )
        )
    need(len(json.dumps(value, separators=(",", ":"))) <= 8192)


def validate_invalid_initialization(value):
    validate_summary(value)
    need(value["error"] == "none" and all(value[key] for key in SUMMARY_BOOLEANS), "incomplete")
    validate_process_contract(
        {kind: value["process_" + kind] for kind in PROCESS_KINDS},
        total=value["job_total"],
        active=value["job_active"],
        covered=value["distinct_births"],
    )


def api():
    from ctypes import wintypes as w

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "GetCurrentProcess": ([], w.HANDLE),
        "DuplicateHandle": (
            [w.HANDLE, w.HANDLE, w.HANDLE, ctypes.POINTER(w.HANDLE), w.DWORD, w.BOOL, w.DWORD],
            w.BOOL,
        ),
        "CreateIoCompletionPort": ([w.HANDLE, w.HANDLE, ctypes.c_size_t, w.DWORD], w.HANDLE),
        "GetQueuedCompletionStatus": (
            [
                w.HANDLE,
                ctypes.POINTER(w.DWORD),
                ctypes.POINTER(ctypes.c_size_t),
                ctypes.POINTER(ctypes.c_void_p),
                w.DWORD,
            ],
            w.BOOL,
        ),
        "PostQueuedCompletionStatus": (
            [w.HANDLE, w.DWORD, ctypes.c_size_t, ctypes.c_void_p],
            w.BOOL,
        ),
        "OpenProcess": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
        "IsProcessInJob": ([w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL)], w.BOOL),
        "QueryFullProcessImageNameW": (
            [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)],
            w.BOOL,
        ),
        "GetProcessTimes": (
            [w.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p],
            w.BOOL,
        ),
        "CloseHandle": ([w.HANDLE], w.BOOL),
        "CreateFileW": (
            [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE],
            w.HANDLE,
        ),
        "CreateEventW": ([ctypes.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR], w.HANDLE),
        "ResetEvent": ([w.HANDLE], w.BOOL),
        "WaitForSingleObject": ([w.HANDLE, w.DWORD], w.DWORD),
        "ReadDirectoryChangesW": (
            [
                w.HANDLE,
                ctypes.c_void_p,
                w.DWORD,
                w.BOOL,
                w.DWORD,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_void_p,
            ],
            w.BOOL,
        ),
        "GetOverlappedResult": (
            [w.HANDLE, ctypes.c_void_p, ctypes.POINTER(w.DWORD), w.BOOL],
            w.BOOL,
        ),
        "CancelIoEx": ([w.HANDLE, ctypes.c_void_p], w.BOOL),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = arguments, result
    return kernel


def native_need(ok, *, missing=False):
    if not ok:
        code = ctypes.get_last_error()
        raise ObservationFailure(
            "access_denied"
            if code == 5
            else "process_unavailable"
            if missing and code == 87
            else "native_error"
        )


def process_class(path: str, admitted: dict[str, tuple[Path, str]]) -> str:
    for kind, (expected, identity) in admitted.items():
        if os.path.normcase(path) == os.path.normcase(str(expected)):
            try:
                need(file_hash(expected) == identity)
            except PermissionError:
                raise ObservationFailure("access_denied") from None
            return kind
    return "unknown"


class ProcessObserver:
    """Completion-port notifications from one owned Job, never global PID scans."""

    def __init__(self, job, admitted, resources):
        from ctypes import wintypes as w

        class Association(ctypes.Structure):
            _fields_ = [("key", ctypes.c_void_p), ("port", w.HANDLE)]

        self.job, self.admitted, self.api = job, admitted, api()
        self.resource_lock = threading.Lock()
        self.closed = False
        self.release_complete = False
        duplicate = w.HANDLE()
        current = self.api.GetCurrentProcess()
        native_need(
            self.api.DuplicateHandle(
                current, job.handle, current, ctypes.byref(duplicate), 0, False, 2
            )
        )
        self.job_query_handle = duplicate.value
        self.port = self.api.CreateIoCompletionPort(ctypes.c_void_p(-1), None, 0, 1)
        if not self.port:
            self.api.CloseHandle(self.job_query_handle)
            native_need(False)
        self.births = {}
        self.handles = []
        self.error = "none"
        self.stop = threading.Event()
        association = Association(1, self.port)
        try:
            native_need(
                job.api.SetInformationJobObject(
                    job.handle, 7, ctypes.byref(association), ctypes.sizeof(association)
                )
            )
            self.thread = threading.Thread(target=self._read, daemon=True)
            resources.append(self)
            self.thread.start()
        except BaseException:
            self.api.CloseHandle(self.port)
            self.api.CloseHandle(self.job_query_handle)
            self.closed = True
            self.release_complete = True
            raise

    def _observe(self, pid):
        from ctypes import wintypes as w

        handle = self.api.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION only.
        native_need(handle, missing=True)
        try:
            belongs = w.BOOL()
            native_need(
                self.api.IsProcessInJob(handle, self.job_query_handle, ctypes.byref(belongs))
            )
            need(bool(belongs.value), "ownership_unproven")
            # Only after exact Job ownership is confirmed may image/birth be read.
            created, exited, kernel, user = (ctypes.c_uint64() for _ in range(4))
            native_need(
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
            if key in self.births:
                return
            need(len(self.births) < MAX_PROCESSES, "limit")
            buffer, count = ctypes.create_unicode_buffer(32768), w.DWORD(32768)
            native_need(self.api.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(count)))
            self.births[key] = process_class(buffer.value, self.admitted)
            self.handles.append(handle)
            handle = None  # Hold the birth's handle until observation cleanup.
            counts = {kind: list(self.births.values()).count(kind) for kind in PROCESS_KINDS}
            need(
                all(counts[kind] <= EXPECTED_PROCESSES[kind] for kind in PROCESS_KINDS),
                "unexpected_process",
            )
        finally:
            if handle is not None:
                need(bool(self.api.CloseHandle(handle)), "cleanup_incomplete")

    def _read(self):
        from ctypes import wintypes as w

        try:
            while not self.stop.is_set():
                message, key, value = w.DWORD(), ctypes.c_size_t(), ctypes.c_void_p()
                ready = self.api.GetQueuedCompletionStatus(
                    self.port, ctypes.byref(message), ctypes.byref(key), ctypes.byref(value), 100
                )
                if not ready:
                    if ctypes.get_last_error() == 258:
                        continue
                    native_need(False)
                if key.value == 2:
                    return
                need(key.value == 1, "native_error")
                if message.value == 6:  # JOB_OBJECT_MSG_NEW_PROCESS.
                    need(value.value is not None and 0 < value.value < 2**32, "native_error")
                    self._observe(value.value)
        except BaseException as error:
            self.error = str(error) if isinstance(error, ObservationFailure) else "native_error"
            # Stop the owned execution on denial or incomplete/unsafe observation.
            self.job.abort()
        finally:
            self._release()

    def _release(self):
        # Only the exited reader (or a caller after joining it) releases these.
        # A separate Job handle stays valid even if the main Job owner times out.
        with self.resource_lock:
            if self.closed:
                return
            complete = True
            for handle in [*self.handles, self.port, self.job_query_handle]:
                complete = bool(self.api.CloseHandle(handle)) and complete
            self.handles.clear()
            self.closed = True
            self.release_complete = complete
            if not complete and self.error == "none":
                self.error = "cleanup_incomplete"

    def finish(self, expected_total):
        deadline = time.monotonic() + 2
        while (
            len(self.births) < expected_total
            and self.error == "none"
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        self.stop.set()
        with self.resource_lock:
            if not self.closed:
                self.api.PostQueuedCompletionStatus(self.port, 0, 2, None)
        self.thread.join(3)
        need(not self.thread.is_alive(), "cleanup_incomplete")
        self._release()
        need(self.release_complete, "cleanup_incomplete")


class TempObserver:
    """Overlapped notifications restricted to an owned TEMP directory tree."""

    def __init__(self, root: Path, abort, resources):
        from ctypes import wintypes as w

        class Overlapped(ctypes.Structure):
            _fields_ = [
                ("internal", ctypes.c_size_t),
                ("internal_high", ctypes.c_size_t),
                ("offset", w.DWORD),
                ("offset_high", w.DWORD),
                ("event", w.HANDLE),
            ]

        self.api, self.error = api(), "none"
        self.abort = abort
        self.io_lock = threading.Lock()
        self.closed = False
        self.counts = dict.fromkeys(
            ("temp_" + kind + "_" + action for kind in TEMP_KINDS for action in ACTIONS.values()), 0
        )
        self.total = 0
        self.lifecycle = PolicyLifecycle()
        self.drain_requested, self.drained = threading.Event(), threading.Event()
        self.drain_complete = False
        self.stop = threading.Event()
        self.handle = self.api.CreateFileW(str(root), 1, 7, None, 3, 0x42000000, None)
        native_need(self.handle not in (None, ctypes.c_void_p(-1).value))
        self.event = self.api.CreateEventW(None, True, False, None)
        if not self.event:
            self.api.CloseHandle(self.handle)
            native_need(False)
        self.overlapped = Overlapped()
        self.overlapped.event = self.event
        self.buffer = ctypes.create_string_buffer(65_536)
        self.begin = threading.Event()
        self.thread = threading.Thread(target=self._read, daemon=True)
        resources.append(self)
        try:
            # Start the waiter before issuing I/O, so failure to create a thread
            # cannot leave a native request referencing an abandoned buffer.
            self.thread.start()
            self._issue()
            self.begin.set()
        except BaseException:
            self.stop.set()
            self.begin.set()
            if self.thread.is_alive():
                self.thread.join(3)
            self.api.CloseHandle(self.event)
            self.api.CloseHandle(self.handle)
            raise

    def _issue(self):
        with self.io_lock:
            if self.stop.is_set():
                return False
            native_need(self.api.ResetEvent(self.event))
            native_need(
                self.api.ReadDirectoryChangesW(
                    self.handle,
                    self.buffer,
                    len(self.buffer),
                    True,
                    0x1B,
                    None,
                    ctypes.byref(self.overlapped),
                    None,
                )
            )
            return True

    def _read(self):
        from ctypes import wintypes as w

        try:
            need(self.begin.wait(3), "incomplete")
            if self.stop.is_set():
                return
            drain_cancel_issued = False
            while True:
                status = self.api.WaitForSingleObject(self.event, 100)
                if status == 258:
                    if self.drain_requested.is_set():
                        with self.io_lock:
                            cancelled = self.api.CancelIoEx(
                                self.handle, ctypes.byref(self.overlapped)
                            )
                            if cancelled:
                                drain_cancel_issued = True
                            else:
                                # Already completed is a race, never evidence of quiet.
                                native_need(ctypes.get_last_error() == 1168)
                    continue
                need(status == 0, "native_error")
                count = w.DWORD()
                success = self.api.GetOverlappedResult(
                    self.handle, ctypes.byref(self.overlapped), ctypes.byref(count), False
                )
                if not success and ctypes.get_last_error() == 995:
                    if (
                        self.drain_requested.is_set()
                        and drain_cancel_issued
                        and self.error == "none"
                        and not self.stop.is_set()
                    ):
                        # Only an aborted fresh empty request certifies the drain.
                        self.drain_complete = True
                        self.stop.set()
                        return
                    if self.stop.is_set():
                        return  # Emergency cleanup cannot certify the drain.
                native_need(success)
                for name, action in _notification_events(self.buffer.raw[: count.value]):
                    kind = classify_temp(name)
                    need(self.total < MAX_EVENTS, "limit")
                    self.total += 1
                    self.counts["temp_" + kind + "_" + action] += 1
                    self.lifecycle.observe(name, action)
                # A successful completion racing cancellation may have more changes
                # buffered behind it. Always reissue until an empty request aborts.
                if not self._issue():
                    return
                drain_cancel_issued = False
        except BaseException as error:
            self.error = str(error) if isinstance(error, ObservationFailure) else "native_error"
            self.abort()
        finally:
            self.drained.set()  # Unblock cleanup on a terminal observer error.

    def finish(self):
        # ReadDirectoryChangesW buffers changes between calls on this handle.
        # After Job quiescence, the reader cancels a pending empty request; every
        # successful cancellation-racing batch is consumed and followed by reissue.
        # https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-readdirectorychangesw
        drained_in_time = True
        if self.error == "none":
            self.drain_requested.set()
            drained_in_time = self.drained.wait(3)
            if not drained_in_time:
                self.error = "incomplete"
                self.abort()
        with self.io_lock:
            self.stop.set()
            if not self.drain_complete:
                # Bounded error cleanup only. This cannot make drain_complete true.
                cancelled = self.api.CancelIoEx(self.handle, ctypes.byref(self.overlapped))
                if not cancelled:
                    need(ctypes.get_last_error() == 1168, "cleanup_incomplete")
        self.thread.join(3)
        need(not self.thread.is_alive(), "cleanup_incomplete")
        closed = bool(self.api.CloseHandle(self.event))
        closed = bool(self.api.CloseHandle(self.handle)) and closed
        self.closed = closed
        need(closed and drained_in_time, "cleanup_incomplete")


def process_record(record, job):
    record["job_total"], record["job_active"] = job.final_total, job.final_active
    record["distinct_births"] = len(job.observer.births)
    for index, (_, kind) in enumerate(
        sorted(job.observer.births.items(), key=lambda item: item[0][1]), 1
    ):
        record["process_" + kind] += 1
        record[f"birth_{index}_class"] = kind
    record["process_coverage_complete"] = (
        job.observer.error == "none"
        and 0 < job.final_total <= MAX_PROCESSES
        and len(job.observer.births) == job.final_total
        and job.final_active == 0
        and record["process_unknown"] == 0
    )
    if job.observer.error != "none":
        record["error"] = job.observer.error


def observers_quiescent(resources):
    return all(not observer.thread.is_alive() for observer in resources)


def guarded_assignment(job, process, errors, assign):
    """A recorded observer abort is terminal, including before Job assignment."""
    with job.abort_lock:
        current = errors()
        if job.aborted or any(error != "none" for error in current):
            job.aborted = True
            raise ObservationFailure("incomplete")
        assign(process)
        # Assignment can wake the observer. Refuse gate opening if it detected a
        # denial while assignment itself was in progress, before returning.
        if any(error != "none" for error in errors()):
            job.aborted = True
            job.api.TerminateJobObject(job.handle, 1)
            raise ObservationFailure("incomplete")


class PreflightObservation:
    """Explicit one-Job factory; no global patching or process-wide observation."""

    def __init__(self, common, *, temp_root: Path, admitted_images):
        self.common, self.temp_root = common, temp_root
        self.admitted = admitted_images
        self.resources, self.jobs = [], []
        self.watcher = None
        self.started = self.finished = False
        self.summary = empty_summary()
        self.base_job = common.WindowsJob

    def start(self):
        need(not self.started)
        self.temp_root = self.common.local_path(self.temp_root, directory=True)
        need(not any(self.temp_root.iterdir()))
        need(
            type(self.admitted) is dict
            and self.admitted.keys() == {k for k, v in EXPECTED_PROCESSES.items() if v}
        )
        for path, identity in self.admitted.values():
            need(self.common.local_path(path) == path)
            need(type(identity) is str and re.fullmatch(r"[0-9a-f]{64}", identity) is not None)
            need(file_hash(path) == identity)
        need(len({os.path.normcase(str(path)) for path, _ in self.admitted.values()}) == 4)
        self.watcher = TempObserver(self.temp_root, self.abort, self.resources)
        self.started = True

    def abort(self):
        for job in self.jobs:
            job.abort()

    def job_factory(self):
        need(self.started and not self.finished and not self.jobs)
        observation = self
        original_job = self.base_job

        class ObservedJob(original_job):
            def __init__(self):
                super().__init__()
                self.abort_lock = threading.Lock()
                self.aborted = False
                self.observation_open = True
                self.resources_closed = False
                self.observer = None
                self.final_total = self.final_active = 0
                observation.jobs.append(self)
                try:
                    self.observer = ProcessObserver(
                        self, observation.admitted, observation.resources
                    )
                    need(observation.watcher.error == "none", observation.watcher.error)
                except BaseException:
                    self.close()
                    raise

            def abort(self):
                with self.abort_lock:
                    self.aborted = True
                    if self.observation_open:
                        self.api.TerminateJobObject(self.handle, 1)

            def assign(self, process):
                guarded_assignment(
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
                    # Capture survivors before forced cleanup; termination cannot turn a
                    # non-clean refusal into successful initialization evidence.
                    state = self.accounting()
                    self.final_total, self.final_active = (
                        state.total_processes,
                        state.active_processes,
                    )
                    native_need(self.api.TerminateJobObject(self.handle, 1))
                    deadline = time.monotonic() + 5
                    while self.accounting().active_processes:
                        need(time.monotonic() < deadline, "cleanup_incomplete")
                        time.sleep(0.01)
                    if self.observer is not None:
                        self.observer.finish(self.final_total)
                except BaseException as error:
                    failure = error
                finally:
                    with self.abort_lock:
                        self.observation_open = False
                        super().close()
                    self.resources_closed = failure is None
                if failure is not None:
                    raise ObservationFailure("cleanup_incomplete") from None

        return ObservedJob()

    def finish(self):
        need(self.started and not self.finished)
        need(len(self.jobs) == 1 and not self.jobs[0].observation_open, "incomplete")
        self.watcher.finish()
        self.finished = True
        self.summary.update(self.watcher.counts)
        self.summary["temp_events"] = self.watcher.total
        self.summary["temporary_root_empty"] = not any(self.temp_root.iterdir())
        self.summary["temp_drain_complete"] = self.watcher.drain_complete
        self.summary["policy_lifecycles_complete"] = self.watcher.lifecycle.complete()
        for extension, count in self.watcher.lifecycle.extensions.items():
            self.summary["policy_" + extension + "_files"] = count
        process_record(self.summary, self.jobs[0])
        if self.watcher.error != "none":
            self.summary["error"] = self.watcher.error
        self.summary["cleanup_complete"] = self.watcher.closed and all(
            job.resources_closed for job in self.jobs
        )
        for path, identity in self.admitted.values():
            need(self.common.local_path(path) == path and file_hash(path) == identity)
        validate_summary(self.summary)
        return dict(self.summary)

    def close(self):
        failures = []
        for job in self.jobs:
            if job.observation_open:
                try:
                    job.close()
                except BaseException:
                    failures.append(True)
        if self.watcher is not None and not self.watcher.closed:
            try:
                self.watcher.finish()
            except BaseException:
                failures.append(True)
        need(not failures and self.quiescent(), "cleanup_incomplete")

    def quiescent(self):
        return observers_quiescent(self.resources)
