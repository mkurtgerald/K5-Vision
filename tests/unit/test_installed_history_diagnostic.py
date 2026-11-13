"""Failure-only history metadata retains the real gate and excludes private data."""

from __future__ import annotations

import ast
import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_DESKTOP = _ROOT / "tests/integration/test_windows_desktop_app.py"
_SPEC = importlib.util.spec_from_file_location(
    "installed_history_failure_unit", _ROOT / "tests/integration/installed_history_diagnostic.py"
)
_HELPER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_HELPER)
_RUNTIME_SPEC = importlib.util.spec_from_file_location(
    "history_runtime_failure_unit", _ROOT / "tests/integration/test_operator_product_runtime.py"
)
_RUNTIME = importlib.util.module_from_spec(_RUNTIME_SPEC)
_RUNTIME_SPEC.loader.exec_module(_RUNTIME)
_PRIVATE = "PRIVATE-user-path-rtsp://account:password@camera.invalid/recording"
_REFERENCE = datetime(2026, 11, 13, 1, tzinfo=UTC)


def _actual_gate(client, require, observe, diagnostic):
    if not hasattr(client, "get"):
        client.get = lambda *args, **kwargs: pytest.fail("Unexpected original GET")
    module = ast.parse(_DESKTOP.read_text(encoding="utf-8"))
    journey = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "_desktop_nvr_journey"
    )
    start = next(
        index
        for index, node in enumerate(journey.body)
        if isinstance(node, ast.Assign) and "history_entered_utc" in ast.unparse(node.targets[0])
    )
    finish = next(
        index
        for index in range(start, len(journey.body))
        if isinstance(journey.body[index], ast.Try)
    )
    imports = []

    def helpers(name):
        imports.append(name)
        return SimpleNamespace(observe_failure=observe, response_metadata=_HELPER.response_metadata)

    namespace = {
        "datetime": datetime,
        "UTC": UTC,
        "time": _HELPER.time,
        "client": client,
        "camera_ids": ["generated-id"],
        "camera_directories": [Path(_PRIVATE)],
        "profile": Path(_PRIVATE),
        "diagnostic": diagnostic,
        "_require": require,
        "_helpers": helpers,
    }
    exercise = ast.parse("def _exercise():\n    pass\n").body[0]
    exercise.body = journey.body[start : finish + 1] + ast.parse("return locals()\n").body
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body=[exercise], type_ignores=[])),
            str(_DESKTOP),
            "exec",
        ),
        namespace,
    )
    return namespace["_exercise"](), imports


def test_success_preserves_original_call_truth_and_result_without_loading_observer():
    response, calls, truths, observations = {"spans": [object()]}, [], [], []
    client = SimpleNamespace(json=lambda *args, **kwargs: calls.append((args, kwargs)) or response)
    namespace, imports = _actual_gate(
        client,
        lambda condition, message: truths.append((condition, message)),
        lambda **kwargs: observations.append(kwargs),
        {},
    )
    assert namespace["paused_history"] is response
    assert calls == [(("/api/v1/operator/cameras/generated-id/history",), {})]
    assert truths == [(True, "Pausing recording removed installed recording history")]
    assert imports == observations == []


@pytest.mark.parametrize(
    "observer_error", [None, ValueError(_PRIVATE), KeyboardInterrupt(_PRIVATE)]
)
def test_original_assertion_identity_and_truth_survive_failed_observation(observer_error):
    original = pytest.fail.Exception(_PRIVATE)
    response, calls, observations, diagnostic = {"spans": []}, [], [], {}

    def require(condition, message):
        assert (
            condition is False
            and message == "Pausing recording removed installed recording history"
        )
        raise original

    def observe(**kwargs):
        observations.append(kwargs)
        if observer_error is not None:
            raise observer_error
        return {"span_count": 0}

    with pytest.raises(pytest.fail.Exception) as caught:
        _actual_gate(
            SimpleNamespace(json=lambda *args, **kwargs: calls.append((args, kwargs)) or response),
            require,
            observe,
            diagnostic,
        )
    assert caught.value is original and len(calls) == len(observations) == 1
    assert observations[0]["response"] is response
    assert observations[0]["entered_utc"] <= observations[0]["completed_utc"]
    assert observations[0]["elapsed"] >= 0
    assert diagnostic["paused_history"] == (
        {"probe_failed": True} if observer_error is not None else {"span_count": 0}
    )
    assert _PRIVATE not in json.dumps(diagnostic)


