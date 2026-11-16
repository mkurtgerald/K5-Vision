"""Portable refusal tests for the installed-only Windows regression controller."""

import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def _module():
    path = Path(__file__).parents[1] / "integration/installed_integration_admission.py"
    spec = importlib.util.spec_from_file_location("installed_admission_review", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fixture(tmp_path):
    module = _module()
    installed = tmp_path / "installed"
    source = installed / "runtime/Lib/site-packages/k5vision/operator_integrations.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(
        (Path(__file__).parents[2] / "src/k5vision/operator_integrations.py").read_bytes()
    )
    manifest = installed / "payload-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "head_sha": "a" * 40,
                "files": [
                    {
                        "path": source.relative_to(installed).as_posix(),
                        "bytes": source.stat().st_size,
                        "sha256": module._sha(source),
                    }
                ],
            }
        )
    )
    return module, installed, source, manifest


def test_manifest_requires_exact_installed_bytes_and_source_identity(tmp_path):
    module, installed, source, manifest = _fixture(tmp_path)
    digest = module._sha(manifest)
    module._manifest(installed, digest, "a" * 40)
    original = source.read_bytes()
    source.write_bytes(original + b"\n")
    with pytest.raises(RuntimeError, match="qualification refused"):
        module._manifest(installed, digest, "a" * 40)
    source.write_bytes(original)
    with pytest.raises(RuntimeError, match="qualification refused"):
        module._manifest(installed, digest, "b" * 40)
    manifest.write_text(manifest.read_text() + " ")
    with pytest.raises(RuntimeError, match="qualification refused"):
        module._manifest(installed, digest, "a" * 40)


def test_manifest_refuses_escaped_installed_file_even_with_valid_digest(tmp_path):
    module, installed, _, manifest = _fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.write_bytes(b"synthetic")
    value = json.loads(manifest.read_text())
    value["files"].append({"path": "../outside", "bytes": 9, "sha256": module._sha(outside)})
    manifest.write_text(json.dumps(value))
    with pytest.raises(RuntimeError, match="qualification refused"):
        module._manifest(installed, module._sha(manifest), "a" * 40)


@pytest.mark.parametrize("count", [0, 16, 17, 18])
def test_only_exactly_seventeen_completed_calls_are_accepted(count):
    results = _module()._Results()
    results.collected = [
        f"test_operator_integration_admission_processes.py::case{i}" for i in range(count)
    ]
    results.calls = list(results.collected)
    assert results.accepted() is (count == 17)
    if count == 17:
        results.calls[-1] = results.calls[0]
        assert not results.accepted()


@pytest.mark.parametrize(
    "when,failed,skipped",
    [("setup", True, False), ("call", False, True), ("teardown", True, False)],
)
def test_fixture_failure_skip_or_teardown_failure_cannot_pass(when, failed, skipped):
    results = _module()._Results()
    results.collected = [
        f"test_operator_integration_admission_processes.py::case{i}" for i in range(17)
    ]
    results.calls = list(results.collected)
    results.pytest_runtest_logreport(
        SimpleNamespace(when=when, failed=failed, skipped=skipped, passed=False)
    )
    assert not results.accepted()


def test_current_source_checkout_is_not_an_installed_import(tmp_path):
    with pytest.raises(RuntimeError, match="qualification refused"):
        _module()._imports(tmp_path)


def test_controller_binds_the_unchanged_regressions():
    module = _module()
    path = Path(__file__).with_name("test_operator_integration_admission_processes.py")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == module._TEST_SHA


def test_external_dependencies_are_an_explicit_allowlist(tmp_path):
    module = _module()
    destination = tmp_path / "test-dependencies"
    module._stage_test_dependencies(destination)
    expected = {
        "pytest",
        "_pytest",
        "py.py",
        "pluggy",
        "iniconfig",
        "packaging",
        "pygments",
        "httpx",
        "httpcore",
        "certifi",
    }
    if module.os.name == "nt":
        expected.add("colorama")
    assert {path.name for path in destination.iterdir()} == expected
    assert not list(destination.glob("*.pth"))
    assert not (destination / "k5vision").exists()
