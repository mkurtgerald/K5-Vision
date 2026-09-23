from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from k5vision.main import create_app
from k5vision.media.playback_pump import PlaybackPumpSnapshot, PlaybackPumpState
from k5vision.media.playback_schedule import PlaybackRate
from k5vision.media.presentation_playback import PresentationPlaybackState
from k5vision.operator_playback_control import (
    BoundedControllablePresentationPlaybackDelivery,
    ControlledWindowsMixedOperatorPlaybackLauncher,
    OperatorPlaybackControlError,
    OperatorPlaybackControlErrorCode,
    OperatorPlaybackControlState,
)
from k5vision.user_admin_api import USER_ADMIN_TOKEN_ENV, USER_DB_PATH_ENV

_ADMIN_TOKEN = "synthetic-control-admin-token"
_PASSWORD = "synthetic-control-password-12345"
_SITE_ID = "synthetic-playback-control-site"


class _FakePump:
    def __init__(self) -> None:
        self.state = PlaybackPumpState.RUNNING

    def _snapshot(self) -> PlaybackPumpSnapshot:
        return PlaybackPumpSnapshot(
            state=self.state,
            rate=PlaybackRate.NORMAL,
            delivered_packets=1,
            delivered_bytes=12,
            late_packets=0,
            source_span_ms=0,
            scheduled_span_ms=0,
            descriptor_verified=True,
        )

    async def pause(self) -> PlaybackPumpSnapshot:
        if self.state is not PlaybackPumpState.RUNNING:
            raise AssertionError("unexpected pause state")
        self.state = PlaybackPumpState.PAUSED
        return self._snapshot()

    async def resume(self) -> PlaybackPumpSnapshot:
        if self.state is not PlaybackPumpState.PAUSED:
            raise AssertionError("unexpected resume state")
        self.state = PlaybackPumpState.RUNNING
        return self._snapshot()


class _ControlledDeliveryHarness:
    pause = BoundedControllablePresentationPlaybackDelivery.pause
    resume = BoundedControllablePresentationPlaybackDelivery.resume

    def __init__(self) -> None:
        self._pump = _FakePump()
        self._state = PresentationPlaybackState.RUNNING

    @property
    def snapshot(self):
        class _Snapshot:
            state = self._state

        return _Snapshot()


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _configure_user_state(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("K5_CONTROL_PLANE_TOKEN", raising=False)
    monkeypatch.delenv("K5_CONTROL_PLANE_READ_TOKEN", raising=False)
    monkeypatch.setenv(USER_ADMIN_TOKEN_ENV, _ADMIN_TOKEN)
    monkeypatch.setenv(USER_DB_PATH_ENV, str(tmp_path / "users.sqlite3"))


def _issue_session(client: TestClient, username: str) -> str:
    created = client.post(
        "/api/v1/users",
        json={
            "username": username,
            "display_name": username,
            "role": "viewer",
            "enabled": True,
        },
        headers=_headers(_ADMIN_TOKEN),
    )
    assert created.status_code == 201
    changed = client.post(
        "/api/v1/auth/bootstrap-password",
        json={
            "username": username,
            "temporary_credential": created.json()["temporary_credential"],
            "new_password": _PASSWORD,
        },
    )
    assert changed.status_code == 204
    login = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": _PASSWORD},
    )
    assert login.status_code == 200
    return login.json()["session_token"]


def test_controllable_delivery_maps_pause_and_resume_to_existing_pump() -> None:
    async def exercise() -> None:
        delivery = _ControlledDeliveryHarness()
        paused = await delivery.pause()
        assert paused is OperatorPlaybackControlState.PAUSED
        assert delivery._pump.state is PlaybackPumpState.PAUSED

        resumed = await delivery.resume()
        assert resumed is OperatorPlaybackControlState.RUNNING
        assert delivery._pump.state is PlaybackPumpState.RUNNING

    asyncio.run(exercise())


def test_control_launcher_binds_controls_to_principal_and_recording() -> None:
    async def exercise() -> None:
        launcher = ControlledWindowsMixedOperatorPlaybackLauncher()
        principal_id = uuid4()
        recording_id = uuid4()
        other_principal = uuid4()
        harness = _ControlledDeliveryHarness()
        launcher._active_controls[(principal_id, recording_id)] = harness

        paused = await launcher.pause(principal_id, recording_id)
        assert paused.recording_id == recording_id
        assert paused.state is OperatorPlaybackControlState.PAUSED

        with pytest.raises(OperatorPlaybackControlError) as wrong_principal:
            await launcher.resume(other_principal, recording_id)
        assert wrong_principal.value.code is OperatorPlaybackControlErrorCode.NOT_ACTIVE

        resumed = await launcher.resume(principal_id, recording_id)
        assert resumed.state is OperatorPlaybackControlState.RUNNING
        assert "source" not in resumed.model_dump_json().casefold()
        assert "path" not in resumed.model_dump_json().casefold()

    asyncio.run(exercise())


def test_pause_resume_routes_require_session_and_fail_closed_when_inactive(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_user_state(monkeypatch, tmp_path)
    application = create_app(
        control_plane_site_id=_SITE_ID,
        device_db_path=tmp_path / "devices.sqlite3",
        operator_source_resolver=lambda *_args: None,
        operator_recording_root=tmp_path / "recordings",
    )
    recording_id = UUID("56565656-5656-4656-8656-565656565656")

    with TestClient(application) as client:
        unauthorized = client.post(
            f"/api/v1/operator/recordings/{recording_id}/playback/pause"
        )
        token = _issue_session(client, "control-viewer")
        inactive_pause = client.post(
            f"/api/v1/operator/recordings/{recording_id}/playback/pause",
            headers=_headers(token),
        )
        inactive_resume = client.post(
            f"/api/v1/operator/recordings/{recording_id}/playback/resume",
            headers=_headers(token),
        )

    assert unauthorized.status_code == 401
    assert unauthorized.headers["www-authenticate"] == "Bearer"
    assert inactive_pause.status_code == 409
    assert inactive_resume.status_code == 409
    assert "not active" in inactive_pause.json()["detail"]
