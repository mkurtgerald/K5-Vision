"""Privacy and classifier forwarding for failure-only admin browser observations."""

import ast
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SOURCE = Path(__file__).resolve().parents[2] / "tests/integration/test_operator_admin_browser.py"
SPEC = importlib.util.spec_from_file_location("admin_failure_candidate", SOURCE)
ADMIN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ADMIN)
PRIVATE = "PRIVATE-rtsp://account:password@camera.invalid/user-identity"


def _exception(source, function="_create_user", following=None):
    error = TimeoutError(PRIVATE)
    namespace = {"original": error}
    definitions = (
        "def _create_user():\n    _open_admin()\n"
        "def _open_admin():\n    _set_access()\n"
        "def _set_access():\n    _credential()\n"
        "def _credential():\n    _api()\n"
        "def _api():\n    raise original\n"
        if following
        else f"def {function}():\n    raise original\n"
    )
    exec(compile(definitions, str(source), "exec"), namespace)
    with pytest.raises(TimeoutError) as caught:
        namespace[function]()
    assert caught.value is error
    return error


def test_frames_keep_only_exact_source_public_names_and_last_four_lines():
    error = _exception(SOURCE, following=True)
    before = error.__traceback__
    observed = ADMIN._admin_failure_frames(error)
    assert len(observed) == 1 and observed[0]["relation"] == "current"
    frames = observed[0]["frames"]
    assert [frame["function"] for frame in frames] == [
        "_open_admin",
        "_set_access",
        "_credential",
        "_api",
    ]
    assert all(frame["file"] == "admin" and 1 <= frame["line"] <= 10000 for frame in frames)
    assert error.__traceback__ is before
    serialized = json.dumps(observed)
    assert PRIVATE not in serialized and str(SOURCE) not in serialized


@pytest.mark.parametrize("known_path", [False, True])
def test_frames_reject_foreign_same_basename_and_private_function_names(known_path):
    path = SOURCE if known_path else SOURCE.parent / "private" / SOURCE.name
    function = "private_user_password" if known_path else "_create_user"
    assert ADMIN._admin_failure_frames(_exception(path, function))[0]["frames"] == []


def _page(raw=None, *, closed=False, broken=False):
    calls = []

    def wait(*args, **kwargs):
        calls.append((args, kwargs))
        assert kwargs == {"timeout": 2000}
        if broken:
            raise RuntimeError(PRIVATE)
        return SimpleNamespace(json_value=lambda: raw)

    return SimpleNamespace(is_closed=lambda: closed, wait_for_function=wait), calls


def test_ui_projects_only_boolean_and_bounded_count_fields():
    page, calls = _page(
        {
            "admin_panel_present": True,
            "admin_panel_visible": False,
            "admin_panel_hidden": True,
            "create_user_button_present": True,
            "create_user_button_visible": False,
            "create_user_button_hidden": False,
            "create_user_button_disabled": False,
            "create_user_button_count": 1,
            "user_dialog_visible": False,
            "private_user_id": PRIVATE,
            "text": PRIVATE,
            "url": PRIVATE,
        }
    )
    result = ADMIN._admin_failure_ui(page)
    assert result["available"] is True
    assert result["admin_panel_visible"] is result["create_user_button_visible"] is False
    assert result["create_user_button_count"] == 1
    assert len(calls) == 1
    assert PRIVATE not in json.dumps(result, allow_nan=False)
    assert set(result) == {
        "available",
        "admin_panel_present",
        "admin_panel_visible",
        "admin_panel_hidden",
        "create_user_button_present",
        "create_user_button_visible",
        "create_user_button_hidden",
        "create_user_button_disabled",
        "create_user_button_count",
        "user_dialog_visible",
    }


@pytest.mark.parametrize("value", [True, -1, 257, 2**4096, float("nan"), float("inf"), PRIVATE])
def test_ui_refuses_invalid_counts_and_never_serializes_private_values(value):
    page, _ = _page({"create_user_button_count": value, "admin_panel_visible": PRIVATE})
    result = ADMIN._admin_failure_ui(page)
    assert result["create_user_button_count"] is None
    assert result["admin_panel_visible"] is None
    assert PRIVATE not in json.dumps(result, allow_nan=False)


def test_missing_or_closed_page_does_no_browser_io():
    assert ADMIN._admin_failure_ui(None) == {"available": False}
    page, calls = _page(closed=True)
    assert ADMIN._admin_failure_ui(page) == {"available": False, "page_closed": True}
    assert calls == []


