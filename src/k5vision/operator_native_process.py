"""Own native service processes without launching outside their cleanup boundary.

Windows starts an isolated Python relay, assigns it to a kill-on-close Job Object,
then opens its stdin gate. Relay and native children never create console windows;
native children inherit the owned job and process group.
Linux uses a new session, retaining the leader until its process group is stopped.
Arguments contain executable/configuration paths, never private source URLs.
"""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

_WINDOWS = os.name == "nt"
_START_GATE = b"K5_NATIVE_START_V1\n"
_GRACE_SECONDS = 5.0
_REAP_SECONDS = 2.0
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000


class NativeProcessError(RuntimeError):
    """Fixed diagnostics that exclude native arguments and operating-system errors."""


class _WindowsJob:
    def __init__(self) -> None:
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_int64),
                ("job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD),
                ("minimum_working_set", ctypes.c_size_t),
                ("maximum_working_set", ctypes.c_size_t),
                ("active_processes", wintypes.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("basic", BasicLimits),
                ("io_counters", ctypes.c_uint64 * 6),
                ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t),
                ("peak_process_memory", ctypes.c_size_t),
                ("peak_job_memory", ctypes.c_size_t),
            ]

        self._handle = None
        self._api = ctypes.WinDLL("kernel32", use_last_error=True)
        self._api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self._api.CreateJobObjectW.restype = wintypes.HANDLE
        self._api.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self._api.SetInformationJobObject.restype = wintypes.BOOL
        self._api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self._api.AssignProcessToJobObject.restype = wintypes.BOOL
        self._api.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self._api.TerminateJobObject.restype = wintypes.BOOL
        self._api.CloseHandle.argtypes = [wintypes.HANDLE]
        self._api.CloseHandle.restype = wintypes.BOOL
        handle = self._api.CreateJobObjectW(None, None)
        if not handle:
            raise NativeProcessError("Native process ownership could not be created.")
        self._handle = handle
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._api.SetInformationJobObject(
            handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            self.close()
            raise NativeProcessError("Native process ownership could not be configured.")

    def assign(self, process: subprocess.Popen[bytes]) -> None:
        if not self._api.AssignProcessToJobObject(self._handle, int(process._handle)):
            raise NativeProcessError("Native process ownership could not be assigned.")

    def terminate(self) -> None:
        if not self._api.TerminateJobObject(self._handle, 1):
            raise NativeProcessError("Native process termination failed.")

    def close(self) -> None:
        if self._handle is not None:
            handle, self._handle = self._handle, None
            if not self._api.CloseHandle(handle):
                self._handle = handle
                raise NativeProcessError("Native process ownership could not be released.")


class OwnedNativeProcess:
    """Start one service and stop only its owned process tree.

    Startup establishes ownership, not application readiness. The service caller
    separately probes its fixed private endpoint. Call ``stop`` even after exit.
    """

    def __init__(self, arguments: list[str], *, cwd: Path) -> None:
        if (
            not isinstance(arguments, list)
            or not 1 <= len(arguments) <= 64
            or any(
                not isinstance(value, str)
                or not value
                or len(value) > 4096
                or any(item in value for item in ("\x00", "\r", "\n", "://"))
                for value in arguments
            )
            or not Path(arguments[0]).is_absolute()
            or not isinstance(cwd, Path)
            or not cwd.is_absolute()
            or not cwd.is_dir()
        ):
            raise NativeProcessError("Native process configuration is invalid.")
        self._stop_lock = threading.Lock()
        self._job: _WindowsJob | None = None
        self._stopped = False
        self._process: subprocess.Popen[bytes] | None = None
        try:
            if _WINDOWS:
                self._job = _WindowsJob()
                relay = [sys.executable, "-I", "-B", "-S", str(Path(__file__).resolve()), "--relay"]
                self._process = subprocess.Popen(
                    [*relay, *arguments],
                    cwd=cwd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=_CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW,
                    close_fds=True,
                )
                self._job.assign(self._process)
                if self._process.stdin is None:
                    raise NativeProcessError("Native process start gate is unavailable.")
                self._process.stdin.write(_START_GATE)
                self._process.stdin.flush()
                self._process.stdin.close()
            else:
                if not hasattr(os, "waitid") or not hasattr(os, "WNOWAIT"):
                    raise NativeProcessError("Native process ownership is unavailable.")
                self._process = subprocess.Popen(
                    list(arguments),
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    close_fds=True,
                )
        except Exception:
            self._abort_start()
            raise NativeProcessError("Native process could not be started.") from None

    @property
    def pid(self) -> int:
        assert self._process is not None
        return self._process.pid

    def poll(self) -> int | None:
        """Observe exit without releasing the Linux process-group leader's PID."""
        process = self._process
        assert process is not None
        if _WINDOWS or process.returncode is not None:
            return process.poll()
        # WNOWAIT reserves the leader's PID until stop() signals the group. A
        # prematurely reaped leader could let that number identify a foreign group.
        try:
            result = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            # stop() may have reaped the leader on another thread after the
            # returncode check above. Popen publishes that result during wait().
            return process.poll()
        if result is None:
            return None
        return result.si_status if result.si_code == os.CLD_EXITED else -result.si_status

    def _abort_start(self) -> None:
        process = self._process
        try:
            if process is not None:
                if process.stdin is not None:
                    process.stdin.close()
                process.kill()
                process.wait(timeout=_REAP_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            pass
        finally:
            if self._job is not None:
                self._job.close()
                self._job = None

    def _stop_windows(self, process: subprocess.Popen[bytes]) -> None:
        job = self._job
        assert job is not None
        try:
            if process.poll() is None:
                try:
                    process.send_signal(getattr(signal, "CTRL_BREAK_EVENT", 1))
                except OSError:
                    # Consoleless workers cannot receive console control events.
                    # Keep the bounded wait, then terminate only the owned job.
                    pass
                try:
                    process.wait(timeout=_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    try:
                        job.terminate()
                    except NativeProcessError:
                        # The kill-on-close boundary below is the final fallback.
                        pass
        finally:
            # Closing also kills descendants left behind by a normally exited relay.
            job.close()
            self._job = None
        process.wait(timeout=_REAP_SECONDS)

    def _stop_linux(self, process: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + _GRACE_SECONDS
        while self.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        try:
            # The unreaped leader still reserves this group ID. Also stop any
            # descendants that outlived the leader after its graceful exit.
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=_REAP_SECONDS)

    def stop(self) -> None:
        """Gracefully stop, then force-stop and reap within fixed waiting bounds."""
        with self._stop_lock:
            if self._stopped:
                return
            process = self._process
            assert process is not None
            try:
                if _WINDOWS:
                    self._stop_windows(process)
                else:
                    self._stop_linux(process)
            except (OSError, subprocess.TimeoutExpired):
                raise NativeProcessError("Native process cleanup is incomplete.") from None
            self._stopped = True


def _relay(arguments: list[str]) -> int:
    """Run only as an isolated script, after the parent's ownership gate opens."""
    if not _WINDOWS or not arguments:
        return 1
    gate = sys.stdin.buffer.read(len(_START_GATE))
    if gate != _START_GATE or sys.stdin.buffer.read(1):
        return 1
    # Keep the relay waiting so native exit status remains observable. Consoleless
    # workers are still contained by the parent's kill-on-close Job Object.
    signal.signal(signal.SIGBREAK, lambda _signal, _frame: None)
    try:
        process = subprocess.Popen(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW,
            close_fds=True,
        )
        return process.wait()
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(_relay(sys.argv[2:]) if sys.argv[1:2] == ["--relay"] else 1)
