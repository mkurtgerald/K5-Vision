"""Hosted-only observation of the unchanged Start preflight boundary.

This copies the already prepared clean runtime probe. It is a negative-boundary
reproduction, never installed-media acceptance. No witness gate is changed here.
"""

from __future__ import annotations

import ctypes
import importlib.util
import os
import re
import shutil
import struct
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "_alpha_preflight_observed", ROOT / "scripts/installed_alpha_launcher_witness.py"
)
assert SPEC is not None and SPEC.loader is not None
alpha = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(alpha)
common = alpha.common
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
}
COUNT_FIELDS = (
    {"job_total", "job_active", "distinct_births", "temp_events"}
    | {"process_" + key for key in PROCESS_KINDS}
    | {"temp_" + kind + "_" + action for kind in TEMP_KINDS for action in ACTIONS.values()}
)
BOOL_FIELDS = {
    "runtime_verified",
    "start_verified",
    "process_coverage_complete",
    "cleanup_complete",
    "temporary_root_empty",
    "old_gate_rejected",
}
FIELDS = (
    COUNT_FIELDS
    | BOOL_FIELDS
    | {"schema_version", "error", "old_contract"}
    | {f"birth_{index}_class" for index in range(1, MAX_PROCESSES + 1)}
)


class ObservationFailure(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code if code in ERRORS else "native_error")


def need(condition: bool, code="fixture_admission") -> None:
    if not condition:
        raise ObservationFailure(code)


def classify_temp(name: str) -> str:
    parts = name.replace("/", "\\").split("\\")
    if len(parts) == 1 and re.fullmatch(
        r"__PSScriptPolicyTest_[A-Za-z0-9._-]{1,80}\.(?:ps1|psm1)", name
    ):
        return "policy_probe"
    if parts and re.fullmatch(r"K5VisionAlpha-[0-9a-fA-F]{32}", parts[0]):
        return "alpha_session"
    return "other_owned_temp"


def notification_categories(data: bytes) -> list[tuple[str, str]]:
    """Parse only a bounded owned-directory buffer; never return raw names."""
    need(0 < len(data) <= 65_536, "limit")
    offset, result = 0, []
    while True:
        need(offset + 12 <= len(data), "native_error")
        following, action, length = struct.unpack_from("<III", data, offset)
        need(action in ACTIONS and length % 2 == 0 and 0 < length <= 4096, "native_error")
        end = offset + 12 + length
        need(end <= len(data), "native_error")
        try:
            kind = classify_temp(data[offset + 12 : end].decode("utf-16-le", errors="strict"))
        except UnicodeError:
            raise ObservationFailure("native_error") from None
        result.append((kind, ACTIONS[action]))
        need(len(result) <= MAX_EVENTS, "limit")
        if following == 0:
            return result
        need(following >= 12 + length and following % 4 == 0, "native_error")
        offset += following


def empty_record():
    return {
        "schema_version": "owned-start-preflight-observation-v1",
        "error": "none",
        "old_contract": "none",
        **dict.fromkeys(COUNT_FIELDS, 0),
        **dict.fromkeys(BOOL_FIELDS, False),
        **{f"birth_{index}_class": None for index in range(1, MAX_PROCESSES + 1)},
    }


