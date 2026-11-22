"""The installed alert experiment emits only the existing public fault contract."""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_operator_product_service import native as native_fixture

native = native_fixture

_PATH = (
    Path(__file__).resolve().parents[1] / "integration/test_windows_recorder_alert_diagnostic.py"
)
_SPEC = importlib.util.spec_from_file_location("installed_alert_component_diagnostic", _PATH)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
_CAMERA = "936d76be-c4ad-4d32-a4d0-2ec256f07c80"


def _public():
    return {
        "visible": True,
        "available": True,
        "monitoring": True,
        "active": [
            {
                "code": "no_new_recording",
                "camera_id": _CAMERA,
                "state": "active",
                "resolution": None,
                "occurred_at": "2026-11-22T07:30:00.000Z",
            }
        ],
        "history": [],
    }


def _client(value):
    return SimpleNamespace(json=lambda _route: {"recorder_alerts": value})


def test_public_active_alert_tuple_can_survive_restart_without_internal_ids():
    value = _public()
    first = _MODULE._camera_fault(_MODULE._alerts(_client(value)), _CAMERA, state="active")
    second = _MODULE._camera_fault(
        _MODULE._alerts(_client(copy.deepcopy(value))), _CAMERA, state="active"
    )
    assert first == second and set(first) == _MODULE._ROW_KEYS
    assert not {"id", "run_id", "elapsed_ms"}.intersection(first)
    first["state"] = "resolved"
    assert value["active"][0]["state"] == "active"


def test_unavailable_monitor_cannot_supply_an_active_or_recovery_witness():
    value = _public()
    value["available"] = False
    value["history"] = [
        {**value["active"][0], "state": "resolved", "resolution": "recording_observed"}
    ]
    observed = _MODULE._alerts(_client(value))
    assert _MODULE._camera_fault(observed, _CAMERA, state="active") is None
    assert _MODULE._camera_fault(observed, _CAMERA, state="resolved") is None


@pytest.mark.parametrize("field", ["id", "run_id", "elapsed_ms", "source_uri", "private_path"])
def test_additional_private_or_internal_event_fields_are_refused(field):
    value = _public()
    value["active"][0][field] = "rtsp://private-user:secret-password@private-camera"
    with pytest.raises(
        ValueError, match="^Finite public recorder alert contract refused$"
    ) as error:
        _MODULE._alerts(_client(value))
    assert "secret-password" not in str(error.value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("code", "secret-password"),
        ("camera_id", "rtsp://private:secret-password@camera"),
        ("state", "secret-password"),
        ("resolution", "secret-password"),
        ("occurred_at", "private-path-secret-password"),
        ("occurred_at", "2026-11-22T07:30:00+00:00"),
        ("occurred_at", "2026-11-22T07:30:00+10:00Z"),
    ],
)
def test_malformed_public_event_fields_emit_no_input_in_failure(field, value):
    public = _public()
    public["active"][0][field] = value
    with pytest.raises(
        ValueError, match="^Finite public recorder alert contract refused$"
    ) as error:
        _MODULE._alerts(_client(public))
    assert "secret-password" not in str(error.value)


def test_fault_for_other_camera_or_other_state_is_not_a_recovery():
    value = _public()
    value["active"][0]["camera_id"] = "5479dfbd-e4e4-4b96-87e8-294077e96a56"
    assert _MODULE._camera_fault(value, _CAMERA, state="active") is None
    assert _MODULE._camera_fault(value, _CAMERA, state="resolved") is None


@pytest.mark.parametrize("name,limit", [("active", 66), ("history", 20)])
def test_bounded_public_event_lists(name, limit):
    value = _public()
    value[name] = [dict(value["active"][0]) for _ in range(limit + 1)]
    with pytest.raises(ValueError, match="Finite public recorder alert contract refused"):
        _MODULE._alerts(_client(value))


def test_actual_composed_authenticated_status_is_consumed_without_invented_envelope(
    native, tmp_path, monkeypatch
):
    from fastapi.testclient import TestClient
    from test_operator_product_service import _product_application

    # The existing composition fixture supplies finite process doubles, while
    # identity/authentication, route serialization and alert monitor are real.
    app, _calls = _product_application(native, tmp_path, monkeypatch)
    app.state.operator_product_service.diagnostics = lambda: {
        "worker_status": "ready",
        "storage": None,
    }
    with TestClient(app) as http:
        login = http.post(
            "/api/v1/auth/login",
            json={"username": "pilot-admin", "password": "local-test-password"},
        )
        assert login.status_code == 200
        headers = {"Authorization": "Bearer " + login.json()["session_token"]}
        response = http.get("/api/v1/operator/status", headers=headers)
        assert response.status_code == 200
        payload = response.json()
        assert "recorder_alerts" in payload and "diagnostics" not in payload
        client = SimpleNamespace(json=lambda route: http.get(route, headers=headers).json())
        assert _MODULE._alerts(client) == payload["recorder_alerts"]


def test_nonmonitoring_state_cannot_supply_active_or_recovery_witness():
    value = _public()
    value["monitoring"] = False
    value["history"] = [
        {**value["active"][0], "state": "resolved", "resolution": "recording_observed"}
    ]
    observed = _MODULE._alerts(_client(value))
    assert _MODULE._camera_fault(observed, _CAMERA, state="active") is None
    assert _MODULE._camera_fault(observed, _CAMERA, state="resolved") is None


@pytest.mark.parametrize("kind", ["publisher_timeout", "browser_error", "cleanup_error"])
def test_final_failure_drops_secret_bearing_exception_and_context(kind):
    import subprocess

    secret = "rtsp://private-user:secret-password@private-camera"
    error = (
        subprocess.TimeoutExpired(["ffmpeg.exe", secret], 5)
        if kind == "publisher_timeout"
        else RuntimeError("Private browser fill or cleanup failed " + secret)
    )
    with pytest.raises(pytest.fail.Exception) as failed:
        _MODULE._finite_failure(error, "actual_publisher_restart_and_new_recording")
    assert "secret-password" not in str(failed.value)
    assert "private-camera" not in str(failed.value)
    assert failed.value.pytrace is False
    assert failed.value.__cause__ is None and failed.value.__suppress_context__ is True
