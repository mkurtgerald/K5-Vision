"""Opt-in real clock writes confined to a disposable hosted Windows VM.

The child watchdog restores UTC on parent EOF or a native monotonic deadline.
Foreground cleanup also restores from the same baseline if the child fails.
Forced runner process-tree termination can kill both; VM disposal is the final
isolation boundary. This helper changes no NTP peers or other machine policy.
"""

from __future__ import annotations

import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from datetime import UTC, datetime, timedelta
from pathlib import Path

_SERVICES = ("W32Time", "vmictimesync")
_MAX_LEASE_SECONDS = 90
_STEP_SECONDS = 172_800
_RESTORE_TOLERANCE_SECONDS = 2.0


class WindowsVmClockError(RuntimeError):
    """A bounded VM-only clock operation or restoration failed."""


def _require_vm_environment(environ=None, *, platform=None):
    env = os.environ if environ is None else environ
    native_platform = os.name if platform is None else platform
    expected = {
        "K5_VM_CLOCK_ACCEPTANCE": "1",
        "GITHUB_ACTIONS": "true",
        "RUNNER_ENVIRONMENT": "github-hosted",
        "RUNNER_OS": "Windows",
    }
    if native_platform != "nt" or any(env.get(key) != value for key, value in expected.items()):
        raise WindowsVmClockError("Actual clock writes require the disposable hosted Windows gate")


def _iso(value):
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


class _SystemTime(ctypes.Structure):
    _fields_ = [
        (name, wintypes.WORD)
        for name in ("year", "month", "weekday", "day", "hour", "minute", "second", "milliseconds")
    ]


class _Luid(ctypes.Structure):
    _fields_ = [("low", wintypes.DWORD), ("high", wintypes.LONG)]


class _TokenPrivileges(ctypes.Structure):
    _fields_ = [("count", wintypes.DWORD), ("luid", _Luid), ("attributes", wintypes.DWORD)]


class _ServiceStatus(ctypes.Structure):
    _fields_ = [
        (name, wintypes.DWORD)
        for name in (
            "kind",
            "state",
            "accepted",
            "win32_exit",
            "service_exit",
            "checkpoint",
            "wait_hint",
        )
    ]


class _ServiceConfig(ctypes.Structure):
    _fields_ = [
        ("kind", wintypes.DWORD),
        ("start_type", wintypes.DWORD),
        ("error_control", wintypes.DWORD),
        ("binary", wintypes.LPWSTR),
        ("group", wintypes.LPWSTR),
        ("tag", wintypes.DWORD),
        ("dependencies", wintypes.LPWSTR),
        ("account", wintypes.LPWSTR),
        ("display", wintypes.LPWSTR),
    ]


