"""Worker policy preserves the real package and admits only the pinned donor stub."""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import io
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

# Unchanged Apache-2.0 donor bytes; these fixtures do not provide an inference SDK.
_STUB = b'''# Copyright (C) 2018-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

class Telemetry(object):
    """
    Stab file for the Telemetry class which is used when Telemetry class is not available.
    """

    def __init__(self, *arg, **kwargs):
        pass

    def send_event(self, *arg, **kwargs):
        pass

    def send_error(self, *arg, **kwargs):
        pass

    def start_session(self, *arg, **kwargs):
        pass

    def end_session(self, *arg, **kwargs):
        pass

    def force_shutdown(self, *arg, **kwargs):
        pass

    def send_stack_trace(self, *arg, **kwargs):
        pass
'''
_UNAVAILABLE = "Local person usage policy is unavailable"


@pytest.fixture
def policy_runtime(tmp_path, monkeypatch):
    """Use actual Python modules/specs without importing a host's native runtime."""
    saved = {
        name: module
        for name, module in sys.modules.items()
        if name == "openvino"
        or name.startswith("openvino.")
        or name == "openvino_telemetry"
        or name.startswith("openvino_telemetry.")
    }
    for name in saved:
        del sys.modules[name]
    root = tmp_path / "site"
    runtime_init = root / "openvino/__init__.py"
    runtime_init.parent.mkdir(parents=True)
    runtime_init.write_text("raise AssertionError('native runtime must not be imported')\n")
    stub = root / "openvino/tools/ovc/telemetry_stub.py"
    stub.parent.mkdir(parents=True)
    stub.write_bytes(_STUB)
    package = root / "openvino_telemetry"
    package.mkdir()
    (package / "__init__.py").write_text(
        "from .main import Telemetry\n__version__ = Telemetry.get_version()\n"
    )
    (package / "main.py").write_text(
        "class Telemetry:\n"
        "    def __init__(self, *args, **kwargs):\n"
        "        raise AssertionError('real reporting constructor must not run')\n"
        "    @staticmethod\n"
        "    def get_version():\n"
        "        return '2025.2.0'\n"
    )
    backend = package / "backend"
    backend.mkdir()
    (backend / "__init__.py").write_text("")
    (backend / "backend_ga4.py").write_text("BACKEND_SENTINEL = 'real-module'\n")
    distributions = {
        "openvino": SimpleNamespace(version="2026.3.1", locate_file=lambda name: root / name),
        "openvino-telemetry": SimpleNamespace(
            version="2025.2.0", locate_file=lambda name: root / name
        ),
    }
    original_distribution = importlib.metadata.distribution

    def distribution(name):
        if name in distributions:
            return distributions[name]
        return original_distribution(name)

    monkeypatch.setattr(importlib.metadata, "distribution", distribution)
    monkeypatch.syspath_prepend(str(root))
    source = Path(__file__).resolve().parents[2] / "src/k5vision/operator_person_telemetry.py"
    spec = importlib.util.spec_from_file_location("_k5_policy_unit", source)
    policy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(policy)
    try:
        yield SimpleNamespace(
            policy=policy,
            root=root,
            stub=stub,
            distributions=distributions,
            configure=policy.configure_local_person_telemetry,
        )
    finally:
        for name in tuple(sys.modules):
            if (
                name == "openvino"
                or name.startswith("openvino.")
                or (name == "openvino_telemetry" or name.startswith("openvino_telemetry."))
            ):
                del sys.modules[name]
        sys.modules.update(saved)


def _reject(runtime):
    with pytest.raises(RuntimeError, match=f"^{_UNAVAILABLE}$") as caught:
        runtime.configure()
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True


def test_selects_only_noop_export_and_preserves_real_package_and_backend(policy_runtime):
    runtime = policy_runtime
    package = importlib.import_module("openvino_telemetry")
    original = package.Telemetry
    before = (package.__spec__, package.__file__, tuple(package.__path__), package.__version__)
    runtime.configure()
    assert sys.modules["openvino_telemetry"] is package
    after = (package.__spec__, package.__file__, tuple(package.__path__), package.__version__)
    assert after == before
    assert importlib.util.find_spec("openvino_telemetry") is before[0]
    assert package.Telemetry is not original
    assert importlib.import_module("openvino_telemetry.main").Telemetry is original
    assert original.get_version() == "2025.2.0"
    assert importlib.metadata.version("openvino-telemetry") == "2025.2.0"
    backend = importlib.import_module("openvino_telemetry.backend.backend_ga4")
    assert backend.BACKEND_SENTINEL == "real-module"
    assert importlib.util.find_spec(backend.__name__) is backend.__spec__
    assert "openvino" not in sys.modules
    bookkeeping = {"__module__", "__dict__", "__weakref__", "__doc__"}
    assert set(package.Telemetry.__dict__) - bookkeeping == {
        "__init__",
        "send_event",
        "send_error",
        "start_session",
        "end_session",
        "force_shutdown",
        "send_stack_trace",
    }
    instance = package.Telemetry("private-argument", ignored=True)
    for name in (
        "send_event",
        "send_error",
        "start_session",
        "end_session",
        "force_shutdown",
        "send_stack_trace",
    ):
        assert getattr(instance, name)("private-argument", ignored=True) is None
    assert not hasattr(instance, "backend")


@pytest.mark.parametrize("distribution", ["openvino", "openvino-telemetry"])
def test_rejects_unadmitted_distribution_version(policy_runtime, distribution):
    policy_runtime.distributions[distribution].version = "unadmitted-private-version"
    _reject(policy_runtime)
    assert "openvino_telemetry" not in sys.modules


