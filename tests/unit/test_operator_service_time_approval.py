"""Session 0 time changes require a bounded, cancellable GUI approval."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from test_operator_time_admin import _native_administrator
from test_operator_time_admin import private_profile as private_profile  # noqa: F401

from k5vision import operator_native_process, operator_time_admin, service_setup
from k5vision.operator_time_admin import TimeAdministrationError


@pytest.fixture
def recorder(private_profile, monkeypatch):
    value, _, configuration = _native_administrator(private_profile)
    value._invoke = value._owned_elevation
    monkeypatch.setattr(operator_time_admin, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(service_setup, "is_recorder_account", lambda: True)

    def forbidden(*args, **kwargs):
        pytest.fail("The service must not launch a UAC broker or process")

    monkeypatch.setattr(operator_time_admin.subprocess, "Popen", forbidden)
    monkeypatch.setattr(operator_native_process, "_WindowsJob", forbidden)
    return value, configuration


def _pending_operation(value, monkeypatch, pool):
    entered = Event()
    paths = []
    original = value._wait_service_approval

    def waiting(request, response):
        paths.extend((request, response))
        entered.set()
        original(request, response)

    monkeypatch.setattr(value, "_wait_service_approval", waiting)
    operation = pool.submit(value.operate, "configure", ntp_servers=["new.example"])
    assert entered.wait(2), "Service did not issue the protected approval request"
    assert not operation.done()
    return operation, paths[0], paths[1]


def _respond(request, response, *, nonce=None, succeeded=True, code="completed"):
    payload = json.loads(request.read_bytes())
    operator_time_admin._private_configuration(
        response,
        json.dumps(
            {
                "schema_version": 1,
                "nonce": payload["nonce"] if nonce is None else nonce,
                "succeeded": succeeded,
                "code": code,
            }
        ),
    )
    return payload


def test_service_waits_for_gui_result_and_retains_native_readback(recorder, monkeypatch):
    value, configuration = recorder
    with ThreadPoolExecutor(max_workers=1) as pool:
        operation, request, response = _pending_operation(value, monkeypatch, pool)
        configuration["ntp_servers"] = ["new.example"]
        payload = _respond(request, response)
        result = operation.result(timeout=2)
    assert payload["owner_created_100ns"] == 134065190123456789
    assert result["operation"] == {"action": "configure", "status": "verified", "verified": True}
    assert result["capabilities"]["requires_interactive_approval"] is True
    assert not value._pending
    assert not list((value.profile / "time-admin").iterdir())


def test_service_waits_for_native_result_writer_to_close(recorder, monkeypatch):
    value, configuration = recorder
    opened = []
    original_open = Path.open
    with ThreadPoolExecutor(max_workers=1) as pool:
        operation, request, response = _pending_operation(value, monkeypatch, pool)

        def native_result_open(path, *args, **kwargs):
            if path == response and (args == ("rb",) or kwargs.get("mode") == "rb"):
                opened.append(path)
                if len(opened) == 1:
                    error = PermissionError("Native result writer is still open")
                    error.winerror = 32
                    raise error
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", native_result_open)
        configuration["ntp_servers"] = ["new.example"]
        _respond(request, response)
        result = operation.result(timeout=2)
    assert len(opened) >= 3  # blocked probe, completed probe, strict result read
    assert result["operation"]["verified"] is True


@pytest.mark.parametrize("action", ["cancel", "close", "request_removed"])
def test_service_approval_stops_after_cancel_close_or_token_removal(recorder, monkeypatch, action):
    value, _ = recorder
    with ThreadPoolExecutor(max_workers=1) as pool:
        operation, request, response = _pending_operation(value, monkeypatch, pool)
        if action == "close":
            value.close()
        elif action == "cancel":
            value.cancel_current()
        else:
            request.unlink()
        expected = "time_unavailable" if action == "close" else "time_elevation_cancelled"
        with pytest.raises(TimeAdministrationError, match="^" + expected + "$"):
            operation.result(timeout=2)
    assert not request.exists() and not response.exists() and not value._pending


@pytest.mark.parametrize(
    "changes,apply,code",
    [
        ({"nonce": "0" * 32}, True, "time_operation_failed"),
        ({}, False, "time_operation_failed"),
        ({"succeeded": False, "code": "ntp_unreachable"}, True, "time_ntp_unreachable"),
    ],
)
def test_service_gui_approval_keeps_nonce_error_and_actual_configuration_checks(
    recorder, monkeypatch, changes, apply, code
):
    value, configuration = recorder
    with ThreadPoolExecutor(max_workers=1) as pool:
        operation, request, response = _pending_operation(value, monkeypatch, pool)
        if apply:
            configuration["ntp_servers"] = ["new.example"]
        _respond(request, response, **changes)
        with pytest.raises(TimeAdministrationError, match="^" + code + "$"):
            operation.result(timeout=2)
    assert not list((value.profile / "time-admin").iterdir())


def test_service_approval_expires_on_monotonic_deadline_without_wall_clock(recorder, monkeypatch):
    value, _ = recorder
    elapsed = iter((100.0, 165.0))
    monkeypatch.setattr(operator_time_admin.time, "monotonic", lambda: next(elapsed))

    def wall_clock():
        pytest.fail("A clock change must not extend the approval lifetime")

    monkeypatch.setattr(operator_time_admin.time, "time", wall_clock)
    with pytest.raises(TimeAdministrationError, match="^time_operation_timeout$"):
        value.operate("resync")
    assert not value._pending and not list((value.profile / "time-admin").iterdir())


def test_service_does_not_accept_result_observed_after_approval_deadline(recorder, monkeypatch):
    value, _ = recorder
    root = value.profile / "time-admin"
    root.mkdir(mode=0o700)
    request, response = root / "request", root / "result"
    operator_time_admin._private_configuration(request, "request")
    operator_time_admin._private_configuration(response, "result")
    value._pending.add(request)
    elapsed = iter((100.0, 101.0, 165.0))
    monkeypatch.setattr(operator_time_admin.time, "monotonic", lambda: next(elapsed))
    with pytest.raises(TimeAdministrationError, match="^time_operation_timeout$"):
        value._wait_service_approval(request, response)


def test_service_status_requires_interactive_approval_without_claiming_administrator(
    recorder, monkeypatch
):
    value, _ = recorder
    assert value.status(reference=False)["capabilities"] == {
        "configure_ntp": True,
        "resynchronize": True,
        "requires_interactive_approval": True,
    }
    monkeypatch.setattr(service_setup, "is_recorder_account", lambda: False)
    assert value.status(reference=False)["capabilities"] == {
        "configure_ntp": True,
        "resynchronize": True,
    }
    value.executable.unlink()
    assert value.status(reference=False)["capabilities"] == {
        "configure_ntp": False,
        "resynchronize": False,
    }


def test_signed_in_user_retains_owned_uac_broker(private_profile, monkeypatch):
    value, _, _ = _native_administrator(private_profile)
    events = []
    monkeypatch.setattr(operator_time_admin, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(service_setup, "is_recorder_account", lambda: False)

    class Job:
        def assign(self, process):
            events.append("assigned")

        def close(self):
            events.append("closed")

    class Process:
        returncode = 0

        def communicate(self, raw, *, timeout):
            assert timeout == 65
            assert json.loads(raw)["executable"] == str(value.executable)
            assert events == ["assigned"]
            events.append("requested")
            return b'{"code":"completed"}', b""

        def poll(self):
            return 0

        def wait(self, *, timeout):
            assert timeout == 3
            events.append("reaped")

    monkeypatch.setattr(operator_native_process, "_WindowsJob", Job)
    monkeypatch.setattr(operator_time_admin.subprocess, "Popen", lambda *args, **kwargs: Process())
    value._owned_elevation(value.executable, value.profile / "request", value.profile / "response")
    assert events == ["assigned", "requested", "reaped", "closed"]
    assert value._broker is None
