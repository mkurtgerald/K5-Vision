"""Bounded RTSP DESCRIBE probe without processes, logs or public source URLs."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import re
import secrets
import ssl
from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from urllib.request import parse_http_list, parse_keqv_list


class RtspError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__("RTSP connection test failed")


@dataclass(frozen=True)
class RtspSelection:
    host: str
    port: int
    source_uri: str = field(repr=False)
    request_uri: str = field(repr=False)
    username: str = field(repr=False)
    password: str = field(repr=False)
    secure: bool = False


def stream_selection(source_uri: str, username: str = "", password: str = "") -> RtspSelection:
    """Normalize a numeric camera endpoint and privately encode optional credentials."""
    try:
        if (
            not isinstance(source_uri, str)
            or not 1 <= len(source_uri) <= 4096
            or not source_uri.isascii()
            or any(ord(c) <= 32 or ord(c) == 127 for c in source_uri)
            or "\\" in source_uri
            or re.search(r"%(?![0-9A-Fa-f]{2})", source_uri)
        ):
            raise ValueError
        parsed = urlsplit(source_uri)
        host = str(ipaddress.ip_address(parsed.hostname or ""))
        port = (
            parsed.port if parsed.port is not None else (322 if parsed.scheme == "rtsps" else 554)
        )
        if (
            parsed.scheme not in {"rtsp", "rtsps"}
            or not source_uri.startswith(("rtsp://", "rtsps://"))
            or not 1 <= port <= 65535
            or parsed.fragment
            or (parsed.username is not None and (username or password))
        ):
            raise ValueError
        if parsed.username is not None:
            username = unquote(parsed.username, errors="strict")
            password = unquote(parsed.password or "", errors="strict")
        if (
            not isinstance(username, str)
            or not isinstance(password, str)
            or len(username) > 256
            or len(password) > 256
            or (password and not username)
            or any(ord(c) < 32 or ord(c) == 127 for c in username + password)
            or ":" in username
        ):
            raise ValueError
        authority = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        request_uri = urlunsplit((parsed.scheme, authority, parsed.path or "/", parsed.query, ""))
        credentials = quote(username, safe="") + ":" + quote(password, safe="") + "@"
        private_uri = urlunsplit(
            (
                parsed.scheme,
                (credentials if username else "") + authority,
                parsed.path or "/",
                parsed.query,
                "",
            )
        )
        if len(private_uri) > 4096:
            raise ValueError
        return RtspSelection(
            host, port, private_uri, request_uri, username, password, parsed.scheme == "rtsps"
        )
    except (ValueError, TypeError, UnicodeError):
        raise RtspError("invalid_stream") from None


def _authorization(challenge: str, selected: RtspSelection) -> str:
    kind, _, raw = challenge.partition(" ")
    if not selected.username:
        raise RtspError("auth_failed")
    if kind.casefold() == "basic":
        value = base64.b64encode((selected.username + ":" + selected.password).encode()).decode()
        return "Basic " + value
    if kind.casefold() != "digest" or len(raw) > 4096:
        raise RtspError("auth_failed")
    items = {}
    for entry in parse_http_list(raw):
        for key, value in parse_keqv_list([entry]).items():
            key = key.casefold()
            if key in items:
                raise RtspError("auth_failed")
            items[key] = value
    realm, nonce = items.get("realm"), items.get("nonce")
    algorithm = items.get("algorithm", "MD5").upper()
    if (
        not isinstance(realm, str)
        or not nonce
        or algorithm not in {"MD5", "SHA-256", "MD5-SESS", "SHA-256-SESS"}
    ):
        raise RtspError("auth_failed")
    qop = items.get("qop")
    if qop is not None and "auth" not in [item.strip().casefold() for item in qop.split(",")]:
        raise RtspError("auth_failed")

    def digest(value):
        return hashlib.new(
            "md5" if algorithm.startswith("MD5") else "sha256", value.encode()
        ).hexdigest()

    ha1 = digest(f"{selected.username}:{realm}:{selected.password}")
    ha2 = digest("DESCRIBE:" + selected.request_uri)
    cnonce = secrets.token_hex(16)
    if algorithm.endswith("-SESS"):
        ha1 = digest(f"{ha1}:{nonce}:{cnonce}")
    answer = digest(
        f"{ha1}:{nonce}:00000001:{cnonce}:auth:{ha2}" if qop else f"{ha1}:{nonce}:{ha2}"
    )

    def quoted(value):
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise RtspError("auth_failed")
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    fields = {
        "username": selected.username,
        "realm": realm,
        "nonce": nonce,
        "uri": selected.request_uri,
        "response": answer,
    }
    if "opaque" in items:
        fields["opaque"] = items["opaque"]
    result = "Digest " + ", ".join(key + "=" + quoted(value) for key, value in fields.items())
    result += ", algorithm=" + algorithm
    if qop:
        result += ", qop=auth, nc=00000001, cnonce=" + quoted(cnonce)
    elif algorithm.endswith("-SESS"):
        result += ", cnonce=" + quoted(cnonce)
    return result


class RtspConnector:
    """At most two requests, bounded headers/body, one overall ten-second deadline."""

    async def probe(self, selected: RtspSelection) -> None:
        writer = None
        try:
            async with asyncio.timeout(10):
                context = ssl.create_default_context() if selected.secure else None
                reader, writer = await asyncio.open_connection(
                    selected.host,
                    selected.port,
                    ssl=context,
                    server_hostname=selected.host if context else None,
                    limit=16384,
                )
                authorization = None
                for sequence in (1, 2):
                    headers = (
                        f"DESCRIBE {selected.request_uri} RTSP/1.0\r\nCSeq: {sequence}\r\n"
                        "Accept: application/sdp\r\nUser-Agent: K5-Vision\r\n"
                    )
                    if authorization:
                        headers += "Authorization: " + authorization + "\r\n"
                    writer.write((headers + "\r\n").encode())
                    await writer.drain()
                    raw = await reader.readuntil(b"\r\n\r\n")
                    if len(raw) > 16384:
                        raise RtspError("invalid_response")
                    lines = raw.decode("ascii").split("\r\n")
                    status = re.fullmatch(r"RTSP/1\.0 ([0-9]{3}) [^\r\n]*", lines[0])
                    fields = {}
                    challenges = []
                    for line in lines[1:-2]:
                        key, delimiter, value = line.partition(":")
                        key = key.casefold()
                        if key == "www-authenticate":
                            challenges.append(value.strip())
                            if len(challenges) > 4:
                                raise RtspError("invalid_response")
                            continue
                        if not delimiter or key in fields or not re.fullmatch(r"[a-z0-9-]+", key):
                            raise RtspError("invalid_response")
                        fields[key] = value.strip()
                    if status is None or fields.get("cseq") != str(sequence):
                        raise RtspError("invalid_response")
                    length = fields.get("content-length", "0")
                    if not length.isascii() or not length.isdecimal() or len(length) > 6:
                        raise RtspError("invalid_response")
                    if int(length) > 65536:
                        raise RtspError("invalid_response")
                    body = await reader.readexactly(int(length))
                    code = int(status[1])
                    if code == 401 and sequence == 1:
                        for challenge in sorted(
                            challenges, key=lambda value: not value.lower().startswith("digest ")
                        ):
                            try:
                                authorization = _authorization(challenge, selected)
                                break
                            except RtspError:
                                pass
                        if authorization is None:
                            raise RtspError("auth_failed")
                        continue
                    if code in {401, 403}:
                        raise RtspError("auth_failed")
                    if code != 200:
                        raise RtspError("unreachable")
                    if fields.get("content-type", "").split(";", 1)[0].lower() != "application/sdp":
                        raise RtspError("invalid_response")
                    if not _has_h264_video(body):
                        raise RtspError("unsupported")
                    return
        except RtspError:
            raise
        except (TimeoutError, OSError, ssl.SSLError):
            raise RtspError("unreachable") from None
        except (ValueError, UnicodeError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            raise RtspError("invalid_response") from None
        finally:
            if writer is not None:
                writer.close()
                try:
                    async with asyncio.timeout(1):
                        await writer.wait_closed()
                except (TimeoutError, OSError):
                    pass


def _has_h264_video(body: bytes) -> bool:
    """Require an H.264 RTP mapping in the same declared video media section."""
    text = body.decode("utf-8")
    video_payloads = set()
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.startswith("m="):
            parts = line[2:].split()
            video_payloads = set(parts[3:]) if len(parts) >= 4 and parts[0] == "video" else set()
        match = re.fullmatch(r"a=rtpmap:([0-9]{1,3}) H264/90000", line, re.IGNORECASE)
        if match and 0 <= int(match[1]) <= 127 and match[1] in video_payloads:
            return True
    return False
