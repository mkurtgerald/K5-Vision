"""Portable contract tests only; synthetic records never establish native evidence."""

from __future__ import annotations

import ctypes
import importlib.util
import io
import json
import queue
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "_facade_witness_test", ROOT / "scripts/installed_alpha_facade_witness.py"
)
assert SPEC is not None and SPEC.loader is not None
witness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(witness)
common, boundary = witness.common, witness.boundary


@pytest.fixture(autouse=True)
def bounded_test_case_id(request):
    # PYTEST_CURRENT_TEST includes this ID on Windows before each test phase.
    assert len(request.node.nodeid) <= 512, "Use bounded semantic IDs for fixture payloads"


def test_hosted_workflow_selects_facade_contracts_and_only_nonmedia_observer():
    workflow = (ROOT / ".github/workflows/windows-alpha-script-smoke.yml").read_text(
        encoding="utf8"
    )
    for path in (
        "scripts/installed_alpha_facade_witness.py",
        "tests/test_installed_alpha_facade_witness.py",
        "tests/test_windows_alpha_facade_witness.py",
    ):
        assert workflow.count('      - "' + path + '"') == 2
    selected = next(
        line.strip().split() for line in workflow.splitlines() if line.strip().startswith("pytest ")
    )
    for path in (
        "tests/test_one_file_windows_bootstrap.py",
        "tests/test_windows_alpha_bootstrap.py",
        "tests/test_public_test_operator_runtime.py",
        "tests/test_windows_alpha_direct_witness.py",
        "tests/test_app_resource_lifetime.py",
        "tests/test_windows_alpha_analytics.py",
        "tests/test_cli.py",
        "tests/test_installed_alpha_facade_witness.py",
    ):
        assert selected.count(path) == 1
    command = (
        "python -m pytest tests/test_windows_alpha_facade_witness.py::"
        "test_windows_facade_process_observer_real_owned_births --no-cov -q -rP"
    )
    assert workflow.count(command) == 1
    assert workflow.index("$env:K5_WITNESS_BASE_PYTHON_SHA256 =") < workflow.index(command)
    assert command + "\n          if ($LASTEXITCODE -ne 0)" in workflow
    assert "K5_FACADE_NATIVE_INPUTS" not in workflow
    assert "test_windows_actual_installed_test_and_two_run_facades" not in workflow
    assert workflow.split("permissions:\n", 1)[1].split("\njobs:\n", 1)[0].strip() == (
        "contents: read"
    )


def expected(installed=True):
    names = witness.IDENTITIES if installed else witness.INPUT_IDENTITIES
    return {
        "revision": "a" * 40,
        "run_nonce": "b" * 32,
        **{key: common.digest(key) for key in names},
    }


def observation(mode):
    """An invented test vector, deliberately NOT a qualified native inventory."""
    value = {
        "schema_version": "owned-facade-observation-v1",
        "mode": mode,
        "error": "none",
        **dict.fromkeys(witness.OBS_COUNTS, 0),
        **dict.fromkeys(witness.OBS_BOOLEANS, True),
    }
    for kind in witness.allowed_images(mode):
        value["process_" + kind] = 1
    value["job_total"] = value["distinct_births"] = len(witness.allowed_images(mode))
    value.update(policy_ps1_files=1, policy_psm1_files=1)
    for action in ("added", "modified", "removed"):
        value["temp_policy_probe_" + action] = 2
    if mode.startswith("run_"):
        value["temp_alpha_session_added"] = value["temp_alpha_session_removed"] = 1
    value["temp_events"] = sum(value[key] for key in witness.TEMP_COUNTS)
    return value


def run_result():
    return {
        "completed": True,
        "analytics_enabled": True,
        "cleanup_complete": True,
        "delivered_frames": 225,
        "presentations": 225,
        "analytics_provider_submissions": 40,
        "analytics_provider_completions": 39,
        "analytics_failures": 0,
    }


def receipt():
    value = witness.new_receipt(expected())
    value.update(
        {key: key not in {"person_box_acceptance", "installer_acceptance"} for key in witness.FLAGS}
    )
    for mode in witness.MODES:
        value[mode + "_observation"] = observation(mode)
    for attempt in (1, 2):
        value.update({f"run_{attempt}_{key}": item for key, item in run_result().items()})
    return value


def test_distinct_source_free_receipt_and_existing_route_unchanged():
    value = receipt()
    witness.validate_receipt(value, expected())
    assert len(common.canonical(value)) <= common.MAX_BYTES
    assert value["schema_version"] != witness.alpha.SCHEMA
    assert witness.RECEIPT_NAME != witness.alpha.RECEIPT_NAME
    assert witness.EXPECTATIONS_NAME != witness.alpha.EXPECTATIONS_NAME
    assert witness.alpha.boundary.EXPECTED_PROCESSES == {
        "base_python": 2,
        "powershell": 1,
        "venv_python": 1,
        "console_host": 1,
        "dotnet_compiler": 0,
        "unknown": 0,
    }
    assert witness.FacadeProcessObserver._read is not boundary.ProcessObserver._read
    assert witness.FacadeProcessObserver.finish is not boundary.ProcessObserver.finish
    assert witness.FacadeTempObserver.finish is boundary.TempObserver.finish
    assert witness.FacadeObservation.close is boundary.PreflightObservation.close


@pytest.mark.parametrize("key", sorted(witness.IDENTITIES | {"revision", "run_nonce"}))
def test_all_independent_identities_are_required_and_exact(key):
    value = receipt()
    value[key] = "c" * len(value[key])
    with pytest.raises(common.WitnessError):
        witness.validate_receipt(value, expected())


@pytest.mark.parametrize("key", sorted(witness.FLAGS))
@pytest.mark.parametrize("bad", [None, 0, 1, "true"])
def test_flags_do_not_coerce(key, bad):
    value = receipt()
    value[key] = bad
    with pytest.raises(common.WitnessError):
        witness.validate_receipt(value, expected())


@pytest.mark.parametrize(
    "change",
    [
        {"source_uri": "rtsp://secret"},
        {"schema_version": witness.alpha.SCHEMA},
        {"acceptance_scope": "installer-upgrade"},
        {"run_1_delivered_frames": 224},
        {"run_2_analytics_provider_submissions": 0},
        {"installer_acceptance": True},
        {"person_box_acceptance": True},
        {"cleanup_complete": False},
    ],
)
def test_receipt_refuses_forged_acceptance(change):
    value = receipt()
    value.update(change)
    with pytest.raises((common.WitnessError, boundary.ObservationFailure)):
        witness.validate_receipt(value, expected())


@pytest.mark.parametrize("mode", sorted(witness.MODES))
@pytest.mark.parametrize(
    "change",
    [
        {"job_total": 33},
        {"job_active": 1},
        {"distinct_births": 0},
        {"process_unknown": 1},
        {"process_gst_launch": True},
        {"process_coverage_complete": False},
        {"cleanup_complete": False},
        {"temporary_root_empty": False},
        {"temp_drain_complete": False},
        {"policy_lifecycles_complete": False},
        {"error": "process_unavailable"},
        {"temp_other_owned_temp_added": 1},
        {"temp_events": 256},
        {"policy_ps1_files": 0},
        {"policy_psm1_files": 3},
        {"birth_pid": 123},
        {"mode": "unknown"},
    ],
)
def test_observation_requires_complete_typed_coverage(mode, change):
    value = observation(mode)
    value.update(change)
    with pytest.raises(boundary.ObservationFailure):
        witness.validate_observation(value, mode)


def test_observed_inventory_is_bounded_but_not_an_invented_exact_profile():
    for mode in witness.MODES:
        value = observation(mode)
        value["process_base_python"] += 2
        value["job_total"] += 2
        value["distinct_births"] += 2
        witness.validate_observation(value, mode)
    value["job_total"] += 1
    with pytest.raises(boundary.ObservationFailure):
        witness.validate_observation(value, mode)


@pytest.mark.parametrize(
    "mode,kind",
    [
        ("test_invalid", "gst_launch"),
        ("test_invalid", "mediamtx"),
        ("test_valid", "gst_inspect"),
        ("test_valid", "gst_scanner"),
        ("test_valid", "mediamtx"),
    ],
)
def test_test_refuses_disallowed_native_images(mode, kind):
    value = observation(mode)
    value["process_" + kind] += 1
    value["job_total"] += 1
    value["distinct_births"] += 1
    with pytest.raises(boundary.ObservationFailure, match="unexpected_process"):
        witness.validate_observation(value, mode)


@pytest.mark.parametrize("mode", ["test_invalid", "test_valid"])
def test_no_session_requires_complete_event_observation(mode):
    value = observation(mode)
    value["temp_alpha_session_added"] = value["temp_alpha_session_removed"] = 1
    value["temp_events"] += 2
    with pytest.raises(boundary.ObservationFailure, match="unexpected_temp"):
        witness.validate_observation(value, mode)


def policy_trace(lifecycle):
    for extension in ("ps1", "psm1"):
        for action in ("added", "modified", "removed"):
            lifecycle.observe("__PSScriptPolicyTest_ab123456.abc." + extension, action)


@pytest.mark.parametrize("mode", sorted(witness.MODES))
def test_temp_lifecycle_admits_complete_policy_only_or_owned_run_session(mode):
    lifecycle = witness.FacadeTempLifecycle(mode)
    policy_trace(lifecycle)
    assert lifecycle.complete()
    name = "K5VisionAlpha-" + "a" * 32 + "\\file"
    if mode.startswith("run_"):
        lifecycle.observe(name, "added")
        lifecycle.observe(name, "removed")
        assert lifecycle.complete()
    else:
        with pytest.raises(boundary.ObservationFailure):
            lifecycle.observe(name, "added")
        assert not lifecycle.complete()