def test_original_request_exception_is_observed_once_without_retry_or_replacement():
    original, calls, observations = RuntimeError(_PRIVATE), [], []

    def request(*args, **kwargs):
        calls.append((args, kwargs))
        raise original

    with pytest.raises(RuntimeError) as caught:
        _actual_gate(
            SimpleNamespace(json=request),
            lambda *args: pytest.fail("Original request must fail before assertion"),
            lambda **kwargs: observations.append(kwargs),
            {},
        )
    assert caught.value is original and len(calls) == len(observations) == 1
    assert observations[0]["response"] is None and observations[0]["error"] is original


def _native_client(response=None, *, failure=None):
    """Use the actual status assertion and JSON parser with a controlled GET."""
    client = _RUNTIME._Client(1234)
    calls, returned = [], []

    def original_get(*args, **kwargs):
        calls.append((args, kwargs))
        if failure is not None:
            raise failure
        return response

    client.get = original_get
    original_json = client.json

    def check_returned_tuple(*args, **kwargs):
        observed_get = client.get

        def identical_result(*get_args, **get_kwargs):
            value = observed_get(*get_args, **get_kwargs)
            returned.append(value)
            assert value is response
            return value

        client.get = identical_result
        try:
            return original_json(*args, **kwargs)
        finally:
            client.get = observed_get

    client.json = check_returned_tuple
    return client, original_get, calls, returned


def test_successful_original_get_tuple_is_identical_and_runs_no_failure_probe():
    response = (200, json.dumps({"spans": [{"private": _PRIVATE}]}).encode(), {_PRIVATE: _PRIVATE})
    client, original, calls, returned = _native_client(response)
    observations, truths, diagnostic = [], [], {}
    namespace, imports = _actual_gate(
        client,
        lambda value, message: truths.append(value),
        lambda **kwargs: observations.append(kwargs),
        diagnostic,
    )
    assert calls == [(("/api/v1/operator/cameras/generated-id/history",), {})]
    assert returned == [response] and returned[0] is response
    assert client.get is original and truths == [True] and observations == [] and diagnostic == {}
    assert namespace["history_response_metadata"] == {"get_completed": True}
    assert namespace["history_original_response"] is None and imports == []


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (503, b'{"detail":"roles_unavailable"}', "roles_unavailable"),
        (503, b'{"detail":"Operator identity unavailable"}', "identity_unavailable"),
        (503, b'{"detail":"Playback inventory unavailable"}', "playback_inventory_unavailable"),
        (401, b'{"detail":"Unauthorized"}', "unauthorized"),
        (403, b'{"detail":"Insufficient operator permission"}', "insufficient_permission"),
        (503, json.dumps({"detail": _PRIVATE, "private": _PRIVATE}).encode(), "other"),
        (503, _PRIVATE.encode(), "other"),
        (503, b"x" * 16_385, "other"),
    ],
)
def test_original_non200_status_assertion_is_preserved_with_safe_metadata(status, body, expected):
    response = (status, body, {_PRIVATE: _PRIVATE})
    client, original, calls, returned = _native_client(response)
    observations, diagnostic = [], {}

    def observe(**kwargs):
        observations.append(kwargs)
        return kwargs["original_response"]

    with pytest.raises(
        AssertionError, match="Operator API returned an unexpected status"
    ) as caught:
        _actual_gate(
            client,
            lambda *args: pytest.fail("HTTP status assertion must fail before the span assertion"),
            observe,
            diagnostic,
        )
    assert len(calls) == len(returned) == len(observations) == 1
    assert returned[0] is response and client.get is original
    assert caught.value is observations[0]["error"]
    assert observations[0]["response"] is None
    assert diagnostic["paused_history"]["http_status"] == status
    assert diagnostic["paused_history"]["error_detail"] == expected
    assert _PRIVATE not in json.dumps(diagnostic, allow_nan=False)


