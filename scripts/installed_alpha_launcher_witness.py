#!/usr/bin/env python3
"""Owned installed-layout/Start-script engineering witness, not installer acceptance.

The generated ball establishes launcher/provider continuity, never person boxes.
The original person-clip app command/acceptance gates and production scripts stay unchanged.
"""

from __future__ import annotations

import argparse
import ctypes
import importlib.util
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "_installed_alpha_common", Path(__file__).with_name("installed_analytics_witness.py")
)
assert _SPEC is not None and _SPEC.loader is not None
common = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(common)

RECEIPT_NAME = "installed-alpha-start-script-witness.json"
EXPECTATIONS_NAME = "installed-alpha-start-script-expectations.json"
SCHEMA = "installed-alpha-start-script-v1"
SCOPE = "installed-layout-start-script-engineering-only"
GSTREAMER_VERSION = "1.28.7"
SCRIPT_NAMES = ("Start-K5VisionAlpha.ps1", "Test-K5VisionAlpha.ps1", "Run-K5VisionAlpha.ps1")
IDENTITIES = {
    "source_tree_sha256",
    "k5_payload_sha256",
    "k5_wheel_sha256",
    "analytics_manifest_sha256",
    "analytics_wheel_sha256",
    "runtime_identity_sha256",
    "model_identity_sha256",
    "seed_identity_sha256",
    "native_cache_sha256",
    "wheelhouse_sha256",
    "start_script_sha256",
    "test_script_sha256",
    "run_script_sha256",
}
# Installed RECORD digests can depend on the disposable venv path. Every other
# identity is admitted before installation; this one is produced by the isolated
# RECORD probe, checked again afterwards, and independently bound at validation.
INPUT_IDENTITIES = IDENTITIES - {"runtime_identity_sha256"}
COUNTERS = {
    "delivered_frames",
    "presentations",
    "analytics_provider_submissions",
    "analytics_provider_completions",
    "analytics_failures",
}
RUN_BOOLEANS = {"completed", "analytics_enabled", "cleanup_complete"}
BOOLEANS = {
    "completed",
    "cleanup_complete",
    "invalid_config_refused",
    "invalid_config_no_session",
    "invalid_config_no_media",
    "person_box_acceptance",
}
STAGES = {
    "admission",
    "build",
    "install",
    "probe",
    "invalid_config",
    "launch_1",
    "launch_2",
    "verify",
    "cleanup",
    "complete",
}
FIELDS = (
    IDENTITIES
    | BOOLEANS
    | {
        "schema_version",
        "revision",
        "analytics_revision",
        "run_nonce",
        "acceptance_scope",
        "fixture",
        "execution_context",
        "stage",
        "failure_code",
    }
    | {f"run_{attempt}_{name}" for attempt in (1, 2) for name in COUNTERS | RUN_BOOLEANS}
)
REFUSAL = b"K5_ALPHA_EXPECTED_CONFIG_REFUSAL"
# This envelope does not replace, dot-source, patch, or intercept Start's work.
# It only maps the reviewed fixed refusal to a bounded scalar exit/marker.
ENVELOPE = """param([string]$Start, [int]$Port)
$ErrorActionPreference = "Stop"
try {
    & $Start -Port $Port -ExitAfterPublicTest
    exit 0
} catch {
    if ($_.Exception.Message -ceq "K5 analytics preflight failed. No alpha session was started.") {
        Write-Output "K5_ALPHA_EXPECTED_CONFIG_REFUSAL"
        exit 23
    }
    Write-Output "K5_ALPHA_START_FAILED"
    exit 24
}
"""


def validate_expectations(value: object, *, installed: bool = True) -> dict[str, str]:
    names = IDENTITIES if installed else INPUT_IDENTITIES
    common.require(
        type(value) is dict and value.keys() == names | {"revision", "run_nonce"}, "receipt_invalid"
    )
    for name, length in [("revision", 40), ("run_nonce", 32), *[(key, 64) for key in names]]:
        common.require(
            type(value[name]) is str
            and re.fullmatch(r"[0-9a-f]{" + str(length) + "}", value[name]) is not None
            and value[name] != "0" * length,
            "receipt_invalid",
        )
    return value