class _NativeClock:
    """Small Win32 boundary; imported safely on non-Windows test machines."""

    def __init__(self):
        _require_vm_environment()
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        signatures = {
            "GetTickCount64": ([], ctypes.c_ulonglong),
            "GetSystemTime": ([ctypes.POINTER(_SystemTime)], None),
            "SetSystemTime": ([ctypes.POINTER(_SystemTime)], wintypes.BOOL),
            "GetCurrentProcess": ([], wintypes.HANDLE),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = arguments, result
        signatures = {
            "OpenProcessToken": (
                [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)],
                wintypes.BOOL,
            ),
            "LookupPrivilegeValueW": (
                [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(_Luid)],
                wintypes.BOOL,
            ),
            "AdjustTokenPrivileges": (
                [
                    wintypes.HANDLE,
                    wintypes.BOOL,
                    ctypes.POINTER(_TokenPrivileges),
                    wintypes.DWORD,
                    ctypes.c_void_p,
                    ctypes.c_void_p,
                ],
                wintypes.BOOL,
            ),
            "OpenSCManagerW": (
                [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD],
                wintypes.HANDLE,
            ),
            "OpenServiceW": ([wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD], wintypes.HANDLE),
            "CloseServiceHandle": ([wintypes.HANDLE], wintypes.BOOL),
            "QueryServiceStatus": (
                [wintypes.HANDLE, ctypes.POINTER(_ServiceStatus)],
                wintypes.BOOL,
            ),
            "ControlService": (
                [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(_ServiceStatus)],
                wintypes.BOOL,
            ),
            "StartServiceW": ([wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
            "QueryServiceConfigW": (
                [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)],
                wintypes.BOOL,
            ),
            "QueryServiceConfig2W": (
                [
                    wintypes.HANDLE,
                    wintypes.DWORD,
                    ctypes.c_void_p,
                    wintypes.DWORD,
                    ctypes.POINTER(wintypes.DWORD),
                ],
                wintypes.BOOL,
            ),
            "ChangeServiceConfig2W": (
                [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p],
                wintypes.BOOL,
            ),
            "ChangeServiceConfigW": (
                [
                    wintypes.HANDLE,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.LPCWSTR,
                    wintypes.LPCWSTR,
                    ctypes.c_void_p,
                    wintypes.LPCWSTR,
                    wintypes.LPCWSTR,
                    wintypes.LPCWSTR,
                    wintypes.LPCWSTR,
                ],
                wintypes.BOOL,
            ),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.advapi, name)
            function.argtypes, function.restype = arguments, result

    def enable_privilege(self):
        token = wintypes.HANDLE()
        if not self.advapi.OpenProcessToken(
            self.kernel.GetCurrentProcess(), 0x28, ctypes.byref(token)
        ):
            raise WindowsVmClockError("Clock privilege token unavailable")
        try:
            privileges = _TokenPrivileges()
            privileges.count, privileges.attributes = 1, 2
            if not self.advapi.LookupPrivilegeValueW(
                None, "SeSystemtimePrivilege", ctypes.byref(privileges.luid)
            ):
                raise WindowsVmClockError("Clock privilege lookup failed")
            ctypes.set_last_error(0)
            result = self.advapi.AdjustTokenPrivileges(
                token, False, ctypes.byref(privileges), 0, None, None
            )
            if not result or ctypes.get_last_error() != 0:
                raise WindowsVmClockError("Clock privilege was not assigned")
        finally:
            self.kernel.CloseHandle(token)

    def ticks_ms(self):
        return int(self.kernel.GetTickCount64())

    def utc(self):
        value = _SystemTime()
        self.kernel.GetSystemTime(ctypes.byref(value))
        return datetime(
            value.year,
            value.month,
            value.day,
            value.hour,
            value.minute,
            value.second,
            value.milliseconds * 1000,
            tzinfo=UTC,
        )

    def set_utc(self, value):
        _require_vm_environment()
        value = value.astimezone(UTC)
        native = _SystemTime(
            value.year,
            value.month,
            0,
            value.day,
            value.hour,
            value.minute,
            value.second,
            value.microsecond // 1000,
        )
        if not self.kernel.SetSystemTime(ctypes.byref(native)):
            raise WindowsVmClockError("Native UTC write failed")

    def service_state(self, name, *, desired=None, start_type=None, delayed_auto_start=None):
        """Read/temporarily change only the two admitted guest sync services."""
        if name not in _SERVICES:
            raise WindowsVmClockError("Unapproved service")
        if desired is not None or start_type is not None or delayed_auto_start is not None:
            _require_vm_environment()
        manager = self.advapi.OpenSCManagerW(None, None, 1)
        if not manager:
            raise WindowsVmClockError("Guest service manager unavailable")
        handle = None
        try:
            handle = self.advapi.OpenServiceW(manager, name, 0x37)
            if not handle:
                if ctypes.get_last_error() == 1060:
                    return {
                        "name": name,
                        "present": False,
                        "running": False,
                        "start_type": None,
                        "delayed_auto_start": None,
                    }
                raise WindowsVmClockError("Guest sync service admission failed")
            required = wintypes.DWORD()
            self.advapi.QueryServiceConfigW(handle, None, 0, ctypes.byref(required))
            if not 1 <= required.value <= 65_536:
                raise WindowsVmClockError("Guest sync configuration unavailable")
            buffer = ctypes.create_string_buffer(required.value)
            if not self.advapi.QueryServiceConfigW(
                handle, buffer, required, ctypes.byref(required)
            ):
                raise WindowsVmClockError("Guest sync configuration read failed")
            config = ctypes.cast(buffer, ctypes.POINTER(_ServiceConfig)).contents
            original_mode = int(config.start_type)
            delayed = wintypes.BOOL()
            if not self.advapi.QueryServiceConfig2W(
                handle, 3, ctypes.byref(delayed), ctypes.sizeof(delayed), ctypes.byref(required)
            ):
                raise WindowsVmClockError("Guest sync delayed-start read failed")
            original_delayed = bool(delayed.value)
            if original_mode not in (2, 3, 4) or (
                start_type is not None and start_type not in (2, 3, 4)
            ):
                raise WindowsVmClockError("Unexpected guest sync startup mode")
            if start_type is not None:
                if not self.advapi.ChangeServiceConfigW(
                    handle,
                    0xFFFFFFFF,
                    start_type,
                    0xFFFFFFFF,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                ):
                    raise WindowsVmClockError("Guest sync startup change failed")
            if delayed_auto_start is not None:
                delayed.value = delayed_auto_start
                if not self.advapi.ChangeServiceConfig2W(handle, 3, ctypes.byref(delayed)):
                    raise WindowsVmClockError("Guest sync delayed-start restore failed")
            status = _ServiceStatus()
            if not self.advapi.QueryServiceStatus(handle, ctypes.byref(status)):
                raise WindowsVmClockError("Guest sync state read failed")
            if desired is not None:
                deadline = self.ticks_ms() + 5000
                while self.ticks_ms() < deadline:
                    if not self.advapi.QueryServiceStatus(handle, ctypes.byref(status)):
                        raise WindowsVmClockError("Guest sync state verification failed")
                    if status.state == (4 if desired else 1):
                        break
                    if desired and status.state == 1:
                        if not self.advapi.StartServiceW(handle, 0, None):
                            if ctypes.get_last_error() != 1056:  # Already running.
                                raise WindowsVmClockError("Guest sync restart failed")
                    elif not desired and status.state == 4:
                        if not self.advapi.ControlService(handle, 1, ctypes.byref(status)):
                            if ctypes.get_last_error() not in (1061, 1062):
                                raise WindowsVmClockError("Guest sync stop failed")
                    elif status.state not in (2, 3, 5, 6):
                        raise WindowsVmClockError("Unexpected guest sync transition")
                    time.sleep(0.05)
            if status.state not in (1, 4) or (
                desired is not None and (status.state == 4) != desired
            ):
                raise WindowsVmClockError("Guest sync state did not settle")
            return {
                "name": name,
                "present": True,
                "running": status.state == 4,
                "start_type": original_mode if start_type is None else start_type,
                "delayed_auto_start": original_delayed
                if delayed_auto_start is None
                else delayed_auto_start,
            }
        finally:
            if handle:
                self.advapi.CloseServiceHandle(handle)
            self.advapi.CloseServiceHandle(manager)


class _ClockController:
    """Recovery logic shared by the watchdog and foreground fallback."""

    def __init__(self, native, seconds, *, baseline=None, services=None):
        if type(seconds) is not int or not 1 <= seconds <= _MAX_LEASE_SECONDS:
            raise WindowsVmClockError("Watchdog lease must be between 1 and 90 seconds")
        self.native, self.seconds = native, seconds
        self.baseline_utc = (
            native.utc()
            if baseline is None
            else datetime.fromisoformat(baseline["utc"].replace("Z", "+00:00"))
        )
        self.baseline_tick_ms = native.ticks_ms() if baseline is None else baseline["tick_ms"]
        self.services = [] if services is None else services
        self.shifts = []
        self.privilege_verified = False
        self.watchdog_armed = False
        self.restore_verified = False
        self.services_restored = False
        self.services_after = []
        self.last_restoration = None

    def arm(self):
        self.native.enable_privilege()
        self.privilege_verified = True
        # Record every original state before the first service mutation.
        self.services = [self.native.service_state(name) for name in _SERVICES]
        for service in self.services:
            if service["present"]:
                # Hyper-V can restart a stopped integration service unless it
                # is temporarily disabled. Restore exact startup modes later.
                self.native.service_state(service["name"], desired=False, start_type=4)
        self.watchdog_armed = True

    def snapshot(self):
        tick = self.native.ticks_ms()
        elapsed = (tick - self.baseline_tick_ms) / 1000
        if elapsed < 0 or elapsed > 3600:
            raise WindowsVmClockError("Native monotonic clock baseline invalid")
        utc = self.native.utc()
        residual = (utc - self.baseline_utc).total_seconds() - elapsed
        return {
            "utc": _iso(utc),
            "tick_ms": tick,
            "elapsed_seconds": round(elapsed, 6),
            "baseline_residual_seconds": round(residual, 6),
        }

    def expired(self):
        return self.native.ticks_ms() - self.baseline_tick_ms >= self.seconds * 1000

    def shift(self, seconds):
        if type(seconds) is not int or seconds not in (-_STEP_SECONDS, _STEP_SECONDS):
            raise WindowsVmClockError("Only the two approved 48-hour VM offsets are allowed")
        if self.expired() or len(self.shifts) >= 2:
            raise WindowsVmClockError("Clock shift lease exhausted")
        if not self.watchdog_armed or not self.privilege_verified:
            raise WindowsVmClockError("Independent watchdog must be armed before a clock step")
        before = self.snapshot()
        if abs(before["baseline_residual_seconds"]) > _RESTORE_TOLERANCE_SECONDS:
            raise WindowsVmClockError("Restore original UTC before the next 48-hour phase")
        self._set_offset(seconds)
        after = self.snapshot()
        actual = (
            datetime.fromisoformat(after["utc"].replace("Z", "+00:00"))
            - datetime.fromisoformat(before["utc"].replace("Z", "+00:00"))
        ).total_seconds() - (after["tick_ms"] - before["tick_ms"]) / 1000
        evidence = {
            "offset_seconds": seconds,
            "actual_step_seconds": round(actual, 6),
            "before": before,
            "after": after,
            "verified": abs(after["baseline_residual_seconds"] - seconds)
            <= _RESTORE_TOLERANCE_SECONDS,
        }
        self.shifts.append(evidence)
        self.restore_verified = False
        if not evidence["verified"]:
            raise WindowsVmClockError("Actual VM clock step was not observed")
        return evidence

    def _set_offset(self, seconds):
        if not self.privilege_verified:
            raise WindowsVmClockError("Clock privilege must be verified before a UTC write")
        elapsed_ms = self.native.ticks_ms() - self.baseline_tick_ms
        if elapsed_ms < 0:
            raise WindowsVmClockError("Native elapsed time moved backwards")
        self.native.set_utc(self.baseline_utc + timedelta(milliseconds=elapsed_ms, seconds=seconds))

    def restore(self):
        self.restore_verified = False
        self._set_offset(0)
        snapshot = self.snapshot()
        self.restore_verified = (
            abs(snapshot["baseline_residual_seconds"]) <= _RESTORE_TOLERANCE_SECONDS
        )
        self.last_restoration = snapshot
        snapshot = {
            **snapshot,
            "restored": self.restore_verified,
            "residual_seconds": snapshot["baseline_residual_seconds"],
        }
        self.last_restoration = snapshot
        if not self.restore_verified:
            raise WindowsVmClockError("Actual VM UTC restoration was not verified")
        return snapshot

    def finish(self, reason):
        clock_error = None
        try:
            self.restore()
        except Exception as error:
            clock_error = error
        service_errors = False
        self.services_after = []
        for service in self.services:
            try:
                if service["present"]:
                    temporarily_startable = service["running"] and service["start_type"] == 4
                    try:
                        self.native.service_state(
                            service["name"],
                            start_type=3 if temporarily_startable else service["start_type"],
                            delayed_auto_start=service["delayed_auto_start"],
                            desired=service["running"],
                        )
                    finally:
                        if temporarily_startable:
                            self.native.service_state(
                                service["name"],
                                start_type=4,
                                delayed_auto_start=service["delayed_auto_start"],
                            )
                restored = self.native.service_state(service["name"])
                self.services_after.append(restored)
                if restored != service:
                    service_errors = True
            except Exception:
                service_errors = True
        self.services_restored = not service_errors
        if clock_error is None:
            try:
                restored = self.snapshot()
                self.restore_verified = (
                    abs(restored["baseline_residual_seconds"]) <= _RESTORE_TOLERANCE_SECONDS
                )
                self.last_restoration = {
                    **restored,
                    "restored": self.restore_verified,
                    "residual_seconds": restored["baseline_residual_seconds"],
                }
            except Exception:
                self.restore_verified = False
        result = self.receipt(reason)
        if not result["clock_restored"] or not result["services_restored"]:
            raise WindowsVmClockError("VM clock or guest sync restoration failed")
        return result

    def receipt(self, reason="active"):
        return {
            "schema_version": 1,
            "reason": reason,
            "native_clock_api": "SetSystemTime",
            "native_elapsed_api": "GetTickCount64",
            "watchdog_seconds": self.seconds,
            "privilege_verified": self.privilege_verified,
            "watchdog_armed": self.watchdog_armed,
            "baseline": {"utc": _iso(self.baseline_utc), "tick_ms": self.baseline_tick_ms},
            "services_before": self.services,
            "services_after": self.services_after,
            "shifts": self.shifts,
            "clock_restored": self.restore_verified,
            "services_restored": self.services_restored,
            "last_restoration": self.last_restoration,
        }


def _write_receipt(path, value):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, allow_nan=False, separators=(",", ":")), encoding="utf-8"
    )
    temporary.replace(path)


