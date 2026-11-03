from __future__ import annotations

import asyncio
import hashlib
import json
import os
import struct
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from k5vision.domain.users import UserRole
from k5vision.media.mediamtx_provider import (
    MediaMTXBinding,
    MediaMTXCredentials,
    MediaPlaybackSpan,
)
from k5vision.operator_export_validator import _leading_video_offset, _presentation_duration
from k5vision.operator_exports import ExportError, ExportManager, _verify_mp4
from k5vision.operator_exports import _download as native_download
from k5vision.operator_exports_api import install_operator_exports_api

_START = datetime(2026, 10, 28, 12, 30, tzinfo=UTC)
_CONTENT = b"genuine export byte fixture" * 16


@pytest.fixture
def private_root(tmp_path, monkeypatch):
    if os.name == "posix":
        original = Path.lstat

        def lstat(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path in (Path("/"), Path("/tmp")) and info.st_uid == 65534:
                values = list(info)
                values[4] = 0
                return os.stat_result(values)
            return info

        monkeypatch.setattr(Path, "lstat", lstat)
    return tmp_path / "exports"


def _download(_service, _binding, _start, _duration, destination, progress, _stopped):
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(_CONTENT)
    progress(len(_CONTENT))


def _validate(path, _duration=None):
    if path.read_bytes() != _CONTENT:
        raise ValueError("invalid fixture")


def _service():
    class Provider:
        gap = False

        async def playback_spans(self, binding, *, start, end):
            half = (end - start).total_seconds() / 2
            return (
                (
                    MediaPlaybackSpan("a", binding.device_id, "main", start, half),
                    MediaPlaybackSpan(
                        "b", binding.device_id, "main", start + timedelta(seconds=half), half
                    ),
                )
                if not self.gap
                else (MediaPlaybackSpan("a", binding.device_id, "main", start, half),)
            )

    return SimpleNamespace(site_id="test-site", provider=Provider(), status=lambda: "ready")


def _manager(root, service=None, **kwargs):
    return ExportManager(
        root, service=service or _service(), downloader=_download, validator=_validate, **kwargs
    )


async def _finish(manager):
    while manager._tasks:
        await asyncio.gather(*tuple(manager._tasks))


def test_completed_export_is_owned_and_reopens_after_product_restart(private_root):
    owner, camera = uuid4(), uuid4()
    service = _service()
    manager = _manager(private_root, service)

    async def run():
        job = await manager.create(
            owner=owner,
            binding=MediaMTXBinding(service.site_id, camera),
            start=_START,
            duration=600,
        )
        await _finish(manager)
        assert job.status == "ready"
        assert job.sha256 == hashlib.sha256(_CONTENT).hexdigest()
        assert manager.file(job).read_bytes() == _CONTENT
        assert manager.get(job.id, owner=uuid4(), camera=camera) is None
        assert manager.get(job.id, owner=owner, camera=uuid4()) is None
        await manager.aclose()
        return job

    job = asyncio.run(run())
    restarted = _manager(private_root, service)
    reopened = restarted.get(job.id, owner=owner, camera=camera)
    assert reopened is not None and reopened.status == "ready"
    asyncio.run(restarted.verify_ready(reopened))
    assert restarted.file(reopened).read_bytes() == _CONTENT
    assert reopened.public()["filename"] == "K5-recording-2026-10-28T12-30-00Z.mp4"
    assert reopened.public()["download_url"] == f"/media/exports/{camera}/{job.id}.mp4"


def test_export_rejects_real_recording_gap_without_creating_file(private_root):
    service = _service()
    service.provider.gap = True
    manager = _manager(private_root, service)

    async def run():
        with pytest.raises(ExportError, match="export_recording_gap"):
            await manager.create(
                owner=uuid4(),
                binding=MediaMTXBinding(service.site_id, uuid4()),
                start=_START,
                duration=120,
            )
        assert not manager._tasks
        assert list(private_root.iterdir()) == []

    asyncio.run(run())


def test_invalid_native_output_never_becomes_downloadable(private_root):
    manager = _manager(private_root)

    def invalid(_path, _duration):
        raise ValueError("native failure includes private-url")

    manager._validator = invalid

    async def run():
        job = await manager.create(
            owner=uuid4(),
            binding=MediaMTXBinding(manager.service.site_id, uuid4()),
            start=_START,
            duration=120,
        )
        await _finish(manager)
        assert job.status == "failed"
        assert job.phase == "validating"
        assert job.public()["download_url"] is None
        assert job.error == "export_preparation_failed"
        assert "private-url" not in str(job.public())
        assert list(private_root.iterdir()) == []

    asyncio.run(run())


def test_oversize_export_reports_fixed_actionable_error(private_root):
    manager = _manager(private_root)

    def too_large(*_args):
        raise ExportError("export_too_large")

    manager._downloader = too_large

    async def run():
        job = await manager.create(
            owner=uuid4(),
            binding=MediaMTXBinding(manager.service.site_id, uuid4()),
            start=_START,
            duration=120,
        )
        await _finish(manager)
        assert job.status == "failed"
        assert job.public()["error"] == "export_too_large"
        assert job.public()["phase"] == "downloading"
        assert job.public()["download_url"] is None

    asyncio.run(run())


def test_export_lease_uses_elapsed_time_and_completed_file_tampering_is_refused(private_root):
    clock = [100.0]
    manager = _manager(private_root, clock=lambda: clock[0])
    owner, camera = uuid4(), uuid4()

    async def run():
        job = await manager.create(
            owner=owner,
            binding=MediaMTXBinding(manager.service.site_id, camera),
            start=_START,
            duration=120,
        )
        await _finish(manager)
        return job

    job = asyncio.run(run())
    clock[0] += 300
    assert manager.get(job.id, owner=owner, camera=camera) is job
    manager.file(job).write_bytes(b"tampered file")
    with pytest.raises(ExportError, match="export_file_unavailable"):
        manager.file(job)
    assert _manager(private_root).get(job.id, owner=owner, camera=camera) is None
    clock[0] += 24 * 3600
    assert manager.get(job.id, owner=owner, camera=camera) is None


def test_stop_joins_actual_native_preparation_and_removes_partial_file(private_root):
    entered = threading.Event()
    exited = threading.Event()
    manager = _manager(private_root)

    def blocked(_service, _binding, _start, _duration, path, progress, stopped):
        path.write_bytes(_CONTENT)
        progress(len(_CONTENT))
        entered.set()
        assert stopped.wait(5)
        exited.set()

    manager._downloader = blocked

    async def run():
        job = await manager.create(
            owner=uuid4(),
            binding=MediaMTXBinding(manager.service.site_id, uuid4()),
            start=_START,
            duration=120,
        )
        assert await asyncio.to_thread(entered.wait, 5)
        await manager.aclose()
        assert exited.is_set()
        assert job.status == "failed"
        assert list(private_root.iterdir()) == []

    asyncio.run(run())


def test_failed_jobs_do_not_exhaust_export_capacity(private_root, monkeypatch):
    monkeypatch.setattr("k5vision.operator_exports.MAX_EXPORT_JOBS", 3)
    manager = _manager(private_root)

    async def run():
        for _ in range(5):
            manager._validator = lambda _path, _duration: (_ for _ in ()).throw(ValueError())
            job = await manager.create(
                owner=uuid4(),
                binding=MediaMTXBinding(manager.service.site_id, uuid4()),
                start=_START,
                duration=120,
            )
            await _finish(manager)
            assert job.status == "failed"
        manager._validator = _validate
        job = await manager.create(
            owner=uuid4(),
            binding=MediaMTXBinding(manager.service.site_id, uuid4()),
            start=_START,
            duration=120,
        )
        await _finish(manager)
        assert job.status == "ready"
        assert len(manager._jobs) == 3

    asyncio.run(run())


def test_crashed_partial_files_are_cleaned_only_on_authorized_operation(private_root):
    private_root.mkdir(mode=0o700)
    orphan = private_root / (str(uuid4()) + ".part")
    orphan.write_bytes(_CONTENT)
    manager = _manager(private_root)
    assert orphan.exists()  # Construction precedes the product profile lock.

    async def run():
        job = await manager.create(
            owner=uuid4(),
            binding=MediaMTXBinding(manager.service.site_id, uuid4()),
            start=_START,
            duration=120,
        )
        await _finish(manager)
        assert job.status == "ready"
        assert not orphan.exists()
        assert sorted(path.suffix for path in private_root.iterdir()) == [".json", ".mp4"]

    asyncio.run(run())


def test_crashed_atomic_metadata_temporary_is_cleaned_after_profile_ownership(private_root):
    private_root.mkdir(mode=0o700)
    orphan = private_root / (".operator-" + uuid4().hex + ".tmp")
    orphan.write_bytes(b"partial JSON")
    manager = _manager(private_root)
    assert orphan.exists()
    manager._prune(reserve=False)
    assert not orphan.exists()


def test_download_reader_keeps_file_alive_through_expiration_then_releases_it(private_root):
    clock = [1.0]
    manager = _manager(private_root, clock=lambda: clock[0])
    owner, camera = uuid4(), uuid4()

    async def run():
        job = await manager.create(
            owner=owner,
            binding=MediaMTXBinding(manager.service.site_id, camera),
            start=_START,
            duration=120,
        )
        await _finish(manager)
        source = await manager.open_file(job)
        assert job.readers == 1
        clock[0] += 24 * 3600 + 1
        assert manager.get(job.id, owner=owner, camera=camera) is None
        assert source.read() == _CONTENT
        assert manager._path(job).exists()
        manager.release_file(job, source)
        manager._prune(reserve=False)
        assert not manager._path(job).exists()
        assert list(private_root.iterdir()) == []

    asyncio.run(run())


def test_reopened_export_is_verified_without_blocking_constructor(private_root):
    calls = []
    manager = _manager(private_root)

    async def run():
        job = await manager.create(
            owner=uuid4(),
            binding=MediaMTXBinding(manager.service.site_id, uuid4()),
            start=_START,
            duration=120,
        )
        await _finish(manager)
        return job

    job = asyncio.run(run())
    restarted = ExportManager(
        private_root,
        service=manager.service,
        downloader=_download,
        validator=lambda path, _duration: calls.append(path),
    )
    assert calls == []
    reopened = restarted.get(job.id, owner=job.owner, camera=job.camera)
    assert reopened is not None
    asyncio.run(restarted.verify_ready(reopened))
    assert calls == [restarted._path(reopened)]


def test_file_replacement_between_admission_and_open_is_rejected(private_root, monkeypatch):
    manager = _manager(private_root)

    async def run():
        job = await manager.create(
            owner=uuid4(),
            binding=MediaMTXBinding(manager.service.site_id, uuid4()),
            start=_START,
            duration=120,
        )
        await _finish(manager)
        original_open = os.open
        path = manager.file(job)

        def replace(value, flags, *args, **kwargs):
            if Path(value) == path:
                path.unlink()
                path.write_bytes(b"changed bytes" * 16)
            return original_open(value, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", replace)
        with pytest.raises(ExportError, match="export_file_unavailable"):
            await manager.open_file(job)
        assert job.readers == 0

    asyncio.run(run())


@pytest.fixture
def api(private_root):
    service = _service()
    manager = _manager(private_root, service)
    camera = uuid4()
    accounts = {
        name: SimpleNamespace(id=uuid4(), role=role)
        for name, role in (
            ("admin", UserRole.ADMINISTRATOR),
            ("operator", UserRole.OPERATOR),
            ("viewer", UserRole.VIEWER),
        )
    }
    tokens = {name * 16: account for name, account in accounts.items()}
    access = {account.id: True for account in accounts.values()}

    async def authenticate(token):
        if token not in tokens:
            raise HTTPException(401, "Unauthorized")
        return tokens[token], token

    async def session(request: Request):
        value = request.headers.get("Authorization", "")
        if not value.startswith("Bearer "):
            raise HTTPException(401, "Unauthorized")
        return await authenticate(value[7:])

    def selection(account, identifier):
        if identifier != camera or not access[account.id]:
            raise HTTPException(404, "Camera not found")
        return SimpleNamespace(binding=MediaMTXBinding(service.site_id, camera))

    def origin(request):
        if request.headers.get("Origin") not in (None, "http://127.0.0.1:8080"):
            raise HTTPException(403, "Invalid operator origin")

    app = FastAPI()
    install_operator_exports_api(
        app,
        service=service,
        state_root=private_root.parent,
        manager=manager,
        require_session=session,
        authenticate=authenticate,
        authorized_selection=selection,
        admit_origin=origin,
    )
    with TestClient(app, base_url="http://127.0.0.1:8080") as client:
        yield SimpleNamespace(
            client=client,
            manager=manager,
            accounts=accounts,
            tokens=tokens,
            access=access,
            camera=camera,
            prefix=f"/api/v1/operator/cameras/{camera}/exports",
        )


def _headers(role="operator"):
    return {"Authorization": "Bearer " + role * 16}


def _ready(api):
    response = api.client.post(
        api.prefix, headers=_headers(), json={"start": _START.isoformat(), "duration": 120}
    )
    assert response.status_code == 202, response.text
    identifier = response.json()["id"]
    for _ in range(100):
        response = api.client.get(api.prefix + "/" + identifier, headers=_headers())
        assert response.status_code == 200
        if response.json()["status"] in {"ready", "failed"}:
            break
    assert response.json()["status"] == "ready", response.text
    return response.json()


def test_normal_media_cookie_download_has_real_attachment_length_and_stable_filename(api):
    job = _ready(api)
    assert job["phase"] == "ready"
    file = api.client.get(job["download_url"])
    assert file.status_code == 200, file.text
    assert file.content == _CONTENT
    assert file.headers["content-type"] == "video/mp4"
    assert file.headers["content-length"] == str(len(_CONTENT))
    assert file.headers["cache-control"] == "no-store"
    assert file.headers["content-disposition"] == (
        'attachment; filename="K5-recording-2026-10-28T12-30-00Z.mp4"'
    )
    cookie = api.client.cookies.get("k5_operator_media")
    assert cookie == "operator" * 16
    assert "expires" not in str(api.client.cookies).lower()


def test_completed_export_requires_current_owner_role_camera_grant_and_session(api):
    job = _ready(api)
    url = api.prefix + "/" + job["id"] + "/file"
    assert api.client.get(url, headers=_headers("admin")).status_code == 404
    assert api.client.get(url, headers=_headers("viewer")).status_code == 403
    operator = api.accounts["operator"]
    api.access[operator.id] = False
    assert api.client.get(job["download_url"]).status_code == 404
    api.access[operator.id] = True
    del api.tokens["operator" * 16]
    assert api.client.get(job["download_url"]).status_code == 401


def test_account_revoked_while_history_is_queried_cannot_start_export(api):
    provider = api.manager.service.provider
    original = provider.playback_spans

    async def revoked(binding, *, start, end):
        spans = await original(binding, start=start, end=end)
        del api.tokens["operator" * 16]
        return spans

    provider.playback_spans = revoked
    result = api.client.post(
        api.prefix, headers=_headers(), json={"start": _START.isoformat(), "duration": 120}
    )
    assert result.status_code == 401
    assert not api.manager._jobs


def test_duplicate_media_cookie_does_not_authorize_export_download(api):
    job = _ready(api)
    result = api.client.get(
        job["download_url"],
        headers={"Cookie": "k5_operator_media=" + "operator" * 16 + "; k5_operator_media=bad"},
    )
    assert result.status_code == 401


@pytest.mark.parametrize("role", [None, "viewer"])
def test_export_creation_refuses_anonymous_and_viewer(api, role):
    result = api.client.post(
        api.prefix,
        headers={} if role is None else _headers(role),
        json={"start": _START.isoformat(), "duration": 120},
    )
    assert result.status_code == (401 if role is None else 403)
    assert not api.manager._jobs


@pytest.mark.parametrize(
    "selection",
    [
        {"start": "2026-10-28T12:30:00", "duration": 60},
        {"start": _START.isoformat(), "duration": 0},
        {"start": _START.isoformat(), "duration": 3601},
        {"start": _START.isoformat(), "duration": 60, "source_uri": "rtsp://private"},
    ],
)
def test_export_selection_is_bounded_and_has_no_camera_uri_input(api, selection):
    result = api.client.post(api.prefix, headers=_headers(), json=selection)
    assert result.status_code == 422
    assert not api.manager._jobs


def test_export_mutation_rejects_cross_origin(api):
    result = api.client.post(
        api.prefix,
        headers={**_headers(), "Origin": "https://example.invalid"},
        json={"start": _START.isoformat(), "duration": 60},
    )
    assert result.status_code == 403
    assert not api.manager._jobs


def test_decoder_refuses_html_and_truncated_mp4_before_native_loading(private_root):
    private_root.mkdir(mode=0o700)
    path = private_root / "clip.mp4"
    path.write_bytes(b"<html>download failed</html>" * 4)
    with pytest.raises(ValueError):
        _verify_mp4(path)
    path.write_bytes(bytes.fromhex("0000002066747970") + b"short")
    with pytest.raises(ValueError):
        _verify_mp4(path)


@pytest.fixture
def worker_http():
    state = SimpleNamespace(body=_CONTENT, declared=len(_CONTENT), redirect=None, requests=[])

    class Worker(BaseHTTPRequestHandler):
        def do_GET(self):
            state.requests.append((self.path, self.headers.get("Authorization")))
            if state.redirect:
                self.send_response(302)
                self.send_header("Location", state.redirect)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            if state.declared is not None:
                self.send_header("Content-Length", str(state.declared))
            self.end_headers()
            self.wfile.write(state.body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Worker)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.port = server.server_port
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        assert not thread.is_alive()


def test_native_export_transfer_uses_fixed_loopback_and_private_header(private_root, worker_http):
    private_root.mkdir(mode=0o700)
    destination = private_root / "output.part"
    service = SimpleNamespace(
        ports={"playback_port": worker_http.port},
        credentials=MediaMTXCredentials("k5test", "a" * 32),
    )
    binding = MediaMTXBinding("site", uuid4())
    progress = []
    native_download(service, binding, _START, 2, destination, progress.append, threading.Event())
    assert destination.read_bytes() == _CONTENT
    assert progress[-1] == len(_CONTENT)
    path, authorization = worker_http.requests[0]
    assert urlsplit(path).path == "/get"
    assert parse_qs(urlsplit(path).query) == {
        "path": [binding.path_name],
        "start": [_START.isoformat()],
        "duration": ["2"],
        "format": ["mp4"],
    }
    assert authorization.startswith("Basic ")
    assert "a" * 32 not in path


def test_native_export_transfer_rejects_oversize_before_reading(private_root, worker_http):
    private_root.mkdir(mode=0o700)
    worker_http.declared = 257 * 1024 * 1024
    service = SimpleNamespace(
        ports={"playback_port": worker_http.port},
        credentials=MediaMTXCredentials("k5test", "a" * 32),
    )
    with pytest.raises(ExportError, match="export_too_large"):
        native_download(
            service,
            MediaMTXBinding("site", uuid4()),
            _START,
            2,
            private_root / "output.part",
            lambda _: None,
            threading.Event(),
        )
    assert (private_root / "output.part").stat().st_size == 0


def test_native_export_transfer_enforces_size_without_length_header(
    private_root, worker_http, monkeypatch
):
    private_root.mkdir(mode=0o700)
    worker_http.declared = None
    monkeypatch.setattr("k5vision.operator_exports.MAX_EXPORT_BYTES", 128)
    service = SimpleNamespace(
        ports={"playback_port": worker_http.port},
        credentials=MediaMTXCredentials("k5test", "a" * 32),
    )
    with pytest.raises(ExportError, match="export_too_large"):
        native_download(
            service,
            MediaMTXBinding("site", uuid4()),
            _START,
            2,
            private_root / "output.part",
            lambda _: None,
            threading.Event(),
        )


def test_native_export_transfer_never_follows_redirect(private_root, worker_http):
    private_root.mkdir(mode=0o700)
    worker_http.redirect = f"http://127.0.0.1:{worker_http.port}/redirect-target"
    service = SimpleNamespace(
        ports={"playback_port": worker_http.port},
        credentials=MediaMTXCredentials("k5test", "a" * 32),
    )
    with pytest.raises(ValueError):
        native_download(
            service,
            MediaMTXBinding("site", uuid4()),
            _START,
            2,
            private_root / "output.part",
            lambda _: None,
            threading.Event(),
        )
    assert len(worker_http.requests) == 1


def test_native_export_transfer_rejects_truncated_declared_length(private_root, worker_http):
    private_root.mkdir(mode=0o700)
    worker_http.declared += 20
    service = SimpleNamespace(
        ports={"playback_port": worker_http.port},
        credentials=MediaMTXCredentials("k5test", "a" * 32),
    )
    with pytest.raises(ValueError):
        native_download(
            service,
            MediaMTXBinding("site", uuid4()),
            _START,
            2,
            private_root / "output.part",
            lambda _: None,
            threading.Event(),
        )


def test_hung_native_decoder_is_killed_and_reaped_without_private_argv(private_root, monkeypatch):
    private_root.mkdir(mode=0o700)
    path = private_root / "private-selection.mp4"
    path.write_bytes(b"\x00\x00\x00\x10ftypisom0000\x00\x00\x00\x08moov\x00\x00\x00\x08mdat")
    calls = []

    class Ownership:
        assigned = False

        def assign(self, process):
            self.assigned = True
            process.assigned = True

        def close(self):
            calls.append("job-closed")

    class Process:
        returncode = None
        assigned = False

        def __init__(self, arguments, **kwargs):
            assert str(path) not in str(arguments)
            assert arguments == [sys.executable, "-B", "-m", "k5vision.operator_export_validator"]
            assert kwargs["stderr"] == subprocess.DEVNULL
            calls.append("started")

        def communicate(self, data=None, timeout=None):
            if data is not None:
                assert json.loads(data) == {"path": str(path)}
                assert timeout == 10
                if os.name == "nt":
                    assert self.assigned
                calls.append("deadline")
                raise subprocess.TimeoutExpired("decoder", 10)
            assert self.returncode == -9 and timeout == 2
            calls.append("reaped")
            return b"", None

        def kill(self):
            calls.append("killed")
            self.returncode = -9

        def poll(self):
            return self.returncode

    monkeypatch.setattr("k5vision.operator_native_process._WindowsJob", Ownership)
    monkeypatch.setattr("k5vision.operator_exports.subprocess.Popen", Process)
    with pytest.raises(ValueError, match="deadline"):
        _verify_mp4(path, 2)
    assert calls[:4] == ["started", "deadline", "killed", "reaped"]


@pytest.mark.skipif(
    os.name != "posix", reason="Linux generated fixture; Windows has installed proof"
)
@pytest.mark.parametrize("fps", [1, 10])
def test_actual_native_decoder_verifies_clip_duration_and_rejects_short_output(private_root, fps):
    pytest.importorskip("cv2")
    private_root.mkdir(mode=0o700)
    path = private_root / "clip.mp4"
    subprocess.run(
        [
            "/usr/bin/ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=160x120:rate={fps}",
            "-t",
            "2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=20,
    )
    _verify_mp4(path, 2)
    with pytest.raises(ValueError):
        _verify_mp4(path, 600)
    raw = path.read_bytes()
    path.write_bytes(raw[: len(raw) // 2])
    with pytest.raises(ValueError):
        _verify_mp4(path, 2)


@pytest.mark.skipif(
    os.name != "posix", reason="Linux generated fixture; Windows has installed proof"
)
@pytest.mark.parametrize("b_frames", [0, 2])
def test_real_edit_list_export_uses_presented_duration_and_decodes_actual_late_frame(
    private_root, b_frames
):
    pytest.importorskip("cv2")
    private_root.mkdir(mode=0o700)
    source, selected = private_root / "source.mp4", private_root / "selected.mp4"
    subprocess.run(
        [
            "/usr/bin/ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x120:rate=15",
            "-t",
            "5",
            "-c:v",
            "libx264",
            "-g",
            "30",
            "-bf",
            str(b_frames),
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=20,
    )
    subprocess.run(
        [
            "/usr/bin/ffmpeg",
            "-v",
            "error",
            "-ss",
            "0.2",
            "-i",
            str(source),
            "-t",
            "2",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(selected),
        ],
        check=True,
        capture_output=True,
        timeout=20,
    )
    inspected = subprocess.run(
        [
            "/usr/bin/ffprobe",
            "-v",
            "error",
            "-count_frames",
            "-show_entries",
            "stream=nb_frames,nb_read_frames,duration",
            "-of",
            "json",
            str(selected),
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )
    stream = json.loads(inspected.stdout)["streams"][0]
    assert int(stream["nb_frames"]) > int(stream["nb_read_frames"]) >= 30
    presented = float(stream["duration"])
    _verify_mp4(selected, presented)
    if b_frames:
        # Stream-copy trimming can preserve trailing reordered frames. Matching
        # the nominal command's request would silently accept a longer clip.
        with pytest.raises(ValueError):
            _verify_mp4(selected, 2)
    with pytest.raises(ValueError):
        _verify_mp4(selected, 600)
    child = subprocess.run(
        [sys.executable, "-B", "-m", "k5vision.operator_export_validator"],
        input=json.dumps({"path": str(selected)}).encode(),
        capture_output=True,
        timeout=12,
    )
    assert child.returncode == 0
    metadata = json.loads(child.stdout)
    assert abs(metadata["duration"] - presented) < 0.002
    assert presented - 0.1 <= metadata["tail_time"] < presented + 0.1


def _edited_timing_movie(path, *, timing, cutoff, composition=None, version=0):
    def box(kind, payload):
        return struct.pack(">I4s", len(payload) + 8, kind) + payload

    clock = bytes(4) + struct.pack(">IIII", 0, 0, 1000, 2066)
    video_clock = bytes(4) + struct.pack(">IIII", 0, 0, 90000, 186000)
    edit = bytes(4) + struct.pack(">IIihh", 1, 2066, cutoff, 1, 0)
    table = box(
        b"stts",
        bytes(4)
        + struct.pack(">I", len(timing))
        + b"".join(struct.pack(">II", n, delta) for n, delta in timing),
    )
    if composition is not None:
        table += box(
            b"ctts",
            bytes([version, 0, 0, 0])
            + struct.pack(">I", len(composition))
            + b"".join(
                struct.pack(">Ii" if version else ">II", n, offset) for n, offset in composition
            ),
        )
    media = box(b"hdlr", bytes(8) + b"vide") + box(b"mdhd", video_clock)
    media += box(b"minf", box(b"stbl", table))
    track = box(b"edts", box(b"elst", edit)) + box(b"mdia", media)
    audio = box(b"trak", box(b"mdia", box(b"hdlr", bytes(8) + b"soun")))
    path.write_bytes(box(b"moov", box(b"mvhd", clock) + audio + box(b"trak", track)))


@pytest.mark.parametrize(
    "timing,cutoff,expected",
    [
        ([(15, 6000), (1, 5999), (18, 6000)], 21107, 2893 / 90000),
        ([(1, 5999), (33, 6000)], 18000, 5999 / 90000),
    ],
)
def test_fractional_edit_boundary_uses_first_retained_video_sample(
    private_root, timing, cutoff, expected
):
    private_root.mkdir(mode=0o700)
    path = private_root / "movie.mp4"
    _edited_timing_movie(path, timing=timing, cutoff=cutoff)
    assert _leading_video_offset(path) == pytest.approx(expected)


@pytest.mark.parametrize("version,offset", [(0, 3000), (1, -3000)])
def test_edit_boundary_includes_unsigned_and_signed_b_frame_composition_offsets(
    private_root, version, offset
):
    private_root.mkdir(mode=0o700)
    path = private_root / "movie.mp4"
    _edited_timing_movie(
        path,
        timing=[(34, 6000)],
        cutoff=18001,
        composition=[(34, offset)],
        version=version,
    )
    assert _leading_video_offset(path) == pytest.approx(2999 / 90000)


@pytest.mark.parametrize("version", [0, 1])
def test_movie_duration_parser_supports_both_mp4_header_versions(private_root, version):
    private_root.mkdir(mode=0o700)
    payload = (
        bytes(4) + struct.pack(">IIII", 0, 0, 1000, 2000)
        if version == 0
        else b"\x01\x00\x00\x00" + struct.pack(">QQIQ", 0, 0, 1000, 2000)
    )
    mvhd = struct.pack(">I4s", len(payload) + 8, b"mvhd") + payload
    movie = struct.pack(">I4s", len(mvhd) + 8, b"moov") + mvhd
    path = private_root / "movie.mp4"
    path.write_bytes(movie)
    assert _presentation_duration(path) == 2


@pytest.mark.parametrize("scale,duration", [(0, 2000), (1000, 0), (1000, 3606000)])
def test_movie_duration_parser_refuses_invalid_or_unbounded_timelines(
    private_root, scale, duration
):
    private_root.mkdir(mode=0o700)
    payload = bytes(4) + struct.pack(">IIII", 0, 0, scale, duration)
    mvhd = struct.pack(">I4s", len(payload) + 8, b"mvhd") + payload
    path = private_root / "movie.mp4"
    path.write_bytes(struct.pack(">I4s", len(mvhd) + 8, b"moov") + mvhd)
    with pytest.raises(ValueError):
        _presentation_duration(path)
