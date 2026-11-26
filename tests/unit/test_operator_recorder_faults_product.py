"""Recorder alert composition starts without a browser and uses cached evidence."""

from __future__ import annotations

import copy
import time
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
from test_operator_product_service import _product_application
from test_operator_product_service import native as native_fixture

from k5vision.operator_product_service import OperatorProductService

native = native_fixture


def test_recorder_alert_adapter_uses_only_cached_health_policy_and_disk_progress():
    service = OperatorProductService.__new__(OperatorProductService)
    identifier = str(uuid4())
    binding = SimpleNamespace(device_id=identifier, path_name="finite-cached-binding")
    service._sources = [SimpleNamespace(binding=binding, recording_enabled=True)]
    service._media_generation, service._ready = 3, True
    service._health_failures = {"media": 0, "gateway": 0}
    service._storage_status = SimpleNamespace(recording_allowed=True)
    policy = {"mode": "motion", "state": "armed"}
    progress = {
        "observed_bytes": 100,
        "last_progress_age_seconds": 12,
        "inspection_complete": True,
        "observed": True,
    }
    service._motion = SimpleNamespace(status=lambda selected: policy)
    service._storage = SimpleNamespace(recording_progress=lambda path: copy.deepcopy(progress))

    def forbidden(*args, **kwargs):
        raise AssertionError("Alert observation must not request native/media/storage work")

    service.status = service.diagnostics = service._storage_operation = forbidden
    service._media = service._gateway = SimpleNamespace(poll=forbidden)
    service.provider = SimpleNamespace(info=forbidden, path_snapshot=forbidden)
    result = service.cached_recording_observations()
    assert result["worker_ready"] is True and result["storage_allowed"] is True
    assert result["cameras"][0]["recording_expected"] is False
    assert result["cameras"][0]["progress"] == progress
    policy["state"] = "recording"
    assert service.cached_recording_observations()["cameras"][0]["recording_expected"] is True
    service._health_failures["media"] = 1
    assert service.cached_recording_observations()["worker_ready"] is False


def test_real_product_lifespan_detects_and_clears_without_browser_then_closes_monitor(
    native, tmp_path, monkeypatch
):
    import k5vision.operator_recorder_faults as faults

    monkeypatch.setattr(faults, "SAMPLE_SECONDS", 0.01)
    app, calls = _product_application(native, tmp_path, monkeypatch)
    service = app.state.operator_product_service
    observation = {"generation": 1, "worker_ready": True, "storage_allowed": False, "cameras": []}
    service.cached_recording_observations = lambda: copy.deepcopy(observation)
    service.diagnostics = lambda: {"worker_status": "ready", "storage": None}
    monitor = app.state.operator_recorder_faults

    def until(predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.005)
        assert predicate(), "Composed recorder fault monitor did not make progress"

    with TestClient(app) as client:
        # No browser, status request or authenticated user starts this transition.
        until(lambda: monitor.snapshot()["available"] and monitor.snapshot()["active"])
        assert monitor.snapshot()["active"][0]["code"] == "storage_blocked"
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "pilot-admin", "password": "local-test-password"},
        )
        assert login.status_code == 200
        headers = {"Authorization": "Bearer " + login.json()["session_token"]}
        first = client.get("/api/v1/operator/status", headers=headers)
        assert first.status_code == 200
        assert first.json()["recorder_alerts"]["active"][0]["code"] == "storage_blocked"
        observation["storage_allowed"] = True
        until(lambda: monitor.snapshot()["active"] == [])
        recovered = client.get("/api/v1/operator/status", headers=headers)
        assert recovered.status_code == 200
        assert recovered.json()["recorder_alerts"]["history"][-1]["resolution"] == "storage_ready"
    assert monitor._task.done() and monitor._write_task is None
    assert monitor.snapshot()["available"] is False
    assert app.state.operator_audit._connection is None
    assert calls == [("start", ()), ("stop", ())]
