"""Portable witness harness tests; these never claim Windows native acceptance."""

from __future__ import annotations

import importlib.util
import os
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "installed_witness", ROOT / "scripts/installed_analytics_witness.py"
)
assert SPEC is not None and SPEC.loader is not None
witness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(witness)
REVISION = "a" * 40


def live() -> dict[str, object]:
    return {
        "schema_version": "2",
        "completed": True,
        "delivered_frames": 225,
        "presentations": 225,
        "processed_controls": 0,
        "analytics_enabled": True,
        "analytics_provider_submissions": 40,
        "analytics_provider_completions": 39,
        "analytics_failures": 0,
        "analytics_rendered_boxes": 120,
    }


def receipt() -> tuple[dict[str, object], dict[str, str]]:
    identities = {key: witness.digest(key) for key in witness.IDENTITIES}
    result = {
        "schema_version": "1",
        "revision": REVISION,
        "analytics_revision": witness.ANALYTICS_REVISION,
        "execution_context": "installed-normal-app-reviewed-loopback-windows-x64",
        "completed": True,
        "cleanup_complete": True,
        "service_token_rejected": True,
        "stage": "complete",
        "failure_code": "none",
        **identities,
    }
    for attempt in (1, 2):
        result.update(
            {
                f"run_{attempt}_{key}": value
                for key, value in live().items()
                if key != "schema_version"
            }
        )
    return result, identities


def test_strict_scalar_receipt_and_cli(tmp_path: Path) -> None:
    result, identities = receipt()
    witness.validate_receipt(result, revision=REVISION, identities=identities)
    path = tmp_path / "receipt.json"
    path.write_bytes(witness.canonical(result))
    child = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            str(ROOT / "scripts/installed_analytics_witness.py"),
            "--validate-receipt",
            "--output",
            str(path),
            "--revision",
            REVISION,
        ],
        capture_output=True,
        timeout=5,
    )
    assert child.returncode == 0
    assert (
        child.stdout.replace(b"\r\n", b"\n") == b"Installed analytics receipt validation passed\n"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("revision", "b" * 40),
        ("analytics_revision", "b" * 40),
        ("analytics_manifest_sha256", "b" * 64),
        ("analytics_wheel_sha256", "b" * 64),
        ("k5_payload_sha256", "b" * 64),
        ("k5_wheel_sha256", "b" * 64),
        ("runtime_identity_sha256", "b" * 64),
        ("model_identity_sha256", "b" * 64),
        ("seed_identity_sha256", "b" * 64),
        ("native_identity_sha256", "b" * 64),
        ("cleanup_complete", False),
        ("service_token_rejected", False),
        ("completed", 1),
        ("stage", "C:\\Users\\private"),
        ("failure_code", "raw native exception"),
        ("run_1_delivered_frames", True),
        ("run_2_delivered_frames", 224),
        ("run_2_analytics_provider_completions", 0),
        ("run_2_analytics_rendered_boxes", 0),
        ("run_2_analytics_enabled", False),
        ("run_1_analytics_failures", 1),
        ("run_1_presentations", 1_000_001),
        ("run_1_presentations", "225"),
    ],
)
def test_rejects_stale_identity_and_malformed_receipt(field: str, value: object) -> None:
    result, identities = receipt()
    result[field] = value
    with pytest.raises(witness.WitnessError):
        witness.validate_receipt(result, revision=REVISION, identities=identities)


@pytest.mark.parametrize(
    "value",
    [
        b'{"x": 1, "x": 2}',
        b'{"x": NaN}',
        b'{"x": Infinity}',
        b"[]",
        b"{",
        b"x" * (witness.MAX_BYTES + 1),
    ],
)
def test_rejects_malformed_json(value: bytes) -> None:
    with pytest.raises(witness.WitnessError):
        witness.parse_json(value)


