"""Clock corrections cannot change ownership or hide real recorder descendants."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[1] / "integration"


def _load(name):
    spec = importlib.util.spec_from_file_location(
        "process_identity_" + name, _ROOT / (name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_NATIVE = _load("windows_process_identity")
_DESKTOP = _load("test_windows_desktop_app")
_BASE = 134080000000000000
_STEP = 172800 * 10_000_000


@pytest.mark.skipif(os.name != "nt", reason="Native process identity ABI requires Windows")
def test_native_process_tree_binds_an_actual_owned_child():
    native = _NATIVE.NativeProcessTree()
    root = os.getpid(), native.birth(os.getpid())
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        identity = child.pid, native.birth(child.pid, handle=child._handle)
        assert identity[1] is not None
        assert identity in native.descendants(root)
    finally:
        child.stdin.close()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


@pytest.mark.parametrize("correction", [-_STEP, _STEP], ids=["backward48h", "forward48h"])
def test_actual_parent_pid_remains_owned_after_opposite_48_hour_birth_orders(correction):
    births = {11: _BASE, 12: _BASE + correction, 13: _BASE - correction, 99: _BASE}
    parents = {11: 1, 12: 11, 13: 12, 99: 1}
    result = _NATIVE.descendant_identities((11, _BASE), parents, births.get, lambda: dict(parents))
    assert set(result) == {(11, births[11]), (12, births[12]), (13, births[13])}


def test_pid_reuse_before_traversal_rejects_cached_root_identity():
    assert (
        _NATIVE.descendant_identities(
            (11, _BASE),
            {12: 11},
            {11: _BASE + 1, 12: _BASE - _STEP}.get,
            lambda: {12: 11},
        )
        == ()
    )


def test_root_pid_reuse_during_traversal_discards_entire_owned_tree():
    births = {11: _BASE, 12: _BASE - _STEP, 13: _BASE + _STEP}
    parents = {12: 11, 13: 12}

    def changed_topology():
        births[11] += 1
        return parents

    assert _NATIVE.descendant_identities((11, _BASE), parents, births.get, changed_topology) == ()


def test_reused_child_pid_is_not_admitted_under_its_old_parent_pid():
    calls = {}
    births = {11: _BASE, 12: _BASE - _STEP, 13: _BASE + _STEP}

    def birth(pid):
        calls[pid] = calls.get(pid, 0) + 1
        return births.get(pid, 0) + (1 if pid == 12 and calls[pid] > 1 else 0)

    assert _NATIVE.descendant_identities(
        (11, _BASE), {12: 11, 13: 12}, birth, lambda: {12: 11, 13: 12}
    ) == ((11, _BASE),)


def test_changed_parent_binding_rejects_child_and_all_descendants():
    births = {11: _BASE, 12: _BASE - _STEP, 13: _BASE + _STEP}
    assert _NATIVE.descendant_identities(
        (11, _BASE), {12: 11, 13: 12}, births.get, lambda: {12: 99, 13: 12}
    ) == ((11, _BASE),)


def test_missing_child_and_unrelated_parent_never_become_owned():
    births = {11: _BASE, 13: _BASE - _STEP, 99: _BASE + _STEP}
    assert _NATIVE.descendant_identities(
        (11, _BASE),
        {12: 11, 13: 12, 99: 1},
        births.get,
        lambda: {12: 11, 13: 12, 99: 1},
    ) == ((11, _BASE),)


def _cohort(monkeypatch, births, parents):
    class Process:
        def __init__(self, pid):
            self.pid = pid

        def is_running(self):
            return self.pid in births

        def children(self, recursive=False):
            raise AssertionError("psutil chronology must not decide native ownership")

        def create_time(self):
            raise AssertionError("Floating-point birth dates are not native identities")

    class Native:
        def birth(self, pid, **_kwargs):
            return births.get(pid)

        def descendants(self, root):
            return _NATIVE.descendant_identities(root, parents, births.get, lambda: dict(parents))

    monkeypatch.setattr(_DESKTOP.psutil, "Process", Process)
    cohort = _DESKTOP._Cohort.__new__(_DESKTOP._Cohort)
    cohort.native = Native()
    cohort.roots, cohort.processes, cohort.native_identities = [], {}, {}
    cohort.lock = threading.Lock()
    return cohort


def test_shared_cohort_finds_clock_inverted_child_without_psutil_chronology(monkeypatch):
    births = {11: _BASE, 12: _BASE - _STEP}
    cohort = _cohort(monkeypatch, births, {12: 11})
    cohort.add(SimpleNamespace(pid=11))
    assert {process.pid for process in cohort.snapshot()} == {11, 12}
    assert set(cohort.processes) == {(11, _BASE), (12, _BASE - _STEP)}


def test_shared_cohort_drops_old_pid_occupant_from_cleanup_after_reuse(monkeypatch):
    births = {11: _BASE, 12: _BASE - _STEP}
    parents = {12: 11}
    cohort = _cohort(monkeypatch, births, parents)
    cohort.add(SimpleNamespace(pid=11))
    original_child = next(process for process in cohort.snapshot() if process.pid == 12)
    births[12] += 1
    parents[12] = 99
    assert {process.pid for process in cohort.snapshot()} == {11}
    assert not cohort.is_owned_running(original_child)
    _DESKTOP._wait_owned_cleanup(
        SimpleNamespace(
            snapshot=lambda: [original_child], is_owned_running=cohort.is_owned_running
        ),
        timeout=0,
    )
