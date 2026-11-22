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
        assert native._handle_creation(handle) == identity[1]
        assert not pinned_child.exited()
        child.stdin.close()
        child.wait(timeout=5)
        assert native._handle_birth(handle) is None
        assert native._handle_creation(handle) == identity[1]
        assert native.parent_creation(child.pid) == identity[1]
        assert native._live_parent_birth(handle) is None
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


@pytest.mark.parametrize("correction", [-_STEP, _STEP], ids=["backward48h", "forward48h"])
def test_completed_snapshot_publishes_exact_edges_without_chronological_order(correction):
    births = {11: _BASE, 12: _BASE + correction, 13: _BASE - correction}
    parents = {12: 11, 13: 12}
    edges = {}
    assert set(
        _NATIVE.descendant_identities(
            (11, _BASE), parents, births.get, lambda: dict(parents), edge_sink=edges
        )
    ) == set(births.items())
    assert edges == {
        (12, births[12]): (11, births[11]),
        (13, births[13]): (12, births[12]),
    }


@pytest.mark.parametrize("fault", ["root_reuse", "changed_edge", "query_failure"])
def test_unstable_or_incomplete_snapshot_cannot_publish_nominated_edges(fault):
    births = {11: _BASE, 12: _BASE - _STEP, 13: _BASE + _STEP}
    parents = {12: 11, 13: 12}
    edges = {}

    def final_topology():
        if fault == "query_failure":
            raise OSError("unretained native query detail")
        if fault == "root_reuse":
            births[11] += 1
            return parents
        return {12: 99, 13: 12}

    if fault == "query_failure":
        with pytest.raises(OSError):
            _NATIVE.descendant_identities(
                (11, _BASE), parents, births.get, final_topology, edge_sink=edges
            )
    else:
        result = _NATIVE.descendant_identities(
            (11, _BASE), parents, births.get, final_topology, edge_sink=edges
        )
        assert result == (() if fault == "root_reuse" else ((11, _BASE),))
    assert edges == {}


