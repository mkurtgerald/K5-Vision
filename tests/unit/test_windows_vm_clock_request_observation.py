"""Failure-only observer privacy and exact original-client forwarding."""

from __future__ import annotations

import importlib.util
import json
from email.message import Message
from http.client import IncompleteRead
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def observer():
    path = Path(__file__).parents[1] / "integration/test_windows_vm_clock_qualification.py"
    spec = importlib.util.spec_from_file_location("clock_request_observation", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Client:
    def __init__(self, values):
        self.values = list(values)
        self.calls = []
        self.transport_failure = None

    def get(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        value = self.values.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


def response(body=b'{"spans":[]}', status=200, headers=None):
    return status, body, headers if headers is not None else {"content-type": "application/json"}


def fail_after_get(module, client, *args, **kwargs):
    diagnostic = {}
    error = KeyError("PRIVATE_EXCEPTION_TOKEN")
    with pytest.raises(KeyError) as caught:
        with module._observe_clock_requests(client, diagnostic):
            result = client.get(*args, **kwargs)
            raise error
    assert caught.value is error
    return result, diagnostic["clock_last_request"]


def test_original_call_and_tuple_body_identity_preserved_on_success(observer):
    body = b'{"spans":[{"PRIVATE_TIMESTAMP":"PRIVATE_ROUTE_ID"}]}'
    original = response(body)
    client = Client([original])
    diagnostic = {}
    payload = {"PRIVATE_BODY": "PRIVATE_TOKEN"}
    with observer._observe_clock_requests(client, diagnostic):
        actual = client.get(
            "/api/v1/operator/cameras/PRIVATE_ROUTE_ID/history?start=PRIVATE_DATE",
            data=payload,
            max_bytes=32768,
        )
    assert actual is original and actual[1] is body and actual[2] is original[2]
    assert len(client.calls) == 1 and client.calls[0][1]["data"] is payload
    assert client.calls[0][1]["max_bytes"] == 32768
    assert diagnostic == {} and "get" not in vars(client)


def test_keyword_route_and_original_override_are_restored(observer):
    original = response()
    client = Client([])
    calls = []

    def override(**kwargs):
        calls.append(kwargs)
        return original

    client.get = override
    actual, note = fail_after_get(observer, client, route="/api/v1/operator/status", method="GET")
    assert actual is original and client.get is override
    assert calls == [{"route": "/api/v1/operator/status", "method": "GET"}]
    assert note["route"] == "status"


@pytest.mark.parametrize(
    ("route", "label"),
    [
        ("/api/v1/operator/status", "status"),
        ("/api/v1/operator/cameras", "cameras"),
        ("/api/v1/auth/me", "auth"),
        ("/api/v1/operator/cameras/PRIVATE_ID/history?start=PRIVATE_DATE", "history"),
        ("https://PRIVATE_HOST/api/v1/operator/cameras/PRIVATE_ID/history", "other"),
        ("/api/v1/operator/cameras/PRIVATE_ID/events", "other"),
        (None, "other"),
        (["PRIVATE_ROUTE"], "other"),
    ],
)
def test_only_finite_public_route_labels(observer, route, label):
    assert observer._clock_public_route(route) == label


@pytest.mark.parametrize(
    ("body", "shape", "present", "is_list"),
    [
        (b'{"spans":[]}', "object", True, True),
        (b'{"spans":[{"PRIVATE_DATE":"PRIVATE_ID"}]}', "object", True, True),
        (b'{"PRIVATE_TOKEN":"PRIVATE_VALUE"}', "object", False, False),
        (b'{"spans":null}', "object", True, False),
        (b'{"spans":{}}', "object", True, False),
        (b"[]", "array", False, False),
        (b'"PRIVATE_VALUE"', "scalar", False, False),
        (b"NaN", "scalar", False, False),
        (b"PRIVATE_BODY", "invalid", None, None),
        (b"\xffPRIVATE_BODY", "invalid", None, None),
        (b"", "invalid", None, None),
        (b" " * 32769, "oversized", None, None),
    ],
)
def test_history_shape_without_private_body_data(observer, body, shape, present, is_list):
    client = Client([response(body)])
    actual, note = fail_after_get(
        observer, client, "/api/v1/operator/cameras/PRIVATE_ID/history?start=PRIVATE_DATE"
    )
    assert actual[1] is body and len(client.calls) == 1
    assert note["status"] == 200 and note["body_bytes"] == len(body)
    assert note["json_shape"] == shape
    assert note.get("spans_present") is present and note.get("spans_is_list") is is_list
    assert "PRIVATE" not in json.dumps(note)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b'{"detail":"Operator identity unavailable"}', "identity_unavailable"),
        (b'{"detail":"Camera state unavailable"}', "camera_state_unavailable"),
        (b'{"detail":"Media unavailable"}', "media_unavailable"),
        (b'{"detail":"Playback inventory unavailable"}', "playback_inventory_unavailable"),
        (
            b'{"detail":"Playback requires an aware interval of at most 24 hours"}',
            "invalid_history_interval",
        ),
        (
            b'{"detail":{"code":"roles_unavailable","PRIVATE_ID":"PRIVATE_TOKEN"}}',
            "roles_unavailable",
        ),
        (b'{"detail":"PRIVATE_EXCEPTION"}', "other"),
        (b'{"detail":{"PRIVATE_CODE":"PRIVATE_EXCEPTION"}}', "other"),
        (b'{"detail":["PRIVATE_TOKEN"]}', "other"),
    ],
)
def test_fixed_api_error_codes_only(observer, body, expected):
    client = Client([response(body, status=503)])
    _, note = fail_after_get(observer, client, "/api/v1/operator/status")
    assert note["status"] == 503 and note["api_error_code"] == expected
    assert "PRIVATE" not in json.dumps(note)


