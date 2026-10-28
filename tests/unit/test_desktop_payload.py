"""Adversarial payload identity checks independent of native GUI qualification."""

from __future__ import annotations

import importlib.util
import io
import json
import tarfile
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


def _onvif_payload(builder, tmp_path):
    payload = tmp_path / "payload"
    module, notice = payload / builder.ONVIF_MODULE_PATH, payload / builder.ONVIF_NOTICE_PATH
    module.parent.mkdir(parents=True)
    notice.parent.mkdir(parents=True)
    module.write_bytes(b'"""Installed stdlib camera connector."""\n')
    notice.write_bytes((builder.ROOT / builder.ONVIF_NOTICE).read_bytes())
    return payload, module, notice


def test_onvif_connector_and_retained_permission_notice_are_sealed(builder, tmp_path):
    payload, module, notice = _onvif_payload(builder, tmp_path)
    adaptation = builder.onvif_source_adaptation(payload)
    assert adaptation["source_commit"] == "b5948cfb25404cc5cb37b166ecaa2dca20b11d4b"
    assert adaptation["license"] == "MIT"
    assert adaptation["retained_notice"] == {
        "path": "licenses/go2rtc-MIT.txt",
        "sha256": sha256(notice.read_bytes()).hexdigest(),
    }
    generated = tmp_path / "PayloadIdentity.generated.cs"
    identity = builder.write_manifest(payload, "b" * 40, generated)
    raw_manifest = (payload / "payload-manifest.json").read_bytes()
    assert sha256(raw_manifest).hexdigest() == identity
    files = {entry["path"]: entry for entry in json.loads(raw_manifest)["files"]}
    for path in (module, notice):
        relative = path.relative_to(payload).as_posix()
        assert files[relative]["sha256"] == sha256(path.read_bytes()).hexdigest()
        assert files[relative]["bytes"] == path.stat().st_size


@pytest.mark.parametrize("missing", ["module", "notice"])
def test_onvif_package_refuses_missing_required_material(builder, tmp_path, missing):
    payload, module, notice = _onvif_payload(builder, tmp_path)
    {"module": module, "notice": notice}[missing].unlink()
    with pytest.raises(ValueError, match="material missing"):
        builder.onvif_source_adaptation(payload)


def test_onvif_notice_accepts_complete_windows_text_and_refuses_changed_terms(builder, tmp_path):
    payload, _module, notice = _onvif_payload(builder, tmp_path)
    canonical = notice.read_bytes().replace(b"\r\n", b"\n")
    assert sha256(canonical).hexdigest() == (
        "b0dcf4855af5a72b4dfbd9117c207b330f4cc35658576a0b5351d6e2becac546"
    )
    for representation in (canonical, canonical.replace(b"\n", b"\r\n")):
        notice.write_bytes(representation)
        retained = builder.onvif_source_adaptation(payload)["retained_notice"]
        assert retained["sha256"] == sha256(representation).hexdigest()
    for changed in (
        canonical.replace(b"Alexey Khit", b"Another Name"),
        canonical.replace(b"\n", b"\r\n", 1),
    ):
        notice.write_bytes(changed)
        with pytest.raises(ValueError, match="notice identity mismatch"):
            builder.onvif_source_adaptation(payload)


def _person_manifest(builder, tmp_path, monkeypatch):
    """Small deterministic donor files exercise the real download admission."""
    from hashlib import sha384

    documents = {"person.xml": b"<reviewed-model/>", "person.bin": b"reviewed weights"}
    artifacts = [
        {
            "relative_path": "person-detection-retail-0013/FP16/" + name,
            "source_url": "https://storage.openvinotoolkit.org/repositories/open_model_zoo/" + name,
            "bytes": len(raw),
            "sha384": sha384(raw).hexdigest(),
        }
        for name, raw in documents.items()
    ]
    manifest = {
        "model": "person-detection-retail-0013",
        "source_repository": "https://github.com/openvinotoolkit/open_model_zoo",
        "source_revision": "a" * 40,
        "license_id": "Apache-2.0",
        "copyright": "Intel Corporation",
        "artifacts": artifacts,
    }
    path = tmp_path / "person-manifest.json"
    path.write_text(json.dumps(manifest) + "\n")
    monkeypatch.setattr(builder, "PERSON_MANIFEST", path)
    monkeypatch.setattr(builder, "PERSON_MANIFEST_SHA256", sha256(path.read_bytes()).hexdigest())

    def response(url, *, timeout):
        assert timeout == 180
        return io.BytesIO(documents[url.rsplit("/", 1)[1]])

    monkeypatch.setattr(builder, "urlopen", response)
    return documents, path


def test_installed_person_model_is_complete_and_identity_bound(builder, tmp_path, monkeypatch):
    documents, _ = _person_manifest(builder, tmp_path, monkeypatch)
    payload = tmp_path / "payload"
    result = builder.provision_person_model(payload)
    assert result["artifact_root"] == builder.PERSON_MODEL_ROOT
    assert result["license"] == "Apache-2.0"
    assert len(result["artifacts"]) == 2
    for item in result["artifacts"]:
        content = (payload / item["path"]).read_bytes()
        assert content == documents[Path(item["path"]).name]
        assert item["bytes"] == len(content)
        assert item["sha256"] == sha256(content).hexdigest()


