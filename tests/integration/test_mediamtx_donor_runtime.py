"""Opt-in native donor acceptance using generated media and supplied tools only.

Set K5_MEDIAMTX_DONOR_EXE, K5_DONOR_FFMPEG and K5_DONOR_FFPROBE to executable
paths. This test does not download or bundle binaries, use a camera, or qualify
analytics/browser behavior. Its receipt identifies the actual execution platform.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import platform
import secrets
import socket
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

import pytest

from k5vision.domain.devices import Device, DeviceProtocol
from k5vision.domain.streams import VideoCodec
from k5vision.media.mediamtx_config import MediaMTXSource, generate_mediamtx_configuration
from k5vision.media.mediamtx_provider import (
    MEDIAMTX_VERSION,
    MediaMTXBinding,
    MediaMTXCredentials,
    MediaMTXProvider,
    MediaPlaybackSpan,
    MediaWorkerError,
    MediaWorkerErrorCode,
)

_TOOLS = ("K5_MEDIAMTX_DONOR_EXE", "K5_DONOR_FFMPEG", "K5_DONOR_FFPROBE")
_GENERATED_SECONDS = 16
_MAX_PLAYBACK_BYTES = 16 * 1024 * 1024
pytestmark = pytest.mark.skipif(
    not all(os.getenv(name) for name in _TOOLS),
    reason="Native donor acceptance requires explicitly supplied MediaMTX, FFmpeg and ffprobe",
)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _tool(name: str) -> Path:
    value = os.getenv(name)
    assert value, "Native donor tool was not explicitly supplied"
    path = Path(value).resolve(strict=True)
    assert path.is_file(), "Native donor tool must be an executable file"
    return path


def _private_write(path: Path, content: str) -> None:
    # Windows fixture state is temporary, not an assertion of production ACLs.
    with path.open("x", encoding="utf-8") as output:
        if os.name == "posix":
            os.fchmod(output.fileno(), 0o600)
        output.write(content)
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600


def _reserve_ports(count: int) -> tuple[list[socket.socket], tuple[int, ...]]:
    reservations: list[socket.socket] = []
    try:
        for _ in range(count):
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            reservations.append(listener)
            if os.name == "nt":
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind(("127.0.0.1", 0))
        ports = tuple(listener.getsockname()[1] for listener in reservations)
        assert len(set(ports)) == count
        return reservations, ports
    except BaseException:
        for listener in reservations:
            listener.close()
        raise


def _start(
    command: list[str], directory: Path, *, env: dict[str, str] | None = None
) -> subprocess.Popen[bytes]:
    # Native source diagnostics stay out of ordinary test output and receipts.
    return subprocess.Popen(
        command,
        cwd=directory,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _stop_owned(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            process.terminate()
        except OSError:
            if process.poll() is None:
                raise
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


async def _listeners_ready(ports: tuple[int, ...]) -> bool:
    for port in ports:
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 0.5)
        except (OSError, TimeoutError):
            return False
        writer.close()
        await writer.wait_closed()
    return True


async def _eventually(
    condition: Callable[[], Awaitable[object]],
    *,
    label: str,
    processes: tuple[subprocess.Popen[bytes], ...],
    timeout: float = 12,
) -> object:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        assert all(process.poll() is None for process in processes), label + ": worker exited"
        try:
            result = await condition()
            if result:
                return result
        except MediaWorkerError as error:
            if error.code not in {MediaWorkerErrorCode.UNAVAILABLE, MediaWorkerErrorCode.TIMEOUT}:
                raise
        await asyncio.sleep(0.1)
    pytest.fail(label + ": readiness deadline expired")


async def _publisher_finished(process: subprocess.Popen[bytes]) -> None:
    try:
        code = await asyncio.to_thread(process.wait, timeout=_GENERATED_SECONDS + 10)
    except subprocess.TimeoutExpired:
        pytest.fail("Generated publisher exceeded its bounded duration")
    assert code == 0, "Generated RTSP publication failed"


def _anonymous_unauthorized(url: str) -> None:
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(Request(url), timeout=3):
            pytest.fail("Native worker admitted an anonymous request")
    except HTTPError as error:
        status = error.code
        error.close()
        assert status == 401, "Native worker did not reject anonymous access with HTTP 401"


def _download(
    origin: str,
    binding: MediaMTXBinding,
    credentials: MediaMTXCredentials,
    span: MediaPlaybackSpan,
    destination: Path,
) -> None:
    query = urlencode(
        {
            "path": binding.path_name,
            "start": span.started_at.isoformat(),
            "duration": span.duration_seconds,
            "format": "mp4",
        }
    )
    token = base64.b64encode(
        f"{credentials.username}:{credentials.password}".encode("ascii")
    ).decode("ascii")
    request = Request(origin + "/get?" + query, headers={"Authorization": "Basic " + token})
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=15) as response:
            assert response.status == 200
            assert response.headers.get_content_type() == "video/mp4"
            payload = response.read(_MAX_PLAYBACK_BYTES + 1)
    except OSError:
        raise AssertionError("Authenticated generated playback download failed") from None
    assert 0 < len(payload) <= _MAX_PLAYBACK_BYTES, "Generated playback exceeded its byte limit"
    destination.write_bytes(payload)


def _decoded_frames(ffprobe: Path, video: Path) -> int:
    result = subprocess.run(
        [
            str(ffprobe),
            "-v",
            "error",
            "-count_frames",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,width,height,nb_read_frames",
            "-of",
            "json",
            str(video),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, "Generated recording could not be decoded"
    assert len(result.stdout) <= 4096, "Generated recording probe output exceeded its bound"
    streams = json.loads(result.stdout)["streams"]
    assert len(streams) == 1
    assert (streams[0]["codec_name"], streams[0]["width"], streams[0]["height"]) == (
        "h264",
        320,
        240,
    )
    frames = int(streams[0]["nb_read_frames"])
    assert 0 < frames <= _GENERATED_SECONDS * 15
    return frames


def test_native_mediamtx_records_reconnects_and_restores_playback(
    tmp_path, record_testsuite_property
) -> None:
    mediamtx, ffmpeg, ffprobe = (_tool(name) for name in _TOOLS)
    version = subprocess.run(
        [str(mediamtx), "--version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=5,
        check=False,
    )
    assert version.returncode == 0 and version.stdout.strip() == MEDIAMTX_VERSION.encode(), (
        "Native donor executable must match the pinned MediaMTX version"
    )
    directory = tmp_path / "native donor fixture with spaces"
    directory.mkdir(mode=0o700)
    recording_directory = directory / "recordings with spaces"
    recording_directory.mkdir(mode=0o700)
    video = directory / "generated moving fixture.mp4"
    generated = subprocess.run(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=15",
            "-t",
            str(_GENERATED_SECONDS),
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-pix_fmt",
            "yuv420p",
            "-g",
            "15",
            "-keyint_min",
            "15",
            "-bf",
            "0",
            str(video),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=20,
        check=False,
    )
    assert generated.returncode == 0, "Generated H264 fixture requires a working libx264 encoder"
    assert _decoded_frames(ffprobe, video) == _GENERATED_SECONDS * 15

    binding = MediaMTXBinding("native-donor-fixture", uuid4())
    device = Device(
        id=binding.device_id,
        name="Generated local fixture",
        host="127.0.0.1",
        protocols={DeviceProtocol.RTSP},
    )
    credentials = MediaMTXCredentials("fixture-worker", secrets.token_urlsafe(48))
    # Four target listeners plus the upstream fixture's RTSP listener.
    reservations, ports = _reserve_ports(5)
    upstream_port, api_port, playback_port, rtsp_port, hls_port = ports
    source = MediaMTXSource(binding, device, f"rtsp://127.0.0.1:{upstream_port}/generated")
    upstream_config = directory / "upstream fixture config.json"
    target_config = directory / "private target config.json"
    processes: list[subprocess.Popen[bytes]] = []
    receipt: dict[str, object] = {
        "platform": platform.system(),
        "native_windows_execution": sys.platform == "win32",
        "version": MEDIAMTX_VERSION,
        "media": "locally_generated_h264",
        "generated_seconds": _GENERATED_SECONDS,
        "production_camera_acceptance": False,
        "analytics_acceptance": False,
        "browser_acceptance": False,
        "time_zone_restart_regression": False,
    }
    try:
        _private_write(
            upstream_config,
            json.dumps(
                {
                    "logLevel": "warn",
                    "authInternalUsers": [
                        {
                            "user": "any",
                            "ips": ["127.0.0.1", "::1"],
                            "permissions": [
                                {"action": "publish", "path": "generated"},
                                {"action": "read", "path": "generated"},
                            ],
                        }
                    ],
                    "rtspAddress": f"127.0.0.1:{upstream_port}",
                    "rtspTransports": ["tcp"],
                    "api": False,
                    "playback": False,
                    "hls": False,
                    "webrtc": False,
                    "rtmp": False,
                    "srt": False,
                    "moq": False,
                    "metrics": False,
                    "pprof": False,
                    "paths": {"generated": {"source": "publisher"}},
                }
            ),
        )
        _private_write(
            target_config,
            generate_mediamtx_configuration(
                site_id=binding.site_id,
                sources=[source],
                credentials=credentials,
                recording_directory=str(recording_directory),
                api_port=api_port,
                playback_port=playback_port,
                rtsp_port=rtsp_port,
                hls_port=hls_port,
                segment_seconds=3,
                retention_seconds=3600,
            ),
        )
        for listener in reservations:
            listener.close()
        upstream = _start([str(mediamtx), str(upstream_config)], directory)
        processes.append(upstream)
        target_env = {**os.environ, "TZ": "UTC"} if sys.platform == "linux" else None
        target = _start([str(mediamtx), str(target_config)], directory, env=target_env)
        processes.append(target)
        api_origin = f"http://127.0.0.1:{api_port}"
        playback_origin = f"http://127.0.0.1:{playback_port}"
        provider = MediaMTXProvider(
            site_id=binding.site_id,
            api_origin=api_origin,
            playback_origin=playback_origin,
            credentials=credentials,
        )

        def publish() -> subprocess.Popen[bytes]:
            process = _start(
                [
                    str(ffmpeg),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-re",
                    "-i",
                    str(video),
                    "-c",
                    "copy",
                    "-f",
                    "rtsp",
                    "-rtsp_transport",
                    "tcp",
                    source.source_uri,
                ],
                directory,
            )
            processes.append(process)
            return process

        async def scenario() -> None:
            nonlocal target
            await _eventually(
                lambda: _listeners_ready((upstream_port, api_port, playback_port)),
                label="Private worker listeners",
                processes=(upstream, target),
            )
            initial_info = await _eventually(
                provider.info, label="Target version admission", processes=(upstream, target)
            )
            configuration = await provider.path_configuration(binding)
            assert configuration.device_id == device.id
            assert configuration.recording_enabled and not configuration.source_on_demand
            assert configuration.recording_format == "fmp4"
            _anonymous_unauthorized(api_origin + "/v3/info")
            _anonymous_unauthorized(
                playback_origin + "/list?" + urlencode({"path": binding.path_name})
            )
            receipt["anonymous_api_status"] = receipt["anonymous_playback_status"] = 401

            async def available() -> bool:
                state = await provider.path_state(binding)
                assert state.device_id == device.id
                assert state.reader_count == 0, "Recording must not depend on a viewer"
                if state.available:
                    assert state.video_codecs == (VideoCodec.H264,)
                    return True
                return False

            async def unavailable() -> bool:
                return not (await provider.path_state(binding)).available

            first = publish()
            await _eventually(
                available, label="Continuous RTSP pull", processes=(upstream, target, first)
            )
            await _publisher_finished(first)
            await _eventually(
                unavailable, label="Source interruption", processes=(upstream, target)
            )
            await asyncio.sleep(5.1)
            second = publish()
            await _eventually(
                available, label="Automatic RTSP reconnect", processes=(upstream, target, second)
            )
            receipt["source_reconnected"] = True
            await _publisher_finished(second)
            await _eventually(
                unavailable, label="Recording finalization", processes=(upstream, target)
            )
            await asyncio.sleep(0.3)
            recordings = await provider.recordings(binding)
            spans = await provider.playback_spans(binding)
            assert len(recordings) >= 3
            assert len(spans) == 2, "Playback must preserve the source outage gap"
            assert spans[1].started_at > spans[0].started_at + timedelta(
                seconds=spans[0].duration_seconds + 1
            )
            assert all(item.device_id == device.id for item in (*recordings, *spans))
            files = sorted(recording_directory.rglob("*.mp4"))
            assert len(files) == len(recordings)
            segment_frames = [_decoded_frames(ffprobe, path) for path in files]
            receipt["independently_decoded_segment_frames"] = segment_frames
            receipt["recording_segments"] = len(recordings)
            receipt["playback_spans"] = len(spans)

            # Source has ended before termination, including on Windows where
            # Popen.terminate uses TerminateProcess rather than a Unix signal.
            _stop_owned(target)
            # Exercise process-local timezone changes on Linux. This never
            # changes the Windows OS timezone or claims Windows TZ acceptance.
            restart_env = {**os.environ, "TZ": "Europe/Berlin"} if sys.platform == "linux" else None
            target = _start([str(mediamtx), str(target_config)], directory, env=restart_env)
            processes.append(target)
            restarted_info = await _eventually(
                provider.info, label="Restart version admission", processes=(upstream, target)
            )
            assert restarted_info.started_at > initial_info.started_at
            assert await provider.recordings(binding) == recordings
            assert await provider.playback_spans(binding) == spans
            assert sorted(recording_directory.rglob("*.mp4")) == files
            receipt["restart_inventory_preserved"] = True
            receipt["time_zone_restart_regression"] = sys.platform == "linux"
            decoded = []
            for index, span in enumerate(spans):
                destination = directory / f"authenticated playback {index}.mp4"
                await asyncio.to_thread(
                    _download, playback_origin, binding, credentials, span, destination
                )
                decoded.append(_decoded_frames(ffprobe, destination))
            receipt["playback_decoded_frames_after_restart"] = decoded

        asyncio.run(scenario())
        # This receipt deliberately omits credentials, argv, URLs and private paths.
        document = json.dumps(receipt, sort_keys=True)
        assert credentials.password not in document and "rtsp://" not in document
        _private_write(directory / "source-free receipt.json", document + "\n")
        record_testsuite_property("native_donor_runtime_receipt", document)
    finally:
        for listener in reservations:
            listener.close()
        cleanup_failures = 0
        for process in reversed(processes):
            try:
                _stop_owned(process)
            except (OSError, subprocess.TimeoutExpired):
                cleanup_failures += 1
        assert cleanup_failures == 0, "Owned native fixture processes could not all be reaped"
