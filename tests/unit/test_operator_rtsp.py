from __future__ import annotations

import asyncio
import hashlib
from urllib.request import parse_http_list, parse_keqv_list

import pytest

from k5vision.operator_rtsp import (
    RtspConnector,
    RtspError,
    _authorization,
    _has_h264_video,
    stream_selection,
)


@pytest.mark.parametrize(
    "uri",
    [
        "http://127.0.0.1/live",
        "rtsp://camera.local/live",
        "rtsp://127.0.0.1:0/live",
        "rtsp://127.0.0.1:65536/live",
        "rtsp://127.0.0.1/live#secret",
        "rtsp://127.0.0.1/%zz",
        "rtsp://127.0.0.1/live\r\nAuthorization: secret",
        "rtsp://127.0.0.1/live path",
        "rtsp://user@127.0.0.1/" + "x" * 4096,
    ],
)
def test_invalid_sources_are_sanitized(uri):
    with pytest.raises(RtspError, match="^RTSP connection test failed$"):
        stream_selection(uri)


def test_private_source_credentials_and_ipv6_are_normalized_without_repr_disclosure():
    selected = stream_selection("rtsp://[::1]/live?token=private", "user", "s:e@cret")
    assert selected.host == "::1" and selected.port == 554
    assert selected.source_uri == "rtsp://user:s%3Ae%40cret@[::1]:554/live?token=private"
    assert selected.request_uri == "rtsp://[::1]:554/live?token=private"
    assert "private" not in repr(selected) and "cret" not in repr(selected)
    assert stream_selection("rtsps://127.0.0.1/live").port == 322
    assert stream_selection("rtsp://user:s%3Ae%40cret@127.0.0.1/live").password == "s:e@cret"
    with pytest.raises(RtspError):
        stream_selection("rtsp://user:old@127.0.0.1/live", "different", "new")


def test_sdp_requires_matching_video_h264_payload():
    assert _has_h264_video(b"v=0\r\nm=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n")
    for body in (
        b"m=audio 0 RTP/AVP 96\na=rtpmap:96 H264/90000\n",
        b"m=video 0 RTP/AVP 97\na=rtpmap:96 H264/90000\n",
        b"m=video 0 RTP/AVP 96\na=rtpmap:96 H265/90000\n",
    ):
        assert not _has_h264_video(body)


@pytest.mark.parametrize("algorithm", ["MD5", "SHA-256"])
def test_digest_response_binds_method_uri_credentials_and_qop(algorithm):
    selected = stream_selection("rtsp://127.0.0.1/live", "camera", "private")
    answer = _authorization(
        f'Digest realm="camera", nonce="nonce", algorithm={algorithm}, qop="auth,auth-int"',
        selected,
    )
    values = parse_keqv_list(parse_http_list(answer.removeprefix("Digest ")))

    def digest(value):
        return hashlib.new("md5" if algorithm == "MD5" else "sha256", value.encode()).hexdigest()

    expected = digest(
        digest("camera:camera:private")
        + ":nonce:00000001:"
        + values["cnonce"]
        + ":auth:"
        + digest("DESCRIBE:" + selected.request_uri)
    )
    assert values["response"] == expected and values["uri"] == selected.request_uri
    assert values["qop"] == "auth" and "private" not in answer


@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("success", None),
        ("wrong-password", "auth_failed"),
        ("redirect", "unreachable"),
        ("oversize", "invalid_response"),
        ("h265", "unsupported"),
        ("wrong-cseq", "invalid_response"),
    ],
)
def test_real_tcp_describe_is_bounded_authenticated_and_source_free(scenario, expected):
    async def exercise():
        requests = []
        errors = []

        async def camera(reader, writer):
            try:
                for seq in (1, 2):
                    request = (await reader.readuntil(b"\r\n\r\n")).decode()
                    requests.append(request)
                    if seq == 1:
                        writer.write(
                            b"RTSP/1.0 401 Unauthorized\r\nCSeq: 1\r\n"
                            b'WWW-Authenticate: Basic realm="camera"\r\n'
                            b'WWW-Authenticate: Digest realm="camera", nonce="nonce"\r\n'
                            b"Content-Length: 0\r\n\r\n"
                        )
                        await writer.drain()
                        continue
                    assert "Authorization: Digest " in request
                    code = (
                        401
                        if scenario == "wrong-password"
                        else 302
                        if scenario == "redirect"
                        else 200
                    )
                    body = b"v=0\r\nm=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n"
                    if scenario == "h265":
                        body = body.replace(b"H264", b"H265")
                    length = 65537 if scenario == "oversize" else len(body)
                    cseq = 3 if scenario == "wrong-cseq" else 2
                    writer.write(
                        (
                            f"RTSP/1.0 {code} Response\r\nCSeq: {cseq}\r\n"
                            f"Content-Type: application/sdp\r\nContent-Length: {length}\r\n\r\n"
                        ).encode()
                        + body
                    )
                    await writer.drain()
            except Exception as error:
                errors.append(error)
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(camera, "127.0.0.1", 0)
        async with server:
            port = server.sockets[0].getsockname()[1]
            selected = stream_selection(f"rtsp://127.0.0.1:{port}/live", "camera", "private")
            if expected:
                with pytest.raises(RtspError) as result:
                    await RtspConnector().probe(selected)
                assert result.value.code == expected
            else:
                await RtspConnector().probe(selected)
        assert len(requests) == 2 and not errors
        assert "Authorization" not in requests[0] and "private" not in "".join(requests)

    asyncio.run(exercise())
