"""Failure evidence stays finite and cannot replace or waive acceptance errors."""

import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest


def _load(name):
    path = Path(__file__).resolve().parents[1] / "integration" / (name + ".py")
    spec = importlib.util.spec_from_file_location("diagnostic_" + name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


admin = _load("test_operator_admin_browser")
person = _load("test_operator_person_view_runtime")
desktop = _load("test_windows_desktop_app")
runtime = _load("test_operator_product_runtime")


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_admin_cleanup_preserves_original_failure_and_closes_every_context(
    monkeypatch, cleanup_fails
):
    closed = []

    def close(index):
        closed.append(index)
        if cleanup_fails and index == 1:
            raise RuntimeError("private cleanup detail")

    contexts = [SimpleNamespace(close=lambda i=i: close(i)) for i in range(2)]
    monkeypatch.setattr(
        admin, "_return_to_cameras", lambda _: pytest.fail("Navigated behind modal")
    )
    original = ValueError("original acceptance failure")
    with pytest.raises(ValueError) as caught:
        try:
            raise original
        finally:
            admin._cleanup_admin_journey(None, contexts, primary_failure=True)
    assert caught.value is original
    assert closed == [1, 0]
    assert admin._ADMIN_DENIAL_DIAGNOSTIC["journey_context_cleanup_failed"] is cleanup_fails


def test_admin_cleanup_failure_cannot_turn_success_into_acceptance(monkeypatch):
    def fail():
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(admin, "_return_to_cameras", lambda _: pytest.fail("Missed cleanup error"))
    with pytest.raises(RuntimeError, match="cleanup failed"):
        admin._cleanup_admin_journey(None, [SimpleNamespace(close=fail)], primary_failure=False)


def test_successful_admin_journey_still_requires_return_navigation(monkeypatch):
    seen = []
    monkeypatch.setattr(admin, "_return_to_cameras", seen.append)
    admin._cleanup_admin_journey("page", [], primary_failure=False)
    assert seen == ["page"]


@pytest.mark.parametrize("message,code", list(admin._FAILURE_MESSAGES.items())[:4])
def test_admin_creation_assertions_have_distinct_fixed_codes(message, code):
    with pytest.raises(pytest.fail.Exception):
        admin._require(False, message)
    assert admin._ADMIN_FAILURE_CODE == code
    assert code.startswith("create_")


def test_person_request_evidence_is_bounded_private_and_snapshotted():
    diagnostic = person._SampleRequestDiagnostic()
    diagnostic.route = "/api/v1/operator/cameras/private-id/person-view"
    request = SimpleNamespace(
        method="POST",
        url="https://private-host" + diagnostic.route + "?secret=private-token",
        failure="net::ERR_ABORTED private request detail",
    )
    for _ in range(1005):
        diagnostic.requested(request)
        diagnostic.responded(SimpleNamespace(request=request, status=200))
        diagnostic.failed(request)
    snapshot = diagnostic.snapshot()
    assert snapshot == {
        "requests": 1000,
        "responses": 1000,
        "response_statuses": [200] * 16,
        "failure_counts": {"aborted": 1000},
        "video_events": dict.fromkeys(("waiting", "playing", "pause", "ended", "error"), 0),
        "last_browser_event_state": {"observed": False},
    }
    diagnostic.statuses.clear()
    diagnostic.failures.clear()
    assert len(snapshot["response_statuses"]) == 16
    assert snapshot["failure_counts"] == {"aborted": 1000}
    assert "private" not in json.dumps(snapshot)


def test_person_diagnostics_ignore_other_operations_and_sanitize_unknown_values():
    diagnostic = person._SampleRequestDiagnostic()
    request = SimpleNamespace(method="POST", url="http://private/target", failure="private detail")
    diagnostic.requested(request)
    assert diagnostic.snapshot()["requests"] == 0
    diagnostic.route = "/target"
    diagnostic.requested(SimpleNamespace(method="GET", url=request.url))
    diagnostic.requested(SimpleNamespace(method="POST", url="http://private/other"))
    diagnostic.responded(SimpleNamespace(request=request, status="private status"))
    diagnostic.failed(request)
    assert diagnostic.snapshot() == {
        "requests": 0,
        "responses": 1,
        "response_statuses": [0],
        "failure_counts": {"other": 1},
        "video_events": dict.fromkeys(("waiting", "playing", "pause", "ended", "error"), 0),
        "last_browser_event_state": {"observed": False},
    }


def test_person_browser_events_are_bounded_allowlisted_and_snapshotted():
    diagnostic = person._SampleRequestDiagnostic()
    diagnostic.video_event("private event", False, 4, True, False)
    diagnostic.video_event("waiting", "private value", 4, True, False)
    diagnostic.video_event("waiting", False, 5, True, False)
    assert not diagnostic.snapshot()["last_browser_event_state"]["observed"]
    for _ in range(1005):
        diagnostic.video_event("waiting", False, 2, False, True)
    snapshot = diagnostic.snapshot()
    assert snapshot["video_events"]["waiting"] == 1000
    assert snapshot["last_browser_event_state"] == {
        "observed": True,
        "video_paused": False,
        "video_ready_state": 2,
        "sample_enabled": False,
        "sample_disabled": True,
    }
    diagnostic.video_event("playing", False, 4, True, False)
    assert snapshot["video_events"]["playing"] == 0
    assert "private" not in json.dumps(snapshot)


def test_person_liveness_is_snapshotted_without_any_browser_probe():
    product = SimpleNamespace(
        thread=SimpleNamespace(is_alive=lambda: True),
        failed=False,
        server=SimpleNamespace(started=True),
    )

    snapshot = person._person_state_before_cleanup(product)
    product.thread.is_alive = lambda: False
    assert snapshot == {
        "product_thread_alive": True,
        "product_thread_failed": False,
        "product_server_started": True,
    }


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("private"),
        ConnectionRefusedError("private"),
        ConnectionResetError("private"),
        URLError("private"),
    ],
)
def test_observer_login_keeps_original_transport_failure_without_private_data(error):
    client = runtime._Client(12345)

    def fail(_request, *, timeout):
        assert timeout == 3
        raise error

    client.opener = SimpleNamespace(open=fail)
    diagnostic = {}
    with pytest.raises(RuntimeError, match="Local operator request did not complete"):
        desktop._observer_reauthenticate(
            runtime, client, "private-user", "private-password", diagnostic
        )
    result = diagnostic["observer_login_transport"]
    assert result["operation"] == "login"
    assert result["phase"] == "open"
    assert result["http_response"] == "not_received"
    assert result["elapsed_seconds"] >= 0
    assert "private" not in json.dumps(diagnostic)


def test_http_denial_is_not_reclassified_as_transport_failure():
    client = runtime._Client(12345)

    def denied(_request, *, timeout):
        raise HTTPError("http://private", 401, "private", {}, io.BytesIO(b"{}"))

    client.opener = SimpleNamespace(open=denied)
    diagnostic = {}
    with pytest.raises(AssertionError, match="unexpected status"):
        desktop._observer_reauthenticate(runtime, client, "private", "private", diagnostic)
    assert not diagnostic
    assert client.transport_failure is None
