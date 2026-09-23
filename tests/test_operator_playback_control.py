from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from k5vision.domain.devices import Device, DeviceCreate, DeviceKind, DeviceProtocol
from k5vision.domain.users import UserAccount, UserRole
from k5vision.main import create_app
from k5vision.media.framed_recording import FramedAtomicRecordingSink
from k5vision.media.playback_control import PlaybackControlState, PlaybackPauseControl
from k5vision.media.playback_pump import BoundedPlaybackPump
from k5vision.media.recording_descriptor import RecordingStreamDescriptor, VideoCodec
from k5vision.operator_launch import ResolvedLiveSource
from k5vision.operator_playback import (
    BoundedOperatorPlaybackCoordinator,
    OperatorPlaybackControlAction,
    OperatorPlaybackError,
    OperatorPlaybackErrorCode,
    OperatorPlaybackMetrics,
    OperatorPlaybackRequest,
)
from k5vision.services.device_registry import DeviceRegistry
from k5vision.user_admin_api import USER_ADMIN_TOKEN_ENV, USER_DB_PATH_ENV

_ADMIN_TOKEN = "synthetic-admin-service-token"
_PASSWORD = "synthetic-password-12345"
_SITE_ID = "synthetic-playback-control-site"


class _FakeClock:
    def __init__(self, now: float = 10.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.now += delay


def _packet(sequence: int = 1, timestamp: int = 0, payload: bytes = b"x") -> memoryview:
    raw = bytearray(12)
    raw[0] = 0x80
    raw[1] = 96
    raw[2:4] = sequence.to_bytes(2, "big")
    raw[4:8] = timestamp.to_bytes(4, "big")
    raw[8:12] = (1).to_bytes(4, "big")
    return memoryview(bytes(raw) + payload)


async def _write_recording_pair(root: Path, recording_id: UUID, source_id: UUID) -> None:
    sink = FramedAtomicRecordingSink(
        root,
        str(recording_id),
        max_packets=4,
        max_payload_bytes=4096,
    )
    await sink.open()
    await sink.write(_packet())
    await sink.finalize()
    counters = sink.snapshot
    started = datetime(2026, 1, 1, tzinfo=UTC)
    descriptor = RecordingStreamDescriptor(
        recording_id=recording_id,
        source_id=source_id,
        codec=VideoCodec.H264,
        payload_type=96,
        clock_rate_hz=90_000,
        started_at_utc=started,
        ended_at_utc=started + timedelta(milliseconds=100),
        duration_ms=100,
        rtp_timestamp_origin=0,
        packet_count=counters.packets,
        payload_bytes=counters.payload_bytes,
        file_bytes=counters.file_bytes,
    )
    (root / f"{recording_id}.k5d").write_bytes(descriptor.to_json_bytes())


def test_pause_control_freezes_virtual_time_and_reports_source_free_state() -> None:
    async def exercise() -> None:
        clock = _FakeClock()
        control = PlaybackPauseControl(clock=clock.monotonic, sleep=clock.sleep)

        assert control.monotonic() == pytest.approx(10.0)
        paused = await control.pause()
        assert paused.state is PlaybackControlState.PAUSED
        clock.now += 5.0
        assert control.monotonic() == pytest.approx(10.0)

        resumed = await control.resume()
        assert resumed.state is PlaybackControlState.RUNNING
        assert resumed.pause_count == 1
        assert resumed.resume_count == 1
        assert resumed.paused_total_ms == 5000
        clock.now += 1.0
        assert control.monotonic() == pytest.approx(11.0)

        retained = resumed.model_dump_json().casefold()
        assert "source" not in retained
        assert "recording" not in retained
        assert "credential" not in retained

    asyncio.run(exercise())


def test_bound_pump_does_not_deliver_while_paused(tmp_path: Path) -> None:
    async def exercise() -> None:
        recording_id = uuid4()
        source_id = uuid4()
        root = tmp_path / "recordings"
        await _write_recording_pair(root, recording_id, source_id)
        descriptor = RecordingStreamDescriptor.model_validate_json(
            (root / f"{recording_id}.k5d").read_text(encoding="utf-8")
        )
        control = PlaybackPauseControl()
        pump = BoundedPlaybackPump(
            root / f"{recording_id}.k5r",
            descriptor,
            0,
            0,
        )
        pump.bind_pause_control(control)
        delivered: list[int] = []

        async def consumer(_packet: memoryview, source_elapsed_ms: int) -> None:
            delivered.append(source_elapsed_ms)

        await control.pause()
        task = asyncio.create_task(pump.run(consumer))
        await asyncio.sleep(0)
        assert delivered == []
        assert task.done() is False

        await control.resume()
        result = await task
        assert delivered == [0]
        assert result.delivered_packets == 1
        assert result.descriptor_verified is True

    asyncio.run(exercise())


class _Resolver:
    async def resolve(self, device: Device, stream_token: str) -> ResolvedLiveSource:
        assert stream_token == "main"
        return ResolvedLiveSource(f"rtsp://synthetic@{device.host}/live", 96)


class _BlockingLauncher:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        self.control: PlaybackPauseControl | None = None

    async def run(
        self,
        source: ResolvedLiveSource,
        recording_path: Path,
        descriptor: RecordingStreamDescriptor,
        *,
        start_ms: int,
        end_ms: int,
        rate,
        width: int,
        height: int,
        pause_control: PlaybackPauseControl | None = None,
    ) -> OperatorPlaybackMetrics:
        assert source.payload_type == descriptor.payload_type == 96
        assert recording_path.is_file()
        assert start_ms == 0
        assert end_ms == 100
        assert width == 1280
        assert height == 720
        self.control = pause_control
        self.started.set()
        await self.finish.wait()
        return OperatorPlaybackMetrics(
            delivered_frames=1,
            presentations=1,
            descriptor_verified=True,
        )


def _registry(tmp_path: Path) -> tuple[DeviceRegistry, Device]:
    registry = DeviceRegistry(
        database_path=tmp_path / "devices.sqlite3",
        site_id="playback-control-test",
    )
    device = registry.enroll(
        DeviceCreate(
            name="Synthetic Camera",
            host="192.0.2.40",
            kind=DeviceKind.CAMERA,
            protocols={DeviceProtocol.RTSP},
        )
    ).device
    return registry, device


def _principal(username: str) -> UserAccount:
    return UserAccount(
        username=username,
        display_name=username,
        role=UserRole.VIEWER,
        enabled=True,
    )


def test_active_control_is_bounded_to_owning_principal_and_removed_on_completion(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        registry, device = _registry(tmp_path)
        root = tmp_path / "recordings"
        recording_id = uuid4()
        await _write_recording_pair(root, recording_id, device.id)
        launcher = _BlockingLauncher()
        coordinator = BoundedOperatorPlaybackCoordinator(
            registry,
            _Resolver(),
            root,
            launcher,
        )
        owner = _principal("owner-viewer")
        other = _principal("other-viewer")
        control_id = uuid4()
        task = asyncio.create_task(
            coordinator.play(
                owner,
                OperatorPlaybackRequest(
                    recording_id=recording_id,
                    control_id=control_id,
                ),
            )
        )
        try:
            await launcher.started.wait()
            assert launcher.control is not None
            paused = await coordinator.control(
                owner,
                control_id,
                OperatorPlaybackControlAction.PAUSE,
            )
            assert paused.state is PlaybackControlState.PAUSED
            assert paused.pause_count == 1

            repeated = await coordinator.control(
                owner,
                control_id,
                OperatorPlaybackControlAction.PAUSE,
            )
            assert repeated.pause_count == 1

            with pytest.raises(OperatorPlaybackError) as forbidden:
                await coordinator.control(
                    other,
                    control_id,
                    OperatorPlaybackControlAction.RESUME,
                )
            assert forbidden.value.code is OperatorPlaybackErrorCode.CONTROL_FORBIDDEN
            assert launcher.control.snapshot.state is PlaybackControlState.PAUSED

            resumed = await coordinator.control(
                owner,
                control_id,
                OperatorPlaybackControlAction.RESUME,
            )
            assert resumed.state is PlaybackControlState.RUNNING
            assert resumed.resume_count == 1

            launcher.finish.set()
            receipt = await task
            assert receipt.completed is True
            assert coordinator.active_playbacks == 0

            with pytest.raises(OperatorPlaybackError) as missing:
                await coordinator.control(
                    owner,
                    control_id,
                    OperatorPlaybackControlAction.PAUSE,
                )
            assert missing.value.code is OperatorPlaybackErrorCode.CONTROL_NOT_FOUND
        finally:
            if not task.done():
                launcher.finish.set()
                await task
            registry.close()

    asyncio.run(exercise())


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


def test_playback_control_route_requires_human_session_and_rejects_unknown_control(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_user_state(monkeypatch, tmp_path)
    application = create_app(
        control_plane_site_id=_SITE_ID,
        device_db_path=tmp_path / "api-devices.sqlite3",
        operator_source_resolver=_Resolver(),
        operator_recording_root=tmp_path / "recordings",
    )
    control_id = uuid4()

    with TestClient(application) as client:
        unauthorized = client.post(
            f"/api/v1/operator/playback-controls/{control_id}/pause"
        )
        viewer = _issue_session(client, "control-viewer")
        missing = client.post(
            f"/api/v1/operator/playback-controls/{control_id}/pause",
            headers=_headers(viewer),
        )

    assert unauthorized.status_code == 401
    assert unauthorized.headers["www-authenticate"] == "Bearer"
    assert missing.status_code == 404
    assert "rtsp://" not in missing.text.casefold()
    assert str(tmp_path).casefold() not in missing.text.casefold()
