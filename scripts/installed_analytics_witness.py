#!/usr/bin/env python3
"""Isolated installed normal-app Windows witness; retained output is scalar only.

No K5 import occurs in the controller. The installed app is always an unmodified
``python -I -B -m k5vision.cli serve --operator`` subprocess. Probe and publication
modes are separate fixture processes, never application/provider injection.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import hashlib
import importlib.metadata
import importlib.util
import io
import json
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ANALYTICS_REVISION = "c8b347ae538991a0c0ce38eabc2dc17b566531d3"
RUNTIME_VERSIONS = {
    "openvino": "2026.3.1",
    "opencv-python-headless": "4.12.0.88",
    "numpy": "2.2.6",
    "openvino-telemetry": "2025.2.0",
}
WINDOWS_RUNTIME_VERSIONS = {"colorama": "0.4.6", "pyreadline3": "3.5.6"}
MEDIA_MTX_ARCHIVE = "faa97974861eb75a68b5aa326c78e7e7a6f670b5ef191bace78e715130381f23"
MAX_BYTES = 16_384
MAX_COUNTER = 1_000_000
GSTREAMER_INSTALLER = "032fc6062b8539838fc8da22589cb9b24c5d820baa7f8cc160af9ea08395badf"
GSTREAMER_ELEMENTS = {
    "tcpclientsrc": ("tcp", "gst-plugins-base", "LGPL"),
    "rawvideoparse": ("rawparse", "gst-plugins-base", "LGPL"),
    "identity": ("coreelements", "gstreamer", "LGPL"),
    "videoconvert": ("videoconvertscale", "gst-plugins-base", "LGPL"),
    "x264enc": ("x264", "gst-plugins-ugly", "GPL"),
    "h264parse": ("videoparsersbad", "gst-plugins-bad", "LGPL"),
    "rtspclientsink": ("rtspclientsink", "gst-rtsp-server", "LGPL"),
    "d3d11h264dec": ("d3d11", "gst-plugins-bad", "LGPL"),
    "rtspsrc": ("rtsp", "gst-plugins-good", "LGPL"),
    "rtph264depay": ("rtp", "gst-plugins-good", "LGPL"),
    "appsink": ("app", "gst-plugins-base", "LGPL"),
    "capsfilter": ("coreelements", "gstreamer", "LGPL"),
}
STAGES = {
    "admission",
    "build",
    "install",
    "probe",
    "fixture",
    "app",
    "auth",
    "live_1",
    "live_2",
    "verify",
    "cleanup",
    "complete",
}
FAILURES = {
    "none",
    "admission_failed",
    "identity_mismatch",
    "dependency_missing",
    "configuration_missing",
    "child_failed",
    "child_timeout",
    "output_invalid",
    "port_occupied",
    "readiness_failed",
    "auth_failed",
    "receipt_invalid",
    "cleanup_incomplete",
    "unexpected",
}
IDENTITIES = {
    "k5_payload_sha256",
    "k5_wheel_sha256",
    "analytics_manifest_sha256",
    "analytics_wheel_sha256",
    "runtime_identity_sha256",
    "model_identity_sha256",
    "seed_identity_sha256",
    "native_identity_sha256",
}
COUNTERS = {
    "delivered_frames",
    "presentations",
    "processed_controls",
    "analytics_provider_submissions",
    "analytics_provider_completions",
    "analytics_failures",
    "analytics_rendered_boxes",
}
RUN_FIELDS = COUNTERS | {"analytics_enabled", "completed"}
RECEIPT_FIELDS = (
    IDENTITIES
    | {
        "schema_version",
        "revision",
        "analytics_revision",
        "execution_context",
        "completed",
        "cleanup_complete",
        "service_token_rejected",
        "stage",
        "failure_code",
    }
    | {f"run_{run}_{field}" for run in (1, 2) for field in RUN_FIELDS}
)


# A separate diagnostic line never extends the retained success-receipt schema.
OPERATIONS = {
    "driver_admission",
    "archive_candidate",
    "archive_analytics",
    "create_venv",
    "build_k5_wheel",
    "build_analytics_wheel",
    "rebuild_analytics_wheel",
    "install_dependencies",
    "download_dependencies",
    "install_local_wheels",
    "check_dependencies",
    "probe_before",
    "probe_after",
    "probe_admission",
    "inspect_gstreamer_version",
    "inspect_mediamtx_version",
    "start_rtsp_server",
    "start_publisher",
    "publish_gstreamer",
    "start_application",
    "wait_rtsp_server",
    "wait_rtsp_publication",
    "wait_application",
    "authenticate",
    "launch_1",
    "launch_2",
    "verify_identity",
    "cleanup_owned",
    "receipt_validate",
    "publish_fixture",
} | {f"inspect_{element}" for element in GSTREAMER_ELEMENTS}
OUTCOMES = {"child_failed", "gate_failed", "launch_failed", "timeout", "failed", "cleanup_failed"}
GATE_STATES = {"not_used", "waiting", "opened", "started", "exited", "launch_failed", "refused"}
# Ordered: a network retry followed by pip's misleading final 'No matching
# distribution' must retain the useful network classification.
ERROR_PATTERNS = (
    (
        "dns_failed",
        rb"getaddrinfo failed|temporary failure in name resolution|"
        rb"name or service not known|failed to resolve|nodename nor servname",
    ),
    ("tls_failed", rb"certificate_verify_failed|certificate verify failed|sslerror"),
    (
        "network_failed",
        rb"newconnectionerror|connection refused|network is unreachable|"
        rb"connecttimeouterror|readtimeouterror|read timed out|broken pipe|connectionreseterror",
    ),
    ("analytics_manifest_invalid", rb"analytics package admission manifest is invalid"),
    (
        "analytics_source_mismatch",
        rb"analytics source inventory|analytics source git blob|"
        rb"analytics package payload (?:hash|size|is missing)",
    ),
    (
        "analytics_configuration_invalid",
        rb"configured analytics (?:input is invalid|runtime admission failed)",
    ),
    ("model_identity_mismatch", rb"artifact checksum mismatch|artifact size mismatch"),
    ("seed_identity_mismatch", rb"seed media.*(?:mismatch|identity)|seed paths may not"),
    ("git_unsafe_repository", rb"detected dubious ownership|unsafe repository"),
    ("git_repository_missing", rb"not a git repository"),
    (
        "git_revision_missing",
        rb"not a valid object name|bad object|unknown revision|"
        rb"invalid object name|not a valid commit name",
    ),
    ("pip_missing", rb"no module named pip"),
    (
        "venv_bootstrap_failed",
        rb"ensurepip.*(?:non-zero exit|failed)|"
        rb"ensurepip is not available",
    ),
    (
        "dependency_conflict",
        rb"resolutionimpossible|conflicting dependencies|"
        rb"has requirement .* but you have",
    ),
    ("dependency_unavailable", rb"no matching distribution|could not find a version"),
    (
        "build_backend_failed",
        rb"metadata-generation-failed|failed building wheel|"
        rb"backendunavailable|subprocess-exited-with-error",
    ),
    (
        "python_runtime_failed",
        rb"fatal python error|failed to load.*python|"
        rb"failed to get the python codec|no python at",
    ),
    ("native_library_missing", rb"dll load failed|specified module could not be found"),
    ("module_missing", rb"no module named"),
    ("gst_element_unavailable", rb"no element |no such element|no property .* in element"),
    (
        "publication_connect_failed",
        rb"could not connect to|failed to connect to|"
        rb"could not open resource for writing",
    ),
    ("permission_denied", rb"permission denied|access is denied|winerror 5"),
    ("executable_missing", rb"winerror 2|no such file or directory|cannot find the file"),
)
CATEGORIES = (
    {item[0] for item in ERROR_PATTERNS}
    | FAILURES
    | {
        "unclassified",
        "readiness_timeout",
        "gate_protocol_invalid",
        "output_limit",
        "collector_incomplete",
        "http_rejected",
    }
)
DIAGNOSTIC_FIELDS = {
    "operation",
    "outcome",
    "gate_state",
    "child_exit_code",
    "relay_exit_code",
    "timed_out",
    "category",
}
_MAX_STDERR_CLASSIFIED = 65_536


def validate_diagnostic(value: object) -> None:
    if type(value) is not dict or value.keys() != DIAGNOSTIC_FIELDS:
        raise ValueError("invalid diagnostic")
    for key, allowed in (
        ("operation", OPERATIONS),
        ("outcome", OUTCOMES),
        ("gate_state", GATE_STATES),
        ("category", CATEGORIES),
    ):
        if type(value[key]) is not str or value[key] not in allowed:
            raise ValueError("invalid diagnostic")
    if type(value["timed_out"]) is not bool:
        raise ValueError("invalid diagnostic")
    for key in ("child_exit_code", "relay_exit_code"):
        code = value[key]
        if code is not None and (type(code) is not int or not -(2**31) <= code < 2**32):
            raise ValueError("invalid diagnostic")


def diagnostic(
    operation: str,
    *,
    outcome: str = "failed",
    gate_state: str = "not_used",
    child_exit_code: int | None = None,
    relay_exit_code: int | None = None,
    timed_out: bool = False,
    category: str = "unclassified",
) -> dict[str, object]:
    value = dict(
        operation=operation,
        outcome=outcome,
        gate_state=gate_state,
        child_exit_code=child_exit_code,
        relay_exit_code=relay_exit_code,
        timed_out=timed_out,
        category=category,
    )
    validate_diagnostic(value)
    return value


def emit_diagnostic(value: dict[str, object]) -> None:
    validate_diagnostic(value)
    print("K5_INSTALLED_DIAGNOSTIC=" + canonical(value).decode("ascii"))


def classify_error(raw: bytes) -> str:
    for category, pattern in ERROR_PATTERNS:
        if re.search(pattern, raw, re.IGNORECASE):
            return category
    return "unclassified"


class StderrSummary:
    """Drain stderr without logging/storing it; retain only enums and exit integers.

    Raw memory is one 4096-byte chunk plus a bounded rolling overlap. Classification
    stops after 64KiB. Drain/discard continues so verbose native failures cannot block
    the child; final relay markers are still read after the classification budget.
    """

    def __init__(self, stream, *, gated: bool, nonce: str = "") -> None:
        self.category = "unclassified"
        self.gate_state = "waiting" if gated else "not_used"
        self.child_exit_code = None
        self.marker = rb"K5_GATE_" + re.escape(nonce.encode("ascii")) + rb"_"
        self.bytes_classified = 0
        self.read_failed = False
        self.thread = threading.Thread(target=self._read, args=(stream,), daemon=True)
        self.thread.start()

    def _promote(self, category: str) -> None:
        order = [item[0] for item in ERROR_PATTERNS]
        if category in CATEGORIES and (
            self.category == "unclassified"
            or category in order
            and (self.category not in order or order.index(category) < order.index(self.category))
        ):
            self.category = category

    def _read(self, stream) -> None:
        overlap = b""
        try:
            while block := stream.read1(4096):
                window = overlap + block
                if self.bytes_classified < _MAX_STDERR_CLASSIFIED:
                    remaining = _MAX_STDERR_CLASSIFIED - self.bytes_classified
                    self._promote(classify_error(overlap + block[:remaining]))
                    self.bytes_classified += min(len(block), remaining)
                for match in re.finditer(
                    rb"(?:^|\n)"
                    + self.marker
                    + rb"STATE=(opened|started|launch_failed|refused)\r?\n",
                    window,
                ):
                    self.gate_state = match[1].decode("ascii")
                for match in re.finditer(
                    rb"(?:^|\n)" + self.marker + rb"EXIT=(-?[0-9]{1,10})\r?\n", window
                ):
                    code = int(match[1])
                    if -(2**31) <= code < 2**32:
                        self.child_exit_code, self.gate_state = code, "exited"
                for match in re.finditer(rb"(?:^|\n)K5_CHILD_CATEGORY=([a-z_]{1,64})\r?\n", window):
                    self._promote(match[1].decode("ascii"))
                overlap = window[-256:]
        except (OSError, ValueError):
            self.read_failed = True
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                self.read_failed = True

    def finish(self, seconds: float = 5) -> bool:
        self.thread.join(seconds)
        return not self.thread.is_alive() and not self.read_failed


class WitnessError(RuntimeError):
    """A fixed project-owned code, never an exception from native code or a URL."""

    def __init__(self, code: str, detail: dict[str, object] | None = None) -> None:
        super().__init__(code if code in FAILURES else "unexpected")
        if detail is not None:
            validate_diagnostic(detail)
        self.diagnostic = detail


def require(condition: bool, code: str = "identity_mismatch") -> None:
    if not condition:
        raise WitnessError(code)


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        require(key not in result, "output_invalid")
        result[key] = value
    return result


def reject_constant(_text: str) -> object:
    raise WitnessError("output_invalid")


def parse_json(raw: bytes, limit: int = MAX_BYTES) -> dict[str, object]:
    require(len(raw) <= limit, "output_invalid")
    try:
        result = json.loads(raw, object_pairs_hook=unique_object, parse_constant=reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise WitnessError("output_invalid") from None
    require(type(result) is dict, "output_invalid")
    return result


def read_json(path: Path, limit: int = MAX_BYTES) -> dict[str, object]:
    require(
        path.is_file() and not path.is_symlink() and path.stat().st_size <= limit, "output_invalid"
    )
    with path.open("rb") as stream:
        return parse_json(stream.read(limit + 1), limit)


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def file_hash(path: Path, algorithm: str = "sha256") -> str:
    hasher = hashlib.new(algorithm)
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            hasher.update(block)
    return hasher.hexdigest()


def local_path(path: Path, *, directory: bool = False) -> Path:
    # Reject links/junctions before resolving them, including ancestor aliases.
    require(path.is_absolute(), "admission_failed")
    for part in (path, *path.parents):
        info = part.lstat()
        require(
            not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
            "admission_failed",
        )
    require(path.is_dir() if directory else path.is_file(), "admission_failed")
    return path.resolve(strict=True)


def validate_live_receipt(value: object) -> dict[str, object]:
    require(
        type(value) is dict and value.keys() == RUN_FIELDS | {"schema_version"}, "receipt_invalid"
    )
    require(
        value["schema_version"] == "2"
        and value["completed"] is True
        and value["analytics_enabled"] is True,
        "receipt_invalid",
    )
    for key in COUNTERS:
        require(type(value[key]) is int and 0 <= value[key] <= MAX_COUNTER, "receipt_invalid")
    require(value["analytics_failures"] == 0, "receipt_invalid")
    for key in COUNTERS - {"processed_controls", "analytics_failures"}:
        require(value[key] > 0, "receipt_invalid")
    require(
        value["analytics_provider_completions"]
        <= value["analytics_provider_submissions"]
        <= value["delivered_frames"],
        "receipt_invalid",
    )
    return {key: value[key] for key in RUN_FIELDS}


def validate_receipt(
    document: object, *, revision: str, identities: dict[str, str], success: bool = True
) -> None:
    require(type(document) is dict and document.keys() == RECEIPT_FIELDS, "receipt_invalid")
    require(re.fullmatch(r"[0-9a-f]{40}", revision) is not None, "identity_mismatch")
    require(
        document["revision"] == revision
        and document["analytics_revision"] == ANALYTICS_REVISION
        and document["schema_version"] == "1"
        and document["execution_context"] == "installed-normal-app-reviewed-loopback-windows-x64",
        "identity_mismatch",
    )
    require(identities.keys() == IDENTITIES, "identity_mismatch")
    for key in IDENTITIES:
        require(
            type(document[key]) is str
            and re.fullmatch(r"[0-9a-f]{64}", document[key]) is not None
            and document[key] == identities[key],
            "identity_mismatch",
        )
        if success:
            require(document[key] != "0" * 64, "identity_mismatch")
    require(document["stage"] in STAGES and document["failure_code"] in FAILURES, "receipt_invalid")
    for key in ("completed", "cleanup_complete", "service_token_rejected"):
        require(type(document[key]) is bool, "receipt_invalid")
    if success:
        require(
            document["completed"] is True
            and document["cleanup_complete"] is True
            and document["service_token_rejected"] is True
            and document["stage"] == "complete"
            and document["failure_code"] == "none",
            "receipt_invalid",
        )
    else:
        require(
            document["completed"] is False and document["failure_code"] != "none", "receipt_invalid"
        )
    for run in (1, 2):
        values = {key: document[f"run_{run}_{key}"] for key in RUN_FIELDS}
        if values["completed"] is True:
            validate_live_receipt({"schema_version": "2", **values})
            require(
                values["delivered_frames"] == 225 and values["presentations"] >= 225,
                "receipt_invalid",
            )
        else:
            require(
                not success
                and values["completed"] is False
                and values["analytics_enabled"] is False,
                "receipt_invalid",
            )
            require(
                all(type(values[key]) is int and values[key] == 0 for key in COUNTERS),
                "receipt_invalid",
            )


class JobAccountingInformation(ctypes.Structure):
    """Win32 JOBOBJECT_BASIC_ACCOUNTING_INFORMATION (fixed-width ABI)."""

    _fields_ = [
        ("total_user_time", ctypes.c_int64),
        ("total_kernel_time", ctypes.c_int64),
        ("period_user_time", ctypes.c_int64),
        ("period_kernel_time", ctypes.c_int64),
        ("total_page_faults", ctypes.c_uint32),
        ("total_processes", ctypes.c_uint32),
        ("active_processes", ctypes.c_uint32),
        ("total_terminated_processes", ctypes.c_uint32),
    ]


GATE_RUNTIME_KEYS = {"K5_WITNESS_BASE_PYTHON", "K5_WITNESS_BASE_PYTHON_SHA256"}


def admitted_gate_python(environment: dict[str, str]) -> Path:
    """Bind the relay to the caller's independently admitted base runtime.

    A Windows venv python.exe is a redirector. Starting it before Job assignment
    can leave its real interpreter outside the Job. Never use that redirector,
    a PATH lookup, or an unverified _base_executable for the gated relay.
    """
    path = environment.get("K5_WITNESS_BASE_PYTHON")
    identity = environment.get("K5_WITNESS_BASE_PYTHON_SHA256")
    require(type(path) is str and bool(path), "admission_failed")
    require(
        type(identity) is str and re.fullmatch(r"[0-9a-f]{64}", identity) is not None,
        "admission_failed",
    )
    admitted = local_path(Path(path))
    actual = getattr(sys, "_base_executable", None)
    require(type(actual) is str and bool(actual), "admission_failed")
    require(
        admitted == local_path(Path(actual))
        and admitted == local_path(Path(sys.base_prefix) / "python.exe")
        and file_hash(admitted) == identity,
        "identity_mismatch",
    )
    return admitted


class WindowsJob:
    """A new kill-on-close Job containing only processes launched by this witness.

    Process handles, not name/path/port lookups, establish ownership. Nested Jobs
    are supported on the target Windows host. Refuse startup if assignment fails.
    """

    def __init__(self) -> None:
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_int64),
                ("job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD),
                ("min_ws", ctypes.c_size_t),
                ("max_ws", ctypes.c_size_t),
                ("active", wintypes.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("basic", BasicLimits),
                ("io", ctypes.c_uint64 * 6),
                ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t),
                ("peak_process", ctypes.c_size_t),
                ("peak_job", ctypes.c_size_t),
            ]

        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.api.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.api.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
        ]
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.api.CreateJobObjectW(None, None)
        require(bool(self.handle), "cleanup_incomplete")
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(
            self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            self.api.CloseHandle(self.handle)
            raise WitnessError("cleanup_incomplete")

    def assign(self, process: subprocess.Popen[bytes]) -> None:
        require(
            bool(self.api.AssignProcessToJobObject(self.handle, int(process._handle))),
            "cleanup_incomplete",
        )

    def accounting(self) -> JobAccountingInformation:
        counters = JobAccountingInformation()
        require(
            bool(
                self.api.QueryInformationJobObject(
                    self.handle, 1, ctypes.byref(counters), ctypes.sizeof(counters), None
                )
            ),
            "cleanup_incomplete",
        )
        return counters

    def close(self) -> None:
        # Wait for owned descendants, not just the root process.
        deadline = time.monotonic() + 5
        try:
            require(bool(self.api.TerminateJobObject(self.handle, 1)), "cleanup_incomplete")
            while time.monotonic() < deadline:
                if self.accounting().active_processes == 0:
                    return
                time.sleep(0.05)
            raise WitnessError("cleanup_incomplete")
        finally:
            self.api.CloseHandle(self.handle)


class OwnedProcess:
    def __init__(
        self,
        arguments: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        operation: str,
        stdout: object = subprocess.DEVNULL,
        job_factory=None,
    ) -> None:
        require(operation in OPERATIONS, "admission_failed")
        self.operation = operation
        self.stderr_summary = None
        self.gated = os.name == "nt"
        require(job_factory is None or self.gated and callable(job_factory), "admission_failed")
        self.gate_nonce = secrets.token_hex(16) if self.gated else ""
        self.job = None
        self.process: subprocess.Popen[bytes] | None = None
        try:
            relay_python = admitted_gate_python(env) if self.gated else None
            self.job = (
                (WindowsJob if job_factory is None else job_factory)() if self.gated else None
            )
            if self.gated and job_factory is not None:
                require(
                    self.job is not None
                    and all(
                        callable(getattr(self.job, name, None)) for name in ("assign", "close")
                    ),
                    "admission_failed",
                )
            command = (
                [
                    str(relay_python),
                    "-I",
                    "-B",
                    "-S",  # No site/.pth hooks before Job assignment opens the gate.
                    str(Path(__file__).resolve()),
                    "--gate",
                    self.gate_nonce,
                    *arguments,
                ]
                if self.job is not None
                else arguments
            )
            self.process = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                stdin=subprocess.PIPE if self.job is not None else subprocess.DEVNULL,
                stdout=stdout,
                stderr=subprocess.PIPE,
            )
            self.stderr_summary = StderrSummary(
                self.process.stderr, gated=self.gated, nonce=self.gate_nonce
            )
            if self.job is not None:
                self.job.assign(self.process)
                self.process.stdin.write(b"1")
                self.process.stdin.flush()
                self.process.stdin.close()
        except BaseException as error:
            self.close()
            detail = diagnostic(
                operation,
                outcome="launch_failed",
                gate_state="waiting" if self.gated else "not_used",
                category=classify_error(str(error)[:_MAX_STDERR_CLASSIFIED].encode()),
            )
            raise WitnessError(
                str(error) if isinstance(error, WitnessError) else "child_failed", detail
            ) from None

    def failure(self, code: str = "child_failed", *, timed_out: bool = False) -> WitnessError:
        relay_code = None if self.process is None else self.process.poll()
        summary = self.stderr_summary
        if relay_code is not None and summary is not None:
            summary.finish()
        state = ("waiting" if self.gated else "not_used") if summary is None else summary.gate_state
        child_code = (
            (None if summary is None else summary.child_exit_code) if self.gated else relay_code
        )
        category = "unclassified" if summary is None else summary.category
        if category == "unclassified" and code != "child_failed":
            category = code
        outcome = (
            "timeout"
            if timed_out
            else (
                "launch_failed"
                if state == "launch_failed"
                else "gate_failed"
                if self.gated and state != "exited"
                else "child_failed"
            )
        )
        return WitnessError(
            code,
            diagnostic(
                self.operation,
                outcome=outcome,
                gate_state=state,
                child_exit_code=child_code,
                relay_exit_code=relay_code if self.gated else None,
                timed_out=timed_out,
                category=category,
            ),
        )

    def ensure_running(self) -> None:
        if self.process is None or self.process.poll() is not None:
            raise self.failure()

    def close(self) -> None:
        process, self.process = self.process, None
        job, self.job = self.job, None
        try:
            if job is not None:
                job.close()
            elif process is not None and process.poll() is None:
                process.terminate()
        except BaseException:
            raise WitnessError(
                "cleanup_incomplete",
                diagnostic(self.operation, outcome="cleanup_failed", category="cleanup_incomplete"),
            ) from None
        finally:
            if process is not None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        raise WitnessError(
                            "cleanup_incomplete",
                            diagnostic(
                                self.operation,
                                outcome="cleanup_failed",
                                category="cleanup_incomplete",
                            ),
                        ) from None
            if self.stderr_summary is not None and not self.stderr_summary.finish():
                raise WitnessError(
                    "cleanup_incomplete",
                    diagnostic(
                        self.operation, outcome="cleanup_failed", category="collector_incomplete"
                    ),
                )

    def wait(self, seconds: float) -> None:
        require(self.process is not None, "child_failed")
        try:
            code = self.process.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            raise self.failure("child_timeout", timed_out=True) from None
        if code != 0:
            raise self.failure()
        if self.stderr_summary is not None and not self.stderr_summary.finish():
            raise WitnessError(
                "cleanup_incomplete",
                diagnostic(
                    self.operation, outcome="cleanup_failed", category="collector_incomplete"
                ),
            )


def close_after_failure(owned: OwnedProcess, original: BaseException | None) -> None:
    try:
        owned.close()
    except BaseException as cleanup_error:
        if isinstance(original, WitnessError) and original.diagnostic is not None:
            emit_diagnostic(original.diagnostic)
        detail = cleanup_error.diagnostic if isinstance(cleanup_error, WitnessError) else None
        raise WitnessError(
            "cleanup_incomplete",
            detail
            or diagnostic(owned.operation, outcome="cleanup_failed", category="cleanup_incomplete"),
        ) from None


def run(
    arguments: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    operation: str,
    seconds: float = 90,
    stdout: object = subprocess.DEVNULL,
) -> None:
    owned = OwnedProcess(arguments, cwd=cwd, env=env, operation=operation, stdout=stdout)
    original = None
    try:
        owned.wait(seconds)
    except BaseException as error:
        original = error
        raise
    finally:
        close_after_failure(owned, original)


def capture(
    arguments: list[str], *, cwd: Path, env: dict[str, str], operation: str, limit: int = 131_072
) -> bytes:
    """Bounded in-memory diagnostics, never persisted or printed."""
    owned = OwnedProcess(arguments, cwd=cwd, env=env, operation=operation, stdout=subprocess.PIPE)
    result: list[bytes] = []

    def read() -> None:
        result.append(owned.process.stdout.read(limit + 1))

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    original = None
    try:
        reader.join(15)
        if reader.is_alive():
            raise owned.failure("child_timeout", timed_out=True)
        if len(result) != 1 or len(result[0]) > limit:
            raise WitnessError("output_invalid", diagnostic(operation, category="output_limit"))
        owned.wait(5)
        return result[0]
    except BaseException as error:
        original = error
        raise
    finally:
        try:
            close_after_failure(owned, original)
        finally:
            reader.join(5)
            if reader.is_alive():
                raise WitnessError(
                    "cleanup_incomplete",
                    diagnostic(
                        operation, outcome="cleanup_failed", category="collector_incomplete"
                    ),
                )


def inspect_plugin(raw: bytes, expected: tuple[str, str, str], gst: Path) -> str:
    require(len(raw) <= 131_072, "output_invalid")
    text = raw.decode("utf-8", errors="strict")
    details = text.split("Plugin Details:", 1)
    require(len(details) == 2, "identity_mismatch")
    values = {}
    for field in ("Name", "Filename", "Version", "License", "Source module"):
        match = re.search(r"(?m)^  " + field + r"\s+([^\r\n]+)\r?$", details[1])
        require(match is not None)
        values[field] = match[1].strip()
    require(
        values["Version"] == "1.28.7"
        and (values["Name"], values["Source module"], values["License"]) == expected
    )
    filename = local_path(Path(values["Filename"]))
    require(filename.is_relative_to(gst))
    return file_hash(filename)


def health_ready(port: int) -> bool:
    code, health = api(port, "/api/v1/health", timeout=0.5)
    return code == 200 and health.get("status") == "ok"


def clean_environment(base: dict[str, str]) -> dict[str, str]:
    # No inherited application sources, credentials, persistence, Python hooks or
    # proxy/plugin overrides can alter this disposable execution.
    excluded = ("K5_", "PYTHON", "GST_", "GSTREAMER_", "GIO_", "GI_", "MTX_", "OTEL_", "PIP_")
    env = {
        key: value
        for key, value in base.items()
        if not key.upper().startswith(excluded)
        and key.upper()
        not in {"CAM_CRED", "VIRTUAL_ENV", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}
    }
    env.update(
        PYTHONNOUSERSITE="1",
        PYTHONPATH="",
        PYTHONHOME="",
        NO_PROXY="*",
        no_proxy="*",
        PIP_REQUIRE_VIRTUALENV="true",
        PIP_DISABLE_PIP_VERSION_CHECK="1",
        PIP_CONFIG_FILE=os.devnull,
    )
    # Non-secret, independently admitted relay binding needed by the installed
    # publisher's nested Job. Every Windows launch revalidates path and bytes.
    env.update({key: base[key] for key in GATE_RUNTIME_KEYS if key in base})
    return env


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def require_free_port(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            listener.bind(("127.0.0.1", port))
        except OSError:
            raise WitnessError("port_occupied") from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise WitnessError("auth_failed")


def api(
    port: int,
    path: str,
    *,
    body: dict[str, object] | None = None,
    token: str | None = None,
    timeout: float = 5,
) -> tuple[int, dict[str, object]]:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=None if body is None else canonical(body),
        headers=headers,
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(MAX_BYTES + 1)
            return response.status, {} if response.status == 204 and not raw else parse_json(raw)
    except urllib.error.HTTPError as error:
        # Status is sufficient for rejection; never retain error/source text.
        code = error.code
        error.close()
        return code, {}


def authenticate(
    port: int, admin: str, service: str, rtsp_port: int
) -> tuple[str, dict[str, object]]:
    username, password = "witness_" + secrets.token_hex(12), secrets.token_urlsafe(32)
    code, user = api(
        port,
        "/api/v1/users",
        token=admin,
        body={
            "username": username,
            "display_name": "Disposable witness",
            "role": "operator",
            "enabled": True,
        },
    )
    require(code == 201 and type(user.get("temporary_credential")) is str, "auth_failed")
    code, _ = api(
        port,
        "/api/v1/auth/bootstrap-password",
        body={
            "username": username,
            "temporary_credential": user["temporary_credential"],
            "new_password": password,
        },
    )
    require(code == 204, "auth_failed")
    code, login = api(port, "/api/v1/auth/login", body={"username": username, "password": password})
    require(
        code == 200 and type(login.get("session_token")) is str and bool(login["session_token"]),
        "auth_failed",
    )
    code, device = api(
        port,
        "/api/v1/devices",
        token=service,
        body={
            "name": "Reviewed loopback fixture",
            "host": "127.0.0.1",
            "management_port": rtsp_port,
            "kind": "camera",
            "protocols": ["rtsp"],
            "tags": ["alpha-local-synthetic", "ephemeral", "non-recording"],
        },
    )
    require(code == 201 and type(device.get("id")) is str, "auth_failed")
    body = {"device_id": device["id"], "stream_token": "local-test", "width": 1280, "height": 720}
    code, _ = api(port, "/api/v1/operator/live", token=service, body=body)
    require(code == 401, "auth_failed")
    return login["session_token"], body


def require_runtime_versions(versions: dict[str, str]) -> None:
    for name, expected in versions.items():
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            raise WitnessError("dependency_missing") from None
        require(actual == expected, "identity_mismatch")


def package_identity(names: list[str]) -> str:
    """Bind installed runtime RECORDs and verify their actual hashed bytes."""
    import base64

    records = {}
    prefix = Path(sys.prefix).resolve(strict=True)
    for name in sorted(names):
        dist = importlib.metadata.distribution(name)
        raw = dist.read_text("RECORD")
        require(raw is not None)
        rows = list(csv.reader(io.StringIO(raw)))
        for row in rows:
            require(len(row) == 3)
            relative, encoded, size = row
            path = Path(dist.locate_file(relative)).resolve(strict=True)
            require(path.is_relative_to(prefix) and path.is_file())
            if not encoded:
                require(relative.endswith(".dist-info/RECORD") and size == "")
                continue
            require(encoded.startswith("sha256=") and str(path.stat().st_size) == size)
            value = base64.urlsafe_b64encode(bytes.fromhex(file_hash(path))).rstrip(b"=").decode()
            require(encoded == "sha256=" + value)
        records[name] = {
            "version": dist.version,
            "record_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        }
    return digest(records)


def require_config_environment(environment: dict[str, str]) -> str:
    value = environment.get("K5_ANALYTICS_CONFIG")
    require(type(value) is str and bool(value.strip()), "configuration_missing")
    return value


def installed_probe(inputs: Path, output: Path) -> None:
    """Read-only installed admission; output contains digests only, never paths."""
    data = read_json(inputs, 1_048_576)
    require(sys.prefix != sys.base_prefix and sys.flags.isolated == 1)
    import site

    require(site.ENABLE_USER_SITE is False)
    prefix = Path(sys.prefix).resolve(strict=True)
    dist = importlib.metadata.distribution("k5-vision")
    payload = data["k5_payload"]
    require(type(payload) is dict and bool(payload))
    for name, expected in payload.items():
        path = Path(dist.locate_file(name))
        require(local_path(path).is_relative_to(prefix) and file_hash(path) == expected)
    spec = importlib.util.find_spec("k5vision")
    require(
        spec is not None
        and spec.origin is not None
        and Path(spec.origin).resolve() == Path(dist.locate_file("k5vision/__init__.py")).resolve()
    )
    require_runtime_versions(data["runtime_versions"])
    installed_names = {
        re.sub(r"[-_.]+", "-", item.metadata["Name"]).lower()
        for item in importlib.metadata.distributions()
    }
    require(
        installed_names - {"pip", "k5-vision", "k5-analytics-runtime"}
        == set(data["runtime_versions"])
    )
    require_config_environment(os.environ)
    from k5vision.analytics_config import load_analytics_configuration
    from k5vision.analytics_package import validate_installed_analytics

    validate_installed_analytics()
    config = load_analytics_configuration(os.environ)
    require(config is not None, "configuration_missing")
    from analytics_lab.artifacts import OPENVINO_OMZ_2023_FP16, verify_artifact_set
    from analytics_lab.validation_seed import GMDCSA24_SEED, _manifest, verify_seed_media

    models = verify_artifact_set(config.artifact_root, OPENVINO_OMZ_2023_FP16)
    require(len(models) == 4)
    root = local_path(Path(data["evidence_root"]), directory=True)
    identities = {}
    for sample in GMDCSA24_SEED:
        clip = local_path(root / "media" / "gmdcsa24" / sample.relative_path)
        require(clip.is_relative_to(root))
        identities[sample.sample_id] = verify_seed_media(clip, sample)
    # This calls the pinned rights ledger's evaluation admission and binds the
    # prepared manifest to immutable reviewed seed bytes, not a caller's path.
    require(read_json(root / "validation-manifest.json") == _manifest(identities))
    model_identity = digest(
        [
            {
                "relative_path": item.spec.relative_path,
                "size_bytes": item.spec.size_bytes,
                "sha384": item.spec.sha384,
            }
            for item in models
        ]
    )
    result = {
        "runtime_identity_sha256": package_identity(list(data["runtime_versions"])),
        "model_identity_sha256": model_identity,
        "seed_identity_sha256": digest(identities),
    }
    output.write_bytes(canonical(result))


def publish(inputs: Path) -> None:
    """Loop only the verified reviewed clip in memory into an owned RTSP publisher."""
    data = read_json(inputs)
    import cv2

    # Revalidate immediately before reading; do not trust paths supplied by a
    # validation manifest or download source/media in any execution mode.
    from analytics_lab.validation_seed import GMDCSA24_SEED, verify_seed_media

    sample = GMDCSA24_SEED[0]
    clip = local_path(Path(data["evidence_root"]) / "media" / "gmdcsa24" / sample.relative_path)
    verify_seed_media(clip, sample)
    capture = cv2.VideoCapture(str(clip))
    child = None
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    peer = None
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(15)
        require(capture.isOpened(), "readiness_failed")
        ok, frame = capture.read()
        require(ok, "readiness_failed")
        height, width = frame.shape[:2]
        require(0 < height <= 2160 and 0 < width <= 3840 and width % 4 == 0, "admission_failed")
        arguments = [
            data["gst_launch"],
            "-q",
            "tcpclientsrc",
            "host=127.0.0.1",
            f"port={listener.getsockname()[1]}",
            "!",
            "rawvideoparse",
            "format=bgr",
            f"width={width}",
            f"height={height}",
            "framerate=15/1",
            "!",
            "identity",
            "sync=true",
            "!",
            "videoconvert",
            "!",
            "video/x-raw,format=I420",
            "!",
            "x264enc",
            "speed-preset=ultrafast",
            "tune=zerolatency",
            "bitrate=2000",
            "key-int-max=30",
            "!",
            "video/x-h264,profile=baseline",
            "!",
            "h264parse",
            "config-interval=1",
            "!",
            "rtspclientsink",
            "protocols=tcp",
            f"location={data['source']}",
        ]
        # This child inherits the controller-owned Windows Job; it cannot outlive
        # the publisher fixture. No command shell or detached grandchildren.
        child = OwnedProcess(
            arguments, cwd=Path.cwd(), env=dict(os.environ), operation="publish_gstreamer"
        )
        try:
            peer, address = listener.accept()
        except OSError:
            child.ensure_running()
            raise WitnessError(
                "readiness_failed",
                diagnostic(
                    "publish_gstreamer",
                    outcome="timeout",
                    timed_out=True,
                    category="readiness_timeout",
                ),
            ) from None
        require(address[0] == "127.0.0.1", "admission_failed")
        peer.settimeout(10)
        listener.close()
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            child.ensure_running()
            require(frame.shape == (height, width, 3), "admission_failed")
            peer.sendall(frame.tobytes())
            ok, frame = capture.read()
            if not ok:
                capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ok, frame = capture.read()
                require(ok, "child_failed")
    finally:
        listener.close()
        if peer is not None:
            peer.close()
        capture.release()
        if child is not None:
            child.close()


def rtsp_listener_ready(port: int) -> bool:
    with socket.create_connection(("127.0.0.1", port), timeout=0.25) as peer:
        peer.settimeout(0.25)
        peer.sendall(
            (f"OPTIONS rtsp://127.0.0.1:{port}/k5reviewed RTSP/1.0\r\nCSeq: 1\r\n\r\n").encode()
        )
        raw = peer.recv(4096)
        return raw.startswith(b"RTSP/1.0 200 ") and b"CSeq: 1\r\n" in raw


def rtsp_ready(port: int) -> bool:
    # A bounded same-port SDP query rejects merely-open or unrelated listeners.
    with socket.create_connection(("127.0.0.1", port), timeout=0.25) as peer:
        peer.settimeout(0.25)
        peer.sendall(
            (
                f"DESCRIBE rtsp://127.0.0.1:{port}/k5reviewed RTSP/1.0\r\n"
                "CSeq: 1\r\nAccept: application/sdp\r\n\r\n"
            ).encode()
        )
        raw = bytearray()
        while b"\r\n\r\n" not in raw:
            block = peer.recv(1024)
            require(bool(block), "readiness_failed")
            raw.extend(block)
            require(len(raw) <= 4096, "readiness_failed")
        head, _, body = raw.partition(b"\r\n\r\n")
        lines = head.decode("ascii").split("\r\n")
        if lines[0].startswith(("RTSP/1.0 404", "RTSP/1.0 503")):
            return False
        require(lines[0].startswith("RTSP/1.0 200 "), "readiness_failed")
        headers = {}
        for line in lines[1:]:
            key, value = line.split(":", 1)
            require(key.lower() not in headers, "readiness_failed")
            headers[key.lower()] = value.strip()
        require(
            headers.get("cseq") == "1" and headers.get("content-type") == "application/sdp",
            "readiness_failed",
        )
        size = headers.get("content-length", "")
        require(re.fullmatch(r"[0-9]{1,4}", size) is not None, "readiness_failed")
        length = int(size)
        require(0 < length <= 4096 and len(body) <= length, "readiness_failed")
        deadline = time.monotonic() + 0.5
        while len(body) < length:
            require(time.monotonic() < deadline, "readiness_failed")
            block = peer.recv(min(1024, length - len(body)))
            require(bool(block), "readiness_failed")
            body.extend(block)
        require(
            re.search(rb"(?mi)^a=rtpmap:96 H264/90000\r?$", body) is not None, "readiness_failed"
        )
        return True


def wait_ready(
    check: object, processes: list[OwnedProcess], seconds: float = 20, *, operation: str
) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for process in processes:
            process.ensure_running()
        try:
            if check():
                require(time.monotonic() < deadline, "readiness_failed")
                return
        except (OSError, urllib.error.URLError):
            pass
        except Exception as error:
            if isinstance(error, WitnessError) and error.diagnostic is not None:
                raise
            raise WitnessError(
                "readiness_failed", diagnostic(operation, category="readiness_failed")
            ) from None
        time.sleep(0.1)
    raise WitnessError(
        "readiness_failed",
        diagnostic(operation, outcome="timeout", timed_out=True, category="readiness_timeout"),
    )


def archive_command(repository: Path, output: Path, revision: str) -> list[str]:
    # Git archive performs checkout conversion unless overridden. Preserve exact
    # pinned LF bytes without changing the repository, user or system Git config.
    return [
        "git",
        "-c",
        "core.autocrlf=false",
        "-c",
        "core.eol=lf",
        "-C",
        str(repository),
        "archive",
        "--format=zip",
        f"--output={output}",
        revision,
    ]


def extract_archive(path: Path, target: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            relative = Path(info.filename)
            require(
                not relative.is_absolute()
                and ".." not in relative.parts
                and not stat.S_ISLNK(info.external_attr >> 16),
                "admission_failed",
            )
        archive.extractall(target)


def wheel_payload(wheel: Path) -> dict[str, str]:
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        require(len(set(names)) == len(names))
        result = {
            name: hashlib.sha256(archive.read(name)).hexdigest()
            for name in names
            if name.startswith("k5vision/") and not name.endswith("/")
        }
    require(bool(result) and "k5vision/analytics_config.py" in result)
    return result


def requirements(path: Path, *, target_platform: str | None = None) -> dict[str, str]:
    """Read exact pins and the one reviewed Windows-only pin, without resolution."""
    target_platform = sys.platform if target_platform is None else target_platform
    require(target_platform in ("win32", "linux", "darwin"), "admission_failed")
    result = {}
    seen = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        windows_only = line == 'pyreadline3==3.5.6; sys_platform == "win32"'
        if windows_only:
            line = "pyreadline3==3.5.6"
        match = re.fullmatch(r"([a-z0-9-]+)==([0-9]+(?:\.[0-9]+)+)", line)
        require(match is not None and match[1] not in seen, "admission_failed")
        seen.add(match[1])
        if not windows_only or target_platform == "win32":
            result[match[1]] = match[2]
    require(bool(result), "admission_failed")
    return result


def adopt_work_root(path: Path, temporary: Path, repo: Path, output: Path) -> Path:
    root = local_path(path.absolute(), directory=True)
    parent = local_path(temporary.absolute(), directory=True)
    require(
        root != parent
        and root.is_relative_to(parent)
        and not root.is_relative_to(repo)
        and not output.is_relative_to(root)
        and not any(root.iterdir()),
        "admission_failed",
    )
    return root


def execute(args: argparse.Namespace) -> int:
    identities = dict.fromkeys(IDENTITIES, "0" * 64)
    document: dict[str, object] = {
        "schema_version": "1",
        "revision": args.revision,
        "analytics_revision": ANALYTICS_REVISION,
        "execution_context": "installed-normal-app-reviewed-loopback-windows-x64",
        "completed": False,
        "cleanup_complete": False,
        "service_token_rejected": False,
        "stage": "admission",
        "failure_code": "none",
        **identities,
    }
    for attempt in (1, 2):
        for key in RUN_FIELDS:
            document[f"run_{attempt}_{key}"] = (
                False if key in {"completed", "analytics_enabled"} else 0
            )
    owned: list[OwnedProcess] = []
    failure_detail = None
    operation = "driver_admission"
    work: Path | None = None
    output = args.output.absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.unlink(missing_ok=True)  # A failed retry never exposes an earlier success.
    try:
        require(os.name == "nt" and sys.version_info[:2] == (3, 12), "admission_failed")
        require(re.fullmatch(r"[0-9a-f]{40}", args.revision) is not None, "admission_failed")
        repo = local_path(args.repo.absolute(), directory=True)
        analytics = local_path(args.analytics_source.absolute(), directory=True)
        root = local_path(Path(os.environ["K5_ANALYTICS_EVIDENCE_ROOT"]), directory=True)
        gst = local_path(Path(os.environ["K5_GSTREAMER_ROOT"]), directory=True)
        mediamtx_root = local_path(
            Path(os.environ["LOCALAPPDATA"]) / "K5RunnerTools" / "mediamtx" / "1.21.1",
            directory=True,
        )
        mediamtx = local_path(mediamtx_root / "mediamtx.exe")
        require((mediamtx_root / "k5-archive.sha256").read_text().strip() == MEDIA_MTX_ARCHIVE)
        require((mediamtx_root / "k5-exe.sha256").read_text().strip() == file_hash(mediamtx))
        work = adopt_work_root(args.work_root, args.temp_root, repo, output)
        env = clean_environment(dict(os.environ))
        env.update(
            TEMP=str(work),
            TMP=str(work),
            RUNNER_TEMP=str(work),
            PIP_CACHE_DIR=str(work / "pip-cache"),
        )
        source = work / "source"
        archive = work / "candidate.zip"
        # The archive binds all built K5 bytes to the requested immutable candidate,
        # unaffected by dirty/untracked checkout files, editable installs or cwd.
        document["stage"] = "build"
        operation = "archive_candidate"
        operation = "archive_candidate"
        run(
            archive_command(repo, archive, args.revision),
            cwd=work,
            env=env,
            operation="archive_candidate",
        )
        extract_archive(archive, source)
        # A preceding seed-preparation step may have produced __pycache__ in the
        # donor checkout. Export exact tracked bytes, never delete another step's
        # work or smuggle those caches into the reproducible wrapper.
        donor_archive, donor_source = work / "analytics.zip", work / "analytics-source"
        operation = "archive_analytics"
        run(
            archive_command(analytics, donor_archive, ANALYTICS_REVISION),
            cwd=work,
            env=env,
            operation="archive_analytics",
        )
        extract_archive(donor_archive, donor_source)
        analytics = donor_source
        operation = "verify_identity"
        driver = work / "installed_analytics_witness.py"
        shutil.copyfile(source / "scripts" / "installed_analytics_witness.py", driver)
        require(
            driver.read_bytes().replace(b"\r\n", b"\n")
            == Path(__file__).read_bytes().replace(b"\r\n", b"\n")
        )
        python = work / "venv" / "Scripts" / "python.exe"
        operation = "create_venv"
        run(
            [sys.executable, "-I", "-B", "-m", "venv", str(work / "venv")],
            cwd=work,
            env=env,
            operation="create_venv",
        )
        wheels = work / "wheels"
        wheels.mkdir()
        operation = "build_k5_wheel"
        run(
            [
                str(python),
                "-I",
                "-B",
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--wheel-dir",
                str(wheels),
                str(source),
            ],
            cwd=work,
            env=env,
            seconds=180,
            operation="build_k5_wheel",
        )
        k5_wheels = list(wheels.glob("k5_vision-*.whl"))
        require(len(k5_wheels) == 1)
        operation = "build_analytics_wheel"
        run(
            [
                str(python),
                "-I",
                "-B",
                str(source / "scripts/build_analytics_runtime_wheel.py"),
                "--source-root",
                str(analytics),
                "--output-dir",
                str(wheels),
            ],
            cwd=work,
            env=env,
            operation="build_analytics_wheel",
        )
        analytics_wheels = list(wheels.glob("k5_analytics_runtime-*.whl"))
        require(len(analytics_wheels) == 1)
        # Independently rebuild to establish wrapper determinism on this host.
        repeat = work / "repeat"
        operation = "rebuild_analytics_wheel"
        run(
            [
                str(python),
                "-I",
                "-B",
                str(source / "scripts/build_analytics_runtime_wheel.py"),
                "--source-root",
                str(analytics),
                "--output-dir",
                str(repeat),
            ],
            cwd=work,
            env=env,
            operation="rebuild_analytics_wheel",
        )
        require(file_hash(analytics_wheels[0]) == file_hash(repeat / analytics_wheels[0].name))
        payload = wheel_payload(k5_wheels[0])
        identities.update(
            k5_payload_sha256=digest(payload),
            k5_wheel_sha256=file_hash(k5_wheels[0]),
            analytics_wheel_sha256=file_hash(analytics_wheels[0]),
            analytics_manifest_sha256=file_hash(
                source / "src/k5vision/data/analytics-runtime-manifest.json"
            ),
        )
        document["stage"] = "install"
        operation = "install_dependencies"
        base = source / "scripts/windows-alpha/runtime-requirements.txt"
        versions = {**requirements(base), **RUNTIME_VERSIONS, **WINDOWS_RUNTIME_VERSIONS}
        constraints = work / "constraints.txt"
        constraints.write_text(
            "\n".join(f"{name}=={version}" for name, version in versions.items())
        )
        operation = "install_dependencies"
        run(
            [
                str(python),
                "-I",
                "-B",
                "-m",
                "pip",
                "install",
                "--no-compile",
                "--constraint",
                str(constraints),
                "-r",
                str(base),
                *[
                    f"{name}=={version}"
                    for name, version in {**RUNTIME_VERSIONS, **WINDOWS_RUNTIME_VERSIONS}.items()
                ],
            ],
            cwd=work,
            env=env,
            seconds=300,
            operation="install_dependencies",
        )
        operation = "install_local_wheels"
        run(
            [
                str(python),
                "-I",
                "-B",
                "-m",
                "pip",
                "install",
                "--no-index",
                "--no-deps",
                "--no-compile",
                str(k5_wheels[0]),
                str(analytics_wheels[0]),
            ],
            cwd=work,
            env=env,
            operation="install_local_wheels",
        )
        operation = "check_dependencies"
        run(
            [str(python), "-I", "-B", "-m", "pip", "check"],
            cwd=work,
            env=env,
            operation="check_dependencies",
        )
        config = work / "analytics.json"
        config.write_bytes(
            canonical(
                {
                    "schema_version": 1,
                    "provider": "analytics-lab-omz-person-v1",
                    "source_revision": ANALYTICS_REVISION,
                    "artifact_root": str(root / "artifacts"),
                }
            )
        )
        env["K5_ANALYTICS_CONFIG"] = str(config)
        probe_inputs, probe_output = work / "probe-inputs.json", work / "probe-output.json"
        probe_inputs.write_bytes(
            canonical(
                {"k5_payload": payload, "runtime_versions": versions, "evidence_root": str(root)}
            )
        )
        document["stage"] = "probe"
        operation = "probe_before"
        operation = "probe_before"
        run(
            [
                str(python),
                "-I",
                "-B",
                str(driver),
                "--probe",
                "--inputs",
                str(probe_inputs),
                "--output",
                str(probe_output),
            ],
            cwd=work,
            env=env,
            seconds=120,
            operation="probe_before",
        )
        checked = read_json(probe_output)
        require(
            checked.keys()
            == {"runtime_identity_sha256", "model_identity_sha256", "seed_identity_sha256"}
        )
        identities.update(checked)
        native_files = [
            local_path(gst / "bin" / name) for name in ("gst-launch-1.0.exe", "gst-inspect-1.0.exe")
        ]
        native_files.append(mediamtx)
        require((gst / "k5-installer.sha256").read_text().strip() == GSTREAMER_INSTALLER)
        native_hashes = {
            "gstreamer-installer": GSTREAMER_INSTALLER,
            "gstreamer-launch": file_hash(native_files[0]),
            "gstreamer-inspect": file_hash(native_files[1]),
            "mediamtx": file_hash(mediamtx),
        }
        env.update(
            K5_GSTREAMER_ROOT=str(gst),
            GST_REGISTRY_1_0=str(work / "gst-registry.bin"),
            GST_PLUGIN_PATH_1_0="",
            GST_PLUGIN_PATH="",
            GST_PLUGIN_SYSTEM_PATH_1_0=str(gst / "lib/gstreamer-1.0"),
            GST_PLUGIN_SYSTEM_PATH=str(gst / "lib/gstreamer-1.0"),
            GIO_USE_PROXY_RESOLVER="dummy",
            GIO_MODULE_DIR=str(work / "gio-modules"),
            PATH=str(gst / "bin") + os.pathsep + env.get("PATH", ""),
        )
        (work / "gio-modules").mkdir()
        for arguments, expected in (
            ([str(native_files[1]), "--version"], "1.28.7"),
            ([str(mediamtx), "--version"], "1.21.1"),
        ):
            operation = (
                "inspect_gstreamer_version" if expected == "1.28.7" else "inspect_mediamtx_version"
            )
            version = capture(
                arguments,
                cwd=work,
                env=env,
                limit=4096,
                operation=(
                    "inspect_gstreamer_version"
                    if expected == "1.28.7"
                    else "inspect_mediamtx_version"
                ),
            ).decode()
            require(re.search(r"(?<![0-9.])" + re.escape(expected) + r"(?![0-9.])", version))
        for element, expected in GSTREAMER_ELEMENTS.items():
            operation = f"inspect_{element}"
            raw = capture(
                [str(native_files[1]), element], cwd=work, env=env, operation=f"inspect_{element}"
            )
            native_hashes[element] = inspect_plugin(raw, expected, gst)
        libraries = sorted((gst / "bin").glob("*.dll"))
        require(bool(libraries))
        for library in libraries:
            native_hashes["bin/" + library.name] = file_hash(local_path(library))
        identities["native_identity_sha256"] = digest(native_hashes)
        document["stage"] = "fixture"
        operation = "wait_rtsp_publication"
        port, rtsp_port = free_port(), free_port()
        require(port != rtsp_port, "port_occupied")
        require_free_port(port)
        require_free_port(rtsp_port)
        source_uri = f"rtsp://127.0.0.1:{rtsp_port}/k5reviewed"
        server_config = work / "mediamtx.yml"
        server_config.write_text(
            "\n".join(
                [
                    "logLevel: warn",
                    "api: false",
                    "metrics: false",
                    "pprof: false",
                    "playback: false",
                    "rtsp: true",
                    "rtspTransports: [tcp]",
                    f"rtspAddress: 127.0.0.1:{rtsp_port}",
                    "rtmp: false",
                    "hls: false",
                    "webrtc: false",
                    "srt: false",
                    "moq: false",
                    "paths:",
                    "  k5reviewed:",
                    "",
                ]
            )
        )
        server = OwnedProcess(
            [str(mediamtx), str(server_config)], cwd=work, env=env, operation="start_rtsp_server"
        )
        owned.append(server)
        wait_ready(
            lambda: rtsp_listener_ready(rtsp_port), [server], 10, operation="wait_rtsp_server"
        )
        publication = work / "publication.json"
        publication.write_bytes(
            canonical(
                {
                    "evidence_root": str(root),
                    "source": source_uri,
                    "gst_launch": str(native_files[0]),
                }
            )
        )
        publisher = OwnedProcess(
            [str(python), "-I", "-B", str(driver), "--publish", "--inputs", str(publication)],
            cwd=work,
            env=env,
            operation="start_publisher",
        )
        owned.append(publisher)
        wait_ready(lambda: rtsp_ready(rtsp_port), owned, 20, operation="wait_rtsp_publication")
        document["stage"] = "app"
        operation = "wait_application"
        admin, service = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        env.update(
            K5_CONTROL_PLANE_SITE_ID="witness-" + secrets.token_hex(16),
            K5_DEVICE_DB_PATH=str(work / "devices.sqlite3"),
            K5_USER_DB_PATH=str(work / "users.sqlite3"),
            K5_CONTROL_PLANE_TOKEN=service,
            K5_CONTROL_PLANE_ADMIN_TOKEN=admin,
            K5_LOCAL_TEST_RTSP_SOURCE=source_uri,
            K5_OPERATOR_STREAM_TOKEN="local-test",
            K5_OPERATOR_RTP_PAYLOAD_TYPE="96",
        )
        app = OwnedProcess(
            [
                str(python),
                "-I",
                "-B",
                "-m",
                "k5vision.cli",
                "serve",
                "--operator",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "critical",
            ],
            cwd=work,
            env=env,
            operation="start_application",
        )
        owned.append(app)
        wait_ready(lambda: health_ready(port), owned, 30, operation="wait_application")
        document["stage"] = "auth"
        operation = "authenticate"
        session, body = authenticate(port, admin, service, rtsp_port)
        document["service_token_rejected"] = True
        for attempt in (1, 2):
            document["stage"] = f"live_{attempt}"
            operation = f"launch_{attempt}"
            code, live = api(port, "/api/v1/operator/live", body=body, token=session, timeout=75)
            require(code == 200, "receipt_invalid")
            metrics = validate_live_receipt(live)
            # The unmodified local-test runtime promises 225 frames per launch.
            require(
                metrics["delivered_frames"] == 225 and metrics["presentations"] >= 225,
                "receipt_invalid",
            )
            document.update({f"run_{attempt}_{key}": value for key, value in metrics.items()})
            for item in owned:
                item.ensure_running()
        document["stage"] = "verify"
        operation = "verify_identity"
        # Re-admit installed payload/models/dependencies after both launches.
        after = work / "probe-after.json"
        operation = "probe_after"
        run(
            [
                str(python),
                "-I",
                "-B",
                str(driver),
                "--probe",
                "--inputs",
                str(probe_inputs),
                "--output",
                str(after),
            ],
            cwd=work,
            env=env,
            seconds=120,
            operation="probe_after",
        )
        require(read_json(after) == checked)
        document["completed"] = True
    except BaseException as error:
        document["failure_code"] = str(error) if isinstance(error, WitnessError) else "unexpected"
        failure_detail = error.diagnostic if isinstance(error, WitnessError) else None
        if failure_detail is None:
            category = (
                str(error)
                if isinstance(error, WitnessError)
                else classify_error(str(error)[:_MAX_STDERR_CLASSIFIED].encode())
            )
            timed_out = isinstance(error, TimeoutError) or (
                isinstance(error, urllib.error.URLError) and isinstance(error.reason, TimeoutError)
            )
            failure_detail = diagnostic(
                operation,
                category=category,
                timed_out=timed_out,
                outcome="timeout" if timed_out else "failed",
            )
    finally:
        cleanup = True
        cleanup_detail = None
        for process in reversed(owned):
            try:
                process.close()
            except BaseException as error:
                cleanup = False
                if cleanup_detail is None:
                    cleanup_detail = (
                        error.diagnostic if isinstance(error, WitnessError) else None
                    ) or diagnostic(
                        process.operation, outcome="cleanup_failed", category="cleanup_incomplete"
                    )
        if work is not None:
            try:
                shutil.rmtree(work)
            except OSError:
                cleanup = False
            cleanup = cleanup and not work.exists()
        document.update(identities)
        document["cleanup_complete"] = cleanup
        if not cleanup:
            document.update(completed=False, failure_code="cleanup_incomplete")
            if failure_detail is not None:
                emit_diagnostic(failure_detail)
            failure_detail = cleanup_detail or diagnostic(
                "cleanup_owned", outcome="cleanup_failed", category="cleanup_incomplete"
            )
        if document["completed"]:
            document["stage"] = "complete"
        elif failure_detail is not None:
            emit_diagnostic(failure_detail)
        validate_receipt(
            document, revision=args.revision, identities=identities, success=document["completed"]
        )
        output.write_bytes(canonical(document) + b"\n")
    print(
        "Installed analytics witness passed"
        if document["completed"]
        else f"Installed analytics witness failed: {document['stage']}/{document['failure_code']}"
    )
    return 0 if document["completed"] else 1


def gated_child(arguments: list[str]) -> int:
    # No requested executable can start before its stdlib-only parent belongs
    # to the freshly-created Job. EOF/failed assignment never opens the gate.
    if len(arguments) < 2 or re.fullmatch(r"[0-9a-f]{32}", arguments[0]) is None:
        return 1
    marker = "K5_GATE_" + arguments[0] + "_"
    arguments = arguments[1:]
    if sys.stdin.buffer.read(1) != b"1":
        print("\n" + marker + "STATE=refused", file=sys.stderr, flush=True)
        return 1
    print("\n" + marker + "STATE=opened", file=sys.stderr, flush=True)
    try:
        child = subprocess.Popen(arguments, stdin=subprocess.DEVNULL)
    except OSError as error:
        print("\n" + marker + "STATE=launch_failed", file=sys.stderr, flush=True)
        category = classify_error(str(error)[:_MAX_STDERR_CLASSIFIED].encode())
        print("K5_CHILD_CATEGORY=" + category, file=sys.stderr, flush=True)
        return 1
    print("\n" + marker + "STATE=started", file=sys.stderr, flush=True)
    code = child.wait()
    if not -(2**31) <= code < 2**32:
        return 1
    print("\n" + marker + f"EXIT={code}", file=sys.stderr, flush=True)
    return code


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--gate":
        return gated_child(sys.argv[2:])
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--probe", action="store_true")
    modes.add_argument("--publish", action="store_true")
    modes.add_argument("--validate-receipt", action="store_true")
    parser.add_argument("--inputs", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--analytics-source", type=Path)
    parser.add_argument("--revision")
    parser.add_argument("--temp-root", type=Path)
    parser.add_argument("--work-root", type=Path)
    args = parser.parse_args()
    try:
        if args.validate_receipt:
            document = read_json(args.output)
            validate_receipt(
                document,
                revision=args.revision,
                identities={key: document.get(key) for key in IDENTITIES},
            )
            for attempt in (1, 2):
                require(
                    document[f"run_{attempt}_delivered_frames"] == 225
                    and document[f"run_{attempt}_presentations"] >= 225,
                    "receipt_invalid",
                )
            print("Installed analytics receipt validation passed")
            return 0
        if args.probe:
            installed_probe(args.inputs, args.output)
            return 0
        if args.publish:
            publish(args.inputs)
            return 0
        require(
            args.repo is not None
            and args.analytics_source is not None
            and args.output is not None
            and args.revision is not None
            and args.work_root is not None
            and args.temp_root is not None,
            "admission_failed",
        )
        return execute(args)
    except BaseException as error:
        detail = error.diagnostic if isinstance(error, WitnessError) else None
        category = (
            detail["category"]
            if detail is not None
            else str(error)
            if isinstance(error, WitnessError)
            else classify_error(str(error)[:_MAX_STDERR_CLASSIFIED].encode())
        )
        if args.probe or args.publish:
            print("K5_CHILD_CATEGORY=" + category, file=sys.stderr, flush=True)
        else:
            emit_diagnostic(
                detail
                or diagnostic(
                    "receipt_validate" if args.validate_receipt else "driver_admission",
                    category=category,
                )
            )
        print("Installed analytics witness failed closed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