def new_receipt(expected: dict[str, str]) -> dict[str, object]:
    return {
        "schema_version": SCHEMA,
        "revision": expected["revision"],
        "analytics_revision": common.ANALYTICS_REVISION,
        "run_nonce": expected["run_nonce"],
        "acceptance_scope": SCOPE,
        "fixture": "generated-ball",
        "execution_context": "owned-installed-start-script-windows-x64",
        "stage": "admission",
        "failure_code": "none",
        **dict.fromkeys(IDENTITIES, "0" * 64),
        **{key: expected[key] for key in IDENTITIES if key in expected},
        **dict.fromkeys(BOOLEANS, False),
        **{
            f"run_{attempt}_{key}": False if key in RUN_BOOLEANS else 0
            for attempt in (1, 2)
            for key in COUNTERS | RUN_BOOLEANS
        },
    }


def validate_run(value: dict[str, object]) -> None:
    common.require(value.keys() == COUNTERS | RUN_BOOLEANS, "receipt_invalid")
    for name in RUN_BOOLEANS:
        common.require(value[name] is True, "receipt_invalid")
    for name in COUNTERS:
        common.require(
            type(value[name]) is int and 0 <= value[name] <= common.MAX_COUNTER, "receipt_invalid"
        )
    common.require(
        value["delivered_frames"] == 225
        and value["presentations"] >= 225
        and 0 < value["analytics_provider_completions"] <= value["analytics_provider_submissions"]
        and value["analytics_failures"] == 0,
        "receipt_invalid",
    )


def validate_receipt(value: object, expected: dict[str, str], *, success: bool = True) -> None:
    validate_expectations(expected)
    common.require(type(value) is dict and value.keys() == FIELDS, "receipt_invalid")
    for name, exact in {
        "schema_version": SCHEMA,
        "analytics_revision": common.ANALYTICS_REVISION,
        "acceptance_scope": SCOPE,
        "fixture": "generated-ball",
        "execution_context": "owned-installed-start-script-windows-x64",
        **expected,
    }.items():
        common.require(type(value[name]) is str and value[name] == exact, "receipt_invalid")
    common.require(
        type(value["stage"]) is str
        and value["stage"] in STAGES
        and type(value["failure_code"]) is str
        and value["failure_code"] in common.FAILURES,
        "receipt_invalid",
    )
    for name in BOOLEANS:
        common.require(type(value[name]) is bool, "receipt_invalid")
    common.require(value["person_box_acceptance"] is False, "receipt_invalid")
    for attempt in (1, 2):
        run = {name: value[f"run_{attempt}_{name}"] for name in COUNTERS | RUN_BOOLEANS}
        for name in RUN_BOOLEANS:
            common.require(type(run[name]) is bool, "receipt_invalid")
        for name in COUNTERS:
            common.require(
                type(run[name]) is int and 0 <= run[name] <= common.MAX_COUNTER, "receipt_invalid"
            )
        if success:
            validate_run(run)
    if success:
        common.require(
            all(value[key] is True for key in BOOLEANS - {"person_box_acceptance"})
            and value["stage"] == "complete"
            and value["failure_code"] == "none",
            "receipt_invalid",
        )
    else:
        common.require(
            value["completed"] is False and value["failure_code"] != "none", "receipt_invalid"
        )


def tree_manifest(root: Path, *, maximum_files: int = 50_000) -> dict[str, str]:
    """No links/junctions; the full inventory (including extra files) is bound."""
    root = common.local_path(root, directory=True)
    result: dict[str, str] = {}
    total = 0
    for current, directories, files in os.walk(root, followlinks=False):
        for name in sorted(directories):
            common.local_path(Path(current) / name, directory=True)
        for name in sorted(files):
            path = common.local_path(Path(current) / name)
            total += path.stat().st_size
            common.require(len(result) < maximum_files and total <= 8 * 1024**3, "admission_failed")
            result[path.relative_to(root).as_posix()] = common.file_hash(path)
    common.require(bool(result), "admission_failed")
    return result


def native_roots(local_appdata: Path) -> tuple[Path, Path]:
    tools = local_appdata / "K5RunnerTools"
    return (
        tools / "k5-gstreamer" / GSTREAMER_VERSION / "msvc_x86_64",
        tools / "mediamtx" / "1.21.1",
    )


