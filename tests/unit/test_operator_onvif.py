"""Real loopback SOAP exchanges and bounded cancellation at the onboarding boundary."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import http.server
import threading
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import urlsplit
from urllib.request import parse_http_list, parse_keqv_list
from xml.sax.saxutils import escape

import pytest

from k5vision import operator_onvif as onvif

_USERNAME = "fixture-viewer"
_PASSWORD = "fixture-only<&>credential"


def _text(root, name):
    return next(
        (item.text or "" for item in root.iter() if item.tag.rsplit("}", 1)[-1] == name), ""
    )


def _soap(body):
    return f'<s:Envelope xmlns:s="{onvif._SOAP}"><s:Body>{body}</s:Body></s:Envelope>'.encode()


def _profiles(media2=False, count=1):
    configuration = "Configurations><VideoEncoder" if media2 else "VideoEncoderConfiguration"
    ending = "VideoEncoder></Configurations" if media2 else "VideoEncoderConfiguration"
    return (
        "<GetProfilesResponse>"
        + "".join(
            f'<Profiles token="profile-{index}"><Name>Entrance</Name><{configuration}>'
            "<Encoding>H264</Encoding><Resolution><Width>1280</Width><Height>720</Height>"
            f"</Resolution></{ending}></Profiles>"
            for index in range(count)
        )
        + "</GetProfilesResponse>"
    )


@pytest.fixture
def camera():
    settings = SimpleNamespace(
        media2=False,
        digest=False,
        count=1,
        foreign=False,
        stream=None,
        malformed=None,
        status=None,
        drip=False,
        requests=[],
        rejected=False,
        first_fault=None,
        clock_offset=None,
        clock_status=None,
        clock_body=None,
        clock_delay=0,
        clock_requests=[],
        clock_sequence=None,
        clock_mode=None,
        shift_after_action=None,
        after_action=None,
    )

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            root = ET.fromstring(body)
            action = list(next(item for item in root if item.tag.endswith("}Body")))[0].tag.rsplit(
                "}", 1
            )[-1]
            if action == "GetSystemDateAndTime":
                settings.clock_requests.append((self.path, root, self.headers.get("Authorization")))
                assert not _text(root, "Username") and not _text(root, "Password")
                assert self.headers.get("Authorization") is None
                time.sleep(settings.clock_delay)
                offset = settings.clock_offset
                if settings.clock_sequence is not None:
                    index = min(len(settings.clock_requests) - 1, len(settings.clock_sequence) - 1)
                    offset = settings.clock_sequence[index]
                when = datetime.now(UTC) + timedelta(seconds=offset or 0)
                response = settings.clock_body
                if response is None:
                    mode = (
                        f"<DateTimeType>{escape(settings.clock_mode)}</DateTimeType>"
                        if settings.clock_mode is not None
                        else ""
                    )
                    response = _soap(
                        f"<GetSystemDateAndTimeResponse><SystemDateAndTime>{mode}<UTCDateTime>"
                        f"<Date><Year>{when.year}</Year><Month>{when.month}</Month>"
                        f"<Day>{when.day}</Day></Date><Time><Hour>{when.hour}</Hour>"
                        f"<Minute>{when.minute}</Minute><Second>{when.second}</Second>"
                        "</Time></UTCDateTime></SystemDateAndTime></GetSystemDateAndTimeResponse>"
                    )
                status = settings.clock_status
                if status is None:
                    status = 200 if offset is not None else 404
                self.send_response(status)
                if status == 401:
                    self.send_header("WWW-Authenticate", 'Digest realm="clock", nonce="private"')
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                try:
                    self.wfile.write(response)
                except OSError:
                    pass
                return
            settings.requests.append((self.path, action, root, self.headers.get("Authorization")))
            if settings.digest:
                authorization = self.headers.get("Authorization", "")
                if not authorization:
                    self.send_response(401)
                    self.send_header(
                        "WWW-Authenticate",
                        'Digest realm="fixture", nonce="nonce", qop="auth", algorithm=MD5',
                    )
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                values = parse_keqv_list(parse_http_list(authorization.removeprefix("Digest ")))
                a1 = hashlib.md5(
                    f"{_USERNAME}:fixture:{_PASSWORD}".encode(), usedforsecurity=False
                ).hexdigest()
                a2 = hashlib.md5(f"POST:{self.path}".encode(), usedforsecurity=False).hexdigest()
                expected = hashlib.md5(
                    f"{a1}:nonce:{values['nc']}:{values['cnonce']}:auth:{a2}".encode(),
                    usedforsecurity=False,
                ).hexdigest()
                assert values["username"] == _USERNAME and hmac.compare_digest(
                    values["response"], expected
                )
            nonce = base64.b64decode(_text(root, "Nonce"))
            created = _text(root, "Created")
            expected = base64.b64encode(
                hashlib.sha1(
                    nonce + created.encode() + _PASSWORD.encode(), usedforsecurity=False
                ).digest()
            ).decode()
            valid = _text(root, "Username") == _USERNAME and hmac.compare_digest(
                _text(root, "Password"), expected
            )
            if settings.clock_offset is not None:
                when = datetime.fromisoformat(created.replace("Z", "+00:00"))
                camera_now = datetime.now(UTC) + timedelta(seconds=settings.clock_offset)
                valid = valid and abs((camera_now - when).total_seconds()) < 120
            status = settings.status or (200 if valid else 401)
            if not valid:
                settings.rejected = True
            if settings.malformed is not None:
                response = settings.malformed
            elif action == "GetCapabilities":
                host = "192.0.2.200" if settings.foreign else "127.0.0.1"
                response = _soap(
                    f"<GetCapabilitiesResponse><Capabilities><Media><XAddr>http://{host}:{self.server.server_port}/media</XAddr></Media></Capabilities></GetCapabilitiesResponse>"
                )
            elif action == "GetServices":
                response = _soap(
                    f"<GetServicesResponse><Service><Namespace>{onvif._MEDIA2}</Namespace><XAddr>http://127.0.0.1:{self.server.server_port}/media2</XAddr></Service></GetServicesResponse>"
                )
            elif action == "GetProfiles" and settings.media2 and self.path != "/media2":
                status = 500
                response = _soap("<Fault><Code><Value>ActionNotSupported</Value></Code></Fault>")
            elif action == "GetProfiles":
                profiles = _profiles(settings.media2, settings.count)
                if settings.first_fault:
                    profiles = profiles.replace(
                        "<Encoding>H264</Encoding>", "<Encoding>H265</Encoding>", 1
                    )
                response = _soap(profiles)
            elif action == "GetStreamUri":
                uri = settings.stream or "rtsp://127.0.0.1:8554/live?channel=1&auth=fixture%2Ftoken"
                response = _soap(
                    f"<GetStreamUriResponse><MediaUri><Uri>{escape(uri)}</Uri></MediaUri></GetStreamUriResponse>"
                )
                if settings.first_fault and _text(root, "ProfileToken") == "profile-0":
                    status = 500
                    response = _soap(
                        f"<Fault><Code><Value>{settings.first_fault}</Value></Code></Fault>"
                    )
            else:
                raise AssertionError("Unexpected SOAP action")
            if settings.shift_after_action and action == settings.shift_after_action[0]:
                settings.clock_offset = settings.shift_after_action[1]
            if settings.after_action is not None:
                settings.after_action(action)
            self.send_response(status)
            self.send_header("Content-Length", str(len(response) if not settings.drip else 1000000))
            self.end_headers()
            try:
                if settings.drip:
                    for _ in range(100):
                        self.wfile.write(b"x")
                        self.wfile.flush()
                        time.sleep(0.02)
                else:
                    self.wfile.write(response)
            except OSError:
                pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings.port = server.server_port
    try:
        yield settings
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _probe(camera, **kwargs):
    arguments = dict(
        host="127.0.0.1", port=camera.port, secure=False, username=_USERNAME, password=_PASSWORD
    )
    arguments.update(kwargs)
    return asyncio.run(onvif.OnvifConnector().probe(**arguments))


def test_real_wsse_soap_profile_preserves_private_query_and_escaped_credentials(
    camera, monkeypatch
):
    monkeypatch.setattr(onvif.socket, "getaddrinfo", lambda *args: pytest.fail("DNS is forbidden"))
    monkeypatch.setenv("HTTP_PROXY", "http://192.0.2.100:9")
    (profile,) = _probe(camera)
    assert (profile.token, profile.codec, profile.width, profile.height) == (
        "profile-0",
        "H264",
        1280,
        720,
    )
    assert profile.source_uri.endswith("/live?channel=1&auth=fixture%2Ftoken")
    parsed = urlsplit(profile.source_uri)
    assert parsed.username == _USERNAME
    assert "fixture%2D" not in parsed.netloc
    assert "fixture-only%3C%26%3Ecredential" in parsed.netloc
    assert _PASSWORD not in repr(profile) and "fixture%2Ftoken" not in repr(profile)
    assert [action for _, action, _, _ in camera.requests] == [
        "GetCapabilities",
        "GetProfiles",
        "GetStreamUri",
    ]


def test_real_http_digest_authentication_and_wsse(camera):
    camera.digest = True
    profiles = _probe(camera)
    assert len(profiles) == 1
    assert camera.requests[0][3] is None
    assert all(authorization.startswith("Digest ") for *_, authorization in camera.requests[1:])


@pytest.mark.parametrize("offset", [-21600, 21600])
def test_real_camera_clock_skew_is_compensated_without_sending_credentials(camera, offset):
    camera.clock_offset = offset
    (profile,) = _probe(camera)
    assert profile.codec == "H264"
    assert len(camera.clock_requests) == 1
    path, root, authorization = camera.clock_requests[0]
    assert path == "/onvif/device_service"
    assert authorization is None
    assert _find_security(root) is None
    assert all(
        action in {"GetCapabilities", "GetProfiles", "GetStreamUri"}
        for _, action, _, _ in camera.requests
    )
    for _, _, document, _ in camera.requests:
        created = datetime.fromisoformat(_text(document, "Created").replace("Z", "+00:00"))
        assert abs((created - datetime.now(UTC)).total_seconds() - offset) < 3


def _find_security(root):
    return next((item for item in root.iter() if item.tag.rsplit("}", 1)[-1] == "Security"), None)


def test_camera_clock_compensation_does_not_admit_bad_credentials(camera):
    camera.clock_offset = 21600
    with pytest.raises(onvif.OnvifError) as caught:
        _probe(camera, password="wrong-fixture-only-password")
    assert caught.value.code == "auth_failed"
    assert len(camera.clock_requests) == 2 and camera.rejected
    assert len(camera.requests) == 1


@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_unavailable_camera_clock_is_optional_and_never_retries_with_credentials(camera, status):
    camera.clock_status = status
    assert len(_probe(camera)) == 1
    assert len(camera.clock_requests) == 1
    assert all(authorization is None for _, _, authorization in camera.clock_requests)


@pytest.mark.parametrize(
    "body",
    [
        b"<broken",
        _soap(
            "<GetSystemDateAndTimeResponse><SystemDateAndTime><LocalDateTime/></SystemDateAndTime></GetSystemDateAndTimeResponse>"
        ),
        _soap(
            "<GetSystemDateAndTimeResponse><UTCDateTime><Date><Year>9999999999999999999</Year><Month>13</Month><Day>40</Day></Date><Time><Hour>25</Hour><Minute>80</Minute><Second>99</Second></Time></UTCDateTime></GetSystemDateAndTimeResponse>"
        ),
        b'<!DOCTYPE x [<!ENTITY x "private">]><x>&x;</x>',
    ],
)
def test_malformed_camera_clock_falls_back_to_local_time_without_exposing_data(camera, body):
    camera.clock_status = 200
    camera.clock_body = body
    assert len(_probe(camera)) == 1
    assert len(camera.clock_requests) == 1


def test_optional_clock_subdeadline_leaves_budget_for_successful_authentication(
    camera, monkeypatch
):
    camera.clock_delay = 0.3
    monkeypatch.setattr(onvif, "_CLOCK_READ_SECONDS", 0.05)
    connector = onvif.OnvifConnector(timeout_seconds=1)
    started = time.monotonic()
    profiles = asyncio.run(connector.probe("127.0.0.1", camera.port, False, _USERNAME, _PASSWORD))
    assert len(profiles) == 1
    assert time.monotonic() - started < 0.5
    assert len(camera.clock_requests) == 1


def test_clock_read_reserves_authenticated_budget_even_for_short_probe(camera):
    camera.clock_delay = 0.7
    connector = onvif.OnvifConnector(timeout_seconds=0.8)
    started = time.monotonic()
    profiles = asyncio.run(connector.probe("127.0.0.1", camera.port, False, _USERNAME, _PASSWORD))
    assert len(profiles) == 1
    assert time.monotonic() - started < 0.6
    assert len(camera.clock_requests) == 1


def test_extreme_clock_arithmetic_falls_back_instead_of_rejecting_authentication():
    client = onvif._Client("127.0.0.1", _USERNAME, _PASSWORD, time.monotonic() + 1)
    client._camera_clock = datetime.max.replace(tzinfo=UTC), time.monotonic() - 2
    assert client._created_at() is None
    assert client._camera_clock is None


@pytest.mark.parametrize("jump", [-1200, 1200])
def test_real_camera_jump_during_auth_rereads_clock_and_retries_failed_operation(camera, jump):
    camera.clock_offset = 0
    camera.shift_after_action = "GetCapabilities", jump
    (profile,) = _probe(camera)
    assert profile.clock.status == "measured"
    assert abs(profile.clock.offset_seconds - jump) < 3
    assert len(camera.clock_requests) == 2
    assert [action for _, action, _, _ in camera.requests] == [
        "GetCapabilities",
        "GetProfiles",
        "GetProfiles",
        "GetStreamUri",
    ]
    assert profile.clock.sync_verified is False


def test_initially_unavailable_clock_can_recover_correct_credentials(camera):
    camera.clock_offset = 1200
    camera.clock_sequence = [None, 1200]
    (profile,) = _probe(camera)
    assert len(camera.clock_requests) == 2
    assert [action for _, action, _, _ in camera.requests] == [
        "GetCapabilities",
        "GetCapabilities",
        "GetProfiles",
        "GetStreamUri",
    ]
    assert profile.clock.status == "measured"


def test_unchanged_clock_never_retries_bad_password_and_exposes_only_safe_reading(camera):
    camera.clock_offset = 1200
    camera.clock_mode = "NTP"
    with pytest.raises(onvif.OnvifError) as caught:
        _probe(camera, password="wrong-fixture-only-password")
    assert len(camera.clock_requests) == 2
    assert len(camera.requests) == 1
    assert caught.value.code == "auth_failed"
    public = caught.value.clock.public()
    assert public["status"] == "measured" and public["reported_mode"] == "ntp"
    assert abs(public["offset_seconds"] - 1200) < 3
    assert public["sync_verified"] is False
    assert _PASSWORD not in repr(public) and "wrong-fixture-only-password" not in repr(public)


def test_clock_reread_failure_never_retries_authentication(camera):
    camera.clock_offset = 0
    camera.clock_sequence = [0, None]
    camera.shift_after_action = "GetCapabilities", 1200
    with pytest.raises(onvif.OnvifError) as caught:
        _probe(camera)
    assert caught.value.clock.status == "unavailable"
    assert caught.value.code == "auth_failed"
    assert len(camera.clock_requests) == 2
    assert [action for _, action, _, _ in camera.requests] == ["GetCapabilities", "GetProfiles"]


def test_second_camera_jump_does_not_create_unbounded_auth_retries(camera):
    camera.clock_offset = 0
    camera.shift_after_action = "GetCapabilities", 1200

    def jump_again(action):
        if action == "GetProfiles" and len(camera.clock_requests) == 2:
            camera.clock_offset = -1200

    camera.after_action = jump_again
    with pytest.raises(onvif.OnvifError) as caught:
        _probe(camera)
    assert caught.value.code == "auth_failed"
    assert len(camera.clock_requests) == 2
    assert [action for _, action, _, _ in camera.requests].count("GetStreamUri") == 1


@pytest.mark.parametrize("jump", [-7200, 7200])
def test_pc_wall_clock_jump_does_not_change_monotonic_wsse_prediction(camera, monkeypatch, jump):
    camera.clock_offset = 0

    def pc_jump(action):
        if action == "GetCapabilities":
            monkeypatch.setattr(
                onvif, "_utc_now", lambda: datetime.now(UTC) + timedelta(seconds=jump)
            )

    camera.after_action = pc_jump
    (profile,) = _probe(camera)
    assert len(camera.clock_requests) == 1 and not camera.rejected
    assert abs(profile.clock.offset_seconds) < 3
    assert [action for _, action, _, _ in camera.requests] == [
        "GetCapabilities",
        "GetProfiles",
        "GetStreamUri",
    ]


@pytest.mark.parametrize(
    "mode,expected", [("Manual", "manual"), ("NTP", "ntp"), ("private-server.example", "unknown")]
)
def test_clock_report_is_frozen_finite_and_never_exposes_time_configuration(camera, mode, expected):
    camera.clock_offset = -1200
    camera.clock_mode = mode
    (profile,) = _probe(camera)
    report = profile.clock.public()
    assert set(report) == {
        "status",
        "offset_seconds",
        "measured_at",
        "uncertainty_seconds",
        "reported_mode",
        "sync_verified",
    }
    assert report["reported_mode"] == expected
    assert report["status"] == "measured"
    assert -1203 < report["offset_seconds"] < -1197
    assert report["measured_at"].endswith("Z") and 1 <= report["uncertainty_seconds"] <= 2
    assert report["sync_verified"] is False
    assert "private-server.example" not in str(report)
    with pytest.raises(AttributeError):
        profile.clock.status = "changed"


def test_unavailable_time_report_has_no_offset_and_is_present_on_failed_probe(camera):
    with pytest.raises(onvif.OnvifError) as caught:
        _probe(camera, password="wrong-fixture-only-password")
    report = caught.value.clock.public()
    assert report["status"] == "unavailable"
    assert report["offset_seconds"] is None and report["uncertainty_seconds"] is None
    assert report["reported_mode"] == "unknown" and report["sync_verified"] is False


@pytest.mark.parametrize(
    "values",
    [
        {"status": "private"},
        {"reported_mode": "https://private"},
        {"sync_verified": True},
        {"status": "measured", "offset_seconds": float("nan")},
        {"status": "measured", "offset_seconds": 0, "uncertainty_seconds": float("inf")},
    ],
)
def test_clock_report_rejects_nonfinite_or_nonallowlisted_metadata(values):
    with pytest.raises(ValueError, match="Invalid camera clock reading"):
        onvif.OnvifClockReading(measured_at=datetime.now(UTC), **values)


def test_real_media2_fallback_only_after_unsupported_action(camera):
    camera.media2 = True
    (profile,) = _probe(camera)
    assert profile.codec == "H264"
    assert [action for _, action, _, _ in camera.requests] == [
        "GetCapabilities",
        "GetProfiles",
        "GetServices",
        "GetProfiles",
        "GetStreamUri",
    ]
    assert camera.requests[-1][0] == "/media2"
    assert _text(camera.requests[-1][2], "Protocol") == "RTSP"


def test_real_profile_response_is_capped_at_eight(camera):
    camera.count = 30
    assert len(_probe(camera)) == 8
    assert sum(action == "GetStreamUri" for _, action, _, _ in camera.requests) == 8


def test_unsupported_first_h265_profile_does_not_hide_later_h264(camera):
    camera.count = 2
    camera.first_fault = "ActionNotSupported"
    (profile,) = _probe(camera)
    assert profile.token == "profile-1" and profile.codec == "H264"


def test_auth_failure_on_one_profile_is_not_ignored(camera):
    camera.count = 2
    camera.first_fault = "NotAuthorized"
    with pytest.raises(onvif.OnvifError) as caught:
        _probe(camera)
    assert caught.value.code == "auth_failed"
    assert sum(action == "GetStreamUri" for _, action, _, _ in camera.requests) == 1


def test_secure_camera_rejects_http_service_downgrade_before_network(monkeypatch):
    monkeypatch.setattr(
        onvif, "_Connection", lambda *args: pytest.fail("Credential downgrade forbidden")
    )
    client = onvif._Client(
        "127.0.0.1", _USERNAME, _PASSWORD, time.monotonic() + 1, require_secure=True
    )
    with pytest.raises(onvif.OnvifError) as caught:
        client.request("http://127.0.0.1/media", "<GetProfiles/>", onvif._MEDIA, "GetProfiles")
    assert caught.value.code == "invalid_response"


def test_foreign_service_endpoint_is_refused_before_any_followup(camera):
    camera.foreign = True
    with pytest.raises(onvif.OnvifError) as caught:
        _probe(camera)
    assert caught.value.code == "invalid_response"
    assert len(camera.requests) == 1


@pytest.mark.parametrize(
    "uri",
    [
        "rtsp://192.0.2.200/live",
        "rtsp://localhost/live",
        "rtsp://127.0.0.1:0/live",
        "http://127.0.0.1/live",
        "rtsp://127.0.0.1/live#secret",
    ],
)
def test_foreign_or_invalid_private_stream_never_enters_connection(camera, uri):
    camera.stream = uri
    with pytest.raises(onvif.OnvifError) as caught:
        _probe(camera)
    assert caught.value.code == "invalid_response"
    assert str(caught.value) == "Camera connection failed"
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    "status,code", [(401, "auth_failed"), (403, "auth_failed"), (302, "invalid_response")]
)
def test_http_denial_and_redirect_are_finite_and_do_not_redirect(camera, status, code):
    camera.status = status
    with pytest.raises(onvif.OnvifError) as caught:
        _probe(camera)
    assert caught.value.code == code
    assert len(camera.requests) == 1


@pytest.mark.parametrize(
    "body",
    [
        b'<!DOCTYPE x [<!ENTITY private "secret">]><x>&private;</x>',
        b"x" * (1024 * 1024 + 1),
        b"<broken",
        b"<NotSOAP/>",
        "<!DOCTYPE x><x/>".encode("utf-16"),
    ],
    ids=["entity", "over-budget", "malformed", "non-soap", "utf16-doctype"],
)
def test_untrusted_xml_and_response_limit_are_enforced(camera, body):
    camera.malformed = body
    with pytest.raises(onvif.OnvifError) as caught:
        _probe(camera)
    assert caught.value.code == "invalid_response"
    assert "secret" not in str(caught.value)


def test_absolute_deadline_closes_dribbling_socket(camera):
    camera.drip = True
    connector = onvif.OnvifConnector(timeout_seconds=0.15)
    started = time.monotonic()
    with pytest.raises(onvif.OnvifError):
        asyncio.run(connector.probe("127.0.0.1", camera.port, False, _USERNAME, _PASSWORD))
    assert time.monotonic() - started < 1
    assert connector._active == 0


def test_cancelled_workers_keep_slots_until_real_work_finishes(monkeypatch):
    release = threading.Event()
    entered = [threading.Event(), threading.Event()]
    count = 0
    lock = threading.Lock()

    def work(*args):
        nonlocal count
        with lock:
            index = count
            count += 1
        entered[index].set()
        assert release.wait(2)
        return ()

    monkeypatch.setattr(onvif, "_probe", work)

    async def run():
        connector = onvif.OnvifConnector()
        tasks = [
            asyncio.create_task(connector.probe("127.0.0.1", 80, False, "", "")) for _ in range(2)
        ]
        try:
            for event in entered:
                assert await asyncio.to_thread(event.wait, 1)
            for task in tasks:
                task.cancel()
            for task in tasks:
                with pytest.raises(asyncio.CancelledError):
                    await task
            with pytest.raises(onvif.OnvifError) as caught:
                await connector.probe("127.0.0.1", 80, False, "", "")
            assert caught.value.code == "busy"
        finally:
            release.set()
            await asyncio.gather(*connector._tasks)
        assert connector._active == 0

    asyncio.run(run())


def test_discovery_extracts_only_source_free_same_sender_candidates():
    body = (
        f'<s:Envelope xmlns:s="{onvif._SOAP}" xmlns:d="{onvif._WSD}">'
        "<s:Body><d:ProbeMatches><d:ProbeMatch>"
        "<d:Scopes>onvif://www.onvif.org/name/Front%20Door</d:Scopes>"
        "<d:XAddrs>http://127.0.0.1:8899/onvif/device_service "
        "https://127.0.0.1/device http://192.0.2.1/device "
        "http://user:secret@127.0.0.1/device</d:XAddrs>"
        "</d:ProbeMatch></d:ProbeMatches></s:Body></s:Envelope>"
    ).encode()
    assert onvif._discovery_candidates(body, "127.0.0.1") == [
        {"host": "127.0.0.1", "port": 8899, "secure": False, "name": "Front Door"},
        {"host": "127.0.0.1", "port": 443, "secure": True, "name": "Front Door"},
    ]


def test_discovery_transport_is_fixed_local_multicast_and_candidate_count_bounded(monkeypatch):
    class Wire:
        count = 0
        options = []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def bind(self, address):
            assert address == ("127.0.0.1", 0)

        def setsockopt(self, *option):
            self.options.append(option)

        def settimeout(self, timeout):
            assert 0 < timeout <= 3

        def setblocking(self, blocking):
            assert blocking is False

        def sendto(self, body, address):
            assert address == ("239.255.255.250", 3702)
            assert b"onvif" not in body or b"Probe" in body

        def recvfrom(self, size):
            self.count += 1
            host = f"192.0.2.{self.count}"
            return f"<ProbeMatch><XAddrs>http://{host}/device</XAddrs></ProbeMatch>".encode(), (
                host,
                3702,
            )

    wire = Wire()
    monkeypatch.setattr(onvif.socket, "socket", lambda *args: wire)
    monkeypatch.setattr(onvif.select, "select", lambda *args: ([wire], [], []))
    results = onvif._discover("127.0.0.1", 3)
    assert len(results) == wire.count == 64
    assert (onvif.socket.IPPROTO_IP, onvif.socket.IP_MULTICAST_TTL, 1) in wire.options


def test_multi_interface_discovery_uses_one_deadline_and_closes_every_socket(monkeypatch):
    sockets = []
    selected_budgets = []

    class Wire:
        closed = False

        def __init__(self, *args):
            sockets.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.closed = True

        def bind(self, address):
            self.address = address

        def setblocking(self, blocking):
            assert blocking is False

        def setsockopt(self, *args):
            pass

        def sendto(self, body, address):
            assert address == ("239.255.255.250", 3702)

    def select(wires, writing, errors, remaining):
        selected_budgets.append(remaining)
        assert len(wires) == 2
        return [], [], []

    monkeypatch.setattr(onvif.socket, "socket", Wire)
    monkeypatch.setattr(onvif.select, "select", select)
    assert onvif._discover(None, 3, lambda: ("192.0.2.1", "192.0.2.2")) == []
    assert [wire.address for wire in sockets] == [("192.0.2.1", 0), ("192.0.2.2", 0)]
    assert len(selected_budgets) == 1 and 0 < selected_budgets[0] <= 3
    assert all(wire.closed for wire in sockets)


def test_interface_provider_count_is_bounded_before_socket_creation(monkeypatch):
    monkeypatch.setattr(onvif.socket, "socket", lambda *args: pytest.fail("Network forbidden"))
    with pytest.raises(onvif.OnvifError) as caught:
        onvif._discover(None, 3, lambda: ["192.0.2.1"] * 17)
    assert caught.value.code == "invalid_response"


def test_invalid_host_is_refused_without_network(monkeypatch):
    monkeypatch.setattr(onvif, "_Connection", lambda *args: pytest.fail("Network forbidden"))
    with pytest.raises(onvif.OnvifError) as caught:
        asyncio.run(onvif.OnvifConnector().probe("camera.local", 80, False, "", ""))
    assert caught.value.code == "invalid_response"