@pytest.mark.parametrize("damage", ["changed", "truncated", "oversized"])
def test_installed_model_refuses_changed_incomplete_or_oversized_download(
    builder, tmp_path, monkeypatch, damage
):
    documents, _ = _person_manifest(builder, tmp_path, monkeypatch)
    original = documents["person.bin"]
    documents["person.bin"] = {
        "changed": b"changed! weights",
        "truncated": original[:-1],
        "oversized": original + b"overflow",
    }[damage]
    with pytest.raises(ValueError, match="Person model artifact (identity mismatch|size exceeded)"):
        builder.provision_person_model(tmp_path / "payload")


def test_installed_model_refuses_changed_manifest_before_network(builder, tmp_path, monkeypatch):
    _, path = _person_manifest(builder, tmp_path, monkeypatch)
    path.write_bytes(path.read_bytes().replace(b"Intel Corporation", b"Another Author"))
    with pytest.raises(ValueError, match="manifest identity mismatch"):
        builder.provision_person_model(tmp_path / "payload")
    assert not (tmp_path / "payload").exists()


def test_app_local_cpp_runtime_keeps_the_admitted_upstream_bytes(builder, tmp_path):
    payload = tmp_path / "payload"
    original = payload / "runtime/Lib/site-packages/numpy.libs/msvcp140-admitted.dll"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"MZ\0admitted Microsoft runtime")
    pin = {
        "source_path": original.relative_to(payload).as_posix(),
        "path": "runtime/msvcp140.dll",
        "bytes": original.stat().st_size,
        "sha256": sha256(original.read_bytes()).hexdigest(),
    }
    assert builder.provision_cpp_runtime(payload, pin) == pin
    assert (payload / pin["path"]).read_bytes() == original.read_bytes()
    with pytest.raises(ValueError, match="destination already exists"):
        builder.provision_cpp_runtime(payload, pin)


def test_app_local_cpp_runtime_refuses_modified_dependency_bytes(builder, tmp_path):
    payload = tmp_path / "payload"
    original = payload / "runtime/Lib/site-packages/numpy.libs/msvcp140-admitted.dll"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"MZ\0admitted Microsoft runtime")
    pin = {
        "source_path": original.relative_to(payload).as_posix(),
        "path": "runtime/msvcp140.dll",
        "bytes": original.stat().st_size,
        "sha256": sha256(original.read_bytes()).hexdigest(),
    }
    original.write_bytes(b"MZ\0modified Microsoft runtime")
    with pytest.raises(ValueError, match="identity mismatch"):
        builder.provision_cpp_runtime(payload, pin)
    assert not (payload / pin["path"]).exists()


def test_corresponding_sources_keep_exact_bytes_and_build_modes(builder, tmp_path):
    source, destination = tmp_path / "upstream.tar.gz", tmp_path / "sources.tar.gz"
    files = {
        "upstream/build.sh": (b"#!/bin/sh\nmake\n", 0o755),
        "upstream/LICENSE": (b"retain complete license\n", 0o644),
        "upstream/legacy-comment.cpp": (b"// copyright \xa9\nint main() {}\n", 0o644),
        "upstream/prebuilt.dll": (b"MZ\0binary\0", 0o755),
    }
    with tarfile.open(source, "w:gz") as archive:
        for name, (raw, mode) in files.items():
            item = tarfile.TarInfo(name)
            item.size, item.mode = len(raw), mode
            archive.addfile(item, io.BytesIO(raw))
    result = builder.retain_text_sources(source, destination)
    assert result["retained_files"] == 3
    assert result["sha256"] == sha256(destination.read_bytes()).hexdigest()
    with tarfile.open(destination) as archive:
        assert "upstream/prebuilt.dll" not in archive.getnames()
        for item in archive:
            assert archive.extractfile(item).read() == files[item.name][0]
            assert item.mode == files[item.name][1]
    receipt = destination.with_suffix(".files.json")
    entries = json.loads(receipt.read_bytes())
    assert len(entries) == 3
    assert result["file_inventory"]["sha256"] == sha256(receipt.read_bytes()).hexdigest()


@pytest.mark.parametrize("unsafe", ["../escape.cpp", "/absolute.cpp", "dir\\escape.cpp"])
def test_corresponding_sources_refuse_archive_path_escape(builder, tmp_path, unsafe):
    source, destination = tmp_path / "upstream.tar.gz", tmp_path / "sources.tar.gz"
    with tarfile.open(source, "w:gz") as archive:
        item = tarfile.TarInfo(unsafe)
        item.size = 4
        archive.addfile(item, io.BytesIO(b"text"))
    with pytest.raises(ValueError, match="archive layout refused"):
        builder.retain_text_sources(source, destination)
    assert not (tmp_path / "escape.cpp").exists()
