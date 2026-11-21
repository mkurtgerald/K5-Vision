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
    pinned_child = None
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
        file_identity = _NATIVE.image_diagnostic(sys.executable, {sys.executable: "python"})
        assert file_identity["matching_payload_roles"] == ["python"]
        assert file_identity["samefile_unavailable_count"] == 0
        console_file = str(PureWindowsPath(native.system_directory()) / "conhost.exe")
        assert _NATIVE.admit_system_console_file(console_file, native.system_directory()) == {
            "system_file_identity_verified": True,
            "trusted_code_custody_verified": True,
        }
        handle = native.api.OpenProcess(0x00101000, False, child.pid)
        assert handle
        pinned_child = _NATIVE.PinnedConsoleHost(native, identity, handle, {})
        assert not pinned_child.exited()
        child.stdin.close()
        child.wait(timeout=5)
        assert pinned_child.exited() and pinned_child.cached_exit["exit_code"] == 0
        pinned_child.close()
        assert pinned_child.handle is None and pinned_child.exited()
    finally:
        child.stdin.close()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
        finally:
            if pinned_child is not None:
                pinned_child.close()


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
        def __init__(self):
            self.last_parents = parents

        def birth(self, pid, **_kwargs):
            return births.get(pid)

        def descendants(self, root):
            return _NATIVE.descendant_identities(root, parents, births.get, lambda: dict(parents))

        def service_sid(self):
            return _SERVICE_SID

        def system_directory(self):
            return r"C:\Windows\System32"

        def parents(self):
            return dict(parents)

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
    cohort.console_pins = {}
    cohort.console_individual_termination_used = False
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


def _image_inventory():
    return {
        str(_INSTALL / relative).casefold(): kind
        for relative, kind in (
            ("K5VisionRecorder.exe", "wrapper"),
            ("K5Vision.exe", "host"),
            ("runtime/python.exe", "python"),
            ("workers/mediamtx.exe", "mediamtx"),
            ("workers/caddy.exe", "caddy"),
        )
    }


@pytest.mark.parametrize(
    "image,form,short",
    [
        (r"\\?\C:\Program Files\K5 Vision\workers\mediamtx.exe", "extended_drive", False),
        (r"C:\PROGRA~1\K5 Vision\workers\mediamtx.exe", "normal_drive", True),
    ],
)
def test_alias_file_identity_is_diagnostic_and_never_admits_image(image, form, short):
    inventory = _image_inventory()
    details = _details(12, _BASE, 11)
    details["image"] = image

    def compare(_actual, expected):
        return expected.endswith("mediamtx.exe")

    with pytest.raises(_NATIVE.OwnershipFailure) as failure:
        _NATIVE.service_candidate(
            details,
            _SERVICE_SID,
            inventory,
            diagnose_image=lambda actual, expected: _NATIVE.image_diagnostic(
                actual, expected, same_file=compare
            ),
        )
    note = failure.value.diagnostic
    assert note["outcome"] == "unexpected_service_image" and note["accepted"] is False
    projected = note["image_diagnostic"]
    assert projected["matching_payload_roles"] == ["mediamtx"]
    assert projected["path_form"] == form and projected["short_component"] is short
    assert image not in str(projected)


@pytest.mark.parametrize(
    "name,category",
    [
        ("mediamtx.exe", "mediamtx"),
        ("conhost.exe", "conhost"),
        ("mfpmp.exe", "mfpmp"),
        ("WerFault.exe", "werfault"),
        ("pythonw.exe", "pythonw"),
        ("private-name.exe", "other"),
    ],
)
def test_matching_basename_of_foreign_file_is_never_payload_authority(name, category):
    image = str(PureWindowsPath(r"C:\private location") / name)
    projected = _NATIVE.image_diagnostic(image, _image_inventory(), same_file=lambda *_: False)
    assert projected["basename_category"] == category
    assert projected["matching_payload_roles"] == []
    assert "private location" not in str(projected) and "private-name" not in str(projected)


def test_unreadable_file_identity_diagnostic_preserves_original_hard_failure():
    details = _details(12, _BASE, 11, kind="foreign")

    def unavailable(*_args):
        raise OSError("unretained private filename")

    with pytest.raises(_NATIVE.OwnershipFailure) as failure:
        _NATIVE.service_candidate(
            details,
            _SERVICE_SID,
            _image_inventory(),
            diagnose_image=lambda actual, expected: _NATIVE.image_diagnostic(
                actual, expected, same_file=unavailable
            ),
        )
    note = failure.value.diagnostic
    assert note["outcome"] == "unexpected_service_image" and note["accepted"] is False
    assert note["image_diagnostic"]["samefile_unavailable_count"] == 5
    assert "unretained" not in str(note)