@pytest.mark.parametrize("failure", [TimeoutError(_PRIVATE), RuntimeError(_PRIVATE)])
def test_original_transport_failure_has_no_extra_get_and_restores_original_method(failure):
    client, original, calls, returned = _native_client(failure=failure)
    observations, diagnostic = [], {}

    def observe(**kwargs):
        observations.append(kwargs)
        return kwargs["original_response"]

    with pytest.raises(type(failure)) as caught:
        _actual_gate(client, lambda *args: pytest.fail("Request failed"), observe, diagnostic)
    assert caught.value is failure and observations[0]["error"] is failure
    assert len(calls) == 1 and returned == [] and client.get is original
    assert diagnostic["paused_history"] == {"get_failed": True}
    assert _PRIVATE not in json.dumps(diagnostic)


def test_original_json_parse_failure_is_observed_without_replaying_the_get():
    response = (200, _PRIVATE.encode(), {})
    client, original, calls, returned = _native_client(response)
    observed = []
    with pytest.raises(json.JSONDecodeError) as caught:
        _actual_gate(
            client,
            lambda *args: pytest.fail("JSON parser must fail first"),
            lambda **kwargs: observed.append(kwargs) or kwargs["original_response"],
            {},
        )
    assert len(calls) == len(returned) == len(observed) == 1 and client.get is original
    assert observed[0]["error"] is caught.value and observed[0]["response"] is None
    assert observed[0]["original_response"]["http_status"] == 200


@pytest.mark.parametrize("failure", [ValueError(_PRIVATE), KeyboardInterrupt(_PRIVATE)])
def test_projection_fault_cannot_replace_original_tuple_or_success(monkeypatch, failure):
    response = (200, b'{"spans":[{}]}', {})
    client, original, calls, returned = _native_client(response)

    def broken(*args):
        raise failure

    monkeypatch.setattr(_HELPER, "response_metadata", broken)
    namespace, imports = _actual_gate(
        client,
        lambda value, _: value or pytest.fail("Original span"),
        lambda **kwargs: pytest.fail("No failure probe on success"),
        {},
    )
    assert len(calls) == len(returned) == 1 and returned[0] is response and client.get is original
    assert namespace["history_response_metadata"] == {"get_completed": True}
    assert imports == []


def test_non200_original_exception_survives_a_failure_probe_exception():
    response = (503, b'{"detail":"roles_unavailable"}', {})
    client, original, calls, _ = _native_client(response)
    observed, diagnostic = [], {}

    def broken(**kwargs):
        observed.append(kwargs["error"])
        raise RuntimeError(_PRIVATE)

    with pytest.raises(AssertionError) as caught:
        _actual_gate(client, lambda *args: pytest.fail("Original status"), broken, diagnostic)
    assert caught.value is observed[0] and client.get is original and len(calls) == 1
    assert diagnostic == {"paused_history": {"probe_failed": True}}


def test_original_non200_assertion_survives_a_deferred_projection_exception(monkeypatch):
    response = (503, b'{"detail":"roles_unavailable"}', {})
    client, original, calls, _ = _native_client(response)
    observed, diagnostic = [], {}

    def broken_projection(*args):
        raise RuntimeError(_PRIVATE)

    def observe(**kwargs):
        observed.append(kwargs)
        return kwargs["original_response"]

    monkeypatch.setattr(_HELPER, "response_metadata", broken_projection)
    with pytest.raises(AssertionError) as caught:
        _actual_gate(client, lambda *args: pytest.fail("Original status"), observe, diagnostic)
    assert caught.value is observed[0]["error"] and client.get is original and len(calls) == 1
    assert diagnostic == {"paused_history": {"get_completed": True, "projection_failed": True}}


