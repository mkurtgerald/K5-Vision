"""Read actual SCM configuration/process ownership for installed service tests.

This helper does not register a substitute service. It observes and, only after
matching executable/birth/SCM identity, terminates the installed K5 service.
"""

from __future__ import annotations

import ctypes
import json
import math
import os
import re
import stat
import subprocess
from ctypes import wintypes
from pathlib import Path

import psutil

SERVICE_NAME = "K5VisionRecorder"
SERVICE_ACCOUNT = r"NT SERVICE\K5VisionRecorder"
_LOG_EXCEPTIONS = {
    "System.InvalidOperationException",
    "System.ComponentModel.Win32Exception",
    "System.Security.SecurityException",
    "System.UnauthorizedAccessException",
    "System.IO.IOException",
    "System.IO.FileNotFoundException",
    "System.IO.DirectoryNotFoundException",
    "System.Security.Principal.IdentityNotMappedException",
    "System.TimeoutException",
    "System.ArgumentException",
    "System.ArgumentNullException",
    "System.ObjectDisposedException",
    "System.PlatformNotSupportedException",
    "System.NotSupportedException",
    "System.TypeInitializationException",
    "System.Management.ManagementException",
    "System.Threading.ThreadAbortException",
    "System.Threading.ThreadStateException",
}
_BOOTSTRAP_STAGES = {
    "token",
    "code_admission",
    "payload_verify",
    "data_admission",
    "parent_admission",
    "identity_probe",
    "pipe_create",
    "account_required",
    "serve_start",
    "running",
    "stopping",
}
_BOOTSTRAP_EXCEPTIONS = {
    "InvalidOperationException",
    "UnauthorizedAccessException",
    "IOException",
    "Win32Exception",
    "ArgumentException",
    "NotSupportedException",
    "TypeInitializationException",
    "SecurityException",
    "TimeoutException",
    "Other",
}


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate bootstrap field")
        result[key] = value
    return result


def _bootstrap_envelopes(raw):
    """Admit only the host's exact finite public bootstrap schema."""
    result = []
    for line in raw.decode("utf-8-sig", errors="replace").splitlines():
        if not 1 <= len(line) <= 512:
            continue
        try:
            value = json.loads(line, object_pairs_hook=_unique)
            if type(value) is not dict or type(value.get("schema_version")) is not int:
                continue
            if (
                value["schema_version"] != 1
                or value.get("k5_recorder_bootstrap") not in _BOOTSTRAP_STAGES
            ):
                continue
            if (
                type(value.get("host_pid")) is not int
                or not 1 <= value["host_pid"] <= 2**32 - 1
                or type(value.get("host_created_100ns")) is not int
                or not 1 <= value["host_created_100ns"] <= 2**63 - 1
            ):
                continue
            if set(value) == {
                "schema_version",
                "k5_recorder_bootstrap",
                "host_pid",
                "host_created_100ns",
            }:
                result.append(value)
            elif set(value) == {
                "schema_version",
                "k5_recorder_bootstrap",
                "host_pid",
                "host_created_100ns",
                "failure_type",
                "failure_code",
            }:
                if (
                    value["failure_type"] in _BOOTSTRAP_EXCEPTIONS
                    and type(value["failure_code"]) is int
                    and 0 <= value["failure_code"] <= 65535
                ):
                    result.append(value)
        except (ValueError, TypeError):
            continue
    return result[-16:]


def _bound_bootstrap(raw, processes):
    current = {
        (host["pid"], host["created_100ns"])
        for host in processes.get("host", {}).get("instances", [])
    }
    return [
        value
        for value in _bootstrap_envelopes(raw)
        if (value["host_pid"], value["host_created_100ns"]) in current
    ]


def _native_error_numbers(text):
    hresults = set()
    for name in _LOG_EXCEPTIONS:
        for value in re.findall(re.escape(name) + r"\s*\((0x[0-9a-fA-F]{8})\)", text):
            hresults.add(int(value, 16))
    for value in re.findall(
        r"\b(?:HRESULT|HResult)\s*[:=]?\s*(0x[0-9a-fA-F]{8}|-?[0-9]{1,10})\b", text
    ):
        number = int(value, 16 if value.lower().startswith("0x") else 10)
        if -(2**31) <= number <= 2**32 - 1:
            hresults.add(number & 0xFFFFFFFF)
    native = {
        int(value)
        for value in re.findall(
            r"\b(?:NativeErrorCode|Win32Error|Win32ErrorCode|Win32 error(?: code)?)"
            r"\s*[:=]?\s*([0-9]{1,5})\b",
            text,
        )
        if int(value) <= 65535
    }
    return sorted(hresults)[:8], sorted(native)[:8]