@pytest.mark.parametrize(
    "image,status",
    [
        (r"\\server\share\mediamtx.exe", "skipped_nonlocal"),
        (r"\\?\UNC\server\share\mediamtx.exe", "skipped_nonlocal"),
        (r"\Device\HarddiskVolume1\mediamtx.exe", "skipped_nonlocal"),
        (r"relative\mediamtx.exe", "skipped_nonlocal"),
        (r"Z:\mapped\mediamtx.exe", "skipped_other_drive"),
    ],
)
def test_image_diagnostic_never_opens_remote_or_unbound_drive_paths(image, status):
    def forbidden(*_args):
        pytest.fail("Image diagnostic attempted an unbound file open")

    projected = _NATIVE.image_diagnostic(image, _image_inventory(), same_file=forbidden)
    assert projected["comparison_status"] == status
    assert projected["matching_payload_roles"] == []


def test_decisive_service_image_failure_precedes_many_foreign_candidates(monkeypatch):
    births = {11: _BASE, **{pid: _BASE - pid * _STEP for pid in range(12, 35)}}
    parents = {pid: 11 for pid in range(12, 35)}
    details = {
        pid: _details(pid, births[pid], 11, kind="foreign", sid=b"SYSTEM TokenUser")
        for pid in range(12, 34)
    }
    details[34] = _details(34, births[34], 11, kind="foreign")
    cohort = _cohort(monkeypatch, births, parents, details)
    cohort.add(SimpleNamespace(pid=11), role="service")
    with pytest.raises(RuntimeError):
        cohort.snapshot()
    diagnostic = cohort.ownership_diagnostic()
    assert diagnostic["instances"][0]["pid"] == 34
    assert diagnostic["instances"][0]["outcome"] == "unexpected_service_image"
    assert diagnostic["rejected_counts"]["foreign_token"] == 22


def _console_cohort(monkeypatch, *, parent_kind="python", fault=None):
    kinds = {11: "wrapper", 12: parent_kind}
    births = {11: _BASE, 12: _BASE - _STEP, 13: _BASE + _STEP}
    parents = {12: 11, 13: 12}
    details = {
        pid: _details(pid, births[pid], parents.get(pid, 0), kind=kind)
        for pid, kind in kinds.items()
    }
    console = _details(13, births[13], 12)
    console["image"] = r"C:\Windows\System32\conhost.exe"
    if fault == "image":
        console["image"] = r"C:\foreign\conhost.exe"
    elif fault == "sid":
        console["sid"] = b"different TokenUser"
    elif fault == "session":
        console["session"] = 1
    details[13] = console
    cohort = _cohort(monkeypatch, births, parents, details)

    class Kernel:
        def __init__(self):
            self.objects, self.closed = {}, []

        def OpenProcess(self, rights, inherited, pid):
            assert rights == 0x00101000 and not rights & 1 and not inherited
            handle = 100 + len(self.objects)
            self.objects[handle] = {"identity": (pid, births[pid]), "running": True}
            return handle

        def WaitForSingleObject(self, handle, timeout):
            assert timeout == 0
            return 258 if self.objects[handle]["running"] else 0

        def GetExitCodeProcess(self, handle, output):
            output._obj.value = 259 if self.objects[handle]["running"] else 1
            return True

        def CloseHandle(self, handle):
            self.closed.append(handle)
            return True

    kernel = Kernel()
    cohort.native.api = kernel
    cohort.native._handle_birth = lambda handle: (
        kernel.objects[handle]["identity"][1] if kernel.objects[handle]["running"] else None
    )
    cohort.native.admit_console_host = _NATIVE.NativeProcessTree.admit_console_host.__get__(
        cohort.native
    )

    def custody(image, system):
        if fault == "custody" or PureWindowsPath(image) != PureWindowsPath(system) / "conhost.exe":
            raise RuntimeError("OS file custody was refused")
        return {"system_file_identity_verified": True, "trusted_code_custody_verified": True}

    monkeypatch.setattr(_NATIVE, "admit_system_console_file", custody)
    cohort.add(SimpleNamespace(pid=11), role="service")
    return cohort, kernel, births


@pytest.mark.parametrize("parent_kind", ["python", "mediamtx", "caddy"])
def test_only_fixed_os_console_host_with_admitted_parent_gains_pinned_ownership(
    monkeypatch, parent_kind
):
    cohort, kernel, _ = _console_cohort(monkeypatch, parent_kind=parent_kind)
    assert {p.pid for p in cohort.snapshot()} == {11, 12, 13}
    pin = next(iter(cohort.console_pins.values()))
    assert pin.facts["admitted_parent_identity_verified"] is True
    process = next(p for p in cohort.snapshot() if p.pid == 13)
    with pytest.raises(RuntimeError, match="survived"):
        cohort.terminate_owned(process)
    assert kernel.closed == [] and not cohort.console_individual_termination_used


@pytest.mark.parametrize("fault", ["image", "session", "custody"])
def test_console_nomination_never_waives_image_session_or_os_custody(monkeypatch, fault):
    cohort, kernel, _ = _console_cohort(monkeypatch, fault=fault)
    with pytest.raises(RuntimeError):
        cohort.snapshot()
    assert cohort.failed and not cohort.console_pins
    assert len(kernel.closed) == 1