def test_rejects_unknown_or_nested_receipt_fields() -> None:
    result, identities = receipt()
    result["source_uri"] = "rtsp://private"
    with pytest.raises(witness.WitnessError):
        witness.validate_receipt(result, revision=REVISION, identities=identities)
    del result["source_uri"]
    result["model_identity_sha256"] = {"path": "private"}
    with pytest.raises(witness.WitnessError):
        witness.validate_receipt(result, revision=REVISION, identities=identities)


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", "1"),
        ("analytics_failures", True),
        ("analytics_rendered_boxes", -1),
        ("analytics_provider_completions", 41),
        ("analytics_enabled", 1),
    ],
)
def test_live_receipt_fail_closed(field: str, value: object) -> None:
    result = live()
    result[field] = value
    with pytest.raises(witness.WitnessError):
        witness.validate_live_receipt(result)


def test_missing_dependency_or_wrong_version(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(_name: str) -> str:
        raise witness.importlib.metadata.PackageNotFoundError

    monkeypatch.setattr(witness.importlib.metadata, "version", missing)
    with pytest.raises(witness.WitnessError, match="dependency_missing"):
        witness.require_runtime_versions(witness.RUNTIME_VERSIONS)
    monkeypatch.setattr(witness.importlib.metadata, "version", lambda _name: "0.0")
    with pytest.raises(witness.WitnessError, match="identity_mismatch"):
        witness.require_runtime_versions(witness.RUNTIME_VERSIONS)


@pytest.mark.parametrize(
    "environment", [{}, {"K5_ANALYTICS_CONFIG": ""}, {"K5_ANALYTICS_CONFIG": " "}]
)
def test_missing_config_fails_closed(environment: dict[str, str]) -> None:
    with pytest.raises(witness.WitnessError, match="configuration_missing"):
        witness.require_config_environment(environment)


def test_environment_clears_checkout_private_sources_and_retention() -> None:
    source = {
        "PATH": "retained-tools",
        "PYTHONPATH": "checkout",
        "PYTHONHOME": "other",
        "PYTHONSTARTUP": "inject.py",
        "K5_STAGE03_SOURCE": "private",
        "K5_STAGE03_CAM_CRED": "secret",
        "CAM_CRED": "secret",
        "K5_STAGE_ONE_RECORDING_ROOT": "private",
        "K5_ANALYTICS_CONFIG": "stale",
        "K5_USER_DB_PATH": "persistent",
        "MTX_RTSPADDRESS": "0.0.0.0:554",
        "GST_PLUGIN_PATH": "foreign",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "remote",
    }
    clean = witness.clean_environment(source)
    assert clean["PATH"] == "retained-tools"
    assert clean["PYTHONPATH"] == clean["PYTHONHOME"] == ""
    assert clean["PYTHONNOUSERSITE"] == "1"
    assert not any(key.startswith(("K5_", "MTX_", "GST_", "OTEL_")) for key in clean)
    assert "CAM_CRED" not in clean and "PYTHONSTARTUP" not in clean
    assert source["K5_USER_DB_PATH"] == "persistent"


def test_existing_listener_is_never_killed() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        with pytest.raises(witness.WitnessError, match="port_occupied"):
            witness.require_free_port(port)
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            connection, _ = listener.accept()
            connection.close()


def test_bounded_child_timeout_cleans_only_owned_process(tmp_path: Path) -> None:
    external = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    owned = witness.OwnedProcess(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        env=witness.clean_environment(dict(os.environ)),
        operation="build_k5_wheel",
    )
    handle = owned.process
    try:
        with pytest.raises(witness.WitnessError, match="child_timeout"):
            owned.wait(0.05)
        owned.close()
        assert handle.poll() is not None
        assert external.poll() is None
        owned.close()  # Idempotent ownership release, no PID rediscovery.
    finally:
        owned.close()
        external.terminate()
        external.wait(timeout=5)


def test_output_capture_bound_and_timeout_are_not_retained(tmp_path: Path) -> None:
    env = witness.clean_environment(dict(os.environ))
    with pytest.raises(witness.WitnessError, match="output_invalid"):
        witness.capture(
            [sys.executable, "-c", "print('sensitive' * 10000)"],
            cwd=tmp_path,
            env=env,
            limit=100,
            operation="build_k5_wheel",
        )
    assert list(tmp_path.iterdir()) == []


def test_plugin_inventory_requires_exact_native_identity(tmp_path: Path) -> None:
    plugin = tmp_path / "plugin.dll"
    plugin.write_bytes(b"native fixture")

    def inventory(version="1.28.7", license_name="LGPL", filename=plugin) -> bytes:
        return (
            f"Factory Details:\n  Name tcpclientsrc\nPlugin Details:\n  Name tcp\n"
            f"  Filename {filename}\n  Version {version}\n  License {license_name}\n"
            "  Source module gst-plugins-base\n"
        ).encode()

    expected = ("tcp", "gst-plugins-base", "LGPL")
    assert witness.inspect_plugin(inventory(), expected, tmp_path) == witness.file_hash(plugin)
    for raw in (inventory(version="1.28.6"), inventory(license_name="unknown"), b"raw exception"):
        with pytest.raises(witness.WitnessError):
            witness.inspect_plugin(raw, expected, tmp_path)


def test_http_authentication_uses_real_control_plane(tmp_path: Path) -> None:
    # Portable contract test of real Uvicorn/auth/enrollment; this deliberately
    # does not claim installed/native analytics. No provider or launcher injection.
    env = witness.clean_environment(dict(os.environ))
    env.update(
        PYTHONPATH=str(ROOT / "src"),
        K5_CONTROL_PLANE_SITE_ID="disposable-harness",
        K5_DEVICE_DB_PATH=str(tmp_path / "devices.sqlite3"),
        K5_USER_DB_PATH=str(tmp_path / "users.sqlite3"),
        K5_CONTROL_PLANE_TOKEN="service-harness-token",
        K5_CONTROL_PLANE_ADMIN_TOKEN="admin-harness-token",
    )
    port = witness.free_port()
    owned = witness.OwnedProcess(
        [
            sys.executable,
            "-m",
            "k5vision.cli",
            "serve",
            "--port",
            str(port),
            "--log-level",
            "critical",
        ],
        cwd=tmp_path,
        env=env,
        operation="build_k5_wheel",
    )
    try:
        witness.wait_ready(
            lambda: witness.health_ready(port), [owned], 15, operation="wait_application"
        )
        session, body = witness.authenticate(
            port, "admin-harness-token", "service-harness-token", 8554
        )
        assert session and body["stream_token"] == "local-test"
        # Human authentication reaches the ordinary unconfigured-launcher gate;
        # authenticate() separately established service-token401 on the same API.
        code, _ = witness.api(port, "/api/v1/operator/live", body=body, token=session)
        assert code == 503
    finally:
        owned.close()


def test_no_success_receipt_on_non_windows_and_stale_receipt_removed(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("Portable non-Windows failure case")
    output = tmp_path / "evidence.json"
    output.write_text("old success")
    arguments = SimpleNamespace(
        output=output, revision=REVISION, repo=ROOT, analytics_source=ROOT, temp_root=tmp_path
    )
    assert witness.execute(arguments) == 1
    result = witness.read_json(output)
    assert result["completed"] is False and result["failure_code"] == "admission_failed"
    assert result["cleanup_complete"] is True
    witness.validate_receipt(
        result,
        revision=REVISION,
        identities={key: result[key] for key in witness.IDENTITIES},
        success=False,
    )
    assert b"old success" not in output.read_bytes()


def test_no_product_injection_or_workflow_mutation_in_entrypoint() -> None:
    text = (ROOT / "scripts/installed_analytics_witness.py").read_text()
    assert '"k5vision.cli"' in text and '"--operator"' in text
    assert "TestClient" not in text and "detection_provider=" not in text
    assert "taskkill" not in text and "Stop-Process" not in text
    entry = (ROOT / "scripts/windows-alpha/Invoke-K5InstalledAnalyticsWitness.ps1").read_text()
    assert "--analytics-source" in entry and "--revision" in entry
    assert "workflow_dispatch" not in entry and "Install-K5VisionAlpha" not in entry


def test_child_execution_gate_closes_on_eof(tmp_path: Path) -> None:
    marker = tmp_path / "executed"
    command = [
        sys.executable,
        "-I",
        "-B",
        str(ROOT / "scripts/installed_analytics_witness.py"),
        "--gate",
        "a" * 32,
        sys.executable,
        "-c",
        f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
    ]
    refused = subprocess.run(command, input=b"", capture_output=True, timeout=5)
    assert refused.returncode == 1 and not marker.exists()
    admitted = subprocess.run(command, input=b"1", capture_output=True, timeout=5)
    assert admitted.returncode == 0 and marker.read_text() == "ran"


def test_owned_job_assignment_precedes_child_execution(tmp_path: Path, monkeypatch) -> None:
    assigned = tmp_path / "assigned"
    executed = tmp_path / "executed"

    class FakeJob:
        def assign(self, process):
            self.process = process
            assert not executed.exists()
            assigned.write_text("assigned")

        def close(self):
            if self.process.poll() is None:
                self.process.terminate()

    monkeypatch.setattr(witness, "WindowsJob", FakeJob)
    monkeypatch.setattr(witness, "os", SimpleNamespace(name="nt"))
    env = dict(os.environ)
    process = witness.OwnedProcess(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; "
            f"assert Path({str(assigned)!r}).exists(); Path({str(executed)!r}).write_text('ran')",
        ],
        cwd=tmp_path,
        env=env,
        operation="build_k5_wheel",
    )
    try:
        process.wait(5)
        assert executed.read_text() == "ran"
    finally:
        process.close()


def test_assignment_failure_never_runs_requested_command(tmp_path: Path, monkeypatch) -> None:
    marker = tmp_path / "executed"

    class RefusingJob:
        def assign(self, process):
            self.process = process
            raise witness.WitnessError("cleanup_incomplete")

        def close(self):
            self.process.terminate()

    monkeypatch.setattr(witness, "WindowsJob", RefusingJob)
    monkeypatch.setattr(witness, "os", SimpleNamespace(name="nt"))
    with pytest.raises(witness.WitnessError, match="cleanup_incomplete"):
        witness.OwnedProcess(
            [
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
            ],
            cwd=tmp_path,
            env=dict(os.environ),
            operation="build_k5_wheel",
        )
    assert not marker.exists()


def test_job_handle_closed_when_termination_fails() -> None:
    closed = []
    job = object.__new__(witness.WindowsJob)
    job.handle = 123
    job.api = SimpleNamespace(
        TerminateJobObject=lambda *_: False, CloseHandle=lambda handle: closed.append(handle)
    )
    with pytest.raises(witness.WitnessError, match="cleanup_incomplete"):
        job.close()
    assert closed == [123]


def test_pip_overrides_and_config_cannot_escape_disposable_install() -> None:
    env = witness.clean_environment(
        {
            "PIP_TARGET": "external",
            "PIP_PREFIX": "external",
            "PIP_USER": "true",
            "PIP_CACHE_DIR": "external",
            "PIP_CONFIG_FILE": "untrusted",
            "PIP_INDEX_URL": "foreign",
        }
    )
    assert env["PIP_CONFIG_FILE"] == os.devnull
    assert not set(env) & {"PIP_TARGET", "PIP_PREFIX", "PIP_USER", "PIP_CACHE_DIR", "PIP_INDEX_URL"}


def test_work_root_requires_owned_empty_directory(tmp_path: Path) -> None:
    parent = tmp_path / "temporary"
    parent.mkdir()
    work = parent / "owned"
    work.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    output = repo / "evidence.json"
    assert witness.adopt_work_root(work, parent, repo, output) == work
    (work / "other-work").write_text("preserve")
    for candidate in (work, parent, repo):
        with pytest.raises(witness.WitnessError, match="admission_failed"):
            witness.adopt_work_root(candidate, parent, repo, output)
    assert (work / "other-work").read_text() == "preserve"


def test_work_root_alias_and_receipt_special_files_rejected(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("Portable link/FIFO fixture")
    owned = tmp_path / "owned"
    owned.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(owned, target_is_directory=True)
    with pytest.raises(witness.WitnessError):
        witness.adopt_work_root(alias, tmp_path, tmp_path / "repo", tmp_path / "receipt")
    fifo = tmp_path / "receipt-fifo"
    os.mkfifo(fifo)
    with pytest.raises(witness.WitnessError, match="output_invalid"):
        witness.read_json(fifo)


@pytest.mark.parametrize(
    "raw,expected",
    [
        (
            b"NewConnectionError: getaddrinfo failed\nNo matching distribution found for hatchling",
            "dns_failed",
        ),
        (b"NewConnectionError\nNo matching distribution found", "network_failed"),
        (b"CERTIFICATE_VERIFY_FAILED\nNo matching distribution found", "tls_failed"),
        (b"Analytics package admission manifest is invalid", "analytics_manifest_invalid"),
        (b"No module named pip", "pip_missing"),
        (b"not a valid object name: hidden", "git_revision_missing"),
        (b"DLL load failed: private path", "native_library_missing"),
        (b"no element unknown_plugin", "gst_element_unavailable"),
        (b"secret=private token=credential rtsp://private", "unclassified"),
    ],
)
def test_fixed_error_categories_do_not_copy_child_output(raw: bytes, expected: str) -> None:
    assert witness.classify_error(raw) == expected


@pytest.mark.parametrize(
    "field,value",
    [
        ("operation", "C:\\Users\\private"),
        ("category", "raw exception rtsp://private"),
        ("child_exit_code", True),
        ("relay_exit_code", 2**32),
        ("timed_out", 1),
        ("gate_state", "private-host"),
        ("outcome", {"nested": "private"}),
    ],
)
def test_diagnostic_schema_rejects_source_values(field: str, value: object) -> None:
    detail = witness.diagnostic("build_k5_wheel")
    detail[field] = value
    with pytest.raises(ValueError, match="invalid diagnostic"):
        witness.validate_diagnostic(detail)


def test_stderr_summary_is_bounded_and_forged_markers_are_ignored() -> None:
    import io

    nonce = "a" * 32
    raw = (
        b"getaddrinfo failed\nsecret=unretained\n"
        + b"x" * 200_000
        + b"\nK5_GATE_wrong_EXIT=0\nK5_CHILD_EXIT=0\n"
        + b"K5_CHILD_CATEGORY=not_allowlisted_private_value\n"
        + f"K5_GATE_{nonce}_EXIT=2\n".encode()
    )
    summary = witness.StderrSummary(io.BytesIO(raw), gated=True, nonce=nonce)
    assert summary.finish()
    assert summary.bytes_classified == witness._MAX_STDERR_CLASSIFIED
    assert summary.child_exit_code == 2 and summary.gate_state == "exited"
    assert summary.category == "dns_failed"
    assert not any(
        "unretained" in str(value) or "private_value" in str(value)
        for value in vars(summary).values()
    )


def test_child_failure_has_fixed_operation_and_numeric_exit_only(tmp_path: Path, capsys) -> None:
    with pytest.raises(witness.WitnessError) as caught:
        witness.run(
            [
                sys.executable,
                "-c",
                "import sys; "
                "sys.stderr.write('private-url token=secret\\nNo module named pip'); sys.exit(2)",
            ],
            cwd=tmp_path,
            env=witness.clean_environment(dict(os.environ)),
            operation="create_venv",
        )
    detail = caught.value.diagnostic
    assert detail["operation"] == "create_venv"
    assert detail["child_exit_code"] == 2
    assert detail["category"] == "pip_missing"
    assert detail["timed_out"] is False
    witness.emit_diagnostic(detail)
    output = capsys.readouterr()
    assert "K5_INSTALLED_DIAGNOSTIC=" in output.out
    assert "secret" not in output.out + output.err and "private-url" not in output.out + output.err
    assert list(tmp_path.iterdir()) == []


def test_child_timeout_diagnostic_remains_bounded(tmp_path: Path) -> None:
    with pytest.raises(witness.WitnessError) as caught:
        witness.run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=tmp_path,
            env=witness.clean_environment(dict(os.environ)),
            operation="probe_before",
            seconds=0.05,
        )
    assert caught.value.diagnostic["operation"] == "probe_before"
    assert caught.value.diagnostic["outcome"] == "timeout"
    assert caught.value.diagnostic["timed_out"] is True


def test_readiness_timeout_identifies_boundary() -> None:
    with pytest.raises(witness.WitnessError) as caught:
        witness.wait_ready(lambda: False, [], 0, operation="wait_rtsp_publication")
    assert caught.value.diagnostic == witness.diagnostic(
        "wait_rtsp_publication", outcome="timeout", timed_out=True, category="readiness_timeout"
    )


@pytest.mark.parametrize(
    "target,exit_code,state,category",
    [
        (
            [sys.executable, "-c", "import sys; sys.stderr.write('without newline'); sys.exit(2)"],
            2,
            "exited",
            "unclassified",
        ),
        (
            [sys.executable, "-c", "import sys; sys.stderr.write('without newline')"],
            0,
            "exited",
            "unclassified",
        ),
        (["k5-intentionally-absent-executable-74e6"], None, "launch_failed", "executable_missing"),
    ],
)
def test_gate_distinguishes_target_exit_from_launch_and_frames_no_newline(
    target, exit_code, state, category, tmp_path: Path
) -> None:
    nonce = "b" * 32
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-B",
            str(ROOT / "scripts/installed_analytics_witness.py"),
            "--gate",
            nonce,
            *target,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        cwd=tmp_path,
    )
    summary = witness.StderrSummary(process.stderr, gated=True, nonce=nonce)
    process.stdin.write(b"1")
    process.stdin.close()
    result = process.wait(timeout=5)
    assert summary.finish()
    assert result == (exit_code if exit_code is not None else 1)
    assert summary.child_exit_code == exit_code
    assert summary.gate_state == state and summary.category == category