def _classify_log(raw):
    """Project fixed WinSW/native failure categories, never private log text."""
    text = raw.decode("utf-8", errors="replace")
    hresults, native = _native_error_numbers(text)
    return {
        "exception_types": sorted(
            _LOG_EXCEPTIONS.intersection(re.findall(r"\bSystem[.][A-Za-z0-9.]+Exception\b", text))
        )[:8],
        "hresults": hresults,
        "win32_errors": native,
        "process_started": min(100, len(re.findall(r"(?i)started (?:the )?process", text))),
        "process_finished_nonzero": min(
            100,
            len(
                re.findall(
                    r"(?i)(?:exit(?:ed)?(?: with)?|finished with|exitcode)[^\r\n]{0,30}"
                    r"(?:code[=: ]*)?[1-9][0-9]*",
                    text,
                )
            ),
        ),
        "access_denied": bool(
            re.search(r"(?i)access (?:is )?denied|UnauthorizedAccessException", text)
        ),
        "file_not_found": bool(
            re.search(r"FileNotFoundException|DirectoryNotFoundException", text)
        ),
        "logon_failed": bool(re.search(r"(?i)logon failure|logon failed|error 1069", text)),
        "dotnet_exception": bool(re.search(r"\bSystem[.][A-Za-z0-9.]+Exception\b", text)),
    }


def _installed_process_snapshot(service):
    status = service.status()
    if status["state"] != 4 or status["pid"] <= 0:
        return {"service_not_running": True}
    root = psutil.Process(status["pid"])
    birth = service.native_birth(root)
    expected_root = service.installed / (SERVICE_NAME + ".exe")
    if Path(root.exe()).resolve() != expected_root or service.session(root) != 0:
        raise RuntimeError("Diagnostic SCM process identity was not admitted")
    roots = service.direct_children(root)
    if len(roots) > 16:
        raise RuntimeError("Diagnostic SCM child inventory exceeded its bound")
    groups = {"host": [], "python": []}
    for host in roots:
        if Path(host.exe()).resolve() != service.installed / "K5Vision.exe":
            continue
        if service.session(host) != 0 or host.ppid() != root.pid:
            raise RuntimeError("Diagnostic installed host identity was not admitted")
        parent = psutil.Process(host.ppid())
        if (parent.pid, service.native_birth(parent)) != (root.pid, birth):
            raise RuntimeError("Diagnostic installed host parent changed")
        host_birth = service.native_birth(host)
        groups["host"].append(host)
        workers = service.direct_children(host)
        if len(workers) > 16:
            raise RuntimeError("Diagnostic host child inventory exceeded its bound")
        for worker in workers:
            if Path(worker.exe()).resolve() != service.installed / "runtime" / "python.exe":
                continue
            if service.session(worker) != 0 or worker.ppid() != host.pid:
                raise RuntimeError("Diagnostic embedded Python identity was not admitted")
            parent = psutil.Process(worker.ppid())
            if (parent.pid, service.native_birth(parent)) != (host.pid, host_birth):
                raise RuntimeError("Diagnostic embedded Python parent changed")
            groups["python"].append(worker)
            if len(groups["python"]) > 16:
                raise RuntimeError("Diagnostic embedded Python inventory exceeded its bound")
    result = {"wrapper_pid": root.pid, "wrapper_created_100ns": birth, "session": 0}
    for name, members in groups.items():
        total_cpu, threads = 0.0, 0
        for process in members:
            cpu = process.cpu_times()
            total_cpu += cpu.user + cpu.system
            threads += process.num_threads()
            if not process.is_running():
                raise RuntimeError("Diagnostic child changed during observation")
        if not math.isfinite(total_cpu) or not 0 <= total_cpu <= 86400 or not 0 <= threads <= 65535:
            raise RuntimeError("Diagnostic process totals exceeded their bound")
        result[name] = {
            "count": len(members),
            "cpu_seconds": round(total_cpu, 3),
            "threads": threads,
            "instances": [
                {"pid": process.pid, "created_100ns": service.native_birth(process)}
                for process in members
            ],
        }
    if (
        not root.is_running()
        or service.native_birth(root) != birth
        or service.status()["pid"] != root.pid
    ):
        raise RuntimeError("Diagnostic SCM wrapper changed during observation")
    return result


