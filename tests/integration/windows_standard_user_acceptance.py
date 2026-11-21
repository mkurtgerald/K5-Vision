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
        if value.get("failure_stage") in _STAGES:
            diagnostic["ordinary_probe_stage"] = value["failure_stage"]
        failure_type = value.get("failure_type")
        if type(failure_type) is str and re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,63}", failure_type):
            diagnostic["ordinary_probe_exception_type"] = failure_type
        native = value.get("failure_win32_error")
        if type(native) is int and 0 <= native <= 65535:
            diagnostic["ordinary_probe_win32_error"] = native
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
