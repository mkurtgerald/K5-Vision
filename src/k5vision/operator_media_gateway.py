"""Private configuration for the native operator HTTP data-plane gateway.

K5 authorizes requests; Caddy streams media between browser and MediaMTX. The
returned configuration contains internal credentials and local paths. It belongs
in protected process state, never an operator response, command argument or log.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

from k5vision.media.mediamtx_provider import MediaMTXCredentials

_ASSETS = (
    "index.html",
    "app.css",
    "app.js",
    "wall.css",
    "wall.js",
    "timeline.css",
    "timeline.js",
    "admin.css",
    "admin.js",
)
_AUTH_ROUTE = "/api/v1/operator/media-auth"
_UNTRUSTED_HEADERS = ["Forwarded", "X-Forwarded-*", "X-K5-*"]


def _transport() -> dict:
    return {
        "protocol": "http",
        # Uvicorn closes idle control connections after five seconds. Retire
        # them first, so a mutation cannot race a stale pooled connection.
        "keep_alive": {"idle_timeout": "2s"},
        "dial_timeout": "2s",
        "response_header_timeout": "10s",
        "read_timeout": "15s",
        "write_timeout": "10s",
        "max_conns_per_host": 32,
        "read_buffer_size": 65_536,
        "write_buffer_size": 65_536,
    }


def _proxy(port: int, *, authorization: str | None = None, uri: str | None = None) -> dict:
    handler = {
        "handler": "reverse_proxy",
        "upstreams": [{"dial": f"127.0.0.1:{port}"}],
        "transport": _transport(),
        "flush_interval": -1,
    }
    if uri is not None:
        handler["rewrite"] = {"uri": uri}
    if authorization is not None:
        handler["headers"] = {
            "request": {"delete": ["Cookie"], "set": {"Authorization": [authorization]}}
        }
    return handler


def _authorize(control_port: int) -> dict:
    handler = _proxy(control_port, uri=_AUTH_ROUTE + "?")
    handler["rewrite"]["method"] = "GET"
    handler["headers"] = {
        "request": {
            "set": {
                "X-Forwarded-Method": ["{http.request.method}"],
                "X-Forwarded-Uri": ["{http.request.uri}"],
            }
        }
    }
    # This is the built-in forward_auth response flow: a successful subrequest
    # resumes the original request. Rejections preserve status, with a fixed
    # body instead of copying arbitrary private authorization response content.
    handler["handle_response"] = [
        {"match": {"status_code": [2]}, "routes": [{"handle": [{"handler": "vars"}]}]},
        {
            "routes": [
                {
                    "handle": [
                        {
                            "handler": "static_response",
                            "status_code": "{http.reverse_proxy.status_code}",
                            "headers": {"Cache-Control": ["no-store"]},
                            "body": "Media access denied",
                        }
                    ]
                }
            ]
        },
    ]
    return handler


def generate_operator_gateway_configuration(
    *,
    public_port: int,
    control_port: int,
    hls_port: int,
    playback_port: int,
    credentials: MediaMTXCredentials,
    cdn_secret: str,
    static_root: Path,
) -> str:
    """Generate an enrolled-media gateway with fixed loopback destinations.

    Media authorization is delegated to K5's cookie-authenticated callback on
    every request. The callback owns device and interval admission. No camera
    URI, user-selectable upstream, donor API route or directory browser exists.
    """
    ports = (public_port, control_port, hls_port, playback_port)
    if any(type(port) is not int or not 1 <= port <= 65535 for port in ports):
        raise ValueError("operator gateway ports are invalid")
    if len(set(ports)) != len(ports):
        raise ValueError("operator gateway requires distinct listener ports")
    if not isinstance(credentials, MediaMTXCredentials):
        raise TypeError("operator gateway requires private media credentials")
    credentials.__post_init__()
    if (
        not isinstance(cdn_secret, str)
        or re.fullmatch(r"[A-Za-z0-9_-]{32,256}", cdn_secret) is None
    ):
        raise ValueError("operator gateway requires an opaque internal media secret")
    valid_root = False
    if isinstance(static_root, Path):
        try:
            valid_root = (
                static_root.is_absolute()
                and len(str(static_root)) <= 4096
                and not any(ord(character) < 32 for character in str(static_root))
                and not any(character in str(static_root) for character in "{}")
                and ".." not in static_root.parts
                and not static_root.is_symlink()
                and static_root.is_dir()
                and all(
                    (static_root / name).is_file() and not (static_root / name).is_symlink()
                    for name in _ASSETS
                )
            )
        except OSError:
            pass
    if not valid_root:
        raise ValueError("operator gateway requires an absolute packaged UI directory")

    basic = base64.b64encode(
        f"{credentials.username}:{credentials.password}".encode("ascii")
    ).decode("ascii")
    javascript = _proxy(hls_port, uri="/_k5_assets/hls.min.js")
    javascript["headers"] = {"request": {"delete": ["Cookie", "Authorization"]}}
    host_expression = f"{{http.request.hostport}} == '127.0.0.1:{public_port}'"
    if public_port == 80:
        # Browsers omit the default HTTP port from Host even when the URL
        # explicitly contains it. Both forms still address this one listener.
        host_expression += " || {http.request.hostport} == '127.0.0.1'"
    routes = [
        {
            "match": [{"not": [{"expression": host_expression}]}],
            "handle": [{"handler": "static_response", "status_code": 404}],
            "terminal": True,
        },
        {
            "handle": [
                {"handler": "headers", "request": {"delete": _UNTRUSTED_HEADERS}},
                {
                    "handler": "headers",
                    "response": {
                        "set": {
                            "X-Content-Type-Options": ["nosniff"],
                            "Referrer-Policy": ["no-referrer"],
                        }
                    },
                },
            ]
        },
        {
            "match": [{"path": ["/api/*"]}],
            "handle": [_proxy(control_port)],
            "terminal": True,
        },
        {
            "match": [{"method": ["GET"], "path": ["/media/hls.js"]}],
            "handle": [javascript],
            "terminal": True,
        },
        {
            "match": [{"method": ["GET"], "path": ["/media/live/*"]}],
            "handle": [
                _authorize(control_port),
                {"handler": "rewrite", "strip_path_prefix": "/media/live"},
                _proxy(hls_port, authorization="Bearer " + cdn_secret),
            ],
            "terminal": True,
        },
        {
            "match": [{"method": ["GET"], "path": ["/media/playback"]}],
            "handle": [
                _authorize(control_port),
                _proxy(playback_port, authorization="Basic " + basic, uri="/get"),
            ],
            "terminal": True,
        },
        {
            "match": [{"method": ["GET", "HEAD"], "path": ["/", "/operator"]}],
            "handle": [
                {
                    "handler": "static_response",
                    "status_code": 308,
                    "headers": {"Location": ["/operator/"]},
                }
            ],
            "terminal": True,
        },
        {
            "match": [{"method": ["GET", "HEAD"], "path": ["/operator/", "/operator/index.html"]}],
            "handle": [
                {"handler": "rewrite", "uri": "/index.html"},
                {"handler": "file_server", "root": str(static_root)},
            ],
            "terminal": True,
        },
        {
            "match": [
                {
                    "method": ["GET", "HEAD"],
                    "path": ["/operator/" + name for name in _ASSETS if name != "index.html"],
                }
            ],
            "handle": [
                {"handler": "rewrite", "strip_path_prefix": "/operator"},
                {"handler": "file_server", "root": str(static_root)},
            ],
            "terminal": True,
        },
        {"handle": [{"handler": "static_response", "status_code": 404}], "terminal": True},
    ]
    configuration = {
        "admin": {"disabled": True},
        "apps": {
            "http": {
                "servers": {
                    "operator": {
                        "listen": [f"127.0.0.1:{public_port}"],
                        "automatic_https": {"disable": True},
                        "read_header_timeout": "5s",
                        "idle_timeout": "30s",
                        "max_header_bytes": 16_384,
                        "routes": routes,
                    }
                }
            }
        },
    }
    return json.dumps(configuration, sort_keys=True, indent=2) + "\n"