def cache_marker(path: Path) -> str:
    path = common.local_path(path)
    common.require(path.stat().st_size <= 128, "identity_mismatch")
    return path.read_bytes().decode("ascii").strip()


def native_manifest(local_appdata: Path) -> dict[str, object]:
    gst, mtx = native_roots(local_appdata)
    inventories = {"gstreamer": tree_manifest(gst), "mediamtx": tree_manifest(mtx)}
    common.require(cache_marker(gst / "k5-installer.sha256") == common.GSTREAMER_INSTALLER)
    common.require(cache_marker(mtx / "k5-archive.sha256") == common.MEDIA_MTX_ARCHIVE)
    common.require(cache_marker(mtx / "k5-exe.sha256") == common.file_hash(mtx / "mediamtx.exe"))
    for name in ("gst-launch-1.0.exe", "gst-inspect-1.0.exe"):
        common.local_path(gst / "bin" / name)
    common.require(
        any(
            (gst / "bin" / name).is_file()
            for name in ("gstreamer-1.0-0.dll", "libgstreamer-1.0-0.dll")
        )
    )
    return inventories


def copy_native_cache(original: Path, destination: Path, expected: str) -> None:
    common.require(common.digest(native_manifest(original)) == expected)
    for source, target in zip(native_roots(original), native_roots(destination), strict=True):
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target)
    common.require(common.digest(native_manifest(destination)) == expected)
    # Detect a shared input changing during the copy without repairing/deleting it.
    common.require(common.digest(native_manifest(original)) == expected)


def source_payload(source: Path) -> dict[str, str]:
    return {
        "k5vision/" + name: value for name, value in tree_manifest(source / "src/k5vision").items()
    }


def install_scripts(source: Path, installed: Path, revision: str) -> dict[str, str]:
    identities = {}
    for name, key in zip(SCRIPT_NAMES, ("start", "test", "run"), strict=True):
        origin, target = source / "scripts/windows-alpha" / name, installed / name
        shutil.copyfile(common.local_path(origin), target)
        common.require(target.read_bytes() == origin.read_bytes())
        identities[f"{key}_script_sha256"] = common.file_hash(target)
    (installed / "k5-revision.txt").write_bytes(revision.encode("ascii"))
    (installed / "gstreamer-version.txt").write_bytes(GSTREAMER_VERSION.encode("ascii"))
    return identities


def verify_layout(installed: Path, expected: dict[str, str]) -> None:
    for name, key in zip(SCRIPT_NAMES, ("start", "test", "run"), strict=True):
        common.require(
            common.file_hash(common.local_path(installed / name))
            == expected[f"{key}_script_sha256"]
        )
    common.require((installed / "k5-revision.txt").read_bytes() == expected["revision"].encode())
    common.require((installed / "gstreamer-version.txt").read_bytes() == GSTREAMER_VERSION.encode())


def clean_environment(base: dict[str, str], work: Path) -> dict[str, str]:
    allowed = {
        "SYSTEMROOT",
        "WINDIR",
        "SYSTEMDRIVE",
        "COMSPEC",
        "OS",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "PROCESSOR_IDENTIFIER",
    }
    env = {key.upper(): value for key, value in base.items() if key.upper() in allowed}
    system = Path(env.get("SYSTEMROOT", "/missing"))
    env.update(common.clean_environment({}))
    env.update({key: base[key] for key in common.GATE_RUNTIME_KEYS if key in base})
    env.update(
        PATH=str(system / "System32") + os.pathsep + str(system),
        USERPROFILE=str(work / "profile"),
        APPDATA=str(work / "profile/roaming"),
        LOCALAPPDATA=str(work / "profile/local"),
        TEMP=str(work / "sessions"),
        TMP=str(work / "sessions"),
        RUNNER_TEMP=str(work),
        PIP_CACHE_DIR=str(work / "pip-cache"),
        PIP_NO_INDEX="1",
        PIP_NO_INPUT="1",
        PIP_NO_COMPILE="1",
    )
    return env


def start_command(powershell: Path, envelope: Path, installed: Path, port: int) -> list[str]:
    common.require(type(port) is int and 1024 <= port <= 65535, "admission_failed")
    return [
        str(powershell),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(envelope),
        "-Start",
        str(installed / SCRIPT_NAMES[0]),
        "-Port",
        str(port),
    ]


