"""Portable contract tests only; synthetic records never establish native evidence."""

from __future__ import annotations

import ctypes
import importlib.util
import io
import json
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
    assert witness.FacadeProcessObserver._read is boundary.ProcessObserver._read
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


def process_observer(monkeypatch, tmp_path, *, kind="gst_launch", belongs=True, denied=0):
    calls = []
    path = tmp_path / "child.exe"
    path.write_bytes(b"admitted")
    birth = [100]

    def opened(access, inherit, pid):
        calls.append(("open", access, inherit, pid))
        return None if denied else 123

    def membership(handle, job, output):
        calls.append(("membership", job))
        output._obj.value = belongs
        return True

    def times(handle, created, *rest):
        calls.append(("birth",))
        created._obj.value = birth[0]
        return True

    def image(handle, flags, buffer, size):
        calls.append(("image",))
        buffer.value = str(path)
        return True

    observer = object.__new__(witness.FacadeProcessObserver)
    observer._initialize_evidence()
    observer.api = SimpleNamespace(
        OpenProcess=opened,
        IsProcessInJob=membership,
        GetProcessTimes=times,
        QueryFullProcessImageNameW=image,
        CloseHandle=lambda handle: calls.append(("close", handle)) or True,
    )
    observer.job_query_handle = 456
    observer.admitted = {kind: (path, common.file_hash(path))}
    observer.births, observer.handles = {}, []
    monkeypatch.setattr(ctypes, "get_last_error", lambda: denied, raising=False)
    return observer, calls, birth, path


def test_owned_birth_must_match_exact_job_before_image_read(monkeypatch, tmp_path):
    observer, calls, birth, path = process_observer(monkeypatch, tmp_path)
    observer._observe(10)
    assert [call[0] for call in calls[:4]] == ["open", "membership", "birth", "image"]
    assert calls[0][1:3] == (0x1000, False)
    observer._observe(10)
    assert len(observer.births) == 1
    birth[0] += 1
    observer._observe(10)
    assert len(observer.births) == 2
    path.write_bytes(b"changed")
    birth[0] += 1
    with pytest.raises(boundary.ObservationFailure):
        observer._observe(10)


def test_foreign_pid_never_exposes_image_or_birth(monkeypatch, tmp_path):
    observer, calls, _, _ = process_observer(monkeypatch, tmp_path, belongs=False)
    with pytest.raises(boundary.ObservationFailure, match="ownership_unproven"):
        observer._observe(10)
    assert [call[0] for call in calls] == ["open", "membership", "close"]


@pytest.mark.parametrize("denied,error", [(5, "access_denied"), (87, "process_unavailable")])
def test_short_lived_or_denied_birth_cannot_be_inferred(monkeypatch, tmp_path, denied, error):
    observer, calls, _, _ = process_observer(monkeypatch, tmp_path, denied=denied)
    with pytest.raises(boundary.ObservationFailure, match=error):
        observer._observe(10)
    assert calls == [("open", 0x1000, False, 10)]
    assert not observer.births


def test_unknown_image_and_birth_limit_refuse_without_reclassification(monkeypatch, tmp_path):
    observer, calls, birth, _ = process_observer(monkeypatch, tmp_path, kind="unknown")
    with pytest.raises(boundary.ObservationFailure, match="unexpected_process"):
        observer._observe(10)
    assert not observer.births
    observer, calls, birth, _ = process_observer(monkeypatch, tmp_path)
    observer.births = {(index, index): "gst_launch" for index in range(witness.MAX_PROCESSES)}
    with pytest.raises(boundary.ObservationFailure, match="limit"):
        observer._observe(100)


def test_native_reader_aborts_owned_job_on_missing_birth(monkeypatch, tmp_path):
    observer, calls, _, _ = process_observer(monkeypatch, tmp_path, denied=87)
    aborted = []
    observer.job = SimpleNamespace(abort=lambda: aborted.append(True))
    observer.error, observer.stop, observer.port = "none", threading.Event(), 1
    observer._release = lambda: None

    def event(port, message, key, value, timeout):
        message._obj.value, key._obj.value, value._obj.value = 6, 1, 10
        return True

    observer.api.GetQueuedCompletionStatus = event
    observer._read()
    assert observer.error == "process_unavailable" and aborted == [True]


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
            resources.append(self)

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