def validate_record(value):
    need(type(value) is dict and value.keys() == FIELDS)
    need(
        type(value["schema_version"]) is str
        and value["schema_version"] == "owned-start-preflight-observation-v1"
    )
    need(type(value["error"]) is str and value["error"] in ERRORS)
    need(
        type(value["old_contract"]) is str
        and value["old_contract"] in alpha.DIAGNOSTIC_CONTRACTS | {"none"}
    )
    for key in BOOL_FIELDS:
        need(type(value[key]) is bool)
    for key in COUNT_FIELDS:
        need(type(value[key]) is int and 0 <= value[key] <= MAX_EVENTS)
    for index in range(1, MAX_PROCESSES + 1):
        value_at = value[f"birth_{index}_class"]
        need(value_at is None or type(value_at) is str and value_at in PROCESS_KINDS)
    if value["process_coverage_complete"]:
        total = value["job_total"]
        need(0 < total <= MAX_PROCESSES and value["distinct_births"] == total)
        need(value["job_active"] == 0 and value["process_unknown"] == 0)
        kinds = [value[f"birth_{index}_class"] for index in range(1, total + 1)]
        need(all(kind is not None for kind in kinds))
        need(
            all(
                value[f"birth_{index}_class"] is None
                for index in range(total + 1, MAX_PROCESSES + 1)
            )
        )
        need(all(value["process_" + kind] == kinds.count(kind) for kind in PROCESS_KINDS))
    need(len(common.canonical(value)) <= common.MAX_BYTES)


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
                need(common.file_hash(expected) == identity)
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
            while True:
                status = self.api.WaitForSingleObject(self.event, 100)
                if status == 258:
                    continue
                need(status == 0, "native_error")
                count = w.DWORD()
                success = self.api.GetOverlappedResult(
                    self.handle, ctypes.byref(self.overlapped), ctypes.byref(count), False
                )
                if not success and ctypes.get_last_error() == 995 and self.stop.is_set():
                    return
                native_need(success)
                for kind, action in notification_categories(self.buffer.raw[: count.value]):
                    need(self.total < MAX_EVENTS, "limit")
                    self.total += 1
                    self.counts["temp_" + kind + "_" + action] += 1
                if self.stop.is_set():
                    return
                if not self._issue():
                    return
        except BaseException as error:
            self.error = str(error) if isinstance(error, ObservationFailure) else "native_error"
            self.abort()

    def finish(self):
        with self.io_lock:
            self.stop.set()
            # Serialize cancellation against reissue; no new request after stop.
            cancelled = self.api.CancelIoEx(self.handle, ctypes.byref(self.overlapped))
            if not cancelled:
                need(ctypes.get_last_error() == 1168, "cleanup_incomplete")
        self.thread.join(3)
        need(not self.thread.is_alive(), "cleanup_incomplete")
        closed = bool(self.api.CloseHandle(self.event))
        closed = bool(self.api.CloseHandle(self.handle)) and closed
        self.closed = closed
        need(closed, "cleanup_incomplete")


IDENTITY_PROBE = """import hashlib, importlib.metadata, importlib.util, json, pathlib, site, sys
v=json.loads(pathlib.Path(sys.argv[1]).read_bytes())
prefix=pathlib.Path(sys.prefix).resolve(strict=True)
assert prefix == pathlib.Path(v['prefix']).resolve(strict=True)
assert sys.prefix != sys.base_prefix and sys.flags.isolated == 1 and site.ENABLE_USER_SITE is False
assert (pathlib.Path(sys._base_executable).resolve(strict=True)
        == pathlib.Path(v['base']).resolve(strict=True))
assert pathlib.Path(importlib.util.find_spec('k5vision').origin).resolve().is_relative_to(prefix)
for name, expected in v['versions'].items():
    assert importlib.metadata.version(name) == expected
package=importlib.metadata.distribution('k5-vision')
for name, expected in v['payload'].items():
    p=pathlib.Path(package.locate_file(name)).resolve(strict=True)
    assert p.is_relative_to(prefix) and hashlib.sha256(p.read_bytes()).hexdigest() in expected
print('COPIED_RUNTIME_VERIFIED')
"""


def trusted_payload_variants(source: Path):
    """Generate exact checkout forms from trusted Git bytes, never normalize input.

    The older hosted probe was installed from a Windows checkout. Only tracked
    Python sources and the two enumerated UTF-8 package data files permit its
    deterministic LF-to-CRLF checkout representation. Production hashes stay exact.
    """
    text_data = {
        "k5vision/data/analytics-runtime-manifest.json",
        "k5vision/data/analytics-runtime-Apache-2.0.txt",
    }
    result = {}
    for name, exact in alpha.source_payload(source).items():
        choices = [exact]
        if name.endswith(".py") or name in text_data:
            data = common.local_path(source / "src" / name).read_bytes()
            try:
                data.decode("utf-8", errors="strict")
            except UnicodeError:
                raise ObservationFailure("fixture_admission") from None
            need(b"\r" not in data)
            converted = common.hashlib.sha256(data.replace(b"\n", b"\r\n")).hexdigest()
            if converted != exact:
                choices.append(converted)
        result[name] = choices
    return result


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
        and record["process_unknown"] == 0
    )
    if job.observer.error != "none":
        record["error"] = job.observer.error


