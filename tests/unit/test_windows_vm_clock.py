"""Safety gates and recovery arithmetic; these tests never write host time."""

from __future__ import annotations

import ctypes
import importlib.util
import io
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_PATH = Path(__file__).parents[1] / "integration" / "windows_vm_clock.py"
_SPEC = importlib.util.spec_from_file_location("k5_vm_clock_guard_units", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
guard = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(guard)

_ENVIRONMENT = {
    "K5_VM_CLOCK_ACCEPTANCE": "1",
    "GITHUB_ACTIONS": "true",
    "RUNNER_ENVIRONMENT": "github-hosted",
    "RUNNER_OS": "Windows",
}


class FakeNative:
    def __init__(self):
        self.tick = 500_000
        self.wall = datetime(2026, 10, 18, 12, tzinfo=UTC)
        self.writes = []
        self.operations = []
        self.privilege = True
        self.fail_writes = False
        self.write_bias = 0
        self.services = {
            "W32Time": {
                "name": "W32Time",
                "present": True,
                "running": True,
                "start_type": 2,
                "delayed_auto_start": True,
            },
            "vmictimesync": {
                "name": "vmictimesync",
                "present": True,
                "running": False,
                "start_type": 3,
                "delayed_auto_start": False,
            },
        }

    def enable_privilege(self):
        if not self.privilege:
            raise guard.WindowsVmClockError("test privilege unavailable")
        self.operations.append("privilege")

    def utc(self):
        return self.wall

    def ticks_ms(self):
        return self.tick

    def advance(self, seconds):
        self.tick += int(seconds * 1000)
        self.wall += timedelta(seconds=seconds)

    def set_utc(self, value):
        self.operations.append("clock_write")
        if self.fail_writes:
            raise guard.WindowsVmClockError("test clock write failed")
        self.writes.append(value)
        self.wall = value + timedelta(seconds=self.write_bias)

    def service_state(self, name, *, desired=None, start_type=None, delayed_auto_start=None):
        value = self.services[name]
        if not value["present"]:
            return dict(value)
        if start_type is not None:
            self.operations.append((name, "mode", start_type))
            value["start_type"] = start_type
        if delayed_auto_start is not None:
            value["delayed_auto_start"] = delayed_auto_start
        if desired is not None:
            self.operations.append((name, "running", desired))
            value["running"] = desired
        return dict(value)


def _armed(seconds=90):
    native = FakeNative()
    controller = guard._ClockController(native, seconds)
    controller.arm()
    return native, controller


@pytest.mark.parametrize("missing", list(_ENVIRONMENT))
def test_every_environment_gate_is_required_before_native_construction(
    monkeypatch, tmp_path, missing
):
    environment = {key: value for key, value in _ENVIRONMENT.items() if key != missing}
    with pytest.raises(guard.WindowsVmClockError):
        guard._require_vm_environment(environment, platform="nt")
    monkeypatch.setattr(guard.os, "environ", environment)
    calls = []
    monkeypatch.setattr(guard, "_NativeClock", lambda: calls.append("native"))
    with pytest.raises(guard.WindowsVmClockError):
        guard.WindowsVmClockGuard(tmp_path / "clock.json")
    assert not calls


@pytest.mark.parametrize("platform", ["posix", "java", ""])
def test_hosted_flags_never_admit_non_windows(platform):
    with pytest.raises(guard.WindowsVmClockError):
        guard._require_vm_environment(_ENVIRONMENT, platform=platform)


def test_exact_environment_gate_accepts_only_dedicated_hosted_windows():
    guard._require_vm_environment(_ENVIRONMENT, platform="nt")
    for key in _ENVIRONMENT:
        changed = {**_ENVIRONMENT, key: "TRUE" if key == "GITHUB_ACTIONS" else "other"}
        with pytest.raises(guard.WindowsVmClockError):
            guard._require_vm_environment(changed, platform="nt")


@pytest.mark.parametrize("seconds", [True, 0, -1, 91, 1.5])
def test_lease_is_strictly_bounded(seconds):
    native = FakeNative()
    with pytest.raises(guard.WindowsVmClockError):
        guard._ClockController(native, seconds)
    assert not native.operations and not native.writes


def test_arm_proves_privilege_and_records_both_original_states_before_mutating():
    native, controller = _armed()
    assert controller.watchdog_armed and controller.privilege_verified
    assert native.operations[0] == "privilege"
    assert not native.writes
    assert controller.services[0]["running"] is True
    assert controller.services[0]["start_type"] == 2
    assert controller.services[0]["delayed_auto_start"] is True
    assert controller.services[1]["running"] is False
    assert all(
        not service["running"] and service["start_type"] == 4
        for service in native.services.values()
    )


def test_missing_privilege_causes_no_clock_or_service_mutation():
    native = FakeNative()
    native.privilege = False
    controller = guard._ClockController(native, 90)
    with pytest.raises(guard.WindowsVmClockError):
        controller.arm()
    assert not native.writes and not native.operations


def test_both_native_steps_restore_to_baseline_plus_elapsed_and_exact_services():
    native, controller = _armed()
    baseline = native.wall
    for offset in (172800, -172800):
        native.advance(3)
        shift = controller.shift(offset)
        assert shift["verified"] is True
        assert shift["actual_step_seconds"] == offset
        assert shift["after"]["baseline_residual_seconds"] == offset
        native.advance(12)
        assert controller.snapshot()["baseline_residual_seconds"] == offset
        restored = controller.restore()
        assert restored["restored"] is True and restored["residual_seconds"] == 0
        assert native.wall == baseline + timedelta(milliseconds=native.tick - 500_000)
    result = controller.finish("closed")
    assert result["clock_restored"] and result["services_restored"]
    assert result["watchdog_armed"] and result["privilege_verified"]
    assert result["native_clock_api"] == "SetSystemTime"
    assert result["native_elapsed_api"] == "GetTickCount64"
    assert result["services_before"] == result["services_after"]
    assert native.services["W32Time"]["running"] is True
    assert native.services["W32Time"]["start_type"] == 2
    assert native.services["W32Time"]["delayed_auto_start"] is True
    assert native.services["vmictimesync"]["running"] is False
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("offset", [True, 0, 1, 86400, 172801, -172801, 172800.0])
def test_no_unapproved_clock_step_is_written(offset):
    native, controller = _armed()
    with pytest.raises(guard.WindowsVmClockError):
        controller.shift(offset)
    assert not native.writes


def test_two_step_limit_and_native_elapsed_lease_cannot_be_evaded_by_wall_time():
    native, controller = _armed()
    controller.shift(172800)
    controller.restore()
    controller.shift(-172800)
    with pytest.raises(guard.WindowsVmClockError):
        controller.shift(172800)
    native.advance(90)
    assert controller.expired()
    result = controller.finish("watchdog_timeout")
    assert result["clock_restored"] and result["last_restoration"]["residual_seconds"] == 0


def test_step_cannot_begin_after_native_lease_deadline():
    native, controller = _armed(seconds=5)
    native.advance(5)
    with pytest.raises(guard.WindowsVmClockError):
        controller.shift(172800)
    assert not native.writes


def test_unobserved_step_fails_and_recovery_does_not_claim_success():
    native, controller = _armed()
    native.write_bias = 3
    with pytest.raises(guard.WindowsVmClockError):
        controller.shift(172800)
    with pytest.raises(guard.WindowsVmClockError):
        controller.finish("error")
    assert controller.restore_verified is False and controller.services_restored


def test_clock_writes_require_privilege_and_prearmed_watchdog():
    native = FakeNative()
    controller = guard._ClockController(native, 90)
    with pytest.raises(guard.WindowsVmClockError):
        controller.shift(172800)
    with pytest.raises(guard.WindowsVmClockError):
        controller.restore()
    assert not native.writes


def test_a_literal_48_hour_step_requires_restoration_between_phases():
    native, controller = _armed()
    controller.shift(172800)
    with pytest.raises(guard.WindowsVmClockError):
        controller.shift(-172800)
    assert len(native.writes) == 1
    controller.restore()
    assert controller.shift(-172800)["actual_step_seconds"] == -172800


def test_failed_clock_restoration_still_restores_guest_service_modes_and_running_states():
    native, controller = _armed()
    controller.shift(172800)
    native.fail_writes = True
    with pytest.raises(guard.WindowsVmClockError):
        controller.finish("error")
    assert controller.services_restored is True
    assert controller.restore_verified is False
    assert [native.services[name] for name in guard._SERVICES] == controller.services


def test_partial_arm_failure_can_restore_all_original_service_states():
    native = FakeNative()
    original = native.service_state

    def fail_second(name, **fields):
        if name == "vmictimesync" and fields.get("start_type") == 4:
            raise guard.WindowsVmClockError("test stop failure")
        return original(name, **fields)

    native.service_state = fail_second
    controller = guard._ClockController(native, 90)
    with pytest.raises(guard.WindowsVmClockError):
        controller.arm()
    assert not controller.watchdog_armed
    assert controller.finish("error")["services_restored"] is True


def test_absent_optional_integration_service_is_preserved_as_absent():
    native = FakeNative()
    native.services["vmictimesync"] = {
        "name": "vmictimesync",
        "present": False,
        "running": False,
        "start_type": None,
        "delayed_auto_start": None,
    }
    controller = guard._ClockController(native, 90)
    controller.arm()
    result = controller.finish("closed")
    assert result["services_before"] == result["services_after"]
    assert result["services_restored"]


def test_originally_running_but_disabled_service_is_started_before_restoring_disabled_mode():
    native = FakeNative()
    native.services["W32Time"]["start_type"] = 4
    native.services["W32Time"]["delayed_auto_start"] = False
    original = native.service_state

    def admit_disabled_service_start(name, **fields):
        if fields.get("desired") and fields.get("start_type") == 4:
            raise guard.WindowsVmClockError("A disabled service cannot be started")
        return original(name, **fields)

    native.service_state = admit_disabled_service_start
    controller = guard._ClockController(native, 90)
    controller.arm()
    result = controller.finish("closed")
    assert result["services_before"] == result["services_after"]
    assert result["services_restored"]
    assert ("W32Time", "mode", 3) in native.operations
    assert native.operations[-1] == ("vmictimesync", "running", False)


def test_foreground_kills_hung_writer_before_fallback_even_if_pipe_close_fails(tmp_path):
    native, controller = _armed()
    controller.shift(172800)
    events = []
    original = native.set_utc

    def record_restoration(value):
        events.append("restore")
        original(value)

    native.set_utc = record_restoration

    class BrokenPipe:
        def write(self, _value):
            raise OSError("closed pipe")

        def flush(self):
            raise OSError("closed pipe")

        def close(self):
            raise OSError("closed pipe")

    class HungChild:
        stdin = BrokenPipe()
        exited = False

        def poll(self):
            return 0 if self.exited else None

        def wait(self, timeout):
            if not self.exited:
                raise subprocess.TimeoutExpired("watchdog", timeout)
            return 0

        def kill(self):
            events.append("kill")
            self.exited = True

    lease = guard.WindowsVmClockGuard.__new__(guard.WindowsVmClockGuard)
    lease.path = tmp_path / "receipt.json"
    lease.process = HungChild()
    lease.fallback = controller
    lease._result = None
    lease._close()
    assert events == ["kill", "restore"]
    assert lease.receipt()["clock_restored"] and lease.receipt()["services_restored"]
    assert lease.process.poll() == 0


def test_watchdog_eof_restores_actual_controller_and_retains_safe_finite_receipt(
    monkeypatch, tmp_path
):
    native = FakeNative()
    monkeypatch.setattr(guard, "_require_vm_environment", lambda: None)
    monkeypatch.setattr(guard, "_NativeClock", lambda: native)
    monkeypatch.setattr(guard.sys, "stdin", io.StringIO('{"operation":"shift","seconds":172800}\n'))
    output = io.StringIO()
    monkeypatch.setattr(guard.sys, "stdout", output)
    path = tmp_path / "watchdog.json"
    guard._watchdog(path, 90)
    result = json.loads(path.read_text())
    assert result["reason"] == "parent_eof"
    assert result["clock_restored"] and result["services_restored"]
    assert result["last_restoration"]["residual_seconds"] == 0
    assert len(result["shifts"]) == 1
    assert len(native.writes) == 2
    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    assert messages[0]["ready"]["watchdog_armed"]
    assert messages[-1]["closed"] == result


def test_watchdog_error_after_mutation_restores_before_reporting_failure(monkeypatch, tmp_path):
    native = FakeNative()
    monkeypatch.setattr(guard, "_require_vm_environment", lambda: None)
    monkeypatch.setattr(guard, "_NativeClock", lambda: native)
    monkeypatch.setattr(
        guard.sys,
        "stdin",
        io.StringIO('{"operation":"shift","seconds":-172800}\n{"operation":"invalid"}\n'),
    )
    monkeypatch.setattr(guard.sys, "stdout", io.StringIO())
    path = tmp_path / "watchdog.json"
    guard._watchdog(path, 90)
    result = json.loads(path.read_text())
    assert result["reason"] == "error"
    assert result["clock_restored"] and result["services_restored"]
    assert result["last_restoration"]["residual_seconds"] == 0


def test_foreground_recovery_uses_cross_process_native_tick_baseline():
    native, watchdog = _armed()
    ready = watchdog.receipt()
    watchdog.shift(-172800)
    native.advance(8)
    foreground = guard._ClockController(
        native, 90, baseline=ready["baseline"], services=ready["services_before"]
    )
    foreground.privilege_verified = True
    result = foreground.finish("foreground_fallback")
    assert result["clock_restored"] and result["services_restored"]
    assert result["last_restoration"]["elapsed_seconds"] == 8
    assert result["last_restoration"]["residual_seconds"] == 0


@pytest.mark.parametrize(
    "desired,states,expected", [(True, [3, 1, 2, 4], "start"), (False, [2, 4, 3, 1], "stop")]
)
def test_native_service_recovery_reconciles_pending_transitions(
    monkeypatch, desired, states, expected
):
    calls = []

    class FakeServiceApi:
        def OpenSCManagerW(self, *_):
            return 1

        def OpenServiceW(self, *_):
            return 2

        def CloseServiceHandle(self, *_):
            return True

        def QueryServiceConfigW(self, _handle, buffer, _size, required):
            ctypes.cast(
                required, ctypes.POINTER(guard.wintypes.DWORD)
            ).contents.value = ctypes.sizeof(guard._ServiceConfig)
            if buffer is not None:
                ctypes.cast(buffer, ctypes.POINTER(guard._ServiceConfig)).contents.start_type = 2
            return buffer is not None

        def QueryServiceConfig2W(self, _handle, _level, buffer, _size, _required):
            ctypes.cast(buffer, ctypes.POINTER(guard.wintypes.BOOL)).contents.value = False
            return True

        def QueryServiceStatus(self, _handle, status):
            value = states.pop(0) if len(states) > 1 else states[0]
            ctypes.cast(status, ctypes.POINTER(guard._ServiceStatus)).contents.state = value
            return True

        def StartServiceW(self, *_):
            calls.append("start")
            return True

        def ControlService(self, *_):
            calls.append("stop")
            return True

    monkeypatch.setattr(guard, "_require_vm_environment", lambda: None)
    monkeypatch.setattr(guard.time, "sleep", lambda _: None)
    native = guard._NativeClock.__new__(guard._NativeClock)
    native.advapi = FakeServiceApi()
    native.ticks_ms = lambda: 0
    result = native.service_state("W32Time", desired=desired)
    assert result["running"] is desired
    assert calls == [expected]


def test_native_watchdog_deadline_restores_even_without_parent_restore_command(
    monkeypatch, tmp_path
):
    native = FakeNative()
    original = native.set_utc

    def advance_after_shift(value):
        original(value)
        if abs((native.wall - datetime(2026, 10, 18, 12, tzinfo=UTC)).total_seconds()) > 100:
            native.advance(2)

    native.set_utc = advance_after_shift
    monkeypatch.setattr(guard, "_require_vm_environment", lambda: None)
    monkeypatch.setattr(guard, "_NativeClock", lambda: native)
    monkeypatch.setattr(guard.sys, "stdin", io.StringIO('{"operation":"shift","seconds":172800}\n'))
    monkeypatch.setattr(guard.sys, "stdout", io.StringIO())
    path = tmp_path / "watchdog.json"
    guard._watchdog(path, 1)
    result = json.loads(path.read_text())
    assert result["reason"] == "watchdog_timeout"
    assert result["clock_restored"] and result["services_restored"]
    assert result["last_restoration"]["residual_seconds"] == 0
