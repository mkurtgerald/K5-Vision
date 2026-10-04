"""Portable contracts, never a substitute for the Windows Start-script witness."""

from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "alpha_witness", ROOT / "scripts/installed_alpha_launcher_witness.py"
)
assert SPEC is not None and SPEC.loader is not None
witness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(witness)


def expected() -> dict[str, str]:
    return {
        "revision": "a" * 40,
        "run_nonce": "b" * 32,
        **{name: witness.common.digest(name) for name in witness.IDENTITIES},
    }


def receipt() -> dict[str, object]:
    value = witness.new_receipt(expected())
    value.update(
        completed=True,
        cleanup_complete=True,
        invalid_config_refused=True,
        invalid_config_no_session=True,
        invalid_config_no_media=True,
        stage="complete",
        failure_code="none",
    )
    for attempt in (1, 2):
        for name, number in {
            "delivered_frames": 225,
            "presentations": 226,
            "analytics_provider_submissions": 40,
            "analytics_provider_completions": 39,
            "analytics_failures": 0,
        }.items():
            value[f"run_{attempt}_{name}"] = number
        for name in ("completed", "analytics_enabled", "cleanup_complete"):
            value[f"run_{attempt}_{name}"] = True
    return value


def test_valid_receipt_and_separate_scope() -> None:
    value = receipt()
    witness.validate_receipt(value, expected())
    assert value["acceptance_scope"] == "installed-layout-start-script-engineering-only"
    assert value["fixture"] == "generated-ball"
    assert value["person_box_acceptance"] is False
    assert not any("rendered_boxes" in key for key in value)


@pytest.mark.parametrize("field", sorted(witness.IDENTITIES | {"revision", "run_nonce"}))
def test_forged_or_stale_identity_fails(field: str) -> None:
    value = receipt()
    value[field] = "c" * len(str(value[field]))
    with pytest.raises(witness.common.WitnessError):
        witness.validate_receipt(value, expected())


@pytest.mark.parametrize(
    "field,value",
    [
        ("completed", 1),
        ("cleanup_complete", False),
        ("invalid_config_refused", False),
        ("invalid_config_no_session", False),
        ("invalid_config_no_media", False),
        ("person_box_acceptance", True),
        ("acceptance_scope", "installer-upgrade"),
        ("run_1_delivered_frames", 224),
        ("run_2_delivered_frames", 226),
        ("run_1_presentations", True),
        ("run_2_presentations", "225"),
        ("run_2_analytics_provider_completions", 0),
        ("run_1_analytics_failures", 1),
        ("run_2_cleanup_complete", False),
        ("model_identity_sha256", {"secret": "path"}),
        ("stage", "private path"),
    ],
)
def test_strict_scalar_rejection(field: str, value: object) -> None:
    data = receipt()
    data[field] = value
    with pytest.raises(witness.common.WitnessError):
        witness.validate_receipt(data, expected())


def test_receipt_extra_field_and_missing_expectation_fail() -> None:
    data = receipt()
    data["raw_output"] = "not retained"
    with pytest.raises(witness.common.WitnessError):
        witness.validate_receipt(data, expected())
    expectations = expected()
    expectations.pop("model_identity_sha256")
    with pytest.raises(witness.common.WitnessError):
        witness.validate_receipt(receipt(), expectations)


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', b'{"x":NaN}', b"[]", b"x" * 16385])
def test_bounded_json(raw: bytes) -> None:
    with pytest.raises(witness.common.WitnessError):
        witness.common.parse_json(raw)


def test_validator_cli_requires_external_expectations(tmp_path: Path) -> None:
    output, identity = tmp_path / witness.RECEIPT_NAME, tmp_path / "expected.json"
    output.write_bytes(witness.common.canonical(receipt()))
    identity.write_bytes(witness.common.canonical(expected()))
    args = [
        sys.executable,
        "-I",
        "-B",
        str(SPEC.origin),
        "--validate-receipt",
        "--output",
        str(output),
        "--expectations",
        str(identity),
    ]
    child = subprocess.run(args, capture_output=True, timeout=10)
    assert child.returncode == 0
    assert b"validation passed" in child.stdout
    data = receipt()
    data["run_nonce"] = "d" * 32
    output.write_bytes(witness.common.canonical(data))
    child = subprocess.run(args, capture_output=True, timeout=10)
    assert child.returncode == 1
    assert b"dddd" not in child.stdout + child.stderr


def success_output() -> bytes:
    return (
        b"K5 analytics configuration admitted; live provider acceptance is pending.\n"
        b"Starting K5 Vision Alpha local synthetic operator test on http://127.0.0.1:8000\n"
        b"K5 analytics PASS: submissions=40, completions=39, failures=0\n"
        b"K5 operator PASS: frames=225, presentations=226\n"
        b"Exiting after one bounded alpha acceptance run.\n"
    )


def test_output_retains_only_scalar_markers() -> None:
    summary = witness.LaunchSummary(io.BytesIO(success_output()))
    summary.finish()
    result = summary.result()
    assert result["delivered_frames"] == 225
    assert result["analytics_provider_completions"] == 39
    assert "rendered_boxes" not in result
    assert not any(isinstance(value, bytes) for value in vars(summary).values())


@pytest.mark.parametrize(
    "raw",
    [
        success_output() * 2,
        success_output().replace(b"frames=225", b"frames=226"),
        success_output().replace(b"completions=39", b"completions=0"),
        success_output().replace(b"failures=0", b"failures=1"),
        success_output().replace(b"local synthetic", b"public RTSP"),
        success_output().replace(b"submissions=40", b"submissions=1"),
        success_output() + b"X" * 65537,
    ],
)
def test_invalid_output_fails_without_exposing_output(raw: bytes) -> None:
    summary = witness.LaunchSummary(io.BytesIO(raw))
    summary.finish()
    with pytest.raises(witness.common.WitnessError):
        summary.result()


def test_actual_unmodified_start_command_no_override_or_bypass(tmp_path: Path) -> None:
    command = witness.start_command(
        tmp_path / "powershell.exe", tmp_path / "envelope.ps1", tmp_path / "installed", 8011
    )
    assert command == [
        str(tmp_path / "powershell.exe"),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(tmp_path / "envelope.ps1"),
        "-Start",
        str(tmp_path / "installed/Start-K5VisionAlpha.ps1"),
        "-Port",
        "8011",
    ]
    assert "& $Start -Port $Port -ExitAfterPublicTest" in witness.ENVELOPE
    assert "AnalyticsPreflightOnly" not in witness.ENVELOPE
    assert "PublicRtspSource" not in witness.ENVELOPE
    assert "ExecutionPolicy" not in " ".join(command)


def test_raw_script_bytes_and_records(tmp_path: Path) -> None:
    source, installed = tmp_path / "source", tmp_path / "installed"
    (source / "scripts/windows-alpha").mkdir(parents=True)
    installed.mkdir()
    for name in witness.SCRIPT_NAMES:
        (source / "scripts/windows-alpha" / name).write_bytes(b"param()\n# raw LF \xc3\xa9\n")
    identities = witness.install_scripts(source, installed, "a" * 40)
    witness.verify_layout(installed, {**identities, "revision": "a" * 40})
    assert (installed / "Start-K5VisionAlpha.ps1").read_bytes() == b"param()\n# raw LF \xc3\xa9\n"
    (installed / "Start-K5VisionAlpha.ps1").write_bytes(b"mutated")
    with pytest.raises(witness.common.WitnessError):
        witness.verify_layout(installed, {**identities, "revision": "a" * 40})


def native_cache(root: Path) -> None:
    gst, mtx = witness.native_roots(root)
    (gst / "bin").mkdir(parents=True)
    mtx.mkdir(parents=True)
    for name in ("gst-launch-1.0.exe", "gst-inspect-1.0.exe", "gstreamer-1.0-0.dll"):
        (gst / "bin" / name).write_bytes(name.encode())
    (gst / "k5-installer.sha256").write_text(witness.common.GSTREAMER_INSTALLER)
    (mtx / "mediamtx.exe").write_bytes(b"reviewed executable")
    (mtx / "k5-archive.sha256").write_text(witness.common.MEDIA_MTX_ARCHIVE)
    (mtx / "k5-exe.sha256").write_text(witness.common.file_hash(mtx / "mediamtx.exe"))


def test_native_cache_copied_and_missing_cache_never_provisioned(tmp_path: Path) -> None:
    origin, owned = tmp_path / "existing", tmp_path / "owned"
    native_cache(origin)
    identity = witness.common.digest(witness.native_manifest(origin))
    witness.copy_native_cache(origin, owned, identity)
    assert witness.common.digest(witness.native_manifest(owned)) == identity
    assert witness.common.digest(witness.native_manifest(origin)) == identity
    with pytest.raises((witness.common.WitnessError, OSError)):
        witness.copy_native_cache(tmp_path / "missing", tmp_path / "not-created", identity)
    assert not (tmp_path / "not-created").exists()


