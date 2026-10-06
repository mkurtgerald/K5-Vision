"""Generated archive/receipt integration; no native queries or package execution."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "capture_provenance", ROOT / "scripts/capture_installer_wheel_provenance.py"
)
assert SPEC and SPEC.loader
capture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(capture)


class Policy:
    principal = "S-1-5-21-1000"

    def volume(self, path):
        pass

    def admit(self, path, **kwargs):
        pass

    def capacity(self, path):
        return 100 * 1024**3, 50 * 1024**3

    def publish(self, source, destination, *, expected_source=None, expected_parent=None):
        assert not destination.exists()
        if expected_source is not None:
            info = source.stat()
            assert (info.st_dev, info.st_ino) == expected_source
            info = destination.parent.stat()
            assert (info.st_dev, info.st_ino) == expected_parent
        source.rename(destination)


@pytest.fixture
def bundle(tmp_path):
    tools = capture.load_tools()
    common, alpha = tools.alpha.common, tools.alpha
    work = tmp_path / "runner-work"
    pipeline = work / "K5-Vision"
    workspace = pipeline / "K5-Vision"
    temp = work / "_temp"
    launcher = temp / "k5-alpha-launcher-123-1"
    source = launcher / "source"
    source.mkdir(parents=True)
    (workspace / "artifacts").mkdir(parents=True)
    source_files = set(capture.MODULES) | {
        "windows-alpha/" + Path(p).name
        for p in tools.transaction.PAYLOAD_FILES
        if "/windows-alpha/" in p
    }
    source_files.add("provision-stage03-gstreamer.ps1")
    for name in source_files:
        target = source / "scripts" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = (ROOT / "scripts" / name).read_bytes()
        if name.endswith("runtime-requirements.txt"):
            raw = raw.replace(b"\r\n", b"\n")
        target.write_bytes(raw)
    # Fixture runtime uses canonical tracked text bytes, never checkout pyc files.
    for origin in (ROOT / "src/k5vision").rglob("*"):
        if origin.suffix not in {".py", ".json", ".txt"} or "__pycache__" in origin.parts:
            continue
        target = source / origin.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(origin.read_bytes().replace(b"\r\n", b"\n"))
    assert common.digest(alpha.source_payload(source)) == capture.RUNTIME_PAYLOAD_SHA256
    versions = {
        **tools.transaction.runtime_versions(
            source / "scripts/windows-alpha/runtime-requirements.txt"
        ),
        **common.RUNTIME_VERSIONS,
        **common.WINDOWS_RUNTIME_VERSIONS,
    }
    wheelhouse, built = launcher / "wheelhouse", launcher / "wheels"
    wheelhouse.mkdir()
    built.mkdir()

    def wheel(directory, name, version, requirements=()):
        escaped = name.replace("-", "_")
        path = directory / f"{escaped}-{version}-py3-none-any.whl"
        prefix = f"{escaped}-{version}.dist-info/"
        with zipfile.ZipFile(path, "w") as archive:
            metadata = (
                f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
                "Requires-Python: >=3.12\n"
            )
            metadata += "".join(f"Requires-Dist: {value}\n" for value in requirements)
            archive.writestr(prefix + "METADATA", metadata)
            archive.writestr(prefix + "WHEEL", "Wheel-Version: 1.0\nTag: py3-none-any\n")
            archive.writestr(prefix + "RECORD", "")
            if name == "k5-vision":
                for package_path in sorted((source / "src/k5vision").rglob("*")):
                    if package_path.is_file():
                        archive.writestr(
                            package_path.relative_to(source / "src").as_posix(),
                            package_path.read_bytes(),
                        )
            else:
                archive.writestr(escaped + "/__init__.py", "# generated fixture only\n")
        return path

    for name, version in versions.items():
        requirements = (
            ('pyreadline3>=3.5.4; sys_platform == "win32"',) if name == "onvif-python" else ()
        )
        wheel(wheelhouse, name, version, requirements)
    k5 = wheel(built, "k5-vision", "0.1.0", ("onvif-python==0.3.1",))
    analytics = wheel(built, "k5-analytics-runtime", "0.0.0+g" + common.ANALYTICS_REVISION)
    python_root = work / "admitted-python"
    python_root.mkdir()
    pip = wheel(python_root, "pip", "25.0.1")
    host = {
        "implementation": "cpython",
        "version": "3.12.10",
        "platform": "win_amd64",
        "executable_sha256": "e" * 64,
        "ensurepip_version": "25.0.1",
        "ensurepip_wheel_sha256": capture.sha256(pip),
    }
    identity = {
        "python": host,
        "bundled_pip": pip,
        "marker_environment": {
            "python_version": "3.12",
            "python_full_version": "3.12.10",
            "os_name": "nt",
            "sys_platform": "win32",
        },
    }
    expected = {
        "revision": "a" * 40,
        "run_nonce": "b" * 32,
        **{name: common.digest(name) for name in alpha.IDENTITIES},
    }
    expected.update(
        source_tree_sha256=common.digest(alpha.tree_manifest(source)),
        k5_payload_sha256=common.digest(alpha.source_payload(source)),
        k5_wheel_sha256=capture.sha256(k5),
        analytics_wheel_sha256=capture.sha256(analytics),
        analytics_manifest_sha256=capture.sha256(
            source / "src/k5vision/data/analytics-runtime-manifest.json"
        ),
        wheelhouse_sha256=common.digest(alpha.tree_manifest(wheelhouse)),
    )
    for name in ("start", "test", "run"):
        expected[name + "_script_sha256"] = capture.sha256(
            source / "scripts/windows-alpha" / (name.title() + "-K5VisionAlpha.ps1")
        )

    def receipts():
        (launcher / "inputs.json").write_bytes(
            common.canonical({k: v for k, v in expected.items() if k != "runtime_identity_sha256"})
        )
        (launcher / alpha.EXPECTATIONS_NAME).write_bytes(common.canonical(expected))
        receipt = alpha.new_receipt(expected)
        receipt.update({name: True for name in alpha.BOOLEANS if name != "person_box_acceptance"})
        receipt.update(stage="complete", failure_code="none")
        for attempt in (1, 2):
            for name in alpha.RUN_BOOLEANS:
                receipt[f"run_{attempt}_{name}"] = True
            for name, value in {
                "delivered_frames": 225,
                "presentations": 225,
                "analytics_provider_submissions": 10,
                "analytics_provider_completions": 9,
                "analytics_failures": 0,
            }.items():
                receipt[f"run_{attempt}_{name}"] = value
        (workspace / "artifacts" / alpha.RECEIPT_NAME).write_bytes(common.canonical(receipt))
        normal = {
            "schema_version": "1",
            "revision": expected["revision"],
            "analytics_revision": common.ANALYTICS_REVISION,
            "execution_context": "installed-normal-app-reviewed-loopback-windows-x64",
            "completed": True,
            "cleanup_complete": True,
            "service_token_rejected": True,
            "stage": "complete",
            "failure_code": "none",
            **{key: expected.get(key, common.digest(key)) for key in common.IDENTITIES},
        }
        for attempt in (1, 2):
            for name in common.RUN_FIELDS:
                value = True if name in {"completed", "analytics_enabled"} else 10
                if name in {"processed_controls", "analytics_failures"}:
                    value = 0
                if name in {"delivered_frames", "presentations"}:
                    value = 225
                if name == "analytics_provider_completions":
                    value = 9
                normal[f"run_{attempt}_{name}"] = value
        (workspace / "artifacts/installed-analytics-candidate.json").write_bytes(
            common.canonical(normal)
        )

    receipts()
    args = SimpleNamespace(
        launcher_root=launcher,
        workspace=workspace,
        runner_workspace=pipeline,
        runner_temp=temp,
        run_id="123",
        run_attempt="1",
        revision="a" * 40,
        base_python_sha256="e" * 64,
        receipt=workspace / "artifacts" / alpha.RECEIPT_NAME,
        normal_receipt=workspace / "artifacts/installed-analytics-candidate.json",
        output=workspace / "artifacts" / capture.OUTPUT_NAME,
    )
    return SimpleNamespace(
        args=args,
        tools=tools,
        policy=Policy(),
        identity=identity,
        launcher=launcher,
        source=source,
        wheelhouse=wheelhouse,
        expected=expected,
        k5=k5,
        receipts=receipts,
        wheel=wheel,
        storage=work / "k5-qualification-artifacts/K5-Vision",
    )


def execute(bundle):
    return capture.capture(
        bundle.args,
        tools=bundle.tools,
        storage_policy=bundle.policy,
        identity_reader=lambda: bundle.identity,
    )


def test_runtime_pin_matches_exact_tracked_git_blob_manifest():
    command = ["git", "--no-replace-objects", "--no-lazy-fetch", "-c", "protocol.allow=never"]
    entries = subprocess.check_output(
        [*command, "ls-files", "--stage", "-z", "--", "src/k5vision"],
        cwd=ROOT,
        timeout=10,
    ).split(b"\0")
    objects = []
    for entry in entries:
        if not entry:
            continue
        metadata, path = entry.split(b"\t", 1)
        mode, oid, stage = metadata.split()
        assert mode == b"100644" and stage == b"0"
        objects.append((oid, path.decode().removeprefix("src/")))
    assert objects
    raw = subprocess.check_output(
        [*command, "cat-file", "--batch"],
        input=b"".join(oid + b"\n" for oid, _ in objects),
        cwd=ROOT,
        timeout=10,
    )
    assert len(raw) <= 8 * 1024 * 1024
    manifest = {}
    for expected_oid, path in objects:
        header, raw = raw.split(b"\n", 1)
        oid, kind, size = header.split()
        assert oid == expected_oid and kind == b"blob"
        size = int(size)
        content, raw = raw[:size], raw[size:]
        assert len(content) == size and raw[:1] == b"\n"
        raw = raw[1:]
        assert hashlib.sha1(b"blob " + str(size).encode() + b"\0" + content).hexdigest() == (
            expected_oid.decode()
        )
        manifest[path] = hashlib.sha256(content).hexdigest()
    assert not raw
    independent = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert len(manifest) == 133
    assert capture.RUNTIME_REVISION == "f14fc48768dbb36599e9c311ae99d371c24c600a"
    assert independent == capture.RUNTIME_PAYLOAD_SHA256


def test_runtime_mutation_cannot_be_admitted_by_rebinding_caller_expectations(bundle):
    target = bundle.source / "src/k5vision/__init__.py"
    target.write_bytes(target.read_bytes() + b"\n# unreviewed runtime fixture mutation\n")
    common, alpha = bundle.tools.alpha.common, bundle.tools.alpha
    bundle.expected["source_tree_sha256"] = common.digest(alpha.tree_manifest(bundle.source))
    bundle.expected["k5_payload_sha256"] = common.digest(alpha.source_payload(bundle.source))
    with pytest.raises(capture.CaptureError, match="runtime_identity"):
        capture.source_identity(bundle.source, bundle.tools, bundle.expected)
    assert not bundle.storage.exists() and not bundle.args.output.exists()


def test_old_runtime_expectation_is_refused_before_retention(bundle):
    bundle.expected["k5_payload_sha256"] = (
        "c755ac54055c3d36ca12da20089f349b76477c7f1757c88b2cda9751160de92e"
    )
    with pytest.raises(capture.CaptureError, match="runtime_identity"):
        capture.source_identity(bundle.source, bundle.tools, bundle.expected)
    assert not bundle.storage.exists() and not bundle.args.output.exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("revision", "eccd0cb88c31e75c98d328ebb5fb7f0407ea5cd1"),
        ("revision", "b" * 40),
        ("payload_sha256", "c755ac54055c3d36ca12da20089f349b76477c7f1757c88b2cda9751160de92e"),
        ("payload_sha256", "c" * 64),
    ],
    ids=["old-revision", "other-revision", "old-payload", "other-payload"],
)
def test_old_or_changed_runtime_provenance_is_rejected(bundle, field, value):
    execute(bundle)
    provenance = json.loads(bundle.args.output.read_bytes())
    provenance["runtime"][field] = value
    with pytest.raises(capture.CaptureError, match="output_schema"):
        capture.validate_provenance(provenance)


def test_generated_full_capture_retains_originals_and_independent_subset(bundle):
    result = execute(bundle)
    assert result["archive_count"] == 36
    stored = bundle.storage / result["receipt_relative_path"]
    assert stored.read_bytes() == bundle.args.output.read_bytes()
    provenance = json.loads(stored.read_bytes())
    assert provenance["installer_accepted"] is False
    assert provenance["closure"]["qualified"]["wheel_count"] == 36
    assert provenance["closure"]["installer"]["wheel_count"] == 30
    assert len(provenance["installer_subset"]) == 30
    selected = {
        r["name"]
        for r in provenance["wheels"]
        if r["relative_path"] in provenance["installer_subset"]
    }
    assert "pyreadline3" in selected and "numpy" not in selected and "colorama" not in selected
    for record in provenance["wheels"]:
        copy = bundle.storage / result["bundle_key"] / record["relative_path"]
        assert capture.sha256(copy) == record["sha256"]
    assert bundle.launcher.exists()  # Existing producer owns its separate cleanup.
    assert str(bundle.launcher) not in stored.read_text()
    assert "S-1-5-21" not in stored.read_text()


@pytest.mark.parametrize("dependency_version", ["2.46.5", "2.46.6"])
def test_full_capture_retains_pydantic_with_observed_metadata_size(
    bundle, monkeypatch, dependency_version
):
    path = bundle.wheelhouse / "pydantic-2.13.5-py3-none-any.whl"
    with zipfile.ZipFile(path) as archive:
        files = {info.filename: archive.read(info) for info in archive.infolist()}
    member = "pydantic-2.13.5.dist-info/METADATA"
    raw = files[member] + f"Requires-Dist: pydantic-core=={dependency_version}\n\n".encode()
    raw += b"x" * (110178 - len(raw))
    files[member] = raw
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    bundle.expected["wheelhouse_sha256"] = bundle.tools.alpha.common.digest(
        bundle.tools.alpha.tree_manifest(bundle.wheelhouse)
    )
    bundle.receipts()
    read = zipfile.ZipFile.read

    def no_metadata_reread(archive, member, *args, **kwargs):
        name = member.filename if isinstance(member, zipfile.ZipInfo) else member
        assert not name.endswith(("/METADATA", "/WHEEL"))
        return read(archive, member, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "read", no_metadata_reread)
    if dependency_version != "2.46.5":
        with pytest.raises(bundle.tools.closure.ClosureError, match="dependency_version_mismatch"):
            execute(bundle)
        assert not bundle.storage.exists() and not bundle.args.output.exists()
        return
    result = execute(bundle)
    provenance = json.loads(bundle.args.output.read_bytes())
    record = next(record for record in provenance["wheels"] if record["name"] == "pydantic")
    for name in ("qualified", "installer"):
        closure = provenance["closure"][name]
        edge = next(edge for edge in closure["dependency_edges"] if edge["source"] == "pydantic")
        assert edge["target"] == "pydantic-core"
        assert edge["specifier"] == "==2.46.5"
        assert edge["active"] is True and edge["selected_version"] == "2.46.5"
    assert record["metadata_sha256"] == capture.hashlib.sha256(raw).hexdigest()
    retained = bundle.storage / result["bundle_key"] / record["relative_path"]
    assert retained.read_bytes() == path.read_bytes()
    assert provenance["closure"]["qualified"]["wheel_count"] == 36
    assert provenance["closure"]["installer"]["wheel_count"] == 30
    assert provenance["installer_accepted"] is False


@pytest.mark.parametrize(
    "fault", ["source", "wheel", "missing", "extra", "revision", "receipt", "base", "pip"]
)
def test_input_admission_refuses_before_retention(bundle, fault):
    if fault == "source":
        (bundle.source / "scripts/installer_wheel_requirements.py").write_bytes(b"wrong")
    elif fault == "wheel":
        bundle.k5.write_bytes(b"different archive")
    elif fault == "missing":
        next(bundle.wheelhouse.glob("*.whl")).unlink()
    elif fault == "extra":
        (bundle.wheelhouse / "unadmitted.whl").write_bytes(b"no")
    elif fault == "revision":
        bundle.args.revision = "c" * 40
    elif fault == "receipt":
        receipt = json.loads(bundle.args.receipt.read_bytes())
        receipt["completed"] = False
        bundle.args.receipt.write_text(json.dumps(receipt))
    elif fault == "base":
        bundle.args.base_python_sha256 = "d" * 64
    elif fault == "pip":
        bundle.identity["python"]["ensurepip_wheel_sha256"] = "d" * 64
    with pytest.raises((capture.CaptureError, bundle.tools.alpha.common.WitnessError)):
        execute(bundle)
    assert not bundle.storage.exists() and not bundle.args.output.exists()


@pytest.mark.parametrize(
    "requirement",
    [
        "pyreadline3>=3.5.7; sys_platform == 'win32'",
        "numpy>=2.0",
        "untrusted @ https://invalid.example/secret-canary ; extra == 'unused'",
    ],
)
def test_installer_subset_and_unsupported_metadata_are_independent_gates(bundle, requirement):
    bundle.wheel(bundle.wheelhouse, "onvif-python", "0.3.1", (requirement,))
    bundle.expected["wheelhouse_sha256"] = bundle.tools.alpha.common.digest(
        bundle.tools.alpha.tree_manifest(bundle.wheelhouse)
    )
    bundle.receipts()
    error = capture.CaptureError if "https://" in requirement else bundle.tools.closure.ClosureError
    with pytest.raises(error):
        execute(bundle)
    assert not bundle.storage.exists() and not bundle.args.output.exists()


def test_existing_report_is_not_deleted_or_overwritten(bundle):
    bundle.args.output.write_bytes(b"other work")
    with pytest.raises(capture.CaptureError, match="output_exists"):
        execute(bundle)
    assert bundle.args.output.read_bytes() == b"other work"
    assert not bundle.storage.exists()


def test_storage_refusal_propagates_without_new_destination(bundle):
    bundle.policy.capacity = lambda path: (100 * 1024**3, 1)
    with pytest.raises(bundle.tools.storage.StorageError):
        execute(bundle)
    assert not bundle.storage.exists() and not bundle.args.output.exists()


def test_host_identity_is_rechecked_before_retention(bundle):
    calls = []

    def identity():
        calls.append(True)
        value = dict(bundle.identity)
        if len(calls) > 1:
            value = {**value, "python": {**value["python"], "version": "3.12.11"}}
        return value

    with pytest.raises(capture.CaptureError, match="host_identity"):
        capture.capture(
            bundle.args, tools=bundle.tools, storage_policy=bundle.policy, identity_reader=identity
        )
    assert not bundle.storage.exists()


def test_wrong_runner_layout_fails_without_guessing_another_root(bundle):
    bundle.args.runner_workspace = bundle.args.runner_workspace.parent
    with pytest.raises(bundle.tools.storage.StorageError):
        execute(bundle)
    assert not bundle.storage.exists()


def test_collector_never_executes_packages_or_starts_processes():
    import ast

    tree = ast.parse((ROOT / "scripts/capture_installer_wheel_provenance.py").read_text())
    imports = {node.names[0].name for node in ast.walk(tree) if isinstance(node, ast.Import)}
    assert not {"subprocess", "urllib", "requests", "pip"} & imports
    text = ast.unparse(tree)
    assert "bootstrap(" not in text and "os.system(" not in text
    assert "k5vision.cli" not in text


def test_capture_hook_and_branch_route_keep_prior_admission_and_cleanup():
    text = (ROOT / ".github/workflows/installed-analytics-candidate.yml").read_text()
    branch = "fix/transactional-alpha-upgrade-20261003"
    assert f"      - {branch}" in text
    assert "@($legacyBranch, $launcherBranch, $upgradeBranch, $facadeBranch)" in text
    assert '$upgradeBranch = "' + branch + '"' in text
    assert "group: stage-one-operator-physical" in text and "cancel-in-progress: false" in text
    assert text.count("    runs-on:") == 2
    assert text.count("    runs-on: [self-hosted,") == 1
    native = text.split("\n  installed-analytics-candidate:\n", 1)[1]
    assert "needs: hosted_admission" in native
    assert "needs.hosted_admission.result == 'success'" in native
    assert "needs.hosted_admission.outputs.qualified_sha == github.sha" in native
    assert "if ($env:K5_CANDIDATE_BRANCH -ceq $legacyBranch)" in text
    for identifier in ("355859781", "369354936", "361865932", "362514400", "364801702"):
        assert identifier in text
    hook = text.index("- name: Retain exact qualified wheel provenance locally")
    assert text.index("- name: Validate exact source-free installed Start-script receipt") < hook
    assert hook < text.index("- name: Remove only this job's disposable fixture and controller")
    assert (
        "steps.safe_evidence.outcome == 'success' && steps.safe_alpha_evidence.outcome == 'success'"
        in text
    )
    assert "path: artifacts/installed-alpha-wheel-provenance.json" in text
    upload = text[text.index("- name: Upload source-free wheel provenance only") :]
    assert "*.whl" not in upload and "k5-qualification-artifacts" not in upload
    assert "$base -I -B -S" in text[hook:]


@pytest.mark.parametrize(
    "field",
    [
        "analytics_manifest_sha256",
        "analytics_wheel_sha256",
        "model_identity_sha256",
        "seed_identity_sha256",
    ],
)
def test_normal_receipt_must_share_deterministic_qualified_identities(bundle, field):
    normal = json.loads(bundle.args.normal_receipt.read_bytes())
    normal[field] = "f" * 64
    bundle.args.normal_receipt.write_text(json.dumps(normal))
    with pytest.raises(capture.CaptureError, match="receipt_identity"):
        execute(bundle)
    assert not bundle.storage.exists() and not bundle.args.output.exists()


@pytest.mark.parametrize(
    "private_marker",
    [
        "'/home/private-account/project'",
        "'C:/private-account/project'",
        "'https://invalid.example/private-account'",
    ],
)
def test_inactive_private_marker_is_refused_without_public_or_local_bundle(bundle, private_marker):
    bundle.wheel(
        bundle.wheelhouse, "onvif-python", "0.3.1", ("missing; sys_platform == " + private_marker,)
    )
    bundle.expected["wheelhouse_sha256"] = bundle.tools.alpha.common.digest(
        bundle.tools.alpha.tree_manifest(bundle.wheelhouse)
    )
    bundle.receipts()
    with pytest.raises(capture.CaptureError, match="metadata_privacy"):
        execute(bundle)
    assert not bundle.storage.exists() and not bundle.args.output.exists()


def test_inputs_are_rechecked_after_retention_before_json_upload(bundle):
    publish = bundle.policy.publish

    def changed(source, destination, **kwargs):
        publish(source, destination, **kwargs)
        (bundle.wheelhouse / "late-extra.whl").write_bytes(b"different unadmitted input")

    bundle.policy.publish = changed
    with pytest.raises(capture.CaptureError, match="input_changed"):
        execute(bundle)
    assert not bundle.args.output.exists()
    # Completed local bytes do not certify the outer qualification workflow.
    assert list(bundle.storage.glob("*/COMPLETE.json"))


def test_public_json_omits_raw_requirement_strings(bundle):
    execute(bundle)
    report = json.loads(bundle.args.output.read_bytes())
    assert all(
        "requires_dist" not in record and "requires_python" not in record
        for record in report["wheels"]
    )
    assert all("metadata_sha256" in record for record in report["wheels"])
    assert report["closure"]["marker_environment"]["sys_platform"] == "win32"


def test_existing_windows_gate_tracks_every_new_provenance_input_and_test():
    text = (ROOT / ".github/workflows/windows-alpha-script-smoke.yml").read_text()
    scripts = [
        "scripts/capture_installer_wheel_provenance.py",
        "scripts/installer_wheel_requirements.py",
        "scripts/installer_wheel_storage.py",
    ]
    tests = [
        "tests/test_capture_installer_wheel_provenance.py",
        "tests/test_installer_wheel_requirements.py",
        "tests/test_installer_wheel_storage.py",
        "tests/test_installer_runtime_platform_dependencies.py",
    ]
    pull = text.split("  pull_request:\n", 1)[1].split("  push:\n", 1)[0]
    push = text.split("  push:\n", 1)[1].split("\npermissions:", 1)[0]
    for path in scripts + tests:
        assert pull.count('      - "' + path + '"') == 1
        assert push.count('      - "' + path + '"') == 1
    boundary = text.split("- name: Run Windows alpha boundary regressions", 1)[1].split(
        "- name:", 1
    )[0]
    for path in tests:
        assert path in boundary
    for path in (
        "tests/test_windows_alpha_upgrade.py",
        "tests/test_app_resource_lifetime.py",
        "tests/test_windows_alpha_analytics.py",
        "tests/test_cli.py",
    ):
        assert path in boundary
    assert text.count("runs-on:") == 1 and "permissions:\n  contents: read" in text


@pytest.mark.parametrize("stage", sorted(capture.DIAGNOSTIC_STAGES - {"unknown"}))
def test_every_capture_stage_has_fixed_private_failure_context(bundle, monkeypatch, stage):
    diagnostic = capture.CaptureDiagnostics()
    original = diagnostic.enter

    def refuse(current, **fields):
        original(current, **fields)
        if current == stage:
            raise RuntimeError(
                "PRIVATE_CANARY C:\\account\\secret.whl https://private.invalid/token"
            )

    monkeypatch.setattr(diagnostic, "enter", refuse)
    with pytest.raises(RuntimeError) as caught:
        capture.capture(
            bundle.args,
            tools=bundle.tools,
            storage_policy=bundle.policy,
            identity_reader=lambda: bundle.identity,
            diagnostics=diagnostic,
        )
    record = capture.failure_diagnostic(caught.value, diagnostic, bundle.tools)
    assert record["stage"] == stage and record["error_class"] == "runtime_error"
    assert record["code"] == "capture_refused" and record["contract"] == stage
    assert not record["cleanup_known"] and not record["cleanup_pending"]
    expected = (
        "report_published"
        if stage == "complete"
        else "retained"
        if stage in {"post_retention_recheck", "retained_report", "report_publication"}
        else "not_started"
    )
    assert record["retention_state"] == expected
    encoded = json.dumps(record)
    assert "PRIVATE_CANARY" not in encoded and str(bundle.launcher) not in encoded
    assert "https:" not in encoded and "\\\\" not in encoded
    if stage == "wheel_metadata":
        assert record["package"] == "annotated-doc"
        assert record["role"] == "alpha_runtime" and record["wheel_index"] == 1


def test_retention_error_does_not_claim_absence_of_retained_state(bundle, monkeypatch):
    diagnostic = capture.CaptureDiagnostics()

    def uncertain(*args, **kwargs):
        raise OSError(5, "PRIVATE_CANARY /private/path")

    monkeypatch.setattr(bundle.tools.storage, "retain_bundle", uncertain)
    with pytest.raises(OSError) as caught:
        capture.capture(
            bundle.args,
            tools=bundle.tools,
            storage_policy=bundle.policy,
            identity_reader=lambda: bundle.identity,
            diagnostics=diagnostic,
        )
    record = capture.failure_diagnostic(caught.value, diagnostic, bundle.tools)
    assert record["stage"] == "retention" and record["retention_state"] == "in_progress"
    assert record["error_class"] == "io_error" and record["errno"] == 5
    assert record["cleanup_known"] is False
    assert "PRIVATE_CANARY" not in json.dumps(record)


def test_offline_and_storage_contract_details_are_bounded(bundle):
    diagnostic = capture.CaptureDiagnostics()
    diagnostic.enter("wheel_metadata", package="pip", role="ensurepip", wheel_index=34)
    transaction = bundle.tools.transaction
    error = transaction.OfflineAdmissionError("admission", expected=36, observed=35)
    record = capture.failure_diagnostic(error, diagnostic, bundle.tools)
    assert record["error_class"] == "offline_admission" and record["contract"] == "admission"
    assert record["expected"] == 36 and record["observed"] == 35
    assert record["package"] == "pip" and record["wheel_index"] == 34
    error.contract, error.expected, error.observed = "PRIVATE_CANARY", "/private/account", 2**100
    record = capture.failure_diagnostic(error, diagnostic, bundle.tools)
    assert record["contract"] == "wheel_metadata"
    assert record["expected"] is None and record["observed"] is None
    assert "PRIVATE_CANARY" not in json.dumps(record)
    diagnostic.enter("retention")
    diagnostic.retention("in_progress")
    error = bundle.tools.storage.StorageError("storage_identity", cleanup_pending=True)
    record = capture.failure_diagnostic(error, diagnostic, bundle.tools)
    assert record["cleanup_known"] and record["cleanup_pending"]
    assert record["code"] == "storage_identity" and record["retention_state"] == "in_progress"


def test_storage_acl_discriminator_is_revalidated_without_claiming_retention(bundle, monkeypatch):
    diagnostic = capture.CaptureDiagnostics()

    def refuse(*args, **kwargs):
        raise bundle.tools.storage.StorageError(
            "storage_acl",
            acl_diagnostic={
                "path_role": "ancestor",
                "path_context": "runner_workspace",
                "ancestor_distance": 1,
                "admission_mode": "ancestor",
                "phase": "ace_policy",
                "native_call": "GetNamedSecurityInfoW",
                "native_error": "success",
                "reason": "foreign_mutating_allow",
                "path": "PRIVATE_CANARY",
            },
        )

    monkeypatch.setattr(bundle.tools.storage, "derive_storage_root", refuse)
    with pytest.raises(bundle.tools.storage.StorageError) as caught:
        capture.capture(
            bundle.args,
            tools=bundle.tools,
            storage_policy=bundle.policy,
            identity_reader=lambda: bundle.identity,
            diagnostics=diagnostic,
        )
    record = capture.failure_diagnostic(caught.value, diagnostic, bundle.tools)
    assert record["stage"] == "storage_root" and record["retention_state"] == "not_started"
    assert record["cleanup_known"] and not record["cleanup_pending"]
    assert record["storage_acl"]["reason"] == "foreign_mutating_allow"
    assert record["storage_acl"]["ancestor_distance"] == 1
    assert not bundle.storage.exists() and not bundle.args.output.exists()
    assert "PRIVATE_CANARY" not in json.dumps(record)
    caught.value.acl_diagnostic.update(
        reason="PRIVATE_CANARY", native_error="PRIVATE_CANARY", path="PRIVATE_CANARY"
    )
    record = capture.failure_diagnostic(caught.value, diagnostic, bundle.tools)
    assert record["storage_acl"]["reason"] == "unknown"
    assert record["storage_acl"]["native_error"] == "unknown"
    assert "PRIVATE_CANARY" not in json.dumps(record)


@pytest.mark.parametrize(
    ("exception", "category"),
    [
        (FileNotFoundError("PRIVATE_CANARY"), "missing_file"),
        (PermissionError("PRIVATE_CANARY"), "permission_error"),
        (zipfile.BadZipFile("PRIVATE_CANARY"), "invalid_zip"),
        (KeyError("PRIVATE_CANARY"), "missing_key"),
        (ImportError("PRIVATE_CANARY"), "import_error"),
        (AttributeError("PRIVATE_CANARY"), "attribute_error"),
        (TypeError("PRIVATE_CANARY"), "type_error"),
        (ValueError("PRIVATE_CANARY"), "value_error"),
        (AssertionError("PRIVATE_CANARY"), "assertion_error"),
        (RuntimeError("PRIVATE_CANARY"), "runtime_error"),
        (KeyboardInterrupt("PRIVATE_CANARY"), "interrupted"),
    ],
)
def test_exception_categories_never_include_private_exception_data(exception, category):
    diagnostic = capture.CaptureDiagnostics()
    diagnostic.enter("retained_report")
    diagnostic.retention("retained")
    record = capture.failure_diagnostic(exception, diagnostic)
    assert record["error_class"] == category and record["retention_state"] == "retained"
    assert not record["cleanup_known"]
    assert "PRIVATE_CANARY" not in json.dumps(record)


def test_untrusted_diagnostic_fields_and_project_error_codes_do_not_escape(bundle):
    diagnostic = capture.CaptureDiagnostics()
    diagnostic.enter(
        "PRIVATE_CANARY",
        package="PRIVATE_CANARY",
        role="PRIVATE_CANARY",
        expected="PRIVATE_CANARY",
        observed=2**100,
        wheel_index=999,
    )
    for error in (
        capture.CaptureError("PRIVATE_CANARY"),
        bundle.tools.closure.ClosureError("PRIVATE_CANARY"),
        bundle.tools.alpha.common.WitnessError("PRIVATE_CANARY"),
    ):
        record = capture.failure_diagnostic(error, diagnostic, bundle.tools)
        assert record["stage"] == "unknown" and record["package"] is None
        assert record["role"] is None and record["wheel_index"] is None
        assert "PRIVATE_CANARY" not in json.dumps(record)


def test_alpha_validator_contract_is_projected_only_after_full_validation(bundle):
    alpha = bundle.tools.alpha
    diagnostic = capture.CaptureDiagnostics()
    diagnostic.enter("start_receipt_validate")
    error = alpha.AlphaWitnessError(alpha.alpha_diagnostic("admission", "input_schema"))
    record = capture.failure_diagnostic(error, diagnostic, bundle.tools)
    assert record["error_class"] == "alpha_contract" and record["contract"] == "input_schema"
    error.alpha_diagnostic["field"] = "PRIVATE_CANARY"
    record = capture.failure_diagnostic(error, diagnostic, bundle.tools)
    assert record["contract"] == "start_receipt_validate" and record["field"] is None
    assert "PRIVATE_CANARY" not in json.dumps(record)


def test_main_loading_error_emits_fixed_diagnostic_without_exception_text(monkeypatch, capsys):
    monkeypatch.setattr(
        capture.sys,
        "argv",
        ["capture"]
        + [
            item
            for name in (
                "launcher-root",
                "receipt",
                "normal-receipt",
                "workspace",
                "runner-workspace",
                "runner-temp",
                "output",
                "revision",
                "run-id",
                "run-attempt",
                "base-python-sha256",
            )
            for item in ("--" + name, "generated-placeholder")
        ],
    )

    def failed_load():
        raise RuntimeError("PRIVATE_CANARY C:\\private\\source.py")

    monkeypatch.setattr(capture, "load_tools", failed_load)
    assert capture.main() == 1
    output = capsys.readouterr().out
    assert output.startswith("K5_WHEEL_PROVENANCE_FAILED=")
    record = json.loads(output.split("=", 1)[1])
    assert record["stage"] == "tool_loading" and record["error_class"] == "runtime_error"
    assert record["retention_state"] == "not_started" and not record["cleanup_known"]
    assert "PRIVATE_CANARY" not in output


def test_failure_envelope_reprojects_nested_ace_without_identity_or_speculation(bundle):
    diagnostic = capture.CaptureDiagnostics()
    diagnostic.enter("storage_root")
    diagnostic.retention("not_started")
    value = {
        "phase": "ace_policy",
        "reason": "foreign_mutating_allow",
        "native_call": "GetNamedSecurityInfoW",
        "native_error": "success",
        "ancestor_distance": 2,
        "ace": {
            "ace_type": "allow",
            "principal_category": "builtin_users",
            "effective_principal_category": "builtin_users",
            "access_mask": 0x40000000,
            "expanded_access_mask": 0x00120116,
            "inheritance_flags": 0x13,
            "sid": "S-1-5-21-PRIVATE_CANARY",
        },
    }
    error = bundle.tools.storage.StorageError("storage_acl", acl_diagnostic=value)
    record = capture.failure_diagnostic(error, diagnostic, bundle.tools)
    assert record["storage_acl"]["schema_version"] == "storage-acl-admission-v2"
    assert record["storage_acl"]["ace"]["access_mask"] == 0x40000000
    assert record["retention_state"] == "not_started"
    assert record["cleanup_known"] and not record["cleanup_pending"]
    assert "PRIVATE_CANARY" not in json.dumps(record)
    error.acl_diagnostic["ace"]["access_mask"] = True
    error.acl_diagnostic["ace"]["principal_category"] = "PRIVATE_CANARY"
    reprojection = capture.failure_diagnostic(error, diagnostic, bundle.tools)
    assert reprojection["storage_acl"]["ace"]["access_mask"] is None
    assert reprojection["storage_acl"]["ace"]["expanded_access_mask"] is None
    assert reprojection["storage_acl"]["ace"]["principal_category"] == "unknown"
    error.acl_diagnostic.update(phase="descriptor_query", reason="descriptor_parse")
    assert capture.failure_diagnostic(error, diagnostic, bundle.tools)["storage_acl"]["ace"] is None
