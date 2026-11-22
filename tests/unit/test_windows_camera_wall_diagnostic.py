"""Failed wall waits retain finite evidence without changing admission or waits."""

from __future__ import annotations

import ast
import importlib.util
import json
import math
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

_PATH = Path(__file__).parents[1] / "integration/test_windows_camera_video_settings.py"
_SPEC = importlib.util.spec_from_file_location("camera_wall_failure_observation", _PATH)
_CAMERA = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_CAMERA)
_ID = "238456fe-8820-4ee2-8e6e-a87f2e7c58b7"
_SECRET = "rtsp://private-user:PRIVATE-PASSWORD@camera.invalid/live?token=PRIVATE-TOKEN"


def _emitted_failure(diagnostic):
    """Execute the real final formatter/property call, rather than a copied list."""
    tree = ast.parse(_PATH.read_text())
    function = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef)
        and n.name == "test_native_windows_camera_video_settings_and_automatic_wall"
    )
    nodes = []
    for node in ast.walk(function):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "safe" for t in node.targets
        ):
            nodes.append(node)
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            call = node.value
            if (
                isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "safe"
                and call.func.attr == "update"
            ):
                nodes.append(node)
            elif (
                isinstance(call.func, ast.Name)
                and call.func.id == "record_testsuite_property"
                and call.args[0].value == "camera_video_windows_diagnostic"
            ):
                nodes.append(node)
    assert len(nodes) == 3
    properties = {}
    scope = {
        "diagnostic": diagnostic,
        "failed": True,
        "cleanup_failed": False,
        "json": json,
        "record_testsuite_property": properties.__setitem__,
    }
    exec(
        compile(
            ast.Module(sorted(nodes, key=lambda n: n.lineno), type_ignores=[]), str(_PATH), "exec"
        ),
        scope,
    )
    assert list(properties) == ["camera_video_windows_diagnostic"]
    return json.loads(properties["camera_video_windows_diagnostic"])


class Page:
    def __init__(self, raw, error, *, phase):
        self.raw, self.error, self.phase = raw, error, phase
        self.waits, self.snapshots = [], []

    def wait_for_function(self, script, **kwargs):
        if kwargs.get("timeout") == 1000:
            assert kwargs["polling"] == 1000
            value = self.evaluate(script, kwargs["arg"])
            return SimpleNamespace(json_value=lambda: json.dumps(value), dispose=lambda: None)
        self.waits.append((script, kwargs))
        if len(self.waits) == (1 if self.phase == "initial_decode" else 2):
            raise self.error

    def locator(self, selector):
        return SimpleNamespace(evaluate=lambda script: 8)

    def evaluate(self, script, selector):
        self.snapshots.append((script, selector))
        assert "currentSrc" not in script and "src:" not in script and "outerHTML" not in script
        return self.raw


@pytest.mark.parametrize("phase", ["initial_decode", "frame_progress"])
def test_original_timeout_survives_and_finite_fields_reach_actual_failure_property(phase):
    error = TimeoutError(_SECRET)
    page = Page(
        {
            "present": True,
            "visible": True,
            "connected": True,
            "paused": False,
            "ready_state": 1,
            "network_state": 2,
            "error_code": 2,
            "video_width": 0,
            "video_height": 0,
            "decoded_frames": 0,
            "current_time": 0.0,
            "box_width": 434.21,
            "box_height": 220,
            "layout": "9",
            "layout_choice": "auto",
            "video_count": 5,
            "src": _SECRET,
            "currentSrc": _SECRET,
            "error_message": _SECRET,
            "private_body": _SECRET,
        },
        error,
        phase=phase,
    )
    diagnostic = {"stage": "automatic_five_camera_wall", "private_body": _SECRET}
    with pytest.raises(TimeoutError) as caught:
        _CAMERA._decoded_geometry(
            page,
            f'[data-camera-id="{_ID}"] video',
            "red",
            640,
            360,
            diagnostic=diagnostic,
            camera_id=_ID,
            camera_index=3,
        )
    assert caught.value is error
    assert len(page.snapshots) == 1
    assert [entry[1]["timeout"] for entry in page.waits] == (
        [25000] if phase == "initial_decode" else [25000, 10000]
    )
    actual = _emitted_failure(diagnostic)
    snapshot = actual["decoded_media_timeout"]
    assert (
        snapshot["wait_phase"] == phase
        and snapshot["camera_id"] == _ID
        and snapshot["camera_index"] == 3
    )
    assert snapshot["observed"]["video_count"] == 5 and snapshot["observed"]["ready_state"] == 1
    assert snapshot["observed"]["error_code"] == 2 and snapshot["expected_width"] == 640
    assert snapshot["snapshot_available"] is True
    assert all(
        secret not in json.dumps(actual)
        for secret in ("PRIVATE", "camera.invalid", "rtsp://", "private_body", "currentSrc")
    )