@pytest.mark.parametrize(
    "mutation", ["exe", "archive", "installer", "extra", "missing", "identity"]
)
def test_native_cache_wrong_identity_fails(tmp_path: Path, mutation: str) -> None:
    native_cache(tmp_path)
    identity = witness.common.digest(witness.native_manifest(tmp_path))
    gst, mtx = witness.native_roots(tmp_path)
    if mutation == "exe":
        (mtx / "mediamtx.exe").write_bytes(b"changed")
    elif mutation == "archive":
        (mtx / "k5-archive.sha256").write_text("0" * 64)
    elif mutation == "installer":
        (gst / "k5-installer.sha256").write_text("0" * 64)
    elif mutation == "extra":
        (gst / "extra.dll").write_bytes(b"unreviewed")
    elif mutation == "missing":
        (gst / "bin/gst-launch-1.0.exe").unlink()
    else:
        identity = "c" * 64
    with pytest.raises((witness.common.WitnessError, OSError)):
        witness.copy_native_cache(tmp_path, tmp_path / "owned", identity)
    assert not (tmp_path / "owned").exists()


def test_tree_and_source_payload_reject_alias_and_track_extra_files(tmp_path: Path) -> None:
    source = tmp_path / "src/k5vision"
    source.mkdir(parents=True)
    (source / "analytics_config.py").write_bytes(b"first")
    first = witness.source_payload(tmp_path)
    (source / "extra.py").write_bytes(b"second")
    assert witness.source_payload(tmp_path) != first
    try:
        (source / "alias.py").symlink_to(source / "extra.py")
    except OSError:
        pytest.skip("Symbolic-link fixture is unavailable on this host")
    with pytest.raises(witness.common.WitnessError):
        witness.source_payload(tmp_path)


def test_child_environment_has_no_account_credential_or_source_state(tmp_path: Path) -> None:
    env = witness.clean_environment(
        {
            "SYSTEMROOT": str(tmp_path / "Windows"),
            "OS": "Windows_NT",
            "PATH": "untrusted-code",
            "GITHUB_TOKEN": "secret",
            "AWS_ACCESS_KEY_ID": "secret",
            "K5_USER_DB_PATH": "real-database",
            "PYTHONPATH": "checkout",
            "HTTP_PROXY": "proxy",
            "LOCALAPPDATA": "shared-cache",
            "USERPROFILE": "real-profile",
        },
        tmp_path,
    )
    assert not any("secret" in value or "real-" in value for value in env.values())
    assert env["PYTHONPATH"] == ""
    assert "checkout" not in env.values()
    assert env["LOCALAPPDATA"] == str(tmp_path / "profile/local")
    assert "untrusted-code" not in env["PATH"]
    assert env["PIP_NO_INDEX"] == "1"


def test_job_accounting_uses_process_not_page_fault_counter(monkeypatch) -> None:
    import ctypes
    from types import SimpleNamespace

    def query(_handle, kind, address, size, _returned):
        assert kind == 1 and size >= 48
        values = ctypes.cast(address, ctypes.POINTER(ctypes.c_uint32))
        values[8] = 999  # TotalPageFaultCount at offset 32, not process count.
        values[9] = 4  # TotalProcesses at offset 36.
        values[10] = 0  # ActiveProcesses at offset 40.
        return True

    job = object.__new__(witness.common.WindowsJob)
    job.handle = 1
    job.api = SimpleNamespace(QueryInformationJobObject=query)
    owned = SimpleNamespace(job=job)
    assert witness.job_accounting(owned) == (4, 0)


def test_invalid_then_two_successful_installed_launches(monkeypatch, tmp_path: Path) -> None:
    expected_identities = expected()
    expected_identities["native_cache_sha256"] = witness.common.digest({"cache": "admitted"})
    document = witness.new_receipt(expected_identities)
    calls = []
    monkeypatch.setattr(witness, "verify_layout", lambda *_: None)
    monkeypatch.setattr(witness, "native_manifest", lambda *_: {"cache": "admitted"})
    monkeypatch.setattr(witness, "require_ports_free", lambda port: calls.append(("ports", port)))
    monkeypatch.setattr(witness.common, "free_port", lambda: 8011)
    monkeypatch.setattr(witness, "admitted_preflight_images", lambda *_: {"explicit": "images"})

    def invoke(command, **kwargs):
        assert "Start-K5VisionAlpha.ps1" in " ".join(command)
        calls.append(("start", kwargs.get("invalid", False)))
        if kwargs.get("invalid"):
            assert kwargs["admitted_images"] == {"explicit": "images"}
            assert type(kwargs["owned_observations"]) is list
            assert (
                Path(kwargs["env"]["K5_ANALYTICS_CONFIG"]).read_bytes().find(b"invalid-selected")
                >= 0
            )
            return dict.fromkeys(
                ("invalid_config_refused", "invalid_config_no_session", "invalid_config_no_media"),
                True,
            )
        assert "admitted_images" not in kwargs and "owned_observations" not in kwargs
        assert kwargs["env"]["K5_ANALYTICS_CONFIG"] == "validated-config"
        return {
            name: receipt()[f"run_1_{name}"] for name in witness.COUNTERS | witness.RUN_BOOLEANS
        }

    monkeypatch.setattr(witness, "invoke_start", invoke)
    witness.launch_sequence(
        work=tmp_path,
        installed=tmp_path / "installed",
        env={"LOCALAPPDATA": str(tmp_path / "local"), "K5_ANALYTICS_CONFIG": "validated-config"},
        powershell=tmp_path / "powershell.exe",
        expected=expected_identities,
        document=document,
        owned_observations=[],
    )
    assert [call for call in calls if call[0] == "start"] == [
        ("start", True),
        ("start", False),
        ("start", False),
    ]
    assert len([call for call in calls if call[0] == "ports"]) == 4
    assert document["invalid_config_no_session"] is True
    assert document["run_2_delivered_frames"] == 225


def invoke_fixture(
    monkeypatch,
    tmp_path,
    *,
    invalid=False,
    total=5,
    active=0,
    unchanged=True,
    exit_code=None,
    output=None,
    leave_session=False,
    cleanup_failure=False,
    detail_override=None,
    profile_overrides=None,
):
    from types import SimpleNamespace

    state = {"closed": False, "observation_closed": False}
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    code = 23 if invalid else 0
    if exit_code is not None:
        code = exit_code

    class Owned:
        def __init__(self, *_args, **_kwargs):
            self.operation = "launch_1"
            if invalid:
                self.job = _kwargs["job_factory"]()
            self.process = SimpleNamespace(
                stdout=io.BytesIO(
                    output
                    if output is not None
                    else REFUSAL_OUTPUT
                    if invalid
                    else success_output()
                ),
                returncode=code,
            )

        def wait(self, seconds):
            assert seconds <= 150
            if leave_session:
                (sessions / "K5VisionAlpha-leftover").mkdir()
            if code:
                detail = witness.common.diagnostic(
                    "probe_admission",
                    outcome="child_failed",
                    gate_state="exited",
                    child_exit_code=code,
                    relay_exit_code=code,
                )
                detail.update(detail_override or {})
                raise witness.common.WitnessError("child_failed", detail)

        def close(self):
            state["closed"] = True
            if invalid:
                self.job.observation_open = False
            if cleanup_failure:
                raise witness.common.WitnessError("cleanup_incomplete")

    class Observation:
        def __init__(self, _common, *, temp_root, admitted_images):
            assert temp_root == sessions and admitted_images == {}
            self.started = False
            self.jobs = []
            self.resources = []

        def start(self):
            self.started = True

        def job_factory(self):
            job = SimpleNamespace(observation_open=True)
            self.jobs.append(job)
            return job

        def finish(self):
            assert state["closed"] and not self.jobs[0].observation_open
            value = witness.boundary.empty_summary()
            kinds = ["base_python", "powershell", "venv_python", "console_host", "base_python"]
            kinds = (kinds + ["base_python"] * total)[:total]
            value.update(
                job_total=total,
                job_active=active,
                distinct_births=total,
                process_coverage_complete=active == 0,
                cleanup_complete=True,
                temporary_root_empty=not any(sessions.iterdir()),
                policy_lifecycles_complete=unchanged,
                temp_drain_complete=True,
                policy_ps1_files=2,
                policy_psm1_files=2,
                temp_events=12,
            )
            for index, kind in enumerate(kinds, 1):
                value[f"birth_{index}_class"] = kind
            for kind in witness.boundary.PROCESS_KINDS:
                value["process_" + kind] = kinds.count(kind)
            for action in ("added", "modified", "removed"):
                value["temp_policy_probe_" + action] = 4
            value.update(profile_overrides or {})
            self.summary = value
            return value

        def close(self):
            state["observation_closed"] = True

        def quiescent(self):
            return state["observation_closed"]

    monkeypatch.setattr(witness.common, "OwnedProcess", Owned)
    monkeypatch.setattr(witness.boundary, "PreflightObservation", Observation)
    monkeypatch.setattr(witness, "job_accounting", lambda _: (total, active))
    return state, sessions


