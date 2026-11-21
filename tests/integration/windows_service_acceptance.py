"""Read actual SCM configuration/process ownership for installed service tests.

This helper does not register a substitute service. It observes and, only after
matching executable/birth/SCM identity, terminates the installed K5 service.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
from ctypes import wintypes
from pathlib import Path

import psutil

SERVICE_NAME = "K5VisionRecorder"
SERVICE_ACCOUNT = r"NT SERVICE\K5VisionRecorder"


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
        self.kernel.ProcessIdToSessionId.argtypes = [
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.kernel.ProcessIdToSessionId.restype = wintypes.BOOL

    def _query(self, operation):
        manager = self.api.OpenSCManagerW(None, None, 1)
        if not manager:
            raise RuntimeError("SCM acceptance could not open the service manager")
        service = None
        try:
            service = self.api.OpenServiceW(manager, SERVICE_NAME, 1 | 4)
            if not service:
                raise RuntimeError("Installed recorder is not registered with SCM")
            return operation(service)
        finally:
            if service:
                self.api.CloseServiceHandle(service)
            self.api.CloseServiceHandle(manager)

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
        if Path(root.exe()).resolve() != self.installed / (SERVICE_NAME + ".exe"):
            raise RuntimeError("Running SCM recorder is not the installed executable")
        descendants = root.children(recursive=True)
        hosts = {
            (p.pid, p.create_time())
            for p in descendants
            if Path(p.exe()).resolve() == self.installed / "K5Vision.exe"
            and "--recorder-host" in p.cmdline()
        }
        backends = []
        for process in descendants:
            if Path(process.exe()).resolve() == self.installed / "runtime" / "python.exe":
                parent = process.parent()
                if parent is not None and (parent.pid, parent.create_time()) in hosts:
                    backends.append(process)
        if len(backends) != 1:
            raise RuntimeError("SCM recorder does not own exactly one embedded backend")
        backend = backends[0]
        if self.status()["pid"] != root.pid:
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
