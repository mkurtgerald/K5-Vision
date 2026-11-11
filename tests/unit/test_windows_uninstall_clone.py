"""Inno's cloned uninstaller must finish before graphical baseline reversal."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SOURCE = Path(__file__).resolve().parents[1] / "integration" / "test_windows_desktop_upgrade.py"
_SPEC = importlib.util.spec_from_file_location("k5_upgrade_uninstall_under_test", _SOURCE)
assert _SPEC is not None and _SPEC.loader is not None
_UPGRADE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_UPGRADE)


class _OriginalUninstaller:
    pid = 4242

    def __init__(self):
        self.returncode = None
        self.polls = 0

    def poll(self):
        self.polls += 1
        if self.polls >= 4:
            self.returncode = 0
        return self.returncode


class _Clone:
    pid = 4243

    def __init__(self):
        self.polls = 0

    def is_running(self):
        self.polls += 1
        return self.polls < 4


class _Parent:
    def __init__(self, child):
        self.child = child

    def children(self, *, recursive):
        assert recursive is True
        return [self.child]


def test_uninstall_waits_for_clone_after_original_exits(monkeypatch):
    original, clone = _OriginalUninstaller(), _Clone()
    observed_args = []

    def spawn(args, **kwargs):
        observed_args.extend(args)
        return original

    monkeypatch.setattr(_UPGRADE.subprocess, "Popen", spawn)
    monkeypatch.setattr(_UPGRADE.time, "monotonic", _monotonic_clock())
    monkeypatch.setattr(_UPGRADE.time, "sleep", lambda _duration: None)
    monkeypatch.setattr(_UPGRADE.psutil, "Process", lambda pid: _Parent(clone))

    exit_code, child_seen = _UPGRADE._uninstall_through_clone_completion(
        Path("synthetic-unins000.exe")
    )
    assert exit_code == 0
    assert child_seen is True
    assert clone.polls >= 4
    assert "/VERYSILENT" in observed_args
    assert "/NORESTART" in observed_args


def _monotonic_clock():
    tick = 0.0

    def now():
        nonlocal tick
        tick += 0.01
        return tick

    return now