REFUSAL_OUTPUT = b"K5_ALPHA_EXPECTED_CONFIG_REFUSAL\n"


def test_invalid_config_proves_no_transient_session_or_native_child(
    monkeypatch, tmp_path: Path
) -> None:
    state, sessions = invoke_fixture(monkeypatch, tmp_path, invalid=True)
    result = witness.invoke_start(
        ["real Start"],
        work=tmp_path,
        env={"TEMP": str(sessions)},
        operation="probe_admission",
        invalid=True,
        admitted_images={},
        owned_observations=[],
    )
    assert result == dict.fromkeys(
        ("invalid_config_refused", "invalid_config_no_session", "invalid_config_no_media"), True
    )
    assert state == {"closed": True, "observation_closed": True}


@pytest.mark.parametrize(
    "overrides",
    [
        {"total": 6},
        {"active": 1},
        {"unchanged": False},
        {"exit_code": 0},
        {"exit_code": 24},
        {"output": b""},
        {"output": REFUSAL_OUTPUT + success_output()},
        {"leave_session": True},
        {"cleanup_failure": True},
    ],
)
def test_invalid_config_failure_closes_every_owned_resource(
    monkeypatch, tmp_path, overrides
) -> None:
    state, sessions = invoke_fixture(monkeypatch, tmp_path, invalid=True, **overrides)
    with pytest.raises(witness.common.WitnessError):
        witness.invoke_start(
            ["real Start"],
            work=tmp_path,
            env={"TEMP": str(sessions)},
            operation="probe_admission",
            invalid=True,
            admitted_images={},
            owned_observations=[],
        )
    assert state["closed"] and state["observation_closed"]


@pytest.mark.parametrize(
    "overrides",
    [{"active": 1}, {"exit_code": 24}, {"leave_session": True}, {"cleanup_failure": True}],
)
def test_success_cannot_hide_cleanup_or_child_failure(monkeypatch, tmp_path, overrides) -> None:
    state, sessions = invoke_fixture(monkeypatch, tmp_path, **overrides)
    with pytest.raises(witness.common.WitnessError):
        witness.invoke_start(
            ["real Start"], work=tmp_path, env={"TEMP": str(sessions)}, operation="launch_1"
        )
    assert state["closed"]


def test_both_required_synthetic_udp_ports_are_admitted(monkeypatch) -> None:
    checks = []
    monkeypatch.setattr(
        witness.common, "require_free_port", lambda port: checks.append(("tcp", port))
    )

    class Listener:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def bind(self, address):
            checks.append(("udp", address[1]))

    monkeypatch.setattr(witness.socket, "socket", lambda *_: Listener())
    witness.require_ports_free(8011)
    assert checks == [("tcp", 8011), ("tcp", 8554), ("udp", 18000), ("udp", 18001)]


