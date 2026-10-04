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


class WitnessError(RuntimeError):
    """A fixed project-owned code, never an exception from native code or a URL."""

    def __init__(self, code: str) -> None:
        super().__init__(code if code in FAILURES else "unexpected")


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

    def close(self) -> None:
        # Basic accounting has four LARGE_INTEGER values followed by four DWORDs;
        # ActiveProcesses is the third DWORD. Wait for descendants, not just root.
        counters = (ctypes.c_uint64 * 6)()
        deadline = time.monotonic() + 5
        try:
            require(bool(self.api.TerminateJobObject(self.handle, 1)), "cleanup_incomplete")
            while time.monotonic() < deadline:
                require(
                    bool(
                        self.api.QueryInformationJobObject(
                            self.handle, 1, ctypes.byref(counters), ctypes.sizeof(counters), None
                        )
                    ),
                    "cleanup_incomplete",
                )
                active = ctypes.cast(ctypes.byref(counters, 40), ctypes.POINTER(ctypes.c_uint32))[0]
                if active == 0:
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
        stdout: object = subprocess.DEVNULL,
    ) -> None:
        self.job = WindowsJob() if os.name == "nt" else None
        self.process: subprocess.Popen[bytes] | None = None
        try:
            command = (
                [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--gate", *arguments]
                if self.job is not None
                else arguments
            )
            self.process = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                stdin=subprocess.PIPE if self.job is not None else subprocess.DEVNULL,
                stdout=stdout,
                stderr=subprocess.DEVNULL,
            )
            if self.job is not None:
                self.job.assign(self.process)
                self.process.stdin.write(b"1")
                self.process.stdin.flush()
                self.process.stdin.close()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        process, self.process = self.process, None
        job, self.job = self.job, None
        try:
            if job is not None:
                job.close()
            elif process is not None and process.poll() is None:
                process.terminate()
        finally:
            if process is not None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        raise WitnessError("cleanup_incomplete") from None

    def wait(self, seconds: float) -> None:
        require(self.process is not None, "child_failed")
        try:
            code = self.process.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            raise WitnessError("child_timeout") from None
        require(code == 0, "child_failed")


def run(
    arguments: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    seconds: float = 90,
    stdout: object = subprocess.DEVNULL,
) -> None:
    owned = OwnedProcess(arguments, cwd=cwd, env=env, stdout=stdout)
    try:
        owned.wait(seconds)
    finally:
        owned.close()


def capture(arguments: list[str], *, cwd: Path, env: dict[str, str], limit: int = 131_072) -> bytes:
    """Bounded in-memory diagnostics, never persisted or printed."""
    owned = OwnedProcess(arguments, cwd=cwd, env=env, stdout=subprocess.PIPE)
    result: list[bytes] = []

    def read() -> None:
        result.append(owned.process.stdout.read(limit + 1))

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        reader.join(15)
        require(not reader.is_alive(), "child_timeout")
        require(len(result) == 1 and len(result[0]) <= limit, "output_invalid")
        owned.wait(5)
        return result[0]
    finally:
        owned.close()
        reader.join(5)
        require(not reader.is_alive(), "cleanup_incomplete")


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
        child = subprocess.Popen(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        peer, address = listener.accept()
        require(address[0] == "127.0.0.1", "admission_failed")
        peer.settimeout(10)
        listener.close()
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            require(child.poll() is None, "child_failed")
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
            if child.poll() is None:
                child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)


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


def wait_ready(check: object, processes: list[OwnedProcess], seconds: float = 20) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        require(
            all(p.process is not None and p.process.poll() is None for p in processes),
            "readiness_failed",
        )
        try:
            if check():
                require(time.monotonic() < deadline, "readiness_failed")
                return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.1)
    raise WitnessError("readiness_failed")


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