def test_gate_refusal_is_not_a_target_exit(tmp_path: Path) -> None:
    nonce = "c" * 32
    child = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-B",
            str(ROOT / "scripts/installed_analytics_witness.py"),
            "--gate",
            nonce,
            sys.executable,
            "-c",
            "raise SystemExit(0)",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        cwd=tmp_path,
    )
    summary = witness.StderrSummary(child.stderr, gated=True, nonce=nonce)
    assert child.wait(timeout=5) == 1 and summary.finish()
    assert summary.gate_state == "refused" and summary.child_exit_code is None


def test_both_archive_calls_override_hostile_autocrlf_without_host_changes(tmp_path: Path) -> None:
    import ast
    import hashlib
    import zipfile

    path = "src/k5vision/data/analytics-runtime-manifest.json"
    expected = subprocess.check_output(["git", "show", f"HEAD:{path}"], cwd=ROOT)
    env = dict(os.environ)
    # Per-child environment simulates the common Windows checkout setting;
    # neither repository nor global Git configuration is written.
    env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.autocrlf", GIT_CONFIG_VALUE_0="true")
    converted = subprocess.check_output(
        ["git", "-C", str(ROOT), "archive", "--format=zip", "HEAD", path], env=env, timeout=10
    )
    import io

    with zipfile.ZipFile(io.BytesIO(converted)) as archive:
        assert archive.read(path) != expected
    output = tmp_path / "exact.zip"
    subprocess.run(
        witness.archive_command(ROOT, output, "HEAD"),
        env=env,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
    )
    with zipfile.ZipFile(output) as archive:
        assert archive.read(path) == expected
    assert (
        hashlib.sha256(expected).hexdigest()
        == "62cd59b95380f429e7b77cfaf4855845ad96242ed6f2d2bafa2a1600cde1f3c2"
    )
    tree = ast.parse((ROOT / "scripts/installed_analytics_witness.py").read_text())
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "archive_command"
    ]
    assert len(calls) == 2