class LaunchSummary:
    """Bounded streaming stdout; retain counters/flags only, never raw output."""

    def __init__(self, stream) -> None:
        self.counts = dict.fromkeys(
            ("admitted", "synthetic", "analytics", "operator", "exit", "refusal"), 0
        )
        self.scalars: dict[str, int] = {}
        self.invalid = False
        self.thread = threading.Thread(target=self._read, args=(stream,), daemon=True)
        self.thread.start()

    def _line(self, line: bytes) -> None:
        line = line.rstrip(b"\r")
        markers = {
            "admitted": (
                b"K5 analytics configuration admitted; live provider acceptance is pending."
            ),
            "exit": b"Exiting after one bounded alpha acceptance run.",
            "refusal": REFUSAL,
        }
        for name, exact in markers.items():
            if line == exact:
                self.counts[name] += 1
        if re.fullmatch(
            rb"Starting K5 Vision Alpha local synthetic operator test on http://127\.0\.0\.1:[0-9]{4,5}",
            line,
        ):
            self.counts["synthetic"] += 1
        analytics = re.fullmatch(
            rb"K5 analytics PASS: submissions=([0-9]{1,7}), "
            rb"completions=([0-9]{1,7}), failures=([0-9]{1,7})",
            line,
        )
        if analytics:
            self.counts["analytics"] += 1
            for name, raw in zip(
                (
                    "analytics_provider_submissions",
                    "analytics_provider_completions",
                    "analytics_failures",
                ),
                analytics.groups(),
                strict=True,
            ):
                self.scalars[name] = int(raw)
        operator = re.fullmatch(
            rb"K5 operator PASS: frames=([0-9]{1,7}), presentations=([0-9]{1,7})", line
        )
        if operator:
            self.counts["operator"] += 1
            self.scalars.update(delivered_frames=int(operator[1]), presentations=int(operator[2]))

    def _read(self, stream) -> None:
        total, pending = 0, b""
        try:
            while block := stream.read1(4096):
                total += len(block)
                if total > 65_536:
                    self.invalid = True
                    pending = b""
                    continue  # Drain/discard a flood; never deadlock child shutdown.
                pending += block
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    if len(line) > 2048:
                        self.invalid = True
                    else:
                        self._line(line)
                if len(pending) > 2048:
                    self.invalid = True
                    pending = b""
            if pending:
                self._line(pending)
        except (OSError, ValueError):
            self.invalid = True
        finally:
            stream.close()

    def finish(self) -> None:
        self.thread.join(5)
        common.require(not self.thread.is_alive(), "cleanup_incomplete")

    def result(self) -> dict[str, object]:
        common.require(
            not self.invalid
            and self.counts
            == {
                "admitted": 1,
                "synthetic": 1,
                "analytics": 1,
                "operator": 1,
                "exit": 1,
                "refusal": 0,
            },
            "receipt_invalid",
        )
        result = {**self.scalars, **dict.fromkeys(RUN_BOOLEANS, True)}
        validate_run(result)
        return result


class DirectoryChangeGuard:
    """Windows kernel notification remembers even a create-then-delete session."""

    def __init__(self, directory: Path) -> None:
        from ctypes import wintypes

        common.require(os.name == "nt", "admission_failed")
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.FindFirstChangeNotificationW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        self.api.FindFirstChangeNotificationW.restype = wintypes.HANDLE
        self.api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.api.WaitForSingleObject.restype = wintypes.DWORD
        self.api.FindCloseChangeNotification.argtypes = [wintypes.HANDLE]
        # File name, directory name, size, and last-write changes; entire subtree.
        self.handle = self.api.FindFirstChangeNotificationW(str(directory), True, 0x1B)
        common.require(self.handle not in (None, ctypes.c_void_p(-1).value), "admission_failed")

    def unchanged(self) -> bool:
        status = self.api.WaitForSingleObject(self.handle, 0)
        common.require(status in (0, 258), "cleanup_incomplete")
        return status == 258

    def close(self) -> None:
        common.require(
            bool(self.api.FindCloseChangeNotification(self.handle)), "cleanup_incomplete"
        )


