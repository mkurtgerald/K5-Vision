"""The native browser status gate admits the actual composed alert contract.

Execute the real inline acceptance predicates so a new status field cannot
silently strand native qualification before its first camera enrollment.
"""

from __future__ import annotations

import ast
import copy
import os
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_operator_product_service import _product_application
from test_operator_product_service import native as native_fixture

native = native_fixture
SOURCE = Path(__file__).resolve().parents[1] / "integration/test_operator_beta_browser.py"
MESSAGES = {
    "Operator service and storage status was incomplete",
    "Recorder alert monitoring was not ready on the fresh site",
}


def _admit(diagnostics):
    # The old six-field gate was inline. Keep extracting the actual predicates
    # so the same permanent regression also reproduces its original failure.
    calls = [
        node
        for node in ast.walk(ast.parse(SOURCE.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_require"
        and len(node.args) == 2
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value in MESSAGES
    ]
    assert calls

    def require(condition, message):
        if not condition:
            pytest.fail(message)

    scope = {
        "diagnostics": diagnostics,
        "alerts": diagnostics.get("recorder_alerts"),
        "_require": require,
    }
    for call in sorted(calls, key=lambda node: node.lineno):
        code = ast.fix_missing_locations(ast.Module(body=[ast.Expr(value=call)], type_ignores=[]))
        exec(compile(code, str(SOURCE), "exec"), scope)


def _healthy():
    return {
        "worker_status": "ready",
        "max_recording_bytes": 20 * 1024**3,
        "storage": {"recording_allowed": True},
        "analytics_status": "disabled",
        "server_utc": "2026-11-22T07:00:00.000Z",
        "clock": {"status": "observing"},
        "recorder_alerts": {
            "visible": True,
            "available": True,
            "monitoring": True,
            "active": [],
            "history": [],
        },
    }


def test_native_browser_gate_admits_real_composed_status_with_monitored_alerts(
    native, tmp_path, monkeypatch
):
    app, _ = _product_application(native, tmp_path, monkeypatch)
    service = app.state.operator_product_service
    core = _healthy()
    del core["recorder_alerts"], core["analytics_status"]
    service.diagnostics = lambda: copy.deepcopy(core)
    service.cached_recording_observations = lambda: {
        "generation": 1,
        "worker_ready": True,
        "storage_allowed": True,
        "cameras": [],
    }
    with TestClient(app) as client:
        deadline = time.monotonic() + 3
        while (
            not app.state.operator_recorder_faults.snapshot()["available"]
            and time.monotonic() < deadline
        ):
            time.sleep(0.005)
        assert app.state.operator_recorder_faults.snapshot()["available"]
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "pilot-admin", "password": "local-test-password"},
        )
        assert login.status_code == 200
        response = client.get(
            "/api/v1/operator/status",
            headers={"Authorization": "Bearer " + login.json()["session_token"]},
        )
        assert response.status_code == 200
        _admit(response.json())


def test_native_browser_gate_requires_new_alerts_instead_of_accepting_old_six_fields():
    value = _healthy()
    del value["recorder_alerts"]
    with pytest.raises(pytest.fail.Exception, match="status was incomplete"):
        _admit(value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("worker_status", "unavailable"),
        ("worker_status", "disk_full"),
        ("analytics_status", "configured"),
        ("max_recording_bytes", 21 * 1024**3),
        ("storage", {"recording_allowed": False}),
        ("storage", {"recording_allowed": 1}),
    ],
)
def test_existing_service_storage_and_recording_requirements_remain_strict(field, value):
    diagnostics = _healthy()
    diagnostics[field] = value
    with pytest.raises(pytest.fail.Exception, match="status was incomplete"):
        _admit(diagnostics)


@pytest.mark.parametrize(
    "field,value",
    [
        ("visible", False),
        ("available", False),
        ("monitoring", False),
        ("visible", 1),
        ("available", 1),
        ("monitoring", 1),
        ("active", [{"code": "storage_blocked"}]),
        ("history", [{"code": "storage_blocked", "state": "resolved"}]),
        ("active", None),
        ("history", ()),
    ],
)
def test_fresh_native_site_requires_available_monitoring_without_recorded_faults(field, value):
    diagnostics = _healthy()
    diagnostics["recorder_alerts"][field] = value
    with pytest.raises(pytest.fail.Exception, match="Recorder alert monitoring was not ready"):
        _admit(diagnostics)


@pytest.mark.parametrize("boundary", ["root_extra", "alert_extra", "alert_missing", "alert_none"])
def test_status_and_public_alert_schemas_refuse_unknown_or_missing_fields(boundary):
    diagnostics = _healthy()
    if boundary == "root_extra":
        diagnostics["unknown"] = "private-test-value"
    elif boundary == "alert_extra":
        diagnostics["recorder_alerts"]["sequence"] = 63
    elif boundary == "alert_missing":
        del diagnostics["recorder_alerts"]["monitoring"]
    else:
        diagnostics["recorder_alerts"] = None
    with pytest.raises(pytest.fail.Exception) as error:
        _admit(diagnostics)
    assert "private-test-value" not in str(error.value)


@pytest.mark.skipif(
    os.getenv("K5_OPERATOR_BROWSER_ACCEPTANCE") != "1",
    reason="Explicit browser acceptance required",
)
def test_existing_native_readiness_wait_observes_monitor_initialization():
    browser_api = pytest.importorskip("playwright.sync_api")
    scripts = [
        node.args[0].value
        for node in ast.walk(ast.parse(SOURCE.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "wait_for_function"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
        and "getElementById('service-status')" in node.args[0].value
        and "getElementById('storage-status')" in node.args[0].value
    ]
    assert len(scripts) == 1
    with browser_api.sync_playwright() as playwright:
        options = {"headless": True}
        if os.name != "nt" and shutil.which("chromium"):
            options["executable_path"] = shutil.which("chromium")
        else:
            options["channel"] = os.getenv("K5_OPERATOR_BROWSER_CHANNEL", "msedge")
        browser = playwright.chromium.launch(**options)
        try:
            page = browser.new_page()
            page.set_content(
                '<span id="service-status">Media service ready</span>'
                '<span id="storage-status">20 GiB recording limit · 2 GiB disk free</span>'
                '<section id="recorder-alerts">Monitoring unavailable</section>'
            )
            assert page.evaluate(scripts[0]) is False
            page.locator("#recorder-alerts").evaluate("element => element.hidden = true")
            assert page.evaluate(scripts[0]) is True
            page.locator("#service-status").evaluate(
                "element => element.textContent = 'Media service restarting'"
            )
            assert page.evaluate(scripts[0]) is False
            page.locator("#service-status").evaluate(
                "element => element.textContent = 'Media service ready'"
            )
            page.locator("#storage-status").evaluate(
                "element => element.textContent = 'Waiting for storage status'"
            )
            assert page.evaluate(scripts[0]) is False
            page.locator("#storage-status").evaluate(
                "element => element.textContent = '20 GiB recording limit · 2 GiB disk free'"
            )
            page.locator("#recorder-alerts").evaluate("element => element.remove()")
            assert page.evaluate(scripts[0]) is False
        finally:
            browser.close()