def test_failed_snapshot_does_not_replace_original_timeout_or_expose_error_text():
    error = TimeoutError(_SECRET)
    page = Page({}, error, phase="initial_decode")

    def unavailable(*args):
        raise RuntimeError(_SECRET)

    page.evaluate = unavailable
    diagnostic = {}
    with pytest.raises(TimeoutError) as caught:
        _CAMERA._decoded_geometry(page, "#live-video", "red", 640, 360, diagnostic=diagnostic)
    assert caught.value is error
    assert diagnostic["decoded_media_timeout"]["snapshot_available"] is False
    assert diagnostic["decoded_media_timeout"]["snapshot_error"] == "other"
    assert "PRIVATE" not in json.dumps(_emitted_failure(diagnostic))


def test_non_timeout_failure_runs_no_dom_observation():
    error = ValueError(_SECRET)
    page = Page({}, error, phase="initial_decode")
    diagnostic = {}
    with pytest.raises(ValueError) as caught:
        _CAMERA._decoded_geometry(page, "#live-video", "red", 640, 360, diagnostic=diagnostic)
    assert caught.value is error and not page.snapshots and not diagnostic


@pytest.mark.parametrize("bad", [True, -1, math.nan, math.inf, 10**1000, _SECRET, {}, []])
def test_dom_projection_drops_untyped_unbounded_or_private_values(bad):
    value = _CAMERA._finite_media_dom(
        {
            "current_time": bad,
            "video_width": bad,
            "decoded_frames": bad,
            "layout": bad,
            "private_body": _SECRET,
        }
    )
    if bad is True:
        assert value == {}
    assert "PRIVATE" not in json.dumps(value)
    assert "private_body" not in value
    if (
        type(bad) not in (int, float)
        or isinstance(bad, bool)
        or not 0 <= bad <= 10**9
        or not math.isfinite(bad)
    ):
        assert (
            "current_time" not in value
            and "video_width" not in value
            and "decoded_frames" not in value
        )


def test_existing_client_and_owned_worker_projection_excludes_every_private_extra():
    process = object()
    client = SimpleNamespace(
        last_http_status=503,
        transport_failure={
            "exception_type": "TimeoutError",
            "reason_type": "none",
            "elapsed_seconds": 3.12345,
            "url": _SECRET,
        },
        token=_SECRET,
    )
    cohort = SimpleNamespace(
        lock=threading.Lock(),
        processes={(41, 99): process},
        ownership_records={
            (41, 99): {"accepted": True, "role": "service", "kind": "mediamtx", "path": _SECRET}
        },
        failed=False,
        service_stopped=False,
        is_owned_running=lambda value: value is process,
    )
    diagnostic = {
        "stage": "automatic_five_camera_wall",
        **_CAMERA._camera_failure_context(client, cohort),
        "private_body": _SECRET,
    }
    actual = _emitted_failure(diagnostic)
    assert actual["client_last_http_status"] == 503
    assert actual["client_transport_failure"] == {
        "exception_type": "TimeoutError",
        "reason_type": "none",
        "elapsed_seconds": 3.123,
    }
    assert actual["owned_worker_health"]["owned_running_counts"]["mediamtx"] == 1
    assert "PRIVATE" not in json.dumps(actual) and "path" not in json.dumps(actual)


def test_successful_decode_keeps_original_waits_and_performs_no_snapshot():
    class SuccessfulPage:
        def __init__(self):
            self.waits, self.reads = [], 0

        def wait_for_function(self, script, **kwargs):
            self.waits.append((script, kwargs))

        def locator(self, selector):
            def read(script):
                self.reads += 1
                return 8 if self.reads == 1 else {"rgb": [255, 0, 0], "frames": 12}

            return SimpleNamespace(evaluate=read)

        def evaluate(self, *args):
            pytest.fail("A successful decode must not run failure observation")

    page, diagnostic = SuccessfulPage(), {}
    assert (
        _CAMERA._decoded_geometry(page, "#live-video", "red", 640, 360, diagnostic=diagnostic) == 12
    )
    assert diagnostic == {} and [entry[1]["timeout"] for entry in page.waits] == [25000, 10000]


def test_five_tile_call_binds_actual_public_camera_id_and_index(monkeypatch):
    ids = [f"238456fe-8820-4ee2-8e6e-a87f2e7c58b{i}" for i in range(5)]
    diagnostic, observed = {}, []
    monkeypatch.setattr(_CAMERA, "_open_wall", lambda page: None)
    page = SimpleNamespace(
        locator=lambda selector: SimpleNamespace(
            input_value=lambda: "auto", evaluate_all=lambda script: ids
        ),
        wait_for_function=lambda *args, **kwargs: None,
    )

    def decoded(page, selector, color, width, height, **context):
        assert context["diagnostic"] is diagnostic
        observed.append((context["camera_id"], context["camera_index"], width, height))
        return 4

    monkeypatch.setattr(_CAMERA, "_decoded_geometry", decoded)
    assert (
        _CAMERA._wall_probe(
            page,
            ids,
            ["red"] * 5,
            layout="auto",
            expected_size=9,
            expected_players=5,
            browser_helpers=None,
            dimensions={ids[0]: (640, 360)},
            diagnostic=diagnostic,
        )
        == 5
    )
    assert observed == [
        (value, index, 640 if index == 0 else 320, 360 if index == 0 else 240)
        for index, value in enumerate(ids)
    ]


