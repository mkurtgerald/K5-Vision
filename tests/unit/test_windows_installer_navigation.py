"""Bounded Inno navigation: one queued native Next click per observed wizard page."""

from __future__ import annotations

import importlib.util
import itertools
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


class _DelayedInstaller:
    """A shared Next/Install HWND with controllable handler completion."""

    def __init__(self, *, original_race=False, never_acknowledge=False):
        self.api = self
        self.phase = 0
        self.clock = 0.0
        self.tick = 0
        self.pending = []
        self.actual_actions = []
        self.finished = False
        self.checked = True
        self.original_race = original_race
        self.never_acknowledge = never_acknowledge
        self.killed = False
        self.waited = False
        self.process = SimpleNamespace(
            pid=91, returncode=None, poll=self.poll, kill=self.kill, wait=self.wait
        )

    def poll(self):
        return self.process.returncode

    def kill(self):
        self.killed = True
        self.process.returncode = -9

    def wait(self, *, timeout):
        assert timeout == 10
        self.waited = True
        return self.process.returncode

    def windows(self, parent=None):
        return [1] if parent is None else [2, 3, 4]

    def pid(self, _window):
        return 91

    def children(self, *, recursive):
        assert recursive
        return []

    def IsWindowVisible(self, _window):
        return True

    def IsWindowEnabled(self, _window):
        return True

    def GetDlgCtrlID(self, window):
        return window

    def class_name(self, window):
        # The original code captures Next, then re-reads controls for its
        # broad signature. Deliver the earlier queued Next at that exact gap.
        if self.original_race and window == 2 and self.pending and self.phase == 0:
            self.complete(self.pending[0])
        return "TNewButton" if window == 2 else "TNewStaticText"

    def text(self, window):
        if window == 2:
            return ("Next >", "Install", "Finish")[self.phase]
        if window == 4 and self.phase == 2:
            return "Launch K5 Vision"
        return "PRIVATE changing progress " + str(self.tick)

    def button_caption(self, window):
        return _DESKTOP._normalized_caption(self.text(window))

    def message(self, window, message):
        assert window == 4 and message == 0x00F0
        return int(self.checked)

    def click(self, window):
        if window == 4:
            self.checked = not self.checked
        else:
            assert window == 2 and self.phase == 2 and not self.checked
            self.actual_actions.append("finish")
            self.finished = True
            self.process.returncode = 0

    def post_click(self, window):
        assert window == 2
        action = ("next", "install", "finish")[self.phase]
        self.actual_actions.append(action)
        ticket = SimpleNamespace(action=action, completed=False, due=self.tick + 4)
        self.pending.append(ticket)
        return ticket

    def complete(self, ticket):
        if not ticket.completed:
            self.phase = 1 if ticket.action == "next" else 2
            ticket.completed = True

    def click_completed(self, ticket):
        if self.tick >= ticket.due and not self.never_acknowledge:
            self.complete(ticket)
        return ticket.completed

    def sleep(self, seconds):
        self.clock += seconds
        self.tick += 1
        if self.original_race:
            for ticket in self.pending:
                if ticket.action == "install":
                    self.complete(ticket)


def _drive_delayed_installer(monkeypatch, wizard, function=None):
    monkeypatch.setattr(_DESKTOP.subprocess, "Popen", lambda *_args, **_kwargs: wizard.process)
    root = SimpleNamespace(
        pid=91,
        children=wizard.children,
        cpu_times=lambda: SimpleNamespace(user=0.0, system=0.0),
        is_running=lambda: wizard.poll() is None,
    )
    monkeypatch.setattr(_DESKTOP.psutil, "Process", lambda _pid: root)
    monkeypatch.setattr(_DESKTOP.time, "monotonic", lambda: wizard.clock)
    monkeypatch.setattr(_DESKTOP.time, "sleep", wizard.sleep)
    diagnostic = {}
    (function or _DESKTOP._install_gui)(
        Path("fixture.exe"), Path("installed"), wizard, diagnostic=diagnostic
    )
    return diagnostic


def test_stale_caption_snapshot_can_dispatch_actual_install_after_pending_next():
    wizard = _DelayedInstaller(original_race=True)
    wizard.post_click(2)
    wizard.sleep(0.2)
    controls = wizard.windows(1)
    captions = {_DESKTOP._normalized_caption(wizard.text(window)): window for window in controls}
    assert "next >" in captions
    _DESKTOP._installer_page_signature(wizard, controls)
    assert wizard.button_caption(captions["next >"]) == "install"
    wizard.post_click(captions["next >"])
    assert wizard.actual_actions == ["next", "install"]


def test_acknowledged_driver_observes_actual_install_after_delayed_next(monkeypatch):
    wizard = _DelayedInstaller()
    diagnostic = _drive_delayed_installer(monkeypatch, wizard)
    assert wizard.actual_actions == ["next", "install", "finish"]
    assert wizard.tick >= 8
    assert diagnostic["installer_install_posted"] is True
    assert diagnostic["installer_next_clicks"] == 1
    assert diagnostic["installer_steps_seen"] == ["finish", "install", "next >"]
    assert "PRIVATE" not in json.dumps(diagnostic)