def test_directory_guard_remembers_transient_change_and_closes(monkeypatch, tmp_path) -> None:
    from types import SimpleNamespace

    state = {"status": 258, "closed": False, "flags": 0}

    class Function:
        def __init__(self, call):
            self.call = call

        def __call__(self, *args):
            return self.call(*args)

    def begin(path, subtree, flags):
        assert path == str(tmp_path) and subtree is True
        state["flags"] = flags
        return 123

    def close(handle):
        assert handle == 123
        state["closed"] = True
        return True

    api = SimpleNamespace(
        FindFirstChangeNotificationW=Function(begin),
        WaitForSingleObject=Function(lambda *_: state["status"]),
        FindCloseChangeNotification=Function(close),
    )
    monkeypatch.setattr(witness, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(witness.ctypes, "WinDLL", lambda *_args, **_kwargs: api, raising=False)
    guard = witness.DirectoryChangeGuard(tmp_path)
    assert guard.unchanged()
    # A created-then-deleted directory leaves the manual-reset notification set.
    state["status"] = 0
    assert not guard.unchanged()
    state["status"] = 0xFFFFFFFF
    with pytest.raises(witness.common.WitnessError):
        guard.unchanged()
    guard.close()
    assert state["closed"] and state["flags"] & 2


def test_platform_refuses_non_windows_and_venv_controllers(monkeypatch) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(witness, "os", SimpleNamespace(name="posix", environ={}))
    with pytest.raises(witness.common.WitnessError):
        witness.admit_platform()
    monkeypatch.setattr(
        witness, "os", SimpleNamespace(name="nt", environ={"PROCESSOR_ARCHITECTURE": "AMD64"})
    )
    monkeypatch.setattr(
        witness,
        "sys",
        SimpleNamespace(
            version_info=(3, 12),
            prefix="venv",
            base_prefix="base",
            flags=SimpleNamespace(isolated=1, dont_write_bytecode=1),
        ),
    )
    with pytest.raises(witness.common.WitnessError):
        witness.admit_platform()
    monkeypatch.setattr(
        witness,
        "sys",
        SimpleNamespace(
            version_info=(3, 12),
            prefix="base",
            base_prefix="base",
            flags=SimpleNamespace(isolated=1, dont_write_bytecode=1),
        ),
    )
    witness.admit_platform()


@pytest.mark.parametrize(
    "field", ["runtime_identity_sha256", "model_identity_sha256", "seed_identity_sha256"]
)
def test_isolated_probe_rejects_wrong_or_changed_identity(monkeypatch, tmp_path, field) -> None:
    expectations = expected()
    output = tmp_path / "probe-output.json"
    output.write_text("stale")
    result = {
        name: expectations[name]
        for name in ("runtime_identity_sha256", "model_identity_sha256", "seed_identity_sha256")
    }
    result[field] = "c" * 64

    def run(_args, **kwargs):
        assert not output.exists()
        assert kwargs["operation"] == "probe_after"
        assert kwargs["seconds"] == 120
        output.write_bytes(witness.common.canonical(result))

    monkeypatch.setattr(witness.common, "run", run)
    with pytest.raises(witness.common.WitnessError):
        witness.probe(["isolated installed probe"], tmp_path, {}, expectations, after=True)


def prepare_fixture(monkeypatch, tmp_path):
    import shutil
    import zipfile
    from types import SimpleNamespace

    candidate = tmp_path / "candidate"
    (candidate / "scripts/windows-alpha").mkdir(parents=True)
    (candidate / "src/k5vision/data").mkdir(parents=True)
    for name in (
        "installed_alpha_launcher_witness.py",
        "installed_analytics_witness.py",
        "windows_owned_preflight.py",
    ):
        shutil.copyfile(ROOT / "scripts" / name, candidate / "scripts" / name)
    for name in witness.SCRIPT_NAMES:
        shutil.copyfile(
            ROOT / "scripts/windows-alpha" / name, candidate / "scripts/windows-alpha" / name
        )
    (candidate / "scripts/windows-alpha/runtime-requirements.txt").write_text("base==1.0\n")
    (candidate / "scripts/build_analytics_runtime_wheel.py").write_text("# admitted builder")
    (candidate / "src/k5vision/analytics_config.py").write_text("# package")
    (candidate / "src/k5vision/data/analytics-runtime-manifest.json").write_text("{}")
    local, wheelhouse = tmp_path / "local", tmp_path / "wheelhouse"
    native_cache(local)
    wheelhouse.mkdir()
    (wheelhouse / "dependency.whl").write_bytes(b"cached dependency")
    wheel = tmp_path / "k5_vision-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for path in (candidate / "src/k5vision").rglob("*"):
            if path.is_file():
                archive.writestr(
                    "k5vision/" + path.relative_to(candidate / "src/k5vision").as_posix(),
                    path.read_bytes(),
                )
    git, evidence = tmp_path / "git.exe", tmp_path / "evidence"
    git.write_bytes(b"admitted git")
    evidence.mkdir()
    args = SimpleNamespace(
        repo=candidate,
        analytics_source=tmp_path / "analytics",
        git=git,
        k5_wheel=wheel,
        wheelhouse=wheelhouse,
        local_appdata=local,
        evidence_root=evidence,
    )
    expectations = {
        key: value for key, value in expected().items() if key != "runtime_identity_sha256"
    }
    expectations.update(
        source_tree_sha256=witness.common.digest(witness.tree_manifest(candidate)),
        k5_payload_sha256=witness.common.digest(witness.source_payload(candidate)),
        k5_wheel_sha256=witness.common.file_hash(wheel),
        analytics_manifest_sha256=witness.common.file_hash(
            candidate / "src/k5vision/data/analytics-runtime-manifest.json"
        ),
        analytics_wheel_sha256=witness.common.hashlib.sha256(b"deterministic wrapper").hexdigest(),
        wheelhouse_sha256=witness.common.digest(witness.tree_manifest(wheelhouse)),
        native_cache_sha256=witness.common.digest(witness.native_manifest(local)),
    )
    for name, prefix in zip(witness.SCRIPT_NAMES, ("start", "test", "run"), strict=True):
        expectations[prefix + "_script_sha256"] = witness.common.file_hash(
            candidate / "scripts/windows-alpha" / name
        )
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if kwargs["operation"] in {"build_analytics_wheel", "rebuild_analytics_wheel"}:
            destination = Path(command[-1])
            destination.mkdir()
            (destination / "k5_analytics_runtime-0.0.0-py3-none-any.whl").write_bytes(
                b"deterministic wrapper"
            )

    def extract(_archive, target):
        if target.name == "source":
            shutil.copytree(candidate, target)
        else:
            target.mkdir()

    monkeypatch.setattr(witness.common, "run", run)
    monkeypatch.setattr(witness.common, "extract_archive", extract)
    return args, expectations, calls


def test_prepare_uses_pinned_offline_packages_and_external_venv(monkeypatch, tmp_path) -> None:
    args, expectations, calls = prepare_fixture(monkeypatch, tmp_path)
    work = tmp_path / "owned"
    work.mkdir()
    installed, env, command = witness.prepare(
        args, work, expectations, witness.new_receipt(expectations)
    )
    assert installed == work / "installed"
    assert not installed.is_relative_to(args.repo)
    assert command[:3] == [str(installed / ".venv/Scripts/python.exe"), "-I", "-B"]
    assert command[3] == str(work / "installed_analytics_witness.py")
    assert env["PYTHONPATH"] == "" and env["PIP_NO_INDEX"] == "1"
    dependencies = next(
        command for command, meta in calls if meta["operation"] == "install_dependencies"
    )
    for flag in ("--no-index", "--no-deps", "--only-binary=:all:", "--no-compile"):
        assert flag in dependencies
    assert "openvino==2026.3.1" in dependencies
    assert "numpy==2.2.6" in dependencies
    assert str(args.wheelhouse) in dependencies
    for command, meta in calls:
        assert meta["cwd"] == work
        assert "download" not in command
    archive_commands = [
        command for command, meta in calls if meta["operation"].startswith("archive_")
    ]
    assert all(
        "core.autocrlf=false" in command and "core.eol=lf" in command
        for command in archive_commands
    )
    assert archive_commands[1][-1] == witness.common.ANALYTICS_REVISION
    witness.verify_layout(installed, expectations)


@pytest.mark.parametrize(
    "field",
    [
        "source_tree_sha256",
        "k5_payload_sha256",
        "k5_wheel_sha256",
        "analytics_manifest_sha256",
        "wheelhouse_sha256",
        "native_cache_sha256",
        "analytics_wheel_sha256",
        "start_script_sha256",
    ],
)
def test_prepare_rejects_wrong_input_identities_before_launch(monkeypatch, tmp_path, field) -> None:
    args, expectations, calls = prepare_fixture(monkeypatch, tmp_path)
    expectations[field] = "c" * 64
    work = tmp_path / "owned"
    work.mkdir()
    with pytest.raises(witness.common.WitnessError):
        witness.prepare(args, work, expectations, witness.new_receipt(expectations))
    assert not any(meta["operation"].startswith("launch_") for _, meta in calls)


def execute_fixture(monkeypatch, tmp_path, *, launch_error=False, cleanup_error=False):
    from types import SimpleNamespace

    repo, temporary, artifacts = tmp_path / "repo", tmp_path / "temporary", tmp_path / "artifacts"
    for directory in (repo, temporary, artifacts):
        directory.mkdir()
    owned = temporary / "owned"
    owned.mkdir()
    identities = expected()
    identities["native_cache_sha256"] = witness.common.digest({"native": "admitted"})
    identities["wheelhouse_sha256"] = witness.common.digest({"dependency": "admitted"})
    input_file = tmp_path / "admission.json"
    input_file.write_bytes(
        witness.common.canonical(
            {key: value for key, value in identities.items() if key != "runtime_identity_sha256"}
        )
    )
    args = SimpleNamespace(
        expectations=input_file,
        output=artifacts / witness.RECEIPT_NAME,
        admitted_expectations=artifacts / witness.EXPECTATIONS_NAME,
        repo=repo,
        temp_root=temporary,
        work_root=owned,
        wheelhouse=tmp_path / "wheelhouse",
    )
    args.output.write_text("stale success must be removed")
    args.admitted_expectations.write_text("stale expectations must be removed")
    state = {"launches": 0}
    monkeypatch.setattr(witness, "admit_platform", lambda: None)

    def prepare(_args, work, _expected, _document):
        installed = work / "installed"
        installed.mkdir()
        system = work / "Windows"
        powershell = system / "System32/WindowsPowerShell/v1.0/powershell.exe"
        powershell.parent.mkdir(parents=True)
        powershell.write_bytes(b"admitted executable")
        (work / "session-database.sqlite3").write_bytes(b"disposable state")
        return (
            installed,
            {"SYSTEMROOT": str(system), "LOCALAPPDATA": str(work / "local")},
            ["probe"],
        )

    def probe(_command, _work, _env, expected_ids, **kwargs):
        if kwargs.get("after"):
            assert expected_ids == identities
        return {
            key: identities[key]
            for key in ("runtime_identity_sha256", "model_identity_sha256", "seed_identity_sha256")
        }

    def launch(**kwargs):
        assert not args.output.exists()
        assert witness.common.read_json(args.admitted_expectations) == identities
        state["launches"] += 1
        if launch_error:
            raise RuntimeError("private path and secret token must never be printed")
        kwargs["document"].update(receipt())
        kwargs["document"].update(identities)
        kwargs["document"]["stage"] = "launch_2"
        kwargs["document"]["completed"] = False

    monkeypatch.setattr(witness, "prepare", prepare)
    monkeypatch.setattr(witness, "probe", probe)
    monkeypatch.setattr(witness, "verify_native", lambda *_: None)
    monkeypatch.setattr(witness, "verify_layout", lambda *_: None)
    monkeypatch.setattr(witness, "launch_sequence", launch)
    monkeypatch.setattr(witness, "native_manifest", lambda *_: {"native": "admitted"})
    monkeypatch.setattr(
        witness, "tree_manifest", lambda *_args, **_kwargs: {"dependency": "admitted"}
    )
    if cleanup_error:

        def refuse_delete(_path):
            raise OSError("private cleanup path must not be printed")

        monkeypatch.setattr(witness.shutil, "rmtree", refuse_delete)
    return args, identities, state


def test_portable_orchestration_writes_admission_before_launch_and_receipt_after_cleanup(
    monkeypatch, tmp_path
) -> None:
    args, identities, state = execute_fixture(monkeypatch, tmp_path)
    assert witness.execute(args) == 0
    assert state["launches"] == 1
    assert not args.work_root.exists()
    witness.validate_receipt(witness.common.read_json(args.output), identities)
    assert witness.common.read_json(args.admitted_expectations) == identities


@pytest.mark.parametrize("failure", ["launch", "cleanup"])
def test_orchestration_failure_has_no_stale_receipt_or_raw_diagnostics(
    monkeypatch, tmp_path, capsys, failure
) -> None:
    args, _identities, _state = execute_fixture(
        monkeypatch, tmp_path, launch_error=failure == "launch", cleanup_error=failure == "cleanup"
    )
    assert witness.execute(args) == 1
    assert not args.output.exists()
    captured = capsys.readouterr()
    assert "private" not in captured.out + captured.err
    assert "secret" not in captured.out + captured.err
    assert "K5_INSTALLED_DIAGNOSTIC=" in captured.out
    if failure == "launch":
        assert not args.work_root.exists()
    else:
        assert "cleanup_incomplete" in captured.out


def test_malformed_admission_removes_stale_success(monkeypatch, tmp_path) -> None:
    args, _identities, _state = execute_fixture(monkeypatch, tmp_path)
    args.expectations.write_bytes(b'{"wrong":"schema"}')
    with pytest.raises(witness.common.WitnessError):
        witness.execute(args)
    assert not args.output.exists() and not args.admitted_expectations.exists()


def test_execution_cli_missing_inputs_cannot_leave_stale_success(tmp_path) -> None:
    output = tmp_path / witness.RECEIPT_NAME
    output.write_bytes(witness.common.canonical(receipt()))
    expectations = tmp_path / "input.json"
    expectations.write_bytes(b"{}")
    child = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            str(SPEC.origin),
            "--expectations",
            str(expectations),
            "--output",
            str(output),
        ],
        capture_output=True,
        timeout=10,
    )
    assert child.returncode == 1 and not output.exists()
    assert str(tmp_path).encode() not in child.stdout + child.stderr


