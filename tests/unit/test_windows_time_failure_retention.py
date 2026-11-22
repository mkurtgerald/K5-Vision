"""Failure evidence keeps native facts and rejects private incidental data."""

from __future__ import annotations

import ctypes
import importlib.util
import json
import shutil
import sys
import threading
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.request import ProxyHandler, build_opener

import pytest

_PRIVATE = "private-nonce-request-path-password-response-must-not-appear"
_BIRTH = 134397926963656973


@pytest.fixture
def native_time():
    path = Path(__file__).parents[1] / "integration/test_windows_time_administration.py"
    spec = importlib.util.spec_from_file_location("k5_time_failure_retention_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def endpoint():
    settings = {"status": 503, "body": {"detail": "time_operation_failed", "secret": _PRIVATE}}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_PATCH(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            body = settings["body"]
            raw = body if type(body) is bytes else json.dumps(body).encode()
            self.send_response(settings["status"])
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (
            SimpleNamespace(
                origin=f"http://127.0.0.1:{server.server_port}",
                token=_PRIVATE,
                opener=build_opener(ProxyHandler({})),
            ),
            settings,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def test_actual_http_failure_survives_emitted_property_with_native_facts_and_no_private_extras(
    native_time, endpoint
):
    client, _ = endpoint
    with pytest.raises(native_time.TimeAdministrationError) as raised:
        native_time._time_request(client, "configure")
    error = raised.value
    emitted = []
    host = {
        "status": "available",
        "mode": "manual",
        "service_running": True,
        "ntp_servers": ["time.cloudflare.com"],
        "private_path": _PRIVATE,
    }
    process = {
        "kind": "helper",
        "pid": 42,
        "created_100ns": _BIRTH,
        "identity_bound": True,
        "exit_observed": True,
        "exited": True,
        "exit_code": 0x800705B4,
        "request": _PRIVATE,
    }
    native_time._record_time_failure(
        lambda name, value: emitted.append((name, value)),
        error,
        operations=[
            {
                "operation": "configure",
                "http": error._k5_time_failure_http,
                "ownership": {
                    "helper_count": 1,
                    "helper_observed": True,
                    "native_command_count": 0,
                    "native_command_observed": False,
                    "processes": [process],
                    "nonce": _PRIVATE,
                },
                "windows": host,
                "response": _PRIVATE,
            }
        ],
        initial_host=host,
        current_host=host,
        helper_ownership=[
            {
                "helper_count": 1,
                "native_command_count": 0,
                "helper_observed": True,
                "native_command_observed": False,
                "private": _PRIVATE,
            }
        ],
        guard_result={
            "clock_restored": True,
            "services_restored": True,
            "time_configuration_restored": True,
            "configuration_watchdog": True,
            "reason": "parent_closed",
            "configuration": _PRIVATE,
            "last_restoration": {
                "restored": True,
                "residual_seconds": 0.007,
                "baseline_residual_seconds": 0.007,
                "elapsed_seconds": 12.0,
                "secret": _PRIVATE,
            },
        },
    )
    name, raw = emitted.pop()
    assert not emitted and name == "k5_native_time_failure_diagnostic"
    assert _PRIVATE not in raw
    # Exercise the actual property wire representation, preserving opaque ints.
    prop = ET.Element("property", name=name, value=raw)
    value = json.loads(ET.fromstring(ET.tostring(prop)).get("value"))
    assert value["qualification_passed"] is False
    (operation,) = value["operations"]
    assert operation["http"] == {
        "response_observed": True,
        "status": 503,
        "api_code": "time_operation_failed",
        "detail_kind": "recognized",
    }
    assert operation["snapshot_phase"] == "before_capability_revocation_and_fixture_cleanup"
    (observed,) = operation["ownership"]["processes"]
    assert observed["created_100ns"] == _BIRTH and type(observed["created_100ns"]) is int
    assert observed["exit_code"] == 0x800705B4 and "request" not in observed
    assert value["guard_restoration"]["time_configuration_restored"] is True
    assert value["helper_ownership"][0]["helper_observed"] is True
    assert value["windows_after_guard_before_fixture_cleanup"]["matches_requested_peer"] is True


@pytest.mark.parametrize("detail", [None, _PRIVATE, [_PRIVATE], {"private": _PRIVATE}])
def test_unrecognized_http_detail_is_retained_as_finite_absence(native_time, endpoint, detail):
    client, settings = endpoint
    settings["status"], settings["body"] = 502, {"detail": detail, "private": _PRIVATE}
    with pytest.raises(native_time.TimeAdministrationError) as raised:
        native_time._time_request(client, "configure")
    note = raised.value._k5_time_failure_http
    assert note == {
        "response_observed": True,
        "status": 502,
        "api_code": None,
        "detail_kind": "unrecognized_or_absent",
    }
    assert _PRIVATE not in json.dumps(note)


def test_malformed_http_json_preserves_original_error_and_response_status(native_time, endpoint):
    client, settings = endpoint
    settings["body"] = b"not-json-" + _PRIVATE.encode()
    with pytest.raises(json.JSONDecodeError) as raised:
        native_time._time_request(client, "configure")
    note = raised.value._k5_time_failure_http
    assert note["status"] == 503 and note["api_code"] is None
    assert _PRIVATE not in json.dumps(note)


def test_known_api_validation_code_survives_existing_exception_code_normalization(
    native_time, endpoint
):
    client, settings = endpoint
    settings["status"], settings["body"] = 422, {"detail": "time_invalid_request"}
    with pytest.raises(native_time.TimeAdministrationError) as raised:
        native_time._time_request(client, "configure")
    assert raised.value.code == "time_operation_failed"  # Existing caller behavior.
    assert raised.value._k5_time_failure_http == {
        "response_observed": True,
        "status": 422,
        "api_code": "time_invalid_request",
        "detail_kind": "recognized",
    }


def test_successful_time_request_keeps_its_existing_response_contract(native_time, endpoint):
    client, settings = endpoint
    settings["status"] = 200
    settings["body"] = {"host": {"mode": "manual"}, "unchanged": _PRIVATE}
    assert native_time._time_request(client, "configure") == settings["body"]


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]


class _Call:
    def __init__(self, function, arguments):
        self.function, self.argtypes = function, arguments

    def __call__(self, *args):
        return self.function(*args)


@pytest.mark.parametrize("state", ["exited", "live", "reused", "gone", "query-failed"])
def test_failure_exit_query_binds_exact_birth_and_closes_only_its_query_handle(native_time, state):
    events = []

    def opened(access, inherit, pid):
        assert access == 0x00101000 and not inherit and pid == 42
        events.append("open")
        return None if state == "gone" else 99

    def times(handle, created, *others):
        assert handle == 99
        actual = _BIRTH + int(state == "reused")
        created._obj.low, created._obj.high = actual & 0xFFFFFFFF, actual >> 32
        return state != "query-failed"

    def waited(handle, milliseconds):
        assert handle == 99 and milliseconds == 0
        events.append("wait")
        return 258 if state == "live" else 0

    def exit_code(handle, code):
        assert handle == 99
        events.append("exit")
        code._obj.value = 0x800705B4
        return True

    api = SimpleNamespace(
        OpenProcess=opened,
        WaitForSingleObject=waited,
        GetProcessTimes=_Call(times, [ctypes.c_void_p, *[ctypes.POINTER(_FileTime)] * 4]),
        GetExitCodeProcess=_Call(exit_code, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]),
        CloseHandle=lambda handle: events.append(("close", handle)),
    )
    tracker = native_time._TimeHelperTracker.__new__(native_time._TimeHelperTracker)
    tracker.native, tracker.helpers, tracker.commands = (
        SimpleNamespace(api=api),
        {(42, _BIRTH)},
        set(),
    )
    snapshot = tracker.failure_snapshot("configure")
    (item,) = snapshot["processes"]
    assert item["created_100ns"] == _BIRTH
    assert events.count(("close", 99)) == int(state != "gone")
    if state == "exited":
        assert item["identity_bound"] and item["exit_observed"] and item["exited"]
        assert item["exit_code"] == 0x800705B4
    elif state == "live":
        assert item["identity_bound"] and item["exited"] is False
        assert item["exit_code"] is None and "exit" not in events
    else:
        assert not item["identity_bound"] and item["exit_code"] is None
        assert "wait" not in events and "exit" not in events


def test_malformed_incidental_fields_cannot_leak_or_certify_an_exit(native_time):
    emitted = []
    native_time._record_time_failure(
        lambda name, raw: emitted.append(json.loads(raw)),
        RuntimeError(_PRIVATE),
        operations=[
            {
                "operation": [_PRIVATE],
                "ownership": {
                    "private": _PRIVATE,
                    "processes": [
                        {
                            "kind": "command",
                            "pid": 7,
                            "created_100ns": _BIRTH,
                            "identity_bound": False,
                            "exit_observed": True,
                            "exited": True,
                            "exit_code": 0,
                        }
                    ],
                },
                "http": {"private": _PRIVATE},
            }
        ],
        initial_host={
            "mode": [_PRIVATE],
            "status": {"private": _PRIVATE},
            "ntp_servers": [_PRIVATE],
        },
        guard_result={
            "reason": [_PRIVATE],
            "configuration": _PRIVATE,
            "last_restoration": {"residual_seconds": float("nan")},
        },
    )
    (value,) = emitted
    assert _PRIVATE not in json.dumps(value, allow_nan=False)
    assert value["primary_error"] == {"exception_type": "RuntimeError"}
    (item,) = value["operations"][0]["ownership"]["processes"]
    assert not item["exit_observed"] and item["exit_code"] is None
    assert value["guard_restoration"]["restoration"]["residual_seconds"] is None


def test_normal_journey_emits_failure_property_and_preserves_primary_error_before_cleanup(
    native_time, tmp_path, monkeypatch
):
    """Exercise the actual outer exception/formatter path, failing before install."""
    emitted, cleanup = [], []
    failure = native_time.TimeAdministrationError("time_operation_failed")
    installed, profile = tmp_path / "not-installed", tmp_path / "not-created"

    class Cohort:
        failed = False

        def finish(self):
            cleanup.append("cohort")

    def refuse_install(*args, **kwargs):
        raise failure

    def remove(path):
        cleanup.append("temporary")
        shutil.rmtree(path)

    desktop = SimpleNamespace(
        _machine_paths=lambda: (installed, profile),
        _Windows=lambda: object(),
        _Cohort=lambda api: Cohort(),
        _install_gui=refuse_install,
        _helpers=lambda name: SimpleNamespace(installer_failure_snapshot=lambda *args: {}),
        _remove_owned_temporary_tree=remove,
    )
    # These fixture boundaries avoid a native installer on the unit-test host;
    # the actual journey's exception and property-emission code runs unchanged.
    monkeypatch.setitem(sys.modules, "test_windows_desktop_app", desktop)
    monkeypatch.setitem(
        sys.modules,
        "windows_time_configuration",
        SimpleNamespace(
            WindowsTimeConfigurationGuard=object,
            _configuration=lambda: None,
        ),
    )
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("K5_DESKTOP_SETUP", str(tmp_path / "generated-setup"))
    monkeypatch.setenv("K5_DESKTOP_HEAD_SHA", "a" * 40)
    monkeypatch.setenv("K5_DESKTOP_SETUP_SHA256", "b" * 64)
    monkeypatch.setattr(
        native_time,
        "_candidate",
        lambda *args: (
            tmp_path / "generated-setup",
            {"launcher_sha256": "c" * 64},
            b"{}",
        ),
    )
    with pytest.raises(native_time.TimeAdministrationError) as raised:
        native_time.test_native_windows_time_configuration_readback_and_resync_with_restoration(
            lambda name, value: emitted.append((name, json.loads(value)))
        )
    assert raised.value is failure
    notes = dict(emitted)
    assert "k5_native_time_administration_receipt" not in notes
    assert notes["k5_native_time_failure_diagnostic"]["primary_error"] == {
        "exception_type": "TimeAdministrationError",
        "code": "time_operation_failed",
    }
    assert notes["k5_native_time_failure_diagnostic"]["guard_restoration"] == {"observed": False}
    assert notes["k5_native_time_failure_diagnostic"]["qualification_passed"] is False
    assert notes["k5_native_time_cleanup_diagnostic"]["primary_failed"] is True
    assert cleanup == ["cohort", "temporary"]