def test_unacknowledged_native_command_keeps_original_deadline_and_cleanup(monkeypatch):
    wizard = _DelayedInstaller(never_acknowledge=True)
    with pytest.raises(pytest.fail.Exception, match="wizard exceeded its deadline"):
        _drive_delayed_installer(monkeypatch, wizard)
    assert 90 <= wizard.clock < 90.3
    assert wizard.actual_actions == ["next"]
    assert wizard.killed and wizard.waited


def test_fresh_caption_mismatch_is_observed_again_before_dispatch(monkeypatch):
    wizard = _DelayedInstaller()
    original = wizard.button_caption

    def caption(window):
        if wizard.phase == 0:
            wizard.phase = 1
        return original(window)

    wizard.button_caption = caption
    diagnostic = _drive_delayed_installer(monkeypatch, wizard)
    assert wizard.actual_actions == ["install", "finish"]
    assert diagnostic["installer_next_clicks"] == 0
    assert diagnostic["installer_install_posted"] is True


def test_missing_real_install_observation_still_fails(monkeypatch):
    wizard = _DelayedInstaller()
    original = wizard.complete

    def complete(ticket):
        original(ticket)
        if ticket.action == "next":
            wizard.phase = 2

    wizard.complete = complete
    with pytest.raises(pytest.fail.Exception, match="Installer wizard was not exercised"):
        _drive_delayed_installer(monkeypatch, wizard)
    assert wizard.actual_actions == ["next", "finish"]
    assert wizard.process.returncode == 0


def test_checked_license_radio_is_not_clicked_again(monkeypatch):
    wizard = _DelayedInstaller()
    original_windows, original_text, original_message = wizard.windows, wizard.text, wizard.message

    def windows(parent=None):
        return original_windows(parent) + ([5] if parent is not None else [])

    wizard.windows = windows
    wizard.text = lambda window: "I accept the agreement" if window == 5 else original_text(window)
    wizard.message = lambda window, message: 1 if window == 5 else original_message(window, message)
    diagnostic = _drive_delayed_installer(monkeypatch, wizard)
    assert diagnostic["installer_action"] == "completed"
    assert wizard.actual_actions == ["next", "install", "finish"]


def test_pending_install_still_observes_and_refuses_payload_modal(monkeypatch):
    wizard = _DelayedInstaller()

    def modal(_api, _forms, diagnostic):
        diagnostic["installer_modal_classification"] = "payload_refusal"

    monkeypatch.setattr(_DESKTOP, "_classify_installer_modal", modal)
    monkeypatch.setattr(_DESKTOP, "_classify_installed_seal_refusal", lambda _path: "fixture")
    with pytest.raises(pytest.fail.Exception, match="refused its extracted sealed payload"):
        _drive_delayed_installer(monkeypatch, wizard)
    assert wizard.actual_actions == ["next", "install"]
    assert wizard.clock < 90
    assert wizard.killed and wizard.waited


def test_async_click_retains_callback_until_native_acknowledgment():
    owner = object.__new__(_DESKTOP._Windows)
    owner._clicks = {}
    owner._click_serial = itertools.count(1)
    owner._click_callback = owner._acknowledge_click
    queued = []

    def send(window, message, parameter, value, callback, identifier):
        queued.append((window, message, callback, identifier))
        return True

    owner.api = SimpleNamespace(
        IsWindow=lambda _window: True,
        IsWindowVisible=lambda _window: True,
        IsWindowEnabled=lambda _window: True,
        SendMessageCallbackW=send,
        PeekMessageW=lambda *_args: False,
    )
    ticket = owner.post_click(11)
    assert owner in _DESKTOP._INSTALLER_COMMAND_OWNERS
    assert not owner.click_completed(ticket)
    window, message, callback, identifier = queued[0]
    assert message == 0x00F5
    callback(window, message, identifier, 0)
    assert owner.click_completed(ticket)
    assert owner not in _DESKTOP._INSTALLER_COMMAND_OWNERS


def test_async_click_dispatch_refusal_does_not_leave_a_callback_owner():
    owner = object.__new__(_DESKTOP._Windows)
    owner._clicks = {}
    owner._click_serial = itertools.count(1)
    owner._click_callback = owner._acknowledge_click
    owner.api = SimpleNamespace(
        IsWindow=lambda _window: True,
        IsWindowVisible=lambda _window: True,
        IsWindowEnabled=lambda _window: True,
        SendMessageCallbackW=lambda *_args: False,
    )
    with pytest.raises(pytest.fail.Exception, match="could not be dispatched"):
        owner.post_click(11)
    assert not owner._clicks
    assert owner not in _DESKTOP._INSTALLER_COMMAND_OWNERS