def test_cache_marker_is_bounded_before_decoding(tmp_path) -> None:
    marker = tmp_path / "record.sha256"
    marker.write_bytes(b"x" * 129)
    with pytest.raises(witness.common.WitnessError):
        witness.cache_marker(marker)


@pytest.mark.skipif(witness.os.name != "nt", reason="Requires real Windows change notifications")
def test_windows_real_directory_guard_remembers_created_then_deleted_session(tmp_path) -> None:
    """Non-media Windows proof; touches only this test's exclusively owned TEMP."""
    import time

    session = tmp_path / "K5VisionAlpha-owned-test"
    guard = witness.DirectoryChangeGuard(tmp_path)
    try:
        assert guard.unchanged()
        session.mkdir()
        session.rmdir()
        deadline = time.monotonic() + 2
        while guard.unchanged() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not session.exists()
        assert not guard.unchanged()
    finally:
        guard.close()
        if session.exists():
            session.rmdir()


def test_invalid_job_count_has_precise_source_free_diagnostic(monkeypatch, tmp_path) -> None:
    _state, sessions = invoke_fixture(monkeypatch, tmp_path, invalid=True, total=6)
    with pytest.raises(witness.common.WitnessError) as caught:
        witness.invoke_start(
            ["real Start"],
            work=tmp_path,
            env={"TEMP": str(sessions)},
            operation="probe_admission",
            invalid=True,
            admitted_images={},
            owned_observations=[],
        )
    record = caught.value.alpha_diagnostic
    assert record["stage"] == "invalid_config"
    assert record["contract"] == "invalid_initialization"
    assert record["job_total"] == 6 and record["expected_job_total"] == 5
    assert record["job_active"] == 0 and record["session_unchanged"] is None
    assert record["marker_refusal"] == 1 and record["child_exit_code"] == 23
    assert record["relay_exit_code"] == 23
    assert record["health_confirmed"] is False


def test_valid_marker_failure_identifies_second_launch_and_health_milestone(
    monkeypatch, tmp_path
) -> None:
    output = success_output().replace(b"K5 operator PASS: frames=225, presentations=226\n", b"")
    output += (
        b"K5 Vision Alpha health check PASS.\n"
        b"Launching the authenticated K5 Windows operator path...\n"
    )
    _state, sessions = invoke_fixture(monkeypatch, tmp_path, output=output)
    with pytest.raises(witness.common.WitnessError) as caught:
        witness.invoke_start(
            ["real Start"], work=tmp_path, env={"TEMP": str(sessions)}, operation="launch_2"
        )
    record = caught.value.alpha_diagnostic
    assert record["stage"] == "launch_2" and record["contract"] == "valid_markers"
    assert record["marker_operator"] == 0 and record["expected_marker_operator"] == 1
    assert record["health_confirmed"] is True and record["operator_request_observed"] is True


def test_final_schema_failure_identifies_exact_boundary() -> None:
    result = receipt()
    result["raw_private_path"] = "sensitive content"
    with pytest.raises(witness.common.WitnessError) as caught:
        witness.validate_receipt(result, expected())
    record = caught.value.alpha_diagnostic
    assert record["stage"] == "final_receipt" and record["contract"] == "receipt_schema"
    assert b"sensitive" not in witness.common.canonical(record)
    assert b"raw_private_path" not in witness.common.canonical(record)


def alpha_records(text):
    prefix = "K5_ALPHA_LAUNCHER_DIAGNOSTIC="
    records = [
        witness.common.parse_json(line[len(prefix) :].encode())
        for line in text.splitlines()
        if line.startswith(prefix)
    ]
    for record in records:
        witness.validate_alpha_diagnostic(record)
    return records


@pytest.mark.parametrize(
    "change",
    [
        {"extra": "private"},
        {"stage": "private path"},
        {"contract": "raw exception"},
        {"field": "unknown private field"},
        {"failure_code": "private error"},
        {"schema_version": 1},
        {"job_total": True},
        {"job_active": -1},
        {"marker_operator": 2**32},
        {"counter_presentations": "225"},
        {"health_confirmed": 1},
        {"session_unchanged": []},
        {"gate_state": "private host"},
        {"child_exit_code": 2**32},
        {"relay_exit_code": -(2**31) - 1},
    ],
)
def test_diagnostic_schema_rejects_forged_raw_or_unbounded_values(change) -> None:
    value = witness.alpha_diagnostic("invalid_config", "invalid_job_total")
    value.update(change)
    with pytest.raises(ValueError, match="invalid Alpha diagnostic"):
        witness.validate_alpha_diagnostic(value)
    with pytest.raises(ValueError):
        witness.emit_alpha_diagnostic(value)


def test_diagnostic_is_bounded_fixed_scalar_and_exception_text_free(capsys) -> None:
    value = witness.alpha_diagnostic(
        "invalid_config",
        "invalid_job_total",
        observations={
            "job_total": 5,
            "expected_job_total": 4,
            "job_active": 0,
            "marker_refusal": 1,
            "session_unchanged": False,
            "child_exit_code": 23,
            "gate_state": "exited",
        },
    )
    witness.emit_alpha_diagnostic(value)
    assert len(witness.common.canonical(value)) < witness.common.MAX_BYTES
    error = witness.contextual_error(
        RuntimeError("secret path or token"), "build", "driver_contract"
    )
    witness.emit_alpha_diagnostic(error.alpha_diagnostic)
    output = capsys.readouterr().out
    assert len(alpha_records(output)) == 2
    assert "secret" not in output and "token" not in output
    assert all(value is None or type(value) in (str, bool, int) for value in value.values())


@pytest.mark.parametrize("value", [True, "225", {"raw": "secret"}, -1, 2**32, 1.0])
def test_untrusted_counter_observation_is_omitted(value) -> None:
    assert witness.observed_integer(value) is None


@pytest.mark.parametrize(
    "overrides,contract",
    [
        ({"output": REFUSAL_OUTPUT + b"x" * 65537}, "invalid_output_limit"),
        ({"output": b""}, "invalid_markers"),
        ({"exit_code": 0}, "invalid_exit"),
        ({"total": 6}, "invalid_initialization"),
        ({"unchanged": False}, "invalid_initialization"),
        ({"active": 1}, "job_active"),
        ({"leave_session": True}, "sessions_empty"),
        ({"exit_code": 24}, "child_process"),
    ],
)
def test_invalid_boundaries_remain_closed_and_are_distinguishable(
    monkeypatch, tmp_path, overrides, contract
) -> None:
    _state, sessions = invoke_fixture(monkeypatch, tmp_path, invalid=True, **overrides)
    with pytest.raises(witness.common.WitnessError) as caught:
        witness.invoke_start(
            ["actual Start"],
            work=tmp_path,
            env={"TEMP": str(sessions)},
            operation="probe_admission",
            invalid=True,
            admitted_images={},
            owned_observations=[],
        )
    value = caught.value.alpha_diagnostic
    assert value["stage"] == "invalid_config" and value["contract"] == contract
    assert value["launcher_requested"] is True
    assert value["launcher_returned"] is True
    assert value["health_confirmed"] is False
    assert value["expected_marker_refusal"] == 1
    assert value["expected_marker_operator"] == 0


@pytest.mark.parametrize(
    "before,after,contract",
    [
        (b"frames=225", b"frames=226", "frame_count"),
        (b"presentations=226", b"presentations=224", "presentation_count"),
        (b"completions=39", b"completions=0", "analytics_activity"),
        (b"failures=0", b"failures=1", "analytics_failures"),
        (b"frames=225", b"frames=1000001", "run_counter"),
    ],
)
def test_first_launch_counter_failures_expose_observation_not_raw_output(
    monkeypatch, tmp_path, before, after, contract
) -> None:
    _state, sessions = invoke_fixture(
        monkeypatch, tmp_path, output=success_output().replace(before, after)
    )
    with pytest.raises(witness.common.WitnessError) as caught:
        witness.invoke_start(
            ["actual Start"], work=tmp_path, env={"TEMP": str(sessions)}, operation="launch_1"
        )
    value = caught.value.alpha_diagnostic
    assert value["stage"] == "launch_1" and value["contract"] == contract
    assert value["expected_frames"] == 225 and value["minimum_presentations"] == 225
    assert value["minimum_completions"] == 1 and value["expected_failures"] == 0
    assert value["health_confirmed"] is False  # No observed marker is not proof of no app.
    assert str(tmp_path).encode() not in witness.common.canonical(value)


