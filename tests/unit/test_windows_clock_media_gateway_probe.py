"""Failure-only, loopback-scoped clock/media separation witness."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SOURCE = Path(__file__).resolve().parents[1] / "integration" / "test_windows_desktop_app.py"
_SPEC = importlib.util.spec_from_file_location("k5_clock_media_probe", _SOURCE)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
_URL = "http://127.0.0.1:18888/media/live/k5_" + "a" * 64 + "/index.m3u8"
_TOKEN = "synthetic_session_" + "a" * 48


class _Connection:
    instances = []

    def __init__(self, host, port, *, timeout):
        assert (host, port, timeout) == ("127.0.0.1", 18888, 2.0)
        self.closed = False
        self.requested = False
        self.read_called = False
        self.instances.append(self)

    def request(self, method, target, *, headers):
        assert method == "GET"
        assert target == "/media/live/k5_" + "a" * 64 + "/index.m3u8"
        assert headers == {"Cookie": "k5_operator_media=" + _TOKEN}
        self.requested = True

    def getresponse(self):
        assert self.requested
        return type("_Response", (), {"status": 200})()

    def close(self):
        self.closed = True


class _TimeoutConnection(_Connection):
    def getresponse(self):
        raise TimeoutError


def test_clock_probe_classifies_same_gateway_without_reading_or_retaining_media():
    _Connection.instances.clear()
    result = _MODULE._clock_live_gateway_probe(_URL, _TOKEN, dial=_Connection)
    assert result["classification"] == "response"
    assert result["http_status"] == 200
    assert 0 <= result["elapsed_ms"] <= 2000
    assert len(_Connection.instances) == 1
    assert _Connection.instances[0].closed
    assert _TOKEN not in json.dumps(result)
    assert _URL not in json.dumps(result)


def test_clock_probe_classifies_timeout_without_retrying_original_browser_request():
    _TimeoutConnection.instances.clear()
    result = _MODULE._clock_live_gateway_probe(_URL, _TOKEN, dial=_TimeoutConnection)
    assert result["classification"] == "timeout"
    assert 0 <= result["elapsed_ms"] <= 2000
    assert len(_TimeoutConnection.instances) == 1
    assert _TimeoutConnection.instances[0].closed
    assert _TOKEN not in json.dumps(result)


@pytest.mark.parametrize(
    "url, token",
    [
        ("http://example.org:18888/media/live/k5_" + "a" * 64 + "/index.m3u8", _TOKEN),
        ("http://localhost:18888/media/live/k5_" + "a" * 64 + "/index.m3u8", _TOKEN),
        ("https://127.0.0.1:18888/media/live/k5_" + "a" * 64 + "/index.m3u8", _TOKEN),
        (_URL + "?_HLS_msn=100", _TOKEN),
        (_URL + "#f", _TOKEN),
        ("http://127.0.0.1:18888/other", _TOKEN),
        ("http://127.0.0.1:18888/media/live/k5_" + "a" * 64 + "/indexXm3u8", _TOKEN),
        ("http://127.0.0.1:bad/media/live/k5_" + "a" * 64 + "/index.m3u8", _TOKEN),
        (_URL, "short"),
    ],
)
def test_clock_probe_refuses_noncanonical_destination_or_session(url, token):
    def must_not_connect(*args, **kwargs):
        pytest.fail("Invalid probe reached transport")

    assert _MODULE._clock_live_gateway_probe(url, token, dial=must_not_connect) == {
        "classification": "refused"
    }