@pytest.mark.parametrize("mode", sorted(witness.MODES))
def test_unrecognized_temp_event_permanently_poisons_completion(mode):
    lifecycle = witness.FacadeTempLifecycle(mode)
    policy_trace(lifecycle)
    with pytest.raises(boundary.ObservationFailure):
        lifecycle.observe("private.sqlite", "added")
    assert not lifecycle.complete()


def test_mode_admission_rejects_unknown_and_coerced_modes():
    for mode in (None, True, "run_3", "test", []):
        with pytest.raises(boundary.ObservationFailure):
            witness.allowed_images(mode)


def test_facade_command_invokes_real_facade_and_never_supplies_source_uri(tmp_path):
    for mode in witness.MODES:
        command = witness.facade_command(
            Path("powershell.exe"), tmp_path / "envelope.ps1", tmp_path, mode, 8123
        )
        facade = "Test" if mode.startswith("test_") else "Run"
        assert str(tmp_path / (facade + "-K5VisionAlpha.ps1")) in command
        assert "Start-K5VisionAlpha.ps1" not in " ".join(command)
        assert "PublicRtspSource" not in witness.ENVELOPE
    assert (
        "& $Facade -InstallRoot $InstallRoot -Port $Port -ExitAfterPublicTest" in witness.ENVELOPE
    )
    assert "Invoke-K5NativeProbe" not in witness.ENVELOPE
    with pytest.raises(common.WitnessError):
        witness.facade_command(Path("powershell.exe"), tmp_path, tmp_path, "run_1", True)


def test_stdout(mode="test_valid"):
    return (
        b"\n".join(
            [
                b"K5 analytics configuration admitted; live provider acceptance is pending.",
                b"k5-vision 0.1.0",
                b"K5 Vision Alpha preflight PASS",
                b"Reviewed K5 revision: " + b"a" * 40,
                b"Reviewed GStreamer: 1.28.7",
                b"No camera was contacted and no camera media was read "
                b"or written by this preflight.",
                b"K5_FACADE_RETURNED",
                b"",
            ]
        )
        if mode == "test_valid"
        else b"K5_FACADE_EXPECTED_REFUSAL\n"
    )


# This helper is not a pytest test, despite producing Test facade output.
test_stdout.__test__ = False


def summary(raw, mode):
    result = witness.FacadeSummary(io.BytesIO(raw), mode, "a" * 40)
    result.finish()
    return result


@pytest.mark.parametrize("mode", ["test_valid", "test_invalid"])
def test_complete_test_stdout_is_recognized_without_persisting_text(mode):
    value = summary(test_stdout(mode), mode)
    assert value.result() == {}
    assert not any(isinstance(item, bytes) for item in vars(value).values())


@pytest.mark.parametrize(
    "bad",
    [
        b"private path\n",
        b"K5_FACADE_RETURNED\n",
        b"K5_FACADE_EXPECTED_REFUSAL\n",
        b"K5_FACADE_FAILED\n",
        b"x" * 65537,
    ],
    ids=[
        "unknown-output",
        "duplicate-return",
        "unexpected-refusal",
        "failed-marker",
        "output-flood",
    ],
)
@pytest.mark.parametrize("mode", ["test_valid", "test_invalid"])
def test_duplicate_unknown_or_flood_test_output_fails(bad, mode):
    value = summary(test_stdout(mode) + bad, mode)
    with pytest.raises(common.WitnessError):
        value.result()


def test_run_reuses_counter_contract_after_actual_facade_return_marker():
    raw = b"\n".join(
        [
            b"K5 analytics configuration admitted; live provider acceptance is pending.",
            b"Starting K5 Vision Alpha local synthetic operator test on http://127.0.0.1:8123",
            b"K5 analytics PASS: submissions=40, completions=39, failures=0",
            b"K5 operator PASS: frames=225, presentations=225",
            b"Exiting after one bounded alpha acceptance run.",
            b"K5_FACADE_RETURNED",
        ]
    )
    assert summary(raw, "run_1").result() == run_result()
    with pytest.raises(common.WitnessError):
        summary(raw.replace(b"K5_FACADE_RETURNED", b""), "run_1").result()


class NativeFacadeHarness:
    """Actual facade constructor, synthetic owned native APIs, and real raw hashes.

    Queue/event waits are deadlock guards, never claims about native scheduling.
    Every synthetic notification gets one fresh handle; its immutable record is
    retained with that handle so PID reuse cannot change an already captured birth.
    """

    def __init__(self, monkeypatch, tmp_path, *, kind="gst_launch", start=True):
        self.path = tmp_path / "private-synthetic-child.exe"
        self.path.write_bytes(b"synthetic executable contents for real per-birth hashing")
        self.real_hash = boundary.file_hash
        self.admitted = {kind: (self.path, self.real_hash(self.path))}
        self.queue = queue.Queue()
        self.local = threading.local()
        self.trace, self.handles, self.closed, self.resources = [], {}, [], []
        self.hashes, self.blockers, self.aborts = [], [], []
        self.aborted = threading.Event()
        self.observer = None
        self.startup_failure = None
        self.close_failures = set()
        self.abort_check = None
        self.job = SimpleNamespace(handle=42, abort=self.abort)
        self.api = SimpleNamespace(
            GetCurrentProcess=lambda: 7,
            DuplicateHandle=self.duplicate,
            CreateIoCompletionPort=self.create_port,
            SetInformationJobObject=self.associate,
            GetQueuedCompletionStatus=self.dequeue,
            PostQueuedCompletionStatus=self.post,
            OpenProcess=self.open,
            IsProcessInJob=self.membership,
            GetProcessTimes=self.times,
            QueryFullProcessImageNameW=self.image,
            CloseHandle=self.close,
        )
        self.job.api = self.api
        monkeypatch.setattr(boundary, "api", lambda: self.api)
        monkeypatch.setattr(
            ctypes, "get_last_error", lambda: getattr(self.local, "error", 0), raising=False
        )
        monkeypatch.setattr(boundary, "file_hash", self.hash)
        if start:
            self.start()

    def start(self):
        self.observer = witness.FacadeProcessObserver(self.job, self.admitted, self.resources)
        return self.observer

    def duplicate(self, current, handle, target, output, access, inherit, options):
        assert (current, handle, target, access, inherit, options) == (7, 42, 7, 0, False, 2)
        if self.startup_failure == "duplicate":
            self.local.error = 5
            return False
        output._obj.value = 456
        self.trace.append(("duplicate", 456))
        return True

    def create_port(self, source, existing, key, concurrency):
        assert existing is None and (key, concurrency) == (0, 1)
        if self.startup_failure == "port":
            self.local.error = 5
            return None
        self.trace.append(("port", 789))
        return 789

    def associate(self, handle, kind, association, size):
        assert handle == 42 and kind == 7
        assert (association._obj.key, association._obj.port) == (1, 789)
        self.trace.append(("associate",))
        self.local.error = 5
        return self.startup_failure != "associate"

    def dequeue(self, port, message, key, value, timeout):
        assert port == 789 and timeout == 100
        try:
            packet = self.queue.get(timeout=timeout / 1000)
        except queue.Empty:
            self.local.error = 258
            return False
        if "ack" in packet:
            packet["ack"].set()
        self.local.record = packet
        message._obj.value = packet.get("message", 6)
        key._obj.value = packet.get("key", 1)
        value._obj.value = packet.get("pid")
        self.trace.append(("dequeue", packet.get("pid")))
        return True

    def post(self, port, message, key, value):
        assert port == 789
        self.queue.put({"message": message, "key": key, "pid": value})
        return True

    def notify(self, pid=10, **record):
        packet = {"pid": pid, "birth": (pid or 0) * 10, **record}
        self.queue.put(packet)
        return packet

    def flush_capture(self):
        event = threading.Event()
        self.queue.put({"message": 0, "key": 1, "ack": event})
        assert event.wait(5), "capture pump did not reach the ordered test barrier"

    def open(self, access, inherit, pid):
        assert (access, inherit) == (0x1000, False)
        self.trace.append(("open", access, inherit, pid))
        record = self.local.record
        if record.get("open_error"):
            self.local.error = record["open_error"]
            return None
        handle = 1000 + len(self.handles)
        self.handles[handle] = dict(record)
        self.trace.append(("opened", handle))
        return handle

    def native_step(self, name, handle):
        record = self.handles[handle]
        self.trace.append((name, handle))
        if record.get("block_at") == name:
            record["entered"].set()
            assert record["release"].wait(5), "native-call test barrier was not released"
        error = record.get(name + "_error", 0)
        self.local.error = error
        return record, not error

    def membership(self, handle, job, output):
        assert job == 456
        record, result = self.native_step("membership", handle)
        output._obj.value = record.get("belongs", True)
        return result

    def times(self, handle, created, *rest):
        record, result = self.native_step("times", handle)
        created._obj.value = record["birth"]
        return result

    def image(self, handle, flags, output, size):
        assert flags == 0 and size._obj.value == 32768
        record, result = self.native_step("image", handle)
        output.value = str(record.get("image", self.path))
        return result

    def close(self, handle):
        assert handle not in self.closed, "an owned native handle was closed twice"
        self.trace.append(("close", handle))
        self.closed.append(handle)
        return handle not in self.close_failures

    def hash(self, path):
        self.hashes.append(path)
        ordinal = len(self.hashes)
        self.trace.append(("hash_enter", ordinal))
        for selected, entered, release, failure in self.blockers:
            if ordinal == selected:
                entered.set()
                assert release.wait(5), "hash test barrier was not released"
                if failure:
                    raise failure
        result = self.real_hash(path)
        self.trace.append(("hash_leave", ordinal))
        return result

    def block_hash(self, ordinal=1, failure=None):
        entered, release = threading.Event(), threading.Event()
        self.blockers.append((ordinal, entered, release, failure))
        return entered, release

    def abort(self):
        self.aborts.append(threading.current_thread())
        if self.abort_check:
            self.abort_check()
        self.aborted.set()

    def finish(self, total):
        self.observer.finish(total)
        self.assert_stopped()

    def assert_stopped(self):
        observer = self.observer
        assert observer.capture_done.is_set() and observer.admission_done.is_set()
        assert not observer.thread.is_alive() and not observer.admission_thread.is_alive()

    def cleanup(self):
        for _, _, release, _ in self.blockers:
            release.set()
        for record in self.handles.values():
            if "release" in record:
                record["release"].set()
        observer = self.observer or getattr(self.job, "observer", None)
        if observer is None:
            return
        observer.stop.set()
        self.post(789, 0, 2, None)
        # Native wake also causes capture_done to notify a waiting validator.
        for name in ("thread", "admission_thread"):
            thread = getattr(observer, name, None)
            if thread is not None and thread.ident is not None:
                thread.join(5)
                assert not thread.is_alive(), "test left an actor running"
        observer._release()