def test_every_bounded_child_and_readiness_call_has_a_fixed_operation() -> None:
    import ast

    tree = ast.parse((ROOT / "scripts/installed_analytics_witness.py").read_text())
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"run", "OwnedProcess", "capture", "wait_ready"}
        ):
            assert any(keyword.arg == "operation" for keyword in node.keywords)


@pytest.mark.parametrize("method", ["run", "capture"])
def test_transient_cleanup_failure_preserves_original_sanitized_diagnostic(
    monkeypatch, tmp_path: Path, capsys, method: str
) -> None:
    import io

    original = witness.diagnostic(
        "build_analytics_wheel",
        outcome="child_failed",
        child_exit_code=2,
        category="analytics_manifest_invalid",
    )

    class FakeOwned:
        def __init__(self, *_args, **_kwargs):
            self.operation = "build_analytics_wheel"
            self.process = SimpleNamespace(stdout=io.BytesIO(b""))

        def wait(self, _seconds):
            raise witness.WitnessError("child_failed", original)

        def close(self):
            raise witness.WitnessError("cleanup_incomplete")

    monkeypatch.setattr(witness, "OwnedProcess", FakeOwned)
    with pytest.raises(witness.WitnessError, match="cleanup_incomplete") as caught:
        getattr(witness, method)(
            ["unused"], cwd=tmp_path, env={}, operation="build_analytics_wheel"
        )
    assert caught.value.diagnostic["outcome"] == "cleanup_failed"
    assert (
        capsys.readouterr().out
        == "K5_INSTALLED_DIAGNOSTIC=" + witness.canonical(original).decode() + "\n"
    )


