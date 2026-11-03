"""Disposable Windows-only registry/service/UTC restoration for time qualification.

No production entry point imports this module. The independent child restores
the original Windows Time values and services on foreground EOF or a native
monotonic deadline; foreground recovery repeats restoration if the child fails.
"""

from __future__ import annotations

import importlib.util
import json
import os
import queue
import re
import subprocess
import sys
import threading
from pathlib import Path

_CLOCK_PATH = Path(__file__).with_name("windows_vm_clock.py")
_SPEC = importlib.util.spec_from_file_location("k5_native_time_restoration_clock", _CLOCK_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_CLOCK = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_CLOCK)
_KEY = r"SYSTEM\CurrentControlSet\Services\W32Time\Parameters"
_VALUES = ("Type", "NtpServer")
_REQUEST_NAME = re.compile(r"\.time-admin-[a-f0-9]{32}\.json\Z")


def _require_environment():
    _CLOCK._require_vm_environment()
    if os.environ.get("K5_NATIVE_TIME_ACCEPTANCE") != "1":
        raise _CLOCK.WindowsVmClockError("Time configuration requires its disposable Windows gate")


def _configuration():
    _require_environment()
    import winreg

    values = []
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _KEY) as key:
        for name in _VALUES:
            try:
                value, kind = winreg.QueryValueEx(key, name)
            except FileNotFoundError:
                values.append({"name": name, "present": False, "value": None, "kind": None})
            else:
                if (
                    type(value) is not str
                    or len(value) > 4096
                    or kind
                    not in {
                        winreg.REG_SZ,
                        winreg.REG_EXPAND_SZ,
                    }
                ):
                    raise _CLOCK.WindowsVmClockError("Unrecognized original time configuration")
                values.append({"name": name, "present": True, "value": value, "kind": kind})
    return values


def _restore_configuration(values):
    _require_environment()
    import winreg

    if type(values) is not list or [entry["name"] for entry in values] != list(_VALUES):
        raise _CLOCK.WindowsVmClockError("Time configuration restore identity refused")
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _KEY, 0, winreg.KEY_SET_VALUE) as key:
        for entry in values:
            if entry["present"]:
                winreg.SetValueEx(key, entry["name"], 0, entry["kind"], entry["value"])
            else:
                try:
                    winreg.DeleteValue(key, entry["name"])
                except FileNotFoundError:
                    pass
    if _configuration() != values:
        raise _CLOCK.WindowsVmClockError("Time configuration restoration did not match")


class _Controller:
    def __init__(self, profile, seconds, *, snapshot=None):
        _require_environment()
        if type(seconds) is not int or not 1 <= seconds <= 180:
            raise _CLOCK.WindowsVmClockError("Time configuration lease refused")
        self.profile = Path(profile)
        self.native = _CLOCK._NativeClock()
        self.native.enable_privilege()
        self.clock = _CLOCK._ClockController(
            self.native,
            min(seconds, 90),
            baseline=None if snapshot is None else snapshot["baseline"],
            services=None if snapshot is None else snapshot["services"],
        )
        self.clock.privilege_verified = True
        self.clock.seconds = seconds
        if snapshot is None:
            self.clock.services = [self.native.service_state(name) for name in _CLOCK._SERVICES]
        self.values = _configuration() if snapshot is None else snapshot["configuration"]
        self.restored = False

    def snapshot(self):
        return {
            "baseline": self.clock.receipt()["baseline"],
            "services": self.clock.services,
            # Private pipe only: source strings never enter a public receipt.
            "configuration": self.values,
        }

    def arm(self):
        self.clock.arm()

    def finish(self, reason):
        configuration_error = None
        try:
            directory = self.profile / "time-admin"
            if directory.is_dir():
                for path in directory.iterdir():
                    if _REQUEST_NAME.fullmatch(path.name):
                        path.unlink(missing_ok=True)
            self.native.service_state("W32Time", desired=False)
            _restore_configuration(self.values)
            self.restored = True
        except Exception as error:
            configuration_error = error
        try:
            result = self.clock.finish(reason)
        except Exception:
            result = self.clock.receipt("restore_failed")
        result["time_configuration_restored"] = self.restored
        result["configuration_watchdog"] = True
        if configuration_error is not None:
            result["reason"] = "configuration_restore_failed"
        return result

    def verified(self):
        return (
            _configuration() == self.values
            and [self.native.service_state(name) for name in _CLOCK._SERVICES]
            == self.clock.services
            and abs(self.clock.snapshot()["baseline_residual_seconds"]) <= 2
        )