def test_nontarget_get_keeps_its_original_arguments_tuple_and_no_projection(monkeypatch):
    response = (200, b'{"spans":[{}]}', {})
    client, original, calls, _ = _native_client(response)
    original_json, projections = client.json, []
    original_project = _HELPER.response_metadata

    def project(value):
        projections.append(value)
        return original_project(value)

    def with_other_request(route, **kwargs):
        assert client.get("/unrelated", method="HEAD", max_bytes=17) is response
        return original_json(route, **kwargs)

    monkeypatch.setattr(_HELPER, "response_metadata", project)
    client.json = with_other_request
    _actual_gate(
        client,
        lambda value, _: value or pytest.fail("Original span"),
        lambda **kwargs: pytest.fail("No failure probe"),
        {},
    )
    assert calls == [
        (("/unrelated",), {"method": "HEAD", "max_bytes": 17}),
        (("/api/v1/operator/cameras/generated-id/history",), {}),
    ]
    assert projections == [] and client.get is original


def test_malformed_nested_error_json_retains_original_status_without_private_body():
    body = b'{"detail":' + b"[" * 1100 + json.dumps(_PRIVATE).encode() + b"]" * 1100 + b"}"
    result = _HELPER.response_metadata((503, body, {_PRIVATE: _PRIVATE}))
    assert result["http_status"] == 503 and result["error_detail"] == "other"
    assert _PRIVATE not in json.dumps(result)


def test_original_failed_request_is_never_reported_as_empty_spans(tmp_path):
    arguments = _arguments(tmp_path)
    arguments.update(
        response=None,
        original_response={
            "http_status": 503,
            "error_detail": "roles_unavailable",
            "get_completed": True,
        },
        error=AssertionError(_PRIVATE),
    )
    result = _HELPER.observe_failure(**arguments)
    assert result["history_request"]["http_status"] == 503
    assert result["history_request"]["error_detail"] == "roles_unavailable"
    assert result["history_request"]["span_count"] is None
    assert result["original_error_type"] == "AssertionError"
    assert _PRIVATE not in json.dumps(result, allow_nan=False)


def test_malformed_captured_metadata_is_projected_again_before_output(tmp_path):
    arguments = _arguments(tmp_path)
    arguments.update(
        response=None,
        original_response={
            "http_status": _PRIVATE,
            "error_detail": _PRIVATE,
            "get_completed": _PRIVATE,
            "private": _PRIVATE,
        },
        error=RuntimeError(_PRIVATE),
    )
    result = _HELPER.observe_failure(**arguments)
    assert result["history_request"]["http_status"] is None
    assert result["history_request"]["error_detail"] == "other"
    assert result["history_request"]["get_completed"] is False
    assert _PRIVATE not in json.dumps(result, allow_nan=False)


def test_file_inventory_emits_only_bounded_metadata_without_reading_contents(tmp_path, monkeypatch):
    (tmp_path / "2026-11-13_01-00-00-123456_Z.mp4").write_text(_PRIVATE)
    (tmp_path / (_PRIVATE.replace("/", "_") + ".txt")).write_text(_PRIVATE)
    monkeypatch.setattr(Path, "read_bytes", lambda *args: pytest.fail("Recording content read"))
    result = _HELPER._file_inventory(tmp_path, _HELPER.time.monotonic() + 2, _REFERENCE)
    assert result["files"] == 2 and result["parsed"] == result["unparsed"] == 1
    assert result["entries"][0]["start_minus_request_entry_seconds"] == 0.123456
    raw = json.dumps(result, allow_nan=False)
    assert _PRIVATE not in raw and str(tmp_path) not in raw


def test_file_inventory_caps_entries_and_refuses_symlink_directory(tmp_path):
    for index in range(65):
        (tmp_path / f"2026-11-13_01-00-00-{index:06}_Z.mp4").touch()
    result = _HELPER._file_inventory(tmp_path, _HELPER.time.monotonic() + 2, _REFERENCE)
    assert result["files"] == len(result["entries"]) == 64 and result["over_limit"] is True
    link = SimpleNamespace(is_symlink=lambda: True)
    assert _HELPER._file_inventory(link, _HELPER.time.monotonic() + 2, _REFERENCE) == {
        "directory_unavailable": True
    }


