"""Privacy, unchanged-failure and real-browser controls for boot observations."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _helper():
    return _load(
        "k5_boot_observer_control",
        Path(__file__).resolve().parents[1] / "integration/installed_boot_diagnostic.py",
    )


class Page:
    def __init__(self):
        self.handlers = {}

    def on(self, name, handler):
        self.handlers[name] = handler

    def remove_listener(self, name, handler):
        assert self.handlers.pop(name) is handler


def test_routes_errors_and_limits_never_emit_private_values(monkeypatch):
    helper = _helper()
    observer = helper.WallBootObserver(Page())
    secret = "generated-password-and-token-do-not-emit"
    request = SimpleNamespace(
        method="POST",
        url=f"http://user:{secret}@10.0.0.1/api/v1/operator/cameras/"
        f"e4fd401d-ae8e-4ac2-9f92-e8287f241532/media-session?token={secret}",
    )
    for _ in range(200):
        observer.event("response", request, secret)
        observer.mark(secret)
        observer.script_error(
            SimpleNamespace(
                name="TypeError", message=secret, stack=f"{secret}/operator/app.js:123:4"
            )
        )
    monkeypatch.setattr(
        helper, "_bounded_session", lambda *_: (_ for _ in ()).throw(TimeoutError())
    )
    original = AssertionError(secret)
    observer.annotate(original)
    note = original.__notes__[0]
    assert secret not in note and "10.0.0.1" not in note and "e4fd401d" not in note
    assert "http://" not in note and "token=" not in note
    payload = json.loads(note.split(": ", 1)[1])
    assert len(payload["events"]) == 128 and len(payload["script_errors"]) == 8
    assert payload["markers"] == []
    assert payload["events"][0]["route"] == "/api/v1/operator/cameras/{camera}/media-session"
    assert payload["events"][0]["status"] is None
    assert len(note.encode()) <= 32768


def test_route_return_and_original_error_survive_observer_faults(monkeypatch):
    helper = _helper()
    observer = helper.WallBootObserver(Page())
    monkeypatch.setattr(observer, "event", lambda *_: (_ for _ in ()).throw(ValueError()))
    route = SimpleNamespace(request=SimpleNamespace(method="GET", url="http://127.0.0.1/operator/"))
    result = object()
    assert observer.intercept(lambda _: result)(route) is result
    original = RuntimeError("generated original error")

    def fail(_route):
        raise original

    with pytest.raises(RuntimeError) as caught:
        observer.intercept(fail)(route)
    assert caught.value is original


def test_malformed_protocol_and_callback_values_are_non_authoritative(monkeypatch):
    helper = _helper()
    page = Page()
    observer = helper.WallBootObserver(page)
    page.handlers["response"](object())
    assert observer.observer_failed
    secret = "private-value-never-emit"
    monkeypatch.setattr(helper, "_bounded_session", lambda *_: object())
    monkeypatch.setattr(
        helper,
        "_bounded_send",
        lambda *_: {
            "result": {
                "value": {
                    "nodes": {"sign-in": {"hidden": secret}},
                    "counts": {"wallVideos": secret},
                    "secret": secret,
                }
            }
        },
    )
    monkeypatch.setattr(helper, "_bounded_detach", lambda *_: None)
    error = AssertionError(secret)
    observer.annotate(error)
    assert secret not in error.__notes__[0]
    payload = json.loads(error.__notes__[0].split(": ", 1)[1])
    assert payload["dom"]["nodes"]["sign-in"]["hidden"] is None
    assert payload["dom"]["counts"]["wallVideos"] is None
    assert "secret" not in payload["dom"]


def test_last_known_message_index_is_preserved_and_unknown_indices_are_rejected():
    helper = _helper()
    last = len(helper._KNOWN_MESSAGES) - 1
    assert helper._KNOWN_MESSAGES[last] == "Username or password is incorrect."
    assert (
        helper.safe_dom({"messages": {"sign-in-message": last}})["messages"]["sign-in-message"]
        == last
    )
    assert (
        helper.safe_dom({"messages": {"sign-in-message": last + 1}})["messages"]["sign-in-message"]
        is None
    )
    assert "__KNOWN_MESSAGES__" not in helper._SNAPSHOT


def test_annotating_and_observer_cleanup_cannot_replace_original_error(monkeypatch):
    helper = _helper()
    page = Page()
    observer = helper.WallBootObserver(page)
    monkeypatch.setattr(helper, "_bounded_session", lambda *_: (_ for _ in ()).throw(ValueError()))

    class OriginalError(AssertionError):
        def add_note(self, _note):
            raise RuntimeError("generated annotation refusal")

    original = OriginalError("generated original failure")
    with pytest.raises(OriginalError) as caught:
        try:
            raise original
        except OriginalError:
            observer.annotate(original)
            raise
        finally:
            observer.close()
    assert caught.value is original and not page.handlers


def test_snapshot_protocol_and_detach_have_finite_caps(monkeypatch):
    helper = _helper()
    observer = helper.WallBootObserver(Page())
    calls = []
    session = object()
    monkeypatch.setattr(
        helper,
        "_bounded_session",
        lambda page, seconds: calls.append(("create", seconds)) or session,
    )

    def send(actual, params, seconds):
        assert actual is session and params["timeout"] == 1000
        assert params["returnByValue"] is True
        calls.append(("evaluate", seconds))
        return {"result": {"value": {"counts": {"wallVideos": 0}}}}

    monkeypatch.setattr(helper, "_bounded_send", send)
    monkeypatch.setattr(
        helper, "_bounded_detach", lambda actual, seconds: calls.append(("detach", seconds))
    )
    observer.annotate(AssertionError())
    assert [name for name, _ in calls] == ["create", "evaluate", "detach"]
    assert all(0 < seconds <= 1.1 for _, seconds in calls)


def test_held_detach_is_cancelled_within_the_protocol_cap():
    helper = _helper()
    cancelled = []

    async def held():
        try:
            await asyncio.Future()
        finally:
            cancelled.append(True)

    session = SimpleNamespace(_impl_obj=SimpleNamespace(detach=held), _sync=asyncio.run)
    with pytest.raises(TimeoutError):
        helper._bounded_detach(session, 0.01)
    assert cancelled == [True]


def test_held_session_creation_is_cancelled_within_the_protocol_cap():
    pytest.importorskip("playwright.sync_api")
    helper = _helper()
    cancelled = []

    async def held(**_kwargs):
        try:
            await asyncio.Future()
        finally:
            cancelled.append(True)

    context = SimpleNamespace(_impl_obj=SimpleNamespace(new_cdp_session=held), _sync=asyncio.run)
    with pytest.raises(TimeoutError):
        helper._bounded_session(SimpleNamespace(context=context), 0.01)
    assert cancelled == [True]


@pytest.mark.skipif(
    os.getenv("K5_OPERATOR_BROWSER_ACCEPTANCE") != "1", reason="Explicit browser acceptance only"
)
def test_actual_initial_wall_assertion_keeps_its_failure_and_safe_note(monkeypatch):
    pytest.importorskip("playwright.sync_api")
    source = _load(
        "k5_actual_boot_assertion_control", Path(__file__).with_name("test_operator_web_ui.py")
    )
    inventory = source._ui_inventory
    monkeypatch.setattr(
        source, "_ui_inventory", lambda account, _cameras, **extra: inventory(account, [], **extra)
    )
    with pytest.raises(AssertionError) as caught:
        source.test_focused_camera_switch_observes_its_own_grant_without_wall_response_ambiguity()
    error = caught.value
    assert "timeout 5000ms" in str(error)
    assert len(error.__notes__) == 1
    payload = json.loads(error.__notes__[0].split(": ", 1)[1])
    assert payload["dom"]["counts"]["wallVideos"] == 0
    assert payload["dom"]["globals"]["K5CameraWall"] is True
    assert payload["assertion_deadline_changed"] is False
    assert payload["request_retries_added"] == 0
    assert any(
        e["route"] == "/api/v1/operator/cameras" and e["status"] == 200 for e in payload["events"]
    )
    assert [row["stage"] for row in payload["markers"]][-1] == "wall_assert_start"
    for private in (
        "generated-human-account-password",
        "generated-observer-session-token",
        "fixture-admin",
    ):
        assert private not in error.__notes__[0]
