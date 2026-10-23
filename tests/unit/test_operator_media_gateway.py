from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from k5vision.media.mediamtx_provider import MediaMTXCredentials
from k5vision.operator_media_gateway import generate_operator_gateway_configuration


@pytest.fixture
def assets(tmp_path):
    root = tmp_path / "packaged UI with spaces"
    root.mkdir()
    for name in (
        "index.html",
        "app.js",
        "app.css",
        "wall.js",
        "wall.css",
        "timeline.js",
        "timeline.css",
    ):
        (root / name).write_text("fixture", encoding="utf-8")
    return root


def _configuration(assets, **changes):
    values = {
        "public_port": 18080,
        "control_port": 18081,
        "hls_port": 18888,
        "playback_port": 19996,
        "credentials": MediaMTXCredentials("worker", "a" * 48),
        "cdn_secret": "b" * 48,
        "static_root": assets,
    }
    values.update(changes)
    return generate_operator_gateway_configuration(**values)


def test_fixed_gateway_authorizes_each_media_request_before_private_proxy(assets):
    document = json.loads(_configuration(assets))

    def proxies(value):
        if isinstance(value, dict):
            if value.get("handler") == "reverse_proxy":
                yield value
            for child in value.values():
                yield from proxies(child)
        elif isinstance(value, list):
            for child in value:
                yield from proxies(child)

    worker_proxies = list(proxies(document))
    assert len(worker_proxies) == 6
    for proxy in worker_proxies:
        # Every route must retire pooled connections before Uvicorn's 5s
        # idle limit; a failed mutation is never replayed as a workaround.
        transport = proxy["transport"]
        assert transport["keep_alive"] == {"idle_timeout": "2s"}
        assert transport["max_conns_per_host"] == 32
        assert transport["read_timeout"] == "15s" and transport["write_timeout"] == "10s"
        assert not any(key.startswith("lb_try") for key in proxy)
    assert document["admin"] == {"disabled": True}
    server = document["apps"]["http"]["servers"]["operator"]
    assert server["listen"] == ["127.0.0.1:18080"]
    assert server["automatic_https"]["disable"] is True
    assert "logs" not in server
    assert server["routes"][0]["match"][0]["not"][0]["expression"] == (
        "{http.request.hostport} == '127.0.0.1:18080'"
    )
    assert server["routes"][1]["handle"][0]["request"]["delete"] == [
        "Forwarded",
        "X-Forwarded-*",
        "X-K5-*",
    ]
    media_routes = [
        route
        for route in server["routes"]
        if any(
            path in {"/media/live/*", "/media/playback"}
            for matcher in route.get("match", [])
            for path in matcher.get("path", [])
        )
    ]
    assert len(media_routes) == 2
    for route in media_routes:
        assert route["match"][0]["method"] == ["GET"]
        auth = route["handle"][0]
        assert auth["upstreams"] == [{"dial": "127.0.0.1:18081"}]
        assert auth["rewrite"] == {"method": "GET", "uri": "/api/v1/operator/media-auth?"}
        assert auth["headers"]["request"]["set"] == {
            "X-Forwarded-Method": ["{http.request.method}"],
            "X-Forwarded-Uri": ["{http.request.uri}"],
        }
        denied = auth["handle_response"][1]["routes"][0]["handle"][0]
        assert denied["status_code"] == "{http.reverse_proxy.status_code}"
        assert denied["body"] == "Media access denied"
        assert route["handle"][-1]["headers"]["request"]["delete"] == ["Cookie"]
    live, playback = media_routes
    assert live["handle"][-1]["headers"]["request"]["set"]["Authorization"] == [
        "Bearer " + "b" * 48
    ]
    assert playback["handle"][-1]["rewrite"] == {"uri": "/get"}
    assert playback["handle"][-1]["headers"]["request"]["set"]["Authorization"] == [
        "Basic " + base64.b64encode(b"worker:" + b"a" * 48).decode("ascii")
    ]


def test_static_allowlist_and_public_javascript_have_no_directory_or_donor_api_route(assets):
    document = json.loads(_configuration(assets))
    routes = document["apps"]["http"]["servers"]["operator"]["routes"]
    paths = [path for route in routes for m in route.get("match", []) for path in m.get("path", [])]
    assert paths == [
        "/api/*",
        "/media/hls.js",
        "/media/live/*",
        "/media/playback",
        "/",
        "/operator",
        "/operator/",
        "/operator/index.html",
        "/operator/app.css",
        "/operator/app.js",
        "/operator/wall.css",
        "/operator/wall.js",
        "/operator/timeline.css",
        "/operator/timeline.js",
    ]
    javascript = routes[3]["handle"][0]
    assert javascript["rewrite"]["uri"] == "/_k5_assets/hls.min.js"
    assert javascript["headers"]["request"]["delete"] == ["Cookie", "Authorization"]
    assert routes[-1]["handle"] == [{"handler": "static_response", "status_code": 404}]
    assert "browse" not in _configuration(assets)


@pytest.mark.parametrize("field", ["public_port", "control_port", "hls_port", "playback_port"])
@pytest.mark.parametrize("value", [True, 0, -1, 65_536, "8000"])
def test_invalid_ports_are_rejected_before_configuration(assets, field, value):
    with pytest.raises(ValueError, match="ports are invalid"):
        _configuration(assets, **{field: value})


def test_listener_port_collisions_are_rejected(assets):
    with pytest.raises(ValueError, match="distinct"):
        _configuration(assets, playback_port=18888)


def test_default_http_port_permits_the_browser_normalized_host_only(assets):
    document = json.loads(_configuration(assets, public_port=80))
    routes = document["apps"]["http"]["servers"]["operator"]["routes"]
    assert routes[0]["match"][0]["not"][0]["expression"] == (
        "{http.request.hostport} == '127.0.0.1:80' || {http.request.hostport} == '127.0.0.1'"
    )


@pytest.mark.parametrize("secret", ["short", "x" * 257, "x" * 40 + "\n", "{" + "x" * 40])
def test_nonopaque_private_secrets_are_rejected_without_echoing_values(assets, secret):
    with pytest.raises(ValueError) as error:
        _configuration(assets, cdn_secret=secret)
    assert secret not in str(error.value)


@pytest.mark.parametrize("root", ["/tmp/private", Path("relative"), Path("/absent")])
def test_unadmitted_static_roots_are_rejected(assets, root):
    with pytest.raises(ValueError, match="packaged UI"):
        _configuration(assets, static_root=root)


def test_missing_or_placeholder_assets_are_rejected(assets):
    (assets / "app.js").unlink()
    with pytest.raises(ValueError, match="packaged UI"):
        _configuration(assets)
    renamed = assets.with_name("{http.request.host}")
    assets.rename(renamed)
    with pytest.raises(ValueError, match="packaged UI"):
        _configuration(renamed)