@pytest.fixture
def native_facade(monkeypatch, tmp_path):
    values = []

    def make(*, start=True, **kwargs):
        value = NativeFacadeHarness(monkeypatch, tmp_path, start=False, **kwargs)
        values.append(value)
        if start:
            value.start()
        return value

    yield make
    for value in values:
        value.cleanup()


def test_source_derived_hash_barrier_allows_later_capture_and_post_exit_admission(native_facade):
    harness = native_facade(start=False)
    entered, release = harness.block_hash()
    harness.notify(10)
    harness.notify(20)
    harness.start()
    assert entered.wait(5)
    harness.flush_capture()
    observer = harness.observer
    assert [call[3] for call in harness.trace if call[0] == "open"] == [10, 20]
    assert len(observer.reservations) == 2 and not observer.births
    assert len(harness.hashes) == 1
    assert ("image", 1001) in harness.trace and ("hash_leave", 1) not in harness.trace
    # The second synthetic process now exits. The captured handle/image are the
    # only evidence; reopening or querying after this point would fail this test.
    harness.handles[1001]["image_error"] = 87
    release.set()
    harness.finish(2)
    assert observer.error == "none" and len(observer.births) == len(harness.hashes) == 2
    assert harness.trace.index(("image", 1001)) < harness.trace.index(("hash_leave", 1))
    assert len([call for call in harness.trace if call[0] == "image"]) == 2
    assert set(harness.closed) == {456, 789, 1000, 1001}
    assert observer.release_complete and not observer.handles and not harness.aborts


@pytest.mark.parametrize(
    "record,error,phase,steps",
    [
        ({"open_error": 87}, "process_unavailable", "open_process", []),
        ({"open_error": 5}, "access_denied", "open_process", []),
        ({"open_error": 6}, "native_error", "open_process", []),
        ({"belongs": False}, "ownership_unproven", "job_membership", ["membership"]),
        ({"membership_error": 5}, "access_denied", "job_membership", ["membership"]),
        ({"times_error": 5}, "access_denied", "process_times", ["membership", "times"]),
        ({"birth": 0}, "native_error", "process_times", ["membership", "times"]),
        ({"image_error": 5}, "access_denied", "image_query", ["membership", "times", "image"]),
        ({"image_error": 87}, "native_error", "image_query", ["membership", "times", "image"]),
    ],
    ids=[
        "unavailable",
        "denied",
        "native",
        "foreign",
        "membership",
        "times",
        "zero-birth",
        "image-denied",
        "image-native",
    ],
)
def test_capture_refusal_never_infers_image_or_retries(native_facade, record, error, phase, steps):
    harness = native_facade()
    harness.notify(10, **record)
    assert harness.aborted.wait(5)
    harness.finish(1)
    observer = harness.observer
    assert observer.error == observer.primary_error == observer.capture_error == error
    assert observer.failure_actor == "capture" and observer.failure_phase == phase
    assert observer.failure_ordinal == observer.capture_ordinal == 1
    assert not observer.births and not harness.hashes
    assert [
        call[0] for call in harness.trace if call[0] in {"membership", "times", "image"}
    ] == steps
    assert len([call for call in harness.trace if call[0] == "open"]) == 1
    assert harness.aborts == [observer.thread]
    assert set(harness.closed) == {456, 789, *harness.handles}


@pytest.mark.parametrize(
    "case,error",
    [
        ("unknown", "unexpected_process"),
        ("mismatch", "fixture_admission"),
        ("permission", "access_denied"),
    ],
)
def test_admission_failure_uses_actor_local_phase_and_real_per_birth_hash(
    native_facade, case, error
):
    harness = native_facade()
    entered, release = harness.block_hash(
        2, PermissionError("private-secret") if case == "permission" else None
    )
    harness.notify(10)
    harness.notify(20, **({"image": "private-unknown.exe"} if case == "unknown" else {}))
    if case == "unknown":
        assert harness.aborted.wait(5)
    else:
        assert entered.wait(5)
        harness.notify(30)
        harness.flush_capture()
        assert len(harness.observer.reservations) == 3
        if case == "mismatch":
            harness.path.write_bytes(b"different raw file bytes")
        release.set()
        assert harness.aborted.wait(5)
    harness.finish(2 if case == "unknown" else 3)
    observer = harness.observer
    assert observer.primary_error == observer.admission_error == error
    assert observer.failure_actor == "admission" and observer.failure_phase == "image_admission"
    assert observer.failure_ordinal == observer.admission_ordinal == 2
    assert list(observer.births.values()) == ["gst_launch"]
    assert len(harness.hashes) == (1 if case == "unknown" else 2)
    assert observer.release_complete


def test_duplicates_pending_validating_admitted_and_pid_reuse_are_bounded(native_facade):
    harness = native_facade()
    entered, release = harness.block_hash()
    harness.notify(10)
    assert entered.wait(5)
    harness.notify(10)  # Original is validating.
    harness.notify(20)
    harness.notify(20)  # Original is pending behind the hash barrier.
    harness.flush_capture()
    assert len(harness.observer.reservations) == 2
    assert harness.observer.duplicate_count == 2
    release.set()
    with harness.observer.condition:
        assert harness.observer.condition.wait_for(lambda: len(harness.observer.births) == 2, 5)
    harness.notify(10)  # Original is now admitted.
    harness.notify(10, birth=101)  # Same PID, distinct creation time.
    for _ in range(witness.MAX_EVENTS + 3):
        harness.notify(10, birth=101)
    harness.flush_capture()
    harness.finish(3)
    observer = harness.observer
    assert observer.error == "none" and len(observer.births) == 3 and len(harness.hashes) == 3
    assert observer.notification_count == observer.duplicate_count == witness.MAX_EVENTS
    assert len([call for call in harness.trace if call[0] == "image"]) == 3
    assert len(harness.closed) == len(harness.handles) + 2


def test_foreign_replacement_of_duplicate_pid_is_never_deduplicated_before_ownership(native_facade):
    harness = native_facade()
    entered, release = harness.block_hash()
    harness.notify(10)
    assert entered.wait(5)
    harness.notify(10, belongs=False)
    assert harness.aborted.wait(5)
    assert harness.observer.duplicate_count == 0
    release.set()
    harness.finish(1)
    assert harness.observer.primary_error == "ownership_unproven"
    assert len([call for call in harness.trace if call[0] == "times"]) == 1
    assert len([call for call in harness.trace if call[0] == "image"]) == 1


def test_32_pending_reservations_allow_only_one_transient_overflow_handle(native_facade):
    harness = native_facade()
    entered, release = harness.block_hash()
    for pid in range(1, witness.MAX_PROCESSES + 1):
        harness.notify(pid)
    assert entered.wait(5)
    harness.flush_capture()
    observer = harness.observer
    assert len(observer.reservations) == len(observer.handles) == witness.MAX_PROCESSES
    assert not observer.births and len(harness.hashes) == 1
    harness.notify(33)
    assert harness.aborted.wait(5)
    assert len(harness.handles) == 33 and len(observer.reservations) == 32
    assert len([call for call in harness.trace if call[0] == "image"]) == 32
    assert observer.primary_error == "limit" and observer.failure_ordinal == 33
    assert observer.failure_phase == "birth_limit"
    assert harness.closed == [1032]  # Validator still owns the other 32 handles.
    release.set()
    harness.finish(33)
    assert observer.release_complete and len(harness.closed) == 35


@pytest.mark.parametrize("block_at", ["membership", "times", "image"])
def test_admission_abort_during_native_capture_never_enqueues_or_closes_under_validator(
    native_facade, block_at
):
    harness = native_facade()
    entered, release = harness.block_hash(failure=PermissionError("private"))
    harness.notify(10)
    assert entered.wait(5)
    capturing, continue_capture = threading.Event(), threading.Event()
    harness.notify(20, block_at=block_at, entered=capturing, release=continue_capture)
    assert capturing.wait(5)
    release.set()
    assert harness.aborted.wait(5)
    observer = harness.observer
    assert observer.primary_error == "access_denied" and observer.failure_actor == "admission"
    assert not harness.closed
    continue_capture.set()
    harness.finish(2)
    assert not observer.births and len(harness.hashes) == 1
    assert observer.release_complete and set(harness.closed) == {456, 789, 1000, 1001}


def test_capture_failure_during_hash_preserves_primary_and_actor_attribution(native_facade):
    harness = native_facade()
    entered, release = harness.block_hash(failure=PermissionError("private"))
    harness.notify(10)
    assert entered.wait(5)
    harness.notify(20, open_error=87)
    assert harness.aborted.wait(5)
    observer = harness.observer
    assert observer.primary_error == "process_unavailable" and not harness.closed
    release.set()
    harness.finish(2)
    assert observer.failure_actor == "capture" and observer.failure_ordinal == 2
    assert observer.failure_phase == "open_process" and observer.admission_error == "access_denied"
    assert observer.cleanup_error == "none" and not observer.births


