"""Failure evidence stays finite and cannot replace or waive acceptance errors."""

import ast
import asyncio
import importlib.util
import io
import json
import runpy
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from functools import partial
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


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
def test_installed_event_denials_remain_fatal_and_cannot_be_reclassified(status):
    client = runtime._Client(12345)

    def denied(_request, *, timeout):
        assert timeout == 3
        raise HTTPError("http://private", status, "private", {}, io.BytesIO(b"{}"))

    client.opener = SimpleNamespace(open=denied)
    diagnostic = {}
    with pytest.raises(pytest.fail.Exception, match="refused its authenticated poll"):
        desktop._poll_installed_person_event(client, "/private-camera", diagnostic)
    client.last_http_status = 200
    assert diagnostic == {"analytics_event_poll_status": status}
    assert "private" not in json.dumps(diagnostic)


@pytest.mark.parametrize("status", [502, 503, 504])
def test_installed_event_temporary_gateway_outage_does_not_admit_an_event(status):
    client = runtime._Client(12345)
    client.opener = SimpleNamespace(
        open=lambda _request, timeout: HTTPError(
            "http://private", status, "private", {}, io.BytesIO(b"{}")
        )
    )
    diagnostic = {}
    assert desktop._poll_installed_person_event(client, "/private-camera", diagnostic) is None
    assert diagnostic == {"analytics_event_poll_status": status}


def test_installed_event_transport_cannot_reuse_previous_http_status():
    client = runtime._Client(12345)
    client.last_http_status = 200

    def unavailable(_request, *, timeout):
        assert timeout == 3
        raise ConnectionResetError("private route and token")

    client.opener = SimpleNamespace(open=unavailable)
    diagnostic = {}
    with pytest.raises(RuntimeError, match="Local operator request did not complete"):
        desktop._poll_installed_person_event(client, "/private-camera", diagnostic)
    assert client.last_http_status is None
    assert client.transport_failure["exception_type"] == "ConnectionResetError"
    assert diagnostic == {}
    assert "private" not in json.dumps(client.transport_failure)


def test_installed_event_success_retains_real_event_contract():
    client = runtime._Client(12345)
    valid_event = {
        "camera_id": "synthetic-camera",
        "person_count": 1,
        "confidence": 0.91,
    }
    payload = json.dumps({"events": [valid_event]}).encode()
    client.opener = SimpleNamespace(
        open=lambda _request, timeout: HTTPError(
            "http://private", 200, "private", {}, io.BytesIO(payload)
        )
    )
    diagnostic = {}
    assert (
        desktop._poll_installed_person_event(client, "/private-camera", diagnostic) == valid_event
    )
    assert diagnostic == {"analytics_event_poll_status": 200}


@pytest.mark.parametrize("bad_body", [b"{}", b'{"events": null}', b'{"events": [null]}'])
def test_installed_event_invalid_envelope_fails_closed(bad_body):
    client = runtime._Client(12345)
    client.opener = SimpleNamespace(
        open=lambda _request, timeout: HTTPError(
            "http://private", 200, "private", {}, io.BytesIO(bad_body)
        )
    )
    with pytest.raises(pytest.fail.Exception, match="invalid"):
        desktop._poll_installed_person_event(client, "/private-camera", {})


@pytest.mark.parametrize("status", [200, 401, 503])
def test_installed_event_http_response_clears_prior_transport_failure(status):
    client = runtime._Client(12345)
    responses = iter(
        [
            ConnectionResetError("private"),
            HTTPError("http://private", status, "private", {}, io.BytesIO(b'{"events": []}')),
        ]
    )

    def respond(_request, *, timeout):
        raise next(responses)

    client.opener = SimpleNamespace(open=respond)
    diagnostic = {}
    with pytest.raises(RuntimeError):
        desktop._poll_installed_person_event(client, "/private-camera", diagnostic)
    assert client.transport_failure is not None
    if status == 401:
        with pytest.raises(pytest.fail.Exception, match="refused its authenticated poll"):
            desktop._poll_installed_person_event(client, "/private-camera", diagnostic)
    else:
        assert desktop._poll_installed_person_event(client, "/private-camera", diagnostic) is None
    assert client.transport_failure is None
    assert diagnostic == {"analytics_event_poll_status": status}


