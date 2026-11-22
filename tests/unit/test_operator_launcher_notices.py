"""Legacy launcher fixtures retain the derivative's complete admitted notices."""

from __future__ import annotations

import importlib.util
import json
import os
from hashlib import sha256
from pathlib import Path
from zipfile import ZipFile

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "integration/test_operator_windows_launcher.py"
SPEC = importlib.util.spec_from_file_location("launcher_notice_fixture", SOURCE)
assert SPEC and SPEC.loader
HELPERS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HELPERS)


def inputs(tmp_path, monkeypatch):
    runtime, tools = tmp_path / "runtime", tmp_path / "tools"
    runtime.mkdir(mode=0o700)
    (runtime / "tools").mkdir(mode=0o700)
    tools.mkdir(mode=0o700)
    archive = tools / "mediamtx.zip"
    archive.write_bytes(b"Complete admitted archive with original notices and provenance")
    monkeypatch.setenv("K5_DONOR_TOOL_ROOT", str(tools))
    wheel = tmp_path / "fixture.whl"
    workers = {}
    with ZipFile(wheel, "w") as zipped:
        for name, component, version in (
            ("mediamtx", "MediaMTX", "v1.21.2-k5-monotonic1"),
            ("caddy", "Caddy", "v2.11.7"),
        ):
            source = tmp_path / (name + "-input")
            source.mkdir(mode=0o700)
            executable = source / (name + ".exe")
            executable.write_bytes(("Pinned " + name).encode())
            license_path = source / "LICENSE"
            license_path.write_bytes(("Original " + name + " license").encode())
            artifact = {
                "platform": "windows_amd64",
                "executable": executable.name,
                "executable_bytes": executable.stat().st_size,
                "executable_sha256": sha256(executable.read_bytes()).hexdigest(),
                "archive_bytes": archive.stat().st_size,
                "sha256": sha256(archive.read_bytes()).hexdigest(),
            }
            manifest = {
                "component": component,
                "version": version,
                "license_sha256": sha256(license_path.read_bytes()).hexdigest(),
                "artifacts": [artifact],
            }
            zipped.writestr(f"k5vision/data/{name}-donor-manifest.json", json.dumps(manifest))
            workers[name] = executable
    return runtime, tools, wheel, workers


def seeded(tmp_path, monkeypatch):
    runtime, tools, wheel, workers = inputs(tmp_path, monkeypatch)
    for name, supplied in workers.items():
        HELPERS._cache_worker(runtime, wheel, name, supplied)
    return runtime, tools, wheel, workers


def test_seeded_derivative_cache_retains_exact_archive_and_rejects_extra_file(
    tmp_path, monkeypatch
):
    runtime, tools, wheel, _ = seeded(tmp_path, monkeypatch)
    retained = runtime / "tools/MediaMTX/v1.21.2-k5-monotonic1/upstream-notices-and-provenance.zip"
    assert retained.read_bytes() == (tools / "mediamtx.zip").read_bytes()
    HELPERS._verify_installed_workers(runtime, wheel)
    (retained.parent / "unadmitted.txt").write_bytes(b"unexpected")
    with pytest.raises(pytest.fail.Exception, match="unexpected extraction files"):
        HELPERS._verify_installed_workers(runtime, wheel)


def test_cached_fixture_refuses_changed_supplied_notice_archive(tmp_path, monkeypatch):
    runtime, tools, wheel, workers = inputs(tmp_path, monkeypatch)
    (tools / "mediamtx.zip").write_bytes(b"Changed provenance")
    with pytest.raises(pytest.fail.Exception, match="notice archive identity was refused"):
        HELPERS._cache_worker(runtime, wheel, "mediamtx", workers["mediamtx"])


def test_installed_fixture_refuses_changed_retained_notice_archive(tmp_path, monkeypatch):
    runtime, _, wheel, _ = seeded(tmp_path, monkeypatch)
    retained = runtime / "tools/MediaMTX/v1.21.2-k5-monotonic1/upstream-notices-and-provenance.zip"
    retained.write_bytes(b"Changed provenance")
    with pytest.raises(pytest.fail.Exception, match="notice archive identity was refused"):
        HELPERS._verify_installed_workers(runtime, wheel)


INSTALL_SOURCE = SOURCE.with_name("test_windows_operator_install.py")
INSTALL_SPEC = importlib.util.spec_from_file_location("installed_notice_fixture", INSTALL_SOURCE)
assert INSTALL_SPEC and INSTALL_SPEC.loader
INSTALL_HELPERS = importlib.util.module_from_spec(INSTALL_SPEC)
INSTALL_SPEC.loader.exec_module(INSTALL_HELPERS)


def installed_seed(tmp_path, monkeypatch, *, changed=False, missing=False):
    runtime, tools, wheel, workers = inputs(tmp_path, monkeypatch)
    # The installed lifecycle helper creates this directory itself.
    (runtime / "tools").rmdir()
    for name, supplied in workers.items():
        monkeypatch.setenv("K5_" + name.upper() + "_DONOR_EXE", str(supplied))
    if changed:
        (tools / "mediamtx.zip").write_bytes(b"Changed provenance")
    if missing:
        (tools / "mediamtx.zip").unlink()
    # Windows private ACLs are exercised by the native journey; use equivalent
    # owner-only creation in this portable, actual fixture-function control.
    previous_umask = os.umask(0o077)
    try:
        INSTALL_HELPERS._cache_workers(runtime, wheel)
    finally:
        os.umask(previous_umask)
    return runtime, tools, wheel


def test_installed_lifecycle_seed_retains_complete_notice_archive(tmp_path, monkeypatch):
    runtime, tools, wheel = installed_seed(tmp_path, monkeypatch)
    retained = runtime / "tools/MediaMTX/v1.21.2-k5-monotonic1/upstream-notices-and-provenance.zip"
    assert retained.read_bytes() == (tools / "mediamtx.zip").read_bytes()
    HELPERS._verify_installed_workers(runtime, wheel)


@pytest.mark.parametrize("case", ["changed", "missing"])
def test_installed_lifecycle_seed_refuses_unadmitted_notice_archive(tmp_path, monkeypatch, case):
    with pytest.raises(pytest.fail.Exception, match="notice archive identity was refused"):
        installed_seed(tmp_path, monkeypatch, **{case: True})
