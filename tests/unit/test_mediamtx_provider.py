from __future__ import annotations

import asyncio
import base64
import json
import threading
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest

from k5vision.domain.devices import Device, DeviceProtocol
from k5vision.domain.streams import VideoCodec
from k5vision.media import mediamtx_provider
from k5vision.media.mediamtx_config import MediaMTXSource, generate_mediamtx_configuration
from k5vision.media.mediamtx_provider import (
    MEDIAMTX_VERSION,
    MediaMTXBinding,
    MediaMTXCredentials,
    MediaMTXProvider,
    MediaWorkerError,
    MediaWorkerErrorCode,
)

_DEVICE = UUID("0886d9dd-5e9b-488e-b806-1b93e0b98873")
_BINDING = MediaMTXBinding("poc-site", _DEVICE)
_CREDENTIALS = MediaMTXCredentials("k5worker", "x" * 43)
_START = "2026-10-11T12:00:00Z"


def _provider(requester, **kwargs) -> MediaMTXProvider:
    return MediaMTXProvider(
        site_id="poc-site",
        api_origin="http://127.0.0.1:9997",
        playback_origin="http://127.0.0.1:9996",
        credentials=_CREDENTIALS,
        requester=requester,
        **kwargs,
    )


def _info() -> bytes:
    return json.dumps({"version": MEDIAMTX_VERSION, "started": _START}).encode()


def test_binding_is_stable_and_site_device_stream_scoped() -> None:
    assert _BINDING.path_name == MediaMTXBinding("poc-site", _DEVICE).path_name
    assert _BINDING.path_name != MediaMTXBinding("other-site", _DEVICE).path_name
    assert _BINDING.path_name != MediaMTXBinding("poc-site", UUID(int=1)).path_name
    assert _BINDING.path_name != MediaMTXBinding("poc-site", _DEVICE, "sub").path_name
    assert "poc-site" not in _BINDING.path_name
    assert len(_BINDING.path_name) == 67
    for stream in ("../camera", "bad/token", "", "x" * 257):
        with pytest.raises(ValueError):
            MediaMTXBinding("poc-site", _DEVICE, stream)


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:9997",
        "http://192.0.2.1:9997",
        "http://user:secret@127.0.0.1:9997",
        "http://127.0.0.1:9997/api",
        "http://127.0.0.1:9997/?secret=yes",
        "http://127.0.0.1",
        "\nhttp://127.0.0.1:9997",
    ],
)
def test_origin_rejects_remote_ambiguous_or_secret_bearing_urls(origin: str) -> None:
    with pytest.raises(ValueError):
        MediaMTXProvider(
            site_id="poc-site",
            api_origin=origin,
            playback_origin="http://127.0.0.1:9996",
            credentials=_CREDENTIALS,
        )