@pytest.mark.parametrize(
    "address",
    [
        "camera.invalid:1234",
        "user:pass@127.0.0.1:1234",
        "127.0.0.1:1234/private",
        "127.0.0.1:1234?private",
        "127.0.0.1:1234#private",
        "[::1]:1234",
    ],
)
def test_private_config_cannot_select_nonlocal_or_credential_bearing_endpoint(address):
    with pytest.raises(ValueError):
        _HELPER._endpoint(address)


def _arguments(tmp_path):
    directory = tmp_path / ("k5_" + "a" * 64)
    directory.mkdir()
    return {
        "client": SimpleNamespace(token=_PRIVATE, origin="http://127.0.0.1:1236"),
        "profile": tmp_path,
        "directory": directory,
        "entered_utc": _REFERENCE,
        "completed_utc": _REFERENCE,
        "elapsed": 0.1,
        "response": {"spans": []},
    }


def _configuration(args, address="127.0.0.1:1234"):
    identity = args["profile"] / "identity"
    identity.mkdir()
    config = {
        "paths": {
            args["directory"].name: {"record": _PRIVATE, "recordPath": _PRIVATE, "source": _PRIVATE}
        },
        "authInternalUsers": [{"user": "fixture", "pass": _PRIVATE}],
        "apiAddress": address,
        "playbackAddress": "127.0.0.1:1235",
    }
    (identity / "operator-mediamtx.json").write_text(json.dumps(config))


def test_unavailable_config_preserves_original_request_metadata_and_emits_no_private_data(tmp_path):
    args = _arguments(tmp_path)
    result = _HELPER.observe_failure(**args)
    assert result["config_unavailable"] is True
    assert result["history_request"]["span_count"] == 0
    assert result["request_retries_added"] == 0
    raw = json.dumps(result, allow_nan=False)
    assert _PRIVATE not in raw and str(tmp_path) not in raw


def test_private_native_fields_are_projected_and_storage_policy_is_preserved(tmp_path, monkeypatch):
    args = _arguments(tmp_path)
    _configuration(args)
    replies = iter(
        [
            ({"status": 200}, {"record": _PRIVATE, "recordPath": _PRIVATE, "source": _PRIVATE}),
            ({"status": 404}, {"error": _PRIVATE}),
            (
                {"status": 200},
                {
                    "server_utc": _PRIVATE,
                    "worker_status": "disk_full",
                    "clock": {
                        "status": _PRIVATE,
                        "change_count": True,
                        "last_step_seconds": float("nan"),
                    },
                    "storage": {
                        "reason": "free_space",
                        "recording_allowed": False,
                        "recording_bytes": 1234,
                        "free_bytes": 1024,
                        "private": _PRIVATE,
                    },
                    "max_recording_bytes": 2048,
                    "private": _PRIVATE,
                },
            ),
            (
                {"status": 200},
                {
                    "quota_bytes": 2048,
                    "minimum_free_bytes": 1024,
                    "retention_seconds": 86400,
                    "revision": 2,
                    "recording_allowed": False,
                    "inspection_available": True,
                    "recording_bytes": 1234,
                    "free_bytes": 1024,
                    "recording_directory": _PRIVATE,
                },
            ),
        ]
    )
    calls = []

    def bounded(*values):
        calls.append(values)
        return next(replies)

    monkeypatch.setattr(_HELPER, "_bounded_json", bounded)
    result = _HELPER.observe_failure(**args)
    assert len(calls) == 4
    assert result["persistent_config"]["record"] is result["native_path_config"]["record"] is None
    assert result["native_list"]["error"] == "other"
    assert result["backend_status"]["storage"] == {
        "reason": "free_space",
        "recording_allowed": False,
        "recording_bytes": 1234,
        "free_bytes": 1024,
    }
    assert result["recording_policy"]["quota_bytes"] == 2048
    assert result["recording_policy"]["retention_seconds"] == 86400
    raw = json.dumps(result, allow_nan=False)
    assert _PRIVATE not in raw and str(tmp_path) not in raw