@pytest.mark.parametrize("frame_raises", [False, True])
@pytest.mark.parametrize("ui_raises", [False, True])
def test_observation_failure_preserves_original_classifier_argument(
    monkeypatch, frame_raises, ui_raises
):
    error = _exception(SOURCE)
    before = error.__traceback__
    frame_calls, ui_calls, classifiers = [], [], []

    def frames(actual):
        assert actual is error
        frame_calls.append(actual)
        if frame_raises:
            raise RuntimeError(PRIVATE)
        return [{"file": "admin", "function": "_create_user", "line": 2}]

    def ui(page):
        ui_calls.append(page)
        if ui_raises:
            raise RuntimeError(PRIVATE)
        return {"available": False}

    monkeypatch.setattr(ADMIN, "_admin_failure_frames", frames)
    monkeypatch.setattr(ADMIN, "_admin_failure_ui", ui)
    module = ast.parse(SOURCE.read_text())
    main = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_real_browser_admin_")
    )
    handler = next(
        node
        for node in ast.walk(main)
        if isinstance(node, ast.ExceptHandler) and node.name == "error"
    )

    def classify(actual):
        assert actual is error
        classifiers.append(actual)
        return "browser_timeout"

    namespace = {
        "error": error,
        "page": object(),
        "browser_helpers": SimpleNamespace(_failure_code=classify),
        "_admin_failure_observation": ADMIN._admin_failure_observation,
    }
    exec(
        compile(ast.Module(body=handler.body, type_ignores=[]), "<actual-admin-except>", "exec"),
        namespace,
    )
    assert namespace["failure"] == "browser_timeout" and classifiers == [error]
    assert frame_calls == [error] and len(ui_calls) == 1
    result = namespace["failure_observation"]
    assert result.get("frame_probe_failed", False) is frame_raises
    assert result.get("ui_probe_failed", False) is ui_raises
    assert error.__traceback__ is before and PRIVATE not in json.dumps(result)


def test_outer_probe_error_still_cannot_replace_classifier_result():
    error = TimeoutError(PRIVATE)
    module = ast.parse(SOURCE.read_text())
    main = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_real_browser_admin_")
    )
    handler = next(
        node
        for node in ast.walk(main)
        if isinstance(node, ast.ExceptHandler) and node.name == "error"
    )
    namespace = {
        "error": error,
        "page": None,
        "browser_helpers": SimpleNamespace(
            _failure_code=lambda actual: "browser_timeout" if actual is error else "wrong"
        ),
        "_admin_failure_observation": lambda *args: (_ for _ in ()).throw(RuntimeError(PRIVATE)),
    }
    exec(
        compile(ast.Module(body=handler.body, type_ignores=[]), "<actual-admin-except>", "exec"),
        namespace,
    )
    assert namespace["failure"] == "browser_timeout"
    assert namespace["failure_observation"] == {"probe_failed": True}


def test_finally_timeout_keeps_original_public_assertion_context():
    original = pytest.fail.Exception(PRIVATE)
    cleanup = TimeoutError(PRIVATE)
    namespace = {"original": original, "cleanup": cleanup}
    exec(
        compile(
            "def _require():\n    raise original\n"
            "def _return_to_cameras():\n    raise cleanup\n"
            "def run_admin_journey():\n"
            "    try:\n        _require()\n"
            "    finally:\n        _return_to_cameras()\n",
            str(SOURCE),
            "exec",
        ),
        namespace,
    )
    with pytest.raises(TimeoutError) as caught:
        namespace["run_admin_journey"]()
    assert caught.value is cleanup and cleanup.__context__ is original
    observed = ADMIN._admin_failure_frames(cleanup)
    assert len(observed) == 2
    assert observed[0]["relation"] == "current" and observed[1]["relation"] == "context"
    assert "_return_to_cameras" in [frame["function"] for frame in observed[0]["frames"]]
    assert "_require" in [frame["function"] for frame in observed[1]["frames"]]
    assert PRIVATE not in json.dumps(observed) and str(SOURCE) not in json.dumps(observed)


def test_cause_and_context_cycles_are_bounded_to_three_unique_exceptions():
    errors = [_exception(SOURCE) for _ in range(5)]
    errors[0].__cause__ = errors[1]
    errors[0].__context__ = errors[2]
    errors[1].__context__ = errors[0]
    errors[2].__cause__ = errors[3]
    errors[3].__context__ = errors[4]
    observed = ADMIN._admin_failure_frames(errors[0])
    assert len(observed) == 3
    assert [entry["relation"] for entry in observed] == ["current", "cause", "context"]
    assert all(len(entry["frames"]) <= 4 and entry["depth"] <= 2 for entry in observed)
    assert PRIVATE not in json.dumps(observed)
