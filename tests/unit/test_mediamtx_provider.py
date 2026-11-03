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
    ids=["version-mismatch", "naive-time", "duplicate-key", "non-finite", "oversized-body"],
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
            pending = tuple(provider._requests)
            assert len(pending) == 1 and not pending[0].done()
        finally:
            released.set()
        # Release acknowledges only the thread's input event. Join its actual
        # completion before asserting the capacity slot is reusable on Windows.
        await asyncio.wait_for(asyncio.shield(pending[0]), timeout=2)
        assert not provider._requests
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
    assert path["recordFormat"] == "fmp4" and path["recordDeleteAfter"] == "0s"
    assert path["useAbsoluteTimestamp"] is False
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


@pytest.mark.parametrize("retention", [1, 3600, 86400, True, -1])
def test_configuration_cannot_reenable_native_wall_clock_age_deletion(retention) -> None:
    with pytest.raises(ValueError):
        generate_mediamtx_configuration(
            site_id="poc-site",
            sources=[_source()],
            credentials=_CREDENTIALS,
            recording_directory=r"C:\Users\Owner\K5Recordings",
            retention_seconds=retention,
        )


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


def _state_item(binding=_BINDING, **changes):
    return {
        "name": binding.path_name,
        "available": True,
        "online": True,
        "tracks2": [{"codec": "H264"}],
        "readers": [{"id": "private-reader"}],
        "inboundBytes": 123,
        "outboundBytes": 456,
        "source": {"id": "rtsp://camera:private-password@127.0.0.1/live"},
        **changes,
    }


def _configuration_item(binding=_BINDING, **changes):
    return {
        "name": binding.path_name,
        "record": True,
        "recordFormat": "fmp4",
        "sourceOnDemand": False,
        "source": "rtsp://camera:private-password@127.0.0.1/live",
        "recordPath": "C:/private-recordings",
        **changes,
    }


def _page(items):
    return {"items": items, "itemCount": len(items), "pageCount": 1 if items else 0}


def test_snapshot_reads_64_selected_paths_with_two_fixed_sequential_requests():
    bindings = tuple(MediaMTXBinding("poc-site", UUID(int=index)) for index in range(64))
    routes = []

    def request(request, *_):
        route = urlsplit(request.full_url)
        routes.append(route.path + ("?" + route.query if route.query else ""))
        if route.path == "/v3/info":
            return _info()
        if route.path == "/v3/paths/list":
            return json.dumps(_page([_state_item(binding) for binding in bindings])).encode()
        assert route.path == "/v3/config/paths/list"
        return json.dumps(_page([_configuration_item(binding) for binding in bindings])).encode()

    async def scenario():
        provider = _provider(request, max_active_requests=1)
        snapshot = await provider.path_snapshot(bindings)
        assert set(snapshot) == set(bindings)
        for binding, (state, configuration) in snapshot.items():
            assert state.device_id == configuration.device_id == binding.device_id
            assert state.online and state.video_codecs == (VideoCodec.H264,)
            assert state.reader_count == 1 and configuration.recording_enabled
        assert "private" not in repr(snapshot) and "rtsp://" not in repr(snapshot)

    asyncio.run(scenario())
    assert routes == [
        "/v3/info",
        "/v3/paths/list?itemsPerPage=64&page=0",
        "/v3/config/paths/list?itemsPerPage=64&page=0",
    ]


def test_snapshot_isolates_missing_disconnected_and_malformed_selected_paths():
    bindings = tuple(MediaMTXBinding("poc-site", UUID(int=index)) for index in range(6))
    foreign = MediaMTXBinding("another-site", UUID(int=99))
    states = [
        _state_item(bindings[0]),
        _state_item(bindings[1], available=False, online=False),
        _state_item(bindings[2], inboundBytes=True),
        _state_item(bindings[4]),
        _state_item(bindings[5]),
        {"name": foreign.path_name, "private-unknown-source": "must-not-return"},
    ]
    configurations = [
        _configuration_item(binding, **({"record": "yes"} if index == 5 else {}))
        for index, binding in enumerate(bindings)
        if index != 4
    ] + [{"name": foreign.path_name, "source": "rtsp://private-unselected"}]

    def request(request, *_):
        route = urlsplit(request.full_url).path
        if route == "/v3/info":
            return _info()
        return json.dumps(_page(states if route == "/v3/paths/list" else configurations)).encode()

    async def scenario():
        result = await _provider(request).path_snapshot(bindings)
        assert result[bindings[0]][0].online is True
        assert result[bindings[1]][0].online is False
        assert result[bindings[1]][1].recording_enabled is True
        assert result[bindings[2]] is MediaWorkerErrorCode.INVALID_RESPONSE
        assert result[bindings[3]] is MediaWorkerErrorCode.NOT_FOUND
        assert result[bindings[4]] is MediaWorkerErrorCode.NOT_FOUND
        assert result[bindings[5]] is MediaWorkerErrorCode.INVALID_RESPONSE
        assert set(result) == set(bindings) and foreign not in result
        assert "private" not in repr(result) and "must-not-return" not in repr(result)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "page",
    [
        {"items": None, "itemCount": 0, "pageCount": 0},
        {**_page([_state_item()]), "itemCount": True},
        {**_page([_state_item()]), "itemCount": 2},
        {**_page([_state_item()]), "pageCount": 0},
        {**_page([_state_item()]), "pageCount": 2},
        _page([_state_item(), _state_item()]),
        _page([_state_item(name="rtsp://private-password@camera")]),
        _page([None]),
        _page([_state_item(MediaMTXBinding("poc-site", UUID(int=i))) for i in range(65)]),
    ],
)
def test_snapshot_refuses_inconsistent_oversized_or_ambiguous_list_envelopes(page):
    def request(request, *_):
        return _info() if request.full_url.endswith("/v3/info") else json.dumps(page).encode()

    async def scenario():
        assert await _provider(request).path_snapshot((_BINDING,)) == {
            _BINDING: MediaWorkerErrorCode.INVALID_RESPONSE
        }

    asyncio.run(scenario())