def job_accounting(owned) -> tuple[int, int]:
    common.require(owned.job is not None, "admission_failed")
    counters = owned.job.accounting()
    return counters.total_processes, counters.active_processes


def invoke_start(
    command: list[str], *, work: Path, env: dict[str, str], operation: str, invalid: bool = False
) -> dict[str, object]:
    sessions = Path(env["TEMP"])
    common.require(not any(sessions.iterdir()), "cleanup_incomplete")
    guard = DirectoryChangeGuard(sessions) if invalid else None
    owned, summary, original = None, None, None
    try:
        owned = common.OwnedProcess(
            command, cwd=work, env=env, operation=operation, stdout=subprocess.PIPE
        )
        summary = LaunchSummary(owned.process.stdout)
        try:
            owned.wait(60 if invalid else 150)
        except common.WitnessError as error:
            detail = error.diagnostic or {}
            if not (
                invalid
                and str(error) == "child_failed"
                and detail.get("child_exit_code") == 23
                and detail.get("gate_state") == "exited"
            ):
                raise
        summary.finish()
        total, active = job_accounting(owned)
        common.require(active == 0, "cleanup_incomplete")
        common.require(not any(sessions.iterdir()), "cleanup_incomplete")
        if invalid:
            common.require(
                not summary.invalid
                and summary.counts
                == {
                    "admitted": 0,
                    "synthetic": 0,
                    "analytics": 0,
                    "operator": 0,
                    "exit": 0,
                    "refusal": 1,
                }
                and owned.process.returncode == 23,
                "receipt_invalid",
            )
            # Base relay Python, PowerShell, installed venv redirector, and its
            # base analytics-preflight interpreter (CPython 3.12 Windows).
            # A native probe, app, or media process would increase this kernel counter.
            common.require(total == 4 and guard.unchanged(), "receipt_invalid")
            return {
                "invalid_config_refused": True,
                "invalid_config_no_session": True,
                "invalid_config_no_media": True,
            }
        return summary.result()
    except BaseException as error:
        original = error
        raise
    finally:
        try:
            if owned is not None:
                common.close_after_failure(owned, original)
        finally:
            try:
                if summary is not None:
                    summary.finish()
            finally:
                if guard is not None:
                    guard.close()


def require_ports_free(port: int) -> None:
    for number in (port, 8554):
        common.require_free_port(number)
    for number in (18000, 18001):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener:
            try:
                listener.bind(("127.0.0.1", number))
            except OSError:
                raise common.WitnessError("port_occupied") from None


def launch_sequence(
    *,
    work: Path,
    installed: Path,
    env: dict[str, str],
    powershell: Path,
    expected: dict[str, str],
    document: dict[str, object],
) -> None:
    envelope = work / "invoke-start.ps1"
    envelope.write_text(ENVELOPE, encoding="ascii", newline="\n")
    invalid_config = work / "invalid-analytics.json"
    invalid_config.write_bytes(b'{"schema_version":1,"provider":"invalid-selected-provider"}')
    port = common.free_port()
    command = start_command(powershell, envelope, installed, port)
    document["stage"] = "invalid_config"
    verify_layout(installed, expected)
    document.update(
        invoke_start(
            command,
            work=work,
            env={**env, "K5_ANALYTICS_CONFIG": str(invalid_config)},
            operation="probe_admission",
            invalid=True,
        )
    )
    for attempt in (1, 2):
        document["stage"] = f"launch_{attempt}"
        verify_layout(installed, expected)
        common.require(
            common.digest(native_manifest(Path(env["LOCALAPPDATA"])))
            == expected["native_cache_sha256"]
        )
        require_ports_free(port)
        result = invoke_start(command, work=work, env=env, operation=f"launch_{attempt}")
        document.update({f"run_{attempt}_{key}": value for key, value in result.items()})
        require_ports_free(port)


