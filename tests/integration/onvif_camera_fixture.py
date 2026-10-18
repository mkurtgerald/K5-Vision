"""A real loopback ONVIF SOAP camera for generated-media browser qualification.

The HTTP server verifies the connector's actual WS-Security PasswordDigest. It
returns a generated local RTSP source only over the device's private SOAP channel;
the operator browser never enters or receives that stream URI. No connector or
application response is mocked by this fixture, and no physical camera is claimed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

_SOAP = "http://www.w3.org/2003/05/soap-envelope"
_DEVICE = "http://www.onvif.org/ver10/device/wsdl"
_MEDIA = "http://www.onvif.org/ver10/media/wsdl"
_SCHEMA = "http://www.onvif.org/ver10/schema"
_WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
_WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
_TOKEN = "generated-h264-profile"


class OnvifCameraFixture:
    """Own bounded actual HTTP requests and never retain SOAP/credential payloads."""

    def __init__(self, *, host: str, stream_uri: str, width=320, height=240) -> None:
        source = urlsplit(stream_uri)
        if host not in {"127.0.0.1", "127.0.0.2"} or (
            source.scheme != "rtsp" or source.hostname != host or source.username is not None
        ):
            raise ValueError("Generated ONVIF fixture must use its exact loopback host")
        if (
            type(width) is not int
            or type(height) is not int
            or not (1 <= width <= 4096 and 1 <= height <= 4096)
        ):
            raise ValueError("Generated ONVIF fixture geometry was refused")
        self.host = host
        self.width, self.height = width, height
        self.username = "generated-camera-user"
        self.password = secrets.token_urlsafe(24)
        self._stream_uri = stream_uri
        self._lock = threading.Lock()
        self._authenticated_requests = {}
        self._authentication_failures = 0
        self._nonces = set()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self):
                super().setup()
                self.connection.settimeout(3)

            def log_message(self, *_args):
                pass

            def reply(self, status, body):
                self.send_response(status)
                self.send_header("Content-Type", "application/soap+xml; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                self.close_connection = True

            def do_POST(self):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 1 <= length <= 65536:
                        raise ValueError
                    raw = self.rfile.read(length)
                    if len(raw) != length or b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
                        raise ValueError
                    document = ET.fromstring(raw)
                    body = document.find("{" + _SOAP + "}Body")
                    if body is None or len(body) != 1:
                        raise ValueError
                    action = body[0].tag.rsplit("}", 1)[-1]
                except (ValueError, ET.ParseError):
                    self.reply(400, b"")
                    return
                if not fixture._authenticate(document):
                    with fixture._lock:
                        fixture._authentication_failures += 1
                    self.reply(403, b"")
                    return
                with fixture._lock:
                    fixture._authenticated_requests[action] = (
                        fixture._authenticated_requests.get(action, 0) + 1
                    )
                payload = fixture._response(action, body[0], self.path)
                if payload is None:
                    self.reply(404, b"")
                    return
                self.reply(200, fixture._envelope(payload))

        self._server = ThreadingHTTPServer((host, 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.05},
            name="generated-onvif-http-camera",
            daemon=True,
        )
        self._thread.start()

    def _authenticate(self, document) -> bool:
        try:
            token = document.find(".//{" + _WSSE + "}UsernameToken")
            username = token.findtext("{" + _WSSE + "}Username")
            password = token.find("{" + _WSSE + "}Password")
            created = token.findtext("{" + _WSU + "}Created")
            nonce_text = token.findtext("{" + _WSSE + "}Nonce")
            if (
                username != self.username
                or password is None
                or not password.attrib.get("Type", "").endswith("#PasswordDigest")
            ):
                return False
            nonce = base64.b64decode(nonce_text, validate=True)
            when = datetime.fromisoformat(created.replace("Z", "+00:00"))
            if (
                len(nonce) < 16
                or when.tzinfo is None
                or abs((datetime.now(UTC) - when).total_seconds()) > 120
            ):
                return False
            expected = base64.b64encode(
                hashlib.sha1(nonce + created.encode() + self.password.encode()).digest()
            ).decode()
            if not hmac.compare_digest(password.text or "", expected):
                return False
            with self._lock:
                if nonce in self._nonces or len(self._nonces) >= 128:
                    return False
                self._nonces.add(nonce)
            return True
        except (AttributeError, TypeError, ValueError):
            return False

    @staticmethod
    def _envelope(payload):
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<s:Envelope xmlns:s="{_SOAP}" xmlns:tds="{_DEVICE}" '
            f'xmlns:trt="{_MEDIA}" xmlns:tt="{_SCHEMA}">'
            "<s:Body>" + payload + "</s:Body></s:Envelope>"
        ).encode()

    def _response(self, action, request, path):
        media = f"http://{self.host}:{self.port}/onvif/media_service"
        if path == "/onvif/device_service" and action == "GetCapabilities":
            return (
                "<tds:GetCapabilitiesResponse><tds:Capabilities><tt:Media>"
                f"<tt:XAddr>{media}</tt:XAddr></tt:Media>"
                "</tds:Capabilities></tds:GetCapabilitiesResponse>"
            )
        if path == "/onvif/device_service" and action == "GetServices":
            return (
                "<tds:GetServicesResponse><tds:Service>"
                f"<tds:Namespace>{_MEDIA}</tds:Namespace><tds:XAddr>{media}</tds:XAddr>"
                "<tds:Version><tt:Major>2</tt:Major><tt:Minor>0</tt:Minor>"
                "</tds:Version></tds:Service></tds:GetServicesResponse>"
            )
        if path == "/onvif/device_service" and action == "GetDeviceInformation":
            return (
                "<tds:GetDeviceInformationResponse>"
                "<tds:Manufacturer>Generated fixture</tds:Manufacturer>"
                "<tds:Model>Generated ONVIF camera</tds:Model>"
                "<tds:FirmwareVersion>1</tds:FirmwareVersion>"
                "<tds:SerialNumber>generated-only</tds:SerialNumber>"
                "<tds:HardwareId>generated-only</tds:HardwareId></tds:GetDeviceInformationResponse>"
            )
        if path == "/onvif/media_service" and action == "GetProfiles":
            return (
                f'<trt:GetProfilesResponse><trt:Profiles token="{_TOKEN}" fixed="true">'
                "<tt:Name>Generated H264 profile</tt:Name>"
                '<tt:VideoEncoderConfiguration token="generated-h264-encoder">'
                "<tt:Name>Generated encoder</tt:Name><tt:Encoding>H264</tt:Encoding>"
                f"<tt:Resolution><tt:Width>{self.width}</tt:Width>"
                f"<tt:Height>{self.height}</tt:Height></tt:Resolution>"
                "</tt:VideoEncoderConfiguration></trt:Profiles></trt:GetProfilesResponse>"
            )
        if path == "/onvif/media_service" and action == "GetStreamUri":
            if request.findtext("{" + _MEDIA + "}ProfileToken") != _TOKEN:
                return None
            return (
                "<trt:GetStreamUriResponse><trt:MediaUri>"
                f"<tt:Uri>{escape(self._stream_uri)}</tt:Uri>"
                "<tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>"
                "<tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>"
                "<tt:Timeout>PT60S</tt:Timeout></trt:MediaUri></trt:GetStreamUriResponse>"
            )
        return None

    def qualified_requests(self) -> bool:
        with self._lock:
            return all(
                self._authenticated_requests.get(action, 0) > 0
                for action in ("GetCapabilities", "GetProfiles", "GetStreamUri")
            )

    def rejected_credentials(self) -> bool:
        with self._lock:
            return self._authentication_failures > 0

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=3)
        if self._thread.is_alive():
            raise RuntimeError("Generated ONVIF fixture did not stop")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
