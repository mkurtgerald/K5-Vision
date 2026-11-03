"""Adversarial payload identity checks independent of native GUI qualification."""

from __future__ import annotations

import importlib.util
import json
from hashlib import sha256
from pathlib import Path
from types import ModuleType
from zipfile import ZipFile

import pytest


@pytest.fixture(scope="module")
def builder() -> ModuleType:
    source = Path(__file__).resolve().parents[2] / "scripts/build_desktop_payload.py"
    spec = importlib.util.spec_from_file_location("desktop_payload_builder", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_exact_donor_identity_detects_size_and_content_tampering(builder, tmp_path):
    donor = tmp_path / "donor.zip"
    donor.write_bytes(b"verified native donor")
    identity = {"bytes": donor.stat().st_size, "sha256": sha256(donor.read_bytes()).hexdigest()}
    builder.admit(donor, identity)
    donor.write_bytes(b"verified native donox")
    with pytest.raises(ValueError, match="identity mismatch"):
        builder.admit(donor, identity)
    donor.write_bytes(b"verified native donor plus")
    with pytest.raises(ValueError, match="identity mismatch"):
        builder.admit(donor, identity)


def test_embedding_refuses_archive_path_escape(builder, tmp_path):
    archive, runtime = tmp_path / "unsafe.zip", tmp_path / "runtime"
    runtime.mkdir()
    with ZipFile(archive, "w") as zipped:
        zipped.writestr("../foreign", "unsafe")
    with pytest.raises(ValueError, match="archive layout"):
        builder.extract_python(archive, runtime)
    assert not (tmp_path / "foreign").exists()
    assert not list(runtime.iterdir())


def test_isolated_embedding_enables_only_bundled_import_path(builder, tmp_path):
    archive, runtime = tmp_path / "python.zip", tmp_path / "runtime"
    runtime.mkdir()
    with ZipFile(archive, "w") as zipped:
        zipped.writestr("python312._pth", "python312.zip\n.\n")
        zipped.writestr("LICENSE.txt", "retained upstream license")
    builder.extract_python(archive, runtime)
    assert (runtime / "python312._pth").read_bytes() == (
        b"python312.zip\r\n.\r\nLib/site-packages\r\nimport site\r\n"
    )
    assert (runtime / "LICENSE.txt").read_text() == "retained upstream license"


def test_manifest_and_compiled_pin_bind_all_payload_files(builder, tmp_path):
    payload = tmp_path / "payload"
    (payload / "runtime").mkdir(parents=True)
    (payload / "runtime/python.exe").write_bytes(b"native executable")
    (payload / "LICENSE").write_bytes(b"license")
    (payload / "K5Vision.exe").write_bytes(b"excluded self")
    generated = tmp_path / "PayloadIdentity.generated.cs"
    identity = builder.write_manifest(payload, "a" * 40, generated)
    manifest = payload / "payload-manifest.json"
    result = json.loads(manifest.read_bytes())
    assert identity == sha256(manifest.read_bytes()).hexdigest()
    assert result["head_sha"] == "a" * 40
    assert [entry["path"] for entry in result["files"]] == ["LICENSE", "runtime/python.exe"]
    assert identity in generated.read_text()
    assert "namespace K5VisionDesktop" in generated.read_text()
    for entry in result["files"]:
        content = (payload / entry["path"]).read_bytes()
        assert entry["sha256"] == sha256(content).hexdigest()
        assert entry["bytes"] == len(content)


def test_unknown_or_changed_dependency_wheel_is_refused(builder, tmp_path):
    wheel = tmp_path / "sample-1.0-py3-none-any.whl"
    with ZipFile(wheel, "w") as zipped:
        zipped.writestr("sample-1.0.dist-info/METADATA", "Name: sample\nVersion: 1.0\n")
        zipped.writestr("sample-1.0.dist-info/licenses/LICENSE", "MIT license")
    pin = {
        "name": "sample",
        "version": "1.0",
        "filename": wheel.name,
        "sha256": sha256(wheel.read_bytes()).hexdigest(),
        "bytes": wheel.stat().st_size,
        "license": "MIT",
    }
    result = builder.inventory_wheels(tmp_path, [pin])
    assert result[0]["retained_notices"][0]["path"].endswith("/licenses/LICENSE")
    (tmp_path / "unexpected.whl").write_bytes(b"unknown")
    with pytest.raises(ValueError, match="Unexpected dependency"):
        builder.inventory_wheels(tmp_path, [pin])
    (tmp_path / "unexpected.whl").unlink()
    wheel.write_bytes(wheel.read_bytes() + b"altered")
    with pytest.raises(ValueError, match="identity mismatch"):
        builder.inventory_wheels(tmp_path, [pin])
