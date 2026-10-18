"""Bounded, read-only ONVIF onboarding without WSDL or native XML dependencies.

Protocol patterns adapted from AlexxIT/go2rtc v1.9.14,
commit b5948cfb25404cc5cb37b166ecaa2dca20b11d4b, pkg/onvif/client.go,
envelope.go and helpers.go. See third_party/licenses/go2rtc-MIT.txt.

MIT License
Copyright (c) 2022 Alexey Khit

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import http.client
import ipaddress
import math
import re
import secrets
import select
import socket
import ssl
import threading
import time
import xml.etree.ElementTree as ET
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from urllib.request import parse_http_list, parse_keqv_list
from uuid import uuid4
from xml.sax.saxutils import escape

_DEVICE = "http://www.onvif.org/ver10/device/wsdl"
_MEDIA = "http://www.onvif.org/ver10/media/wsdl"
_MEDIA2 = "http://www.onvif.org/ver20/media/wsdl"
_SOAP = "http://www.w3.org/2003/05/soap-envelope"
_WSD = "http://schemas.xmlsoap.org/ws/2005/04/discovery"
_WSA = "http://schemas.xmlsoap.org/ws/2004/08/addressing"
_MAX_RESPONSE = 1024 * 1024
_CLOCK_READ_SECONDS = 2.0
_CODES = frozenset({"auth_failed", "unreachable", "unsupported", "invalid_response", "busy"})
_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")


@dataclass(frozen=True)
class OnvifClockReading:
    """Read-only clock comparison to this PC, never proof of NTP synchronization."""

    status: str = "unavailable"
    offset_seconds: float | None = None
    measured_at: datetime | None = None
    uncertainty_seconds: float | None = None
    reported_mode: str = "unknown"
    sync_verified: bool = False

    def __post_init__(self) -> None:
        valid = (
            type(self.status) is str
            and self.status in {"measured", "unavailable"}
            and type(self.reported_mode) is str
            and self.reported_mode in {"ntp", "manual", "unknown"}
            and self.sync_verified is False
            and (
                self.measured_at is None
                or (
                    isinstance(self.measured_at, datetime)
                    and self.measured_at.utcoffset() == timedelta(0)
                )
            )
        )
        if self.status == "measured":
            valid = (
                valid
                and self.measured_at is not None
                and type(self.offset_seconds) in {int, float}
                and abs(self.offset_seconds) <= 400_000_000_000
                and math.isfinite(self.offset_seconds)
                and type(self.uncertainty_seconds) in {int, float}
                and 0 <= self.uncertainty_seconds <= 10
                and math.isfinite(self.uncertainty_seconds)
            )
        else:
            valid = valid and self.offset_seconds is None and self.uncertainty_seconds is None
        if not valid:
            raise ValueError("Invalid camera clock reading")

    def public(self) -> dict:
        return {
            "status": self.status,
            "offset_seconds": self.offset_seconds,
            "measured_at": (
                self.measured_at.isoformat(timespec="milliseconds").replace("+00:00", "Z")
                if self.measured_at is not None
                else None
            ),
            "uncertainty_seconds": self.uncertainty_seconds,
            "reported_mode": self.reported_mode,
            "sync_verified": False,
        }


def _utc_now() -> datetime:
    return datetime.now(UTC)


class OnvifError(RuntimeError):
    """Finite diagnostic that contains no remote text or private values."""

    def __init__(self, code: str, *, clock: OnvifClockReading | None = None) -> None:
        self.code = code if code in _CODES else "invalid_response"
        self.clock = clock if isinstance(clock, OnvifClockReading) else None
        super().__init__("Camera connection failed")


@dataclass(frozen=True)
class OnvifProfile:
    token: str
    name: str
    codec: str
    width: int
    height: int
    source_uri: str = field(repr=False)
    clock: OnvifClockReading | None = None


def _ip(value: str) -> str:
    if not isinstance(value, str) or "%" in value:
        raise OnvifError("invalid_response")
    address = ipaddress.ip_address(value)
    if address.is_unspecified or address.is_multicast:
        raise OnvifError("invalid_response")
    return str(address)


def _label(value: str | None, fallback: str) -> str:
    value = (value or "").strip()
    if not value or len(value) > 128 or "://" in value or "@" in value:
        return fallback
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return fallback
    return value


def _local(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _find(element: ET.Element, name: str) -> ET.Element | None:
    return next((item for item in element.iter() if _local(item) == name), None)


def _text(element: ET.Element, name: str) -> str:
    item = _find(element, name)
    return "" if item is None else (item.text or "").strip()


def _xml(body: bytes) -> ET.Element:
    if len(body) > _MAX_RESPONSE or b"\x00" in body:
        raise OnvifError("invalid_response")
    text = body.decode("utf-8-sig")
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)", text, flags=re.IGNORECASE):
        raise OnvifError("invalid_response")
    root = ET.fromstring(text)
    stack = [(root, 0)]
    count = 0
    while stack:
        item, depth = stack.pop()
        count += 1
        if count > 8192 or depth > 48:
            raise OnvifError("invalid_response")
        stack.extend((child, depth + 1) for child in item)
    return root


def _endpoint(value: str, host: str) -> str:
    if not value or len(value) > 4096 or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        raise OnvifError("invalid_response")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or _ip(parsed.hostname or "") != host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not 1
        <= (parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80))
        <= 65535
        or re.search(r"%(?![0-9a-fA-F]{2})", value)
    ):
        raise OnvifError("invalid_response")
    return value


def _stream_uri(value: str, host: str, username: str, password: str) -> str:
    if not value or len(value) > 4096 or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        raise OnvifError("invalid_response")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"rtsp", "rtsps"}
        or not value.startswith(("rtsp://", "rtsps://"))
        or _ip(parsed.hostname or "") != host
        or not 1 <= (554 if parsed.port is None else parsed.port) <= 65535
        or parsed.fragment
        or re.search(r"%(?![0-9a-fA-F]{2})", value)
    ):
        raise OnvifError("invalid_response")
    if parsed.username is None and username:
        address = f"[{host}]" if ":" in host else host
        if parsed.port is not None:
            address += f":{parsed.port}"
        address = f"{quote(username, safe='')}:{quote(password, safe='')}@{address}"
        return urlunsplit((parsed.scheme, address, parsed.path, parsed.query, ""))
    if parsed.username is not None and (not parsed.username or parsed.password is None):
        raise OnvifError("invalid_response")
    return value


class _LimitedReader:
    def __init__(self, stream) -> None:
        self._stream = stream
        self._left = _MAX_RESPONSE

    def _read(self, method: str, size: int = -1):
        size = min(size, self._left + 1) if size >= 0 else self._left + 1
        data = getattr(self._stream, method)(size)
        self._left -= len(data)
        if self._left < 0:
            raise OnvifError("invalid_response")
        return data

    def read(self, size=-1):
        return self._read("read", size)

    def read1(self, size=-1):
        return self._read("read1", size)

    def readline(self, size=-1):
        return self._read("readline", size)

    def __getattr__(self, name):
        return getattr(self._stream, name)


class _SocketReader:
    def __init__(self, wire) -> None:
        self._wire = wire

    def makefile(self, *args, **kwargs):
        return _LimitedReader(self._wire.makefile(*args, **kwargs))


class _Response(http.client.HTTPResponse):
    def __init__(self, sock, *args, **kwargs):
        super().__init__(_SocketReader(sock), *args, **kwargs)


class _Connection(http.client.HTTPConnection):
    response_class = _Response

    def __init__(self, host: str, port: int, secure: bool, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._secure = secure
        self.wire = None

    def connect(self) -> None:
        wire = socket.socket(
            socket.AF_INET6 if ":" in self.host else socket.AF_INET, socket.SOCK_STREAM
        )
        self.wire = self.sock = wire
        wire.settimeout(self.timeout)
        wire.connect((self.host, self.port))
        if self._secure:
            wire = ssl.create_default_context().wrap_socket(
                wire, server_hostname=self.host, do_handshake_on_connect=False
            )
            self.wire = self.sock = wire
            wire.do_handshake()

    def abort(self) -> None:
        wire = self.wire
        if wire is not None:
            try:
                wire.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            wire.close()


def _envelope(
    operation: str, username: str, password: str, *, created_at: datetime | None = None
) -> bytes:
    nonce = secrets.token_bytes(16)
    created = (created_at or _utc_now()).isoformat(timespec="seconds").replace("+00:00", "Z")
    digest = base64.b64encode(
        hashlib.sha1(nonce + created.encode() + password.encode(), usedforsecurity=False).digest()
    ).decode("ascii")
    security = ""
    if username:
        security = (
            '<s:Header><wsse:Security xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/'
            'oasis-200401-wss-wssecurity-secext-1.0.xsd"><wsse:UsernameToken>'
            f"<wsse:Username>{escape(username)}</wsse:Username>"
            '<wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/'
            'oasis-200401-wss-username-token-profile-1.0#PasswordDigest">'
            f"{digest}</wsse:Password>"
            '<wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/'
            'oasis-200401-wss-soap-message-security-1.0#Base64Binary">'
            f"{base64.b64encode(nonce).decode('ascii')}</wsse:Nonce>"
            '<wsu:Created xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/'
            'oasis-200401-wss-wssecurity-utility-1.0.xsd">'
            f"{created}</wsu:Created></wsse:UsernameToken></wsse:Security></s:Header>"
        )
    return (
        f'<s:Envelope xmlns:s="{_SOAP}" xmlns:tds="{_DEVICE}" xmlns:trt="{_MEDIA}" '
        f'xmlns:tr2="{_MEDIA2}" xmlns:tt="http://www.onvif.org/ver10/schema">'
        f"{security}<s:Body>{operation}</s:Body></s:Envelope>"
    ).encode()


class _Client:
    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        deadline: float,
        *,
        require_secure: bool = False,
    ) -> None:
        self.host, self.username, self.password = host, username, password
        self.deadline = deadline
        self.require_secure = require_secure
        self._camera_clock: tuple[datetime, float] | None = None
        self.clock = OnvifClockReading(measured_at=_utc_now())
        self._clock_endpoint: str | None = None
        self._clock_recheck_used = False
        self.challenge: dict[str, str] | None = None
        self.nonce_count = 0

    def synchronize_clock(self, endpoint: str) -> bool:
        """Read the device's UTC clock anonymously without changing either clock.

        This optional PRE_AUTH action has its own short deadline. An older or
        restricted camera can still use the remaining authenticated probe budget.
        """
        started = time.monotonic()
        self._clock_endpoint = endpoint
        self.clock = OnvifClockReading(measured_at=_utc_now())
        remaining = self.deadline - started
        if remaining <= 0:
            return False
        clock_reader = _Client(
            self.host,
            "",
            "",
            started + min(_CLOCK_READ_SECONDS, remaining / 4),
            require_secure=self.require_secure,
        )
        try:
            response = clock_reader.request(
                endpoint,
                "<tds:GetSystemDateAndTime/>",
                _DEVICE,
                "GetSystemDateAndTime",
                authenticated=False,
            )
            mode = {"NTP": "ntp", "Manual": "manual"}.get(
                _text(response, "DateTimeType"), "unknown"
            )
            self.clock = OnvifClockReading(measured_at=_utc_now(), reported_mode=mode)
            utc = _find(response, "UTCDateTime")
            if utc is None:
                return False
            date, clock = _find(utc, "Date"), _find(utc, "Time")
            if date is None or clock is None:
                return False
            camera_time = datetime(
                int(_text(date, "Year")),
                int(_text(date, "Month")),
                int(_text(date, "Day")),
                int(_text(clock, "Hour")),
                int(_text(clock, "Minute")),
                int(_text(clock, "Second")),
                tzinfo=UTC,
            )
            # Refuse a boundary date that cannot advance through this probe.
            camera_time + timedelta(seconds=max(0, self.deadline - started) + 1)
            finished = time.monotonic()
            sampled = (started + finished) / 2
            observed = _utc_now()
            offset = (
                camera_time + timedelta(seconds=finished - sampled) - observed
            ).total_seconds()
            self._camera_clock = camera_time, sampled
            self.clock = OnvifClockReading(
                status="measured",
                offset_seconds=round(offset, 3),
                measured_at=observed,
                uncertainty_seconds=math.ceil((1 + (finished - started) / 2) * 1000) / 1000,
                reported_mode=mode,
            )
            return True
        except (
            OnvifError,
            OSError,
            ValueError,
            OverflowError,
            ET.ParseError,
            http.client.HTTPException,
        ):
            return False

    def _created_at(self) -> datetime | None:
        if self._camera_clock is None:
            return None
        camera_time, sampled = self._camera_clock
        try:
            return camera_time + timedelta(seconds=time.monotonic() - sampled)
        except OverflowError:
            self._camera_clock = None
            return None

    def _authorization(self, path: str) -> str:
        challenge = self.challenge
        if not challenge:
            return ""
        algorithm = challenge.get("algorithm", "MD5").upper()
        base = algorithm.removesuffix("-SESS")
        if base not in {"MD5", "SHA-256"}:
            raise OnvifError("unsupported")
        if any('"' in value or "\\" in value for value in (*challenge.values(), self.username)):
            raise OnvifError("unsupported")
        realm, nonce = challenge.get("realm", ""), challenge.get("nonce", "")
        if not nonce or len(nonce) > 2048:
            raise OnvifError("invalid_response")
        qops = challenge.get("qop", "").split(",")
        if qops != [""] and "auth" not in [item.strip() for item in qops]:
            raise OnvifError("unsupported")

        def digest(value: str) -> str:
            return hashlib.new(
                base.lower().replace("-", ""), value.encode(), usedforsecurity=False
            ).hexdigest()

        cnonce = secrets.token_hex(16)
        self.nonce_count += 1
        nc = f"{self.nonce_count:08x}"
        a1 = digest(f"{self.username}:{realm}:{self.password}")
        if algorithm.endswith("-SESS"):
            a1 = digest(f"{a1}:{nonce}:{cnonce}")
        a2 = digest(f"POST:{path}")
        response = (
            digest(f"{a1}:{nonce}:{nc}:{cnonce}:auth:{a2}")
            if qops != [""]
            else digest(f"{a1}:{nonce}:{a2}")
        )
        fields = [
            f'username="{self.username}"',
            f'realm="{realm}"',
            f'nonce="{nonce}"',
            f'uri="{path}"',
            f'response="{response}"',
            f"algorithm={algorithm}",
        ]
        if qops != [""]:
            fields += ["qop=auth", f"nc={nc}", f'cnonce="{cnonce}"']
        if "opaque" in challenge:
            fields.append(f'opaque="{challenge["opaque"]}"')
        return "Digest " + ", ".join(fields)

    def _changed_clock_after_refresh(self) -> bool:
        previous = self._camera_clock
        previous_uncertainty = self.clock.uncertainty_seconds or 0
        if self._clock_endpoint is None or not self.synchronize_clock(self._clock_endpoint):
            return False
        fresh = self._created_at()
        if fresh is None:
            return False
        try:
            predicted = (
                previous[0] + timedelta(seconds=time.monotonic() - previous[1])
                if previous is not None
                else _utc_now()
            )
        except OverflowError:
            return False
        uncertainty = previous_uncertainty + (self.clock.uncertainty_seconds or 0)
        return abs((fresh - predicted).total_seconds()) > uncertainty

    def request(
        self,
        endpoint: str,
        operation: str,
        namespace: str,
        action: str,
        *,
        authenticated: bool = True,
    ) -> ET.Element:
        def call():
            try:
                return None, self._request(
                    endpoint, operation, namespace, action, authenticated=authenticated
                )
            except OnvifError as error:
                return error.code, None
            except (OSError, http.client.HTTPException):
                return "unreachable", None
            except Exception:
                return "invalid_response", None

        code, result = call()
        if code is None:
            return result
        if (
            code == "auth_failed"
            and authenticated
            and self._clock_endpoint is not None
            and not self._clock_recheck_used
        ):
            self._clock_recheck_used = True
            if self._changed_clock_after_refresh():
                code, result = call()
                if code is None:
                    return result
        raise OnvifError(code, clock=self.clock)

    def _request(
        self,
        endpoint: str,
        operation: str,
        namespace: str,
        action: str,
        *,
        authenticated: bool = True,
    ) -> ET.Element:
        endpoint = _endpoint(endpoint, self.host)
        parsed = urlsplit(endpoint)
        if self.require_secure and parsed.scheme != "https":
            raise OnvifError("invalid_response")
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        for attempt in range(2):
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise OnvifError("unreachable")
            connection = _Connection(
                self.host,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                parsed.scheme == "https",
                remaining,
            )
            timer = threading.Timer(remaining, connection.abort)
            timer.daemon = True
            timer.start()
            response = None
            try:
                headers = {
                    "Content-Type": (
                        f'application/soap+xml; charset=utf-8; action="{namespace}/{action}"'
                    ),
                    "SOAPAction": f'"{namespace}/{action}"',
                    "Connection": "close",
                }
                authorization = self._authorization(path) if authenticated else ""
                if authorization:
                    headers["Authorization"] = authorization
                connection.request(
                    "POST",
                    path,
                    _envelope(
                        operation,
                        self.username if authenticated else "",
                        self.password if authenticated else "",
                        created_at=self._created_at() if authenticated else None,
                    ),
                    headers,
                )
                response = connection.getresponse()
                if response.status in {401, 403}:
                    challenge = response.getheader("WWW-Authenticate", "")
                    if (
                        authenticated
                        and response.status == 401
                        and attempt == 0
                        and challenge.lower().startswith("digest ")
                    ):
                        self.challenge = parse_keqv_list(parse_http_list(challenge[7:]))
                        continue
                    raise OnvifError("auth_failed")
                if response.status in {404, 405, 501}:
                    raise OnvifError("unsupported")
                if response.status not in {200, 400, 500}:
                    raise OnvifError("invalid_response")
                body = bytearray()
                while True:
                    if time.monotonic() >= self.deadline:
                        raise OnvifError("unreachable")
                    chunk = response.read1(65536)
                    if not chunk:
                        break
                    body.extend(chunk)
                root = _xml(bytes(body))
                if root.tag not in {
                    f"{{{_SOAP}}}Envelope",
                    "{http://schemas.xmlsoap.org/soap/envelope/}Envelope",
                }:
                    raise OnvifError("invalid_response")
                body_element = _find(root, "Body")
                if body_element is None:
                    raise OnvifError("invalid_response")
                fault = _find(body_element, "Fault")
                if fault is not None:
                    terms = " ".join(fault.itertext()).lower()
                    if any(
                        value in terms
                        for value in ("notauthorized", "unauthorized", "failedauthentication")
                    ):
                        raise OnvifError("auth_failed")
                    if any(
                        value in terms
                        for value in ("actionnotsupported", "notimplemented", "unsupported")
                    ):
                        raise OnvifError("unsupported")
                    raise OnvifError("invalid_response")
                if response.status != 200 or _find(body_element, action + "Response") is None:
                    raise OnvifError("invalid_response")
                return body_element
            finally:
                timer.cancel()
                if response is not None:
                    response.close()
                connection.abort()
                connection.close()
        raise OnvifError("auth_failed")


def _profiles(root: ET.Element) -> list[tuple[str, str, str, int, int]]:
    results = []
    for profile in root.iter():
        if _local(profile) != "Profiles":
            continue
        if len(results) >= 8:
            break
        token = profile.get("token", "")
        if not _TOKEN.fullmatch(token):
            continue
        encoder = _find(profile, "VideoEncoderConfiguration")
        if encoder is None:
            encoder = _find(profile, "VideoEncoder")
        if encoder is None:
            continue
        try:
            width, height = int(_text(encoder, "Width")), int(_text(encoder, "Height"))
        except ValueError:
            continue
        if not 1 <= width <= 16384 or not 1 <= height <= 16384:
            continue
        codec = _text(encoder, "Encoding").upper().replace(".", "")
        codec = {"JPEG": "MJPEG", "HEVC": "H265"}.get(codec, codec)
        if codec not in {"H264", "H265", "MJPEG"}:
            codec = "UNKNOWN"
        results.append(
            (token, _label(_text(profile, "Name"), "Camera stream"), codec, width, height)
        )
    return results


def _probe(host: str, port: int, secure: bool, username: str, password: str, budget: float):
    host = _ip(host)
    if type(port) is not int or not 1 <= port <= 65535 or type(secure) is not bool:
        raise OnvifError("invalid_response")
    if (
        not isinstance(username, str)
        or not isinstance(password, str)
        or len(username) > 256
        or len(password) > 1024
    ):
        raise OnvifError("invalid_response")
    if any(ord(c) < 32 or ord(c) == 127 for c in username + password):
        raise OnvifError("invalid_response")
    authority = f"[{host}]" if ":" in host else host
    base = f"{'https' if secure else 'http'}://{authority}:{port}"
    client = _Client(host, username, password, time.monotonic() + budget, require_secure=secure)
    try:
        return _probe_profiles(client, base, host, username, password)
    except OnvifError as error:
        raise OnvifError(error.code, clock=client.clock) from None
    except (OSError, http.client.HTTPException):
        raise OnvifError("unreachable", clock=client.clock) from None
    except Exception:
        raise OnvifError("invalid_response", clock=client.clock) from None


def _probe_profiles(client: _Client, base: str, host: str, username: str, password: str):
    device = base + "/onvif/device_service"
    client.synchronize_clock(device)
    media = base + "/onvif/media_service"
    try:
        capabilities = client.request(
            device,
            "<tds:GetCapabilities><tds:Category>All</tds:Category></tds:GetCapabilities>",
            _DEVICE,
            "GetCapabilities",
        )
    except OnvifError as error:
        if error.code != "unsupported":
            raise
    else:
        for element in capabilities.iter():
            if _local(element) == "Media" and _text(element, "XAddr"):
                media = _endpoint(_text(element, "XAddr"), host)
                break
    namespace, prefix = _MEDIA, "trt"
    try:
        profiles = client.request(media, "<trt:GetProfiles/>", namespace, "GetProfiles")
    except OnvifError as error:
        if error.code != "unsupported":
            raise
        media = base + "/onvif/media2_service"
        try:
            services = client.request(
                device,
                "<tds:GetServices><tds:IncludeCapability>false</tds:IncludeCapability></tds:GetServices>",
                _DEVICE,
                "GetServices",
            )
        except OnvifError as service_error:
            if service_error.code != "unsupported":
                raise
        else:
            for element in services.iter():
                if _local(element) == "Service" and _text(element, "Namespace") == _MEDIA2:
                    media = _endpoint(_text(element, "XAddr"), host)
                    break
        namespace, prefix = _MEDIA2, "tr2"
        profiles = client.request(
            media,
            "<tr2:GetProfiles><tr2:Type>All</tr2:Type></tr2:GetProfiles>",
            namespace,
            "GetProfiles",
        )
    results = []
    for token, name, codec, width, height in _profiles(profiles):
        setup = (
            "<trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream><tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>"
            if prefix == "trt"
            else "<tr2:Protocol>RTSP</tr2:Protocol>"
        )
        operation = (
            f"<{prefix}:GetStreamUri>{setup}"
            f"<{prefix}:ProfileToken>{escape(token)}</{prefix}:ProfileToken>"
            f"</{prefix}:GetStreamUri>"
        )
        try:
            response = client.request(media, operation, namespace, "GetStreamUri")
        except OnvifError as error:
            if error.code == "unsupported":
                continue
            raise
        uri = _stream_uri(_text(response, "Uri"), host, username, password)
        results.append(OnvifProfile(token, name, codec, width, height, uri, client.clock))
    if not results:
        raise OnvifError("unsupported")
    return tuple(replace(profile, clock=client.clock) for profile in results)


def _discovery_candidates(body: bytes, sender: str) -> list[dict]:
    root = _xml(body)
    candidates = []
    for match in root.iter():
        if _local(match) != "ProbeMatch":
            continue
        name = ""
        for scope in _text(match, "Scopes").split():
            if scope.startswith("onvif://www.onvif.org/name/"):
                name = unquote(scope.removeprefix("onvif://www.onvif.org/name/"))
                break
        for endpoint in _text(match, "XAddrs").split()[:8]:
            try:
                parsed = urlsplit(endpoint)
                host = _ip(parsed.hostname or "")
                if host != _ip(sender):
                    continue
                _endpoint(endpoint, host)
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
            except (ValueError, OnvifError):
                continue
            candidates.append(
                {
                    "host": host,
                    "port": port,
                    "secure": parsed.scheme == "https",
                    "name": _label(name, host),
                }
            )
    return candidates


def _discover(interface: str | None, budget: float, interface_provider=None) -> list[dict]:
    if interface is not None:
        interfaces = [_ip(interface)]
    else:
        provided = () if interface_provider is None else interface_provider()
        if not isinstance(provided, (tuple, list)) or len(provided) > 16:
            raise OnvifError("invalid_response")
        interfaces = list(dict.fromkeys(_ip(value) for value in provided)) or ["0.0.0.0"]
    if any(":" in address for address in interfaces):
        raise OnvifError("unsupported")
    probe = (
        f'<s:Envelope xmlns:s="{_SOAP}" xmlns:a="{_WSA}" xmlns:d="{_WSD}">'
        f"<s:Header><a:Action>{_WSD}/Probe</a:Action>"
        f"<a:MessageID>{uuid4().urn}</a:MessageID>"
        "<a:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</a:To></s:Header>"
        "<s:Body><d:Probe><d:Types/><d:Scopes/></d:Probe></s:Body></s:Envelope>"
    ).encode()
    deadline = time.monotonic() + budget
    results, seen = [], set()
    with ExitStack() as owned:
        wires = []
        for address in interfaces:
            wire = owned.enter_context(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
            try:
                wire.setblocking(False)
                wire.bind((address, 0))
                wire.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
                if address != "0.0.0.0":
                    wire.setsockopt(
                        socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(address)
                    )
                wire.sendto(probe, ("239.255.255.250", 3702))
            except OSError:
                continue
            wires.append(wire)
        if not wires:
            raise OnvifError("unreachable")
        packets = 0
        while packets < 256 and len(results) < 64:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            readable, _, _ = select.select(wires, [], [], remaining)
            if not readable:
                break
            for wire in readable:
                if packets >= 256 or len(results) >= 64:
                    break
                try:
                    body, sender = wire.recvfrom(65536)
                except BlockingIOError:
                    continue
                packets += 1
                try:
                    candidates = _discovery_candidates(body, sender[0])
                except (OnvifError, ValueError, ET.ParseError, UnicodeError):
                    continue
                for candidate in candidates:
                    key = (candidate["host"], candidate["port"], candidate["secure"])
                    if key not in seen:
                        results.append(candidate)
                        seen.add(key)
                    if len(results) >= 64:
                        break
    return results


class OnvifConnector:
    """Two finite worker slots; cancellation does not free an active socket slot."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 12.0,
        discovery_timeout_seconds: float = 3.0,
        interface_provider=None,
    ):
        if not 0 < timeout_seconds <= 15 or not 0 < discovery_timeout_seconds <= 3:
            raise ValueError("Invalid camera connection budget")
        self._timeout = timeout_seconds
        self._discovery_timeout = discovery_timeout_seconds
        if interface_provider is not None and not callable(interface_provider):
            raise ValueError("Invalid camera interface provider")
        self._interface_provider = interface_provider
        self._lock = threading.Lock()
        self._active = 0
        self._tasks: set[asyncio.Task] = set()

    def _worker(self, function, arguments):
        try:
            try:
                return True, function(*arguments)
            except OnvifError as error:
                return False, (error.code, error.clock)
            except (OSError, http.client.HTTPException):
                return False, ("unreachable", None)
            except Exception:
                return False, ("invalid_response", None)
        finally:
            with self._lock:
                self._active -= 1

    async def _run(self, function, *arguments):
        with self._lock:
            if self._active >= 2:
                raise OnvifError("busy")
            self._active += 1
        try:
            task = asyncio.create_task(asyncio.to_thread(self._worker, function, arguments))
        except BaseException:
            with self._lock:
                self._active -= 1
            raise
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        success, result = await asyncio.shield(task)
        if not success:
            code, clock = result
            raise OnvifError(code, clock=clock)
        return result

    async def discover(self, interface: str | None = None) -> list[dict]:
        return await self._run(
            _discover, interface, self._discovery_timeout, self._interface_provider
        )

    async def probe(
        self, host: str, port: int, secure: bool, username: str, password: str
    ) -> tuple[OnvifProfile, ...]:
        return await self._run(_probe, host, port, secure, username, password, self._timeout)