def copy_runtime(source_python: Path, target: Path, base: Path):
    source_python = common.local_path(source_python)
    need(
        source_python.name.lower() == "python.exe"
        and source_python.parent.name.lower() == "scripts"
    )
    source = common.local_path(source_python.parent.parent, directory=True)
    config = common.local_path(source / "pyvenv.cfg")
    need(config.stat().st_size < 8192)
    values = {}
    for line in config.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = (item.strip() for item in line.split("=", 1))
            need(key not in values)
            values[key] = value
    need(values.get("include-system-site-packages") == "false")
    need(common.local_path(Path(values["home"]), directory=True) == base.parent)
    need(common.local_path(Path(values["executable"])) == base)
    need(values.get("version") == sys.version.split()[0])
    before = alpha.tree_manifest(source)
    shutil.copytree(source, target)
    need(alpha.tree_manifest(target) == before and alpha.tree_manifest(source) == before)


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


def run_reproduction(tmp_path: Path, monkeypatch, record, resources):
    base = common.admitted_gate_python(dict(os.environ))
    owned = tmp_path / "owned-preflight"
    owned.mkdir()
    installed = owned / "installed"
    installed.mkdir()
    env = alpha.clean_environment(dict(os.environ), owned)
    for name in ("TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        Path(env[name]).mkdir(parents=True, exist_ok=True)
    git = common.local_path(Path(shutil.which("git")))
    revision = (
        common.capture(
            [str(git), "-C", str(ROOT), "rev-parse", "HEAD"],
            cwd=owned,
            env=env,
            operation="driver_admission",
            limit=128,
        )
        .decode("ascii")
        .strip()
    )
    need(re.fullmatch(r"[0-9a-f]{40}", revision) is not None)
    command = common.archive_command(ROOT, owned / "source.zip", revision)
    command[0] = str(git)
    common.run(command, cwd=owned, env=env, operation="archive_candidate")
    source = owned / "source"
    common.extract_archive(owned / "source.zip", source)
    start = source / "scripts/windows-alpha/Start-K5VisionAlpha.ps1"
    shutil.copyfile(start, installed / start.name)
    need(start.read_bytes() == (installed / start.name).read_bytes())
    record["start_verified"] = True
    copy_runtime(Path(os.environ["K5_PREFLIGHT_PROBE_PYTHON"]), installed / ".venv", base)
    probe, inputs = owned / "identity-probe.py", owned / "identity-inputs.json"
    probe.write_text(IDENTITY_PROBE, encoding="ascii")
    inputs.write_bytes(
        common.canonical(
            {
                "prefix": str(installed / ".venv"),
                "base": str(base),
                "versions": common.requirements(
                    source / "scripts/windows-alpha/runtime-requirements.txt"
                ),
                "payload": trusted_payload_variants(source),
            }
        )
    )
    python = installed / ".venv/Scripts/python.exe"
    output = common.capture(
        [str(python), "-I", "-B", str(probe), str(inputs)],
        cwd=owned,
        env=env,
        operation="probe_admission",
        limit=128,
    )
    need(output.strip() == b"COPIED_RUNTIME_VERIFIED")
    record["runtime_verified"] = True
    config = owned / "invalid-analytics.json"
    config.write_bytes(b'{"schema_version":1,"provider":"invalid-selected-provider"}')
    env["K5_ANALYTICS_CONFIG"] = str(config)
    shell = common.local_path(
        Path(env["SYSTEMROOT"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    admitted = {
        "base_python": (base, common.file_hash(base)),
        "venv_python": (python, common.file_hash(python)),
        "powershell": (shell, common.file_hash(shell)),
    }
    for kind, relative in (
        ("console_host", "System32/conhost.exe"),
        ("dotnet_compiler", "Microsoft.NET/Framework64/v4.0.30319/csc.exe"),
    ):
        candidate = Path(env["SYSTEMROOT"]) / relative
        if candidate.is_file():
            candidate = common.local_path(candidate)
            admitted[kind] = (candidate, common.file_hash(candidate))
    envelope = owned / "invoke-start.ps1"
    envelope.write_text(alpha.ENVELOPE, encoding="ascii", newline="\n")
    need(not (installed / "gstreamer-version.txt").exists())
    observed = []
    original_job = common.WindowsJob

    class ObservedJob(original_job):
        def __init__(self):
            super().__init__()
            self.abort_lock = threading.Lock()
            self.aborted = False
            self.observation_open = True
            self.resources_closed = False
            self.observer = None
            self.final_total = self.final_active = 0
            try:
                self.observer = ProcessObserver(self, admitted, resources)
                observed.append(self)
                need(watcher.error == "none", watcher.error)
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
                lambda: (watcher.error, self.observer.error),
                lambda child: original_job.assign(self, child),
            )

        def close(self):
            failure = None
            try:
                native_need(self.api.TerminateJobObject(self.handle, 1))
                deadline = time.monotonic() + 5
                while True:
                    state = self.accounting()
                    self.final_total, self.final_active = (
                        state.total_processes,
                        state.active_processes,
                    )
                    if not self.final_active:
                        break
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

    def abort_owned():
        for job in observed:
            job.abort()

    watcher = TempObserver(Path(env["TEMP"]), abort_owned, resources)
    old_error = None
    try:
        need(watcher.error == "none", watcher.error)
        monkeypatch.setattr(common, "WindowsJob", ObservedJob)
        try:
            alpha.invoke_start(
                alpha.start_command(shell, envelope, installed, 8011),
                work=owned,
                env=env,
                operation="probe_admission",
                invalid=True,
            )
        except common.WitnessError as error:
            old_error = error
            record["old_gate_rejected"] = True
            if isinstance(error, alpha.AlphaWitnessError):
                record["old_contract"] = error.alpha_diagnostic["contract"]
                alpha.emit_alpha_diagnostic(error.alpha_diagnostic)
    finally:
        watcher.finish()
        record.update(watcher.counts)
        record["temp_events"] = watcher.total
        record["temporary_root_empty"] = not any(Path(env["TEMP"]).iterdir())
        if len(observed) == 1:
            job = observed[0]
            process_record(record, job)
        if watcher.error != "none":
            record["error"] = watcher.error
        record["cleanup_complete"] = watcher.closed and all(
            job.resources_closed for job in observed
        )
    need(len(observed) == 1 and record["process_coverage_complete"], "incomplete")
    need(watcher.error == "none", watcher.error)
    if old_error is not None:
        raise ObservationFailure("launcher_rejected") from None


@pytest.mark.skipif(os.name != "nt", reason="Hosted Windows owned-only boundary reproduction")
def test_windows_actual_start_bad_config_owned_observation(tmp_path, monkeypatch):
    record = empty_record()
    resources = []
    failure = None
    try:
        run_reproduction(tmp_path, monkeypatch, record, resources)
    except BaseException as error:
        failure = str(error) if isinstance(error, ObservationFailure) else "fixture_admission"
        if record["error"] == "none":
            record["error"] = failure
    finally:
        try:
            owned = tmp_path / "owned-preflight"
            need(observers_quiescent(resources), "cleanup_incomplete")
            if owned.exists():
                shutil.rmtree(owned)
            record["cleanup_complete"] = record["cleanup_complete"] and not owned.exists()
        except BaseException:
            failure = record["error"] = "cleanup_incomplete"
        try:
            validate_record(record)
            print("K5_OWNED_PREFLIGHT_OBSERVATION=" + common.canonical(record).decode("ascii"))
        except BaseException:
            failure = "incomplete"
            print("K5_OWNED_PREFLIGHT_OBSERVATION_INVALID")
    if failure is not None:
        pytest.fail("Owned Start preflight observation failed: " + failure, pytrace=False)


def test_fixed_temp_categories_never_return_names():
    assert classify_temp("__PSScriptPolicyTest_abcdefgh.xyz.ps1") == "policy_probe"
    assert classify_temp("__PSScriptPolicyTest_abcdefgh.xyz.psm1") == "policy_probe"
    assert classify_temp("K5VisionAlpha-" + "a" * 32 + "\\users.sqlite3") == "alpha_session"
    assert classify_temp("sensitive-unexpected-name") == "other_owned_temp"


def test_notification_parser_is_bounded_and_source_free():
    name = "__PSScriptPolicyTest_abcdefgh.xyz.ps1".encode("utf-16-le")
    assert notification_categories(struct.pack("<III", 0, 1, len(name)) + name) == [
        ("policy_probe", "added")
    ]
    for data in (b"", b"x" * 65537, struct.pack("<III", 0, 99, 2) + b"aa"):
        with pytest.raises(ObservationFailure):
            notification_categories(data)


@pytest.mark.parametrize(
    "change",
    [
        {"path": "secret"},
        {"error": "raw exception"},
        {"birth_1_class": "private executable"},
        {"job_total": True},
        {"job_total": 257},
        {"process_coverage_complete": 1},
        {"old_contract": "private path"},
    ],
)
def test_observation_record_rejects_unknown_or_unbounded_content(change):
    record = empty_record()
    record.update(change)
    with pytest.raises(ObservationFailure):
        validate_record(record)


def test_hosted_reproduction_paths_cannot_trigger_native_candidate_job():
    from fnmatch import fnmatchcase

    text = (ROOT / ".github/workflows/installed-analytics-candidate.yml").read_text()
    push = text.split("  push:\n", 1)[1].split("\n\n", 1)[0]
    patterns = [
        line.strip()[2:].strip("\"'")
        for line in push.split("    paths:\n", 1)[1].splitlines()
        if line.strip().startswith("- ")
    ]
    changed = (
        "tests/test_windows_alpha_preflight_boundary.py",
        ".github/workflows/windows-alpha-script-smoke.yml",
    )
    assert patterns and all(
        not fnmatchcase(path, pattern) for path in changed for pattern in patterns
    )
    smoke = (ROOT / changed[1]).read_text()
    assert "K5_PREFLIGHT_PROBE_PYTHON" in smoke
    assert "tests/test_windows_alpha_preflight_boundary.py --no-cov -q -s" in smoke
    assert "windows_real_venv_child_is_owned" in smoke
    assert "test_windows_real_directory_guard_remembers_created_then_deleted_session" in smoke


def fake_process_observer(monkeypatch, tmp_path, *, belongs=True, denied=False):
    from types import SimpleNamespace

    path = tmp_path / "admitted.exe"
    path.write_bytes(b"admitted")
    calls, births = [], [100]
    aborted = []

    def opened(access, inherit, pid):
        calls.append(("open", access, inherit, pid))
        return None if denied else 123

    def membership(_handle, job, output):
        calls.append(("membership", job))
        output._obj.value = belongs
        return True

    def times(_handle, created, *_rest):
        calls.append(("birth",))
        created._obj.value = births[0]
        return True

    def image(_handle, _flags, buffer, _size):
        calls.append(("image",))
        buffer.value = str(path)
        return True

    observer = object.__new__(ProcessObserver)
    observer.api = SimpleNamespace(
        OpenProcess=opened,
        IsProcessInJob=membership,
        GetProcessTimes=times,
        QueryFullProcessImageNameW=image,
        CloseHandle=lambda handle: calls.append(("close", handle)) or True,
    )
    observer.job = SimpleNamespace(handle=456, abort=lambda: aborted.append(True))
    observer.job_query_handle = 456
    observer._release = lambda: None
    observer.admitted = {"base_python": (path, common.file_hash(path))}
    observer.births, observer.handles, observer.error = {}, [], "none"
    observer.stop, observer.port = threading.Event(), 789
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
    return observer, calls, births, aborted


def test_owned_membership_precedes_birth_and_image_and_dedupes_births(monkeypatch, tmp_path):
    observer, calls, births, _aborted = fake_process_observer(monkeypatch, tmp_path)
    observer._observe(10)
    assert [call[0] for call in calls[:4]] == ["open", "membership", "birth", "image"]
    assert calls[0][1:3] == (0x1000, False)
    observer._observe(10)
    assert len(observer.births) == 1 and len(observer.handles) == 1
    births[0] = 200
    observer._observe(10)
    assert len(observer.births) == 2 and len(observer.handles) == 2


def test_reused_or_foreign_job_pid_never_exposes_image_or_birth(monkeypatch, tmp_path):
    observer, calls, _births, _aborted = fake_process_observer(monkeypatch, tmp_path, belongs=False)
    with pytest.raises(ObservationFailure, match="ownership_unproven"):
        observer._observe(10)
    assert [call[0] for call in calls] == ["open", "membership", "close"]
    assert not observer.births


def test_owned_open_denial_stops_job_without_retry_or_rights_expansion(monkeypatch, tmp_path):
    observer, calls, _births, aborted = fake_process_observer(monkeypatch, tmp_path, denied=True)

    def notification(_port, message, key, value, _timeout):
        message._obj.value, key._obj.value, value._obj.value = 6, 1, 10
        return True

    observer.api.GetQueuedCompletionStatus = notification
    observer._read()
    assert observer.error == "access_denied" and aborted == [True]
    assert calls == [("open", 0x1000, False, 10)]
    assert not observer.births


def test_incomplete_process_queue_cannot_claim_coverage():
    record = empty_record()
    record.update(
        job_total=5,
        distinct_births=4,
        process_base_python=2,
        process_venv_python=1,
        process_powershell=1,
    )
    assert record["distinct_births"] != record["job_total"]
    assert record["process_coverage_complete"] is False


def test_temp_reissue_is_blocked_after_cancellation():
    from types import SimpleNamespace

    watcher = object.__new__(TempObserver)
    watcher.io_lock, watcher.stop = threading.Lock(), threading.Event()
    watcher.stop.set()
    calls = []
    watcher.api = SimpleNamespace(
        ResetEvent=lambda *_: calls.append("reset"),
        ReadDirectoryChangesW=lambda *_: calls.append("read"),
    )
    assert watcher._issue() is False and calls == []


def test_temp_access_denial_aborts_only_owned_execution(monkeypatch):
    from types import SimpleNamespace

    watcher = object.__new__(TempObserver)
    watcher.stop, watcher.error = threading.Event(), "none"
    watcher.begin = threading.Event()
    watcher.begin.set()
    watcher.event, watcher.handle, watcher.overlapped = 1, 2, ctypes.c_uint64()
    aborted = []
    watcher.abort = lambda: aborted.append(True)
    watcher.api = SimpleNamespace(
        WaitForSingleObject=lambda *_: 0, GetOverlappedResult=lambda *_: False
    )
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
    watcher._read()
    assert watcher.error == "access_denied" and aborted == [True]


def test_trusted_checkout_variants_reject_mixed_endings_and_tamper(tmp_path):
    package = tmp_path / "src/k5vision"
    (package / "data").mkdir(parents=True)
    files = {
        "analytics_config.py": b"first = 1\nsecond = 2\n",
        "data/analytics-runtime-manifest.json": b'{\n"schema": 1\n}\n',
        "data/analytics-runtime-Apache-2.0.txt": b"reviewed\nnotice\n",
        "data/other.txt": b"unclassified\ntext\n",
    }
    for name, data in files.items():
        (package / name).write_bytes(data)
    allowed = trusted_payload_variants(tmp_path)
    for name in (
        "analytics_config.py",
        "data/analytics-runtime-manifest.json",
        "data/analytics-runtime-Apache-2.0.txt",
    ):
        data = files[name]
        expected = allowed["k5vision/" + name]
        assert common.hashlib.sha256(data).hexdigest() in expected
        assert common.hashlib.sha256(data.replace(b"\n", b"\r\n")).hexdigest() in expected
        assert common.hashlib.sha256(data.replace(b"\n", b"\r\n", 1)).hexdigest() not in expected
        assert common.hashlib.sha256(data + b"tamper").hexdigest() not in expected
    assert len(allowed["k5vision/data/other.txt"]) == 1
    (package / "analytics_config.py").write_bytes(b"not canonical\r\n")
    with pytest.raises(ObservationFailure):
        trusted_payload_variants(tmp_path)


def test_process_record_requires_full_distinct_birth_reconciliation():
    from types import SimpleNamespace

    births = {
        (1, 100): "base_python",
        (2, 200): "powershell",
        (3, 300): "venv_python",
        (4, 400): "base_python",
    }
    observer = SimpleNamespace(error="none", births=births)
    job = SimpleNamespace(observer=observer, final_total=5, final_active=0)
    record = empty_record()
    process_record(record, job)
    assert record["distinct_births"] == 4 and record["process_coverage_complete"] is False
    births[(5, 500)] = "console_host"
    record = empty_record()
    process_record(record, job)
    assert record["distinct_births"] == 5 and record["process_coverage_complete"] is True
    assert record["birth_5_class"] == "console_host"
    births[(5, 500)] = "unknown"
    record = empty_record()
    process_record(record, job)
    assert record["process_coverage_complete"] is False


def test_temp_finish_cancels_only_its_request_before_closing_handles():
    from types import SimpleNamespace

    watcher = object.__new__(TempObserver)
    watcher.io_lock, watcher.stop = threading.Lock(), threading.Event()
    watcher.handle, watcher.event, watcher.overlapped, watcher.closed = (
        1,
        2,
        ctypes.c_uint64(),
        False,
    )
    calls = []
    watcher.api = SimpleNamespace(
        CancelIoEx=lambda handle, _overlap: calls.append(("cancel", handle)) or True,
        CloseHandle=lambda handle: calls.append(("close", handle)) or True,
    )
    watcher.thread = SimpleNamespace(
        join=lambda seconds: calls.append(("join", seconds)), is_alive=lambda: False
    )
    watcher.finish()
    assert calls == [("cancel", 1), ("join", 3), ("close", 2), ("close", 1)]
    assert watcher.stop.is_set() and watcher.closed


def test_reader_join_timeout_keeps_query_handle_and_blocks_file_cleanup():
    from types import SimpleNamespace

    observer = object.__new__(ProcessObserver)
    observer.births, observer.error = {(10, 100): "base_python"}, "none"
    observer.stop, observer.resource_lock = threading.Event(), threading.Lock()
    observer.closed, observer.port, observer.job_query_handle = False, 123, 456
    alive, calls = [True], []
    observer.thread = SimpleNamespace(
        join=lambda seconds: calls.append(("join", seconds)), is_alive=lambda: alive[0]
    )
    observer.api = SimpleNamespace(
        PostQueuedCompletionStatus=lambda *_: calls.append(("wake",)),
        CloseHandle=lambda handle: calls.append(("close", handle)),
    )
    with pytest.raises(ObservationFailure, match="cleanup_incomplete"):
        observer.finish(1)
    assert calls == [("wake",), ("join", 3)]
    assert observer.job_query_handle == 456 and not observer.closed
    assert observers_quiescent([observer]) is False
    alive[0] = False
    assert observers_quiescent([observer]) is True


def test_membership_uses_observer_duplicate_not_closed_owner_handle(monkeypatch, tmp_path):
    observer, calls, _births, _aborted = fake_process_observer(monkeypatch, tmp_path)
    observer.job.handle = 999  # Original owner handle may already be closed/reused.
    observer._observe(10)
    assert ("membership", 456) in calls and ("membership", 999) not in calls


def test_false_complete_record_is_rejected():
    record = empty_record()
    record.update(job_total=5, distinct_births=4, process_coverage_complete=True)
    with pytest.raises(ObservationFailure):
        validate_record(record)


@pytest.mark.parametrize("aborted,error", [(True, "none"), (False, "access_denied")])
def test_observer_abort_before_assignment_cannot_open_execution_gate(aborted, error):
    from types import SimpleNamespace

    calls = []
    job = SimpleNamespace(
        abort_lock=threading.Lock(),
        aborted=aborted,
        handle=123,
        api=SimpleNamespace(TerminateJobObject=lambda *_: calls.append("terminate")),
    )
    with pytest.raises(ObservationFailure, match="incomplete"):
        guarded_assignment(
            job, object(), lambda: (error, "none"), lambda _process: calls.append("assign")
        )
    assert calls == []
    assert job.aborted is True


def test_denial_during_assignment_terminates_before_return_to_gate_opener():
    from types import SimpleNamespace

    calls, errors = [], ["none", "none"]
    job = SimpleNamespace(
        abort_lock=threading.Lock(),
        aborted=False,
        handle=123,
        api=SimpleNamespace(TerminateJobObject=lambda *_: calls.append("terminate")),
    )

    def assign(_process):
        calls.append("assign")
        errors[0] = "access_denied"

    with pytest.raises(ObservationFailure, match="incomplete"):
        guarded_assignment(job, object(), lambda: tuple(errors), assign)
    assert calls == ["assign", "terminate"] and job.aborted is True