def verify_native(local: Path, work: Path, env: dict[str, str]) -> None:
    gst, mtx = native_roots(local)
    for command, version, operation in (
        (
            [str(gst / "bin/gst-inspect-1.0.exe"), "--version"],
            GSTREAMER_VERSION,
            "inspect_gstreamer_version",
        ),
        ([str(mtx / "mediamtx.exe"), "--version"], "1.21.1", "inspect_mediamtx_version"),
    ):
        raw = common.capture(command, cwd=work, env=env, operation=operation, limit=4096)
        common.require(
            re.search(
                rb"(?<![0-9.])" + version.encode().replace(b".", rb"\.") + rb"(?![0-9.])", raw
            )
            is not None
        )
    elements = {
        **common.GSTREAMER_ELEMENTS,
        "videotestsrc": ("videotestsrc", "gst-plugins-base", "LGPL"),
        "queue": ("coreelements", "gstreamer", "LGPL"),
        "fakesink": ("coreelements", "gstreamer", "LGPL"),
    }
    for element, expected in elements.items():
        raw = common.capture(
            [str(gst / "bin/gst-inspect-1.0.exe"), element],
            cwd=work,
            env=env,
            operation="probe_admission",
        )
        common.inspect_plugin(raw, expected, gst)


def prepare(args, work: Path, expected: dict[str, str], document: dict[str, object]):
    env = clean_environment(dict(os.environ), work)
    for key in ("TEMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        Path(env[key]).mkdir(parents=True, exist_ok=True)
    source, donor = work / "source", work / "analytics-source"
    document["stage"] = "build"
    for repository, revision, target, operation in (
        (args.repo, expected["revision"], source, "archive_candidate"),
        (args.analytics_source, common.ANALYTICS_REVISION, donor, "archive_analytics"),
    ):
        archive = work / (target.name + ".zip")
        command = common.archive_command(repository, archive, revision)
        command[0] = str(common.local_path(args.git.absolute()))
        common.run(command, cwd=work, env=env, operation=operation)
        common.extract_archive(archive, target)
    common.require(common.digest(tree_manifest(source)) == expected["source_tree_sha256"])
    # Controller and reused helper must themselves be exact candidate bytes.
    for name in (Path(__file__).name, "installed_analytics_witness.py"):
        common.require(
            (source / "scripts" / name).read_bytes() == Path(__file__).with_name(name).read_bytes()
        )
    payload = source_payload(source)
    common.require(common.digest(payload) == expected["k5_payload_sha256"])
    wheel = common.local_path(args.k5_wheel.absolute())
    common.require(
        common.file_hash(wheel) == expected["k5_wheel_sha256"]
        and common.wheel_payload(wheel) == payload
    )
    common.require(
        common.file_hash(source / "src/k5vision/data/analytics-runtime-manifest.json")
        == expected["analytics_manifest_sha256"]
    )
    wheelhouse = common.local_path(args.wheelhouse.absolute(), directory=True)
    common.require(
        common.digest(tree_manifest(wheelhouse, maximum_files=256)) == expected["wheelhouse_sha256"]
    )
    local = Path(env["LOCALAPPDATA"])
    copy_native_cache(
        common.local_path(args.local_appdata.absolute(), directory=True),
        local,
        expected["native_cache_sha256"],
    )
    gst, _ = native_roots(local)
    env.update(
        K5_GSTREAMER_ROOT=str(gst),
        GST_REGISTRY_1_0=str(work / "gst-registry.bin"),
        GST_PLUGIN_PATH_1_0="",
        GST_PLUGIN_PATH="",
        GST_PLUGIN_SYSTEM_PATH_1_0=str(gst / "lib/gstreamer-1.0"),
        GST_PLUGIN_SYSTEM_PATH=str(gst / "lib/gstreamer-1.0"),
        GIO_USE_PROXY_RESOLVER="dummy",
        GIO_MODULE_DIR=str(work / "gio-modules"),
        PATH=str(gst / "bin") + os.pathsep + env["PATH"],
    )
    (work / "gio-modules").mkdir()
    installed = work / "installed"
    installed.mkdir()
    identities = install_scripts(source, installed, expected["revision"])
    common.require(all(expected[name] == value for name, value in identities.items()))
    python = installed / ".venv/Scripts/python.exe"
    common.run(
        [sys.executable, "-I", "-B", "-m", "venv", str(installed / ".venv")],
        cwd=work,
        env=env,
        operation="create_venv",
    )
    wheels = work / "wheels"
    for output, operation in (
        (wheels, "build_analytics_wheel"),
        (work / "repeat", "rebuild_analytics_wheel"),
    ):
        common.run(
            [
                str(python),
                "-I",
                "-B",
                str(source / "scripts/build_analytics_runtime_wheel.py"),
                "--source-root",
                str(donor),
                "--output-dir",
                str(output),
            ],
            cwd=work,
            env=env,
            operation=operation,
        )
    analytics_wheels = list(wheels.glob("*.whl"))
    common.require(len(analytics_wheels) == 1)
    analytics_wheel = analytics_wheels[0]
    common.require(
        common.file_hash(analytics_wheel)
        == expected["analytics_wheel_sha256"]
        == common.file_hash(work / "repeat" / analytics_wheel.name)
    )
    versions = {
        **common.requirements(source / "scripts/windows-alpha/runtime-requirements.txt"),
        **common.RUNTIME_VERSIONS,
        **common.WINDOWS_RUNTIME_VERSIONS,
    }
    document["stage"] = "install"
    common.run(
        [
            str(python),
            "-I",
            "-B",
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--only-binary=:all:",
            "--no-compile",
            "--find-links",
            str(wheelhouse),
            *[f"{name}=={version}" for name, version in sorted(versions.items())],
        ],
        cwd=work,
        env=env,
        seconds=300,
        operation="install_dependencies",
    )
    common.run(
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
            str(wheel),
            str(analytics_wheel),
        ],
        cwd=work,
        env=env,
        operation="install_local_wheels",
    )
    common.run(
        [str(python), "-I", "-B", "-m", "pip", "check"],
        cwd=work,
        env=env,
        operation="check_dependencies",
    )
    config = work / "analytics.json"
    root = common.local_path(args.evidence_root.absolute(), directory=True)
    config.write_bytes(
        common.canonical(
            {
                "schema_version": 1,
                "provider": "analytics-lab-omz-person-v1",
                "source_revision": common.ANALYTICS_REVISION,
                "artifact_root": str(root / "artifacts"),
            }
        )
    )
    env["K5_ANALYTICS_CONFIG"] = str(config)
    inputs = work / "probe-inputs.json"
    inputs.write_bytes(
        common.canonical(
            {"k5_payload": payload, "runtime_versions": versions, "evidence_root": str(root)}
        )
    )
    # Run installed admission from a stdlib-only helper outside the archive/cwd.
    driver = work / "installed_analytics_witness.py"
    shutil.copyfile(source / "scripts/installed_analytics_witness.py", driver)
    command = [
        str(python),
        "-I",
        "-B",
        str(driver),
        "--probe",
        "--inputs",
        str(inputs),
        "--output",
        str(work / "probe-output.json"),
    ]
    return installed, env, command