def test_abort_runs_outside_state_and_resource_locks(native_facade):
    harness = native_facade()
    observer = harness.observer

    def outside_locks():
        acquired = []

        def inspect_locks():
            for lock in (observer.condition, observer.resource_lock):
                success = lock.acquire(blocking=False)
                acquired.append(success)
                if success:
                    lock.release()

        other = threading.Thread(target=inspect_locks)
        other.start()
        other.join(5)
        assert not other.is_alive() and acquired == [True, True]

    harness.abort_check = outside_locks
    harness.notify(10, open_error=87)
    assert harness.aborted.wait(5)
    harness.finish(1)


@pytest.mark.parametrize("failed_handle", [1000, 789, 456])
def test_close_failure_retains_primary_and_attempts_each_handle_exactly_once(
    native_facade, failed_handle
):
    harness = native_facade()
    harness.close_failures.add(failed_handle)
    harness.notify(10, belongs=False)
    assert harness.aborted.wait(5)
    with pytest.raises(boundary.ObservationFailure, match="cleanup_incomplete"):
        harness.finish(1)
    observer = harness.observer
    assert (
        observer.primary_error == "ownership_unproven"
        and observer.failure_phase == "job_membership"
    )
    assert observer.cleanup_error == "cleanup_incomplete" and not observer.release_complete
    assert set(harness.closed) == {456, 789, 1000}
    observer._release()
    assert len(harness.closed) == 3


def test_success_exact_totals_keep_capture_and_admission_counts_equal(native_facade):
    harness = native_facade()
    for pid in range(1, 33):
        harness.notify(pid)
    harness.finish(32)
    observer = harness.observer
    assert observer.error == "none" and observer.release_complete
    assert len(observer.births) == len(observer.reservations) == len(harness.hashes) == 32
    assert list(observer.births) == [(pid, pid * 10) for pid in range(1, 33)]
    assert not observer.pending and observer.validating is None
    assert len(harness.closed) == 34


def test_actor_state_and_both_resources_are_published_before_first_start(monkeypatch, tmp_path):
    harness = NativeFacadeHarness(monkeypatch, tmp_path, start=False)
    real_start = threading.Thread.start
    seen = []

    def start(thread):
        observer = harness.job.observer
        assert observer.capture_phase == observer.admission_phase == "not_started"
        assert observer.capture_ordinal == observer.admission_ordinal == 0
        assert observer.primary_error == observer.cleanup_error == "none"
        assert observer.thread is not observer.admission_thread
        assert {resource.thread for resource in harness.resources} == {
            observer.thread,
            observer.admission_thread,
        }
        seen.append(thread)
        return real_start(thread)

    monkeypatch.setattr(threading.Thread, "start", start)
    try:
        harness.start()
        assert len(seen) == 2
        harness.notify(10)
        harness.finish(1)
    finally:
        harness.cleanup()


@pytest.mark.parametrize(
    "stage", ["duplicate", "port", "associate", "capture-start", "admission-start"]
)
def test_startup_failures_account_for_all_allocated_handles(monkeypatch, tmp_path, stage):
    harness = NativeFacadeHarness(monkeypatch, tmp_path, start=False)
    harness.startup_failure = stage
    real_start = threading.Thread.start

    def start(thread):
        observer = harness.job.observer
        if thread is getattr(
            observer, "thread" if stage == "capture-start" else "admission_thread"
        ):
            raise RuntimeError("private-startup-error")
        return real_start(thread)

    if stage.endswith("-start"):
        monkeypatch.setattr(threading.Thread, "start", start)
    try:
        with pytest.raises((boundary.ObservationFailure, RuntimeError)):
            harness.start()
        for resource in harness.resources:
            assert not resource.thread.is_alive()
        assert set(harness.closed) == (
            set() if stage == "duplicate" else {456} if stage == "port" else {456, 789}
        )
    finally:
        harness.cleanup()