def installer_failure_snapshot(installed, profile):
    """Observe a failed real installation before GUI setup returned a service owner."""
    result = {"installed_root_present": Path(installed).is_dir()}
    try:
        service = InstalledService(installed)
        result["service_registered"] = not service.absent()
        if result["service_registered"]:
            status = service.status()
            result["scm"] = {
                key: status[key]
                for key in (
                    "state",
                    "pid",
                    "win32_exit",
                    "service_exit",
                )
                if type(status.get(key)) is int and 0 <= status[key] <= 2**32 - 1
            }
            configuration = service.configuration()
            result["configuration"] = {
                "service_type": configuration["service_type"],
                "start_type": configuration["start_type"],
                "virtual_account": configuration["account"].casefold()
                == SERVICE_ACCOUNT.casefold(),
                "installed_executable_verified": configuration["installed_executable_verified"],
            }
            try:
                result["processes"] = _installed_process_snapshot(service)
            except (OSError, RuntimeError, ValueError, psutil.Error):
                result["process_query_unavailable"] = True
    except (OSError, RuntimeError, ValueError):
        result["scm_query_unavailable"] = True
    profile = Path(profile)
    for name, path in (
        ("data", profile),
        ("identity", profile / "identity"),
        ("logs", profile / "logs"),
    ):
        result[name + "_directory_present"] = path.is_dir()
    logs = []
    try:
        info = (profile / "logs").lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise RuntimeError("Diagnostic log root is not an ordinary directory")
        for index, path in enumerate((profile / "logs").iterdir()):
            if index >= 16:
                break
            kind = next(
                (
                    kind
                    for kind in ("wrapper", "out", "err")
                    if path.name.endswith("." + kind + ".log")
                ),
                None,
            )
            if kind is None:
                continue
            info = path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or getattr(info, "st_file_attributes", 0) & 0x400
            ):
                continue
            with path.open("rb") as stream:
                stream.seek(max(0, info.st_size - 65536))
                raw = stream.read(65536)
            logs.append(
                {
                    "kind": kind,
                    "bytes": min(info.st_size, 2**31 - 1),
                    "events": _classify_log(raw),
                    **(
                        {"bootstrap": _bound_bootstrap(raw, result.get("processes", {}))}
                        if kind == "out"
                        else {}
                    ),
                }
            )
    except (OSError, RuntimeError, ValueError):
        result["log_query_unavailable"] = True
    result["logs"] = logs
    return result


class _Status(ctypes.Structure):
    _fields_ = [
        (name, wintypes.DWORD)
        for name in (
            "type",
            "state",
            "controls",
            "exit",
            "specific_exit",
            "checkpoint",
            "wait_hint",
            "pid",
            "flags",
        )
    ]


class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("size", wintypes.DWORD),
        ("usage", wintypes.DWORD),
        ("pid", wintypes.DWORD),
        ("heap", ctypes.c_size_t),
        ("module", wintypes.DWORD),
        ("threads", wintypes.DWORD),
        ("parent_pid", wintypes.DWORD),
        ("priority", wintypes.LONG),
        ("flags", wintypes.DWORD),
        ("image", wintypes.WCHAR * 260),
    ]


class _Configuration(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.DWORD),
        ("start", wintypes.DWORD),
        ("error", wintypes.DWORD),
        ("binary", wintypes.LPWSTR),
        ("group", wintypes.LPWSTR),
        ("tag", wintypes.DWORD),
        ("dependencies", wintypes.LPWSTR),
        ("account", wintypes.LPWSTR),
        ("display", wintypes.LPWSTR),
    ]


class _Action(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("delay_ms", wintypes.DWORD)]


class _FailureActions(ctypes.Structure):
    _fields_ = [
        ("reset_seconds", wintypes.DWORD),
        ("reboot", wintypes.LPWSTR),
        ("command", wintypes.LPWSTR),
        ("count", wintypes.DWORD),
        ("actions", ctypes.POINTER(_Action)),
    ]