def _emit(value):
    sys.stdout.write(json.dumps(value, allow_nan=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _watchdog(path, seconds):
    _require_vm_environment()
    controller = _ClockController(_NativeClock(), seconds)
    commands = queue.Queue(maxsize=4)

    def read_commands():
        try:
            while True:
                line = sys.stdin.readline(513)
                if not line:
                    break
                if len(line) > 512 or not line.endswith("\n"):
                    break
                commands.put(json.loads(line), timeout=0.1)
        except Exception:
            pass
        finally:
            try:
                commands.put(None, timeout=0.1)
            except queue.Full:
                pass

    reason = "error"
    try:
        controller.arm()
        _emit({"ok": True, "ready": controller.receipt()})
        threading.Thread(target=read_commands, daemon=True).start()
        while not controller.expired():
            try:
                command = commands.get(timeout=0.05)
            except queue.Empty:
                continue
            if command is None:
                reason = "parent_eof"
                break
            operation = command.get("operation") if type(command) is dict else None
            if operation == "close":
                reason = "closed"
                break
            if operation == "shift":
                value = controller.shift(command.get("seconds"))
            elif operation == "restore":
                value = controller.restore()
            elif operation == "snapshot":
                value = controller.snapshot()
            else:
                raise WindowsVmClockError("Invalid watchdog operation")
            _emit({"ok": True, "value": value})
        else:
            reason = "watchdog_timeout"
    except Exception:
        # No exception text, local paths, environment values or credentials.
        reason = "error"
    finally:
        try:
            result = controller.finish(reason)
        except Exception:
            result = controller.receipt("restore_failed")
        _write_receipt(path, result)
        try:
            _emit(
                {"ok": result["clock_restored"] and result["services_restored"], "closed": result}
            )
        except (BrokenPipeError, OSError):
            pass


class WindowsVmClockGuard:
    """Bounded context manager for an explicitly enabled actual VM clock test."""

    def __init__(self, artifact_path, *, watchdog_seconds=90):
        _require_vm_environment()
        if type(watchdog_seconds) is not int or not 1 <= watchdog_seconds <= 90:
            raise WindowsVmClockError("Watchdog lease must be between 1 and 90 seconds")
        self.path, self.seconds = Path(artifact_path), watchdog_seconds
        if not self.path.parent.is_dir() or self.path.exists():
            raise WindowsVmClockError(
                "VM clock receipt requires a fresh path in an existing directory"
            )
        self.process = None
        self.fallback = None
        self.responses = queue.Queue(maxsize=8)
        self._result = None

    def __enter__(self):
        _require_vm_environment()
        native = _NativeClock()
        native.enable_privilege()
        # Foreground recovery knows all original service states even if the
        # child fails partway through arming and cannot send its ready message.
        self.fallback = _ClockController(native, self.seconds)
        self.fallback.privilege_verified = True
        self.fallback.services = [native.service_state(name) for name in _SERVICES]
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                str(Path(__file__).resolve()),
                "--watchdog",
                str(self.path.resolve()),
                str(self.seconds),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
            # Deliberately inherit RUNNER_TRACKING_ID and the normal job environment.
        )

        def read_responses():
            try:
                while True:
                    line = self.process.stdout.readline(16_385)
                    if not line or len(line) > 16_384:
                        break
                    self.responses.put(json.loads(line), timeout=0.1)
            except Exception:
                pass
            finally:
                try:
                    self.responses.put(None, timeout=0.1)
                except queue.Full:
                    pass

        threading.Thread(target=read_responses, daemon=True).start()
        try:
            response = self._receive(timeout=15)
            ready = response.get("ready")
            if (
                response.get("ok") is not True
                or type(ready) is not dict
                or ready.get("privilege_verified") is not True
            ):
                raise WindowsVmClockError("Independent VM watchdog did not arm")
            self.fallback = _ClockController(
                native, self.seconds, baseline=ready["baseline"], services=ready["services_before"]
            )
            self.fallback.privilege_verified = True
            self.fallback.watchdog_armed = ready["watchdog_armed"]
            return self
        except BaseException:
            self._close()
            raise

    def _receive(self, *, timeout=10):
        try:
            response = self.responses.get(timeout=timeout)
        except queue.Empty:
            raise WindowsVmClockError("VM watchdog response timed out") from None
        if type(response) is not dict:
            raise WindowsVmClockError("VM watchdog exited unexpectedly")
        return response

    def _request(self, operation, **fields):
        if self.process is None or self.process.poll() is not None:
            raise WindowsVmClockError("VM watchdog is not running")
        try:
            self.process.stdin.write(json.dumps({"operation": operation, **fields}) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            raise WindowsVmClockError("VM watchdog command failed") from None
        response = self._receive()
        if response.get("ok") is not True or "value" not in response:
            raise WindowsVmClockError("VM watchdog rejected the clock operation")
        return response["value"]

    def shift(self, offset_seconds):
        return self._request("shift", seconds=offset_seconds)

    def restore(self):
        return self._request("restore")

    def snapshot(self):
        return self._request("snapshot")

    def receipt(self):
        if self._result is not None:
            return self._result
        raise WindowsVmClockError("Final watchdog receipt is available after context exit")

    def _close(self):
        child_restored = False
        try:
            if self.process is not None:
                if self.process.poll() is None:
                    try:
                        self.process.stdin.write('{"operation":"close"}\n')
                        self.process.stdin.flush()
                    except (BrokenPipeError, OSError):
                        pass
                    try:
                        self.process.stdin.close()  # EOF also requests restoration.
                    except (BrokenPipeError, OSError):
                        pass
                try:
                    self.process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    # Stop a hung writer before foreground recovery so it
                    # cannot overwrite the recovered clock or service states.
                    self.process.kill()
                    self.process.wait(timeout=5)
                if self.path.is_file():
                    candidate = json.loads(self.path.read_text(encoding="utf-8"))
                    if (
                        candidate.get("clock_restored") is True
                        and candidate.get("services_restored") is True
                    ):
                        self._result = candidate
                        current = self.fallback.snapshot()
                        states = [self.fallback.native.service_state(name) for name in _SERVICES]
                        child_restored = (
                            abs(current["baseline_residual_seconds"]) <= _RESTORE_TOLERANCE_SECONDS
                            and states == self.fallback.services
                        )
        finally:
            # A failed pipe close or receipt read must never leave a second
            # clock writer alive while foreground restoration is running.
            if self.process is not None and self.process.poll() is None:
                self.process.kill()
                self.process.wait(timeout=5)
            if not child_restored and self.fallback is not None:
                try:
                    self._result = self.fallback.finish("foreground_fallback")
                except BaseException:
                    self._result = self.fallback.receipt("restore_failed")
                    _write_receipt(self.path, self._result)
                    raise
                else:
                    _write_receipt(self.path, self._result)
        if not child_restored and self.fallback is None:
            raise WindowsVmClockError("VM watchdog initialization/restoration was not verified")

    def __exit__(self, kind, value, traceback):
        self._close()
        return False


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "--watchdog":
        raise SystemExit(2)
    _watchdog(Path(sys.argv[2]), int(sys.argv[3]))
