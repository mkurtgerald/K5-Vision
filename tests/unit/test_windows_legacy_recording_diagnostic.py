"""Retain the original recording predicate's finite state without changing its wait."""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import Request

ROOT = Path(__file__).resolve().parents[2]


def _growth_wait():
    source = (ROOT / "tests/integration/test_windows_desktop_upgrade.py").read_text()
    waits = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and len(node.args) > 1
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == "Service recordings grow after all clients close"
    ]
    assert len(waits) == 1
    call = waits[0]
    assert ast.unparse(call.func) == "d._wait"
    assert len(call.args) == 2
    assert [(item.arg, ast.literal_eval(item.value)) for item in call.keywords] == [("timeout", 30)]
    predicate = call.args[0]
    assert isinstance(predicate, ast.Lambda)
    assert ast.unparse(predicate.body.func) == "d._tray_recording"
    assert len(predicate.body.args) == 5 and not predicate.body.keywords
    return predicate


def _desktop():
    path = ROOT / "tests/integration/test_windows_desktop_app.py"
    spec = importlib.util.spec_from_file_location("legacy_growth_real_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("response", ["not_recording", "no_growth", "disconnected", "unavailable"])
def test_original_failed_wait_retains_only_last_finite_camera_and_byte_state(
    tmp_path, monkeypatch, response
):
    desktop = _desktop()
    state = {"elapsed": 0.0}

    def advance(seconds):
        state["elapsed"] += seconds

    monkeypatch.setattr(
        desktop, "time", SimpleNamespace(monotonic=lambda: state["elapsed"], sleep=advance)
    )
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    (recordings / "synthetic-counter.mp4").write_bytes(b"counter fixture")
    camera = {
        "device_id": "generated-device",
        "status": "disconnected" if response == "disconnected" else "live",
        "recording": response != "not_recording",
        "source_uri": "private-url-never-retained",
        "username": "private-username-never-retained",
    }

    def get(_path):
        if response == "unavailable":
            raise RuntimeError("private transport detail never retained")
        return 200, json.dumps({"cameras": [camera]}).encode(), {}

    diagnostic = {"phase": "client_closed_recording"}
    previous = 15 if response == "no_growth" else 0
    namespace = dict(
        d=desktop,
        client=SimpleNamespace(get=get),
        machine_profile=tmp_path,
        camera_id="generated-device",
        previous_bytes=previous,
        diagnostic=diagnostic,
    )
    predicate = eval(
        compile(
            ast.fix_missing_locations(ast.Expression(_growth_wait())),
            "<actual_growth_lambda>",
            "eval",
        ),
        namespace,
    )
    with pytest.raises(pytest.fail.Exception, match="readiness deadline expired"):
        desktop._wait(predicate, "Service recordings grow after all clients close", timeout=0.2)
    assert 0.2 <= state["elapsed"] < 0.3
    expected = {
        "phase": "client_closed_recording",
        "recording_file_count": 1,
        "recording_bytes_before": previous,
        "recording_bytes_after": 15,
        "camera_request_unavailable": response == "unavailable",
        "camera_http_status": 0 if response == "unavailable" else 200,
        "camera_status": (
            "unavailable"
            if response == "unavailable"
            else "disconnected"
            if response == "disconnected"
            else "live"
        ),
        "camera_recording": response not in {"not_recording", "unavailable"},
    }
    if response == "unavailable":
        expected.pop("camera_status")
        expected.pop("camera_recording")
    assert diagnostic == expected
    assert "private-" not in json.dumps(diagnostic)


def _actual_browser_session_step():
    """Execute the actual login statement and any immediate token handoff."""
    source = (ROOT / "tests/integration/test_windows_desktop_upgrade.py").read_text()
    tree = ast.parse(source)
    statements = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Expr, ast.Assign))
        and isinstance(node.value, ast.Call)
        and ast.unparse(node.value.func) == "settings._mfa_login"
    ]
    assert len(statements) == 1
    statement = statements[0]
    following = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and node.lineno == statement.end_lineno + 1
        and len(node.targets) == 1
        and ast.unparse(node.targets[0]) == "client.token"
    ]
    assert len(following) <= 1
    return ast.fix_missing_locations(ast.Module(body=[statement, *following], type_ignores=[]))


def test_actual_browser_token_handoff_keeps_single_session_security_and_growth_observer(
    tmp_path,
):
    """Synthetic completed-login endpoint; actual session issue/resolve and HTTP."""
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    from k5vision.auth_sessions import UserSessionManager
    from k5vision.domain.users import UserAccount

    sessions = UserSessionManager()
    account = UserAccount(
        username="generated-admin", display_name="Generated", role="administrator"
    )
    application = FastAPI()

    @application.post("/generated-completed-login")
    async def issue():
        token, _ = await sessions.issue(account)
        return {"session_token": token}

    @application.get("/api/v1/operator/cameras")
    async def cameras(request: Request):
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if await sessions.resolve(token) is None:
            raise HTTPException(401, "unauthorized")
        return {"cameras": [{"device_id": "generated-device", "status": "live", "recording": True}]}

    desktop = _desktop()
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    (recordings / "synthetic-byte-counter.mp4").write_bytes(b"new recording bytes")
    observed = {}
    with TestClient(application) as http:
        previous = http.post("/generated-completed-login").json()["session_token"]
        client = SimpleNamespace(token=previous, origin="http://testserver")

        def get(path):
            response = http.get(path, headers={"Authorization": "Bearer " + client.token})
            return response.status_code, response.content, {}

        client.get = get
        assert desktop._tray_recording(client, tmp_path, "generated-device", 0, {})

        def browser_completed_login(*_args):
            signed_in = http.post("/generated-completed-login").json()
            assert signed_in["session_token"] != previous
            assert get("/api/v1/operator/cameras")[0] == 401
            assert not desktop._tray_recording(client, tmp_path, "generated-device", 0, observed)
            assert observed["camera_http_status"] == 401
            assert observed["recording_bytes_after"] > observed["recording_bytes_before"]
            return signed_in

        namespace = dict(
            settings=SimpleNamespace(_mfa_login=browser_completed_login),
            page=None,
            client=client,
            username=account.username,
            password="synthetic-password",
            administrator_id=str(account.id),
            recovery=["synthetic-factor"] * 5,
        )
        exec(
            compile(_actual_browser_session_step(), "<actual_browser_token_handoff>", "exec"),
            namespace,
        )
        assert get("/api/v1/operator/cameras")[0] == 200
        assert desktop._tray_recording(client, tmp_path, "generated-device", 0, observed)
        # The old session remains revoked; observer repair does not weaken it.
        assert (
            http.get(
                "/api/v1/operator/cameras", headers={"Authorization": "Bearer " + previous}
            ).status_code
            == 401
        )