def _watchdog(profile, output, seconds):
    controller = _Controller(profile, seconds)
    commands = queue.Queue(maxsize=1)

    def read():
        try:
            sys.stdin.buffer.read(1)
        finally:
            commands.put(True)

    reason = "error"
    try:
        controller.arm()
        _CLOCK._emit({"ready": True, "snapshot": controller.snapshot()})
        threading.Thread(target=read, daemon=True).start()
        while not controller.clock.expired():
            try:
                commands.get(timeout=0.05)
            except queue.Empty:
                continue
            reason = "parent_closed"
            break
        else:
            reason = "watchdog_timeout"
    finally:
        result = controller.finish(reason)
        _CLOCK._write_receipt(Path(output), result)


class WindowsTimeConfigurationGuard:
    def __init__(self, profile, output, *, seconds=180):
        _require_environment()
        self.profile, self.output, self.seconds = Path(profile), Path(output), seconds
        if self.output.exists() or not self.output.parent.is_dir():
            raise _CLOCK.WindowsVmClockError("Time configuration receipt path refused")
        self.fallback = _Controller(profile, seconds)
        self.process = None
        self.result = None

    def __enter__(self):
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                str(Path(__file__).resolve()),
                "--watchdog",
                str(self.profile),
                str(self.output),
                str(self.seconds),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        ready = queue.Queue(maxsize=1)

        def read_ready():
            try:
                raw = self.process.stdout.readline(16385)
                ready.put(json.loads(raw) if 0 < len(raw) <= 16384 else None)
            except Exception:
                ready.put(None)

        threading.Thread(target=read_ready, daemon=True).start()
        try:
            value = ready.get(timeout=15)
            if type(value) is not dict or value.get("ready") is not True:
                raise _CLOCK.WindowsVmClockError("Time configuration watchdog unavailable")
            self.fallback = _Controller(self.profile, self.seconds, snapshot=value["snapshot"])
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, kind, value, traceback):
        restored = False
        try:
            if self.process is not None:
                if self.process.poll() is None:
                    self.process.stdin.close()
                try:
                    self.process.wait(timeout=25)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
                if self.output.is_file():
                    candidate = json.loads(self.output.read_text(encoding="utf-8"))
                    if (
                        all(
                            candidate.get(key) is True
                            for key in (
                                "clock_restored",
                                "services_restored",
                                "time_configuration_restored",
                            )
                        )
                        and self.fallback.verified()
                    ):
                        self.result, restored = candidate, True
        finally:
            if self.process is not None and self.process.poll() is None:
                self.process.kill()
                self.process.wait(timeout=5)
            if not restored:
                self.result = self.fallback.finish("foreground_fallback")
                _CLOCK._write_receipt(self.output, self.result)
            if not all(
                self.result.get(key) is True
                for key in ("clock_restored", "services_restored", "time_configuration_restored")
            ):
                raise _CLOCK.WindowsVmClockError("Native time configuration restoration failed")
        return False


if __name__ == "__main__":
    if len(sys.argv) != 5 or sys.argv[1] != "--watchdog":
        raise SystemExit(2)
    _watchdog(Path(sys.argv[2]), Path(sys.argv[3]), int(sys.argv[4]))