def test_aborted_observer_never_assigns_job_or_opens_gate(monkeypatch, tmp_path):
    actual, calls = fake_observation(monkeypatch, tmp_path)
    actual.start()
    job = actual.job_factory()
    actual.watcher.error = "unexpected_temp"
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


def test_missing_birth_retains_attempt_and_native_phase(monkeypatch, tmp_path):
    observer, calls, _, _ = process_observer(monkeypatch, tmp_path, denied=87)
    # Production initializes before starting the inherited reader. This test's
    # synthetic observer has no constructor/native thread.
    initialize = getattr(observer, "_initialize_evidence", lambda: None)
    initialize()
    observer.births[(5, 50)] = "base_python"
    with pytest.raises(boundary.ObservationFailure, match="process_unavailable"):
        observer._observe(10)
    assert observer.attempted_birth_ordinal == 2
    assert observer.failure_phase == "open_process"
    assert observer.primary_error == "process_unavailable"
    assert list(observer.births.values()) == ["base_python"]
    assert calls == [("open", 0x1000, False, 10)]


def diagnostic_observation(tmp_path):
    """Synthetic stopped collectors, never a native inventory qualification."""
    actual = witness.FacadeObservation(temp_root=tmp_path, admitted_images={}, mode="run_1")
    quiet = SimpleNamespace(is_alive=lambda: False)
    observer = object.__new__(witness.FacadeProcessObserver)
    observer._initialize_evidence()
    observer.thread, observer.error, observer.release_complete = quiet, "process_unavailable", True
    observer.attempted_birth_ordinal = 3
    observer.notification_count = 3
    observer.phase = observer.failure_phase = "open_process"
    observer.primary_error = "process_unavailable"
    observer.births = {(123456, 999999): "base_python", (234567, 888888): "powershell"}
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
    actual.resources = [observer, actual.watcher]
    actual.collectors = [actual.stdout_summary, actual.owned.stderr_summary]
    return actual


def test_failure_snapshot_is_distinct_source_free_prefix_and_cached_exits(tmp_path):
    actual = diagnostic_observation(tmp_path)
    value = witness.failure_evidence(actual, "run_1")
    witness.validate_failure_evidence(value)
    assert value["snapshot_state"] == "quiescent"
    assert value["observer"]["admitted_classes"] == ["base_python", "powershell"]
    assert value["observer"]["admitted_counts"]["unknown"] == 0
    assert value["observer"]["attempted_birth_ordinal"] == 3
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


@pytest.mark.parametrize("slot", ["process", "temp", "stdout", "stderr"])
def test_live_reader_makes_entire_snapshot_unavailable_without_accessing_maps(tmp_path, slot):
    actual = diagnostic_observation(tmp_path)
    target = {
        "process": actual.jobs[0].observer,
        "temp": actual.watcher,
        "stdout": actual.stdout_summary,
        "stderr": actual.owned.stderr_summary,
    }[slot]
    target.thread = SimpleNamespace(is_alive=lambda: True)

    # A racing map must not be touched even if other readers are already stopped.
    class MutableBirths:
        def values(self):
            pytest.fail("live snapshot accessed mutable birth data")

    actual.jobs[0].observer.births = MutableBirths()
    value = witness.failure_evidence(actual, "run_1")
    witness.validate_failure_evidence(value)
    assert value["snapshot_state"] == "not_quiescent"
    assert all(value[key] is None for key in witness.EVIDENCE_PARTS)


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
def test_owned_abort_records_only_stopped_prior_exit_without_polling(monkeypatch, tmp_path, prior):
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
    job.observer.thread = threading.current_thread()
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
    job.observer.thread = SimpleNamespace(is_alive=lambda: False)
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


