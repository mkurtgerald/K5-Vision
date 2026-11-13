"""Admit only the remembered embedded backend before diagnostic attachment.

This module never creates a process lookup from a PID, enumerates unrelated
processes, accesses the network, or changes time. Results contain fixed enums
and booleans only. Loaded-module enumeration is deliberately not performed.
"""

from __future__ import annotations

import ctypes
import hashlib
import math
import os
import stat
from ctypes import wintypes
from pathlib import Path

_CORE_BYTES = 6_945_272
_CORE_SHA256 = "9a0e3435aaa680d868150f87ab3e388ad2eebc22f87e036155c7b4eda8cd2120"
_PYTHON_VERSION = "3.12.10"
_MAX_PROCESSES = 512
_MAX_ROOTS = 32


def _identity(process):
    """Read the process object's remembered identity without a new PID lookup."""
    value = getattr(process, "_ident", None)
    if (
        type(value) is not tuple
        or len(value) != 2
        or type(value[0]) is not int
        or not 0 < value[0] <= 0xFFFFFFFF
        or type(value[1]) not in {int, float}
        or not 0 < value[1] <= 1e12
        or not math.isfinite(value[1])
    ):
        return None
    return value


def _live(process, identity):
    return (
        _identity(process) == identity
        and process.is_running()
        and process.pid == identity[0]
        and process.create_time() == identity[1]
    )


def _plain_file(path):
    info = path.lstat()
    return stat.S_ISREG(info.st_mode) and not (getattr(info, "st_file_attributes", 0) & 0x400)


def _backend_arguments(arguments):
    if type(arguments) is not list or not 3 <= len(arguments) <= 12:
        return False
    if not all(type(item) is str and 0 < len(item) <= 4096 for item in arguments):
        return False
    if arguments.count("-m") != 1:
        return False
    marker = arguments.index("-m")
    return (
        marker >= 1
        and arguments[marker:] == ["-m", "k5vision.desktop_setup"]
        and all(flag in {"-I", "-B", "-E", "-s", "-S", "-u"} for flag in arguments[1:marker])
    )


def _version_api():
    if os.name != "nt":
        return None
    api = ctypes.WinDLL("version.dll", use_last_error=True, winmode=0x00000800)
    api.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    api.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    api.GetFileVersionInfoW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    api.GetFileVersionInfoW.restype = wintypes.BOOL
    api.VerQueryValueW.argtypes = [
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.UINT),
    ]
    api.VerQueryValueW.restype = wintypes.BOOL
    return api