@pytest.mark.parametrize("name", ["openvino", "openvino_telemetry"])
@pytest.mark.parametrize("defect", ["missing", "no-origin", "different-origin"])
def test_rejects_spec_distribution_disagreement(policy_runtime, monkeypatch, name, defect):
    original = importlib.util.find_spec

    def find_spec(requested, *args, **kwargs):
        if requested != name:
            return original(requested, *args, **kwargs)
        if defect == "missing":
            return None
        return SimpleNamespace(origin=None if defect == "no-origin" else "/private/wrong.py")

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)
    _reject(policy_runtime)


@pytest.mark.parametrize("defect", ["name", "file", "path"])
def test_rejects_loaded_package_identity_disagreement(policy_runtime, defect):
    package = importlib.import_module("openvino_telemetry")
    if defect == "name":
        package.__spec__.name = "other_package"
    elif defect == "file":
        package.__file__ = "/private/wrong.py"
    else:
        package.__path__ = []
    _reject(policy_runtime)


@pytest.mark.parametrize("defect", ["missing", "directory", "short", "corrupt", "symlink"])
def test_rejects_nonregular_or_unadmitted_stub_before_execution(policy_runtime, defect):
    stub = policy_runtime.stub
    if defect == "missing":
        stub.unlink()
    elif defect == "directory":
        stub.unlink()
        stub.mkdir()
    elif defect == "short":
        stub.write_bytes(_STUB[:-1])
    elif defect == "corrupt":
        # Valid Python with an observable side effect must fail hash admission first.
        marker = policy_runtime.root / "must-not-exist"
        payload = f"open({str(marker)!r}, 'w').write('executed')\n".encode()
        stub.write_bytes(payload + b"#" * (619 - len(payload)))
    else:
        target = stub.with_name("elsewhere.py")
        target.write_bytes(_STUB)
        stub.unlink()
        try:
            stub.symlink_to(target)
        except OSError:
            pytest.skip("host cannot create a symlink")
    _reject(policy_runtime)
    assert not (policy_runtime.root / "must-not-exist").exists()


def test_rejects_windows_reparse_leaf(policy_runtime, monkeypatch):
    original = Path.stat

    def stat(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path == policy_runtime.stub:
            return SimpleNamespace(
                st_mode=result.st_mode, st_size=result.st_size, st_file_attributes=0x400
            )
        return result

    monkeypatch.setattr(Path, "stat", stat)
    _reject(policy_runtime)


def test_private_dependency_error_is_projected_to_fixed_failure(policy_runtime, monkeypatch):
    def unavailable(_name):
        raise importlib.metadata.PackageNotFoundError("private-user-path-and-secret")

    monkeypatch.setattr(importlib.metadata, "distribution", unavailable)
    _reject(policy_runtime)


@pytest.mark.parametrize("loaded", [None, ModuleType("openvino")])
def test_first_selection_refuses_a_late_openvino_import(policy_runtime, loaded):
    sys.modules["openvino"] = loaded
    _reject(policy_runtime)
    assert "openvino_telemetry" not in sys.modules


def test_repeated_selection_remains_idempotent_after_runtime_import(policy_runtime):
    policy_runtime.configure()
    package = sys.modules["openvino_telemetry"]
    selected = package.Telemetry
    sys.modules["openvino"] = ModuleType("openvino")
    policy_runtime.configure()
    assert sys.modules["openvino_telemetry"] is package
    assert package.Telemetry is selected


@pytest.mark.parametrize("defect", ["module", "export", "spec", "path"])
def test_repeated_selection_rejects_identity_tampering(policy_runtime, defect):
    policy_runtime.configure()
    package = sys.modules["openvino_telemetry"]
    if defect == "module":
        replacement = ModuleType("openvino_telemetry")
        replacement.__dict__.update(package.__dict__)
        sys.modules["openvino_telemetry"] = replacement
    elif defect == "export":
        package.Telemetry = object()
    elif defect == "spec":
        package.__spec__ = importlib.util.spec_from_file_location(
            "openvino_telemetry", package.__file__
        )
    else:
        package.__path__ = ["/private/different-package"]
    _reject(policy_runtime)


def test_view_child_refuses_invalid_model_before_policy_or_detector(tmp_path, monkeypatch):
    from k5vision import operator_analytics_person as person
    from k5vision import operator_person_telemetry as telemetry
    from k5vision import operator_person_view_process as view

    # Admit package versions so the real validator must reject missing model bytes.
    versions = person._manifest()["runtime_versions"]
    monkeypatch.setattr(person.metadata, "version", versions.__getitem__)
    calls = []
    forbidden_calls = []
    original_validate = person.validate_person_runtime

    def validate(config):
        calls.append(config.artifact_root)
        original_validate(config)

    def forbidden(*args, **kwargs):
        forbidden_calls.append(True)
        raise AssertionError("invalid model reached policy or native Detector")

    monkeypatch.setattr(person, "validate_person_runtime", validate)
    monkeypatch.setattr(person, "_PersonDetector", forbidden)
    monkeypatch.setattr(telemetry, "configure_local_person_telemetry", forbidden)
    incoming = json.dumps({"artifact_root": str(tmp_path)}).encode() + b"\n"
    output = io.BytesIO()
    monkeypatch.setattr(view.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(incoming)))
    monkeypatch.setattr(view.sys, "stdout", SimpleNamespace(buffer=output))
    assert view._child() == 1
    assert calls == [tmp_path]
    assert forbidden_calls == []
    assert output.getvalue() == b'{"status":"failed"}\n'