def probe(
    command: list[str], work: Path, env: dict[str, str], expected: dict[str, str], *, after=False
):
    output = work / "probe-output.json"
    output.unlink(missing_ok=True)
    common.run(
        command,
        cwd=work,
        env=env,
        operation="probe_after" if after else "probe_before",
        seconds=120,
    )
    value = common.read_json(output)
    common.require(
        value.keys() == {"runtime_identity_sha256", "model_identity_sha256", "seed_identity_sha256"}
    )
    for name, identity in value.items():
        common.require(
            type(identity) is str
            and re.fullmatch(r"[0-9a-f]{64}", identity) is not None
            and identity != "0" * 64
        )
        if name != "runtime_identity_sha256" or after:
            common.require(identity == expected[name])
    return value


def clear_output(path: Path, name: str) -> Path:
    output = path.absolute()
    common.require(output.name == name, "admission_failed")
    common.local_path(output.parent, directory=True)
    if output.exists() or output.is_symlink():
        common.local_path(output)
        output.unlink()
    return output


def admit_platform() -> None:
    # This controller deliberately starts from the admitted base runtime. The
    # shared helper also independently binds its no-site relay to that runtime.
    common.require(
        os.name == "nt"
        and sys.version_info[:2] == (3, 12)
        and sys.prefix == sys.base_prefix
        and sys.flags.isolated == 1
        and sys.flags.dont_write_bytecode == 1
        and os.environ.get("PROCESSOR_ARCHITECTURE", "").upper() == "AMD64",
        "admission_failed",
    )