def test_timeout_is_sticky_preserves_live_resources_and_blocks_root_cleanup(
    native_facade, monkeypatch, tmp_path
):
    harness = native_facade()
    entered, release = harness.block_hash()
    harness.notify(10)
    assert entered.wait(5)
    harness.flush_capture()
    observer = harness.observer
    actual = witness.FacadeObservation(temp_root=tmp_path, admitted_images={}, mode="test_valid")
    actual.resources = harness.resources
    assert not actual.quiescent()
    # Advance only the facade controller's budget clock. Real threads and Events
    # remain live; no arbitrary delay or five-second test timeout is needed.
    ticks = iter(range(0, 1000, 10))
    with monkeypatch.context() as patch:
        patch.setattr(witness, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
        with pytest.raises(boundary.ObservationFailure, match="cleanup_incomplete"):
            observer.finish(1)
    assert observer.error == observer.cleanup_error == "cleanup_incomplete"
    assert observer.admission_thread.is_alive() and not observer.release_complete
    assert not actual.quiescent() and not harness.closed
    assert tmp_path.exists()
    release.set()
    for thread in (observer.thread, observer.admission_thread):
        thread.join(5)
        assert not thread.is_alive()
    observer._release()
    assert observer.error == observer.cleanup_error == "cleanup_incomplete"
    assert not observer.births and observer.release_complete
    with pytest.raises(boundary.ObservationFailure, match="cleanup_incomplete"):
        observer.finish(1)


@pytest.mark.parametrize("expected_total", [0, 2, 33], ids=["zero", "missing-birth", "over-limit"])
def test_stopped_actors_with_unreconciled_job_total_never_become_complete(
    native_facade, expected_total
):
    harness = native_facade()
    harness.notify(10)
    harness.flush_capture()
    # End capture after the sole event. Admission drains its FIFO before exiting.
    harness.post(789, 0, 2, None)
    for thread in (harness.observer.thread, harness.observer.admission_thread):
        thread.join(5)
        assert not thread.is_alive()
    harness.finish(expected_total)
    observer = harness.observer
    assert observer.error == observer.primary_error == "incomplete"
    assert observer.failure_actor == "lifecycle" and observer.failure_phase == "finish"
    assert len(observer.births) == 1 and not observer.reconciled(expected_total)
    assert observer.release_complete


@pytest.mark.parametrize("slot", ["handles", "reservations", "pending"])
def test_capture_publication_failure_keeps_exact_handle_ownership(native_facade, slot):
    harness = native_facade()

    class RefuseAppend(list):
        def append(self, record):
            raise RuntimeError("private-publication-error")

    class RefuseInsert(dict):
        def __setitem__(self, key, value):
            raise RuntimeError("private-publication-error")

    observer = harness.observer
    with observer.condition:
        setattr(observer, slot, RefuseInsert() if slot == "reservations" else RefuseAppend())
    harness.notify(10)
    assert harness.aborted.wait(5)
    harness.finish(1)
    assert observer.primary_error == "native_error" and observer.failure_actor == "capture"
    assert observer.failure_phase == "capture_publication" and observer.failure_ordinal == 1
    assert not observer.births and not harness.hashes
    assert len(observer.reservations) == (1 if slot == "pending" else 0)
    assert observer.release_complete and set(harness.closed) == {456, 789, 1000}


def test_wake_failure_is_cleanup_failure_even_after_all_births_admitted(native_facade):
    harness = native_facade()
    harness.notify(10)
    with harness.observer.condition:
        assert harness.observer.condition.wait_for(lambda: len(harness.observer.births) == 1, 5)
    harness.api.PostQueuedCompletionStatus = lambda *args: False
    with pytest.raises(boundary.ObservationFailure, match="cleanup_incomplete"):
        harness.finish(1)
    harness.assert_stopped()
    assert harness.observer.cleanup_error == harness.observer.error == "cleanup_incomplete"
    assert harness.observer.release_complete and set(harness.closed) == {456, 789, 1000}


@pytest.mark.parametrize(
    "packet",
    [{"key": 3}, {"pid": None}, {"pid": 0}, {"pid": 2**32}],
    ids=["foreign-key", "null-pid", "zero-pid", "large-pid"],
)
def test_completion_port_rejects_unowned_or_invalid_birth_before_open(native_facade, packet):
    harness = native_facade()
    harness.notify(**packet)
    assert harness.aborted.wait(5)
    harness.finish(1)
    assert harness.observer.primary_error == "native_error"
    assert harness.observer.failure_actor == "capture"
    assert harness.observer.failure_phase == "notification"
    assert not harness.handles and not harness.hashes


def test_live_admission_lifetime_alone_blocks_execute_work_root_removal(
    native_facade, monkeypatch, tmp_path
):
    harness = native_facade()
    entered, release = harness.block_hash()
    harness.notify(10)
    assert entered.wait(5)
    harness.flush_capture()
    harness.post(789, 0, 2, None)
    harness.observer.thread.join(5)
    assert not harness.observer.thread.is_alive() and harness.observer.admission_thread.is_alive()
    work = tmp_path / "retained-work"
    work.mkdir()
    marker = work / "owned-input"
    marker.write_bytes(b"must remain while hashing is live")
    actual = witness.FacadeObservation(temp_root=work, admitted_images={}, mode="test_valid")
    actual.resources = harness.resources
    inputs = tmp_path / "input-expectations.json"
    inputs.write_bytes(common.canonical(expected(False)))
    args = SimpleNamespace(
        output=tmp_path / witness.RECEIPT_NAME,
        admitted_expectations=tmp_path / witness.EXPECTATIONS_NAME,
        expectations=inputs,
        repo=tmp_path,
        work_root=work,
        temp_root=tmp_path,
    )
    monkeypatch.setattr(witness.alpha, "admit_platform", lambda: None)
    monkeypatch.setattr(common, "adopt_work_root", lambda *args: work)
    monkeypatch.setattr(witness, "bind_controller", lambda *args: None)
    monkeypatch.setattr(
        witness.alpha,
        "prepare",
        lambda *args: (work, {"LOCALAPPDATA": str(work), "SYSTEMROOT": str(work)}, []),
    )
    monkeypatch.setattr(witness, "verify_inputs", lambda *args: None)
    monkeypatch.setattr(witness, "generated_authorities", lambda *args: ({}, []))
    monkeypatch.setattr(witness, "verify_generated", lambda *args: None)
    monkeypatch.setattr(witness.alpha, "probe", lambda *args: {"runtime_identity_sha256": "d" * 64})
    monkeypatch.setattr(witness.alpha, "verify_native", lambda *args: None)
    monkeypatch.setattr(common, "local_path", lambda path, **kwargs: path)

    def sequence(**kwargs):
        kwargs["observations"].append(actual)
        raise common.WitnessError("child_failed")

    monkeypatch.setattr(witness, "facade_sequence", sequence)
    assert witness.execute(args) == 1
    assert marker.read_bytes() == b"must remain while hashing is live"
    assert not args.output.exists()
    assert not actual.quiescent() and not harness.closed
    release.set()
    harness.finish(1)
    assert actual.quiescent()


@pytest.mark.parametrize("stage", ["accounting", "terminate", "drain"])
def test_job_close_native_failure_still_stops_both_observer_actors(monkeypatch, tmp_path, stage):
    harness = NativeFacadeHarness(monkeypatch, tmp_path, start=False)
    job_closed = []

    class Job:
        def __init__(self):
            self.handle, self.api, self.accounting_calls = 42, harness.api, 0

        def accounting(self):
            self.accounting_calls += 1
            if stage == "accounting" or stage == "drain" and self.accounting_calls > 1:
                raise boundary.ObservationFailure("native_error")
            return SimpleNamespace(total_processes=1, active_processes=0)

        def assign(self, process):
            pytest.fail("no process assignment in a native cleanup fixture")

        def close(self):
            job_closed.append(self.handle)

    harness.api.TerminateJobObject = lambda *args: stage != "terminate"
    monkeypatch.setattr(common, "WindowsJob", Job)
    actual = witness.FacadeObservation(
        temp_root=tmp_path, admitted_images=harness.admitted, mode="test_valid"
    )
    # Isolate the Job lifecycle while retaining its real constructor/observer.
    # TEMP/product invocation is covered separately and is not synthetic proof.
    actual.started = True
    actual.watcher = SimpleNamespace(error="none")
    try:
        job = actual.job_factory()
        harness.observer, harness.job, harness.resources = job.observer, job, actual.resources
        harness.notify(10)
        with job.observer.condition:
            assert job.observer.condition.wait_for(lambda: len(job.observer.births) == 1, 5)
        with pytest.raises(boundary.ObservationFailure, match="cleanup_incomplete"):
            job.close()
        harness.assert_stopped()
        assert not job.observation_open and not job.resources_closed
        assert job.observer.release_complete and actual.quiescent()
        assert (
            job.observer.primary_error == "incomplete" and job.observer.failure_actor == "lifecycle"
        )
        assert set(harness.closed) == {456, 789, 1000} and job_closed == [42]
        with pytest.raises(boundary.ObservationFailure, match="cleanup_incomplete"):
            job.close()
        assert job_closed == [42]
    finally:
        harness.cleanup()


def test_failure_diagnostics_never_include_raw_exceptions(capsys):
    witness.emit_failure("test_valid", RuntimeError("rtsp://private:secret@host/private"))
    witness.emit_failure("test_valid", boundary.ObservationFailure("process_unavailable"))
    lines = capsys.readouterr().out.splitlines()
    assert "secret" not in " ".join(lines)
    values = [json.loads(line.split("=", 1)[1]) for line in lines]
    assert values[1]["observation_error"] == "process_unavailable"
    assert all(
        value.keys() == {"schema_version", "stage", "failure_code", "observation_error"}
        for value in values
    )


def test_off_windows_execute_removes_stale_success_without_starting_children(tmp_path, monkeypatch):
    output = tmp_path / witness.RECEIPT_NAME
    output.write_text("stale")
    inputs = tmp_path / "inputs.json"
    inputs.write_bytes(common.canonical(expected(False)))
    args = SimpleNamespace(
        output=output,
        admitted_expectations=tmp_path / witness.EXPECTATIONS_NAME,
        expectations=inputs,
    )
    monkeypatch.setattr(
        witness.alpha,
        "admit_platform",
        lambda: (_ for _ in ()).throw(common.WitnessError("admission_failed")),
    )
    monkeypatch.setattr(
        witness, "bind_controller", lambda *args: pytest.fail("no native admission")
    )
    assert witness.execute(args) == 1
    assert not output.exists()


def test_cli_validation_is_independent_and_read_only(tmp_path):
    output, inputs = tmp_path / witness.RECEIPT_NAME, tmp_path / witness.EXPECTATIONS_NAME
    output.write_bytes(common.canonical(receipt()))
    inputs.write_bytes(common.canonical(expected()))
    command = [
        sys.executable,
        str(ROOT / "scripts/installed_alpha_facade_witness.py"),
        "--validate-receipt",
        "--expectations",
        str(inputs),
        "--output",
        str(output),
    ]
    assert subprocess.run(command, capture_output=True, timeout=10).returncode == 0
    output.write_bytes(common.canonical({**receipt(), "person_box_acceptance": True}))
    before = output.read_bytes()
    result = subprocess.run(command, capture_output=True, timeout=10)
    assert result.returncode == 1 and output.read_bytes() == before
    assert b"person_box" not in result.stdout


def fake_observation(monkeypatch, tmp_path, *, active=0, mode="test_valid"):
    value = observation(mode)
    calls, state = [], {"active": active}

    class Job:
        def __init__(self):
            self.handle = 1

            def terminate(*args):
                state["active"] = 0
                calls.append("terminate")
                return True

            self.api = SimpleNamespace(TerminateJobObject=terminate)

        def accounting(self):
            return SimpleNamespace(
                total_processes=value["job_total"], active_processes=state["active"]
            )

        def assign(self, process):
            calls.append("assign")

        def close(self):
            calls.append("job_closed")

    class Processes:
        def __init__(self, job, admitted, resources):
            self.error = "none"
            self.births = {(index, index + 1): kind for index, kind in enumerate(sorted(admitted))}
            self.thread = SimpleNamespace(is_alive=lambda: False)
            self.admission_thread = SimpleNamespace(is_alive=lambda: False)
            self.reservations = dict(self.births)
            self.pending, self.validating = [], None
            self.release_complete = True
            self.capture_done, self.admission_done = threading.Event(), threading.Event()
            self.capture_done.set()
            self.admission_done.set()
            resources.extend([self, SimpleNamespace(thread=self.admission_thread, error="none")])

        def reconciled(self, total):
            return self.error == "none" and len(self.births) == len(self.reservations) == total

        def finish(self, total):
            calls.append(("process_finished", total))

    class Temp:
        def __init__(self, root, abort, resources, mode):
            self.error, self.closed, self.drain_complete = "none", False, True
            self.counts = {key: value[key] for key in witness.TEMP_COUNTS}
            self.total = value["temp_events"]
            self.lifecycle = witness.FacadeTempLifecycle(mode)
            policy_trace(self.lifecycle)
            self.thread = SimpleNamespace(is_alive=lambda: False)
            resources.append(self)

        def finish(self):
            self.closed = True
            calls.append("temp_finished")

    monkeypatch.setattr(common, "WindowsJob", Job)
    monkeypatch.setattr(witness, "FacadeProcessObserver", Processes)
    monkeypatch.setattr(witness, "FacadeTempObserver", Temp)
    admitted = {}
    for kind in witness.allowed_images(mode):
        path = tmp_path / (kind + ".exe")
        path.write_bytes(kind.encode())
        admitted[kind] = (path, common.file_hash(path))
    temp = tmp_path / "temp"
    temp.mkdir()
    actual = witness.FacadeObservation(temp_root=temp, admitted_images=admitted, mode=mode)
    return actual, calls


def test_single_job_observation_closes_before_final_temp_drain(monkeypatch, tmp_path):
    actual, calls = fake_observation(monkeypatch, tmp_path)
    actual.start()
    job = actual.job_factory()
    job.assign(object())
    with pytest.raises(boundary.ObservationFailure):
        actual.job_factory()
    job.close()
    witness.validate_observation(actual.finish(), "test_valid")
    actual.close()
    assert calls == ["assign", "terminate", ("process_finished", 5), "job_closed", "temp_finished"]


def test_final_observation_refuses_live_admission_before_reading_maps(monkeypatch, tmp_path):
    actual, _ = fake_observation(monkeypatch, tmp_path)
    actual.start()
    job = actual.job_factory()
    job.close()
    live = SimpleNamespace(is_alive=lambda: True)
    job.observer.admission_thread = live
    actual.resources[2].thread = live

    class MutableBirths:
        def values(self):
            pytest.fail("final observation read births while admission was live")

    job.observer.births = MutableBirths()
    with pytest.raises(boundary.ObservationFailure, match="cleanup_incomplete"):
        actual.finish()


def test_forced_cleanup_cannot_erase_active_survivor_evidence(monkeypatch, tmp_path):
    actual, _ = fake_observation(monkeypatch, tmp_path, active=1)
    actual.start()
    job = actual.job_factory()
    job.close()
    assert job.final_active == 1
    with pytest.raises(boundary.ObservationFailure):
        actual.finish()
    actual.close()


def test_observation_rechecks_each_admitted_executable_after_execution(monkeypatch, tmp_path):
    actual, _ = fake_observation(monkeypatch, tmp_path)
    actual.start()
    job = actual.job_factory()
    job.close()
    actual.admitted["gst_launch"][0].write_bytes(b"tampered")
    with pytest.raises(boundary.ObservationFailure):
        actual.finish()
    actual.close()


@pytest.mark.parametrize("source", ["temp", "capture", "admission"])
def test_aborted_observer_never_assigns_job_or_opens_gate(monkeypatch, tmp_path, source):
    actual, calls = fake_observation(monkeypatch, tmp_path)
    actual.start()
    job = actual.job_factory()
    if source == "temp":
        actual.watcher.error = "unexpected_temp"
    else:
        job.observer.error = "process_unavailable" if source == "capture" else "access_denied"
    with pytest.raises(boundary.ObservationFailure):
        job.assign(object())
    assert "assign" not in calls
    job.close()
    actual.close()


def test_sequence_independently_revalidates_every_boundary_and_uses_fresh_invocations(
    monkeypatch, tmp_path
):
    events = []
    env = {"K5_ANALYTICS_CONFIG": str(tmp_path / "analytics.json"), "LOCALAPPDATA": "owned"}
    monkeypatch.setattr(witness, "verify_inputs", lambda *args: events.append("identities"))
    monkeypatch.setattr(
        witness.alpha, "probe", lambda *args, **kwargs: events.append(("probe", kwargs))
    )
    monkeypatch.setattr(witness.alpha, "require_ports_free", lambda port: events.append("ports"))
    monkeypatch.setattr(common, "free_port", lambda: 8123)
    monkeypatch.setattr(witness, "admitted_images", lambda *args: {})

    def invoke(command, **kwargs):
        events.append(("invoke", kwargs["mode"], kwargs["env"]["K5_ANALYTICS_CONFIG"]))
        assert (
            "Test-K5VisionAlpha.ps1" in command[command.index("-Facade") + 1]
            if kwargs["mode"].startswith("test_")
            else "Run-K5VisionAlpha.ps1" in command[command.index("-Facade") + 1]
        )
        return (run_result() if kwargs["mode"].startswith("run_") else {}), observation(
            kwargs["mode"]
        )

    monkeypatch.setattr(witness, "invoke_facade", invoke)
    document, state = witness.new_receipt(expected()), {}
    witness.facade_sequence(
        args=None,
        work=tmp_path,
        installed=tmp_path / "installed",
        env=env,
        powershell=Path("powershell.exe"),
        expected=expected(),
        document=document,
        observations=[],
        probe_command=["installed-probe"],
        state=state,
        authorities={},
        expected_probe_command=["installed-probe"],
    )
    assert len(events) == 28
    for index, mode in enumerate(("test_invalid", "test_valid", "run_1", "run_2")):
        before, probe1, ports1, invoked, after, probe2, ports2 = events[index * 7 : index * 7 + 7]
        assert before == after == "identities" and ports1 == ports2 == "ports"
        assert probe1 == probe2 == ("probe", {"after": True})
        assert invoked[1] == mode
        assert invoked[2] == (
            str(tmp_path / "invalid-facade-analytics.json")
            if mode == "test_invalid"
            else str(tmp_path / "analytics.json")
        )
    assert env["K5_ANALYTICS_CONFIG"] == str(tmp_path / "analytics.json")
    document.update(completed=True, cleanup_complete=True)
    witness.validate_receipt(document, expected())


def test_binding_export_checks_exact_controller_helper_and_raw_tree_before_prepare(
    monkeypatch, tmp_path
):
    import shutil

    work = tmp_path / "work"
    work.mkdir()
    source = tmp_path / "source"
    (source / "scripts").mkdir(parents=True)
    names = (
        "installed_alpha_facade_witness.py",
        "installed_alpha_launcher_witness.py",
        "installed_analytics_witness.py",
        "windows_owned_preflight.py",
    )
    for name in names:
        shutil.copyfile(ROOT / "scripts" / name, source / "scripts" / name)
    inputs = expected(False)
    inputs["source_tree_sha256"] = common.digest(witness.alpha.tree_manifest(source))
    inputs["facade_controller_sha256"] = common.file_hash(ROOT / "scripts" / names[0])
    git = tmp_path / "git.exe"
    git.write_bytes(b"never executed")
    args = SimpleNamespace(repo=tmp_path, git=git)

    def run(command, **kwargs):
        assert kwargs["operation"] == "archive_candidate"
        (work / "facade-binding.zip").write_bytes(b"test-only archive")

    monkeypatch.setattr(common, "run", run)
    monkeypatch.setattr(
        common, "extract_archive", lambda archive, target: shutil.copytree(source, target)
    )
    witness.bind_controller(args, work, inputs)
    assert not (work / "facade-binding").exists()
    for name in names:
        original = (source / "scripts" / name).read_bytes()
        (source / "scripts" / name).write_bytes(original + b"\n# changed\n")
        inputs["source_tree_sha256"] = common.digest(witness.alpha.tree_manifest(source))
        with pytest.raises(common.WitnessError):
            witness.bind_controller(args, work, inputs)
        shutil.rmtree(work / "facade-binding")
        (work / "facade-binding.zip").unlink()
        (source / "scripts" / name).write_bytes(original)


def test_primary_and_cleanup_failures_remain_distinct_source_free_diagnostics(
    monkeypatch, tmp_path, capsys
):
    closed, registered = [], []

    class Observation:
        def __init__(self, **kwargs):
            self.started, self.jobs, self.resources = False, [], []

        def start(self):
            self.started = True

        def job_factory(self):
            return None

        def close(self):
            closed.append(True)
            raise boundary.ObservationFailure("cleanup_incomplete")

    def launch(*args, **kwargs):
        raise common.WitnessError("child_timeout")

    monkeypatch.setattr(witness, "FacadeObservation", Observation)
    monkeypatch.setattr(witness, "FacadeOwnedProcess", launch)
    with pytest.raises(common.WitnessError, match="cleanup_incomplete"):
        witness.invoke_facade(
            [],
            work=tmp_path,
            env={"TEMP": str(tmp_path)},
            mode="test_valid",
            expected=expected(),
            images={},
            observations=registered,
        )
    values = [
        json.loads(line.split("=", 1)[1])
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("K5_FACADE_FAILURE=")
    ]
    assert [
        (value["stage"], value["failure_code"], value["observation_error"]) for value in values
    ] == [
        ("test_valid", "child_timeout", "none"),
        ("cleanup", "receipt_invalid", "cleanup_incomplete"),
    ]
    assert len(registered) == 1 and closed == [True]


def test_changed_generated_envelope_refuses_before_next_facade(monkeypatch, tmp_path):
    env, calls = {"K5_ANALYTICS_CONFIG": str(tmp_path / "analytics.json")}, []
    monkeypatch.setattr(witness, "verify_inputs", lambda *args: None)
    monkeypatch.setattr(witness.alpha, "probe", lambda *args, **kwargs: None)
    monkeypatch.setattr(witness.alpha, "require_ports_free", lambda *args: None)
    monkeypatch.setattr(common, "free_port", lambda: 8123)
    monkeypatch.setattr(witness, "admitted_images", lambda *args: {})

    def invoke(command, **kwargs):
        calls.append(kwargs["mode"])
        (tmp_path / "invoke-facade.ps1").write_text("untrusted replacement")
        return {}, observation(kwargs["mode"])

    monkeypatch.setattr(witness, "invoke_facade", invoke)
    with pytest.raises(common.WitnessError):
        witness.facade_sequence(
            args=None,
            work=tmp_path,
            installed=tmp_path,
            env=env,
            powershell=Path("powershell.exe"),
            expected=expected(),
            document={},
            observations=[],
            probe_command=[],
            state={},
            authorities={},
            expected_probe_command=[],
        )
    assert calls == ["test_invalid"]


def test_generated_probe_and_configs_are_bound_before_they_are_trusted(tmp_path):
    source = tmp_path / "source"
    (source / "scripts/windows-alpha").mkdir(parents=True)
    (source / "scripts/windows-alpha/runtime-requirements.txt").write_text("fastapi==0.1.0\n")
    (source / "src/k5vision").mkdir(parents=True)
    (source / "src/k5vision/__init__.py").write_bytes(b"# pinned package\n")
    evidence, installed = tmp_path / "evidence", tmp_path / "installed"
    evidence.mkdir()
    args = SimpleNamespace(evidence_root=evidence)
    files, command = witness.generated_authorities(args, tmp_path, installed)
    env = {"K5_ANALYTICS_CONFIG": str(tmp_path / "analytics.json")}
    for path, raw in files.items():
        path.write_bytes(raw)
    witness.verify_generated(files, command, list(command), env, tmp_path)
    for path, raw in files.items():
        path.write_bytes(raw + b"changed")
        with pytest.raises(common.WitnessError):
            witness.verify_generated(files, command, list(command), env, tmp_path)
        path.write_bytes(raw)
    with pytest.raises(common.WitnessError):
        witness.verify_generated(files, command + ["--untrusted"], command, env, tmp_path)
    with pytest.raises(common.WitnessError):
        witness.verify_generated(
            files, command, command, {"K5_ANALYTICS_CONFIG": "elsewhere"}, tmp_path
        )
    assert common.parse_json(files[tmp_path / "probe-inputs.json"])[
        "k5_payload"
    ] == witness.alpha.source_payload(source)


def test_live_stdout_or_stderr_collector_blocks_work_cleanup(tmp_path):
    observed = witness.FacadeObservation(temp_root=tmp_path, admitted_images={}, mode="test_valid")
    live = [True]
    observed.collectors.append(SimpleNamespace(thread=SimpleNamespace(is_alive=lambda: live[0])))
    assert not observed.quiescent()
    live[0] = False
    assert observed.quiescent()


def test_failed_process_construction_still_retains_stderr_collector(monkeypatch):
    collector = SimpleNamespace(thread=SimpleNamespace(is_alive=lambda: True))
    observed = SimpleNamespace(collectors=[])

    def construct(self, *args, **kwargs):
        self.stderr_summary = collector
        raise common.WitnessError("cleanup_incomplete")

    monkeypatch.setattr(common.OwnedProcess, "__init__", construct)
    with pytest.raises(common.WitnessError, match="cleanup_incomplete"):
        witness.FacadeOwnedProcess([], observation=observed)
    assert observed.collectors == [collector]


def diagnostic_observation(tmp_path):
    """Synthetic stopped collectors, never a native inventory qualification."""
    actual = witness.FacadeObservation(temp_root=tmp_path, admitted_images={}, mode="run_1")
    quiet = SimpleNamespace(is_alive=lambda: False)
    observer = object.__new__(witness.FacadeProcessObserver)
    observer._initialize_evidence()
    observer.thread, observer.error, observer.release_complete = quiet, "process_unavailable", True
    observer.admission_thread = quiet
    observer.capture_done.set()
    observer.admission_done.set()
    observer.capture_ordinal = observer.failure_ordinal = 3
    observer.admission_ordinal = 2
    observer.notification_count = 3
    observer.capture_phase = observer.failure_phase = "open_process"
    observer.admission_phase = "admitted_birth"
    observer.failure_actor = "capture"
    observer.primary_error = observer.capture_error = "process_unavailable"
    observer.births = {(123456, 999999): "base_python", (234567, 888888): "powershell"}
    observer.reservations = {
        key: ("private-source-image.exe", index + 1) for index, key in enumerate(observer.births)
    }
    actual.jobs = [
        SimpleNamespace(
            observer=observer,
            final_total=3,
            final_active=0,
            accounting_observed=True,
            aborted=True,
            observer_abort_requested=True,
            observation_open=False,
            resources_closed=True,
        )
    ]
    actual.watcher = SimpleNamespace(thread=quiet, closed=True, drain_complete=True)
    actual.owned = SimpleNamespace(
        stderr_summary=SimpleNamespace(
            thread=quiet,
            gate_state="started",
            child_exit_code=None,
            read_failed=False,
        )
    )
    actual.relay_exit_code = 1
    actual.stdout_summary = summary(b"K5_FACADE_FAILED\n", "run_1")
    actual.resources = [
        observer,
        SimpleNamespace(thread=observer.admission_thread, error="none"),
        actual.watcher,
    ]
    actual.collectors = [actual.stdout_summary, actual.owned.stderr_summary]
    return actual


def test_failure_snapshot_is_distinct_source_free_prefix_and_cached_exits(tmp_path):
    actual = diagnostic_observation(tmp_path)
    value = witness.failure_evidence(actual, "run_1")
    witness.validate_failure_evidence(value)
    assert value["snapshot_state"] == "quiescent"
    assert value["observer"]["admitted_classes"] == ["base_python", "powershell"]
    assert value["observer"]["admitted_counts"]["unknown"] == 0
    assert value["schema_version"] == "installed-alpha-facade-evidence-v2"
    assert value["observer"]["capture_ordinal"] == value["observer"]["failure_ordinal"] == 3
    assert value["observer"]["admission_ordinal"] == 2
    assert value["observer"]["captured_count"] == value["observer"]["admitted_count"] == 2
    assert value["observer"]["pending_count"] == value["observer"]["validating_count"] == 0
    assert value["observer"]["failure_actor"] == "capture"
    assert value["job"]["observer_abort_requested"] is True
    assert value["exit"]["relay_exit_code"] == 1
    assert value["exit"]["child_exit_code"] is None
    assert value["exit_before_observer_abort"] is None
    assert value["stdout"]["failed"] == 1
    assert all(item is True for item in value["collectors"].values())
    raw = common.canonical(value)
    assert all(secret not in raw for secret in (b"123456", b"234567", b"999999", b"888888"))
    assert len(raw) < 8192
    with pytest.raises((common.WitnessError, boundary.ObservationFailure)):
        witness.validate_receipt(value, expected())


@pytest.mark.parametrize("slot", ["process", "admission", "temp", "stdout", "stderr"])
def test_live_reader_makes_entire_snapshot_unavailable_without_accessing_maps(tmp_path, slot):
    actual = diagnostic_observation(tmp_path)
    target = {
        "process": actual.jobs[0].observer,
        "admission": actual.resources[1],
        "temp": actual.watcher,
        "stdout": actual.stdout_summary,
        "stderr": actual.owned.stderr_summary,
    }[slot]
    target.thread = SimpleNamespace(is_alive=lambda: True)
    if slot == "admission":
        actual.jobs[0].observer.admission_thread = target.thread

    # A racing map must not be touched even if other readers are already stopped.
    class MutableBirths:
        def values(self):
            pytest.fail("live snapshot accessed mutable birth data")

    actual.jobs[0].observer.births = MutableBirths()
    value = witness.failure_evidence(actual, "run_1")
    witness.validate_failure_evidence(value)
    assert value["snapshot_state"] == "not_quiescent"
    assert all(value[key] is None for key in witness.EVIDENCE_PARTS)


def test_stopped_failure_snapshot_counts_all_unadmitted_reservations(tmp_path):
    actual = diagnostic_observation(tmp_path)
    observer = actual.jobs[0].observer
    observer.births.pop((234567, 888888))
    third = (345678, 777777)
    observer.reservations[third] = (third, "private-image-path.exe", 3)
    observer.pending.append(observer.reservations[third])
    observer.capture_ordinal, observer.capture_phase = 0, "notification"
    observer.admission_ordinal = observer.failure_ordinal = 2
    observer.failure_actor = "admission"
    observer.admission_phase = observer.failure_phase = "image_admission"
    observer.capture_error = "none"
    observer.error = observer.primary_error = observer.admission_error = "access_denied"
    value = witness.failure_evidence(actual, "run_1")
    witness.validate_failure_evidence(value)
    evidence = value["observer"]
    assert evidence["captured_count"] == 3 and evidence["admitted_count"] == 1
    assert evidence["pending_count"] == 2 and evidence["validating_count"] == 0
    assert evidence["failure_actor"] == "admission" and evidence["failure_ordinal"] == 2
    assert evidence["capture_ordinal"] == 0 and evidence["admission_ordinal"] == 2
    encoded = common.canonical(value)
    assert b"private" not in encoded and b"345678" not in encoded and b"777777" not in encoded


def test_snapshot_does_not_invent_accounting_or_successful_release(tmp_path):
    actual = diagnostic_observation(tmp_path)
    job = actual.jobs[0]
    job.accounting_observed = job.resources_closed = job.observer.release_complete = False
    actual.watcher.closed = actual.watcher.drain_complete = False
    value = witness.failure_evidence(actual, "run_1")
    assert value["job"]["total"] is value["job"]["active_before_close"] is None
    assert value["job"]["resources_closed"] is False
    assert value["collectors"]["process_handles_released"] is False
    assert value["collectors"]["temp_handles_released"] is False
    assert value["collectors"]["temp_drain_complete"] is False


@pytest.mark.parametrize("prior", ["none", "live", "exited", "read_failed"])
@pytest.mark.parametrize("actor", ["thread", "admission_thread"])
def test_owned_abort_records_only_stopped_prior_exit_without_polling(
    monkeypatch, tmp_path, prior, actor
):
    actual, calls = fake_observation(monkeypatch, tmp_path)
    actual.start()
    job = actual.job_factory()

    class Relay:
        returncode = 24

        def poll(self):
            pytest.fail("diagnostics must not query the process")

    if prior != "none":
        actual.owned = SimpleNamespace(
            process=Relay(),
            stderr_summary=SimpleNamespace(
                thread=SimpleNamespace(is_alive=lambda: prior == "live"),
                gate_state="exited",
                child_exit_code=24,
                read_failed=prior == "read_failed",
            ),
        )
    setattr(job.observer, actor, threading.current_thread())
    job.abort()
    assert job.observer_abort_requested and job.aborted
    assert calls == ["terminate"]
    value = actual.exit_before_observer_abort
    witness.validate_exit_evidence(value)
    assert value["child_exit_code"] == (24 if prior in {"exited", "read_failed"} else None)
    assert value["stderr_read_failed"] == (
        prior == "read_failed" if prior in {"exited", "read_failed"} else None
    )
    assert value["relay_exit_code"] == (None if prior == "none" else 24)
    # A stopped, read-failed collector is explicitly marked, never clean exit proof.
    setattr(job.observer, actor, SimpleNamespace(is_alive=lambda: False))
    job.close()
    actual.close()


def test_other_abort_source_is_not_mislabeled_as_process_observer(monkeypatch, tmp_path):
    actual, _ = fake_observation(monkeypatch, tmp_path)
    actual.start()
    job = actual.job_factory()
    job.abort()
    assert job.aborted and not job.observer_abort_requested
    assert actual.exit_before_observer_abort is None
    job.close()
    actual.close()


@pytest.mark.parametrize(
    "raw,mode",
    [
        (b"private-token-user-name-path\n", "test_valid"),
        (b"K5_FACADE_FAILED\n" * 4000 + b"private-secret", "run_1"),
        (b"publisher: rtsp://private-secret@host/path\n", "run_1"),
        (b"K5 analytics PASS: submissions=9999999, completions=1, failures=0\n", "run_1"),
    ],
    ids=["private-test-output", "output-flood", "private-prefix-milestone", "large-run-counter"],
)
def test_bounded_stdout_evidence_never_contains_raw_output(tmp_path, raw, mode):
    actual = diagnostic_observation(tmp_path)
    actual.stdout_summary = summary(raw, mode)
    actual.collectors[0] = actual.stdout_summary
    # Classes must match this mode; private output never supplies process authority.
    value = witness.failure_evidence(actual, mode)
    witness.validate_failure_evidence(value)
    encoded = common.canonical(value)
    assert b"private" not in encoded and b"secret" not in encoded and b"rtsp" not in encoded
    assert len(encoded) < 8192
    if mode == "test_valid" or len(raw) > 65536:
        assert value["stdout"]["invalid"]
    with pytest.raises(common.WitnessError):
        actual.stdout_summary.result()


@pytest.mark.parametrize(
    "section,key,bad",
    [
        (None, "schema_version", "installed-alpha-facades-v1"),
        (None, "mode", True),
        (None, "snapshot_state", "complete"),
        ("observer", "capture_ordinal", True),
        ("observer", "capture_ordinal", 34),
        ("observer", "admission_ordinal", True),
        ("observer", "failure_ordinal", 34),
        ("observer", "captured_count", 33),
        ("observer", "pending_count", True),
        ("observer", "pending_count", 1),
        ("observer", "admitted_count", 1),
        ("observer", "validating_count", 1),
        ("observer", "failure_actor", "private-actor"),
        ("observer", "admission_phase", "private-phase"),
        ("observer", "capture_error", "private-error"),
        ("observer", "admission_error", "private-error"),
        ("observer", "notifications_capped", 257),
        ("observer", "duplicates_capped", -1),
        ("observer", "capture_phase", "private-path"),
        ("observer", "primary_error", "raw-native-error"),
        ("observer", "admitted_classes", ["unknown"]),
        ("job", "total", True),
        ("job", "total", 2**32),
        ("job", "observer_abort_requested", 1),
        ("collectors", "stdout_stopped", 1),
        ("stdout", "invalid", 0),
        ("stdout", "returned", True),
        ("stdout", "failed", 65537),
        ("exit", "gate_state", "raw-child-error"),
        ("exit", "child_exit_code", True),
        ("exit", "relay_exit_code", 2**32),
        ("exit", "stderr_read_failed", 0),
    ],
    ids=[
        "schema",
        "mode-bool",
        "state",
        "ordinal-bool",
        "ordinal-overflow",
        "admission-ordinal-bool",
        "failure-ordinal-overflow",
        "captured-overflow",
        "pending-bool",
        "pending-contradiction",
        "admitted-contradiction",
        "live-validation",
        "raw-actor",
        "raw-admission-phase",
        "raw-capture-error",
        "raw-admission-error",
        "count-overflow",
        "negative-duplicate",
        "raw-phase",
        "raw-error",
        "unknown-image",
        "total-bool",
        "total-overflow",
        "abort-int",
        "stopped-int",
        "invalid-int",
        "marker-bool",
        "marker-overflow",
        "raw-gate",
        "exit-bool",
        "exit-overflow",
        "read-failed-int",
    ],
)
def test_failure_evidence_strict_types_and_bounds(tmp_path, section, key, bad):
    value = witness.failure_evidence(diagnostic_observation(tmp_path), "run_1")
    target = value if section is None else value[section]
    target[key] = bad
    with pytest.raises(ValueError):
        witness.validate_failure_evidence(value)


@pytest.mark.parametrize("section", [None, "observer", "job", "collectors", "stdout", "exit"])
@pytest.mark.parametrize("mutation", ["extra", "missing"])
def test_failure_evidence_exact_field_sets(tmp_path, section, mutation):
    value = witness.failure_evidence(diagnostic_observation(tmp_path), "run_1")
    target = value if section is None else value[section]
    if mutation == "extra":
        target["private_path"] = "private-secret"
    else:
        target.pop(next(iter(target)))
    with pytest.raises(ValueError):
        witness.validate_failure_evidence(value)


@pytest.mark.parametrize(
    "section", ["admitted_counts", "test_counts", "run_markers", "start_milestones", "run_counters"]
)
@pytest.mark.parametrize("bad", [True, -1, "private-secret", 2**32])
def test_nested_counter_schema_never_coerces(tmp_path, section, bad):
    value = witness.failure_evidence(diagnostic_observation(tmp_path), "run_1")
    owner = value["observer"] if section == "admitted_counts" else value["stdout"]
    owner[section][next(iter(owner[section]))] = bad
    with pytest.raises(ValueError):
        witness.validate_failure_evidence(value)


def test_unavailable_snapshot_never_attaches_partial_mutable_data(tmp_path):
    value = witness.failure_evidence(diagnostic_observation(tmp_path), "run_1")
    for state in ("not_quiescent", "unavailable"):
        value["snapshot_state"] = state
        with pytest.raises(ValueError):
            witness.validate_failure_evidence(value)


def test_diagnostic_failure_emits_fixed_unavailable_without_private_exception(
    tmp_path, monkeypatch, capsys
):
    actual = diagnostic_observation(tmp_path)
    monkeypatch.setattr(
        witness,
        "failure_evidence",
        lambda *args: (_ for _ in ()).throw(RuntimeError("private-token-user-name-path")),
    )
    witness.emit_failure_evidence(actual, "run_1")
    raw = capsys.readouterr().out
    assert "private" not in raw
    value = json.loads(raw.split("=", 1)[1])
    witness.validate_failure_evidence(value)
    assert value["snapshot_state"] == "unavailable"
    monkeypatch.setattr(
        witness, "print", lambda *args: (_ for _ in ()).throw(OSError("private")), raising=False
    )
    witness.emit_failure_evidence(actual, "run_1")  # Output errors also cannot escape.


@pytest.mark.parametrize("failure", [False, True])
def test_facade_close_retains_only_scalar_and_releases_process_lifetime(monkeypatch, failure):
    import gc
    import weakref

    observed = SimpleNamespace(relay_exit_code=None)
    released = []

    class Process:
        returncode = 24

        def poll(self):
            pytest.fail("diagnostic close must not query the process")

        def __del__(self):
            released.append(True)

    process = Process()
    reference = weakref.ref(process)
    owned = object.__new__(witness.FacadeOwnedProcess)
    owned.facade_observation, owned.process = observed, process
    del process
    error = common.WitnessError("cleanup_incomplete")

    def close(self):
        self.process = None
        if failure:
            raise error

    monkeypatch.setattr(common.OwnedProcess, "close", close)
    for _ in range(2):
        if failure:
            try:
                owned.close()
            except common.WitnessError as caught:
                assert caught is error
            else:
                pytest.fail("cleanup failure was swallowed")
        else:
            owned.close()
        # A caught exception's traceback can retain ordinary call locals while
        # retained by the caller. Discard it to check persistent diagnostic state.
        error.__traceback__ = None
        gc.collect()
        assert reference() is None and released == [True]
        assert observed.relay_exit_code == 24
        assert vars(observed) == {"relay_exit_code": 24}


@pytest.mark.parametrize("original_code", ["child_failed", "child_timeout"])
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_invoke_diagnostic_error_never_changes_primary_or_cleanup_outcomes(
    monkeypatch, tmp_path, capsys, original_code, cleanup_fails
):
    original = common.WitnessError(original_code)

    class Observation:
        def __init__(self, **kwargs):
            self.started, self.jobs, self.resources = False, [], []

        def start(self):
            self.started = True

        def job_factory(self):
            return None

        def close(self):
            if cleanup_fails:
                raise boundary.ObservationFailure("cleanup_incomplete")

        def quiescent(self):
            raise RuntimeError("private-user-token")

    def launch(*args, **kwargs):
        raise original

    monkeypatch.setattr(witness, "FacadeObservation", Observation)
    monkeypatch.setattr(witness, "FacadeOwnedProcess", launch)
    with pytest.raises(common.WitnessError) as caught:
        witness.invoke_facade(
            [],
            work=tmp_path,
            env={"TEMP": str(tmp_path)},
            mode="run_1",
            expected=expected(),
            images={},
            observations=[],
        )
    assert str(caught.value) == ("cleanup_incomplete" if cleanup_fails else original_code)
    if not cleanup_fails:
        assert caught.value is original
    raw = capsys.readouterr().out
    assert "private" not in raw
    values = [json.loads(line.split("=", 1)[1]) for line in raw.splitlines()]
    assert values[0]["snapshot_state"] == "unavailable"
    assert [value["stage"] for value in values[1:]] == (
        ["run_1", "cleanup"] if cleanup_fails else []
    )


@pytest.mark.parametrize("mode", ["test_valid", "test_invalid"])
def test_success_path_does_not_emit_diagnostics_or_change_receipt(monkeypatch, tmp_path, mode):
    actual, _ = fake_observation(monkeypatch, tmp_path, mode=mode)
    monkeypatch.setattr(witness, "FacadeObservation", lambda **kwargs: actual)
    exit_code = 23 if mode == "test_invalid" else 0

    class Owned:
        def __init__(self, *args, **kwargs):
            self.job = kwargs["job_factory"]()
            self.process = SimpleNamespace(
                stdout=io.BytesIO(test_stdout(mode)), returncode=exit_code
            )
            self.stderr_summary = SimpleNamespace(gate_state="exited", child_exit_code=exit_code)

        def wait(self, timeout):
            assert timeout == 60
            if exit_code:
                raise common.WitnessError(
                    "child_failed",
                    common.diagnostic(
                        "probe_admission",
                        gate_state="exited",
                        child_exit_code=23,
                        relay_exit_code=23,
                    ),
                )

        def close(self):
            self.job.close()

    monkeypatch.setattr(witness, "FacadeOwnedProcess", Owned)
    monkeypatch.setattr(
        witness, "emit_failure_evidence", lambda *args: pytest.fail("success diagnostic")
    )
    result, record = witness.invoke_facade(
        [],
        work=tmp_path,
        env={"TEMP": str(tmp_path)},
        mode=mode,
        expected=expected(),
        images={},
        observations=[],
    )
    assert result == {} and record == observation(mode)


@pytest.mark.parametrize(
    "key",
    [
        "process_capture_stopped",
        "process_admission_stopped",
        "temp_stopped",
        "stdout_stopped",
        "stderr_stopped",
    ],
)
def test_quiescent_schema_rejects_contradictory_live_reader(tmp_path, key):
    value = witness.failure_evidence(diagnostic_observation(tmp_path), "run_1")
    value["collectors"][key] = False
    with pytest.raises(ValueError):
        witness.validate_failure_evidence(value)


def test_final_exit_collector_must_match_quiescent_collection(tmp_path):
    value = witness.failure_evidence(diagnostic_observation(tmp_path), "run_1")
    value["exit"]["stderr_stopped"] = None
    value["exit"]["stderr_read_failed"] = None
    value["exit"]["gate_state"] = None
    with pytest.raises(ValueError):
        witness.validate_failure_evidence(value)
