"""Browser launch failures preserve finite evidence without weakening handoff."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def desktop():
    source = Path(__file__).parents[1] / "integration/test_windows_desktop_app.py"
    spec = importlib.util.spec_from_file_location("browser_launch_diagnostic_under_test", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_browser_failure_before_page_window_retains_dispatch_and_origin_observation(
    desktop, monkeypatch
):
    watcher = SimpleNamespace(
        ready=SimpleNamespace(wait=lambda **_kwargs: True),
        thread=SimpleNamespace(start=lambda: None),
        action_started=SimpleNamespace(set=lambda: None),
        failed=False,
        argument_matched=True,
        observed={(11, 22)},
        origin_ancestries=[frozenset({(11, 22)})],
        finish=lambda: True,
    )
    monkeypatch.setattr(desktop, "_BrowserLaunchWatch", lambda *_args: watcher)
    monkeypatch.setattr(desktop, "_operator_document_observation", lambda _port: {})
    monkeypatch.setattr(desktop.psutil, "process_iter", lambda **_kwargs: [])
    monkeypatch.setattr(
        desktop.psutil, "Process", lambda _pid: SimpleNamespace(name=lambda: "msedge.exe")
    )
    api = SimpleNamespace(
        windows=lambda: [55],
        pid=lambda _window: 11,
        text=lambda _window: "PRIVATE PAGE TITLE https://private.example/",
        control=lambda *_args: 123,
        click=lambda _window: None,
        api=SimpleNamespace(IsWindowVisible=lambda _window: True),
    )

    def fail_missing_page(condition, _label, **_kwargs):
        assert condition() is None
        pytest.fail("Actual page window readiness deadline")

    monkeypatch.setattr(desktop, "_wait", fail_missing_page)
    diagnostic = {}
    with pytest.raises(pytest.fail.Exception, match="page window readiness deadline"):
        desktop._default_browser_handoff(api, 1, 8000, diagnostic=diagnostic)
    assert diagnostic["browser_handoff_stage"] == "opening_browser"
    assert diagnostic["native_launch_argument_matched"] is True
    assert diagnostic["browser_handoff_cached_origin_records"] == 1
    assert diagnostic["browser_handoff_new_browser_processes"] == 1
    assert diagnostic["browser_handoff_window_counts"]["existing_other"] == 1
    assert "PRIVATE" not in json.dumps(diagnostic)


def test_native_browser_dispatch_projection_keeps_only_allowed_fields(desktop):
    data = {
        "schema_version": 1,
        "browser_dispatch": "failed",
        "browser_attempts": 1,
        "browser_native_error": 1155,
        "private": "PRIVATE PATH AND ERROR MESSAGE",
    }
    api = SimpleNamespace(control_text=lambda *_args: json.dumps(data))
    observed = desktop._desktop_probe(api, 1)
    assert observed["browser_dispatch"] == "failed"
    assert observed["browser_native_error"] == 1155
    assert observed["browser_attempts"] == 1
    assert "PRIVATE" not in json.dumps(observed)
    data.update(browser_dispatch="PRIVATE", browser_attempts=-1, browser_native_error=2**32)
    observed = desktop._desktop_probe(api, 1)
    assert not {"browser_dispatch", "browser_attempts", "browser_native_error"} & observed.keys()


def test_operator_document_observation_verifies_title_without_retaining_html(desktop, monkeypatch):
    requests = []
    response = SimpleNamespace(
        status=200,
        getheader=lambda *_args: "text/html; charset=utf-8",
        read=lambda size: b"<title>K5 Vision - Operator</title><body>PRIVATE DATA</body>",
    )
    connection = SimpleNamespace(
        request=lambda *args, **kwargs: requests.append((args, kwargs)),
        getresponse=lambda: response,
        close=lambda: requests.append("closed"),
    )
    monkeypatch.setattr(desktop.http.client, "HTTPConnection", lambda *_args, **_kwargs: connection)
    observed = desktop._operator_document_observation(8000)
    assert observed == {
        "operator_document_http_status": 200,
        "operator_document_html": True,
        "operator_document_expected_title": True,
    }
    assert requests[0][0] == ("GET", "/operator/") and requests[-1] == "closed"
    assert "PRIVATE" not in json.dumps(observed)
