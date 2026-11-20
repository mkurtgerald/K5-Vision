"""Bounded Inno navigation: one queued native Next click per observed wizard page."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

_SOURCE = Path(__file__).resolve().parents[1] / "integration" / "test_windows_desktop_app.py"
_SPEC = importlib.util.spec_from_file_location("k5_desktop_navigation_under_test", _SOURCE)
assert _SPEC is not None and _SPEC.loader is not None
_DESKTOP = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_DESKTOP)


class _Wizard:
    def __init__(self, controls):
        self.controls = controls
        self.api = self

    def GetDlgCtrlID(self, window):
        return window

    def class_name(self, window):
        return self.controls[window][0]

    def text(self, window):
        return self.controls[window][1]


def test_wizard_page_signature_unchanged_while_next_handler_is_pending():
    controls = {1: ("TNewStaticText", "Welcome"), 2: ("TNewButton", "Next >")}
    wizard = _Wizard(controls)
    initial = _DESKTOP._installer_page_signature(wizard, [1, 2])
    assert initial == _DESKTOP._installer_page_signature(wizard, [2, 1])
    assert initial == _DESKTOP._installer_page_signature(wizard, [1, 2])


def test_wizard_page_signature_changes_only_with_observed_native_page():
    controls = {1: ("TNewStaticText", "Welcome"), 2: ("TNewButton", "Next >")}
    wizard = _Wizard(controls)
    initial = _DESKTOP._installer_page_signature(wizard, [1, 2])
    wizard.controls[1] = ("TNewStaticText", "License")
    next_page = _DESKTOP._installer_page_signature(wizard, [1, 2])
    assert next_page != initial
    assert next_page == _DESKTOP._installer_page_signature(wizard, [1, 2])


def test_wizard_signature_tracks_control_identity_not_only_caption():
    wizard = _Wizard({1: ("TNewStaticText", "Ready"), 2: ("TNewButton", "Next >")})
    current = _DESKTOP._installer_page_signature(wizard, [1, 2])
    wizard.controls[3] = ("TNewStaticText", "Ready")
    assert current != _DESKTOP._installer_page_signature(wizard, [2, 3])


class _EventPollClient:
    def __init__(self, status, envelope=None):
        self.status = status
        self.envelope = envelope if envelope is not None else {"events": []}

    def get(self, route):
        assert route == "/api/v1/operator/cameras/synthetic/events"
        return self.status, json.dumps(self.envelope).encode("utf-8"), {}


@pytest.mark.parametrize("status", [502, 503, 504])
def test_installed_person_poll_retries_only_gateway_reconnects(status):
    diagnostic = {}
    event = _DESKTOP._poll_installed_person_event(
        _EventPollClient(status), "/api/v1/operator/cameras/synthetic", diagnostic
    )
    assert event is None
    assert diagnostic == {"analytics_event_poll_status": status}


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
def test_installed_person_poll_refuses_auth_and_nontransient_errors(status):
    diagnostic = {}
    with pytest.raises(pytest.fail.Exception):
        _DESKTOP._poll_installed_person_event(
            _EventPollClient(status), "/api/v1/operator/cameras/synthetic", diagnostic
        )
    assert diagnostic == {"analytics_event_poll_status": status}


def test_installed_person_poll_requires_a_valid_envelope_and_positive_event():
    route = "/api/v1/operator/cameras/synthetic"
    diagnostic = {}
    assert _DESKTOP._poll_installed_person_event(_EventPollClient(200), route, diagnostic) is None
    assert diagnostic == {"analytics_event_poll_status": 200}

    event = {"camera_id": "synthetic", "person_count": 1, "confidence": 0.9}
    assert (
        _DESKTOP._poll_installed_person_event(
            _EventPollClient(200, {"events": [event]}), route, diagnostic
        )
        == event
    )
    with pytest.raises(pytest.fail.Exception):
        _DESKTOP._poll_installed_person_event(
            _EventPollClient(200, {"events": "malformed"}), route, diagnostic
        )


class _ProcessActivityFixture:
    def __init__(self, *, pid, user, system, children=()):
        self.pid = pid
        self._user = user
        self._system = system
        self._children = children

    def children(self, *, recursive):
        assert recursive
        return list(self._children)

    def cpu_times(self):
        return SimpleNamespace(user=self._user, system=self._system)

    def is_running(self):
        return True


def test_installer_timeout_activity_reports_only_bounded_process_metrics(monkeypatch):
    child = _ProcessActivityFixture(pid=92, user=2.5, system=0.5)
    root = _ProcessActivityFixture(pid=91, user=3.0, system=1.25, children=(child,))
    monkeypatch.setattr(_DESKTOP.psutil, "Process", lambda pid: root)
    witness = _DESKTOP._installer_timeout_activity(SimpleNamespace(pid=91))
    assert witness == {
        "observed_processes": 2,
        "processes_capped": False,
        "aggregate_cpu_seconds": 7.25,
        "root_running": True,
    }


def test_installer_timeout_activity_refuses_unavailable_process_metadata(monkeypatch):
    def missing(_pid):
        raise _DESKTOP.psutil.NoSuchProcess(91)

    monkeypatch.setattr(_DESKTOP.psutil, "Process", missing)
    assert _DESKTOP._installer_timeout_activity(SimpleNamespace(pid=91)) == {
        "observation": "unavailable"
    }