def test_milestones_are_observational_and_leave_success_gate_identical() -> None:
    raw = success_output()
    plain = witness.LaunchSummary(io.BytesIO(raw))
    observed = witness.LaunchSummary(
        io.BytesIO(
            raw + b"K5 Vision Alpha health check PASS.\n"
            b"Launching the authenticated K5 Windows operator path...\n"
        )
    )
    plain.finish()
    observed.finish()
    assert plain.result() == observed.result()
    assert plain.counts == observed.counts
    assert plain.observations()["health_confirmed"] is False
    assert observed.observations()["health_confirmed"] is True
    assert set(observed.result()) == witness.COUNTERS | witness.RUN_BOOLEANS


def test_primary_receipt_and_process_cleanup_failures_both_survive(
    monkeypatch, tmp_path, capsys
) -> None:
    _state, sessions = invoke_fixture(
        monkeypatch, tmp_path, invalid=True, output=b"", cleanup_failure=True
    )
    with pytest.raises(witness.common.WitnessError) as caught:
        witness.invoke_start(
            ["actual Start"],
            work=tmp_path,
            env={"TEMP": str(sessions)},
            operation="probe_admission",
            invalid=True,
            admitted_images={},
            owned_observations=[],
        )
    primary = alpha_records(capsys.readouterr().out)
    assert len(primary) == 1 and primary[0]["contract"] == "invalid_markers"
    assert caught.value.alpha_diagnostic["stage"] == "cleanup"
    assert caught.value.alpha_diagnostic["contract"] == "owned_process_cleanup"


def test_primary_and_layout_cleanup_failure_both_logged(monkeypatch, tmp_path, capsys) -> None:
    args, _identities, _state = execute_fixture(
        monkeypatch, tmp_path, launch_error=True, cleanup_error=True
    )
    assert witness.execute(args) == 1
    values = alpha_records(capsys.readouterr().out)
    assert len(values) == 2
    assert values[0]["contract"] == "driver_contract"
    assert values[1]["stage"] == "cleanup" and values[1]["contract"] == "owned_layout_cleanup"
    assert not args.output.exists()


@pytest.mark.parametrize("stage", ["build", "install", "probe"])
def test_prelaunch_stage_survives_fixed_driver_failure(
    monkeypatch, tmp_path, capsys, stage
) -> None:
    args, _identities, _state = execute_fixture(monkeypatch, tmp_path)

    def fail_before_launch(_args, _work, _expected, document):
        document["stage"] = stage
        raise witness.common.WitnessError("identity_mismatch")

    monkeypatch.setattr(witness, "prepare", fail_before_launch)
    assert witness.execute(args) == 1
    values = alpha_records(capsys.readouterr().out)
    assert len(values) == 1 and values[0]["stage"] == stage
    assert values[0]["launcher_requested"] is None and values[0]["health_confirmed"] is None
    assert not args.output.exists()


@pytest.mark.parametrize("validation", [False, True])
def test_outer_cli_catch_emits_input_or_final_boundary(tmp_path, validation) -> None:
    output = tmp_path / witness.RECEIPT_NAME
    expectations = tmp_path / "input.json"
    expectations.write_bytes(
        witness.common.canonical(expected() if validation else {"unknown": "secret"})
    )
    args = [
        sys.executable,
        "-I",
        "-B",
        str(SPEC.origin),
        "--expectations",
        str(expectations),
        "--output",
        str(output),
    ]
    if validation:
        data = receipt()
        data["unknown"] = "private raw output"
        output.write_bytes(witness.common.canonical(data))
        args.append("--validate-receipt")
    else:
        for flag in (
            "repo",
            "analytics-source",
            "k5-wheel",
            "wheelhouse",
            "evidence-root",
            "local-appdata",
            "git",
            "temp-root",
            "work-root",
        ):
            args += ["--" + flag, str(tmp_path)]
        args += ["--admitted-expectations", str(tmp_path / witness.EXPECTATIONS_NAME)]
    result = subprocess.run(args, capture_output=True, timeout=10)
    assert result.returncode == 1
    records = alpha_records(result.stdout.decode())
    assert len(records) == 1
    assert records[0]["stage"] == ("final_receipt" if validation else "admission")
    assert records[0]["contract"] == ("receipt_schema" if validation else "input_schema")
    assert b"secret" not in result.stdout and b"private" not in result.stdout


def test_existing_session_refuses_before_launcher_request(monkeypatch, tmp_path) -> None:
    _state, sessions = invoke_fixture(monkeypatch, tmp_path, invalid=True)
    (sessions / "existing-state").write_bytes(b"preserve")
    with pytest.raises(witness.common.WitnessError) as caught:
        witness.invoke_start(
            ["actual Start"],
            work=tmp_path,
            env={"TEMP": str(sessions)},
            operation="probe_admission",
            invalid=True,
            admitted_images={},
            owned_observations=[],
        )
    value = caught.value.alpha_diagnostic
    assert value["stage"] == "invalid_config" and value["contract"] == "sessions_empty"
    assert value["launcher_requested"] is False and value["launcher_returned"] is False
    assert value["health_confirmed"] is None
    assert (sessions / "existing-state").read_bytes() == b"preserve"


def test_timeout_preserves_known_gate_state_without_claiming_return(monkeypatch, tmp_path) -> None:
    _state, sessions = invoke_fixture(monkeypatch, tmp_path)
    original = witness.common.OwnedProcess

    class TimedOut(original):
        def wait(self, _seconds):
            self.process.returncode = None
            raise witness.common.WitnessError(
                "child_timeout",
                witness.common.diagnostic(
                    "launch_1",
                    outcome="timeout",
                    gate_state="started",
                    timed_out=True,
                    category="child_timeout",
                ),
            )

    monkeypatch.setattr(witness.common, "OwnedProcess", TimedOut)
    with pytest.raises(witness.common.WitnessError) as caught:
        witness.invoke_start(
            ["actual Start"], work=tmp_path, env={"TEMP": str(sessions)}, operation="launch_1"
        )
    value = caught.value.alpha_diagnostic
    assert value["timed_out"] is True and value["gate_state"] == "started"
    assert value["launcher_requested"] is True and value["launcher_returned"] is False
    assert value["child_exit_code"] is None and value["relay_exit_code"] is None


def test_launch_failure_retains_fixed_gate_details(monkeypatch, tmp_path) -> None:
    _state, sessions = invoke_fixture(monkeypatch, tmp_path)

    def failed(*_args, **_kwargs):
        raise witness.common.WitnessError(
            "child_failed",
            witness.common.diagnostic(
                "launch_1",
                outcome="launch_failed",
                gate_state="waiting",
                category="executable_missing",
            ),
        )

    monkeypatch.setattr(witness.common, "OwnedProcess", failed)
    with pytest.raises(witness.common.WitnessError) as caught:
        witness.invoke_start(
            ["actual Start"], work=tmp_path, env={"TEMP": str(sessions)}, operation="launch_1"
        )
    value = caught.value.alpha_diagnostic
    assert value["gate_state"] == "waiting"
    assert value["launcher_requested"] is True and value["launcher_returned"] is False
    assert value["health_confirmed"] is None


def test_primary_and_multiple_cleanup_failures_each_reported_once(
    monkeypatch, tmp_path, capsys
) -> None:
    _state, sessions = invoke_fixture(
        monkeypatch, tmp_path, invalid=True, output=b"", cleanup_failure=True
    )
    original_finish = witness.LaunchSummary.finish
    calls = []

    def finish(summary):
        calls.append(1)
        original_finish(summary)
        if len(calls) > 1:
            raise witness.common.WitnessError("cleanup_incomplete")

    def close(_guard):
        raise witness.common.WitnessError("cleanup_incomplete")

    monkeypatch.setattr(witness.LaunchSummary, "finish", finish)
    monkeypatch.setattr(witness.boundary.PreflightObservation, "close", close)
    with pytest.raises(witness.common.WitnessError) as caught:
        witness.invoke_start(
            ["actual Start"],
            work=tmp_path,
            env={"TEMP": str(sessions)},
            operation="probe_admission",
            invalid=True,
            admitted_images={},
            owned_observations=[],
        )
    records = alpha_records(capsys.readouterr().out)
    records.append(caught.value.alpha_diagnostic)
    assert [record["contract"] for record in records] == [
        "invalid_markers",
        "owned_process_cleanup",
        "collector_cleanup",
        "invalid_observation_cleanup",
    ]
    assert all(record["relay_exit_code"] == 23 for record in records)