def test_native_descendants_replace_previous_snapshot_edges_after_root_reuse():
    native = _NATIVE.NativeProcessTree.__new__(_NATIVE.NativeProcessTree)
    births, parents = {11: _BASE, 12: _BASE - _STEP}, {12: 11}
    native.parents = lambda: dict(parents)
    native.birth = births.get
    assert native.descendants((11, _BASE)) == tuple(births.items())
    assert native.last_identity_edges == {(12, births[12]): (11, _BASE)}
    births[11] += 1
    assert native.descendants((11, _BASE)) == ()
    assert native.last_identity_edges == {}


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
            self.last_identity_edges = {}

        def birth(self, pid, **_kwargs):
            return births.get(pid)

        def descendants(self, root):
            self.last_identity_edges = {}
            return _NATIVE.descendant_identities(
                root,
                parents,
                births.get,
                lambda: dict(parents),
                edge_sink=self.last_identity_edges,
            )

        def pin_service_parent(self, identity, kind, service_sid, expected_image):
            assert kind in {"python", "mediamtx", "caddy"}
            assert service_sid == _SERVICE_SID
            assert PureWindowsPath(self.details(identity)["image"]) == PureWindowsPath(
                expected_image
            )
            return SimpleNamespace(identity=identity, kind=kind, handle=None, close=lambda: None)

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
    cohort.parent_pins = {}
    cohort.console_individual_termination_used = False
    cohort.failed = False
    cohort.first_failure = None
    cohort.last_cleanup_observation = None
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
            self.query_handles, self.query_closed = set(), []
            self.originals = {}

        def OpenProcess(self, rights, inherited, pid):
            assert rights in {0x00101000, 0x1000} and not rights & 1 and not inherited
            if pid not in births:
                return 0
            identity = pid, births[pid]
            process = self.originals.setdefault(identity, {"identity": identity, "running": True})
            handle = 100 + len(self.objects)
            self.objects[handle] = process
            if rights == 0x1000:
                self.query_handles.add(handle)
            return handle

        def GetProcessTimes(self, handle, created, exited, kernel, user):
            assert isinstance(created._obj, _NATIVE._FileTime)
            birth = self.objects[handle]["identity"][1]
            created._obj.low, created._obj.high = birth & 0xFFFFFFFF, birth >> 32
            for value in (exited, kernel, user):
                value._obj.low = value._obj.high = 0
            return True

        def WaitForSingleObject(self, handle, timeout):
            assert timeout == 0
            return 258 if self.objects[handle]["running"] else 0

        def GetExitCodeProcess(self, handle, output):
            assert isinstance(output._obj, _NATIVE.wintypes.DWORD)
            output._obj.value = 259 if self.objects[handle]["running"] else 1
            return True

        def CloseHandle(self, handle):
            (self.query_closed if handle in self.query_handles else self.closed).append(handle)
            return True

    kernel = Kernel()
    monkeypatch.setattr(_NATIVE.ctypes, "get_last_error", lambda: 87, raising=False)
    cohort.native.api = kernel
    cohort.native._handle_creation = _NATIVE.NativeProcessTree._handle_creation.__get__(
        cohort.native
    )
    cohort.native._handle_birth = _NATIVE.NativeProcessTree._handle_birth.__get__(cohort.native)
    cohort.native._live_parent_birth = _NATIVE.NativeProcessTree._live_parent_birth.__get__(
        cohort.native
    )
    cohort.native.parent_creation = _NATIVE.NativeProcessTree.parent_creation.__get__(cohort.native)
    cohort.native.pin_service_parent = _NATIVE.NativeProcessTree.pin_service_parent.__get__(
        cohort.native
    )
    cohort.native.admit_console_host = _NATIVE.NativeProcessTree.admit_console_host.__get__(
        cohort.native
    )
    cohort.native._verify_console_parent = _NATIVE.NativeProcessTree._verify_console_parent.__get__(
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
    assert {row["identity"][0] for row in kernel.objects.values()} == {12}
    assert not cohort.console_pins


def test_console_host_under_host_wrapper_cannot_borrow_an_allowed_parent_role(monkeypatch):
    cohort, kernel, _ = _console_cohort(monkeypatch, parent_kind="host")
    with pytest.raises(RuntimeError):
        cohort.snapshot()
    assert cohort.failed and not kernel.objects


@pytest.mark.parametrize("phase", ["before_child_query", "during_os_custody"])
def test_exact_pinned_parent_exit_after_stable_snapshot_does_not_disown_console(monkeypatch, phase):
    cohort, kernel, births = _console_cohort(monkeypatch)
    original_details = cohort.native.details
    original_custody = _NATIVE.admit_system_console_file
    parent_identity = 12, births[12]
    child_identity = 13, births[13]
    expired = []

    def expire_original_parent():
        if expired:
            return
        assert cohort.native.last_identity_edges[child_identity] == parent_identity
        parent_pin = cohort.parent_pins[parent_identity]
        assert parent_pin.identity == parent_identity
        kernel.objects[parent_pin.handle]["running"] = False
        births.pop(parent_identity[0])
        assert cohort.native._handle_birth(parent_pin.handle) is None
        assert cohort.native._handle_creation(parent_pin.handle) == parent_identity[1]
        expired.append(parent_pin)

    def details(identity):
        if identity == child_identity and phase == "before_child_query":
            expire_original_parent()
        return original_details(identity)

    def custody(image, system):
        result = original_custody(image, system)
        if phase == "during_os_custody":
            expire_original_parent()
        return result

    cohort.native.details = details
    monkeypatch.setattr(_NATIVE, "admit_system_console_file", custody)
    assert {process.pid for process in cohort.snapshot()} == {11, 12, 13}
    assert len(expired) == 1 and not cohort.failed
    parent_pin, child_pin = expired[0], cohort.console_pins[child_identity]
    assert child_pin.facts["parent_created_100ns"] == parent_identity[1] > 2**53
    assert child_pin.facts["admitted_parent_identity_verified"] is True
    assert parent_pin.exited() and parent_pin.cached_exit == {"exited": True, "exit_code": 1}
    parent_handle, child_handle = parent_pin.handle, child_pin.handle
    assert not kernel.closed  # Retained original parent still backs the admitted child.
    kernel.objects[child_handle]["running"] = False
    births.clear()
    _finish_ready(cohort)
    cohort.finish()
    assert not cohort.failed
    assert kernel.closed == [child_handle, parent_handle]
    assert child_pin.handle is parent_pin.handle is None
    assert not cohort.console_individual_termination_used


@pytest.mark.parametrize(
    "fault",
    [
        "parent_pid_reused",
        "missing_edge",
        "wrong_edge_birth",
        "changed_current_topology",
        "missing_parent_pin",
        "closed_parent_pin",
        "foreign_pin_kind",
        "foreign_pin_sid",
        "foreign_pin_native",
        "wrong_pin_identity",
        "changed_handle_creation",
        "parent_query_failure",
        "current_parent_query_failure",
        "unavailable_live_parent",
    ],
)
def test_pinned_parent_exit_exception_never_waives_exact_edge_or_native_proof(monkeypatch, fault):
    cohort, kernel, births = _console_cohort(monkeypatch)
    original_details = cohort.native.details
    parent_identity, child_identity = (12, births[12]), (13, births[13])
    changed = []

    def details(identity):
        if identity == child_identity and not changed:
            changed.append(True)
            parent_pin = cohort.parent_pins[parent_identity]
            if fault == "parent_pid_reused":
                kernel.objects[parent_pin.handle]["running"] = False
                births[12] += 1
            elif fault == "missing_edge":
                cohort.native.last_identity_edges.pop(child_identity)
            elif fault == "wrong_edge_birth":
                cohort.native.last_identity_edges[child_identity] = (12, parent_identity[1] + 1)
            elif fault == "changed_current_topology":
                cohort.native.last_parents[13] = 99
            elif fault == "missing_parent_pin":
                cohort.parent_pins.pop(parent_identity).close()
            elif fault == "closed_parent_pin":
                parent_pin.close()
            elif fault == "foreign_pin_kind":
                parent_pin.kind = "host"
            elif fault == "foreign_pin_sid":
                parent_pin.service_sid = b"foreign TokenUser"
            elif fault == "foreign_pin_native":
                parent_pin.native = object()
            elif fault == "wrong_pin_identity":
                parent_pin.identity = (12, parent_identity[1] + 1)
            elif fault == "changed_handle_creation":
                kernel.objects[parent_pin.handle]["identity"] = (12, parent_identity[1] + 1)
            elif fault == "parent_query_failure":
                original_query = kernel.GetProcessTimes
                kernel.GetProcessTimes = lambda handle, *values: (
                    False if handle == parent_pin.handle else original_query(handle, *values)
                )
            elif fault == "current_parent_query_failure":

                def unavailable_birth(pid):
                    raise OSError("unretained original parent query detail")

                cohort.native.parent_creation = unavailable_birth
            elif fault == "unavailable_live_parent":
                # Fresh PID lookup is absent while retained original is live.
                cohort.native.parent_creation = lambda pid: None
        return original_details(identity)

    cohort.native.details = details
    with pytest.raises(RuntimeError):
        cohort.snapshot()
    assert changed and cohort.failed and not cohort.console_pins
    assert child_identity not in cohort.processes
    assert all(
        handle in kernel.closed
        for handle, process in kernel.objects.items()
        if process["identity"] == child_identity
    )
    note = cohort.ownership_records[child_identity]
    assert note["accepted"] is False and note["outcome"] == "proof_unavailable"


@pytest.mark.parametrize("changed", ["exact_edge", "current_topology"])
def test_parent_edge_is_rechecked_after_console_os_custody(monkeypatch, changed):
    cohort, kernel, births = _console_cohort(monkeypatch)
    child_identity = 13, births[13]
    original_custody = _NATIVE.admit_system_console_file

    def custody(image, system):
        result = original_custody(image, system)
        if changed == "exact_edge":
            cohort.native.last_identity_edges[child_identity] = (12, births[12] + 1)
        else:
            cohort.native.last_parents[13] = 99
        return result

    monkeypatch.setattr(_NATIVE, "admit_system_console_file", custody)
    with pytest.raises(RuntimeError):
        cohort.snapshot()
    assert cohort.failed and not cohort.console_pins
    child_handles = [
        handle for handle, row in kernel.objects.items() if row["identity"] == child_identity
    ]
    assert child_handles and all(handle in kernel.closed for handle in child_handles)


@pytest.mark.parametrize("fault", ["sid", "session", "image", "birth", "query"])
def test_service_parent_pin_independently_rechecks_native_authority_and_closes_on_failure(
    monkeypatch, fault
):
    cohort, kernel, births = _console_cohort(monkeypatch)
    identity = 12, births[12]
    original_details = cohort.native.details
    expected = original_details(identity)["image"]

    def details(value):
        result = dict(original_details(value))
        if fault == "sid":
            result["sid"] = b"foreign TokenUser"
        elif fault == "session":
            result["session"] = 1
        elif fault == "image":
            result["image"] = r"C:\foreign\python.exe"
        return result

    cohort.native.details = details
    if fault == "birth":
        identity = 12, births[12] + 1
    elif fault == "query":
        kernel.GetProcessTimes = lambda *_args: False
    with pytest.raises((OSError, RuntimeError)):
        cohort.native.pin_service_parent(identity, "python", _SERVICE_SID, expected)
    assert len(kernel.objects) == 1 and kernel.closed == list(kernel.objects)


def test_retained_parent_close_failure_preserves_handle_and_does_not_terminate(monkeypatch):
    cohort, kernel, _ = _console_cohort(monkeypatch)
    cohort.snapshot()
    parent_pin = next(iter(cohort.parent_pins.values()))
    original_handle = parent_pin.handle
    kernel.CloseHandle = lambda _handle: False
    with pytest.raises(RuntimeError, match="released"):
        parent_pin.close()
    assert parent_pin.handle == original_handle
    assert kernel.objects[original_handle]["running"]
    assert not cohort.console_individual_termination_used


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
    parent_handles = [parent_pin.handle for parent_pin in cohort.parent_pins.values()]
    kernel.objects[handle]["running"] = False
    births.clear()
    _finish_ready(cohort)
    cohort.finish()
    assert not cohort.failed and kernel.closed == [handle, *parent_handles]
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
    parent_handles = [parent_pin.handle for parent_pin in cohort.parent_pins.values()]
    _finish_ready(cohort)
    cohort.service_stopped = False

    def observe(_cohort, *, service_only=False):
        assert service_only
        calls.append("existing_wait")
        raise RuntimeError("Live helper must fail cleanup")

    monkeypatch.setattr(_DESKTOP, "_service_control", lambda *_args: calls.append("stop"))
    monkeypatch.setattr(_DESKTOP, "_wait_owned_cleanup", observe)
    cohort.finish()
    assert calls == ["stop", "existing_wait"]
    assert cohort.failed and kernel.closed == [handle, *parent_handles]
    assert pin.cached_exit == {"exited": False, "exit_code": None}
    assert not cohort.console_individual_termination_used


def test_finish_drains_service_without_misclassifying_a_live_gui_root(monkeypatch):
    cohort, kernel, births = _console_cohort(monkeypatch)
    cohort.snapshot()
    gui_identity = 17, _BASE + 17 * _STEP
    births[17] = gui_identity[1]
    cohort.add(SimpleNamespace(pid=17), role="gui")
    gui = next(p for p in cohort.snapshot() if p.pid == 17)
    gui.name = lambda: "K5Vision.exe"
    for pid in (11, 12, 13):
        births.pop(pid)
    pin = next(iter(cohort.console_pins.values()))
    kernel.objects[pin.handle]["running"] = False
    # The original helper still requires ALL roots unless service-only scope
    # is explicitly requested by finish's engine drain.
    with pytest.raises(pytest.fail.Exception):
        _DESKTOP._wait_owned_cleanup(cohort, timeout=0)
    _finish_ready(cohort)
    cohort.finish()
    assert not cohort.failed and cohort.first_failure is None
    assert cohort.is_owned_running(gui) and pin.cached_exit["exited"]
    assert cohort.last_cleanup_observation["scope"] == "service"
    assert not any(cohort.last_cleanup_observation["live_counts"].values())
    killed = []
    gui.kill = lambda: (killed.append(gui_identity), births.pop(17))
    gui.wait = lambda timeout: 0
    assert cohort.terminate_owned(gui) is True
    assert killed == [gui_identity] and not cohort.failed


@pytest.mark.parametrize("survivor", ["console", "service"])
def test_service_only_finish_scope_still_fails_actual_engine_survivors(monkeypatch, survivor):
    cohort, kernel, births = _console_cohort(monkeypatch)
    cohort.snapshot()
    pin = next(iter(cohort.console_pins.values()))
    kernel.objects[pin.handle]["running"] = survivor == "console"
    if survivor == "console":
        births.pop(11)
        births.pop(12)
    else:
        births.pop(12)
        births.pop(13)
    for process in cohort.processes.values():
        process.name = lambda: "conhost.exe" if survivor == "console" else "K5VisionRecorder.exe"
    original = _DESKTOP._wait_owned_cleanup
    monkeypatch.setattr(
        _DESKTOP, "_wait_owned_cleanup", lambda c, **kwargs: original(c, timeout=0, **kwargs)
    )
    _finish_ready(cohort)
    cohort.finish()
    assert cohort.failed
    assert cohort.first_failure["phase"] == "finish_drain"
    assert cohort.first_failure["exception_type"] == "Failed"
    assert cohort.last_cleanup_observation["scope"] == "service"
    assert any(cohort.last_cleanup_observation["live_counts"].values())
    assert kernel.closed and not cohort.console_individual_termination_used


def test_observer_retains_first_finite_failure_without_private_exception_content():
    cohort = _DESKTOP._Cohort.__new__(_DESKTOP._Cohort)
    cohort.failed, cohort.first_failure = False, None
    cohort.stop = SimpleNamespace(wait=lambda _seconds: False)
    error = OSError("private path, credentials and command must stay unretained")
    error.winerror = 5

    def failed_snapshot():
        raise error

    cohort.snapshot = failed_snapshot
    cohort._observe()
    assert cohort.failed
    note = cohort.first_failure
    assert note["phase"] == "observer_snapshot" and note["exception_type"] == "OSError"
    assert note["native_code"] == 5
    assert note["source"]["file"] == "test_windows_desktop_app.py"
    assert note["source"]["function"] == "_observe"
    assert "private path" not in str(note) and "credentials" not in str(note)
    cohort._record_failure(RuntimeError("later private failure"), "finish_pin_close")
    assert cohort.first_failure is note


@pytest.mark.parametrize(
    "fault", ["exit_query", "creation_query", "release", "dead_reused_pid", "access_denied"]
)
def test_fresh_parent_native_failures_or_dead_pid_reuse_are_not_exit_proof(monkeypatch, fault):
    cohort, kernel, births = _console_cohort(monkeypatch)
    original_details = cohort.native.details
    original_open, original_exit = kernel.OpenProcess, kernel.GetExitCodeProcess
    original_times, original_close = kernel.GetProcessTimes, kernel.CloseHandle
    changed = []

    def open_process(rights, inherited, pid):
        if rights == 0x1000 and fault == "access_denied":
            return 0
        handle = original_open(rights, inherited, pid)
        if rights == 0x1000 and fault == "dead_reused_pid":
            kernel.objects[handle]["running"] = False
        return handle

    def exit_code(handle, output):
        return (
            False
            if handle in kernel.query_handles and fault == "exit_query"
            else original_exit(handle, output)
        )

    def creation(handle, *values):
        return (
            False
            if handle in kernel.query_handles and fault == "creation_query"
            else original_times(handle, *values)
        )

    def close(handle):
        return (
            False
            if handle in kernel.query_handles and fault == "release"
            else original_close(handle)
        )

    def details(identity):
        if identity[0] == 13 and not changed:
            changed.append(True)
            original_pin = cohort.parent_pins[(12, births[12])]
            kernel.objects[original_pin.handle]["running"] = False
            if fault == "dead_reused_pid":
                births[12] += 1
            kernel.OpenProcess, kernel.GetExitCodeProcess = open_process, exit_code
            kernel.GetProcessTimes, kernel.CloseHandle = creation, close
            if fault == "access_denied":
                monkeypatch.setattr(_NATIVE.ctypes, "get_last_error", lambda: 5)
        return original_details(identity)

    cohort.native.details = details
    with pytest.raises(RuntimeError):
        cohort.snapshot()
    assert changed and cohort.failed and not cohort.console_pins
    assert cohort.ownership_records[(13, births[13])]["outcome"] == "proof_unavailable"
    if fault not in {"access_denied", "release"}:
        assert kernel.query_handles and set(kernel.query_closed) == kernel.query_handles
    if fault == "release":
        assert kernel.query_handles and not kernel.query_closed
    assert all(
        handle in kernel.closed
        for handle, process in kernel.objects.items()
        if process["identity"][0] == 13
    )


@pytest.mark.parametrize(
    "phase", ["initial_exit_query", "final_exit_query", "live_authority_lookup"]
)
def test_new_parent_pin_native_query_failure_is_not_silently_skipped(monkeypatch, phase):
    cohort, kernel, births = _console_cohort(monkeypatch)
    births.pop(13)
    cohort.native.last_parents.pop(13)
    original_exit, original_details = kernel.GetExitCodeProcess, cohort.native.details
    queries, details_calls = [], []

    def exit_code(handle, output):
        queries.append(handle)
        fail_at = 1 if phase == "initial_exit_query" else 2
        if phase != "live_authority_lookup" and len(queries) == fail_at:
            return False
        return original_exit(handle, output)

    def details(identity):
        if identity[0] == 12:
            details_calls.append(identity)
            if phase == "live_authority_lookup" and len(details_calls) == 2:
                raise ProcessLookupError("Unretained native authority detail")
        return original_details(identity)

    kernel.GetExitCodeProcess, cohort.native.details = exit_code, details
    with pytest.raises(RuntimeError):
        cohort.snapshot()
    assert cohort.failed and not cohort.parent_pins and not cohort.console_pins
    assert kernel.objects and kernel.closed == list(kernel.objects)
    assert cohort.ownership_records[(12, births[12])]["outcome"] == "proof_unavailable"


@pytest.mark.parametrize("target", ["parent", "child"])
def test_failed_pin_transfer_cannot_hide_native_release_failure_as_disappearance(
    monkeypatch, target
):
    cohort, kernel, births = _console_cohort(monkeypatch)
    target_pid = 12 if target == "parent" else 13
    if target == "parent":
        births.pop(13)
        cohort.native.last_parents.pop(13)
    original_open, original_close = kernel.OpenProcess, kernel.CloseHandle
    rejected_handles = []

    def open_exited(rights, inherited, pid):
        handle = original_open(rights, inherited, pid)
        if rights == 0x00101000 and pid == target_pid:
            kernel.objects[handle]["running"] = False
            rejected_handles.append(handle)
        return handle

    def close(handle):
        return False if handle in rejected_handles else original_close(handle)

    kernel.OpenProcess, kernel.CloseHandle = open_exited, close
    with pytest.raises(RuntimeError):
        cohort.snapshot()
    assert rejected_handles and cohort.failed and not cohort.console_pins
    assert all(handle not in kernel.closed for handle in rejected_handles)
    assert (
        cohort.ownership_records[(target_pid, births[target_pid])]["outcome"] == "proof_unavailable"
    )


@pytest.mark.parametrize(
    "name,category",
    [
        ("WerFaultSecure.exe", "werfaultsecure"),
        ("wermgr.exe", "wermgr"),
        ("cmd.exe", "cmd"),
        ("powershell.exe", "powershell"),
        ("pwsh.exe", "pwsh"),
        ("wmic.exe", "wmic"),
        ("w32tm.exe", "w32tm"),
        ("sc.exe", "sc"),
        ("ffmpeg.exe", "ffmpeg"),
        ("ffprobe.exe", "ffprobe"),
        ("secret-tenant-name.exe", "other"),
    ],
)
def test_rejected_known_basename_is_finite_and_never_admitted(monkeypatch, name, category):
    image = str(PureWindowsPath(r"C:\private tenant path") / name)
    details = _details(12, _BASE, 11)
    details["image"] = image
    monkeypatch.setattr(
        _NATIVE,
        "_fixed_image_file_diagnostic",
        lambda *_args, **_kwargs: {"status": "different_fixed_file"},
    )
    with pytest.raises(_NATIVE.OwnershipFailure) as failure:
        _NATIVE.service_candidate(
            details,
            _SERVICE_SID,
            _image_inventory(),
            diagnose_image=lambda actual, images: _NATIVE.image_diagnostic(
                actual, images, same_file=lambda *_: False
            ),
        )
    note = failure.value.diagnostic
    assert note["accepted"] is False and note["outcome"] == "unexpected_service_image"
    assert note["image_diagnostic"]["basename_category"] == category
    encoded = str(note)
    assert image not in encoded and "private tenant" not in encoded
    assert "secret-tenant-name" not in encoded


@pytest.mark.parametrize("kind", ["wrapper", "host", "python", "mediamtx", "caddy"])
def test_admitted_service_image_does_not_run_failure_observation(kind):
    details = _details(12, _BASE, 11, kind=kind)

    def forbidden(*_args):
        pytest.fail("Success-path admission performed failure-only file observation")

    note = _NATIVE.service_candidate(
        details, _SERVICE_SID, _image_inventory(), diagnose_image=forbidden
    )
    assert note["accepted"] is True and "image_diagnostic" not in note


def test_unlisted_basename_never_resolves_or_opens_known_file(monkeypatch):
    def forbidden(*_args):
        pytest.fail("Unlisted image attempted fixed-path resolution or hashing")

    monkeypatch.setattr(_NATIVE, "_fixed_image_target", forbidden)
    result = _NATIVE._fixed_image_file_diagnostic(
        r"C:\private\secret-customer.exe", same_file=forbidden
    )
    assert result == {"status": "unlisted_basename"}
    assert "secret" not in str(result)


def _fixed_file_fixture(monkeypatch, tmp_path):
    import hashlib

    from k5vision import installed_code_security, windows_identity_security

    expected = tmp_path / "fixed-code" / "cmd.exe"
    expected.parent.mkdir()
    expected.write_bytes(b"actual retained fixed code bytes")
    # Bypass only the OS selector in this platform-independent filesystem test.
    # The actual custody traversal, same-file prerequisite, hash and snapshots run.
    monkeypatch.setattr(_NATIVE, "os", SimpleNamespace(name="nt", fstat=os.fstat))
    monkeypatch.setattr(
        _NATIVE,
        "_fixed_image_target",
        lambda _name: (expected, expected.parent, "fixed_system32"),
    )
    monkeypatch.setattr(installed_code_security, "_native_security", lambda: "native")
    checks = []
    monkeypatch.setattr(
        installed_code_security,
        "_code_policy",
        lambda native, path, *, within: checks.append((native, path, within)),
    )
    monkeypatch.setattr(windows_identity_security, "check_windows_volume", lambda *_: None)
    return expected, checks, hashlib.sha256(expected.read_bytes()).hexdigest()


def test_fixed_known_image_hash_requires_same_file_and_custody(monkeypatch, tmp_path):
    expected, checks, sha = _fixed_file_fixture(monkeypatch, tmp_path)
    comparisons = []

    def same_file(actual, target):
        comparisons.append((actual, target))
        return True

    image = r"C:\Windows\System32\cmd.exe"
    result = _NATIVE._fixed_image_file_diagnostic(image, same_file=same_file)
    assert result["status"] == "observed" and result["same_file"] is True
    assert result["scope"] == "fixed_system32"
    assert result["trusted_code_custody_verified"] is True
    assert result["file_sha256"] == sha and result["file_bytes"] == expected.stat().st_size
    assert result["file_identity"]["file_id"] == expected.stat().st_ino
    assert len(comparisons) == 2 and all(row[1] == str(expected) for row in comparisons)
    assert checks and checks[-1] == ("native", expected, True)
    assert str(expected) not in str(result) and image not in str(result)


def test_same_basename_outside_fixed_target_never_hashes(monkeypatch, tmp_path):
    _expected, checks, _sha = _fixed_file_fixture(monkeypatch, tmp_path)

    def forbidden(*_args, **_kwargs):
        pytest.fail("Different file content was opened or hashed")

    monkeypatch.setattr(_NATIVE.hashlib, "file_digest", forbidden)
    result = _NATIVE._fixed_image_file_diagnostic(r"C:\private\cmd.exe", same_file=lambda *_: False)
    assert result == {
        "status": "different_fixed_file",
        "scope": "fixed_system32",
        "same_file": False,
    }
    assert checks == []


@pytest.mark.parametrize("fault", ["custody", "reparse", "changed_identity"])
def test_fixed_image_observation_faults_preserve_original_rejection(monkeypatch, tmp_path, fault):
    from k5vision import installed_code_security

    expected, _checks, _sha = _fixed_file_fixture(monkeypatch, tmp_path)
    if fault == "custody":

        def denied(*_args, **_kwargs):
            raise PermissionError("private path / secret source URI")

        monkeypatch.setattr(installed_code_security, "_code_policy", denied)
    elif fault == "reparse":
        target = expected.with_suffix(".real")
        expected.rename(target)
        expected.symlink_to(target)
    count = 0

    def compare(_actual, target):
        nonlocal count
        if target == str(expected):
            count += 1
            return fault != "changed_identity" or count < 2
        return False

    details = _details(12, _BASE, 11)
    details["image"] = r"C:\Windows\System32\cmd.exe"
    with pytest.raises(_NATIVE.OwnershipFailure) as failure:
        _NATIVE.service_candidate(
            details,
            _SERVICE_SID,
            _image_inventory(),
            diagnose_image=lambda actual, images: _NATIVE.image_diagnostic(
                actual, images, same_file=compare
            ),
        )
    result = failure.value.diagnostic
    assert result["accepted"] is False and result["outcome"] == "unexpected_service_image"
    note = result["image_diagnostic"]["fixed_known_file"]
    assert note["status"] == "unavailable" and "file_sha256" not in note
    assert "secret" not in str(result) and str(tmp_path) not in str(result)


def test_known_file_facts_survive_actual_failure_property_without_private_source(
    monkeypatch, tmp_path
):
    import ast
    import json

    expected, _checks, sha = _fixed_file_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(_DESKTOP, "_helpers", lambda _name: _NATIVE)
    monkeypatch.setattr(
        _NATIVE, "_same_local_file", lambda _actual, target: target == str(expected)
    )
    births, parents = {11: _BASE, 12: _BASE + 1}, {12: 11}
    details = _details(12, births[12], 11)
    details["image"] = r"C:\Windows\System32\cmd.exe"
    cohort = _cohort(monkeypatch, births, parents, {12: details})
    cohort.add(SimpleNamespace(pid=11), role="service")
    with pytest.raises(_NATIVE.OwnershipFailure):
        cohort.snapshot()
    assert cohort.failed
    # Execute the actual normal Motion failure-property projection, not a mirror.
    source = ast.parse((_ROOT / "test_windows_motion_recording.py").read_text())
    failure = next(
        node
        for node in ast.walk(source)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.BoolOp)
        and ast.unparse(node.test) == "failed or cleanup_failed"
    )
    properties = []
    namespace = {
        "diagnostic": {
            "stage": "owned_stop_and_uninstall",
            "process_ownership": cohort.ownership_diagnostic(),
            "private_source_url": "rtsp://private-user:secret-password@private-camera",
        },
        "failed": True,
        "cleanup_failed": True,
        "json": json,
        "record": lambda name, value: properties.append((name, json.loads(value))),
    }
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body=failure.body[:3], type_ignores=[])),
            "actual_normal_motion_failure_property",
            "exec",
        ),
        namespace,
    )
    assert len(properties) == 1 and properties[0][0] == "motion_windows_diagnostic"
    emitted = properties[0][1]
    refused = next(row for row in emitted["process_ownership"]["instances"] if not row["accepted"])
    assert refused["image_diagnostic"]["basename_category"] == "cmd"
    assert refused["image_diagnostic"]["fixed_known_file"]["file_sha256"] == sha
    assert emitted["failed"] is True and emitted["cleanup_failed"] is True
    assert "secret-password" not in json.dumps(emitted) and str(tmp_path) not in json.dumps(emitted)