def _version_is_expected(path):
    api = _version_api()
    if api is None:
        return False
    ignored = wintypes.DWORD()
    size = api.GetFileVersionInfoSizeW(str(path), ctypes.byref(ignored))
    if not 1 <= size <= 1024**2:
        return False
    block = ctypes.create_string_buffer(size)
    if not api.GetFileVersionInfoW(str(path), 0, size, block):
        return False
    base, end = ctypes.addressof(block), ctypes.addressof(block) + size

    def query(key, *, characters=False):
        pointer, length = ctypes.c_void_p(), wintypes.UINT()
        if not api.VerQueryValueW(block, key, ctypes.byref(pointer), ctypes.byref(length)):
            return None
        count = length.value
        limit = 64 if characters else 128
        width = ctypes.sizeof(ctypes.c_wchar) if characters else 1
        if (
            not pointer.value
            or not 1 <= count <= limit
            or not base <= pointer.value < end
            or pointer.value + count * width > end
        ):
            return None
        return pointer.value, count

    translated = query("\\VarFileInfo\\Translation")
    if translated is None or translated[1] % 4:
        return False
    words = (wintypes.WORD * (translated[1] // 2)).from_address(translated[0])
    for index in range(0, min(len(words), 16), 2):
        prefix = f"\\StringFileInfo\\{words[index]:04x}{words[index + 1]:04x}\\"
        matched = []
        for field in ("FileVersion", "ProductVersion"):
            value = query(prefix + field, characters=True)
            matched.append(
                value is not None
                and ctypes.wstring_at(value[0], value[1]).rstrip("\0") == _PYTHON_VERSION
            )
        if all(matched):
            return True
    return False


def _core_bytes_match(path):
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if before.st_size != _CORE_BYTES:
            return False
        digest = hashlib.sha256()
        remaining = _CORE_BYTES
        while remaining:
            chunk = stream.read(min(128 * 1024, remaining))
            if not chunk:
                return False
            digest.update(chunk)
            remaining -= len(chunk)
        if stream.read(1):
            return False
        actual = digest.hexdigest()
        after = os.fstat(stream.fileno())
    named = path.lstat()
    identity = before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns
    return (
        identity
        == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        == (named.st_dev, named.st_ino, named.st_size, named.st_mtime_ns)
        and actual == _CORE_SHA256
        and _plain_file(path)
    )


def verify_owned_target(cohort, selected_process):
    """Return safe identity flags; any unverified identity refuses attachment."""
    result = {
        "status": "cohort_unavailable",
        "ready_for_attachment": False,
        "remembered_target": False,
        "target_live": False,
        "python_executable_verified": False,
        "backend_module_verified": False,
        "remembered_desktop_root": False,
        "desktop_root_live": False,
        "desktop_executable_verified": False,
        "live_parent_relation_verified": False,
        "same_installation_verified": False,
        "python_core_present": False,
        "python_core_version_verified": False,
        "python_core_bytes_verified": False,
    }
    try:
        lock = cohort.lock
        if not lock.acquire(timeout=0.25):
            return result
        try:
            if (
                type(cohort.processes) is not dict
                or len(cohort.processes) > _MAX_PROCESSES
                or type(cohort.roots) is not list
                or len(cohort.roots) > _MAX_ROOTS
            ):
                return result
            processes, roots = dict(cohort.processes), list(cohort.roots)
        finally:
            lock.release()
        identity = _identity(selected_process)
        target = processes.get(identity) if identity is not None else None
        if target is None or _identity(target) != identity:
            result["status"] = "target_not_remembered"
            return result
        result["remembered_target"] = True
        result["target_live"] = bool(_live(target, identity))
        if not result["target_live"]:
            result["status"] = "target_not_live"
            return result
        executable = Path(target.exe())
        result["python_executable_verified"] = (
            executable.is_absolute() and executable.name.casefold() in {"python.exe", "pythonw.exe"}
        )
        result["backend_module_verified"] = _backend_arguments(target.cmdline())
        if not result["python_executable_verified"] or not result["backend_module_verified"]:
            result["status"] = "target_not_backend"
            return result
        parent = target.ppid()
        root = None
        root_identity = None
        for remembered in roots:
            candidate_identity = _identity(remembered)
            if candidate_identity is not None and candidate_identity[0] == parent:
                candidate = processes.get(candidate_identity)
                if candidate is not None and _identity(candidate) == candidate_identity:
                    root, root_identity = candidate, candidate_identity
                    break
        if root is None:
            result["status"] = "desktop_root_unverified"
            return result
        result["remembered_desktop_root"] = True
        result["desktop_root_live"] = bool(_live(root, root_identity))
        desktop_executable = Path(root.exe())
        result["desktop_executable_verified"] = (
            desktop_executable.is_absolute()
            and desktop_executable.name.casefold() == "k5vision.exe"
        )
        result["live_parent_relation_verified"] = (
            result["desktop_root_live"] and target.ppid() == root_identity[0]
        )
        if not result["desktop_executable_verified"] or not result["live_parent_relation_verified"]:
            result["status"] = "desktop_root_unverified"
            return result
        if not _plain_file(executable) or not _plain_file(desktop_executable):
            result["status"] = "installation_unverified"
            return result
        executable = executable.resolve(strict=True)
        desktop_executable = desktop_executable.resolve(strict=True)
        result["same_installation_verified"] = (
            executable.parent.name.casefold() == "runtime"
            and executable.parent.parent == desktop_executable.parent
        )
        if not result["same_installation_verified"]:
            result["status"] = "installation_unverified"
            return result
        core = executable.with_name("python312.dll")
        result["python_core_present"] = core.is_file() and _plain_file(core)
        if not result["python_core_present"]:
            result["status"] = "python_core_missing"
            return result
        result["python_core_bytes_verified"] = bool(_core_bytes_match(core))
        result["python_core_version_verified"] = bool(_version_is_expected(core))
        if not result["python_core_bytes_verified"] or not result["python_core_version_verified"]:
            result["status"] = "python_core_unverified"
            return result
        if (
            not _live(target, identity)
            or not _live(root, root_identity)
            or target.ppid() != root_identity[0]
        ):
            result["status"] = "target_changed_during_inspection"
            return result
        result.update(status="ready", ready_for_attachment=True)
        return result
    except Exception:  # noqa: BLE001 - never disclose process or OS exception content.
        result["status"] = "inspection_unavailable"
        return result
