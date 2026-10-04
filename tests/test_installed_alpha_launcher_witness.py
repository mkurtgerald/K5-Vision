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

    def invoke(command, **kwargs):
        assert "Start-K5VisionAlpha.ps1" in " ".join(command)
        calls.append(("start", kwargs.get("invalid", False)))
        if kwargs.get("invalid"):
            assert (
                Path(kwargs["env"]["K5_ANALYTICS_CONFIG"]).read_bytes().find(b"invalid-selected")
                >= 0
            )
            return dict.fromkeys(
                ("invalid_config_refused", "invalid_config_no_session", "invalid_config_no_media"),
                True,
            )
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
    total=4,
    active=0,
    unchanged=True,
    exit_code=None,
    output=None,
    leave_session=False,
    cleanup_failure=False,
):
    from types import SimpleNamespace

    state = {"closed": False, "guard_closed": False}
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    code = 23 if invalid else 0
    if exit_code is not None:
        code = exit_code

    class Owned:
        def __init__(self, *_args, **_kwargs):
            self.operation = "launch_1"
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
                raise witness.common.WitnessError(
                    "child_failed",
                    witness.common.diagnostic(
                        "probe_admission",
                        outcome="child_failed",
                        gate_state="exited",
                        child_exit_code=code,
                        relay_exit_code=code,
                    ),
                )

        def close(self):
            state["closed"] = True
            if cleanup_failure:
                raise witness.common.WitnessError("cleanup_incomplete")

    class Guard:
        def __init__(self, path):
            assert path == sessions

        def unchanged(self):
            return unchanged

        def close(self):
            state["guard_closed"] = True

    monkeypatch.setattr(witness.common, "OwnedProcess", Owned)
    monkeypatch.setattr(witness, "DirectoryChangeGuard", Guard)
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
    )
    assert result == dict.fromkeys(
        ("invalid_config_refused", "invalid_config_no_session", "invalid_config_no_media"), True
    )
    assert state == {"closed": True, "guard_closed": True}


@pytest.mark.parametrize(
    "overrides",
    [
        {"total": 5},
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
        )
    assert state["closed"] and state["guard_closed"]


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
    for name in ("installed_alpha_launcher_witness.py", "installed_analytics_witness.py"):
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
