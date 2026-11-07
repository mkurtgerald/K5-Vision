"""Real loopback Media1/Media2 encoder changes and bounded authority failures."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import hmac
import http.server
import json
import socket
import threading
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.request import parse_http_list, parse_keqv_list
from uuid import uuid4
from xml.sax.saxutils import escape

import pytest

from k5vision.domain.devices import Device, DeviceProtocol
from k5vision.media.mediamtx_config import MediaMTXSource
from k5vision.media.mediamtx_provider import MediaMTXBinding
from k5vision.operator_camera_settings import (
    CameraVideoSettingsError,
    OnvifVideoSettingsService,
)
from k5vision.operator_onvif import _DEVICE, _MEDIA, _MEDIA2, _SOAP

_TT = "http://www.onvif.org/ver10/schema"


def _local(node):
    return node.tag.rsplit("}", 1)[-1]


def _value(root, name):
    return next(((n.text or "") for n in root.iter() if _local(n) == name), "")


def _configuration(namespace):
    rate = "<tt:EncodingInterval>1</tt:EncodingInterval>" if namespace == _MEDIA else ""
    h264 = (
        "<tt:H264><tt:GovLength>30</tt:GovLength><tt:H264Profile>Main</tt:H264Profile></tt:H264>"
        if namespace == _MEDIA
        else ""
    )
    attributes = "" if namespace == _MEDIA else ' GovLength="30" Profile="Main"'
    return ET.fromstring(
        f'<tr:Configuration xmlns:tr="{namespace}" xmlns:tt="{_TT}" '
        f'xmlns:v="urn:fixture:preserve" token="encoder-main"{attributes}>'
        "<tt:Name>Private encoder name</tt:Name><tt:UseCount>1</tt:UseCount>"
        "<tt:Encoding>H264</tt:Encoding><tt:Resolution><tt:Width>1920</tt:Width>"
        "<tt:Height>1080</tt:Height></tt:Resolution><tt:Quality>4.25</tt:Quality>"
        f"<tt:RateControl><tt:FrameRateLimit>30</tt:FrameRateLimit>{rate}"
        f"<tt:BitrateLimit>2048</tt:BitrateLimit></tt:RateControl>{h264}"
        "<tt:Multicast><tt:Address><tt:Type>IPv4</tt:Type>"
        "<tt:IPv4Address>239.1.2.3</tt:IPv4Address></tt:Address><tt:Port>0</tt:Port>"
        "<tt:TTL>1</tt:TTL><tt:AutoStart>false</tt:AutoStart></tt:Multicast>"
        "<tt:SessionTimeout>PT60S</tt:SessionTimeout>"
        '<v:Retained enabled="true">preserve-me</v:Retained></tr:Configuration>'
    )


def _options(namespace):
    resolutions = (
        "<tt:ResolutionsAvailable><tt:Width>1920</tt:Width><tt:Height>1080</tt:Height>"
        "</tt:ResolutionsAvailable><tt:ResolutionsAvailable><tt:Width>1280</tt:Width>"
        "<tt:Height>720</tt:Height></tt:ResolutionsAvailable>"
    )
    quality = "<tt:QualityRange><tt:Min>1</tt:Min><tt:Max>10</tt:Max></tt:QualityRange>"
    bitrate = "<tt:BitrateRange><tt:Min>256</tt:Min><tt:Max>8192</tt:Max></tt:BitrateRange>"
    if namespace == _MEDIA:
        return (
            f"<tr:Options>{quality}<tt:JPEG>{resolutions}</tt:JPEG><tt:H264>{resolutions}"
            "<tt:FrameRateRange><tt:Min>1</tt:Min><tt:Max>30</tt:Max></tt:FrameRateRange>"
            "<tt:GovLengthRange><tt:Min>1</tt:Min><tt:Max>120</tt:Max></tt:GovLengthRange>"
            "<tt:EncodingIntervalRange><tt:Min>1</tt:Min><tt:Max>4</tt:Max>"
            "</tt:EncodingIntervalRange><tt:H264ProfilesSupported>Main</tt:H264ProfilesSupported>"
            f"</tt:H264><tt:Extension><tt:H264>{bitrate}</tt:H264>"
            "</tt:Extension></tr:Options>"
        )
    return (
        '<tr:Options FrameRatesSupported="30 29.97 15 7.5" GovLengthRange="1 120" '
        f'ProfilesSupported="Main"><tt:Encoding>H264</tt:Encoding>{quality}'
        f"{resolutions}{bitrate}</tr:Options>"
        '<tr:Options FrameRatesSupported="30 15" GovLengthRange="1 120" '
        f'ProfilesSupported="Main"><tt:Encoding>H265</tt:Encoding>{quality}'
        f"{resolutions}{bitrate}</tr:Options>"
    )


@pytest.fixture
def camera(request):
    namespace = _MEDIA2 if getattr(request, "param", False) else _MEDIA
    settings = SimpleNamespace(
        namespace=namespace,
        configuration=_configuration(namespace),
        profiles=[("profile-main", "encoder-main", "source-main", "/main?camera=1")],
        username="stream-viewer",
        password="stream-secret",
        digest=False,
        clock_offset=0,
        requests=[],
        writes=[],
        ignore_write=False,
        disconnect_after_write=False,
        reassign_after_write=None,
        unsupported_options=False,
        unsafe_namespace=False,
        malformed_options=False,
        media1_unmatched=False,
        unsupported_profiles=set(),
        hold_action=None,
        hold=threading.Event(),
        entered=threading.Event(),
        before=None,
    )

    class Server(http.server.ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, request, client_address):
            pass

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, action, body, namespace=None, status=200):
            ns = namespace or getattr(self, "response_namespace", settings.namespace)
            raw = (
                f'<s:Envelope xmlns:s="{_SOAP}" xmlns:tr="{ns}" xmlns:tt="{_TT}" '
                f'xmlns:tds="{_DEVICE}"><s:Body><tr:{action}Response>'
                f"{body}</tr:{action}Response></s:Body></s:Envelope>"
            ).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/soap+xml")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def authorized(self, root):
            try:
                created = _value(root, "Created")
                when = datetime.fromisoformat(created.replace("Z", "+00:00"))
                now = datetime.now(UTC) + timedelta(seconds=settings.clock_offset)
                nonce = base64.b64decode(_value(root, "Nonce"), validate=True)
                digest = base64.b64encode(
                    hashlib.sha1(
                        nonce + created.encode() + settings.password.encode(), usedforsecurity=False
                    ).digest()
                ).decode()
                valid = (
                    _value(root, "Username") == settings.username
                    and hmac.compare_digest(_value(root, "Password"), digest)
                    and abs((when - now).total_seconds()) < 3
                )
                if settings.digest:
                    header = self.headers.get("Authorization", "")
                    if not header.startswith("Digest "):
                        return False
                    parts = parse_keqv_list(parse_http_list(header[7:]))

                    def md5(value):
                        return hashlib.md5(value.encode(), usedforsecurity=False).hexdigest()

                    a1 = md5(settings.username + ":video-fixture:" + settings.password)
                    a2 = md5("POST:" + self.path)
                    expected = md5(f"{a1}:fixture-nonce:{parts['nc']}:{parts['cnonce']}:auth:{a2}")
                    valid = valid and hmac.compare_digest(parts["response"], expected)
                return valid
            except Exception:
                return False

        def do_POST(self):
            try:
                root = ET.fromstring(self.rfile.read(int(self.headers["Content-Length"])))
                body = next(n for n in root if _local(n) == "Body")
                operation = list(body)[0]
                action = _local(operation)
                self.response_namespace = operation.tag.partition("}")[0][1:]
                settings.requests.append(action)
                if action == "GetSystemDateAndTime":
                    now = datetime.now(UTC) + timedelta(seconds=settings.clock_offset)
                    self.respond(
                        action,
                        "<tt:SystemDateAndTime><tt:DateTimeType>Manual</tt:DateTimeType>"
                        f"<tt:UTCDateTime><tt:Date><tt:Year>{now.year}</tt:Year>"
                        f"<tt:Month>{now.month}</tt:Month><tt:Day>{now.day}</tt:Day>"
                        f"</tt:Date><tt:Time><tt:Hour>{now.hour}</tt:Hour>"
                        f"<tt:Minute>{now.minute}</tt:Minute><tt:Second>{now.second}</tt:Second>"
                        "</tt:Time></tt:UTCDateTime></tt:SystemDateAndTime>",
                        _DEVICE,
                    )
                    return
                if not self.authorized(root):
                    self.send_response(401)
                    if settings.digest:
                        self.send_header(
                            "WWW-Authenticate",
                            'Digest realm="video-fixture",nonce="fixture-nonce",'
                            'algorithm=MD5,qop="auth"',
                        )
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if settings.before:
                    settings.before(action, operation)
                if action == settings.hold_action:
                    settings.entered.set()
                    settings.hold.wait(2)
                ns = self.response_namespace
                if action == "GetServices":
                    endpoint = f"http://127.0.0.1:{self.server.server_port}/media"
                    services = (
                        [_MEDIA, _MEDIA2] if settings.media1_unmatched else [settings.namespace]
                    )
                    self.respond(
                        action,
                        "".join(
                            f"<tds:Service><tds:Namespace>{service}</tds:Namespace>"
                            f"<tds:XAddr>{endpoint}</tds:XAddr></tds:Service>"
                            for service in services
                        ),
                        _DEVICE,
                    )
                elif action == "GetProfiles":
                    rows = []
                    for token, encoder, source, _ in settings.profiles:
                        configuration = copy.deepcopy(settings.configuration)
                        configuration.set("token", encoder)
                        configuration.tag = (
                            f"{{{_TT}}}VideoEncoderConfiguration"
                            if ns == _MEDIA
                            else f"{{{ns}}}VideoEncoder"
                        )
                        src = (
                            f'<tt:VideoSourceConfiguration token="source-config">'
                            f"<tt:SourceToken>{source}</tt:SourceToken>"
                            "</tt:VideoSourceConfiguration>"
                            if ns == _MEDIA
                            else f'<tr:VideoSource token="source-config"><tt:SourceToken>{source}'
                            "</tt:SourceToken></tr:VideoSource>"
                        )
                        content = src + ET.tostring(configuration, encoding="unicode")
                        if ns == _MEDIA2:
                            content = "<tr:Configurations>" + content + "</tr:Configurations>"
                        rows.append(
                            f'<tr:Profiles token="{token}"><tt:Name>Camera</tt:Name>'
                            + content
                            + "</tr:Profiles>"
                        )
                    self.respond(action, "".join(rows))
                elif action == "GetStreamUri":
                    token = _value(operation, "ProfileToken")
                    if token in settings.unsupported_profiles:
                        self.send_response(404)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    path = next(p[3] for p in settings.profiles if p[0] == token)
                    if ns == _MEDIA and settings.media1_unmatched:
                        path = "/unrelated-media1-stream"
                    uri = "rtsp://camera-admin:management-secret@127.0.0.1:8554" + path
                    content = (
                        f"<tr:MediaUri><tt:Uri>{escape(uri)}</tt:Uri></tr:MediaUri>"
                        if ns == _MEDIA
                        else f"<tr:Uri>{escape(uri)}</tr:Uri>"
                    )
                    self.respond(action, content)
                elif action in {"GetVideoEncoderConfiguration", "GetVideoEncoderConfigurations"}:
                    node = copy.deepcopy(settings.configuration)
                    node.tag = f"{{{ns}}}" + ("Configuration" if ns == _MEDIA else "Configurations")
                    self.respond(action, ET.tostring(node, encoding="unicode"))
                elif action == "GetVideoEncoderConfigurationOptions":
                    if settings.unsupported_options:
                        self.send_response(404)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                    else:
                        content = _options(ns)
                        if settings.malformed_options:
                            content = content.replace("<tt:Min>1</tt:Min>", "<tt:Min>NaN</tt:Min>")
                        self.respond(
                            action, content, "urn:wrong" if settings.unsafe_namespace else ns
                        )
                elif action == "SetVideoEncoderConfiguration":
                    node = next(n for n in operation if _local(n) == "Configuration")
                    if ns == _MEDIA:
                        assert _value(operation, "ForcePersistence") == "true"
                    else:
                        assert not _value(operation, "ForcePersistence")
                    settings.writes.append(copy.deepcopy(node))
                    if not settings.ignore_write:
                        settings.configuration = copy.deepcopy(node)
                    if settings.reassign_after_write is not None:
                        settings.profiles[0] = settings.reassign_after_write
                    if settings.disconnect_after_write:
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.connection.close()
                    else:
                        self.respond(action, "")
                else:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    server = Server(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    device = Device(
        id=uuid4(),
        name="Video fixture",
        host="127.0.0.1",
        management_port=server.server_port,
        protocols={DeviceProtocol.ONVIF, DeviceProtocol.RTSP},
    )
    settings.source = MediaMTXSource(
        MediaMTXBinding("video-fixture", device.id),
        device,
        "rtsp://stream-viewer:stream-secret@127.0.0.1:8554/main?camera=1",
    )
    yield settings
    settings.hold.set()
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


@pytest.mark.parametrize("camera", [False, True], indirect=True)
def test_inspects_both_media_versions_and_applies_actual_configuration(camera):
    async def run():
        service = OnvifVideoSettingsService()
        original = copy.deepcopy(camera.configuration)
        inspection = await service.inspect(camera.source)
        public = inspection.public()
        assert public["current"]["frame_rate"] == 30
        assert public["options"]["frame_rate"]["integer_only"] is (camera.namespace == _MEDIA)
        assert public["writable"] is True and public["stream_verified"] is False
        result = await service.apply(
            inspection,
            {
                "frame_rate": 15,
                "resolution": {"width": 1280, "height": 720},
                "bitrate_kbps": 1024,
                "gop": 15,
                "quality": 5.5,
            },
        )
        report = result.public()
        assert report["current"]["resolution"] == {"width": 1280, "height": 720}
        assert report["current"]["frame_rate"] == 15
        assert report["current"]["bitrate_kbps"] == 1024
        assert report["current"]["gop"] == 15
        assert report["configuration_readback_verified"] is True
        assert report["stream_verified"] is False and report["observed_fps"] is None
        assert len(camera.writes) == 1
        assert result.source_uri == camera.source.source_uri
        assert _value(camera.writes[0], "SessionTimeout") == _value(original, "SessionTimeout")
        assert _value(camera.writes[0], "IPv4Address") == "239.1.2.3"
        retained = camera.writes[0].find("{urn:fixture:preserve}Retained")
        assert retained.text == "preserve-me" and retained.get("enabled") == "true"
        text = json.dumps(report) + repr(inspection) + repr(result)
        assert all(
            v not in text for v in ["rtsp://", "management-secret", "stream-secret", "encoder-main"]
        )
        result.clear()
        inspection.clear()
        assert result.source_uri == "" and inspection.source is None and inspection.password == ""
        await service.aclose()

    asyncio.run(run())


def test_digest_and_camera_clock_compensation_preserve_original_stream_account(camera):
    camera.username, camera.password = "camera-admin", "management-secret"
    camera.digest, camera.clock_offset = True, 180

    async def run():
        service = OnvifVideoSettingsService()
        inspection = await service.inspect(
            camera.source, username="camera-admin", password="management-secret"
        )
        result = await service.apply(inspection, {"frame_rate": 12})
        assert result.source_uri == camera.source.source_uri
        assert "camera-admin" not in result.source_uri
        assert len(camera.writes) == 1
        result.clear()
        inspection.clear()
        await service.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "field,value",
    [
        ("frame_rate", 31),
        ("frame_rate", True),
        ("frame_rate", float("nan")),
        ("frame_rate", 7.5),
        ("bitrate_kbps", 1),
        ("gop", -1),
        ("quality", 11),
        ("resolution", {"width": 999, "height": 999}),
        ("encoding_interval", 0),
        ("unknown", "private"),
    ],
)
def test_out_of_capability_inputs_never_write(camera, field, value):
    async def run():
        service = OnvifVideoSettingsService()
        inspection = await service.inspect(camera.source)
        before = len(camera.requests)
        with pytest.raises(CameraVideoSettingsError, match="video_invalid_request"):
            await service.apply(inspection, {field: value})
        assert not camera.writes and len(camera.requests) == before
        inspection.clear()
        await service.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("camera", [True], indirect=True)
def test_media2_discrete_fractional_rates_and_h264_restore(camera):
    camera.configuration.find(f"{{{_TT}}}Encoding").text = "H265"

    async def run():
        service = OnvifVideoSettingsService()
        inspection = await service.inspect(camera.source)
        assert inspection.public()["current"]["codec"] == "H265"
        assert {
            c["codec"]: c["product_supported"] for c in inspection.public()["options"]["codecs"]
        } == {"H264": True, "H265": False}
        with pytest.raises(CameraVideoSettingsError, match="video_codec_unsupported"):
            await service.apply(inspection, {"frame_rate": 7.5})
        with pytest.raises(CameraVideoSettingsError, match="video_invalid_request"):
            await service.apply(inspection, {"codec": "H264", "frame_rate": 8.5})
        result = await service.apply(inspection, {"codec": "H264", "frame_rate": 7.5})
        assert result.public()["current"]["codec"] == "H264"
        assert result.public()["current"]["frame_rate"] == 7.5
        assert len(camera.writes) == 1
        result.clear()
        inspection.clear()
        await service.aclose()

    asyncio.run(run())


def test_conflict_and_shared_acknowledgement_are_checked_before_set(camera):
    camera.profiles.append(("profile-alias", "encoder-main", "source-main", "/main?camera=1"))
    camera.configuration.find(f"{{{_TT}}}UseCount").text = "2"

    async def run():
        service = OnvifVideoSettingsService()
        inspection = await service.inspect(camera.source)
        assert inspection.public()["shared_configuration"] is True
        with pytest.raises(CameraVideoSettingsError, match="video_shared_configuration"):
            await service.apply(inspection, {"frame_rate": 15})
        camera.configuration.find(f"{{{_TT}}}Quality").text = "6"
        with pytest.raises(CameraVideoSettingsError, match="video_configuration_conflict"):
            await service.apply(inspection, {"frame_rate": 15, "acknowledge_shared": True})
        assert not camera.writes
        inspection.clear()
        fresh = await service.inspect(camera.source)
        result = await service.apply(fresh, {"frame_rate": 15, "acknowledge_shared": True})
        assert result.public()["current"]["frame_rate"] == 15
        result.clear()
        fresh.clear()
        await service.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "profile",
    [
        ("other-channel", "encoder-main", "source-other", "/main?camera=1"),
        ("other-encoder", "encoder-other", "source-main", "/main?camera=1"),
    ],
)
def test_same_uri_different_channel_or_encoder_is_ambiguous(camera, profile):
    camera.profiles.append(profile)

    async def run():
        service = OnvifVideoSettingsService()
        with pytest.raises(CameraVideoSettingsError, match="video_profile_ambiguous"):
            await service.inspect(camera.source)
        assert not camera.writes
        await service.aclose()

    asyncio.run(run())


def test_no_profile_match_never_selects_first_profile(camera):
    camera.profiles[0] = ("unrelated", "encoder-main", "source-main", "/another")

    async def run():
        service = OnvifVideoSettingsService()
        with pytest.raises(CameraVideoSettingsError, match="video_profile_unidentified"):
            await service.inspect(camera.source)
        assert not camera.writes
        await service.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("camera", [True], indirect=True)
def test_media2_matching_profile_is_used_when_media1_exposes_another_stream(camera):
    camera.media1_unmatched = True

    async def run():
        service = OnvifVideoSettingsService()
        inspection = await service.inspect(camera.source)
        assert inspection.public()["protocol"] == "media2"
        result = await service.apply(inspection, {"frame_rate": 7.5})
        assert result.public()["current"]["frame_rate"] == 7.5
        assert len(camera.writes) == 1
        result.clear()
        inspection.clear()
        await service.aclose()

    asyncio.run(run())


def test_unrelated_profile_without_rtsp_does_not_hide_matching_profile(camera):
    camera.profiles.insert(0, ("no-rtsp", "another-encoder", "source-main", "/unused"))
    camera.unsupported_profiles.add("no-rtsp")

    async def run():
        service = OnvifVideoSettingsService()
        inspection = await service.inspect(camera.source)
        assert inspection.public()["current"]["codec"] == "H264"
        assert not camera.writes
        inspection.clear()
        await service.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "fault,code",
    [
        ("ignore_write", "video_readback_mismatch"),
        ("disconnect_after_write", "video_outcome_unverified"),
    ],
)
def test_acknowledged_or_disconnected_write_is_not_fake_success(camera, fault, code):
    setattr(camera, fault, True)

    async def run():
        service = OnvifVideoSettingsService()
        inspection = await service.inspect(camera.source)
        with pytest.raises(CameraVideoSettingsError, match=code):
            await service.apply(inspection, {"frame_rate": 15})
        assert len(camera.writes) == 1
        inspection.clear()
        await service.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "replacement",
    [
        ("profile-main", "encoder-main", "source-other", "/another-channel"),
        ("profile-main", "encoder-other", "source-main", "/another-encoder"),
        ("profile-replaced", "encoder-main", "source-main", "/replacement-profile"),
    ],
)
def test_post_set_profile_reassignment_never_returns_another_channel_uri(camera, replacement):
    camera.reassign_after_write = replacement

    async def run():
        service = OnvifVideoSettingsService()
        inspection = await service.inspect(camera.source)
        saved_uri = camera.source.source_uri
        with pytest.raises(CameraVideoSettingsError, match="video_outcome_unverified"):
            await service.apply(inspection, {"frame_rate": 15})
        assert len(camera.writes) == 1
        assert camera.source.source_uri == saved_uri
        assert inspection.source is camera.source and not inspection.cleared
        inspection.clear()
        await service.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("qname", ["vendor:Opaque", "tt:Opaque"])
def test_unsupported_options_and_unsafe_qname_configuration_remain_readonly(camera, qname):
    async def run():
        service = OnvifVideoSettingsService()
        camera.unsupported_options = True
        inspection = await service.inspect(camera.source)
        assert inspection.public()["writable_reason"] == "video_options_unavailable"
        with pytest.raises(CameraVideoSettingsError, match="video_unsupported"):
            await service.apply(inspection, {"frame_rate": 15})
        inspection.clear()
        camera.unsupported_options = False
        extension = camera.configuration.find("{urn:fixture:preserve}Retained")
        extension.set("{http://www.w3.org/2001/XMLSchema-instance}type", qname)
        inspection = await service.inspect(camera.source)
        assert inspection.public()["writable_reason"] == "video_unsafe_extension"
        with pytest.raises(CameraVideoSettingsError, match="video_unsupported"):
            await service.apply(inspection, {"frame_rate": 15})
        assert not camera.writes
        inspection.clear()
        await service.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("camera", [True], indirect=True)
def test_h264_restore_rejects_carried_parameters_outside_target_capabilities(camera):
    camera.configuration.find(f"{{{_TT}}}Encoding").text = "H265"
    camera.configuration.find(f"{{{_TT}}}RateControl/{{{_TT}}}FrameRateLimit").text = "60"

    async def run():
        service = OnvifVideoSettingsService()
        inspection = await service.inspect(camera.source)
        with pytest.raises(CameraVideoSettingsError, match="video_invalid_request"):
            await service.apply(inspection, {"codec": "H264"})
        assert not camera.writes
        result = await service.apply(inspection, {"codec": "H264", "frame_rate": 15})
        assert result.public()["current"]["frame_rate"] == 15
        assert len(camera.writes) == 1
        result.clear()
        inspection.clear()
        await service.aclose()

    asyncio.run(run())


def test_standard_string_names_are_not_mistaken_for_extension_qnames(camera):
    camera.configuration.find(f"{{{_TT}}}Name").text = "North:Entrance"

    async def run():
        service = OnvifVideoSettingsService()
        inspection = await service.inspect(camera.source)
        assert inspection.public()["writable"] is True
        result = await service.apply(inspection, {"frame_rate": 15})
        assert camera.writes[0].findtext(f"{{{_TT}}}Name") == "North:Entrance"
        result.clear()
        inspection.clear()
        await service.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("fault", ["unsafe_namespace", "malformed_options"])
def test_wrong_namespace_and_nonfinite_capability_are_rejected(camera, fault):
    setattr(camera, fault, True)

    async def run():
        service = OnvifVideoSettingsService()
        with pytest.raises(CameraVideoSettingsError, match="video_invalid_response"):
            await service.inspect(camera.source)
        assert not camera.writes
        await service.aclose()

    asyncio.run(run())


def test_live_authority_gate_runs_after_fresh_preflight_and_can_refuse_without_write(camera):
    class AuthorityRefused(RuntimeError):
        pass

    async def run():
        service = OnvifVideoSettingsService()
        inspection = await service.inspect(camera.source)
        before = len(camera.requests)

        async def deny():
            assert len(camera.requests) > before
            assert not camera.writes
            raise AuthorityRefused("request denied")

        with pytest.raises(AuthorityRefused, match="request denied"):
            await service.apply(inspection, {"frame_rate": 15}, before_write=deny)
        assert not camera.writes
        inspection.clear()
        await service.aclose()

    asyncio.run(run())


def test_cancellation_joins_bounded_worker_and_prevents_write_after_failed_preflight(camera):
    async def run():
        service = OnvifVideoSettingsService(inspect_seconds=1, apply_seconds=0.35)
        inspection = await service.inspect(camera.source)
        camera.hold_action = "GetVideoEncoderConfigurationOptions"
        task = asyncio.create_task(service.apply(inspection, {"frame_rate": 15}))
        for _ in range(100):
            if camera.entered.is_set():
                break
            await asyncio.sleep(0.005)
        assert camera.entered.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not camera.writes and service.connector._active == 0
        inspection.clear()
        await service.aclose()
        with pytest.raises(CameraVideoSettingsError, match="video_closed"):
            await service.inspect(camera.source)

    asyncio.run(run())