def test_build_postconditions_keep_their_exact_operation_context() -> None:
    import ast

    tree = ast.parse((ROOT / "scripts/installed_analytics_witness.py").read_text())
    execute = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "execute"
    )
    block = next(node for node in execute.body if isinstance(node, ast.Try)).body
    current = None
    observed = {}
    for statement in block:
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target = statement.targets[0]
            if isinstance(target, ast.Name) and target.id == "operation":
                if isinstance(statement.value, ast.Constant):
                    current = statement.value.value
            if isinstance(target, ast.Name) and target.id in {
                "k5_wheels",
                "analytics_wheels",
                "payload",
            }:
                observed[target.id] = current
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
            call = statement.value
            if isinstance(call.func, ast.Name) and call.func.id == "extract_archive":
                observed[ast.unparse(call.args[0])] = current
    assert observed == {
        "archive": "archive_candidate",
        "donor_archive": "archive_analytics",
        "k5_wheels": "build_k5_wheel",
        "analytics_wheels": "build_analytics_wheel",
        "payload": "rebuild_analytics_wheel",
    }


@pytest.mark.parametrize("mode", ["--probe", "--publish"])
@pytest.mark.parametrize(
    "code", ["dependency_missing", "identity_mismatch", "configuration_missing"]
)
def test_helper_modes_preserve_known_failure_codes_without_raw_output(
    monkeypatch, capsys, mode: str, code: str
) -> None:
    def fail(*_args):
        raise witness.WitnessError(code)

    monkeypatch.setattr(witness, "installed_probe" if mode == "--probe" else "publish", fail)
    monkeypatch.setattr(sys, "argv", ["installed_analytics_witness.py", mode])
    assert witness.main() == 1
    output = capsys.readouterr()
    assert output.err == f"K5_CHILD_CATEGORY={code}\n"
    assert output.out == "Installed analytics witness failed closed\n"