def test_final_validator_error_in_execute_finally_reaches_outer_catch(
    monkeypatch, tmp_path, capsys
) -> None:
    args, _identities, _state = execute_fixture(monkeypatch, tmp_path)

    def reject(*_args, **_kwargs):
        witness.contract_require(False, "final_receipt", "receipt_state")

    monkeypatch.setattr(witness, "validate_receipt", reject)
    monkeypatch.setattr(witness.argparse.ArgumentParser, "parse_args", lambda _self: args)
    args.validate_receipt = False
    for name in ("analytics_source", "k5_wheel", "evidence_root", "local_appdata", "git"):
        setattr(args, name, tmp_path)
    assert witness.main() == 1
    records = alpha_records(capsys.readouterr().out)
    assert len(records) == 1
    assert records[0]["stage"] == "final_receipt" and records[0]["contract"] == "receipt_state"
    assert not args.output.exists() and not args.work_root.exists()


def test_observation_setup_failure_is_before_requested_launcher(monkeypatch, tmp_path) -> None:
    _state, sessions = invoke_fixture(monkeypatch, tmp_path, invalid=True)

    def fail(*_args, **_kwargs):
        raise OSError("private source path")

    monkeypatch.setattr(witness.boundary, "PreflightObservation", fail)
    with pytest.raises(witness.common.WitnessError) as caught:
        witness.invoke_start(
            ["actual Start"],
            work=tmp_path,
            env={"TEMP": str(sessions)},
            operation="probe_admission",
            invalid=True,
            admitted_images={},
            owned_observations=[],
        )
    value = caught.value.alpha_diagnostic
    assert value["stage"] == "invalid_config" and value["contract"] == "invalid_observation_setup"
    assert value["launcher_requested"] is False and value["launcher_returned"] is False
    assert value["health_confirmed"] is None


def test_final_receipt_write_failure_keeps_final_boundary(monkeypatch, tmp_path, capsys) -> None:
    args, _identities, _state = execute_fixture(monkeypatch, tmp_path)
    original_open = Path.open

    def fail_output(path, *positional, **kwargs):
        if path == args.output and positional == ("xb",):
            raise OSError("private receipt path")
        return original_open(path, *positional, **kwargs)

    monkeypatch.setattr(Path, "open", fail_output)
    monkeypatch.setattr(witness.argparse.ArgumentParser, "parse_args", lambda _self: args)
    args.validate_receipt = False
    for name in ("analytics_source", "k5_wheel", "evidence_root", "local_appdata", "git"):
        setattr(args, name, tmp_path)
    assert witness.main() == 1
    records = alpha_records(capsys.readouterr().out)
    assert len(records) == 1
    assert records[0]["stage"] == "final_receipt" and records[0]["contract"] == "receipt_write"
    assert not args.output.exists() and not args.work_root.exists()


@pytest.mark.parametrize(
    "stage,operation", [("build", "build_k5_wheel"), ("probe", "probe_before")]
)
def test_prelaunch_child_exit_is_not_a_launcher_return(
    monkeypatch, tmp_path, capsys, stage, operation
) -> None:
    args, _identities, _state = execute_fixture(monkeypatch, tmp_path)

    def fail_before_launch(_args, _work, _expected, document):
        document["stage"] = stage
        raise witness.common.WitnessError(
            "child_failed",
            witness.common.diagnostic(
                operation,
                outcome="child_failed",
                gate_state="exited",
                child_exit_code=1,
                relay_exit_code=1,
            ),
        )

    monkeypatch.setattr(witness, "prepare", fail_before_launch)
    assert witness.execute(args) == 1
    records = alpha_records(capsys.readouterr().out)
    assert len(records) == 1 and records[0]["stage"] == stage
    assert records[0]["gate_state"] == "exited" and records[0]["child_exit_code"] == 1
    assert records[0]["launcher_requested"] is None
    assert records[0]["launcher_returned"] is None
    assert records[0]["health_confirmed"] is None


def test_prepare_binds_observer_raw_bytes_even_with_admitted_tree_hash(monkeypatch, tmp_path):
    args, expectations, calls = prepare_fixture(monkeypatch, tmp_path)
    (args.repo / "scripts/windows_owned_preflight.py").write_bytes(b"# substituted observer\n")
    expectations["source_tree_sha256"] = witness.common.digest(witness.tree_manifest(args.repo))
    work = tmp_path / "owned"
    work.mkdir()
    with pytest.raises(witness.common.WitnessError):
        witness.prepare(args, work, expectations, witness.new_receipt(expectations))
    assert not any(meta["operation"] == "create_venv" for _, meta in calls)


def test_execution_refuses_to_delete_layout_with_live_observer(monkeypatch, tmp_path):
    from types import SimpleNamespace

    args, _identities, _state = execute_fixture(monkeypatch, tmp_path)

    def launch(**kwargs):
        kwargs["owned_observations"].append(SimpleNamespace(quiescent=lambda: False))
        raise witness.common.WitnessError("cleanup_incomplete")

    monkeypatch.setattr(witness, "launch_sequence", launch)
    assert witness.execute(args) == 1
    assert args.work_root.exists() and not args.output.exists()


def test_invalid_success_uses_one_registered_observer_and_real_profile_validator(
    monkeypatch, tmp_path
):
    state, sessions = invoke_fixture(monkeypatch, tmp_path, invalid=True)
    tracked = []
    observation_class = witness.boundary.PreflightObservation
    original_start = observation_class.start

    def start(observation):
        assert tracked == [observation]
        original_start(observation)

    monkeypatch.setattr(observation_class, "start", start)
    result = witness.invoke_start(
        ["actual Start"],
        work=tmp_path,
        env={"TEMP": str(sessions)},
        operation="probe_admission",
        invalid=True,
        admitted_images={},
        owned_observations=tracked,
    )
    assert len(tracked) == 1 and tracked[0].quiescent()
    witness.boundary.validate_invalid_initialization(tracked[0].summary)
    assert all(result.values()) and state["closed"] and state["observation_closed"]


@pytest.mark.parametrize(
    "output,contract",
    [
        (b"", "invalid_markers"),
        (REFUSAL_OUTPUT + b"K5 Vision Alpha health check PASS.\n", "invalid_milestones"),
        (
            REFUSAL_OUTPUT + b"Launching the authenticated K5 Windows operator path...\n",
            "invalid_milestones",
        ),
    ],
)
def test_valid_os_profile_cannot_hide_wrong_refusal_or_milestones(
    monkeypatch, tmp_path, output, contract
):
    _state, sessions = invoke_fixture(monkeypatch, tmp_path, invalid=True, output=output)
    tracked = []
    with pytest.raises(witness.AlphaWitnessError) as caught:
        witness.invoke_start(
            ["actual Start"],
            work=tmp_path,
            env={"TEMP": str(sessions)},
            operation="probe_admission",
            invalid=True,
            admitted_images={},
            owned_observations=tracked,
        )
    assert caught.value.alpha_diagnostic["contract"] == contract
    assert len(tracked) == 1
    witness.boundary.validate_invalid_initialization(tracked[0].summary)


def test_observer_setup_denial_keeps_registered_owner_and_fixed_evidence(
    monkeypatch, tmp_path, capsys
):
    state, sessions = invoke_fixture(monkeypatch, tmp_path, invalid=True)
    tracked = []

    def denied(observation):
        assert tracked == [observation]
        raise witness.boundary.ObservationFailure("access_denied")

    monkeypatch.setattr(witness.boundary.PreflightObservation, "start", denied)
    with pytest.raises(witness.AlphaWitnessError) as caught:
        witness.invoke_start(
            ["actual Start"],
            work=tmp_path,
            env={"TEMP": str(sessions)},
            operation="probe_admission",
            invalid=True,
            admitted_images={},
            owned_observations=tracked,
        )
    assert len(tracked) == 1 and state["observation_closed"] and not state["closed"]
    assert caught.value.alpha_diagnostic["contract"] == "invalid_observation_setup"
    assert caught.value.alpha_diagnostic["launcher_requested"] is False
    text = capsys.readouterr().out
    assert '"error":"access_denied"' in text and '"phase":"setup"' in text


