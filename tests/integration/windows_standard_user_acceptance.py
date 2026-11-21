"""Run the actual installed IPC client under a freshly created ordinary account."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

_TRUE = {
    "ordinary_token_user_verified",
    "startup_identity_timestamp_order_independent",
    "builtin_users_group_present",
    "admin_pipe_denied",
    "control_pipe_denied",
    "malformed_status_reconnect",
    "eof_status_reconnect",
    "slow_status_reconnect",
    "actual_product_client",
    "generated_account_removed",
    "generated_profile_removed",
    "generated_probe_tree_removed",
}
_FALSE = {"administrator_group_present", "token_elevated"}
_FIELDS = {"slow_status_seconds", "product_status_success_count"}
_STAGES = {
    "start",
    "ordinary_token",
    "clock_independent_startup_identity",
    "product_status",
    "admin_pipe_denial",
    "control_pipe_denial",
    "malformed_status",
    "eof_status",
    "slow_status",
    "create_ordinary_account",
    "launch_ordinary_account",
    "child_cleanup",
    "account_cleanup",
    "profile_cleanup",
    "tree_cleanup",
}
_FAILURE_TYPES = {
    "TargetInvocationException",
    "TypeInitializationException",
    "InvalidOperationException",
    "UnauthorizedAccessException",
    "IOException",
    "Win32Exception",
    "ArgumentException",
    "NotSupportedException",
    "SecurityException",
    "TimeoutException",
    "IdentityNotMappedException",
    "ObjectDisposedException",
    "Other",
}
_PRODUCT_FRAMES = {
    "RecorderClient.Exchange",
    "RecorderClient.ExchangeOwned",
    "RecorderNative.AdmitStatusOwner",
    "RecorderPaths..cctor",
    "RecorderWire.Transfer",
    "RecorderWire.Read",
    "RecorderWire.Write",
    "RecorderWire.Operation",
    "Json.Parse",
}


def _probe_failure_note(value, diagnostic):
    if type(value.get("failure_stage")) is str and value["failure_stage"] in _STAGES:
        diagnostic["ordinary_probe_stage"] = value["failure_stage"]
    for source, target in (
        ("failure_type", "ordinary_probe_exception_type"),
        ("failure_outer_type", "ordinary_probe_outer_exception_type"),
    ):
        if type(value.get(source)) is str and value[source] in _FAILURE_TYPES:
            diagnostic[target] = value[source]
    for source, target, maximum in (
        ("failure_hresult", "ordinary_probe_hresult", 2**32 - 1),
        ("failure_win32_error", "ordinary_probe_win32_error", 65535),
    ):
        if type(value.get(source)) is int and 0 <= value[source] <= maximum:
            diagnostic[target] = value[source]
    frames = value.get("failure_product_frames")
    if type(frames) is list:
        diagnostic["ordinary_probe_product_frames"] = [
            frame for frame in frames if type(frame) is str and frame in _PRODUCT_FRAMES
        ][:8]
    diagnostic["ordinary_probe_completed_checks"] = {
        key: value[key] for key in _TRUE | _FALSE if type(value.get(key)) is bool
    }


def run_probe(installed, parent, diagnostic):
    if os.name != "nt":
        raise RuntimeError("Ordinary-account acceptance requires Windows")
    source = Path(__file__).with_name("windows_standard_user_probe.cs")
    executable, output = parent / "OrdinaryUserProbe.exe", parent / "ordinary-user-result.json"
    compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    compilation = subprocess.run(
        [
            str(compiler),
            "/nologo",
            "/target:winexe",
            "/platform:x64",
            "/optimize+",
            "/warn:4",
            "/warnaserror+",
            "/codepage:65001",
            "/reference:System.dll",
            "/reference:System.Core.dll",
            "/reference:System.Web.Extensions.dll",
            "/out:" + str(executable),
            str(source),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if compilation.returncode:
        codes = sorted(set(re.findall(rb"\berror (CS[0-9]{4})\b", compilation.stdout)))
        diagnostic["ordinary_probe_compiler_codes"] = [code.decode("ascii") for code in codes[:8]]
        raise RuntimeError("Ordinary-account probe compilation failed")
    result = subprocess.run(
        [str(executable), str(installed), str(output)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=60,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if not output.is_file() or not 0 < output.stat().st_size <= 4096:
        raise RuntimeError("Ordinary-account probe produced no finite result")
    value = json.loads(output.read_text(encoding="utf-8-sig"))
    if type(value) is not dict:
        raise RuntimeError("Ordinary-account probe result was invalid")
    if result.returncode:
        _probe_failure_note(value, diagnostic)
        raise RuntimeError("Ordinary-account installed IPC boundary failed")
    if set(value) != _TRUE | _FALSE | _FIELDS:
        raise RuntimeError("Ordinary-account probe evidence was incomplete")
    if not all(value[key] is True for key in _TRUE) or not all(
        value[key] is False for key in _FALSE
    ):
        raise RuntimeError("Ordinary-account probe boundary was not satisfied")
    if (
        type(value["product_status_success_count"]) is not int
        or value["product_status_success_count"] != 4
    ):
        raise RuntimeError(
            "Ordinary-account probe did not reconnect through the actual product client"
        )
    elapsed = value["slow_status_seconds"]
    if type(elapsed) not in (int, float) or not 4 <= elapsed < 8.5:
        raise RuntimeError("Ordinary-account slow status timeout was not bounded")
    return {**value, "probe_executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest()}
