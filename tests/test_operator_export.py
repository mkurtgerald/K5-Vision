from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from k5vision.domain.devices import DeviceCreate, DeviceKind, DeviceProtocol
from k5vision.domain.users import UserAccount, UserRole
from k5vision.main import create_app
from k5vision.media.framed_recording import FramedAtomicRecordingSink
from k5vision.media.recording_descriptor import RecordingStreamDescriptor, VideoCodec
from k5vision.operator_export import (
    BoundedOperatorExportCoordinator,
    OperatorExportError,
    OperatorExportErrorCode,
    OperatorExportHandle,
)
from k5vision.operator_recording_api import (
    _OperatorExportResponse,
    install_operator_recording_api,
)
from k5vision.services.device_registry import DeviceRegistry
from k5vision.user_admin_api import USER_ADMIN_TOKEN_ENV, USER_DB_PATH_ENV

_ADMIN_TOKEN = "synthetic-admin-service-token"
_PASSWORD = "synthetic-password-12345"
_SITE_ID = "synthetic-export-site"


def _packet(sequence: int, timestamp: int, payload_type: int = 96) -> memoryview:
    raw = bytearray(12)
    raw[0] = 0x80
    raw[1] = payload_type
    raw[2:4] = sequence.to_bytes(2, "big")
    raw[4:8] = timestamp.to_bytes(4, "big")
    raw[8:12] = (1).to_bytes(4, "big")
    return memoryview(raw)


async def _write_recording_pair(root: Path, recording_id: UUID, source_id: UUID) -> None:
    sink = FramedAtomicRecordingSink(
        root,
        str(recording_id),
        max_packets=2,
        max_payload_bytes=4096,
    )
    await sink.open()
    await sink.write(_packet(1, 0))
    await sink.write(_packet(2, 9_000))
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