def execute(args) -> int:
    output = clear_output(args.output, RECEIPT_NAME)
    admitted = clear_output(args.admitted_expectations, EXPECTATIONS_NAME)
    expected = validate_expectations(common.read_json(args.expectations), installed=False)
    document = new_receipt(expected)
    work = None
    detail = None
    try:
        admit_platform()
        repo = common.local_path(args.repo.absolute(), directory=True)
        work = common.adopt_work_root(args.work_root, args.temp_root, repo, output)
        common.require(not admitted.is_relative_to(work), "admission_failed")
        installed, env, command = prepare(args, work, expected, document)
        document["stage"] = "probe"
        checked = probe(command, work, env, expected)
        expected = {**expected, "runtime_identity_sha256": checked["runtime_identity_sha256"]}
        document.update(checked)
        validate_expectations(expected)
        with admitted.open("xb") as stream:
            stream.write(common.canonical(expected) + b"\n")
        verify_native(Path(env["LOCALAPPDATA"]), work, env)
        powershell = common.local_path(
            Path(env["SYSTEMROOT"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        )
        launch_sequence(
            work=work,
            installed=installed,
            env=env,
            powershell=powershell,
            expected=expected,
            document=document,
        )
        document["stage"] = "verify"
        probe(command, work, env, expected, after=True)
        verify_layout(installed, expected)
        common.require(
            common.digest(native_manifest(Path(env["LOCALAPPDATA"])))
            == expected["native_cache_sha256"]
        )
        common.require(
            common.digest(tree_manifest(args.wheelhouse.absolute(), maximum_files=256))
            == expected["wheelhouse_sha256"]
        )
        document["completed"] = True
    except BaseException as error:
        document["failure_code"] = (
            str(error) if isinstance(error, common.WitnessError) else "unexpected"
        )
        detail = error.diagnostic if isinstance(error, common.WitnessError) else None
        detail = detail or common.diagnostic("driver_admission", category=document["failure_code"])
    finally:
        cleanup = True
        if work is not None:
            try:
                shutil.rmtree(work)
                cleanup = not work.exists()
            except OSError:
                cleanup = False
        document["cleanup_complete"] = cleanup
        if not cleanup:
            document.update(completed=False, failure_code="cleanup_incomplete", stage="cleanup")
            detail = common.diagnostic(
                "cleanup_owned", outcome="cleanup_failed", category="cleanup_incomplete"
            )
        if document["completed"]:
            document["stage"] = "complete"
            validate_receipt(document, expected)
            with output.open("xb") as stream:
                stream.write(common.canonical(document) + b"\n")
        # Failure is a separate fixed diagnostic, never a partly populated success
        # file or an exception/path/native stream published as acceptance evidence.
        elif detail is not None:
            common.emit_diagnostic(detail)
    print(
        "Installed Alpha Start-script witness passed"
        if document["completed"]
        else "Installed Alpha Start-script witness failed closed"
    )
    return 0 if document["completed"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate-receipt", action="store_true")
    parser.add_argument("--expectations", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--admitted-expectations", type=Path)
    for name in (
        "repo",
        "analytics-source",
        "k5-wheel",
        "wheelhouse",
        "evidence-root",
        "local-appdata",
        "git",
        "temp-root",
        "work-root",
    ):
        parser.add_argument("--" + name, type=Path)
    args = parser.parse_args()
    try:
        if args.validate_receipt:
            common.require(args.output.name == RECEIPT_NAME, "receipt_invalid")
            validate_receipt(common.read_json(args.output), common.read_json(args.expectations))
            print("Installed Alpha Start-script receipt validation passed")
            return 0
        # Also remove stale success on malformed/missing execute inputs. Validation
        # mode never deletes either input. Refuse aliases before touching output.
        clear_output(args.output, RECEIPT_NAME)
        common.require(
            all(
                getattr(args, name) is not None
                for name in (
                    "repo",
                    "analytics_source",
                    "k5_wheel",
                    "wheelhouse",
                    "evidence_root",
                    "local_appdata",
                    "git",
                    "temp_root",
                    "work_root",
                    "admitted_expectations",
                )
            ),
            "admission_failed",
        )
        return execute(args)
    except BaseException as error:
        code = str(error) if isinstance(error, common.WitnessError) else "unexpected"
        common.emit_diagnostic(
            common.diagnostic(
                "receipt_validate" if args.validate_receipt else "driver_admission", category=code
            )
        )
        print("Installed Alpha Start-script witness failed closed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