def test_installed_sample_response_evidence_distinguishes_first_and_last_reply():
    """Multiple HTTP 429 retries must not disguise initial responsiveness."""
    import inspect

    source = inspect.getsource(desktop._installed_sampled_person_boxes)
    reply = source.split("    def observed_response(response):", 1)[1].split(
        "    def observed_failure(request):", 1
    )[0]
    assert 'traffic.setdefault("first_response_ms", elapsed_ms)' in reply
    assert 'traffic["last_response_ms"] = elapsed_ms' in reply
    assert 'traffic["first_response_ms"] =' not in reply
    # Instrumentation may not make a transient HTTP 429 a passed native box.
    assert "response.status == 200" in source
    assert "timeout=27_000" in source


def test_installed_person_control_readiness_cannot_skip_real_decoding_or_authority():
    """A transient disabled control is retried; a genuinely unavailable view still fails."""
    import inspect

    source = inspect.getsource(desktop._installed_sampled_person_boxes)
    gate = source.split('diagnostic["analytics_sample_stage"] = "control_ready"', 1)[1].split(
        'diagnostic["analytics_sample_stage"] = "native_frame_request"', 1
    )[0]
    assert "timeout=5_000" in gate
    assert "!button.disabled" in gate
    assert "!panel.hidden" in gate
    assert "!video.paused" in gate
    assert "video.readyState >= 2" in gate
    assert "video.videoWidth > 0 && video.videoHeight > 0" in gate
    assert "button.is_enabled()" in gate
    assert '"analytics_sample_control_state"' in gate
    # Only fixed booleans and bounded numbers may leave the Windows runner.
    assert "camera_online_badge:" in gate
    assert "decoded_frames:" in gate
    assert "Math.min(frames, 1000000)" in gate
    for private in ("source_uri", "request.url", "response.body", "screenshot", "getImageData"):
        assert private not in gate


def test_auth_phase_events_are_finite_capped_private_and_snapshotted():
    trace = runtime._AuthPhaseTrace(2, clock=lambda: 1.0)
    trace.begin_login(3)
    for _ in range(600):
        trace.event("client_open", "headers", login=3, status="private-CANARY")
    trace.event("private-CANARY", "headers", login=3)
    snapshot = trace.snapshot()
    assert snapshot["diagnostic_only"] is True and snapshot["product_qualification"] is False
    assert snapshot["product_generation"] == 2 and snapshot["dropped"] == 88
    assert len(snapshot["events"]) == 512
    assert all(
        event
        == {
            "stage": "client_open",
            "edge": "headers",
            "login_ordinal": 3,
            "work_item": 0,
            "elapsed_seconds": 0.0,
            "http_status": 0,
        }
        for event in snapshot["events"]
    )
    trace._events[0]["http_status"] = 401
    assert snapshot["events"][0]["http_status"] == 0
    assert "CANARY" not in json.dumps(snapshot, allow_nan=False)


def test_auth_method_wrapper_preserves_return_exception_and_restoration():
    calls, result = [], object()
    original_error = RuntimeError("private-CANARY")

    def operation(value):
        calls.append(value)
        if value is original_error:
            raise original_error
        return result

    owner = SimpleNamespace(operation=operation)
    trace = runtime._AuthPhaseTrace(1)
    trace.wrap(owner, "operation", "password_verify")
    assert owner.operation("private-CANARY") is result
    assert trace.snapshot()["events"] == []  # An unrelated call is not a login trace.
    token = runtime._AUTH_TRACE_CONTEXT.set((trace, 2))
    try:
        assert owner.operation(result) is result
        with pytest.raises(RuntimeError) as caught:
            owner.operation(original_error)
        assert caught.value is original_error
    finally:
        runtime._AUTH_TRACE_CONTEXT.reset(token)
        trace.restore()
    assert owner.operation is operation and trace.restored
    assert calls == ["private-CANARY", result, original_error]
    assert [event["edge"] for event in trace.snapshot()["events"]] == [
        "entered",
        "returned",
        "entered",
        "raised",
    ]
    assert "CANARY" not in json.dumps(trace.snapshot())