def requirements(path: Path) -> dict[str, str]:
    result = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        match = re.fullmatch(r"([a-z0-9-]+)==([0-9]+(?:\.[0-9]+)+)", line)
        require(match is not None and match[1] not in result, "admission_failed")
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
        run(
            [
                "git",
                "-C",
                str(repo),
                "archive",
                "--format=zip",
                f"--output={archive}",
                args.revision,
            ],
            cwd=work,
            env=env,
        )
        extract_archive(archive, source)
        # A preceding seed-preparation step may have produced __pycache__ in the
        # donor checkout. Export exact tracked bytes, never delete another step's
        # work or smuggle those caches into the reproducible wrapper.
        donor_archive, donor_source = work / "analytics.zip", work / "analytics-source"
        run(
            [
                "git",
                "-C",
                str(analytics),
                "archive",
                "--format=zip",
                f"--output={donor_archive}",
                ANALYTICS_REVISION,
            ],
            cwd=work,
            env=env,
        )
        extract_archive(donor_archive, donor_source)
        analytics = donor_source
        driver = work / "installed_analytics_witness.py"
        shutil.copyfile(source / "scripts" / "installed_analytics_witness.py", driver)
        require(
            driver.read_bytes().replace(b"\r\n", b"\n")
            == Path(__file__).read_bytes().replace(b"\r\n", b"\n")
        )
        python = work / "venv" / "Scripts" / "python.exe"
        run([sys.executable, "-I", "-B", "-m", "venv", str(work / "venv")], cwd=work, env=env)
        wheels = work / "wheels"
        wheels.mkdir()
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
        )
        k5_wheels = list(wheels.glob("k5_vision-*.whl"))
        require(len(k5_wheels) == 1)
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
        )
        analytics_wheels = list(wheels.glob("k5_analytics_runtime-*.whl"))
        require(len(analytics_wheels) == 1)
        # Independently rebuild to establish wrapper determinism on this host.
        repeat = work / "repeat"
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
        base = source / "scripts/windows-alpha/runtime-requirements.txt"
        versions = {**requirements(base), **RUNTIME_VERSIONS, **WINDOWS_RUNTIME_VERSIONS}
        constraints = work / "constraints.txt"
        constraints.write_text(
            "\n".join(f"{name}=={version}" for name, version in versions.items())
        )
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
        )
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
        )
        run([str(python), "-I", "-B", "-m", "pip", "check"], cwd=work, env=env)
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
            version = capture(arguments, cwd=work, env=env, limit=4096).decode()
            require(re.search(r"(?<![0-9.])" + re.escape(expected) + r"(?![0-9.])", version))
        for element, expected in GSTREAMER_ELEMENTS.items():
            raw = capture([str(native_files[1]), element], cwd=work, env=env)
            native_hashes[element] = inspect_plugin(raw, expected, gst)
        libraries = sorted((gst / "bin").glob("*.dll"))
        require(bool(libraries))
        for library in libraries:
            native_hashes["bin/" + library.name] = file_hash(local_path(library))
        identities["native_identity_sha256"] = digest(native_hashes)
        document["stage"] = "fixture"
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
        server = OwnedProcess([str(mediamtx), str(server_config)], cwd=work, env=env)
        owned.append(server)
        wait_ready(lambda: rtsp_listener_ready(rtsp_port), [server], 10)
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
        )
        owned.append(publisher)
        wait_ready(lambda: rtsp_ready(rtsp_port), owned, 20)
        document["stage"] = "app"
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
        )
        owned.append(app)
        wait_ready(lambda: health_ready(port), owned, 30)
        document["stage"] = "auth"
        session, body = authenticate(port, admin, service, rtsp_port)
        document["service_token_rejected"] = True
        for attempt in (1, 2):
            document["stage"] = f"live_{attempt}"
            code, live = api(port, "/api/v1/operator/live", body=body, token=session, timeout=75)
            require(code == 200, "receipt_invalid")
            metrics = validate_live_receipt(live)
            # The unmodified local-test runtime promises 225 frames per launch.
            require(
                metrics["delivered_frames"] == 225 and metrics["presentations"] >= 225,
                "receipt_invalid",
            )
            document.update({f"run_{attempt}_{key}": value for key, value in metrics.items()})
            require(all(item.process.poll() is None for item in owned), "child_failed")
        document["stage"] = "verify"
        # Re-admit installed payload/models/dependencies after both launches.
        after = work / "probe-after.json"
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
        )
        require(read_json(after) == checked)
        document["completed"] = True
    except BaseException as error:
        document["failure_code"] = str(error) if isinstance(error, WitnessError) else "unexpected"
    finally:
        cleanup = True
        for process in reversed(owned):
            try:
                process.close()
            except BaseException:
                cleanup = False
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
        if document["completed"]:
            document["stage"] = "complete"
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
    if not arguments or sys.stdin.buffer.read(1) != b"1":
        return 1
    return subprocess.call(arguments, stdin=subprocess.DEVNULL)


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
    except BaseException:
        print("Installed analytics witness failed closed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
