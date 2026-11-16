"""Run the exact process regressions in the GUI-installed embedded interpreter.

Test dependencies stay outside the installation. Only finite evidence is returned;
raw pytest output, temporary database paths and fixture credentials are discarded.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

_TEST_HASHES = {
    "test_operator_integration_admission_processes.py": (
        "b1c9c17027c7b348cc52f2381301bea631ae0e33443607a76c8fd5a33249b7e9"
    ),
    "test_operator_pull_revocation_processes.py": (
        "1ca5741d6bd21a5c3e0716359fe2544a4143824b297c8a21b9c5841419693218"
    ),
}
_TEST_COUNTS = {
    "test_operator_integration_admission_processes.py": 17,
    "test_operator_pull_revocation_processes.py": 8,
}
_TOTAL_TESTS = sum(_TEST_COUNTS.values())
_SOURCE_SHA = "42f844f540b227d16cf9eec8f84ff5549a0fc80173a33c47716c9b247607e1ed"


def _sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _require(value):
    if not value:
        raise RuntimeError("Installed integration qualification refused")


def _manifest(installed, expected_manifest, expected_head):
    path = installed / "payload-manifest.json"
    _require(_sha(path) == expected_manifest)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    _require(manifest["head_sha"] == expected_head and manifest["schema_version"] == 1)
    entries = manifest["files"]
    _require(type(entries) is list and 0 < len(entries) <= 20000)
    for entry in entries:
        target = (installed / entry["path"]).resolve(strict=True)
        _require(target.is_relative_to(installed))
        _require(target.stat().st_size == entry["bytes"] and _sha(target) == entry["sha256"])
    source = installed / "runtime/Lib/site-packages/k5vision/operator_integrations.py"
    _require(_sha(source) == _SOURCE_SHA)


def _imports(installed):
    root = installed / "runtime/Lib/site-packages/k5vision"
    for name in (
        "k5vision.operator_integrations",
        "k5vision.operator_events",
        "k5vision.identity_state",
    ):
        importlib.import_module(name)
    for name, module in tuple(sys.modules.items()):
        if name == "k5vision" or name.startswith("k5vision."):
            path = getattr(module, "__file__", None)
            _require(path is not None and Path(path).resolve(strict=True).is_relative_to(root))
    _require(_sha(root / "operator_integrations.py") == _SOURCE_SHA)


class _Results:
    def __init__(self):
        self.collected = []
        self.calls = []
        self.bad = False

    def pytest_collection_finish(self, session):
        self.collected = [item.nodeid for item in session.items]

    def pytest_runtest_logreport(self, report):
        if report.failed or report.skipped:
            self.bad = True
        if report.when == "call" and report.passed:
            self.calls.append(report.nodeid)

    def accepted(self):
        return (
            not self.bad
            and len(self.collected) == len(set(self.collected)) == _TOTAL_TESTS
            and sorted(self.calls) == sorted(self.collected)
            and {
                name: sum(Path(node.split("::")[0]).name == name for node in self.collected)
                for name in _TEST_COUNTS
            }
            == _TEST_COUNTS
        )


def _stage_test_dependencies(destination):
    # Stage only pytest and the HTTP client used by the existing test fixtures. Never expose the
    # development site-packages directory or execute its .pth files.
    destination.mkdir()
    names = (
        "pytest",
        "_pytest",
        "py",
        "pluggy",
        "iniconfig",
        "packaging",
        "pygments",
        "httpx",
        "httpcore",
        "certifi",
    )
    if os.name == "nt":
        names += ("colorama",)
    for name in names:
        spec = importlib.util.find_spec(name)
        _require(spec is not None and spec.origin is not None)
        source = Path(spec.origin).resolve(strict=True)
        if spec.submodule_search_locations is not None:
            shutil.copytree(
                source.parent, destination / name, ignore=shutil.ignore_patterns("__pycache__")
            )
        else:
            shutil.copyfile(source, destination / (name + ".py"))


def _bootstrap(config):
    installed = Path(config["installed"]).resolve(strict=True)
    _require(os.name == "nt" and sys.flags.isolated and sys.dont_write_bytecode)
    _require(Path(sys.executable).resolve(strict=True) == installed / "runtime/python.exe")
    _require(Path(sys.prefix).resolve(strict=True) == installed / "runtime")
    # Only the staged test dependency allowlist is externally available.
    # Installed product/dependency paths retain precedence in parent and spawn children.
    sys.path.append(config["test_dependencies"])
    sys.path.append(config["tests"])
    _imports(installed)
    return installed


def _test_paths(tests):
    for name, digest in _TEST_HASHES.items():
        _require(_sha(tests / name) == digest)
    return [str(tests / name) for name in _TEST_HASHES]


def _main(config, installed):
    import pytest

    tests = Path(config["tests"])
    test_paths = _test_paths(tests)
    _manifest(installed, config["manifest_sha256"], config["head_sha"])
    results = _Results()
    code = pytest.main(
        [
            *test_paths,
            "-c",
            str(tests / "pytest.ini"),
            "--confcutdir",
            str(tests),
            "-o",
            "addopts=",
            "-p",
            "no:cacheprovider",
            "--basetemp",
            str(tests / "state"),
            "-q",
            "--tb=no",
        ],
        plugins=[results],
    )
    _require(code == 0 and results.accepted())
    _imports(installed)
    _manifest(installed, config["manifest_sha256"], config["head_sha"])
    receipt = {
        "head_sha": config["head_sha"],
        "manifest_sha256": config["manifest_sha256"],
        "installer_sha256": config["installer_sha256"],
        "source_sha256": _SOURCE_SHA,
        "tests_sha256": _TEST_HASHES,
        "installed_imports": True,
        "spawned_installed_interpreter": True,
        "passed": _TOTAL_TESTS,
        "failed": 0,
        "skipped": 0,
    }
    Path(config["receipt"]).write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")


def qualify(installed, setup, parent):
    """Called after actual GUI installation, inside the existing test cleanup boundary."""
    from k5vision.operator_native_process import OwnedNativeProcess

    _require(os.name == "nt" and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted")
    _require(
        os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("RUNNER_OS") == "Windows"
    )
    head = os.environ.get("K5_DESKTOP_HEAD_SHA", "")
    _require(re.fullmatch(r"[0-9a-f]{40}", head) is not None)
    manifest_hash = _sha(Path(os.environ["K5_DESKTOP_PAYLOAD"]) / "payload-manifest.json")
    _manifest(installed, manifest_hash, head)
    test_root = parent / "integration-admission-proof"
    test_root.mkdir(mode=0o700)
    worker = None
    try:
        source_tests = Path(__file__).resolve().parents[1] / "unit"
        _test_paths(source_tests)
        for name in (*_TEST_HASHES, "test_operator_integrations.py"):
            shutil.copyfile(source_tests / name, test_root / name)
        (test_root / "pytest.ini").write_text("[pytest]\n", encoding="ascii")
        dependencies = test_root / "test-dependencies"
        _stage_test_dependencies(dependencies)
        receipt_path = test_root / "receipt.json"
        config = {
            "installed": str(installed.resolve(strict=True)),
            "tests": str(test_root.resolve(strict=True)),
            "test_dependencies": str(dependencies),
            "manifest_sha256": manifest_hash,
            "head_sha": head,
            "installer_sha256": _sha(setup),
            "receipt": str(receipt_path),
        }
        config_path = test_root / "config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        # The existing owner puts a gated relay and every spawned descendant in
        # a kill-on-close Windows Job Object before allowing execution.
        worker = OwnedNativeProcess(
            [
                str(installed / "runtime/python.exe"),
                "-I",
                "-B",
                str(Path(__file__).resolve()),
                str(config_path),
            ],
            cwd=test_root,
        )
        deadline = time.monotonic() + 120
        while worker.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        _require(worker.poll() == 0)
        _require(receipt_path.is_file() and receipt_path.stat().st_size < 4096)
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        _require(receipt["passed"] == _TOTAL_TESTS and receipt["failed"] == receipt["skipped"] == 0)
        _require(receipt["head_sha"] == head and receipt["manifest_sha256"] == manifest_hash)
    finally:
        if worker is not None:
            worker.stop()
        shutil.rmtree(test_root)
    _require(not test_root.exists())
    return receipt | {"owned_cleanup": True}


# multiprocessing runs this top-level bootstrap as __mp_main__ before importing
# the unchanged regression worker. Every spawned worker must admit installed paths.
if __name__ in {"__main__", "__mp_main__"}:
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    _config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    _installed = _bootstrap(_config)
    if __name__ == "__main__":
        _main(_config, _installed)