def _auth_trace_application(monkeypatch, *, workers=2):
    from k5vision.services import user_registry

    result, private_error, calls = object(), RuntimeError("private-CANARY"), []

    def derive(*args, **kwargs):
        calls.append("kdf")
        if kwargs.get("fail"):
            raise private_error
        return result

    monkeypatch.setattr(user_registry, "_derive_credential", derive)
    executor = ThreadPoolExecutor(max_workers=workers)

    def verify(*args, **kwargs):
        return user_registry._derive_credential(*args, **kwargs)

    async def async_result(*args, **kwargs):
        return result

    state = SimpleNamespace(
        user_login_executor=executor,
        operator_audit=SimpleNamespace(record=lambda *args, **kwargs: result),
        user_registry=SimpleNamespace(verify_password_snapshot=verify),
        operator_identity_security=SimpleNamespace(
            primary_started=lambda: result,
            password_authenticated=lambda *args: result,
            validate_primary_proof=lambda *args: result,
            store=SimpleNamespace(status=lambda *args: result),
        ),
        user_session_manager=SimpleNamespace(issue=async_result, resolve=async_result),
    )
    return SimpleNamespace(state=state), executor, result, private_error, calls, derive


def test_auth_real_executor_audit_kdf_session_and_asgi_trace_preserve_calls(monkeypatch):
    from k5vision.services import user_registry

    app, executor, result, original_error, calls, derive = _auth_trace_application(monkeypatch)
    trace = runtime._AuthPhaseTrace(2)
    trace.begin_login(3)
    trace.install(app)
    messages = []

    async def application(scope, receive, send):
        loop = asyncio.get_running_loop()
        audit = partial(app.state.operator_audit.record, "auth.login", outcome="started")
        assert await loop.run_in_executor(executor, copy_context().run, audit) is result
        verify = partial(app.state.user_registry.verify_password_snapshot, "private-CANARY")
        assert await loop.run_in_executor(executor, copy_context().run, verify) is result
        assert await app.state.user_session_manager.issue("private-CANARY") is result
        assert await app.state.user_session_manager.resolve("private-CANARY") is result
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"private-CANARY"})

    async def send(message):
        messages.append(message)

    try:
        # Outside this ASGI login's context, even the patched global KDF passes through silently.
        assert user_registry._derive_credential("unrelated-CANARY") is result
        assert trace.snapshot()["events"] == []
        asyncio.run(
            trace.application(
                application,
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/api/v1/auth/login",
                },
                None,
                send,
            )
        )
        assert runtime._AUTH_TRACE_CONTEXT.get() is None
        assert executor._max_workers == 2
    finally:
        executor.shutdown(wait=True)
        trace.restore()
    assert user_registry._derive_credential is derive and trace.restored
    assert calls == ["kdf", "kdf"]
    events = trace.snapshot()["events"]
    assert {event["stage"] for event in events} >= {
        "application",
        "executor",
        "audit_started",
        "password_verify",
        "kdf",
        "session_issue",
        "session_resolve",
    }
    assert all(event["login_ordinal"] == 3 for event in events)
    assert any(event["http_status"] == 200 for event in events)
    assert messages[-1]["body"] == b"private-CANARY"
    assert "CANARY" not in json.dumps(trace.snapshot())


def test_auth_executor_preserves_queued_stage_and_original_exception(monkeypatch):
    import threading

    app, executor, _result, original_error, _calls, _derive = _auth_trace_application(
        monkeypatch, workers=1
    )
    entered, release = threading.Event(), threading.Event()

    def occupy():
        entered.set()
        assert release.wait(2)

    first = executor.submit(occupy)
    assert entered.wait(1)
    trace = runtime._AuthPhaseTrace(1)
    trace.install(app)
    token = runtime._AUTH_TRACE_CONTEXT.set((trace, 1))
    try:
        task = executor.submit(
            copy_context().run,
            partial(app.state.user_registry.verify_password_snapshot, "private-CANARY", fail=True),
        )
        assert [event["edge"] for event in trace.snapshot()["events"]] == ["queued"]
        release.set()
        first.result(timeout=1)
        with pytest.raises(RuntimeError) as caught:
            task.result(timeout=1)
        assert caught.value is original_error
    finally:
        release.set()
        runtime._AUTH_TRACE_CONTEXT.reset(token)
        executor.shutdown(wait=True)
        trace.restore()
    stages = trace.snapshot()["events"]
    assert [v["edge"] for v in stages if v["stage"] == "executor"] == [
        "queued",
        "entered",
        "raised",
    ]
    assert [v["edge"] for v in stages if v["stage"] == "kdf"] == ["entered", "raised"]
    assert "CANARY" not in json.dumps(trace.snapshot())