def _principal() -> UserAccount:
    return UserAccount(
        username="export-viewer",
        display_name="Export Viewer",
        role=UserRole.VIEWER,
        enabled=True,
    )


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _configure_user_state(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("K5_CONTROL_PLANE_TOKEN", raising=False)
    monkeypatch.delenv("K5_CONTROL_PLANE_READ_TOKEN", raising=False)
    monkeypatch.setenv(USER_ADMIN_TOKEN_ENV, _ADMIN_TOKEN)
    monkeypatch.setenv(USER_DB_PATH_ENV, str(tmp_path / "users.sqlite3"))


def _issue_session(client: TestClient, username: str, role: str) -> str:
    created = client.post(
        "/api/v1/users",
        json={"username": username, "display_name": username, "role": role, "enabled": True},
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


def test_export_streams_validated_pair_without_live_runtime(monkeypatch, tmp_path: Path) -> None:
    _configure_user_state(monkeypatch, tmp_path)
    root = tmp_path / "recordings"
    application = create_app(
        control_plane_site_id=_SITE_ID,
        device_db_path=tmp_path / "devices.sqlite3",
        operator_recording_root=root,
    )

    with TestClient(application) as client:
        viewer = _issue_session(client, "viewer-user", "viewer")
        administrator = _issue_session(client, "administrator-user", "administrator")
        enrolled = client.post(
            "/api/v1/devices",
            json={
                "name": "Synthetic Camera",
                "host": "192.0.2.30",
                "kind": "camera",
                "protocols": ["rtsp"],
            },
            headers=_headers(administrator),
        )
        assert enrolled.status_code == 201
        recording_id = uuid4()
        asyncio.run(_write_recording_pair(root, recording_id, UUID(enrolled.json()["id"])))

        response = client.get(
            f"/api/v1/operator/recordings/{recording_id}/export",
            headers=_headers(viewer),
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("multipart/mixed; boundary=k5-")
    assert int(response.headers["content-length"]) == len(response.content)
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-k5-recording-id"] == str(recording_id)
    assert response.headers["x-k5-descriptor-validated"] == "true"
    assert b"application/json" in response.content
    assert b"application/vnd.k5.recording" in response.content
    assert b"K5RTPF\x00\x01" in response.content
    assert str(recording_id).encode("ascii") in response.content
    assert b"192.0.2.30" not in response.content
    assert b"rtsp://" not in response.content.lower()
    assert str(tmp_path).encode().lower() not in response.content.lower()
    assert application.state.operator_export_coordinator.active_exports == 0


def test_export_rejects_corrupted_recording_before_media_response(
    monkeypatch, tmp_path: Path
) -> None:
    _configure_user_state(monkeypatch, tmp_path)
    root = tmp_path / "recordings"
    application = create_app(
        control_plane_site_id=_SITE_ID,
        device_db_path=tmp_path / "corrupt-devices.sqlite3",
        operator_recording_root=root,
    )

    with TestClient(application) as client:
        viewer = _issue_session(client, "corrupt-viewer", "viewer")
        administrator = _issue_session(client, "corrupt-administrator", "administrator")
        enrolled = client.post(
            "/api/v1/devices",
            json={
                "name": "Synthetic Camera",
                "host": "192.0.2.31",
                "kind": "camera",
                "protocols": ["rtsp"],
            },
            headers=_headers(administrator),
        )
        assert enrolled.status_code == 201
        recording_id = uuid4()
        asyncio.run(_write_recording_pair(root, recording_id, UUID(enrolled.json()["id"])))
        recording_path = root / f"{recording_id}.k5r"
        corrupted = bytearray(recording_path.read_bytes())
        corrupted[-1] ^= 0x01
        recording_path.write_bytes(corrupted)

        response = client.get(
            f"/api/v1/operator/recordings/{recording_id}/export",
            headers=_headers(viewer),
        )

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/json")
    assert b"K5RTPF\x00\x01" not in response.content
    assert application.state.operator_export_coordinator.active_exports == 0


def test_export_route_requires_human_session(monkeypatch, tmp_path: Path) -> None:
    _configure_user_state(monkeypatch, tmp_path)
    application = create_app(
        control_plane_site_id=_SITE_ID,
        device_db_path=tmp_path / "unauthorized-devices.sqlite3",
        operator_recording_root=tmp_path / "recordings",
    )

    with TestClient(application) as client:
        response = client.get(f"/api/v1/operator/recordings/{uuid4()}/export")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_export_enforces_byte_and_concurrency_limits(tmp_path: Path) -> None:
    async def exercise() -> None:
        registry = DeviceRegistry(
            database_path=tmp_path / "coordinator.sqlite3",
            site_id="export-coordinator",
        )
        device = registry.enroll(
            DeviceCreate(
                name="Synthetic Camera",
                host="192.0.2.32",
                kind=DeviceKind.CAMERA,
                protocols={DeviceProtocol.RTSP},
            )
        ).device
        root = tmp_path / "recordings"
        recording_id = uuid4()
        await _write_recording_pair(root, recording_id, device.id)
        tiny = BoundedOperatorExportCoordinator(registry, root, max_export_bytes=32)
        single = BoundedOperatorExportCoordinator(
            registry,
            root,
            max_export_bytes=4096,
            max_active_exports=1,
        )
        try:
            with pytest.raises(OperatorExportError) as too_large:
                await tiny.begin_export(_principal(), recording_id)
            assert too_large.value.code == OperatorExportErrorCode.EXPORT_TOO_LARGE
            assert tiny.active_exports == 0

            first = await single.begin_export(_principal(), recording_id)
            assert single.active_exports == 1
            with pytest.raises(OperatorExportError) as busy:
                await single.begin_export(_principal(), recording_id)
            assert busy.value.code == OperatorExportErrorCode.EXPORT_BUSY
            assert single.active_exports == 1
            single.release(first)
            assert single.active_exports == 0
            assert first.recording_id == recording_id
        finally:
            registry.close()

    asyncio.run(exercise())


# These transport/lifetime tests use no recording bytes, files, sockets or native runtime.
def _memory_export_coordinator() -> BoundedOperatorExportCoordinator:
    coordinator = object.__new__(BoundedOperatorExportCoordinator)
    coordinator._max_active_exports = 2
    coordinator._reservations = set()
    coordinator._state_lock = asyncio.Lock()
    return coordinator


async def _memory_export_handle(coordinator) -> OperatorExportHandle:
    return OperatorExportHandle(
        recording_id=UUID(int=1),
        recording_path=Path("/never-opened/synthetic.k5r"),
        descriptor_bytes=b"{}",
        file_bytes=0,
        file_identity=(0, 0, 0, 0),
        boundary="synthetic-export-boundary",
        response_bytes=0,
        _reservation=await coordinator._reserve(),
    )


@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
@pytest.mark.parametrize("phase", ["http.response.start", "http.response.body"])
def test_export_response_cancellation_releases_only_its_lease(spec_version, phase) -> None:
    async def exercise() -> None:
        coordinator = _memory_export_coordinator()
        handle = await _memory_export_handle(coordinator)
        other = await _memory_export_handle(coordinator)
        response = _OperatorExportResponse(coordinator, handle)
        blocked = asyncio.Event()
        never = asyncio.Event()

        async def send(message):
            if message["type"] == phase:
                blocked.set()
                await never.wait()

        async def receive():
            await blocked.wait()
            return {"type": "http.disconnect"}

        task = asyncio.create_task(
            response({"type": "http", "asgi": {"spec_version": spec_version}}, receive, send)
        )
        await asyncio.wait_for(blocked.wait(), 1)
        if spec_version == "2.4":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await asyncio.wait_for(task, 1)
        assert coordinator.active_exports == 1
        await response.body_iterator.aclose()
        coordinator.release(handle)
        assert coordinator.active_exports == 1
        replacement = await _memory_export_handle(coordinator)
        assert coordinator.active_exports == 2
        coordinator.release(other)
        coordinator.release(replacement)
        assert coordinator.active_exports == 0

    asyncio.run(exercise())


@pytest.mark.parametrize("phase", ["http.response.start", "http.response.body"])
def test_export_response_send_failure_releases_lease(phase) -> None:
    async def exercise() -> None:
        coordinator = _memory_export_coordinator()
        handle = await _memory_export_handle(coordinator)
        response = _OperatorExportResponse(coordinator, handle)

        async def send(message):
            if message["type"] == phase:
                raise OSError("synthetic transport failure")

        async def receive():
            raise AssertionError("ASGI 2.4 does not poll disconnect")

        with pytest.raises(ClientDisconnect):
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        assert coordinator.active_exports == 0

    asyncio.run(exercise())


def test_export_response_normal_completion_and_body_finalizer_release_once(monkeypatch) -> None:
    async def exercise() -> None:
        coordinator = _memory_export_coordinator()
        handle = await _memory_export_handle(coordinator)
        other = await _memory_export_handle(coordinator)

        async def empty_stream(selected):
            try:
                yield b""
            finally:
                coordinator.release(selected)

        monkeypatch.setattr(coordinator, "stream", empty_stream)
        response = _OperatorExportResponse(coordinator, handle)
        messages = []

        async def send(message):
            messages.append(message)

        async def receive():
            raise AssertionError("ASGI 2.4 does not poll disconnect")

        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        assert messages[-1] == {"type": "http.response.body", "body": b"", "more_body": False}
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-k5-recording-id"] == str(handle.recording_id)
        assert response.headers["x-k5-descriptor-validated"] == "true"
        assert coordinator.active_exports == 1
        coordinator.release(other)
        assert coordinator.active_exports == 0

    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["response_allocation", "header_initialization"])
def test_export_response_constructor_failure_releases_lease(monkeypatch, failure) -> None:
    import k5vision.operator_recording_api as api

    async def exercise() -> None:
        coordinator = _memory_export_coordinator()
        handle = await _memory_export_handle(coordinator)

        async def begin_export(*_args):
            return handle

        async def resolve(_credential):
            return _principal()

        def fail_response(*_args, **_kwargs):
            raise MemoryError("synthetic response allocation failure")

        monkeypatch.setattr(coordinator, "begin_export", begin_export)
        monkeypatch.setattr(api, "BoundedOperatorExportCoordinator", lambda *_args: coordinator)
        monkeypatch.setattr(api, "BoundedOperatorPlaybackTimeline", lambda *_args: None)
        monkeypatch.setattr(
            api, "BoundedRecordingCatalog", lambda *_args: SimpleNamespace(recover=lambda: None)
        )
        if failure == "response_allocation":
            monkeypatch.setattr(api, "_OperatorExportResponse", fail_response)
        else:
            monkeypatch.setattr(api.StreamingResponse, "__init__", fail_response)
        application = FastAPI()
        install_operator_recording_api(
            application,
            registry=SimpleNamespace(),
            session_manager=SimpleNamespace(resolve=resolve),
            source_resolver=None,
            recording_root=Path("/never-opened"),
        )
        endpoint = next(
            route.endpoint
            for route in application.routes
            if getattr(route, "path", "").endswith("/{recording_id}/export")
        )
        with pytest.raises(MemoryError):
            await endpoint(handle.recording_id, ["Bearer synthetic-session"])
        assert coordinator.active_exports == 0

    asyncio.run(exercise())


def _stub_export_validation(monkeypatch, coordinator) -> None:
    import k5vision.operator_export as export

    descriptor = SimpleNamespace(source_id=UUID(int=2), file_bytes=0, to_json_bytes=lambda: b"{}")
    coordinator._recording_root = Path("/never-opened")
    coordinator._max_export_bytes = 4096
    coordinator._registry = SimpleNamespace(get=lambda _source_id: object())
    monkeypatch.setattr(
        export, "_load_recording_pair", lambda *_args: (Path("/never-opened"), descriptor)
    )
    monkeypatch.setattr(
        export.BoundedOperatorPlaybackCoordinator, "_validate_device", lambda _device: None
    )
    monkeypatch.setattr(export, "_verify_recording_for_export", lambda *_args: (0, 0, 0, 0))

    async def in_memory_thread(function, *args):
        return function(*args)

    monkeypatch.setattr(asyncio, "to_thread", in_memory_thread)


@pytest.mark.parametrize("failure", ["thread", "task", "handle", "validation"])
def test_export_reservation_released_on_allocation_or_validation_failure(monkeypatch, failure):
    import k5vision.operator_export as export

    async def exercise() -> None:
        coordinator = _memory_export_coordinator()
        _stub_export_validation(monkeypatch, coordinator)

        def fail(*_args, **_kwargs):
            raise MemoryError("synthetic allocation failure")

        if failure == "thread":
            original_to_thread = asyncio.to_thread

            def fail_validation_thread(function, *args):
                if function is export._verify_recording_for_export:
                    return fail()
                return original_to_thread(function, *args)

            monkeypatch.setattr(asyncio, "to_thread", fail_validation_thread)
        elif failure == "task":
            monkeypatch.setattr(asyncio, "create_task", fail)
        elif failure == "handle":
            monkeypatch.setattr(export, "OperatorExportHandle", fail)
        else:
            monkeypatch.setattr(export, "_verify_recording_for_export", fail)
        with pytest.raises(OperatorExportError) as error:
            await coordinator.begin_export(_principal(), UUID(int=1))
        assert error.value.code == OperatorExportErrorCode.EXPORT_INVALID
        assert "synthetic" not in str(error.value)
        assert coordinator.active_exports == 0

    asyncio.run(exercise())


@pytest.mark.parametrize("validation_fails", [False, True])
def test_export_reservation_survives_cancel_until_validation_settles(monkeypatch, validation_fails):
    import k5vision.operator_export as export

    async def exercise() -> None:
        coordinator = _memory_export_coordinator()
        coordinator._max_active_exports = 1
        _stub_export_validation(monkeypatch, coordinator)
        started = asyncio.Event()
        finish = asyncio.Event()
        workers = []
        original_create_task = asyncio.create_task

        def track_worker(coroutine):
            worker = original_create_task(coroutine)
            workers.append(worker)
            return worker

        async def in_memory_thread(function, *args):
            if function is export._verify_recording_for_export:
                started.set()
                await finish.wait()
                if validation_fails:
                    raise RuntimeError("synthetic validation failure")
            return function(*args)

        monkeypatch.setattr(asyncio, "create_task", track_worker)
        monkeypatch.setattr(asyncio, "to_thread", in_memory_thread)
        request = original_create_task(coordinator.begin_export(_principal(), UUID(int=1)))
        await asyncio.wait_for(started.wait(), 1)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        request.cancel()
        assert coordinator.active_exports == 1
        with pytest.raises(OperatorExportError) as error:
            await coordinator._reserve()
        assert error.value.code == OperatorExportErrorCode.EXPORT_BUSY
        finish.set()
        if validation_fails:
            with pytest.raises(RuntimeError):
                await workers[0]
        else:
            await workers[0]
        await asyncio.sleep(0)
        assert coordinator.active_exports == 0

    asyncio.run(exercise())


@pytest.mark.parametrize("close_error", [RuntimeError, asyncio.CancelledError])
def test_export_response_close_failure_still_releases_lease(monkeypatch, close_error) -> None:
    async def exercise() -> None:
        coordinator = _memory_export_coordinator()
        handle = await _memory_export_handle(coordinator)
        response = _OperatorExportResponse(coordinator, handle)

        async def finished_response(*_args):
            return None

        async def fail_close():
            raise close_error()

        monkeypatch.setattr(StreamingResponse, "__call__", finished_response)
        response._stream = SimpleNamespace(aclose=fail_close)
        with pytest.raises(close_error):
            await response({}, None, None)
        assert coordinator.active_exports == 0
        await response.body_iterator.aclose()
        assert coordinator.active_exports == 0

    asyncio.run(exercise())