def test_fixed_get_routes_normalize_and_do_not_return_source_material() -> None:
    requests = []

    def request(request, timeout, max_bytes):
        requests.append(request)
        assert timeout == 3
        assert max_bytes == mediamtx_provider.MAX_RESPONSE_BYTES
        route = urlsplit(request.full_url).path
        if route == "/v3/info":
            return _info()
        if route.startswith("/v3/paths/get/"):
            payload = {
                "name": _BINDING.path_name,
                "available": True,
                "online": True,
                "tracks2": [{"codec": "H264"}, {"codec": "MPEG4Audio"}],
                "readers": [{"id": "private-session"}],
                "inboundBytes": 123,
                "outboundBytes": 456,
                "source": {"id": "rtsp://user:secret@127.0.0.1/live"},
            }
        elif route.startswith("/v3/config/paths/get/"):
            payload = {
                "record": True,
                "recordFormat": "fmp4",
                "sourceOnDemand": False,
                "source": "rtsp://user:secret@127.0.0.1/live",
                "recordPath": "C:/private/recordings",
            }
        elif route.startswith("/v3/recordings/get/"):
            payload = {
                "name": _BINDING.path_name,
                "segments": [{"start": _START}, {"start": "2026-10-11T14:00:00+02:00"}],
            }
        elif route == "/list":
            query = parse_qs(urlsplit(request.full_url).query)
            assert query["path"] == [_BINDING.path_name]
            payload = [{"start": _START, "duration": 12.5, "url": "http://secret/path"}]
        else:
            pytest.fail("unexpected donor endpoint")
        return json.dumps(payload).encode()

    async def scenario():
        provider = _provider(request)
        info = await provider.info()
        state = await provider.path_state(_BINDING)
        configuration = await provider.path_configuration(_BINDING)
        recordings = await provider.recordings(_BINDING)
        spans = await provider.playback_spans(_BINDING)
        assert info.version == MEDIAMTX_VERSION
        assert state.video_codecs == (VideoCodec.H264,)
        assert state.reader_count == 1
        assert configuration.recording_enabled and not configuration.source_on_demand
        assert len(recordings) == 1
        assert recordings[0].started_at == datetime(2026, 10, 11, 12, tzinfo=UTC)
        assert spans[0].recording_id == recordings[0].recording_id
        assert spans[0].duration_seconds == 12.5
        retained = repr((info, state, configuration, recordings, spans, provider, _CREDENTIALS))
        assert all(secret not in retained for secret in ("secret", "private", "x" * 43, "rtsp://"))

    asyncio.run(scenario())
    expected = "Basic " + base64.b64encode(b"k5worker:" + b"x" * 43).decode()
    assert all(request.get_method() == "GET" for request in requests)
    assert all(request.get_header("Authorization") == expected for request in requests)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            b'{"version":"v9","started":"2026-10-11T12:00:00Z"}',
            MediaWorkerErrorCode.VERSION_MISMATCH,
        ),
        (
            b'{"version":"v1.21.2","started":"2026-10-11T12:00:00"}',
            MediaWorkerErrorCode.INVALID_RESPONSE,
        ),
        (b'{"version":"v1.21.2","version":"v9"}', MediaWorkerErrorCode.INVALID_RESPONSE),
        (b'{"version":"v1.21.2","started":NaN}', MediaWorkerErrorCode.INVALID_RESPONSE),
        (b"x" * 262_145, MediaWorkerErrorCode.INVALID_RESPONSE),
    ],
)
def test_version_and_response_admission_fail_closed(body, expected) -> None:
    async def scenario():
        with pytest.raises(MediaWorkerError) as raised:
            await _provider(lambda *_: body).info()
        assert raised.value.code is expected
        assert raised.value.__context__ is None

    asyncio.run(scenario())


def test_unknown_site_and_wrong_returned_path_do_not_cross_identity_boundary() -> None:
    routes = []

    def request(request, *_):
        routes.append(request.full_url)
        if request.full_url.endswith("/v3/info"):
            return _info()
        return json.dumps({"name": MediaMTXBinding("other-site", _DEVICE).path_name}).encode()

    async def scenario():
        provider = _provider(request)
        with pytest.raises(ValueError):
            await provider.path_state(MediaMTXBinding("other-site", _DEVICE))
        assert not routes
        with pytest.raises(MediaWorkerError) as raised:
            await provider.path_state(_BINDING)
        assert raised.value.code is MediaWorkerErrorCode.INVALID_RESPONSE

    asyncio.run(scenario())


def test_failed_explicit_version_check_revokes_previous_admission() -> None:
    version = MEDIAMTX_VERSION
    routes = []

    def request(request, *_):
        routes.append(request.full_url)
        return json.dumps({"version": version, "started": _START}).encode()

    async def scenario():
        nonlocal version
        provider = _provider(request)
        await provider.info()
        version = "v9"
        with pytest.raises(MediaWorkerError):
            await provider.info()
        with pytest.raises(MediaWorkerError) as raised:
            await provider.path_state(_BINDING)
        assert raised.value.code is MediaWorkerErrorCode.VERSION_MISMATCH
        assert all(route.endswith("/v3/info") for route in routes)

    asyncio.run(scenario())