def test_public_observation_timeout_never_replaces_the_original_failure():
    original = TimeoutError(_SECRET)
    page = Page({}, original, phase="initial_decode")
    wait = page.wait_for_function
    observation_calls = []

    def unavailable(expression, **kwargs):
        if kwargs.get("timeout") == 1000:
            observation_calls.append(kwargs)
            raise TimeoutError(_SECRET)
        return wait(expression, **kwargs)

    page.wait_for_function = unavailable
    diagnostic = {}
    with pytest.raises(TimeoutError) as caught:
        _CAMERA._decoded_geometry(page, "#live-video", "red", 640, 360, diagnostic=diagnostic)
    assert caught.value is original and len(observation_calls) == 1
    assert observation_calls[0]["polling"] == 1000
    assert diagnostic["decoded_media_timeout"]["snapshot_available"] is False
    assert diagnostic["decoded_media_timeout"]["snapshot_error"] == "TimeoutError"
    assert "PRIVATE" not in json.dumps(_emitted_failure(diagnostic))


def test_huge_cached_transport_elapsed_is_dropped_without_conversion_overflow():
    client = SimpleNamespace(
        last_http_status=503, transport_failure={"elapsed_seconds": 10**1000, "url": _SECRET}
    )
    cohort = SimpleNamespace(
        lock=threading.Lock(),
        processes={},
        ownership_records={},
        failed=False,
        service_stopped=False,
    )
    context = _CAMERA._camera_failure_context(client, cohort)
    assert context["client_transport_failure"] == {}
    assert "PRIVATE" not in json.dumps(_emitted_failure(context))


def test_actual_outer_failure_handler_retains_original_flag_and_property_when_observer_raises():
    tree = ast.parse(_PATH.read_text())
    function = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef)
        and n.name == "test_native_windows_camera_video_settings_and_automatic_wall"
    )
    handler = next(
        n
        for n in ast.walk(function)
        if isinstance(n, ast.ExceptHandler)
        and isinstance(n.type, ast.Name)
        and n.type.id == "BaseException"
        and any(
            isinstance(x, ast.Expr)
            and isinstance(x.value, ast.Call)
            and isinstance(x.value.func, ast.Name)
            and x.value.func.id == "_failure_note"
            for x in n.body
        )
    )
    diagnostic = {"decoded_media_timeout": {"snapshot_available": False}}

    def unavailable(*args):
        raise OverflowError(_SECRET)

    scope = {
        "diagnostic": diagnostic,
        "error": TimeoutError(_SECRET),
        "failed": False,
        "_failure_note": _CAMERA._failure_note,
        "_camera_failure_context": unavailable,
        "client": None,
        "cohort": None,
    }
    exec(compile(ast.Module(handler.body, type_ignores=[]), str(_PATH), "exec"), scope)
    assert scope["failed"] is True and diagnostic["failure_kind"] == "timeout"
    actual = _emitted_failure(diagnostic)
    assert actual["camera_observation_error"] == "OverflowError" and actual["failed"] is True
    assert "PRIVATE" not in json.dumps(actual)


@pytest.mark.parametrize("encoded", ["x" * 8193, "not-json"])
def test_primitive_snapshot_parse_limits_still_dispose_the_owned_handle(encoded):
    disposed = []
    page = SimpleNamespace(
        wait_for_function=lambda *args, **kwargs: SimpleNamespace(
            json_value=lambda: encoded, dispose=lambda: disposed.append(True)
        )
    )
    with pytest.raises(ValueError):
        _CAMERA._bounded_media_dom_read(page, "argument => ({present:false})", "#live-video")
    assert disposed == [True]


def test_dom_read_error_marker_is_explicitly_unavailable_without_private_exception_text():
    page = Page(
        {"snapshot_read_failed": True, "exception": _SECRET},
        TimeoutError(_SECRET),
        phase="initial_decode",
    )
    diagnostic = {}
    with pytest.raises(TimeoutError):
        _CAMERA._decoded_geometry(page, "#live-video", "red", 640, 360, diagnostic=diagnostic)
    value = _emitted_failure(diagnostic)["decoded_media_timeout"]
    assert value["snapshot_available"] is False and value["snapshot_error"] == "dom_read_failed"
    assert value["observed"] == {"snapshot_read_failed": True}
    assert "PRIVATE" not in json.dumps(value)