@pytest.mark.skipif(os.name != "nt", reason="Fixed System32 evidence requires native Windows")
def test_native_fixed_known_file_diagnostic_still_rejects_system_command():
    native = _NATIVE.NativeProcessTree()
    image = str(PureWindowsPath(native.system_directory()) / "cmd.exe")
    details = _details(12, _BASE, 11)
    details["image"] = image
    with pytest.raises(_NATIVE.OwnershipFailure) as failure:
        _NATIVE.service_candidate(
            details,
            _SERVICE_SID,
            _image_inventory(),
            diagnose_image=_NATIVE.image_diagnostic,
        )
    note = failure.value.diagnostic
    assert not note["accepted"] and note["outcome"] == "unexpected_service_image"
    facts = note["image_diagnostic"]["fixed_known_file"]
    assert facts["status"] == "observed" and facts["scope"] == "fixed_system32"
    assert facts["same_file"] and facts["trusted_code_custody_verified"]
    assert len(facts["file_sha256"]) == 64 and facts["file_bytes"] > 0
    assert image not in str(note)


@pytest.mark.parametrize("changed_type", [False, True])
def test_windows_executable_path_mode_and_handle_type_comparison(
    monkeypatch, tmp_path, changed_type
):
    import stat

    expected, _checks, sha = _fixed_file_fixture(monkeypatch, tmp_path)
    expected.chmod(0o755)
    native_fstat = os.fstat

    def handle_stat(fd):
        info = native_fstat(fd)
        return SimpleNamespace(
            st_dev=info.st_dev,
            st_ino=info.st_ino,
            # Model CPython Windows fstat: no path-based synthetic .exe 0111.
            st_mode=(stat.S_IFDIR if changed_type else stat.S_IFREG)
            | (stat.S_IMODE(info.st_mode) & ~0o111),
            st_nlink=info.st_nlink,
            st_size=info.st_size,
            st_mtime_ns=info.st_mtime_ns,
            st_file_attributes=getattr(info, "st_file_attributes", 0),
        )

    monkeypatch.setattr(_NATIVE.os, "fstat", handle_stat)
    if changed_type:
        with pytest.raises(ValueError, match="open identity changed"):
            _NATIVE._fixed_image_file_diagnostic(
                r"C:\Windows\System32\cmd.exe", same_file=lambda *_: True
            )
    else:
        facts = _NATIVE._fixed_image_file_diagnostic(
            r"C:\Windows\System32\cmd.exe", same_file=lambda *_: True
        )
        assert facts["status"] == "observed" and facts["file_sha256"] == sha
