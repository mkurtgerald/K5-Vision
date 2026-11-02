from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_PATH = Path(__file__).parents[1] / "integration/windows_time_configuration.py"
_SPEC = importlib.util.spec_from_file_location("k5_test_time_configuration", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
guard_module = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(guard_module)


class _Native:
    def __init__(self):
        self.wall = datetime(2026, 10, 31, 12, tzinfo=UTC)
        self.tick = 0
        self.states = {
            name: {
                "name": name,
                "present": True,
                "running": True,
                "start_type": 3,
                "delayed_auto_start": False,
            }
            for name in ("W32Time", "vmictimesync")
        }

    def enable_privilege(self):
        pass

    def utc(self):
        return self.wall

    def ticks_ms(self):
        return self.tick

    def set_utc(self, value):
        self.wall = value

    def service_state(self, name, *, desired=None, start_type=None, delayed_auto_start=None):
        value = self.states[name]
        if desired is not None:
            value["running"] = desired
        if start_type is not None:
            value["start_type"] = start_type
        if delayed_auto_start is not None:
            value["delayed_auto_start"] = delayed_auto_start
        return dict(value)


@pytest.fixture
def environment(monkeypatch, tmp_path):
    native = _Native()
    original = [
        {"name": "Type", "present": True, "value": "NT5DS", "kind": 1},
        {"name": "NtpServer", "present": True, "value": "private.source.example,0x8", "kind": 1},
    ]
    configuration = copy.deepcopy(original)
    monkeypatch.setattr(guard_module, "_require_environment", lambda: None)
    monkeypatch.setattr(guard_module._CLOCK, "_NativeClock", lambda: native)
    monkeypatch.setattr(guard_module, "_configuration", lambda: copy.deepcopy(configuration))

    def restore(values):
        configuration[:] = copy.deepcopy(values)

    monkeypatch.setattr(guard_module, "_restore_configuration", restore)
    profile = tmp_path / "profile"
    profile.mkdir()
    return native, configuration, original, profile


def test_guard_restores_exact_sources_services_and_utc_after_actual_operation(environment):
    native, configuration, original, profile = environment
    before_services = copy.deepcopy(native.states)
    controller = guard_module._Controller(profile, 180)
    controller.arm()
    assert all(
        not state["running"] and state["start_type"] == 4 for state in native.states.values()
    )
    native.tick = 14000
    native.wall += timedelta(hours=48)
    native.states["W32Time"].update(running=True, start_type=2)
    configuration[0]["value"] = "NTP"
    configuration[1]["value"] = "changed-source.example,0x9"
    result = controller.finish("closed")
    assert configuration == original and native.states == before_services
    assert native.utc() == datetime(2026, 10, 31, 12, 0, 14, tzinfo=UTC)
    assert (
        result["clock_restored"]
        and result["services_restored"]
        and result["time_configuration_restored"]
    )
    assert "private.source.example" not in json.dumps(result)
    assert controller.verified()


def test_configuration_restore_failure_still_restores_utc_and_services(environment, monkeypatch):
    native, _configuration, _original, profile = environment
    before_services = copy.deepcopy(native.states)
    controller = guard_module._Controller(profile, 180)
    controller.arm()
    native.wall += timedelta(hours=48)

    def failed(values):
        raise OSError("private native diagnostics")

    monkeypatch.setattr(guard_module, "_restore_configuration", failed)
    result = controller.finish("closed")
    assert result["time_configuration_restored"] is False
    assert result["reason"] == "configuration_restore_failed"
    assert result["clock_restored"] and result["services_restored"]
    assert native.states == before_services
    assert "private native diagnostics" not in json.dumps(result)


def test_restoration_revokes_only_owned_request_capabilities_before_recovery(environment):
    _native, _configuration, _original, profile = environment
    controller = guard_module._Controller(profile, 180)
    controller.arm()
    directory = profile / "time-admin"
    directory.mkdir()
    request = directory / (".time-admin-" + "a" * 32 + ".json")
    request.write_text("capability")
    unrelated = directory / "unknown.keep"
    unrelated.write_text("preserve")
    assert controller.finish("parent_eof")["time_configuration_restored"]
    assert not request.exists() and unrelated.read_text() == "preserve"


@pytest.mark.parametrize("seconds", [0, 181, True, 1.5])
def test_guard_refuses_unbounded_or_implicit_leases(environment, seconds):
    with pytest.raises(guard_module._CLOCK.WindowsVmClockError):
        guard_module._Controller(environment[3], seconds)


def test_stuck_child_is_killed_and_reaped_before_foreground_recovery(
    environment, monkeypatch, tmp_path
):
    events = []
    value = guard_module.WindowsTimeConfigurationGuard(
        environment[3], tmp_path / "restoration.json"
    )
    original_finish = value.fallback.finish

    def finish(reason):
        events.append("foreground_recovery")
        return original_finish(reason)

    value.fallback.finish = finish

    class Input:
        def close(self):
            events.append("eof")

    class Process:
        stdin = Input()
        exited = False

        def poll(self):
            return 0 if self.exited else None

        def wait(self, *, timeout):
            if timeout == 25:
                raise subprocess.TimeoutExpired("owned watchdog", timeout)
            events.append("reaped")
            self.exited = True

        def kill(self):
            events.append("killed")

    value.process = Process()
    value.__exit__(None, None, None)
    assert events == ["eof", "killed", "reaped", "foreground_recovery"]
    assert value.result["time_configuration_restored"] is True
