"""Hosted-only observation of the unchanged Start preflight boundary.

This copies the already prepared clean runtime probe. It is a negative-boundary
reproduction, never installed-media acceptance. No witness gate is changed here.
"""

from __future__ import annotations

import ctypes
import importlib.util
import os
import re
import shutil
import struct
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "_alpha_preflight_observed", ROOT / "scripts/installed_alpha_launcher_witness.py"
)
assert SPEC is not None and SPEC.loader is not None
alpha = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(alpha)
common = alpha.common
boundary = alpha.boundary
MAX_PROCESSES, MAX_EVENTS = boundary.MAX_PROCESSES, boundary.MAX_EVENTS
PROCESS_KINDS, TEMP_KINDS, ACTIONS, ERRORS = (
    boundary.PROCESS_KINDS,
    boundary.TEMP_KINDS,
    boundary.ACTIONS,
    boundary.ERRORS,
)
ObservationFailure, need = boundary.ObservationFailure, boundary.need
classify_temp, notification_categories = boundary.classify_temp, boundary.notification_categories
ProcessObserver, TempObserver = boundary.ProcessObserver, boundary.TempObserver
process_record, observers_quiescent = boundary.process_record, boundary.observers_quiescent
guarded_assignment = boundary.guarded_assignment

COUNT_FIELDS = (
    {"job_total", "job_active", "distinct_births", "temp_events"}
    | {"process_" + key for key in PROCESS_KINDS}
    | {"temp_" + kind + "_" + action for kind in TEMP_KINDS for action in ACTIONS.values()}
)
BOOL_FIELDS = {
    "runtime_verified",
    "start_verified",
    "process_coverage_complete",
    "cleanup_complete",
    "temporary_root_empty",
    "old_gate_rejected",
    "initialization_contract_verified",
}
FIELDS = (
    COUNT_FIELDS
    | BOOL_FIELDS
    | {"schema_version", "error", "old_contract"}
    | {f"birth_{index}_class" for index in range(1, MAX_PROCESSES + 1)}
)


def empty_record():
    return {
        "schema_version": "owned-start-preflight-qualification-v1",
        "error": "none",
        "old_contract": "none",
        **dict.fromkeys(COUNT_FIELDS, 0),
        **dict.fromkeys(BOOL_FIELDS, False),
        **{f"birth_{index}_class": None for index in range(1, MAX_PROCESSES + 1)},
    }


def validate_record(value):
    need(type(value) is dict and value.keys() == FIELDS)
    need(
        type(value["schema_version"]) is str
        and value["schema_version"] == "owned-start-preflight-qualification-v1"
    )
    need(type(value["error"]) is str and value["error"] in ERRORS)
    need(
        type(value["old_contract"]) is str
        and value["old_contract"] in alpha.DIAGNOSTIC_CONTRACTS | {"none"}
    )
    for key in BOOL_FIELDS:
        need(type(value[key]) is bool)
    for key in COUNT_FIELDS:
        need(type(value[key]) is int and 0 <= value[key] <= MAX_EVENTS)
    for index in range(1, MAX_PROCESSES + 1):
        value_at = value[f"birth_{index}_class"]
        need(value_at is None or type(value_at) is str and value_at in PROCESS_KINDS)
    if value["process_coverage_complete"]:
        total = value["job_total"]
        need(0 < total <= MAX_PROCESSES and value["distinct_births"] == total)
        need(value["job_active"] == 0 and value["process_unknown"] == 0)
        kinds = [value[f"birth_{index}_class"] for index in range(1, total + 1)]
        need(all(kind is not None for kind in kinds))
        need(
            all(
                value[f"birth_{index}_class"] is None
                for index in range(total + 1, MAX_PROCESSES + 1)
            )
        )
        need(all(value["process_" + kind] == kinds.count(kind) for kind in PROCESS_KINDS))
    need(len(common.canonical(value)) <= common.MAX_BYTES)


IDENTITY_PROBE = """import hashlib, importlib.metadata, importlib.util, json, pathlib, site, sys
v=json.loads(pathlib.Path(sys.argv[1]).read_bytes())
prefix=pathlib.Path(sys.prefix).resolve(strict=True)
assert prefix == pathlib.Path(v['prefix']).resolve(strict=True)
assert sys.prefix != sys.base_prefix and sys.flags.isolated == 1 and site.ENABLE_USER_SITE is False
assert (pathlib.Path(sys._base_executable).resolve(strict=True)
        == pathlib.Path(v['base']).resolve(strict=True))
assert pathlib.Path(importlib.util.find_spec('k5vision').origin).resolve().is_relative_to(prefix)
for name, expected in v['versions'].items():
    assert importlib.metadata.version(name) == expected
package=importlib.metadata.distribution('k5-vision')
for name, expected in v['payload'].items():
    p=pathlib.Path(package.locate_file(name)).resolve(strict=True)
    assert p.is_relative_to(prefix) and hashlib.sha256(p.read_bytes()).hexdigest() in expected
print('COPIED_RUNTIME_VERIFIED')
"""


def trusted_payload_variants(source: Path):
    """Generate exact checkout forms from trusted Git bytes, never normalize input.

    The older hosted probe was installed from a Windows checkout. Only tracked
    Python sources and the two enumerated UTF-8 package data files permit its
    deterministic LF-to-CRLF checkout representation. Production hashes stay exact.
    """
    text_data = {
        "k5vision/data/analytics-runtime-manifest.json",
        "k5vision/data/analytics-runtime-Apache-2.0.txt",
    }
    result = {}
    for name, exact in alpha.source_payload(source).items():
        choices = [exact]
        if name.endswith(".py") or name in text_data:
            data = common.local_path(source / "src" / name).read_bytes()
            try:
                data.decode("utf-8", errors="strict")
            except UnicodeError:
                raise ObservationFailure("fixture_admission") from None
            need(b"\r" not in data)
            converted = common.hashlib.sha256(data.replace(b"\n", b"\r\n")).hexdigest()
            if converted != exact:
                choices.append(converted)
        result[name] = choices
    return result


def copy_runtime(source_python: Path, target: Path, base: Path):
    source_python = common.local_path(source_python)
    need(
        source_python.name.lower() == "python.exe"
        and source_python.parent.name.lower() == "scripts"
    )
    source = common.local_path(source_python.parent.parent, directory=True)
    config = common.local_path(source / "pyvenv.cfg")
    need(config.stat().st_size < 8192)
    values = {}
    for line in config.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = (item.strip() for item in line.split("=", 1))
            need(key not in values)
            values[key] = value
    need(values.get("include-system-site-packages") == "false")
    need(common.local_path(Path(values["home"]), directory=True) == base.parent)
    need(common.local_path(Path(values["executable"])) == base)
    need(values.get("version") == sys.version.split()[0])
    before = alpha.tree_manifest(source)
    shutil.copytree(source, target)
    need(alpha.tree_manifest(target) == before and alpha.tree_manifest(source) == before)


