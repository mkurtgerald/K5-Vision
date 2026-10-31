from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest
import test_operator_time_admin as time_helpers

private_profile = time_helpers.private_profile
_PATH = Path(__file__).parents[1] / "integration/test_windows_time_administration.py"
_SPEC = importlib.util.spec_from_file_location("k5_time_private_staging_test", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
_HELPERS = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_HELPERS)


@pytest.fixture
def payload(private_profile):
    source = private_profile / "readable build workspace"
    source.mkdir(mode=0o755)
    (source / "runtime").mkdir(mode=0o755)
    executable = source / "K5Vision.exe"
    executable.write_bytes(b"compiled fixed native supervisor")
    (source / "runtime/python.exe").write_bytes(b"embedded runtime bytes")
    file = source / "runtime/python.exe"
    manifest = {
        "schema_version": 1,
        "head_sha": "a" * 40,
        "files": [
            {
                "path": "runtime/python.exe",
                "bytes": file.stat().st_size,
                "sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
            }
        ],
    }
    raw = json.dumps(manifest).encode()
    (source / "payload-manifest.json").write_bytes(raw)
    return (
        source,
        {
            "supervisor_sha": hashlib.sha256(executable.read_bytes()).hexdigest(),
            "manifest_sha": hashlib.sha256(raw).hexdigest(),
        },
        private_profile,
    )


def test_staging_copies_only_exact_payload_bytes_into_private_installed_layout(payload):
    source, pins, parent = payload
    target = _HELPERS._stage_private_payload(source, parent, **pins)
    assert target == parent / "sealed native payload"
    assert (target / "K5Vision.exe").read_bytes() == (source / "K5Vision.exe").read_bytes()
    assert (target / "payload-manifest.json").read_bytes() == (
        source / "payload-manifest.json"
    ).read_bytes()
    assert (target / "runtime/python.exe").read_bytes() == (
        source / "runtime/python.exe"
    ).read_bytes()
    if os.name == "posix":
        assert target.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("entry", ["unknown.bin", "unknown empty directory"])
def test_unknown_build_entries_cannot_enter_private_staging(payload, entry):
    source, pins, parent = payload
    if "directory" in entry:
        (source / entry).mkdir()
    else:
        (source / entry).write_bytes(b"unknown bytes")
    with pytest.raises(AssertionError):
        _HELPERS._stage_private_payload(source, parent, **pins)
    assert not (parent / "sealed native payload").exists()


def test_hardlinked_source_is_not_adopted_as_private_payload(payload):
    source, pins, parent = payload
    os.link(source / "runtime/python.exe", parent / "external runtime link")
    with pytest.raises(AssertionError):
        _HELPERS._stage_private_payload(source, parent, **pins)
    assert not (parent / "sealed native payload").exists()


def test_source_bytes_cannot_change_without_matching_manifest(payload):
    source, pins, parent = payload
    (source / "runtime/python.exe").write_bytes(b"tampered bytes")
    with pytest.raises(AssertionError):
        _HELPERS._stage_private_payload(source, parent, **pins)
    assert not (parent / "sealed native payload").exists()
    assert (source / "runtime/python.exe").read_bytes() == b"tampered bytes"


def test_different_supervisor_is_rejected_without_rebuilding_or_repinning(payload):
    source, pins, parent = payload
    pins["supervisor_sha"] = "f" * 64
    with pytest.raises(AssertionError):
        _HELPERS._stage_private_payload(source, parent, **pins)
    assert not (parent / "sealed native payload").exists()