@pytest.mark.parametrize(
    ("mime", "expected"),
    [
        ("application/json; charset=utf-8", "application_json"),
        ("text/plain", "text_plain"),
        ("text/html", "text_html"),
        ("PRIVATE_MIME_TOKEN", "other"),
        ("", "missing"),
        ([], "missing"),
    ],
)
def test_only_content_type_enums(observer, mime, expected):
    _, note = fail_after_get(
        observer,
        Client([response(headers={"content-type": mime})]),
        "/api/v1/operator/cameras/PRIVATE_ID/history",
    )
    assert note["content_type"] == expected and "PRIVATE" not in json.dumps(note)


def test_actual_http_message_header_case_insensitivity(observer):
    headers = Message()
    headers["Content-Type"] = "application/json; charset=utf-8"
    original = response(headers=headers)
    actual, note = fail_after_get(
        observer, Client([original]), "/api/v1/operator/cameras/PRIVATE_ID/history"
    )
    assert actual is original and actual[2] is headers
    assert note["content_type"] == "application_json"


def test_get_failure_replaces_prior_success_and_preserves_original_error(observer):
    error = RuntimeError("PRIVATE_TRANSPORT_TOKEN")
    client = Client([response(), error])
    diagnostic = {}
    with pytest.raises(RuntimeError) as caught:
        with observer._observe_clock_requests(client, diagnostic):
            client.get("/api/v1/operator/status")
            client.get("/api/v1/operator/cameras/PRIVATE_ID/history?start=PRIVATE_DATE")
    assert caught.value is error and len(client.calls) == 2 and "get" not in vars(client)
    assert diagnostic["clock_last_request"] == {
        "route": "history",
        "response_received": False,
        "status": None,
        "exception_kind": "RuntimeError",
    }


def test_observer_processing_cannot_replace_tuple_or_phase_error(observer, monkeypatch):
    original = response()
    client = Client([original])

    def bad_note(*args):
        raise RuntimeError("PRIVATE_PROBE_FAILURE")

    monkeypatch.setattr(observer, "_clock_response_note", bad_note)
    actual, note = fail_after_get(observer, client, "/api/v1/operator/status")
    assert actual is original and len(client.calls) == 1
    assert note["response_received"] is True and note["observation_failed"] is True
    assert "PRIVATE" not in json.dumps(note)


def test_original_incomplete_read_and_partial_body_are_not_exposed(observer):
    error = IncompleteRead(b"PRIVATE_PARTIAL_BODY", 50)
    client = Client([error])
    diagnostic = {}
    with pytest.raises(IncompleteRead) as caught:
        with observer._observe_clock_requests(client, diagnostic):
            client.get("/api/v1/operator/cameras/PRIVATE_ID/history")
    assert caught.value is error
    assert diagnostic["clock_last_request"]["exception_kind"] == "IncompleteRead"
    assert "PRIVATE" not in json.dumps(diagnostic)


@pytest.mark.parametrize(
    "seconds", [None, True, float("nan"), float("inf"), -(10**400), 10**400, "PRIVATE_TIME"]
)
def test_existing_transport_projection_rejects_nonfinite_or_private_values(observer, seconds):
    client = SimpleNamespace(
        transport_failure={
            "operation": ["PRIVATE_ROUTE"],
            "exception_type": "PRIVATE_EXCEPTION",
            "reason_type": "PRIVATE_REASON",
            "elapsed_seconds": seconds,
            "PRIVATE_TOKEN": "PRIVATE_VALUE",
        }
    )
    note = observer._clock_transport_note(client)
    assert note == {
        "operation": "other",
        "exception_type": "other",
        "reason_type": "other",
        "elapsed_seconds": None,
    }


def test_existing_transport_public_values_are_retained_without_extras(observer):
    client = SimpleNamespace(
        transport_failure={
            "operation": "other",
            "exception_type": "URLError",
            "reason_type": "TimeoutError",
            "elapsed_seconds": 3.125,
            "PRIVATE_FIELD": "PRIVATE_TOKEN",
        }
    )
    assert observer._clock_transport_note(client) == {
        "operation": "other",
        "exception_type": "URLError",
        "reason_type": "TimeoutError",
        "elapsed_seconds": 3.125,
    }


def test_unknown_exception_name_cannot_expose_private_class_or_text(observer):
    private = type("PRIVATE_EXCEPTION_CLASS", (RuntimeError,), {})
    assert observer._clock_exception_kind(private("PRIVATE_MESSAGE")) == "other"
    assert observer._clock_exception_kind(KeyError("PRIVATE_ID")) == "KeyError"