def test_auth_client_keeps_exact_three_seconds_and_single_failed_request():
    trace = runtime._AuthPhaseTrace(2)
    client = runtime._Client(12345)
    client.auth_trace, client.auth_login_ordinal = trace, 2
    calls = []

    def fail(request, *, timeout):
        calls.append(timeout)
        raise TimeoutError("private-CANARY")

    client.opener = SimpleNamespace(open=fail)
    with pytest.raises(RuntimeError, match="Local operator request did not complete"):
        client.get("/api/v1/auth/login", method="POST", data={"password": "private-CANARY"})
    assert calls == [3]
    assert client.transport_failure["exception_type"] == "TimeoutError"
    assert [event["edge"] for event in trace.snapshot()["events"]] == ["entered", "raised"]
    assert all(event["login_ordinal"] == 3 for event in trace.snapshot()["events"])
    assert "CANARY" not in json.dumps(trace.snapshot())


def test_auth_async_and_asgi_wrappers_keep_original_failure_and_reset_context():
    original_error = ValueError("private-CANARY")

    async def fail(*args, **kwargs):
        raise original_error

    trace = runtime._AuthPhaseTrace(2)
    trace.begin_login(3)
    owner = SimpleNamespace(resolve=fail)
    trace.wrap_async(owner, "resolve", "session_resolve")

    async def application(scope, receive, send):
        await owner.resolve("private-CANARY")

    with pytest.raises(ValueError) as caught:
        asyncio.run(
            trace.application(
                application,
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/api/v1/auth/login",
                },
                None,
                None,
            )
        )
    assert caught.value is original_error and runtime._AUTH_TRACE_CONTEXT.get() is None
    trace.restore()
    assert owner.resolve is fail
    assert [event["edge"] for event in trace.snapshot()["events"]] == [
        "entered",
        "entered",
        "raised",
        "raised",
    ]
    assert "CANARY" not in json.dumps(trace.snapshot())


def test_auth_diagnostic_properties_cannot_be_a_normal_product_receipt(tmp_path):
    verifier = runpy.run_path(
        str(Path(__file__).resolve().parents[2] / "scripts/verify_operator_beta_receipt.py")
    )
    suite = ET.Element("testsuite")
    properties = ET.SubElement(suite, "properties")
    for name, property_name in verifier["_TESTS"]["product"]:
        ET.SubElement(suite, "testcase", name=name)
        group = "donor" if property_name == "native_donor_runtime_receipt" else "product"
        receipt = {**verifier["_COMMON"], **dict.fromkeys(verifier["_TRUE"][group], True)}
        receipt.update(
            portable_ancestor_adjustment=False,
            recording_spans=3,
            playback_decoded_frames=8,
            version="v1.21.2-k5-monotonic1",
            media="locally_generated_h264",
            production_camera_acceptance=False,
            analytics_acceptance=False,
            browser_acceptance=False,
            anonymous_api_status=401,
            anonymous_playback_status=401,
            recording_segments=3,
            playback_decoded_frames_after_restart=[8, 8],
        )
        ET.SubElement(properties, "property", name=property_name, value=json.dumps(receipt))
    path = tmp_path / "diagnostic.xml"
    ET.ElementTree(suite).write(path)
    assert len(verifier["verify"]("product", path)) == 2  # Positive ordinary-receipt control.
    for item in properties:
        item.set("name", "diagnostic_" + item.get("name"))
    ET.ElementTree(suite).write(path)
    with pytest.raises(ValueError):
        verifier["verify"]("product", path)
    syntax = ast.parse(Path(runtime.__file__).read_text())
    emitted = [
        node.args[0].value
        for node in ast.walk(syntax)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "record_testsuite_property"
    ]
    assert emitted and all(name.startswith("diagnostic_") for name in emitted)