class InstalledService:
    def __init__(self, installed):
        if os.name != "nt":
            raise RuntimeError("SCM acceptance requires actual Windows")
        self.installed = Path(installed).resolve(strict=True)
        self.api = ctypes.WinDLL("advapi32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.OpenSCManagerW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        self.api.OpenSCManagerW.restype = wintypes.HANDLE
        self.api.OpenServiceW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD]
        self.api.OpenServiceW.restype = wintypes.HANDLE
        self.api.CloseServiceHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseServiceHandle.restype = wintypes.BOOL
        self.api.QueryServiceStatusEx.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.api.QueryServiceStatusEx.restype = wintypes.BOOL
        self.api.QueryServiceConfigW.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.api.QueryServiceConfigW.restype = wintypes.BOOL
        self.api.QueryServiceConfig2W.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.api.QueryServiceConfig2W.restype = wintypes.BOOL
        self.api.StartServiceW.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p]
        self.api.StartServiceW.restype = wintypes.BOOL
        self.api.ControlService.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p]
        self.api.ControlService.restype = wintypes.BOOL
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.kernel.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            *([ctypes.POINTER(wintypes.FILETIME)] * 4),
        ]
        self.kernel.GetProcessTimes.restype = wintypes.BOOL
        self.kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        self.kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        for function in (self.kernel.Process32FirstW, self.kernel.Process32NextW):
            function.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
            function.restype = wintypes.BOOL
        self.kernel.ProcessIdToSessionId.argtypes = [
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.kernel.ProcessIdToSessionId.restype = wintypes.BOOL

    def _query(self, operation, *, access=1 | 4):
        manager = self.api.OpenSCManagerW(None, None, 1)
        if not manager:
            raise RuntimeError("SCM acceptance could not open the service manager")
        service = None
        try:
            service = self.api.OpenServiceW(manager, SERVICE_NAME, access)
            if not service:
                raise RuntimeError("Installed recorder is not registered with SCM")
            return operation(service)
        finally:
            if service:
                self.api.CloseServiceHandle(service)
            self.api.CloseServiceHandle(manager)

    def start_without_readiness(self):
        if self.status()["state"] != 1:
            raise RuntimeError("Native restart proof requires a stopped recorder")

        def start(service):
            if not self.api.StartServiceW(service, 0, None):
                raise RuntimeError("Native recorder start request failed")

        self._query(start, access=0x10 | 4)

    def request_stop(self):
        def stop(service):
            status = ctypes.create_string_buffer(28)
            if not self.api.ControlService(service, 1, status):
                raise RuntimeError("Native recorder stop request failed")

        self._query(stop, access=0x20 | 4)

    def native_birth(self, process):
        handle = self.kernel.OpenProcess(0x1000, False, process.pid)
        if not handle:
            raise RuntimeError("Native process birth handle could not be opened")
        try:
            created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
            if not self.kernel.GetProcessTimes(
                handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                raise RuntimeError("Native process birth could not be read")
            if not process.is_running():
                raise RuntimeError("Native process birth changed during observation")
            return created.dwLowDateTime | (created.dwHighDateTime << 32)
        finally:
            self.kernel.CloseHandle(handle)

    def direct_children(self, parent):
        """Use native creator PIDs; psutil.children infers wall-clock ordering."""
        parent_birth = self.native_birth(parent)
        snapshot = self.kernel.CreateToolhelp32Snapshot(2, 0)
        if snapshot in (None, ctypes.c_void_p(-1).value):
            raise RuntimeError("Native process snapshot could not be opened")
        children = []
        try:
            entry = _ProcessEntry()
            entry.size = ctypes.sizeof(entry)
            available = self.kernel.Process32FirstW(snapshot, ctypes.byref(entry))
            count = 0
            while available:
                count += 1
                if count > 4096:
                    raise RuntimeError("Native process snapshot exceeded its bound")
                if entry.parent_pid == parent.pid:
                    child = psutil.Process(entry.pid)
                    if child.ppid() != parent.pid:
                        raise RuntimeError("Native creator PID changed during observation")
                    children.append(child)
                    if len(children) > 16:
                        raise RuntimeError("Native direct-child inventory exceeded its bound")
                available = self.kernel.Process32NextW(snapshot, ctypes.byref(entry))
            if (
                ctypes.get_last_error() != 18
                or not parent.is_running()
                or self.native_birth(parent) != parent_birth
            ):
                raise RuntimeError("Native process snapshot ended without stable ownership")
            return children
        finally:
            self.kernel.CloseHandle(snapshot)

    def _buffer(self, function, service, *prefix):
        needed = wintypes.DWORD()
        function(service, *prefix, None, 0, ctypes.byref(needed))
        if not 1 <= needed.value <= 65536:
            raise RuntimeError("SCM acceptance configuration exceeded its bound")
        result = ctypes.create_string_buffer(needed.value)
        if not function(service, *prefix, result, len(result), ctypes.byref(needed)):
            raise RuntimeError("SCM acceptance configuration could not be read")
        return result

    def status(self):
        def query(service):
            value, needed = _Status(), wintypes.DWORD()
            if not self.api.QueryServiceStatusEx(
                service, 0, ctypes.byref(value), ctypes.sizeof(value), ctypes.byref(needed)
            ):
                raise RuntimeError("SCM recorder status could not be read")
            return {
                "state": value.state,
                "pid": value.pid,
                "type": value.type,
                "win32_exit": value.exit,
                "service_exit": value.specific_exit,
            }

        return self._query(query)

    def absent(self):
        manager = self.api.OpenSCManagerW(None, None, 1)
        if not manager:
            raise RuntimeError("SCM absence observation could not open the service manager")
        service = None
        try:
            service = self.api.OpenServiceW(manager, SERVICE_NAME, 4)
            if service:
                return False
            if ctypes.get_last_error() == 1060:
                return True
            raise RuntimeError("SCM absence observation did not establish service removal")
        finally:
            if service:
                self.api.CloseServiceHandle(service)
            self.api.CloseServiceHandle(manager)

    def configuration(self):
        def query(service):
            raw = self._buffer(self.api.QueryServiceConfigW, service)
            value = ctypes.cast(raw, ctypes.POINTER(_Configuration)).contents
            failures_raw = self._buffer(self.api.QueryServiceConfig2W, service, 2)
            failures = ctypes.cast(failures_raw, ctypes.POINTER(_FailureActions)).contents
            if not 1 <= failures.count <= 8:
                raise RuntimeError("SCM recorder failure actions are absent or excessive")
            executable = (value.binary or "").strip()
            expected = str(self.installed / (SERVICE_NAME + ".exe"))
            if executable.casefold() not in {
                expected.casefold(),
                ('"' + expected + '"').casefold(),
            }:
                raise RuntimeError("SCM recorder executable differs from the installed payload")
            return {
                "service_type": value.type,
                "start_type": value.start,
                "account": value.account,
                "reset_seconds": failures.reset_seconds,
                "failure_actions": [
                    {"type": failures.actions[i].type, "delay_ms": failures.actions[i].delay_ms}
                    for i in range(failures.count)
                ],
                "installed_executable_verified": True,
            }

        return self._query(query)

    def session(self, process):
        result = wintypes.DWORD()
        if not self.kernel.ProcessIdToSessionId(process.pid, ctypes.byref(result)):
            raise RuntimeError("SCM process session could not be read")
        return result.value

    def processes(self):
        status = self.status()
        if status["state"] != 4 or status["pid"] <= 0:
            raise RuntimeError("SCM recorder is not running")
        root = psutil.Process(status["pid"])
        root_birth = self.native_birth(root)
        if Path(root.exe()).resolve() != self.installed / (SERVICE_NAME + ".exe"):
            raise RuntimeError("Running SCM recorder is not the installed executable")
        backends = []
        for host in self.direct_children(root):
            if Path(host.exe()).resolve() != self.installed / "K5Vision.exe":
                continue
            host_birth = self.native_birth(host)
            for process in self.direct_children(host):
                if Path(process.exe()).resolve() != self.installed / "runtime" / "python.exe":
                    continue
                parent = psutil.Process(process.ppid())
                if (parent.pid, self.native_birth(parent)) != (host.pid, host_birth):
                    raise RuntimeError("Running backend parent changed during observation")
                backends.append(process)
        if len(backends) != 1:
            raise RuntimeError("SCM recorder does not own exactly one embedded backend")
        backend = backends[0]
        if self.status()["pid"] != root.pid or self.native_birth(root) != root_birth:
            raise RuntimeError("SCM recorder changed during process observation")
        return root, backend

    def observe(self):
        root, backend = self.processes()
        return {
            "service_pid": root.pid,
            "service_created": root.create_time(),
            "backend_pid": backend.pid,
            "backend_created": backend.create_time(),
            "service_session": self.session(root),
            "backend_session": self.session(backend),
            "service_account_verified": root.username().casefold() == SERVICE_ACCOUNT.casefold(),
            "backend_account_verified": backend.username().casefold() == SERVICE_ACCOUNT.casefold(),
        }

    def kill_owned(self, observation, *, backend):
        root, worker = self.processes()
        target = worker if backend else root
        prefix = "backend" if backend else "service"
        if (
            target.pid != observation[prefix + "_pid"]
            or target.create_time() != observation[prefix + "_created"]
            or self.session(target) != 0
            or target.username().casefold() != SERVICE_ACCOUNT.casefold()
        ):
            raise RuntimeError("SCM failure target no longer matches the owned observation")
        target.kill()
        target.wait(timeout=10)

    def stop(self):
        result = subprocess.run(
            [str(Path(os.environ["SYSTEMROOT"]) / "System32" / "sc.exe"), "stop", SERVICE_NAME],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        if result.returncode not in {0, 1062}:
            raise RuntimeError("SCM recorder did not accept its stop request")
