"""Use the real wait/click contracts, with explicitly synthetic control states."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def _module(filename, name):
    path = Path(__file__).resolve().parents[1] / "integration" / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


@pytest.fixture
def controls(monkeypatch):
    harness = _module("test_windows_desktop_upgrade.py", "legacy_control_harness")
    desktop = _module("test_windows_desktop_app.py", "legacy_control_contract")
    state = SimpleNamespace(
        visible=False, enabled=True, exists=True, elapsed=0.0, shown_at=None, messages=[]
    )

    def visible(_control):
        return state.visible or state.shown_at is not None and state.elapsed >= state.shown_at

    def advance(seconds):
        state.elapsed += seconds

    api = desktop._Windows.__new__(desktop._Windows)
    api.api = SimpleNamespace(
        IsWindowVisible=visible, IsWindowEnabled=lambda _control: state.enabled
    )
    api.control = lambda _form, _identifier: 1104 if state.exists else None
    api.message = lambda *arguments: state.messages.append(arguments)
    monkeypatch.setattr(
        desktop, "time", SimpleNamespace(monotonic=lambda: state.elapsed, sleep=advance)
    )
    return harness, desktop, api, state


def test_hidden_enabled_control_reproduces_old_click_refusal_without_admitting_readiness(controls):
    harness, _desktop, api, state = controls
    # This is the previous predicate, followed by the actual existing click.
    assert api.api.IsWindowEnabled(api.control(1, 1012))
    with pytest.raises(
        pytest.fail.Exception, match="Expected enabled desktop button is unavailable"
    ):
        api.click(api.control(1, 1012))
    assert not harness._migration_control_ready(api, 1, 1012)
    assert state.messages == []


@pytest.mark.parametrize("identifier", [1012, 1104])
def test_actual_wait_observes_visible_enabled_control_within_its_existing_budget(
    controls, identifier
):
    harness, desktop, api, state = controls
    state.shown_at = 0.3
    assert desktop._wait(
        lambda: harness._migration_control_ready(api, 1, identifier),
        "Generated control",
        timeout=0.4,
    )
    api.click(api.control(1, identifier))
    assert 0.3 <= state.elapsed < 0.4
    assert len(state.messages) == 1


@pytest.mark.parametrize("failure", ["missing", "hidden", "disabled"])
def test_missing_never_visible_and_disabled_controls_still_fail_the_original_wait(
    controls, failure
):
    harness, desktop, api, state = controls
    if failure == "missing":
        state.exists = False
    elif failure == "disabled":
        state.visible = True
        state.enabled = False
    with pytest.raises(pytest.fail.Exception, match="readiness deadline expired"):
        desktop._wait(
            lambda: harness._migration_control_ready(api, 1, 1012),
            "Generated unavailable control",
            timeout=0.4,
        )
    assert 0.4 <= state.elapsed < 0.5
    assert state.messages == []


@pytest.mark.parametrize("identifier", [1012, 1104])
def test_failed_click_retains_only_control_facts_and_preserves_the_original_failure(
    controls, identifier
):
    harness, _desktop, api, _state = controls
    failure = pytest.fail.Exception("Generated native click refusal")

    def refused(_control):
        raise failure

    api.click = refused
    diagnostic = {}
    with pytest.raises(pytest.fail.Exception) as captured:
        harness._migration_click(api, 1, identifier, diagnostic)
    assert captured.value is failure
    assert diagnostic == {
        "migration_click_failure_control_id": identifier,
        "migration_click_failure_control_exists": True,
        "migration_click_failure_control_visible": False,
        "migration_click_failure_control_enabled": True,
    }
    assert len(json.dumps(diagnostic)) < 256


def test_successful_click_adds_no_failure_fields(controls):
    harness, _desktop, api, state = controls
    state.visible = True
    diagnostic = {}
    harness._migration_click(api, 1, 1012, diagnostic)
    assert diagnostic == {} and len(state.messages) == 1


def test_failed_diagnostic_inspection_cannot_replace_the_original_click_failure(controls):
    harness, _desktop, api, _state = controls
    primary = pytest.fail.Exception("Generated primary failure")
    calls = 0

    def control(_form, _identifier):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("PRIVATE diagnostic error")
        return 1012

    def click(_control):
        raise primary

    api.control, api.click = control, click
    diagnostic = {}
    with pytest.raises(pytest.fail.Exception) as captured:
        harness._migration_click(api, 1, 1012, diagnostic)
    assert captured.value is primary
    assert diagnostic == {"migration_click_failure_control_id": 1012}


@pytest.mark.parametrize("failure", ["hidden", "disabled"])
def test_unready_wizard_is_owned_before_the_original_window_readiness_deadline(
    controls, monkeypatch, failure
):
    harness, desktop, api, state = controls
    admissions = []
    process = SimpleNamespace(pid=4242)
    api.windows = lambda: [99]
    api.pid = lambda _window: process.pid
    api.control = lambda form, _identifier: 1012 if form == 1 else 1104
    api.api.IsWindowVisible = lambda window: not (window == 1104 and failure == "hidden")
    api.api.IsWindowEnabled = lambda window: not (window == 1104 and failure == "disabled")
    native = SimpleNamespace(
        birth=lambda pid: pid * 10,
        details=lambda _identity: {"sid": "SYNTHETIC_OWNER", "elevated": True, "session": 1},
    )
    cohort = SimpleNamespace(
        native=native,
        add=lambda *args, **kwargs: admissions.append((args, kwargs)),
    )
    monkeypatch.setattr(harness, "psutil", SimpleNamespace(Process=lambda _pid: process))
    with pytest.raises(pytest.fail.Exception, match="readiness deadline expired"):
        harness._wizard(desktop, api, 1, cohort)
    assert 30 <= state.elapsed < 30.1
    assert len(admissions) == 1
    args, kwargs = admissions[0]
    assert args == (process,)
    assert kwargs == {"role": "gui", "expected_identity": (4242, 42420)}