@pytest.mark.parametrize("denied,error", [(5, "access_denied"), (87, "process_unavailable")])
def test_reader_keeps_single_query_refusal_phase_and_abort(monkeypatch, tmp_path, denied, error):
    observer, calls, _, _ = process_observer(monkeypatch, tmp_path, denied=denied)
    aborted = []
    observer.job = SimpleNamespace(abort=lambda: aborted.append(True))
    observer.error, observer.stop, observer.port = "none", threading.Event(), 1
    observer._release = lambda: None

    def event(port, message, key, value, timeout):
        assert timeout == 100
        message._obj.value, key._obj.value, value._obj.value = 6, 1, 10
        return True

    observer.api.GetQueuedCompletionStatus = event
    observer._read()
    assert observer.error == observer.primary_error == error
    assert observer.failure_phase == "open_process"
    assert observer.attempted_birth_ordinal == observer.notification_count == 1
    assert not observer.births and aborted == [True]
    assert calls == [("open", 0x1000, False, 10)]


@pytest.mark.parametrize(
    "case,phase,error",
    [
        ("foreign", "job_membership", "ownership_unproven"),
        ("unknown", "image_admission", "unexpected_process"),
        ("overflow", "birth_limit", "limit"),
        ("image-denied", "image_query", "access_denied"),
        ("times-denied", "process_times", "access_denied"),
    ],
    ids=["foreign", "unknown", "overflow", "image-denied", "times-denied"],
)
def test_failed_birth_records_only_admitted_prefix(monkeypatch, tmp_path, case, phase, error):
    observer, calls, _, _ = process_observer(
        monkeypatch,
        tmp_path,
        kind="unknown" if case == "unknown" else "gst_launch",
        belongs=case != "foreign",
    )
    if case == "overflow":
        observer.births = {(i, i): "gst_launch" for i in range(witness.MAX_PROCESSES)}
    if case in {"image-denied", "times-denied"}:
        name = "QueryFullProcessImageNameW" if case == "image-denied" else "GetProcessTimes"
        setattr(observer.api, name, lambda *args: False)
        monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
    before = dict(observer.births)
    with pytest.raises(boundary.ObservationFailure, match=error):
        observer._observe(100)
    assert observer.primary_error == error and observer.failure_phase == phase
    assert observer.births == before
    assert observer.attempted_birth_ordinal == len(before) + 1
    assert len([call for call in calls if call[0] == "open"]) == 1


def test_duplicate_notification_and_pid_reuse_keep_distinct_admitted_ordinals(
    monkeypatch, tmp_path
):
    observer, calls, birth, _ = process_observer(monkeypatch, tmp_path)
    observer._observe(10)
    observer._observe(10)
    assert observer.duplicate_count == 1 and observer.notification_count == 2
    assert observer.attempted_birth_ordinal == 2 and len(observer.births) == 1
    assert observer.phase == "duplicate_birth"
    birth[0] += 1
    observer._observe(10)
    assert observer.attempted_birth_ordinal == 2 and len(observer.births) == 2
    assert observer.phase == "admitted_birth"
    assert len([call for call in calls if call[0] == "image"]) == 2
    for _ in range(witness.MAX_EVENTS + 1):
        observer._observe(10)
    assert observer.duplicate_count == observer.notification_count == witness.MAX_EVENTS
    assert len(observer.births) == 2  # Capping telemetry cannot change admission.


def test_close_error_cannot_erase_primary_native_phase(monkeypatch, tmp_path):
    observer, _, _, _ = process_observer(monkeypatch, tmp_path, belongs=False)
    observer.api.CloseHandle = lambda handle: False
    with pytest.raises(boundary.ObservationFailure, match="cleanup_incomplete"):
        observer._observe(10)
    assert observer.primary_error == "ownership_unproven"
    assert observer.failure_phase == "job_membership"
    assert observer.cleanup_error == "cleanup_incomplete"


def test_diagnostic_state_initialized_before_inherited_reader_can_start(monkeypatch):
    def constructor(self, *args):
        assert self.phase == self.failure_phase == "not_started"
        assert self.primary_error == self.cleanup_error == "none"
        assert self.attempted_birth_ordinal == self.notification_count == self.duplicate_count == 0

    monkeypatch.setattr(boundary.ProcessObserver, "__init__", constructor)
    witness.FacadeProcessObserver(None, None, [])


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
        ("observer", "attempted_birth_ordinal", True),
        ("observer", "attempted_birth_ordinal", 34),
        ("observer", "notifications_capped", 257),
        ("observer", "duplicates_capped", -1),
        ("observer", "phase", "private-path"),
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
    "key", ["process_stopped", "temp_stopped", "stdout_stopped", "stderr_stopped"]
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