def run_reproduction(tmp_path: Path, monkeypatch, record, resources):
    base = common.admitted_gate_python(dict(os.environ))
    owned = tmp_path / "owned-preflight"
    owned.mkdir()
    installed = owned / "installed"
    installed.mkdir()
    env = alpha.clean_environment(dict(os.environ), owned)
    for name in ("TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        Path(env[name]).mkdir(parents=True, exist_ok=True)
    git = common.local_path(Path(shutil.which("git")))
    revision = (
        common.capture(
            [str(git), "-C", str(ROOT), "rev-parse", "HEAD"],
            cwd=owned,
            env=env,
            operation="driver_admission",
            limit=128,
        )
        .decode("ascii")
        .strip()
    )
    need(re.fullmatch(r"[0-9a-f]{40}", revision) is not None)
    command = common.archive_command(ROOT, owned / "source.zip", revision)
    command[0] = str(git)
    common.run(command, cwd=owned, env=env, operation="archive_candidate")
    source = owned / "source"
    common.extract_archive(owned / "source.zip", source)
    start = source / "scripts/windows-alpha/Start-K5VisionAlpha.ps1"
    shutil.copyfile(start, installed / start.name)
    need(start.read_bytes() == (installed / start.name).read_bytes())
    record["start_verified"] = True
    copy_runtime(Path(os.environ["K5_PREFLIGHT_PROBE_PYTHON"]), installed / ".venv", base)
    probe, inputs = owned / "identity-probe.py", owned / "identity-inputs.json"
    probe.write_text(IDENTITY_PROBE, encoding="ascii")
    inputs.write_bytes(
        common.canonical(
            {
                "prefix": str(installed / ".venv"),
                "base": str(base),
                "versions": common.requirements(
                    source / "scripts/windows-alpha/runtime-requirements.txt"
                ),
                "payload": trusted_payload_variants(source),
            }
        )
    )
    python = installed / ".venv/Scripts/python.exe"
    output = common.capture(
        [str(python), "-I", "-B", str(probe), str(inputs)],
        cwd=owned,
        env=env,
        operation="probe_admission",
        limit=128,
    )
    need(output.strip() == b"COPIED_RUNTIME_VERIFIED")
    record["runtime_verified"] = True
    config = owned / "invalid-analytics.json"
    config.write_bytes(b'{"schema_version":1,"provider":"invalid-selected-provider"}')
    env["K5_ANALYTICS_CONFIG"] = str(config)
    shell = common.local_path(
        Path(env["SYSTEMROOT"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    admitted = {
        "base_python": (base, common.file_hash(base)),
        "venv_python": (python, common.file_hash(python)),
        "powershell": (shell, common.file_hash(shell)),
    }
    console = common.local_path(Path(env["SYSTEMROOT"]) / "System32/conhost.exe")
    admitted["console_host"] = (console, common.file_hash(console))
    envelope = owned / "invoke-start.ps1"
    envelope.write_text(alpha.ENVELOPE, encoding="ascii", newline="\n")
    need(not (installed / "gstreamer-version.txt").exists())
    captured, owned_observations = [], []
    actual_observation = boundary.PreflightObservation

    class CapturedObservation(actual_observation):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            captured.append(self)

    # Capture construction only. The actual production negative path creates its
    # own single observer and performs every refusal/initialization acceptance gate.
    monkeypatch.setattr(boundary, "PreflightObservation", CapturedObservation)
    try:
        result = alpha.invoke_start(
            alpha.start_command(shell, envelope, installed, 8011),
            work=owned,
            env=env,
            operation="probe_admission",
            invalid=True,
            admitted_images=admitted,
            owned_observations=owned_observations,
        )
        need(
            result
            == {
                "invalid_config_refused": True,
                "invalid_config_no_session": True,
                "invalid_config_no_media": True,
            },
            "launcher_rejected",
        )
        need(len(captured) == 1 and owned_observations == captured, "incomplete")
        observation = captured[0]
        need(observation.finished and observation.quiescent(), "incomplete")
        summary = observation.summary
        boundary.validate_invalid_initialization(summary)
        record.update(
            {
                key: value
                for key, value in summary.items()
                if key in COUNT_FIELDS | BOOL_FIELDS or key.startswith("birth_") or key == "error"
            }
        )
        record["initialization_contract_verified"] = True
    finally:
        resources[:] = [resource for observation in captured for resource in observation.resources]


def close_observation(observation, resources):
    try:
        observation.close()
    finally:
        resources[:] = observation.resources


def remove_owned_fixture(owned, resources):
    need(observers_quiescent(resources), "cleanup_incomplete")
    if owned.exists():
        shutil.rmtree(owned)


def validate_expected_legacy_refusal(error):
    need(isinstance(error, alpha.AlphaWitnessError), "launcher_rejected")
    value = error.alpha_diagnostic
    alpha.validate_alpha_diagnostic(value)
    expected = {
        "stage": "invalid_config",
        "contract": "invalid_job_total",
        "failure_code": "receipt_invalid",
        "child_exit_code": 23,
        "relay_exit_code": 23,
        "gate_state": "exited",
        "timed_out": False,
        "collector_finished": True,
        "output_invalid": False,
        "sessions_empty": True,
        "launcher_requested": True,
        "launcher_returned": True,
        "health_confirmed": False,
        "operator_request_observed": False,
        "job_total": 5,
        "job_active": 0,
        **{f"marker_{key}": int(key == "refusal") for key in alpha.MARKERS},
    }
    need(
        all(
            value[key] == expected_value and type(value[key]) is type(expected_value)
            for key, expected_value in expected.items()
        ),
        "launcher_rejected",
    )


@pytest.mark.skipif(os.name != "nt", reason="Hosted Windows owned-only boundary reproduction")
def test_windows_actual_start_bad_config_owned_observation(tmp_path, monkeypatch):
    record = empty_record()
    resources = []
    failure = None
    try:
        run_reproduction(tmp_path, monkeypatch, record, resources)
    except BaseException as error:
        failure = str(error) if isinstance(error, ObservationFailure) else "fixture_admission"
        if record["error"] == "none":
            record["error"] = failure
    finally:
        try:
            owned = tmp_path / "owned-preflight"
            remove_owned_fixture(owned, resources)
            record["cleanup_complete"] = record["cleanup_complete"] and not owned.exists()
        except BaseException:
            failure = record["error"] = "cleanup_incomplete"
        try:
            validate_record(record)
            print("K5_OWNED_PREFLIGHT_QUALIFICATION=" + common.canonical(record).decode("ascii"))
        except BaseException:
            failure = "incomplete"
            print("K5_OWNED_PREFLIGHT_QUALIFICATION_INVALID")
    if failure is not None:
        pytest.fail("Owned Start preflight observation failed: " + failure, pytrace=False)


def test_fixed_temp_categories_never_return_names():
    assert classify_temp("__PSScriptPolicyTest_abcdefgh.xyz.ps1") == "policy_probe"
    assert classify_temp("__PSScriptPolicyTest_abcdefgh.xyz.psm1") == "policy_probe"
    assert classify_temp("K5VisionAlpha-" + "a" * 32 + "\\users.sqlite3") == "alpha_session"
    assert classify_temp("sensitive-unexpected-name") == "other_owned_temp"


def test_notification_parser_is_bounded_and_source_free():
    name = "__PSScriptPolicyTest_abcdefgh.xyz.ps1".encode("utf-16-le")
    assert notification_categories(struct.pack("<III", 0, 1, len(name)) + name) == [
        ("policy_probe", "added")
    ]
    for data in (b"", b"x" * 65537, struct.pack("<III", 0, 99, 2) + b"aa"):
        with pytest.raises(ObservationFailure):
            notification_categories(data)


@pytest.mark.parametrize(
    "change",
    [
        {"path": "secret"},
        {"error": "raw exception"},
        {"birth_1_class": "private executable"},
        {"job_total": True},
        {"job_total": 257},
        {"process_coverage_complete": 1},
        {"old_contract": "private path"},
    ],
)
def test_observation_record_rejects_unknown_or_unbounded_content(change):
    record = empty_record()
    record.update(change)
    with pytest.raises(ObservationFailure):
        validate_record(record)


def test_hosted_reproduction_paths_cannot_trigger_native_candidate_job():
    from fnmatch import fnmatchcase

    text = (ROOT / ".github/workflows/installed-analytics-candidate.yml").read_text()
    push = text.split("  push:\n", 1)[1].split("\n\n", 1)[0]
    patterns = [
        line.strip()[2:].strip("\"'")
        for line in push.split("    paths:\n", 1)[1].splitlines()
        if line.strip().startswith("- ")
    ]
    changed = (
        "tests/test_windows_alpha_preflight_boundary.py",
        ".github/workflows/windows-alpha-script-smoke.yml",
    )
    assert patterns and all(
        not fnmatchcase(path, pattern) for path in changed for pattern in patterns
    )
    assert "scripts/windows_owned_preflight.py" in patterns
    smoke = (ROOT / changed[1]).read_text()
    assert "K5_PREFLIGHT_PROBE_PYTHON" in smoke
    assert "tests/test_windows_alpha_preflight_boundary.py --no-cov -q -s" in smoke
    assert "windows_real_venv_child_is_owned" in smoke
    assert "test_windows_real_directory_guard_remembers_created_then_deleted_session" in smoke


def fake_process_observer(monkeypatch, tmp_path, *, belongs=True, denied=False):
    from types import SimpleNamespace

    path = tmp_path / "admitted.exe"
    path.write_bytes(b"admitted")
    calls, births = [], [100]
    aborted = []

    def opened(access, inherit, pid):
        calls.append(("open", access, inherit, pid))
        return None if denied else 123

    def membership(_handle, job, output):
        calls.append(("membership", job))
        output._obj.value = belongs
        return True

    def times(_handle, created, *_rest):
        calls.append(("birth",))
        created._obj.value = births[0]
        return True

    def image(_handle, _flags, buffer, _size):
        calls.append(("image",))
        buffer.value = str(path)
        return True

    observer = object.__new__(ProcessObserver)
    observer.api = SimpleNamespace(
        OpenProcess=opened,
        IsProcessInJob=membership,
        GetProcessTimes=times,
        QueryFullProcessImageNameW=image,
        CloseHandle=lambda handle: calls.append(("close", handle)) or True,
    )
    observer.job = SimpleNamespace(handle=456, abort=lambda: aborted.append(True))
    observer.job_query_handle = 456
    observer._release = lambda: None
    observer.admitted = {"base_python": (path, common.file_hash(path))}
    observer.births, observer.handles, observer.error = {}, [], "none"
    observer.stop, observer.port = threading.Event(), 789
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
    return observer, calls, births, aborted


def test_owned_membership_precedes_birth_and_image_and_dedupes_births(monkeypatch, tmp_path):
    observer, calls, births, _aborted = fake_process_observer(monkeypatch, tmp_path)
    observer._observe(10)
    assert [call[0] for call in calls[:4]] == ["open", "membership", "birth", "image"]
    assert calls[0][1:3] == (0x1000, False)
    observer._observe(10)
    assert len(observer.births) == 1 and len(observer.handles) == 1
    births[0] = 200
    observer._observe(10)
    assert len(observer.births) == 2 and len(observer.handles) == 2


def test_reused_or_foreign_job_pid_never_exposes_image_or_birth(monkeypatch, tmp_path):
    observer, calls, _births, _aborted = fake_process_observer(monkeypatch, tmp_path, belongs=False)
    with pytest.raises(ObservationFailure, match="ownership_unproven"):
        observer._observe(10)
    assert [call[0] for call in calls] == ["open", "membership", "close"]
    assert not observer.births


def test_owned_open_denial_stops_job_without_retry_or_rights_expansion(monkeypatch, tmp_path):
    observer, calls, _births, aborted = fake_process_observer(monkeypatch, tmp_path, denied=True)

    def notification(_port, message, key, value, _timeout):
        message._obj.value, key._obj.value, value._obj.value = 6, 1, 10
        return True

    observer.api.GetQueuedCompletionStatus = notification
    observer._read()
    assert observer.error == "access_denied" and aborted == [True]
    assert calls == [("open", 0x1000, False, 10)]
    assert not observer.births


def test_incomplete_process_queue_cannot_claim_coverage():
    record = empty_record()
    record.update(
        job_total=5,
        distinct_births=4,
        process_base_python=2,
        process_venv_python=1,
        process_powershell=1,
    )
    assert record["distinct_births"] != record["job_total"]
    assert record["process_coverage_complete"] is False


def test_temp_reissue_is_blocked_after_cancellation():
    from types import SimpleNamespace

    watcher = object.__new__(TempObserver)
    watcher.io_lock, watcher.stop = threading.Lock(), threading.Event()
    watcher.stop.set()
    calls = []
    watcher.api = SimpleNamespace(
        ResetEvent=lambda *_: calls.append("reset"),
        ReadDirectoryChangesW=lambda *_: calls.append("read"),
    )
    assert watcher._issue() is False and calls == []


def test_temp_access_denial_aborts_only_owned_execution(monkeypatch):
    from types import SimpleNamespace

    watcher = object.__new__(TempObserver)
    watcher.stop, watcher.error = threading.Event(), "none"
    watcher.drain_requested, watcher.drained = threading.Event(), threading.Event()
    watcher.begin = threading.Event()
    watcher.begin.set()
    watcher.event, watcher.handle, watcher.overlapped = 1, 2, ctypes.c_uint64()
    aborted = []
    watcher.abort = lambda: aborted.append(True)
    watcher.api = SimpleNamespace(
        WaitForSingleObject=lambda *_: 0, GetOverlappedResult=lambda *_: False
    )
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
    watcher._read()
    assert watcher.error == "access_denied" and aborted == [True]


def test_trusted_checkout_variants_reject_mixed_endings_and_tamper(tmp_path):
    package = tmp_path / "src/k5vision"
    (package / "data").mkdir(parents=True)
    files = {
        "analytics_config.py": b"first = 1\nsecond = 2\n",
        "data/analytics-runtime-manifest.json": b'{\n"schema": 1\n}\n',
        "data/analytics-runtime-Apache-2.0.txt": b"reviewed\nnotice\n",
        "data/other.txt": b"unclassified\ntext\n",
    }
    for name, data in files.items():
        (package / name).write_bytes(data)
    allowed = trusted_payload_variants(tmp_path)
    for name in (
        "analytics_config.py",
        "data/analytics-runtime-manifest.json",
        "data/analytics-runtime-Apache-2.0.txt",
    ):
        data = files[name]
        expected = allowed["k5vision/" + name]
        assert common.hashlib.sha256(data).hexdigest() in expected
        assert common.hashlib.sha256(data.replace(b"\n", b"\r\n")).hexdigest() in expected
        assert common.hashlib.sha256(data.replace(b"\n", b"\r\n", 1)).hexdigest() not in expected
        assert common.hashlib.sha256(data + b"tamper").hexdigest() not in expected
    assert len(allowed["k5vision/data/other.txt"]) == 1
    (package / "analytics_config.py").write_bytes(b"not canonical\r\n")
    with pytest.raises(ObservationFailure):
        trusted_payload_variants(tmp_path)


def test_process_record_requires_full_distinct_birth_reconciliation():
    from types import SimpleNamespace

    births = {
        (1, 100): "base_python",
        (2, 200): "powershell",
        (3, 300): "venv_python",
        (4, 400): "base_python",
    }
    observer = SimpleNamespace(error="none", births=births)
    job = SimpleNamespace(observer=observer, final_total=5, final_active=0)
    record = empty_record()
    process_record(record, job)
    assert record["distinct_births"] == 4 and record["process_coverage_complete"] is False
    births[(5, 500)] = "console_host"
    record = empty_record()
    process_record(record, job)
    assert record["distinct_births"] == 5 and record["process_coverage_complete"] is True
    assert record["birth_5_class"] == "console_host"
    births[(5, 500)] = "unknown"
    record = empty_record()
    process_record(record, job)
    assert record["process_coverage_complete"] is False


def test_temp_finish_cancels_only_its_request_before_closing_handles():
    from types import SimpleNamespace

    watcher = object.__new__(TempObserver)
    watcher.io_lock, watcher.stop = threading.Lock(), threading.Event()
    watcher.error = "none"
    watcher.drain_complete = False
    watcher.drain_requested, watcher.drained = threading.Event(), threading.Event()
    watcher.drained.set()
    watcher.handle, watcher.event, watcher.overlapped, watcher.closed = (
        1,
        2,
        ctypes.c_uint64(),
        False,
    )
    calls = []
    watcher.api = SimpleNamespace(
        CancelIoEx=lambda handle, _overlap: calls.append(("cancel", handle)) or True,
        CloseHandle=lambda handle: calls.append(("close", handle)) or True,
    )
    watcher.thread = SimpleNamespace(
        join=lambda seconds: calls.append(("join", seconds)), is_alive=lambda: False
    )
    watcher.finish()
    assert calls == [("cancel", 1), ("join", 3), ("close", 2), ("close", 1)]
    assert watcher.stop.is_set() and watcher.closed


def test_reader_join_timeout_keeps_query_handle_and_blocks_file_cleanup():
    from types import SimpleNamespace

    observer = object.__new__(ProcessObserver)
    observer.births, observer.error = {(10, 100): "base_python"}, "none"
    observer.stop, observer.resource_lock = threading.Event(), threading.Lock()
    observer.closed, observer.port, observer.job_query_handle = False, 123, 456
    alive, calls = [True], []
    observer.thread = SimpleNamespace(
        join=lambda seconds: calls.append(("join", seconds)), is_alive=lambda: alive[0]
    )
    observer.api = SimpleNamespace(
        PostQueuedCompletionStatus=lambda *_: calls.append(("wake",)),
        CloseHandle=lambda handle: calls.append(("close", handle)),
    )
    with pytest.raises(ObservationFailure, match="cleanup_incomplete"):
        observer.finish(1)
    assert calls == [("wake",), ("join", 3)]
    assert observer.job_query_handle == 456 and not observer.closed
    assert observers_quiescent([observer]) is False
    alive[0] = False
    assert observers_quiescent([observer]) is True


def test_membership_uses_observer_duplicate_not_closed_owner_handle(monkeypatch, tmp_path):
    observer, calls, _births, _aborted = fake_process_observer(monkeypatch, tmp_path)
    observer.job.handle = 999  # Original owner handle may already be closed/reused.
    observer._observe(10)
    assert ("membership", 456) in calls and ("membership", 999) not in calls


def test_false_complete_record_is_rejected():
    record = empty_record()
    record.update(job_total=5, distinct_births=4, process_coverage_complete=True)
    with pytest.raises(ObservationFailure):
        validate_record(record)


@pytest.mark.parametrize("aborted,error", [(True, "none"), (False, "access_denied")])
def test_observer_abort_before_assignment_cannot_open_execution_gate(aborted, error):
    from types import SimpleNamespace

    calls = []
    job = SimpleNamespace(
        abort_lock=threading.Lock(),
        aborted=aborted,
        handle=123,
        api=SimpleNamespace(TerminateJobObject=lambda *_: calls.append("terminate")),
    )
    with pytest.raises(ObservationFailure, match="incomplete"):
        guarded_assignment(
            job, object(), lambda: (error, "none"), lambda _process: calls.append("assign")
        )
    assert calls == []
    assert job.aborted is True


def test_denial_during_assignment_terminates_before_return_to_gate_opener():
    from types import SimpleNamespace

    calls, errors = [], ["none", "none"]
    job = SimpleNamespace(
        abort_lock=threading.Lock(),
        aborted=False,
        handle=123,
        api=SimpleNamespace(TerminateJobObject=lambda *_: calls.append("terminate")),
    )

    def assign(_process):
        calls.append("assign")
        errors[0] = "access_denied"

    with pytest.raises(ObservationFailure, match="incomplete"):
        guarded_assignment(job, object(), lambda: tuple(errors), assign)
    assert calls == ["assign", "terminate"] and job.aborted is True


def load_boundary_helper():
    spec = importlib.util.spec_from_file_location(
        "_owned_preflight_contract", ROOT / "scripts/windows_owned_preflight.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reviewed_initialization_requires_exact_classes_not_total_alone():
    helper = load_boundary_helper()
    expected = {
        "base_python": 2,
        "powershell": 1,
        "venv_python": 1,
        "console_host": 1,
        "dotnet_compiler": 0,
        "unknown": 0,
    }
    assert helper.EXPECTED_PROCESSES == expected
    helper.validate_process_contract(expected, total=5, active=0, covered=5)
    wrong = {**expected, "console_host": 0, "dotnet_compiler": 1}
    with pytest.raises(helper.ObservationFailure):
        helper.validate_process_contract(wrong, total=5, active=0, covered=5)


def test_reviewed_policy_trace_requires_each_complete_lifecycle():
    helper = load_boundary_helper()
    trace = helper.PolicyLifecycle()
    for name in (
        "__PSScriptPolicyTest_aaaaaaaa.aaa.ps1",
        "__PSScriptPolicyTest_bbbbbbbb.bbb.psm1",
        "__PSScriptPolicyTest_cccccccc.ccc.ps1",
        "__PSScriptPolicyTest_dddddddd.ddd.psm1",
    ):
        for action in ("added", "modified", "removed"):
            trace.observe(name, action)
    assert trace.complete()
    with pytest.raises(helper.ObservationFailure):
        trace.observe("__PSScriptPolicyTest_eeeeeeee.eee.ps1", "added")


POLICY_NAMES = (
    "__PSScriptPolicyTest_aaaaaaaa.aaa.ps1",
    "__PSScriptPolicyTest_bbbbbbbb.bbb.psm1",
    "__PSScriptPolicyTest_cccccccc.ccc.ps1",
    "__PSScriptPolicyTest_dddddddd.ddd.psm1",
)


def complete_policy_trace():
    trace = boundary.PolicyLifecycle()
    for name in POLICY_NAMES:
        for action in ("added", "modified", "removed"):
            trace.observe(name, action)
    return trace


@pytest.mark.parametrize(
    "name",
    [
        "__PSScriptPolicyTest_x.ps1",
        "__PSScriptPolicyTest_aaaaaaaa.aaa.psd1",
        "__PSScriptPolicyTest_AAAAAAAA.aaa.ps1",
        "__PSScriptPolicyTest_aaaaaaaa.aaa.ps1:stream",
        "sub/__PSScriptPolicyTest_aaaaaaaa.aaa.ps1",
        "../__PSScriptPolicyTest_aaaaaaaa.aaa.ps1",
        "K5VisionAlpha-" + "a" * 32,
        "unrecognized-private-name",
        None,
    ],
)
def test_policy_lifecycle_rejects_non_exact_top_level_names(name):
    trace = boundary.PolicyLifecycle()
    with pytest.raises(ObservationFailure, match="unexpected_temp"):
        trace.observe(name, "added")
    assert trace.invalid and not trace.complete()


@pytest.mark.parametrize(
    "actions",
    [
        ("modified",),
        ("removed",),
        ("renamed_from",),
        ("added", "added"),
        ("added", "removed"),
        ("added", "modified", "modified"),
        ("added", "modified", "removed", "modified"),
        ("added", "modified", "removed", "added"),
    ],
)
def test_policy_lifecycle_rejects_every_unexpected_event_order(actions):
    trace = boundary.PolicyLifecycle()
    with pytest.raises(ObservationFailure):
        for action in actions:
            trace.observe(POLICY_NAMES[0], action)
    assert trace.invalid and not trace.complete()
    with pytest.raises(ObservationFailure):
        trace.observe(POLICY_NAMES[1], "added")


def test_lifecycle_error_is_terminal_even_after_complete_trace():
    trace = complete_policy_trace()
    assert trace.complete()
    with pytest.raises(ObservationFailure):
        trace.observe("__PSScriptPolicyTest_eeeeeeee.eee.ps1", "added")
    assert not trace.complete()


def test_policy_aggregate_counts_cannot_substitute_for_per_name_lifecycles():
    trace = boundary.PolicyLifecycle()
    for name in POLICY_NAMES:
        trace.observe(name, "added")
    trace.observe(POLICY_NAMES[0], "modified")
    with pytest.raises(ObservationFailure):
        trace.observe(POLICY_NAMES[0], "modified")
    assert not trace.complete()
    wrong_extensions = boundary.PolicyLifecycle()
    for name in POLICY_NAMES[::2]:
        wrong_extensions.observe(name, "added")
    with pytest.raises(ObservationFailure):
        wrong_extensions.observe("__PSScriptPolicyTest_eeeeeeee.eee.ps1", "added")
    incomplete = boundary.PolicyLifecycle()
    for name in POLICY_NAMES:
        incomplete.observe(name, "added")
        incomplete.observe(name, "modified")
    assert not incomplete.complete()


def valid_initialization_summary():
    value = boundary.empty_summary()
    value.update(
        job_total=5, distinct_births=5, temp_events=12, policy_ps1_files=2, policy_psm1_files=2
    )
    value.update(dict.fromkeys(boundary.SUMMARY_BOOLEANS, True))
    kinds = ["base_python", "powershell", "venv_python", "console_host", "base_python"]
    for kind, count in boundary.EXPECTED_PROCESSES.items():
        value["process_" + kind] = count
    for index, kind in enumerate(kinds, 1):
        value[f"birth_{index}_class"] = kind
    for action in ("added", "modified", "removed"):
        value["temp_policy_probe_" + action] = 4
    return value


@pytest.mark.parametrize(
    "change",
    [
        {"path": "raw private path"},
        {"error": "raw private exception"},
        {"birth_1_class": {}},
        {"job_total": True},
        {"job_active": 1},
        {"distinct_births": 4},
        {"job_total": 6},
        {"temp_events": 257},
        {"policy_ps1_files": 1},
        {"policy_lifecycles_complete": False},
        {"temp_drain_complete": False},
        {"temp_other_owned_temp_added": 1},
        {"temp_alpha_session_removed": 1},
        {"temp_policy_probe_renamed_to": 1},
        {"temp_policy_probe_modified": 5},
        {"cleanup_complete": False},
        {"temporary_root_empty": False},
        {"process_coverage_complete": False},
        {"error": "access_denied"},
        {
            "process_console_host": 0,
            "process_dotnet_compiler": 1,
            "birth_4_class": "dotnet_compiler",
        },
    ],
)
def test_initialization_summary_rejects_forged_or_unexpected_profiles(change):
    value = valid_initialization_summary()
    boundary.validate_invalid_initialization(value)
    value.update(change)
    with pytest.raises(ObservationFailure):
        boundary.validate_invalid_initialization(value)


def expected_refusal_error():
    observations = {
        "child_exit_code": 23,
        "relay_exit_code": 23,
        "gate_state": "exited",
        "timed_out": False,
        "collector_finished": True,
        "output_invalid": False,
        "sessions_empty": True,
        "launcher_requested": True,
        "launcher_returned": True,
        "health_confirmed": False,
        "operator_request_observed": False,
        "job_total": 5,
        "job_active": 0,
        **{f"marker_{key}": int(key == "refusal") for key in alpha.MARKERS},
    }
    return alpha.AlphaWitnessError(
        alpha.alpha_diagnostic("invalid_config", "invalid_job_total", observations=observations)
    )


@pytest.mark.parametrize(
    "change",
    [
        {"contract": "invalid_markers"},
        {"stage": "launch_1"},
        {"child_exit_code": 0},
        {"relay_exit_code": 0},
        {"gate_state": "waiting"},
        {"timed_out": True},
        {"collector_finished": False},
        {"output_invalid": True},
        {"sessions_empty": False},
        {"health_confirmed": True},
        {"operator_request_observed": True},
        {"marker_refusal": 0},
        {"marker_admitted": 1},
        {"marker_synthetic": 1},
        {"marker_analytics": 1},
        {"marker_operator": 1},
        {"marker_exit": 1},
        {"job_active": 1},
        {"job_total": 4},
        {"launcher_requested": False},
    ],
)
def test_hosted_green_requires_complete_exact_refusal_evidence(change):
    error = expected_refusal_error()
    validate_expected_legacy_refusal(error)
    error.alpha_diagnostic.update(change)
    with pytest.raises(ObservationFailure, match="launcher_rejected"):
        validate_expected_legacy_refusal(error)


def test_hosted_green_rejects_generic_old_gate_failure():
    for error in (None, common.WitnessError("receipt_invalid"), RuntimeError("private output")):
        with pytest.raises(ObservationFailure, match="launcher_rejected"):
            validate_expected_legacy_refusal(error)


def test_process_image_admission_requires_exact_path_and_hash(tmp_path):
    admitted = tmp_path / "python.exe"
    admitted.write_bytes(b"admitted")
    identities = {"base_python": (admitted, common.file_hash(admitted))}
    assert boundary.process_class(str(admitted), identities) == "base_python"
    assert boundary.process_class(str(tmp_path / "elsewhere/python.exe"), identities) == "unknown"
    admitted.write_bytes(b"tampered")
    with pytest.raises(ObservationFailure):
        boundary.process_class(str(admitted), identities)


def fake_facade(monkeypatch, tmp_path, *, active=0):
    from types import SimpleNamespace

    calls = []
    state = {"total": 5, "active": active}

    class Job:
        def __init__(self):
            self.handle = 123
            self.api = SimpleNamespace(TerminateJobObject=self.terminate)

        def terminate(self, *_args):
            calls.append("terminate")
            state["active"] = 0
            return True

        def accounting(self):
            return SimpleNamespace(total_processes=state["total"], active_processes=state["active"])

        def assign(self, _process):
            calls.append("assign")

        def close(self):
            calls.append("job_closed")

    class Processes:
        def __init__(self, _job, _admitted, resources):
            self.error = "none"
            self.births = {
                (i + 1, (i + 1) * 100): kind
                for i, kind in enumerate(
                    ["base_python", "powershell", "venv_python", "console_host", "base_python"]
                )
            }
            self.thread = SimpleNamespace(is_alive=lambda: False)
            resources.append(self)

        def finish(self, expected):
            calls.append(("process_finished", expected))

    class Temp:
        def __init__(self, _root, abort, resources):
            self.error, self.closed = "none", False
            self.counts = {
                key: value
                for key, value in valid_initialization_summary().items()
                if key.startswith("temp_") and key != "temp_events"
            }
            self.total, self.lifecycle = 12, complete_policy_trace()
            self.drain_complete = True
            self.thread = SimpleNamespace(is_alive=lambda: False)
            resources.append(self)

        def finish(self):
            calls.append("temp_finished")
            self.closed = True

    monkeypatch.setattr(boundary, "ProcessObserver", Processes)
    monkeypatch.setattr(boundary, "TempObserver", Temp)
    admitted = {}
    for kind in ("base_python", "powershell", "venv_python", "console_host"):
        path = tmp_path / (kind + ".exe")
        path.write_bytes(kind.encode())
        admitted[kind] = (path, common.file_hash(path))
    temp = tmp_path / "temp"
    temp.mkdir()
    facade = boundary.PreflightObservation(
        SimpleNamespace(WindowsJob=Job, local_path=common.local_path),
        temp_root=temp,
        admitted_images=admitted,
    )
    return facade, calls


def test_facade_uses_explicit_single_job_factory_and_complete_profile(monkeypatch, tmp_path):
    facade, calls = fake_facade(monkeypatch, tmp_path)
    original = common.WindowsJob
    facade.start()
    job = facade.job_factory()
    job.assign(object())
    with pytest.raises(ObservationFailure):
        facade.job_factory()
    job.close()
    summary = facade.finish()
    boundary.validate_invalid_initialization(summary)
    facade.close()
    assert common.WindowsJob is original
    assert calls == ["assign", "terminate", ("process_finished", 5), "job_closed", "temp_finished"]


def test_facade_does_not_erase_active_survivors_through_forced_cleanup(monkeypatch, tmp_path):
    facade, _calls = fake_facade(monkeypatch, tmp_path, active=1)
    facade.start()
    job = facade.job_factory()
    job.close()
    summary = facade.finish()
    assert summary["job_active"] == 1 and summary["process_coverage_complete"] is False
    with pytest.raises(ObservationFailure):
        boundary.validate_invalid_initialization(summary)
    facade.close()


def test_facade_rechecks_admitted_images_after_owned_execution(monkeypatch, tmp_path):
    facade, _calls = fake_facade(monkeypatch, tmp_path)
    facade.start()
    job = facade.job_factory()
    job.close()
    facade.admitted["console_host"][0].write_bytes(b"tamper")
    with pytest.raises(ObservationFailure):
        facade.finish()
    facade.close()


def test_live_observer_is_preserved_after_start_and_close_failure(tmp_path):
    from types import SimpleNamespace

    class FailedObservation:
        def __init__(self):
            self.resources = []

        def start(self):
            self.resources.append(SimpleNamespace(thread=SimpleNamespace(is_alive=lambda: True)))
            raise ObservationFailure("native_error")

        def close(self):
            raise ObservationFailure("cleanup_incomplete")

    owned = tmp_path / "owned"
    owned.mkdir()
    (owned / "copy").write_bytes(b"still in use")
    observation, resources = FailedObservation(), []
    with pytest.raises(ObservationFailure, match="cleanup_incomplete"):
        try:
            observation.start()
        finally:
            close_observation(observation, resources)
    assert resources == observation.resources and len(resources) == 1
    with pytest.raises(ObservationFailure, match="cleanup_incomplete"):
        remove_owned_fixture(owned, resources)
    assert (owned / "copy").read_bytes() == b"still in use"


@pytest.mark.parametrize("quota_excess", [False, True])
def test_unexpected_process_identity_or_class_quota_aborts_owned_job(
    monkeypatch, tmp_path, quota_excess
):
    observer, _calls, _births, aborted = fake_process_observer(monkeypatch, tmp_path)
    if quota_excess:
        observer.births = {(1, 10): "base_python", (2, 20): "base_python"}
    else:
        observer.admitted = {}

    def notification(_port, message, key, value, _timeout):
        message._obj.value, key._obj.value, value._obj.value = 6, 1, 30
        return True

    observer.api.GetQueuedCompletionStatus = notification
    observer._read()
    assert observer.error == "unexpected_process" and aborted == [True]
    assert observer.births[(30, 100)] == ("base_python" if quota_excess else "unknown")


def notification_buffer(events):
    parts = []
    for index, (name, action) in enumerate(events):
        encoded = name.encode("utf-16-le")
        padded = encoded + b"\0" * (-(12 + len(encoded)) % 4)
        following = 12 + len(padded) if index + 1 < len(events) else 0
        parts.append(struct.pack("<III", following, action, len(encoded)) + padded)
    return b"".join(parts)


def test_final_drain_consumes_extra_queued_event_before_certifying_profile():
    from types import SimpleNamespace

    watcher = object.__new__(TempObserver)
    watcher.stop, watcher.begin = threading.Event(), threading.Event()
    watcher.begin.set()
    watcher.drain_requested, watcher.drained = threading.Event(), threading.Event()
    watcher.io_lock, watcher.closed, watcher.error = threading.Lock(), False, "none"
    watcher.event, watcher.handle, watcher.overlapped = 1, 2, ctypes.c_uint64()
    watcher.buffer = ctypes.create_string_buffer(65_536)
    watcher.counts = dict.fromkeys(
        ("temp_" + kind + "_" + action for kind in TEMP_KINDS for action in ACTIONS.values()), 0
    )
    watcher.total, watcher.lifecycle = 0, boundary.PolicyLifecycle()
    watcher.drain_complete = False
    first_batch_processed, release_batch = threading.Event(), threading.Event()
    observe = watcher.lifecycle.observe

    def paused_observe(name, action):
        observe(name, action)
        if watcher.lifecycle.complete():
            first_batch_processed.set()
            assert release_batch.wait(3)

    watcher.lifecycle.observe = paused_observe
    first = notification_buffer([(name, action) for name in POLICY_NAMES for action in (1, 3, 2)])
    extra = notification_buffer([("__PSScriptPolicyTest_eeeeeeee.eee.ps1", 1)])
    current = [first]
    watcher.buffer.raw = first
    calls, aborted = [], []
    watcher.abort = lambda: aborted.append(True)

    def completed(_handle, _overlap, count, _wait):
        count._obj.value = len(current[0])
        return True

    def reissue(*_args):
        calls.append("reissue")
        current[0] = extra
        watcher.buffer.raw = extra
        return True

    watcher.api = SimpleNamespace(
        WaitForSingleObject=lambda *_: 0,
        GetOverlappedResult=completed,
        ResetEvent=lambda *_: True,
        ReadDirectoryChangesW=reissue,
        CancelIoEx=lambda *_: calls.append("cancel") or True,
        CloseHandle=lambda *_: True,
    )
    watcher.thread = threading.Thread(target=watcher._read)
    watcher.thread.start()
    assert first_batch_processed.wait(3)
    failures = []

    def finish():
        try:
            watcher.finish()
        except BaseException as error:
            failures.append(error)

    closing = threading.Thread(target=finish)
    closing.start()
    assert watcher.drain_requested.wait(3)
    assert not watcher.stop.is_set()  # No cancellation until the buffered batch is drained.
    release_batch.set()
    closing.join(3)
    assert not closing.is_alive() and not failures
    assert watcher.total == 13 and watcher.error == "policy_contract"
    assert aborted == [True] and not watcher.lifecycle.complete()
    assert calls == ["reissue", "cancel"]


@pytest.mark.parametrize("queued_extra", [False, True])
def test_successful_completion_racing_cancel_is_consumed_then_reissued(monkeypatch, queued_extra):
    from types import SimpleNamespace

    watcher = object.__new__(TempObserver)
    watcher.stop, watcher.begin = threading.Event(), threading.Event()
    watcher.begin.set()
    watcher.drain_requested, watcher.drained = threading.Event(), threading.Event()
    watcher.drain_requested.set()
    watcher.drain_complete = False
    watcher.io_lock, watcher.closed, watcher.error = threading.Lock(), False, "none"
    watcher.event, watcher.handle, watcher.overlapped = 1, 2, ctypes.c_uint64()
    watcher.buffer = ctypes.create_string_buffer(65_536)
    watcher.counts = dict.fromkeys(
        ("temp_" + kind + "_" + action for kind in TEMP_KINDS for action in ACTIONS.values()), 0
    )
    watcher.total, watcher.lifecycle = 9, boundary.PolicyLifecycle()
    for name in POLICY_NAMES[:3]:
        for action in ("added", "modified", "removed"):
            watcher.lifecycle.observe(name, action)
            watcher.counts["temp_policy_probe_" + action] += 1
    pending = [notification_buffer([(POLICY_NAMES[3], action) for action in (1, 3, 2)])]
    watcher.buffer.raw = pending[0]
    waits = iter([258, 0, 0] if queued_extra else [258, 0, 258, 0])
    calls, aborted = [], []
    watcher.abort = lambda: aborted.append(True)

    def completed(_handle, _overlap, count, _wait):
        if pending[0] is None:
            calls.append("confirmed_abort")
            return False
        calls.append("successful_bytes")
        count._obj.value = len(pending[0])
        return True

    def reissue(*_args):
        calls.append("reissue")
        pending[0] = (
            notification_buffer([("__PSScriptPolicyTest_eeeeeeee.eee.ps1", 1)])
            if queued_extra
            else None
        )
        if pending[0] is not None:
            watcher.buffer.raw = pending[0]
        return True

    watcher.api = SimpleNamespace(
        WaitForSingleObject=lambda *_: next(waits),
        GetOverlappedResult=completed,
        ResetEvent=lambda *_: True,
        ReadDirectoryChangesW=reissue,
        CancelIoEx=lambda *_: calls.append("cancel") or True,
    )
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 995, raising=False)
    watcher._read()
    assert calls[:3] == ["cancel", "successful_bytes", "reissue"]
    assert watcher.drained.is_set()
    if queued_extra:
        assert calls[3:] == ["successful_bytes"]
        assert watcher.error == "policy_contract" and aborted == [True]
        assert watcher.total == 13 and not watcher.drain_complete
        assert not watcher.lifecycle.complete()
    else:
        assert calls[3:] == ["cancel", "confirmed_abort"]
        assert watcher.error == "none" and not aborted
        assert watcher.total == 12 and watcher.drain_complete and watcher.lifecycle.complete()


def test_late_abort_after_emergency_timeout_cannot_certify_drain(monkeypatch):
    from types import SimpleNamespace

    watcher = object.__new__(TempObserver)
    watcher.stop, watcher.begin = threading.Event(), threading.Event()
    watcher.begin.set()
    watcher.drain_requested, watcher.drained = threading.Event(), threading.Event()
    watcher.drain_requested.set()
    watcher.drain_complete, watcher.error = False, "none"
    watcher.io_lock = threading.Lock()
    watcher.event, watcher.handle, watcher.overlapped = 1, 2, ctypes.c_uint64()
    calls, waits = [], iter([258, 0])

    def completed(_handle, _overlap, _count, _wait):
        # Simulate finish's deadline expiring after the reader issued CancelIoEx,
        # but before its delayed completion is consumed by the reader.
        watcher.error = "incomplete"
        watcher.stop.set()
        return False

    watcher.api = SimpleNamespace(
        WaitForSingleObject=lambda *_: next(waits),
        GetOverlappedResult=completed,
        CancelIoEx=lambda *_: calls.append("cancel") or True,
    )
    watcher.abort = lambda: calls.append("abort")
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 995, raising=False)
    watcher._read()
    assert calls == ["cancel"]
    assert watcher.drained.is_set() and watcher.error == "incomplete"
    assert watcher.drain_complete is False


def test_hosted_capture_overrides_construction_only_and_keeps_real_validation():
    import ast

    tree = ast.parse(Path(__file__).read_text())
    capture = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "CapturedObservation"
    )
    assert [node.name for node in capture.body if isinstance(node, ast.FunctionDef)] == ["__init__"]
    reproduction = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "run_reproduction"
    )
    text = ast.unparse(reproduction)
    assert "alpha.invoke_start(" in text and "boundary.validate_invalid_initialization(" in text
    assert "monkeypatch.setattr(common, 'WindowsJob'" not in text
    assert "owned_observations=owned_observations" in text and "admitted_images=admitted" in text


def single_pair_summary():
    value = valid_initialization_summary()
    value.update(policy_ps1_files=1, policy_psm1_files=1, temp_events=6)
    for action in ("added", "modified", "removed"):
        value["temp_policy_probe_" + action] = 2
    return value


def test_observed_single_pair_completes_exact_lifecycle():
    trace = boundary.PolicyLifecycle()
    for name in POLICY_NAMES[:2]:
        for action in ("added", "modified", "removed"):
            trace.observe(name, action)
    assert trace.complete()
    assert trace.extensions == {"ps1": 1, "psm1": 1} and trace.events == 6


def test_observed_single_pair_summary_is_accepted():
    boundary.validate_invalid_initialization(single_pair_summary())


def test_policy_profiles_are_exact_finite_cases_and_keep_summary_schema():
    assert boundary.POLICY_PROFILES == frozenset({(1, 1, 2, 6), (2, 2, 4, 12)})
    one, two = single_pair_summary(), valid_initialization_summary()
    assert one.keys() == two.keys() == boundary.SUMMARY_FIELDS
    assert one["schema_version"] == two["schema_version"] == "owned-preflight-initialization-v1"
    boundary.validate_invalid_initialization(one)
    boundary.validate_invalid_initialization(two)


@pytest.mark.parametrize(
    "ps1,psm1,events,actions",
    [
        (0, 0, 0, 0),
        (3, 3, 18, 6),
        (1, 0, 3, 1),
        (0, 1, 3, 1),
        (1, 2, 9, 3),
        (2, 1, 9, 3),
        (2, 0, 6, 2),
        (0, 2, 6, 2),
        (1, 1, 12, 4),
        (2, 2, 6, 2),
        (1, 1, 5, 2),
        (2, 2, 13, 4),
    ],
)
def test_policy_union_rejects_every_other_aggregate_profile(ps1, psm1, events, actions):
    value = valid_initialization_summary()
    value.update(policy_ps1_files=ps1, policy_psm1_files=psm1, temp_events=events)
    for action in ("added", "modified", "removed"):
        value["temp_policy_probe_" + action] = actions
    with pytest.raises(ObservationFailure):
        boundary.validate_invalid_initialization(value)


def test_lifecycle_zero_asymmetric_and_three_pairs_never_complete():
    trace = boundary.PolicyLifecycle()
    assert not trace.complete()
    for name in POLICY_NAMES[:3]:
        for action in ("added", "modified", "removed"):
            trace.observe(name, action)
    assert trace.extensions == {"ps1": 2, "psm1": 1} and not trace.complete()
    for action in ("added", "modified", "removed"):
        trace.observe(POLICY_NAMES[3], action)
    assert trace.complete()
    with pytest.raises(ObservationFailure, match="policy_contract"):
        trace.observe("__PSScriptPolicyTest_eeeeeeee.eee.ps1", "added")
    assert trace.invalid and not trace.complete()
    with pytest.raises(ObservationFailure):
        trace.observe("__PSScriptPolicyTest_ffffffff.fff.psm1", "added")


def test_second_pair_must_finish_before_first_pair_can_be_certified_again():
    trace = boundary.PolicyLifecycle()
    for name in POLICY_NAMES[:2]:
        for action in ("added", "modified", "removed"):
            trace.observe(name, action)
    assert trace.complete()
    for name in POLICY_NAMES[2:]:
        trace.observe(name, "added")
        assert not trace.complete()
    trace.observe(POLICY_NAMES[2], "modified")
    trace.observe(POLICY_NAMES[2], "removed")
    assert not trace.complete()
    trace.observe(POLICY_NAMES[3], "modified")
    assert not trace.complete()
    trace.observe(POLICY_NAMES[3], "removed")
    assert trace.complete()


@pytest.mark.parametrize("action", ["added", "modified", "removed", "renamed_from", "renamed_to"])
def test_completed_single_pair_cannot_be_recreated_or_reused(action):
    trace = boundary.PolicyLifecycle()
    for name in POLICY_NAMES[:2]:
        for event in ("added", "modified", "removed"):
            trace.observe(name, event)
    assert trace.complete()
    with pytest.raises(ObservationFailure):
        trace.observe(POLICY_NAMES[0], action)
    assert trace.invalid and not trace.complete()


@pytest.mark.parametrize(
    "change",
    [
        {"temp_policy_probe_removed": 1},
        {"temp_policy_probe_modified": 3},
        {"temp_policy_probe_renamed_to": 1},
        {"temp_other_owned_temp_added": 1},
        {"temp_alpha_session_added": 1},
        {"temp_drain_complete": False},
        {"policy_lifecycles_complete": False},
        {"temporary_root_empty": False},
        {"cleanup_complete": False},
        {"process_unknown": 1},
        {"job_total": 4},
        {"job_active": 1},
        {"error": "access_denied"},
        {"policy_ps1_files": True},
    ],
)
def test_single_pair_cannot_bypass_any_other_initialization_gate(change):
    value = single_pair_summary()
    value.update(change)
    with pytest.raises(ObservationFailure):
        boundary.validate_invalid_initialization(value)