@pytest.mark.parametrize(
    "value", [True, -1, 2**4096, float("inf"), float("nan"), _PRIVATE, {_PRIVATE: _PRIVATE}]
)
def test_malformed_storage_and_clock_scalars_cannot_escape(value):
    result = {
        "status": _HELPER._status_snapshot(
            {
                "worker_status": value,
                "server_utc": value,
                "clock": {"status": value, "last_step_seconds": value, "change_count": value},
                "storage": {
                    "recording_allowed": value,
                    "free_bytes": value,
                    "recording_bytes": value,
                },
            }
        ),
        "policy": _HELPER._policy_snapshot(
            {
                key: value
                for key in (
                    "quota_bytes",
                    "minimum_free_bytes",
                    "retention_seconds",
                    "free_bytes",
                    "recording_bytes",
                    "inspection_available",
                )
            }
        ),
    }
    raw = json.dumps(result, allow_nan=False)
    assert _PRIVATE not in raw
    assert result["status"]["storage"]["free_bytes"] is None
    assert result["policy"]["quota_bytes"] is None


def test_refused_private_endpoint_prevents_all_network_requests(tmp_path, monkeypatch):
    args = _arguments(tmp_path)
    _configuration(args, "camera.invalid:1234")
    monkeypatch.setattr(_HELPER, "_bounded_json", lambda *args: pytest.fail("Unadmitted endpoint"))
    result = _HELPER.observe_failure(**args)
    assert result["native_observation_refused"] is True
    assert _PRIVATE not in json.dumps(result)


class _Response:
    code = 200

    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def read1(self, limit):
        return next(self.chunks, b"")[:limit]


def test_http_body_and_read_budget_are_bounded_and_response_is_closed(monkeypatch):
    clock, opens = [0.0], []
    response = _Response([b" "] * 10)
    original = response.read1

    def read(limit):
        clock[0] += 1
        return original(limit)

    response.read1 = read
    monkeypatch.setattr(_HELPER.time, "monotonic", lambda: clock[0])
    opener = SimpleNamespace(open=lambda *args, **kwargs: opens.append(kwargs) or response)
    result = _HELPER._bounded_json(opener, "http://127.0.0.1:1234", {}, 2.5)
    assert result == ({"status": 200, "budget_exhausted": True}, None)
    assert len(opens) == 1 and opens[0]["timeout"] == 0.35 and response.closed
    assert _HELPER._bounded_json(opener, "http://127.0.0.1:1234", {}, 2.5) == (
        {"budget_exhausted": True},
        None,
    )
    assert len(opens) == 1


@pytest.mark.parametrize("payload", [b"x" * 131_073, _PRIVATE.encode()])
def test_oversized_or_invalid_json_body_never_enters_metadata(payload):
    response = _Response([payload[:65_536], payload[65_536:131_072], payload[131_072:]])
    result, value = _HELPER._bounded_json(
        SimpleNamespace(open=lambda *args, **kwargs: response),
        "http://127.0.0.1:1234",
        {},
        _HELPER.time.monotonic() + 2.5,
    )
    assert value is None and response.closed
    assert _PRIVATE not in json.dumps(result)
    assert result.get("body_over_limit", False) is (len(payload) > 131_072)


def test_transport_exception_and_redirect_target_are_never_emitted():
    def broken(*args, **kwargs):
        raise RuntimeError(_PRIVATE)

    result = _HELPER._bounded_json(
        SimpleNamespace(open=broken), "http://127.0.0.1:1234", {}, _HELPER.time.monotonic() + 2.5
    )
    assert result == ({"transport": "other"}, None)
    assert _HELPER._NoRedirect().redirect_request(None, None, 302, _PRIVATE, {}, _PRIVATE) is None