@pytest.mark.parametrize("field", ["tracks2", "readers"])
@pytest.mark.parametrize("malformed", [False, 0, {}, ""])
def test_malformed_empty_path_collections_are_not_coerced_to_lists(field, malformed) -> None:
    def request(request, *_):
        if request.full_url.endswith("/v3/info"):
            return _info()
        payload = {
            "name": _BINDING.path_name,
            "available": False,
            "online": False,
            "tracks2": None,
            "readers": None,
            "inboundBytes": 0,
            "outboundBytes": 0,
            field: malformed,
        }
        return json.dumps(payload).encode()

    async def scenario():
        with pytest.raises(MediaWorkerError) as raised:
            await _provider(request).path_state(_BINDING)
        assert raised.value.code is MediaWorkerErrorCode.INVALID_RESPONSE

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "status,expected", [(401, "unauthorized"), (404, "not_found"), (302, "unavailable")]
)
def test_http_failures_are_sanitized_without_retaining_raw_errors(status, expected) -> None:
    def request(request, *_):
        raise HTTPError(request.full_url, status, "private-source-secret", {}, None)

    async def scenario():
        with pytest.raises(MediaWorkerError) as raised:
            await _provider(request).info()
        assert raised.value.code.value == expected
        assert "secret" not in repr(raised.value)
        assert raised.value.__context__ is None

    asyncio.run(scenario())


def test_network_failure_and_missing_recordings_are_explicit() -> None:
    def unavailable(*_):
        raise URLError("rtsp://user:secret@example.invalid")

    def missing(request, *_):
        if request.full_url.endswith("/v3/info"):
            return _info()
        raise HTTPError(request.full_url, 404, "private", {}, None)

    async def scenario():
        with pytest.raises(MediaWorkerError) as raised:
            await _provider(unavailable).info()
        assert raised.value.code is MediaWorkerErrorCode.UNAVAILABLE
        provider = _provider(missing)
        assert await provider.recordings(_BINDING) == ()
        assert await provider.playback_spans(_BINDING) == ()

    asyncio.run(scenario())


def test_timeout_keeps_worker_slot_until_real_read_completes() -> None:
    released = threading.Event()

    def stalled(*_):
        released.wait(2)
        return _info()

    async def scenario():
        provider = _provider(stalled, timeout_seconds=0.02, max_active_requests=1)
        try:
            with pytest.raises(MediaWorkerError) as raised:
                await provider.info()
            assert raised.value.code is MediaWorkerErrorCode.TIMEOUT
            with pytest.raises(MediaWorkerError) as busy:
                await provider.info()
            assert busy.value.code is MediaWorkerErrorCode.BUSY
        finally:
            released.set()
        await asyncio.sleep(0.03)
        assert (await provider.info()).version == MEDIAMTX_VERSION

    asyncio.run(scenario())


def test_default_transport_disables_proxy_and_redirects(monkeypatch) -> None:
    handlers = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self, limit):
            assert limit == 1025
            return _info()

    class Opener:
        def open(self, request, *, timeout):
            assert request.full_url == "http://127.0.0.1:9997/v3/info"
            assert timeout == 3
            return Response()

    def opener(*args):
        handlers.extend(args)
        return Opener()

    monkeypatch.setattr(mediamtx_provider, "build_opener", opener)
    request = mediamtx_provider.Request("http://127.0.0.1:9997/v3/info")
    assert mediamtx_provider._http_get(request, 3, 1024) == _info()
    assert handlers[0].proxies == {}
    assert handlers[1].redirect_request(None, None, 302, "", {}, "http://elsewhere") is None


def _source(uri="rtsp://user:private@127.0.0.1/live") -> MediaMTXSource:
    device = Device(id=_DEVICE, name="Camera", host="127.0.0.1", protocols={DeviceProtocol.RTSP})
    return MediaMTXSource(_BINDING, device, uri)