def test_foreign_token_console_host_is_excluded_before_any_handle_pin(monkeypatch):
    cohort, kernel, _ = _console_cohort(monkeypatch, fault="sid")
    assert {p.pid for p in cohort.snapshot()} == {11, 12}
    assert not kernel.objects and not cohort.console_pins


def test_console_host_under_host_wrapper_cannot_borrow_an_allowed_parent_role(monkeypatch):
    cohort, kernel, _ = _console_cohort(monkeypatch, parent_kind="host")
    with pytest.raises(RuntimeError):
        cohort.snapshot()
    assert cohort.failed and not kernel.objects


def test_pinned_exit_does_not_follow_pid_reuse_or_individual_termination(monkeypatch):
    cohort, kernel, births = _console_cohort(monkeypatch)
    process = next(p for p in cohort.snapshot() if p.pid == 13)
    pin = next(iter(cohort.console_pins.values()))
    births[13] += _STEP
    assert cohort.is_owned_running(process)  # Original pinned object still alive.
    kernel.objects[pin.handle]["running"] = False
    assert not cohort.is_owned_running(process)
    assert cohort.terminate_owned(process) is False
    handle = pin.handle
    pin.close()
    pin.close()
    assert kernel.closed == [handle] and pin.cached_exit == {"exited": True, "exit_code": 1}
    assert pin.exited() and not cohort.console_individual_termination_used


def test_pinned_close_caches_a_live_leak_as_failure_without_killing_it(monkeypatch):
    cohort, kernel, _ = _console_cohort(monkeypatch)
    process = next(p for p in cohort.snapshot() if p.pid == 13)
    pin = next(iter(cohort.console_pins.values()))
    handle = pin.handle
    pin.close()
    assert pin.cached_exit == {"exited": False, "exit_code": None}
    assert cohort.is_owned_running(process)
    with pytest.raises(RuntimeError):
        cohort.terminate_owned(process)
    assert cohort.failed and kernel.closed == [handle]


def test_pin_close_failure_retains_handle_and_blocks_release_proof(monkeypatch):
    cohort, kernel, _ = _console_cohort(monkeypatch)
    cohort.snapshot()
    pin = next(iter(cohort.console_pins.values()))
    kernel.objects[pin.handle]["running"] = False
    kernel.CloseHandle = lambda _handle: False
    with pytest.raises(RuntimeError, match="released"):
        pin.close()
    assert pin.handle is not None


def _finish_ready(cohort):
    cohort.stop = SimpleNamespace(set=lambda: None)
    cohort.thread = SimpleNamespace(join=lambda timeout: None)
    cohort.service_stopped = True


def test_finish_closes_exited_pin_once_and_receipt_keeps_os_helper_separate(monkeypatch):
    cohort, kernel, births = _console_cohort(monkeypatch)
    original_details = cohort.native.details
    extra = {}
    for pid, kind in ((14, "mediamtx"), (15, "caddy"), (16, "host")):
        births[pid] = _BASE - pid * _STEP
        cohort.native.last_parents[pid] = 12
        extra[pid] = _details(pid, births[pid], 12, kind=kind)
    cohort.native.details = lambda identity: (
        extra[identity[0]] if identity[0] in extra else original_details(identity)
    )
    assert len(cohort.snapshot()) == 6
    pin = next(iter(cohort.console_pins.values()))
    handle = pin.handle
    kernel.objects[handle]["running"] = False
    births.clear()
    _finish_ready(cohort)
    cohort.finish()
    assert not cohort.failed and kernel.closed == [handle]
    receipt = cohort.ownership_receipt()
    assert receipt["admitted_service_count"] == 5
    assert receipt["admitted_service_images"] == ["caddy", "host", "mediamtx", "python", "wrapper"]
    assert receipt["console_hosts"] == {
        "observed_count": 1,
        "exited_count": 1,
        "system_file_identity_verified": True,
        "trusted_code_custody_verified": True,
        "admitted_parent_identity_verified": True,
        "pinned_native_exit_verified": True,
        "individual_termination_used": False,
    }
    assert cohort.ownership_diagnostic()["instances"][2]["pinned_exit"]["exit_code"] == 1


def test_finish_live_helper_fails_once_with_existing_cleanup_budget_and_no_kill(monkeypatch):
    cohort, kernel, _ = _console_cohort(monkeypatch)
    cohort.snapshot()
    pin = next(iter(cohort.console_pins.values()))
    handle, calls = pin.handle, []
    _finish_ready(cohort)
    cohort.service_stopped = False

    def observe(_cohort):
        calls.append("existing_wait")
        raise RuntimeError("Live helper must fail cleanup")

    monkeypatch.setattr(_DESKTOP, "_service_control", lambda *_args: calls.append("stop"))
    monkeypatch.setattr(_DESKTOP, "_wait_owned_cleanup", observe)
    cohort.finish()
    assert calls == ["stop", "existing_wait"]
    assert cohort.failed and kernel.closed == [handle]
    assert pin.cached_exit == {"exited": False, "exit_code": None}
    assert not cohort.console_individual_termination_used
