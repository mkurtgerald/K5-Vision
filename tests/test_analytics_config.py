from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import k5vision.analytics_config as config


@pytest.fixture
def configured(tmp_path, monkeypatch):
    root = tmp_path / "models"
    root.mkdir()
    path = tmp_path / "analytics.json"
    document = {
        "schema_version": 1,
        "provider": config.PROVIDER,
        "source_revision": config.ANALYTICS_REVISION,
        "artifact_root": str(root),
    }
    path.write_text(json.dumps(document))
    monkeypatch.setattr(config, "validate_analytics_runtime", lambda _: None)
    return path, root, document


def test_default_is_disabled_without_reading_dependencies(monkeypatch):
    monkeypatch.setattr(config, "_local_path", lambda *a, **kw: pytest.fail("filesystem access"))
    monkeypatch.setattr(
        config, "validate_installed_analytics", lambda: pytest.fail("package access")
    )
    assert config.load_analytics_configuration({}) is None


def test_explicit_local_config_is_bound_to_only_reviewed_provider(configured):
    path, root, _ = configured
    assert config.load_analytics_configuration({config.ANALYTICS_CONFIG_ENV: str(path)}) == (
        config.AnalyticsConfiguration(root)
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("schema_version", 2),
        ("provider", "arbitrary.module"),
        ("source_revision", "main"),
        ("artifact_root", "https://unapproved/models"),
        ("artifact_root", "relative"),
        ("artifact_root", "//server/models"),
        ("extra", "private-secret"),
    ],
)
def test_invalid_configuration_is_rejected_without_input_disclosure(configured, field, value):
    path, _, document = configured
    document[field] = value
    path.write_text(json.dumps(document))
    with pytest.raises(config.AnalyticsConfigurationError) as error:
        config.load_analytics_configuration({config.ANALYTICS_CONFIG_ENV: str(path)})
    assert str(error.value) == "Configured analytics input is invalid."


@pytest.mark.parametrize("text", ["[]", "null", "{", '{"provider":1,"provider":2}', "x" * 4097])
def test_malformed_duplicate_or_oversized_config_fails(configured, text):
    path, _, _ = configured
    path.write_text(text)
    with pytest.raises(config.AnalyticsConfigurationError):
        config.load_analytics_configuration({config.ANALYTICS_CONFIG_ENV: str(path)})


@pytest.mark.parametrize("value", ["", "missing", "https://invalid/config", "//server/config"])
def test_explicit_bad_config_does_not_silently_disable(value):
    with pytest.raises(config.AnalyticsConfigurationError):
        config.load_analytics_configuration({config.ANALYTICS_CONFIG_ENV: value})


def test_symlink_config_and_artifact_ancestors_refused(configured, tmp_path):
    path, root, document = configured
    link = tmp_path / "linked.json"
    link.symlink_to(path)
    with pytest.raises(config.AnalyticsConfigurationError):
        config.load_analytics_configuration({config.ANALYTICS_CONFIG_ENV: str(link)})
    parent_link = tmp_path / "linked-models"
    parent_link.symlink_to(root, target_is_directory=True)
    document["artifact_root"] = str(parent_link)
    path.write_text(json.dumps(document))
    with pytest.raises(config.AnalyticsConfigurationError):
        config.load_analytics_configuration({config.ANALYTICS_CONFIG_ENV: str(path)})


def test_path_parent_traversal_and_wrong_file_kind_refused(tmp_path):
    for path, directory in ((tmp_path / ".." / tmp_path.name, True), (tmp_path, False)):
        with pytest.raises(ValueError):
            config._local_path(str(path), directory=directory)


@pytest.fixture
def runtime_admission(tmp_path, monkeypatch):
    models = tmp_path / "models"
    models.mkdir()
    (models / "model.bin").write_bytes(b"model fixture")
    calls = []
    monkeypatch.setattr(config, "validate_installed_analytics", lambda: calls.append("package"))
    monkeypatch.setattr(config.metadata, "version", lambda name: config.RUNTIME_VERSIONS[name])
    artifact_module = SimpleNamespace(
        OPENVINO_OMZ_2023_FP16=(SimpleNamespace(relative_path="model.bin"),),
        verify_artifact_set=lambda root, specs: calls.append((root, specs)),
    )
    monkeypatch.setitem(sys.modules, "analytics_lab.artifacts", artifact_module)
    return config.AnalyticsConfiguration(models), calls, artifact_module


def test_package_versions_and_model_bytes_checked_before_native_import(runtime_admission):
    value, calls, artifacts = runtime_admission
    config.validate_analytics_runtime(value)
    assert calls == ["package", (value.artifact_root, artifacts.OPENVINO_OMZ_2023_FP16)]


@pytest.mark.parametrize("failure", ["package", "dependency", "model", "missing", "symlink"])
def test_runtime_admission_errors_are_sanitized(runtime_admission, monkeypatch, failure):
    value, _, artifacts = runtime_admission

    def reject(*args):
        raise RuntimeError("private-source-and-model-path")

    if failure == "package":
        monkeypatch.setattr(config, "validate_installed_analytics", reject)
    elif failure == "dependency":
        monkeypatch.setattr(config.metadata, "version", lambda _: "unreviewed")
    elif failure == "model":
        artifacts.verify_artifact_set = reject
    else:
        model = value.artifact_root / "model.bin"
        model.unlink()
        if failure == "symlink":
            target = value.artifact_root / "other.bin"
            target.write_bytes(b"model fixture")
            model.symlink_to(target)
    with pytest.raises(config.AnalyticsConfigurationError) as error:
        config.validate_analytics_runtime(value)
    assert str(error.value) == "Configured analytics runtime admission failed."


def test_config_failure_does_not_create_or_modify_files(configured):
    path, root, document = configured
    missing = root / "absent"
    document["artifact_root"] = str(missing)
    path.write_text(json.dumps(document))
    before = path.read_bytes()
    with pytest.raises(config.AnalyticsConfigurationError):
        config.load_analytics_configuration({config.ANALYTICS_CONFIG_ENV: str(path)})
    assert not missing.exists()
    assert path.read_bytes() == before


def test_windows_volume_refusal_precedes_filesystem_access(monkeypatch):
    monkeypatch.setattr(config, "_WINDOWS", True)

    def reject(_):
        raise ValueError("unavailable fixed local volume")

    monkeypatch.setattr(config, "check_windows_volume", reject)
    monkeypatch.setattr(Path, "lstat", lambda _: pytest.fail("untrusted path was accessed"))
    with pytest.raises(ValueError, match="fixed local volume"):
        config._local_path(str(Path.cwd() / "fixture"), directory=True)


def test_runtime_errors_propagate_as_failure_not_disabled(configured, monkeypatch):
    path, _, _ = configured

    def reject(_):
        raise config.AnalyticsConfigurationError("Configured analytics runtime admission failed.")

    monkeypatch.setattr(config, "validate_analytics_runtime", reject)
    with pytest.raises(config.AnalyticsConfigurationError):
        config.load_analytics_configuration({config.ANALYTICS_CONFIG_ENV: str(path)})