def test_configuration_is_enrolled_only_private_and_windows_compatible() -> None:
    source = _source()
    config = json.loads(
        generate_mediamtx_configuration(
            site_id="poc-site",
            sources=[source],
            credentials=_CREDENTIALS,
            recording_directory=r"C:\Users\Owner\K5Recordings",
        )
    )
    assert list(config["paths"]) == [_BINDING.path_name]
    path = config["paths"][_BINDING.path_name]
    assert path["source"] == source.source_uri
    assert path["record"] and not path["sourceOnDemand"]
    assert path["recordFormat"] == "fmp4" and path["recordDeleteAfter"] == "86400s"
    assert path["recordPath"].startswith(r"C:\Users\Owner\K5Recordings/%path/")
    assert path["recordPath"].endswith("_%z")
    assert config["authInternalUsers"][0]["user"] == "k5worker"
    assert all(
        permission["action"] != "publish"
        for permission in config["authInternalUsers"][0]["permissions"]
    )
    assert all(
        config[key].startswith("127.0.0.1:")
        for key in ("apiAddress", "playbackAddress", "hlsAddress", "rtspAddress")
    )
    assert all(config[key] is False for key in ("webrtc", "rtmp", "srt", "moq", "metrics", "pprof"))
    assert "private" not in repr(source)
    assert "x" * 43 not in repr(_CREDENTIALS)


@pytest.mark.parametrize(
    "uri",
    [
        "rtsp://192.0.2.1/live",
        "http://127.0.0.1/live",
        "rtsp://127.0.0.1/live#secret",
        "rtsp://localhost/live",
        "rtsp://127.0.0.1:0/live",
        "RTSP://127.0.0.1/live",
        " rtsp://127.0.0.1/live",
        "rtsp://127.0.0.1/live ",
        "rtsp://user:private%ZZ@127.0.0.1/live",
        "rtsp://user:private%@127.0.0.1/live",
        "rtsp://user:private\\@127.0.0.1/live",
        'rtsp://user:private"@127.0.0.1/live',
        "rtsp://user:private{@127.0.0.1/live",
        "rtsp://user:private}@127.0.0.1/live",
        "rtsp://user:private|@127.0.0.1/live",
        "rtsp://user:private^@127.0.0.1/live",
        "rtsp://user:privat\u00e9@127.0.0.1/live",
        "rtsp://user@127.0.0.1/live",
        "rtsp://:private@127.0.0.1/live",
        "rtsp://user:private@127.0.0.1/live\u007f",
    ],
)
def test_configuration_rejects_sources_outside_enrolled_endpoint(uri) -> None:
    with pytest.raises(ValueError):
        _source(uri)


@pytest.mark.parametrize(
    "uri", ["rtsp://user:private%25%40%5C%22@127.0.0.1/live", "rtsp://user:@127.0.0.1/live"]
)
def test_configuration_accepts_donor_supported_source_credentials(uri) -> None:
    source = _source(uri)
    assert source.binding == _BINDING


@pytest.mark.parametrize(
    "directory",
    ["relative", "C:\\", "C:\\a\\..\\b", "//server/share", "/", "/tmp/%path", "C:\\a:stream"],
)
def test_configuration_rejects_unsafe_recording_destinations(directory) -> None:
    with pytest.raises(ValueError):
        generate_mediamtx_configuration(
            site_id="poc-site",
            sources=[_source()],
            credentials=_CREDENTIALS,
            recording_directory=directory,
        )


def test_configuration_refuses_duplicate_foreign_and_mutated_bindings() -> None:
    source = _source()
    for site, sources in (("other-site", [source]), ("poc-site", [source, source])):
        with pytest.raises(ValueError):
            generate_mediamtx_configuration(
                site_id=site,
                sources=sources,
                credentials=_CREDENTIALS,
                recording_directory="/tmp/private",
            )
    source.device.host = "192.0.2.1"
    with pytest.raises(ValueError):
        generate_mediamtx_configuration(
            site_id="poc-site",
            sources=[source],
            credentials=_CREDENTIALS,
            recording_directory="/tmp/private",
        )


def test_playback_query_rejects_unbounded_or_naive_intervals_before_network() -> None:
    async def scenario():
        provider = _provider(lambda *_: pytest.fail("network must not start"))
        now = datetime.now(UTC)
        for start, end in ((now, now), (now, now + timedelta(days=2)), (datetime.now(), now)):
            with pytest.raises(ValueError):
                await provider.playback_spans(_BINDING, start=start, end=end)

    asyncio.run(scenario())
