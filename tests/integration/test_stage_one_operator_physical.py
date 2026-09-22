"""Physical Stage-One witness for authenticated enrollment-to-Windows-live operation.

The witness deliberately retains only aggregate counters. Private source/credential
material is consumed in-process, and temporary durable user/device state is removed
from the camera-lab host after the test.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from k5vision.stage_one_app import create_stage_one_app

pytestmark = pytest.mark.skipif(
    os.getenv("K5_STAGE_ONE_OPERATOR_PHYSICAL") != "1",
    reason="Stage One authenticated physical operator qualification is opt-in",
)

_ADMIN_TOKEN = "stage-one-physical-bootstrap-admin"
_SITE_ID = "stage-one-physical-witness"


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _remove_temporary_state(root: Path) -> None:
    for path in root.glob("stage-one-*.sqlite3*"):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def test_authenticated_enrollment_launches_private_source_in_windows_operator(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = os.environ["K5_STAGE03_SOURCE"]
    private_credentials = os.environ["K5_STAGE03_CAM_CRED"]
    output = Path(os.environ["K5_STAGE_ONE_OUTPUT"])
    revision = os.environ["K5_STAGE_ONE_REVISION"].casefold()

    parsed = urlsplit(source)
    host = parsed.hostname
    assert host is not None

    device_db = tmp_path / "stage-one-devices.sqlite3"
    user_db = tmp_path / "stage-one-users.sqlite3"
    monkeypatch.setenv("K5_CONTROL_PLANE_SITE_ID", _SITE_ID)
    monkeypatch.setenv("K5_DEVICE_DB_PATH", str(device_db))
    monkeypatch.setenv("K5_USER_DB_PATH", str(user_db))
    monkeypatch.setenv("K5_CONTROL_PLANE_ADMIN_TOKEN", _ADMIN_TOKEN)
    monkeypatch.setenv("K5_OPERATOR_STREAM_TOKEN", "main")
    monkeypatch.delenv("K5_OPERATOR_RTP_PAYLOAD_TYPE", raising=False)

    username = f"physical-{secrets.token_hex(6)}"
    permanent_password = secrets.token_urlsafe(32)
    temporary_credential = ""
    session_token = ""

    try:
        application = create_stage_one_app()
        with TestClient(application) as client:
            created = client.post(
                "/api/v1/users",
                json={
                    "username": username,
                    "display_name": "Stage One Physical Operator",
                    "role": "operator",
                    "enabled": True,
                },
                headers=_headers(_ADMIN_TOKEN),
            )
            assert created.status_code == 201
            temporary_credential = created.json()["temporary_credential"]

            initialized = client.post(
                "/api/v1/auth/bootstrap-password",
                json={
                    "username": username,
                    "temporary_credential": temporary_credential,
                    "new_password": permanent_password,
                },
            )
            assert initialized.status_code == 204

            logged_in = client.post(
                "/api/v1/auth/login",
                json={"username": username, "password": permanent_password},
            )
            assert logged_in.status_code == 200
            session_token = logged_in.json()["session_token"]

            enrolled = client.post(
                "/api/v1/devices",
                json={
                    "name": "Stage One Physical Camera",
                    "host": host,
                    "kind": "camera",
                    "protocols": ["rtsp"],
                    "tags": ["stage-one-physical-witness"],
                },
                headers=_headers(session_token),
            )
            assert enrolled.status_code in {200, 201}
            device_id = enrolled.json()["id"]

            service_token_attempt = client.post(
                "/api/v1/operator/live",
                json={"device_id": device_id, "stream_token": "main"},
                headers=_headers(_ADMIN_TOKEN),
            )
            assert service_token_attempt.status_code == 401

            launched = client.post(
                "/api/v1/operator/live",
                json={
                    "device_id": device_id,
                    "stream_token": "main",
                    "width": 1280,
                    "height": 720,
                },
                headers=_headers(session_token),
            )
            assert launched.status_code == 200, launched.json().get(
                "detail", "operator launch failed"
            )
            receipt = launched.json()
            assert receipt["completed"] is True
            assert receipt["delivered_frames"] >= 1
            assert receipt["presentations"] >= 1

        evidence = {
            "schema_version": "1",
            "revision": revision,
            "execution_context": "camera-lab-windows-x64",
            "human_session_authenticated": True,
            "device_enrolled": True,
            "service_token_rejected": True,
            "windows_live_launch_completed": True,
            "delivered_frames": receipt["delivered_frames"],
            "presentations": receipt["presentations"],
            "processed_controls": receipt["processed_controls"],
        }
        payload = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
        lowered = payload.casefold()
        for forbidden in (
            source,
            host,
            private_credentials,
            temporary_credential,
            permanent_password,
            session_token,
            _ADMIN_TOKEN,
        ):
            assert forbidden and forbidden not in payload
        for forbidden_marker in (
            "rtsp://",
            "rtsps://",
            "credential",
            "password",
            "session_token",
            "device_id",
            "source_uri",
            "runner_name",
            "payload",
        ):
            assert forbidden_marker not in lowered

        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload, encoding="utf-8")
    finally:
        _remove_temporary_state(tmp_path)