@pytest.mark.parametrize(
    "change",
    [
        {"path": "private"},
        {"error": "private exception"},
        {"error": {"nested": "value"}},
        {"stage": "launch_1"},
        {"phase": "unknown"},
        {"schema_version": True},
        {"error": "none"},
    ],
)
def test_observation_failure_record_rejects_raw_or_forged_fields(change):
    value = {
        "schema_version": "owned-preflight-failure-v1",
        "stage": "invalid_config",
        "phase": "validate",
        "error": "incomplete",
    }
    value.update(change)
    with pytest.raises(witness.common.WitnessError):
        witness.validate_observation_failure(value)


def test_observation_external_exception_text_is_never_emitted(capsys):
    witness.emit_observation_failure(OSError("private path and token"), "finish")
    text = capsys.readouterr().out
    assert '"error":"native_error"' in text
    assert "private" not in text and "token" not in text


def test_profile_failure_preserves_fixed_process_and_temp_evidence(monkeypatch, tmp_path, capsys):
    _state, sessions = invoke_fixture(monkeypatch, tmp_path, invalid=True, unchanged=False)
    with pytest.raises(witness.AlphaWitnessError) as caught:
        witness.invoke_start(
            ["actual Start"],
            work=tmp_path,
            env={"TEMP": str(sessions)},
            operation="probe_admission",
            invalid=True,
            admitted_images={},
            owned_observations=[],
        )
    text = capsys.readouterr().out
    assert "K5_OWNED_PREFLIGHT_INITIALIZATION=" in text
    assert '"policy_lifecycles_complete":false' in text
    assert '"process_console_host":1' in text
    assert '"phase":"validate"' in text
    assert caught.value.alpha_diagnostic["contract"] == "invalid_initialization"


def test_explicit_image_admission_ignores_mutable_path(monkeypatch, tmp_path):
    system = tmp_path / "Windows"
    installed = tmp_path / "installed"
    base = tmp_path / "base/python.exe"
    shell = system / "System32/WindowsPowerShell/v1.0/powershell.exe"
    python = installed / ".venv/Scripts/python.exe"
    console = system / "System32/conhost.exe"
    for path in (base, shell, python, console):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(path.name.encode())
    env = {"SYSTEMROOT": str(system), "PATH": "untrusted search directory"}
    monkeypatch.setattr(
        witness.common, "admitted_gate_python", lambda value: base if value is env else None
    )
    values = witness.admitted_preflight_images(env, installed, shell)
    assert values == {
        kind: (path, witness.common.file_hash(path))
        for kind, path in (
            ("base_python", base),
            ("powershell", shell),
            ("venv_python", python),
            ("console_host", console),
        )
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"error": "access_denied"},
        {
            "process_console_host": 0,
            "process_unknown": 1,
            "birth_4_class": "unknown",
            "process_coverage_complete": False,
            "error": "unexpected_process",
        },
        {"policy_lifecycles_complete": False, "temp_policy_probe_removed": 3, "temp_events": 11},
        {
            "policy_lifecycles_complete": False,
            "temp_other_owned_temp_added": 1,
            "temp_events": 13,
            "error": "unexpected_temp",
        },
        {"temp_drain_complete": False, "error": "incomplete"},
    ],
)
def test_refusal_cannot_hide_unknown_incomplete_or_extra_initialization(
    monkeypatch, tmp_path, capsys, changes
):
    _state, sessions = invoke_fixture(
        monkeypatch, tmp_path, invalid=True, profile_overrides=changes
    )
    with pytest.raises(witness.AlphaWitnessError) as caught:
        witness.invoke_start(
            ["actual Start"],
            work=tmp_path,
            env={"TEMP": str(sessions)},
            operation="probe_admission",
            invalid=True,
            admitted_images={},
            owned_observations=[],
        )
    assert caught.value.alpha_diagnostic["contract"] == "invalid_initialization"
    text = capsys.readouterr().out
    assert "K5_OWNED_PREFLIGHT_INITIALIZATION=" in text
    assert "K5_OWNED_PREFLIGHT_FAILURE=" in text
    assert "private" not in text


@pytest.mark.parametrize(
    "detail",
    [
        {"child_exit_code": 0},
        {"relay_exit_code": 24},
        {"gate_state": "waiting"},
        {"timed_out": True},
    ],
)
def test_valid_initialization_cannot_hide_refusal_gate_exit_mismatch(monkeypatch, tmp_path, detail):
    _state, sessions = invoke_fixture(monkeypatch, tmp_path, invalid=True, detail_override=detail)
    tracked = []
    with pytest.raises(witness.AlphaWitnessError):
        witness.invoke_start(
            ["actual Start"],
            work=tmp_path,
            env={"TEMP": str(sessions)},
            operation="probe_admission",
            invalid=True,
            admitted_images={},
            owned_observations=tracked,
        )
    witness.boundary.validate_invalid_initialization(tracked[0].summary)


def test_failed_observer_start_and_close_keep_outer_layout_owned(monkeypatch, tmp_path, capsys):
    from types import SimpleNamespace

    args, _identities, _state = execute_fixture(monkeypatch, tmp_path)
    tracked = []

    def launch(**kwargs):
        _state, sessions = invoke_fixture(monkeypatch, kwargs["work"], invalid=True)
        fake = witness.boundary.PreflightObservation

        class BrokenObservation(fake):
            def start(self):
                assert kwargs["owned_observations"] == [self]
                self.resources.append(
                    SimpleNamespace(thread=SimpleNamespace(is_alive=lambda: True))
                )
                tracked.append(self)
                raise witness.boundary.ObservationFailure("access_denied")

            def close(self):
                raise witness.boundary.ObservationFailure("cleanup_incomplete")

            def quiescent(self):
                return False

        monkeypatch.setattr(witness.boundary, "PreflightObservation", BrokenObservation)
        witness.invoke_start(
            ["actual Start"],
            work=kwargs["work"],
            env={"TEMP": str(sessions)},
            operation="probe_admission",
            invalid=True,
            admitted_images={},
            owned_observations=kwargs["owned_observations"],
        )

    monkeypatch.setattr(witness, "launch_sequence", launch)
    assert witness.execute(args) == 1
    assert len(tracked) == 1 and args.work_root.exists() and not args.output.exists()
    text = capsys.readouterr().out
    assert '"phase":"setup"' in text and '"error":"access_denied"' in text
    assert '"phase":"cleanup"' in text and '"error":"cleanup_incomplete"' in text
    assert "owned_layout_cleanup" in text


def test_observed_single_pair_passes_real_invalid_path_gates(monkeypatch, tmp_path):
    trace = witness.boundary.PolicyLifecycle()
    for name in ("__PSScriptPolicyTest_aaaaaaaa.aaa.ps1", "__PSScriptPolicyTest_bbbbbbbb.bbb.psm1"):
        for action in ("added", "modified", "removed"):
            trace.observe(name, action)
    profile = {
        "policy_ps1_files": 1,
        "policy_psm1_files": 1,
        "temp_events": 6,
        "policy_lifecycles_complete": trace.complete(),
        **{"temp_policy_probe_" + action: 2 for action in ("added", "modified", "removed")},
    }
    state, sessions = invoke_fixture(monkeypatch, tmp_path, invalid=True, profile_overrides=profile)
    tracked = []
    result = witness.invoke_start(
        ["actual Start"],
        work=tmp_path,
        env={"TEMP": str(sessions)},
        operation="probe_admission",
        invalid=True,
        admitted_images={},
        owned_observations=tracked,
    )
    assert result == {
        "invalid_config_refused": True,
        "invalid_config_no_session": True,
        "invalid_config_no_media": True,
    }
    assert state["closed"] and state["observation_closed"] and len(tracked) == 1
    witness.boundary.validate_invalid_initialization(tracked[0].summary)


@pytest.mark.parametrize(
    "overrides",
    [
        {"exit_code": 0},
        {"exit_code": 24},
        {"output": b""},
        {"output": REFUSAL_OUTPUT + b"K5 Vision Alpha health check PASS.\n"},
        {"active": 1},
        {"leave_session": True},
        {"cleanup_failure": True},
    ],
)
def test_single_pair_cannot_bypass_refusal_activity_or_cleanup(monkeypatch, tmp_path, overrides):
    profile = {
        "policy_ps1_files": 1,
        "policy_psm1_files": 1,
        "temp_events": 6,
        "policy_lifecycles_complete": True,
        **{"temp_policy_probe_" + action: 2 for action in ("added", "modified", "removed")},
    }
    state, sessions = invoke_fixture(
        monkeypatch, tmp_path, invalid=True, profile_overrides=profile, **overrides
    )
    with pytest.raises(witness.AlphaWitnessError):
        witness.invoke_start(
            ["actual Start"],
            work=tmp_path,
            env={"TEMP": str(sessions)},
            operation="probe_admission",
            invalid=True,
            admitted_images={},
            owned_observations=[],
        )
    assert state["closed"] and state["observation_closed"]
