"""Clock corrections cannot change ownership or hide real recorder descendants."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import threading
from pathlib import Path, PureWindowsPath
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
        child_details, current = native.details(identity), native.details(root)
        assert 8 <= len(child_details["sid"]) <= 68
        assert child_details["sid"] == current["sid"]
        assert PureWindowsPath(child_details["image"]) == PureWindowsPath(sys.executable)
        assert child_details["session"] == current["session"]
        assert child_details["created_100ns"] == identity[1]
        assert child_details["parent_pid"] == os.getpid()
        assert type(child_details["elevated"]) is bool
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


_INSTALL = PureWindowsPath(r"C:\Program Files\K5 Vision")
_SERVICE_SID = b"native service TokenUser"


def _details(pid, birth, parent, *, kind="python", sid=_SERVICE_SID, session=0):
    relative = {
        "wrapper": "K5VisionRecorder.exe",
        "host": "K5Vision.exe",
        "python": "runtime/python.exe",
        "mediamtx": "workers/mediamtx.exe",
        "caddy": "workers/caddy.exe",
        "foreign": "C:/Windows/System32/system-child.exe",
    }[kind]
    return {
        "pid": pid,
        "created_100ns": birth,
        "parent_pid": parent,
        "image": str(_INSTALL / relative),
        "sid": sid,
        "session": session,
        "elevated": False,
    }


def _cohort(monkeypatch, births, parents, details=None):
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
        last_parents = parents

        def birth(self, pid, **_kwargs):
            return births.get(pid)

        def descendants(self, root):
            return _NATIVE.descendant_identities(root, parents, births.get, lambda: dict(parents))

        def service_sid(self):
            return _SERVICE_SID

        def system_directory(self):
            return r"C:\Windows\System32"

        def details(self, identity):
            if details is not None and identity[0] in details:
                result = details[identity[0]]
                if isinstance(result, BaseException):
                    raise result
                return result
            return _details(
                identity[0],
                identity[1],
                parents.get(identity[0], 0),
                kind="wrapper" if identity[0] == 11 else "python",
            )

    monkeypatch.setattr(_DESKTOP.psutil, "Process", Process)
    cohort = _DESKTOP._Cohort.__new__(_DESKTOP._Cohort)
    cohort.native = Native()
    cohort.roots, cohort.processes, cohort.native_identities = [], {}, {}
    cohort.root_roles, cohort.ownership_records = {}, {}
    cohort.failed = False
    cohort.service = SimpleNamespace(installed=_INSTALL)
    cohort.lock = threading.Lock()
    return cohort


def test_shared_cohort_finds_clock_inverted_child_without_psutil_chronology(monkeypatch):
    births = {11: _BASE, 12: _BASE - _STEP}
    cohort = _cohort(monkeypatch, births, {12: 11})
    cohort.add(SimpleNamespace(pid=11), role="service")
    assert {process.pid for process in cohort.snapshot()} == {11, 12}
    assert set(cohort.processes) == {(11, _BASE), (12, _BASE - _STEP)}


def test_shared_cohort_drops_old_pid_occupant_from_cleanup_after_reuse(monkeypatch):
    births = {11: _BASE, 12: _BASE - _STEP}
    parents = {12: 11}
    cohort = _cohort(monkeypatch, births, parents)
    cohort.add(SimpleNamespace(pid=11), role="service")
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


def test_stale_system_child_parent_pid_never_grants_cleanup_ownership(monkeypatch):
    births = {11: _BASE, 12: _BASE - _STEP}
    details = {12: _details(12, births[12], 11, kind="foreign", sid=b"SYSTEM TokenUser")}
    cohort = _cohort(monkeypatch, births, {12: 11}, details)
    cohort.add(SimpleNamespace(pid=11), role="service")
    assert {p.pid for p in cohort.snapshot()} == {11}
    assert (12, births[12]) not in cohort.processes
    diagnostic = cohort.ownership_diagnostic()
    assert diagnostic["rejected_counts"] == {"foreign_token": 1}
    foreign = next(v for v in diagnostic["instances"] if v["pid"] == 12)
    assert foreign["parent_pid"] == 11 and foreign["sid_role"] == "foreign"
    assert foreign["kind"] == "outside_payload" and foreign["accepted"] is False
    assert "image" not in foreign and "sid" not in foreign


def test_service_sid_as_group_does_not_replace_native_token_user(monkeypatch):
    births = {11: _BASE, 12: _BASE - _STEP}
    value = _details(12, births[12], 11, sid=b"another TokenUser")
    value["groups"] = [_SERVICE_SID]
    cohort = _cohort(monkeypatch, births, {12: 11}, {12: value})
    cohort.add(SimpleNamespace(pid=11), role="service")
    assert {p.pid for p in cohort.snapshot()} == {11}


@pytest.mark.parametrize("failure", ["unexpected_image", "wrong_session", "unreadable"])
def test_genuine_or_unclassified_service_candidate_failure_blocks_proof(monkeypatch, failure):
    births = {11: _BASE, 12: _BASE - _STEP}
    value = (
        OSError("private image path and credential text must never enter evidence")
        if failure == "unreadable"
        else _details(
            12,
            births[12],
            11,
            kind="foreign" if failure == "unexpected_image" else "python",
            session=1 if failure == "wrong_session" else 0,
        )
    )
    cohort = _cohort(monkeypatch, births, {12: 11}, {12: value})
    cohort.add(SimpleNamespace(pid=11), role="service")
    with pytest.raises(RuntimeError):
        cohort.snapshot()
    assert cohort.failed and (12, births[12]) not in cohort.processes
    diagnostic = cohort.ownership_diagnostic()
    assert diagnostic["instances"][-1]["accepted"] is True  # Wrapper retained independently.
    rejected = diagnostic["instances"][0]
    assert rejected["pid"] == 12 and rejected["accepted"] is False
    assert "private image" not in str(diagnostic)
    with pytest.raises(pytest.fail.Exception):
        cohort.ownership_receipt()


@pytest.mark.parametrize("role", ["gui", "time_helper"])
def test_explicit_client_and_helper_roots_never_infer_parent_pid_ownership(monkeypatch, role):
    births = {11: _BASE, 12: _BASE - _STEP}
    cohort = _cohort(monkeypatch, births, {12: 11})
    if role == "time_helper":
        # This test concerns traversal only; authority admission is independently
        # tested by the real native ABI and the time helper acceptance fixture.
        cohort.root_roles[(11, _BASE)] = role
        root = _DESKTOP.psutil.Process(11)
        cohort.roots = [(11, _BASE)]
        cohort.processes[(11, _BASE)] = root
        cohort.native_identities[id(root)] = (11, _BASE)
    else:
        cohort.add(SimpleNamespace(pid=11), role=role)
    assert {p.pid for p in cohort.snapshot()} == {11}


def test_receipt_comes_from_all_five_actual_admitted_service_image_roles(monkeypatch):
    kinds = {11: "wrapper", 12: "host", 13: "python", 14: "mediamtx", 15: "caddy"}
    births = {pid: _BASE - (pid - 11) * _STEP for pid in kinds}
    parents = {12: 11, 13: 12, 14: 13, 15: 13}
    details = {
        pid: _details(pid, birth, parents.get(pid, 0), kind=kinds[pid])
        for pid, birth in births.items()
    }
    cohort = _cohort(monkeypatch, births, parents, details)
    cohort.add(SimpleNamespace(pid=11), role="service")
    assert len(cohort.snapshot()) == 5
    receipt = cohort.ownership_receipt()
    assert receipt["admitted_service_images"] == ["caddy", "host", "mediamtx", "python", "wrapper"]
    assert receipt["admitted_service_count"] == 5 and receipt["rejected_foreign_count"] == 0
    assert all(
        receipt[k] is True
        for k in ("service_token_verified", "session_zero_verified", "installed_images_verified")
    )
    cohort.ownership_records.pop((15, births[15]))
    with pytest.raises(pytest.fail.Exception):
        cohort.ownership_receipt()


def test_unreadable_native_birth_is_projected_before_admission_without_private_text(monkeypatch):
    births = {11: _BASE, 12: _BASE - _STEP}
    parents = {12: 11}
    cohort = _cohort(monkeypatch, births, parents)
    cohort.add(SimpleNamespace(pid=11), role="service")
    native = _NATIVE.NativeProcessTree.__new__(_NATIVE.NativeProcessTree)
    native.parents = lambda: dict(parents)

    def birth(pid):
        if pid == 12:
            raise OSError("unretained secret path or command")
        return births.get(pid)

    native.birth = birth
    cohort.native.descendants = native.descendants
    with pytest.raises(RuntimeError):
        cohort.snapshot()
    note = cohort.ownership_diagnostic()["instances"][0]
    assert note["pid"] == 12 and note["created_100ns"] == 0 and note["parent_pid"] == 11
    assert note["outcome"] == "proof_unavailable" and note["accepted"] is False
    assert "unretained" not in str(note) and cohort.failed
    assert (12, births[12]) not in cohort.processes


def test_early_rejected_candidate_survives_bounded_diagnostics(monkeypatch):
    births = {11: _BASE, **{pid: _BASE - pid * _STEP for pid in range(12, 70)}}
    parents = {pid: 11 for pid in range(12, 70)}
    details = {12: _details(12, births[12], 11, kind="foreign", sid=b"SYSTEM TokenUser")}
    cohort = _cohort(monkeypatch, births, parents, details)
    cohort.add(SimpleNamespace(pid=11), role="service")
    cohort.snapshot()
    diagnostic = cohort.ownership_diagnostic()
    assert len(diagnostic["instances"]) == 32
    assert diagnostic["instances"][0]["pid"] == 12
    assert diagnostic["instances"][0]["outcome"] == "foreign_token"
    assert diagnostic["rejected_counts"] == {"foreign_token": 1}


@pytest.mark.parametrize("fault", [None, "image", "sid", "session", "elevation", "birth"])
def test_time_helper_requires_explicit_fixed_image_and_current_admin_authority(monkeypatch, fault):
    births = {11: _BASE, os.getpid(): _BASE + _STEP}
    helper = _details(11, _BASE, 99, kind="host", sid=b"current admin SID", session=1)
    current = _details(os.getpid(), births[os.getpid()], 1, sid=b"current admin SID", session=1)
    helper["elevated"], current["elevated"] = True, True
    if fault == "image":
        helper["image"] = r"C:\private\K5Vision.exe"
    elif fault == "sid":
        helper["sid"] = b"different admin SID"
    elif fault == "session":
        helper["session"] = 0
    elif fault == "elevation":
        helper["elevated"] = False
    cohort = _cohort(monkeypatch, births, {}, {11: helper, os.getpid(): current})
    monkeypatch.setattr(_DESKTOP, "_machine_paths", lambda: (_INSTALL, _INSTALL / "data"))
    expected = 11, _BASE + (1 if fault == "birth" else 0)
    if fault is None:
        assert (
            cohort.add(SimpleNamespace(pid=11), role="time_helper", expected_identity=expected)
            == expected
        )
        assert {p.pid for p in cohort.snapshot()} == {11}
    else:
        with pytest.raises(RuntimeError):
            cohort.add(SimpleNamespace(pid=11), role="time_helper", expected_identity=expected)
        assert not cohort.processes