def test_snapshot_empty_worker_lists_report_missing_selections():
    def request(request, *_):
        return _info() if request.full_url.endswith("/v3/info") else json.dumps(_page([])).encode()

    async def scenario():
        assert await _provider(request).path_snapshot((_BINDING,)) == {
            _BINDING: MediaWorkerErrorCode.NOT_FOUND
        }

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["state", "configuration"])
def test_snapshot_and_individual_getters_share_selected_field_validation(operation):
    state = _state_item(inboundBytes=True) if operation == "state" else _state_item()
    configuration = (
        _configuration_item(record=1) if operation == "configuration" else _configuration_item()
    )

    def request(request, *_):
        route = urlsplit(request.full_url).path
        if route == "/v3/info":
            return _info()
        payload = configuration if route.startswith("/v3/config/") else state
        return json.dumps(_page([payload]) if route.endswith("/list") else payload).encode()

    async def scenario():
        provider = _provider(request)
        getter = provider.path_state if operation == "state" else provider.path_configuration
        with pytest.raises(MediaWorkerError) as raised:
            await getter(_BINDING)
        assert raised.value.code is MediaWorkerErrorCode.INVALID_RESPONSE
        assert await provider.path_snapshot((_BINDING,)) == {
            _BINDING: MediaWorkerErrorCode.INVALID_RESPONSE
        }

    asyncio.run(scenario())


def test_snapshot_rejects_invalid_selections_before_network_and_skips_empty_selection():
    async def scenario():
        provider = _provider(lambda *_: pytest.fail("Invalid selection must not start HTTP"))
        assert await provider.path_snapshot(()) == {}
        for selected in (
            [_BINDING],
            (_BINDING, _BINDING),
            tuple(MediaMTXBinding("poc-site", UUID(int=i)) for i in range(65)),
            (MediaMTXBinding("foreign-site", _DEVICE),),
        ):
            with pytest.raises(ValueError):
                await provider.path_snapshot(selected)

    asyncio.run(scenario())


def test_snapshot_shares_inflight_reads_and_cancellation_preserves_native_slot():
    released = threading.Event()
    bindings = tuple(MediaMTXBinding("poc-site", UUID(int=i)) for i in range(4))
    routes = []

    async def scenario():
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()

        def request(request, *_):
            route = urlsplit(request.full_url).path
            routes.append(route)
            if route == "/v3/info":
                return _info()
            if route == "/v3/paths/list":
                loop.call_soon_threadsafe(entered.set)
                assert released.wait(5)
                items = [_state_item(binding) for binding in bindings]
            else:
                assert route == "/v3/config/paths/list"
                items = [_configuration_item(binding) for binding in bindings]
            return json.dumps(_page(items)).encode()

        provider = _provider(request, max_active_requests=1)
        first = asyncio.create_task(provider.path_snapshot(bindings))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            second = asyncio.create_task(provider.path_snapshot(tuple(reversed(bindings))))
            cancelled = asyncio.create_task(provider.path_snapshot(bindings))
            await asyncio.sleep(0)
            cancelled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled
            assert len(provider._requests) == 1 and len(provider._snapshots) == 1
            assert not first.done() and not second.done()
            busy = await provider.path_snapshot((_BINDING,))
            assert busy == {_BINDING: MediaWorkerErrorCode.BUSY}
        finally:
            released.set()
        one, two = await asyncio.gather(first, second)
        assert all(isinstance(value, tuple) for value in one.values())
        assert one == two and one is not two
        assert routes == ["/v3/info", "/v3/paths/list", "/v3/config/paths/list"]
        one.clear()
        assert len(two) == 4
        await provider.path_snapshot(bindings)
        assert routes[-2:] == ["/v3/paths/list", "/v3/config/paths/list"]
        assert len(routes) == 5  # Only in-flight reads are shared; no stale cache.

    asyncio.run(scenario())


def test_close_joins_native_read_after_sole_snapshot_waiter_cancels_without_late_second_get():
    released = threading.Event()
    routes = []

    async def scenario():
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()

        def request(request, *_):
            route = urlsplit(request.full_url).path
            routes.append(route)
            if route == "/v3/info":
                return _info()
            assert route == "/v3/paths/list", "Closed provider issued a late configuration GET"
            loop.call_soon_threadsafe(entered.set)
            assert released.wait(5)
            return json.dumps(_page([_state_item()])).encode()

        provider = _provider(request, max_active_requests=1)
        owner = asyncio.create_task(provider.path_snapshot((_BINDING,)))
        closing = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            owner.cancel()
            with pytest.raises(asyncio.CancelledError):
                await owner
            closing = asyncio.create_task(provider.aclose())
            for _ in range(3):
                await asyncio.sleep(0)
            assert not closing.done() and len(provider._requests) == 1
            assert await provider.path_snapshot((_BINDING,)) == {
                _BINDING: MediaWorkerErrorCode.UNAVAILABLE
            }
            with pytest.raises(MediaWorkerError) as raised:
                await provider.path_state(_BINDING)
            assert raised.value.code is MediaWorkerErrorCode.UNAVAILABLE
            closing.cancel()
            await asyncio.sleep(0)
            assert not closing.done() and len(provider._requests) == 1
        finally:
            released.set()
        assert closing is not None
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(closing), 2)
        await provider.aclose()
        assert not provider._requests and not provider._snapshots
        assert routes == ["/v3/info", "/v3/paths/list"]

    asyncio.run(scenario())
