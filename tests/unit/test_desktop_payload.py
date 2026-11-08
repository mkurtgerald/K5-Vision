"""Adversarial payload identity checks independent of native GUI qualification."""

from __future__ import annotations

import ast
import importlib.util
import io
import json
import sys
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


def test_native_worker_archive_reuses_only_verified_ci_donor(builder, tmp_path, monkeypatch):
    root = tmp_path / "CI verified donors"
    root.mkdir()
    raw = b"immutable pinned official worker archive"
    source = root / "mediamtx.zip"
    source.write_bytes(raw)
    identity = {"bytes": len(raw), "sha256": sha256(raw).hexdigest()}
    monkeypatch.setenv("K5_DONOR_TOOL_ROOT", str(root))

    def no_redownload(*_args, **_kwargs):
        raise AssertionError("Verified CI donor must not be fetched again")

    monkeypatch.setattr(builder, "download", no_redownload)
    destination = tmp_path / "worker-mediamtx-official.zip"
    builder.acquire_worker_archive(identity, destination, "mediamtx")
    assert destination.read_bytes() == raw

    # A changed archive fails without falling back to the network.
    source.write_bytes(b"tampered upstream archive")
    refused = tmp_path / "refused.zip"
    with pytest.raises(ValueError, match="identity mismatch"):
        builder.acquire_worker_archive(identity, refused, "mediamtx")
    assert not refused.exists()
    with pytest.raises(ValueError, match="component"):
        builder.acquire_worker_archive(identity, refused, "unknown")


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


def test_identity_native_smoke_executes_rs256_jwk_and_totp_primitives(builder):
    # This executes genuine crypto primitives; the Windows payload builder runs
    # the same code in its isolated embedded interpreter before packaging.
    exec(builder.IDENTITY_CRYPTO_SMOKE, {})


def test_actual_combined_embedded_smoke_script_is_valid_python(builder):
    source = (builder.ROOT / "scripts/build_desktop_payload.py").read_text(encoding="utf-8")
    expressions = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Add)
        and any(
            isinstance(child, ast.Name) and child.id == "IDENTITY_CRYPTO_SMOKE"
            for child in ast.walk(node)
        )
    ]
    # The largest containing expression is the real combined -c argument.
    expression = max(expressions, key=lambda node: len(ast.get_source_segment(source, node)))
    assembled = eval(compile(ast.Expression(expression), "<smoke argument>", "eval"), vars(builder))
    compile(assembled, "<actual embedded -c smoke>", "exec")


def test_embedded_decoder_child_probe_uses_real_worker_with_no_camera_media(builder):
    # The deployed worker's local pipe, process ownership and isolated
    # embedded Python import path must work before packaging is admitted.
    script = builder.NATIVE_DECODER_CHILD_SMOKE
    compile(script, "<embedded decoder-child smoke>", "exec")
    assert "load_installed_person_configuration" in script
    assert "_RecordedNativeWorker(person)" in script
    assert "worker.request(None)" in script
    assert "worker._person_ready" in script
    assert "__k5_missing_native_probe__.mp4" in script
    assert "worker.close()" in script


def _identity_crypto_material(builder, tmp_path, monkeypatch):
    root, payload = tmp_path / "source", tmp_path / "payload"
    root.mkdir()
    payload.mkdir()
    monkeypatch.setattr(builder, "ROOT", root)
    native = payload / "runtime/native.pyd"
    native.parent.mkdir()
    native.write_bytes(b"reviewed native identity binary")
    notice = root / "native-NOTICES.txt"
    notice.write_bytes(b"Complete upstream permission notice\nSecond original line\n")
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as target:
        for name, raw in (
            ("upstream/native.c", b"/* Original complete license */\nsource\n"),
            ("upstream/prebuilt.lib", b"\x00unneededbinary"),
        ):
            member = tarfile.TarInfo(name)
            member.size = len(raw)
            target.addfile(member, io.BytesIO(raw))
    source_bytes = archive.getvalue()
    pin = {
        "native_modules": [
            {
                "path": "runtime/native.pyd",
                "bytes": native.stat().st_size,
                "sha256": builder.digest(native),
            }
        ],
        "notices": [
            {
                "source_path": notice.name,
                "path": "licenses/native-NOTICES.txt",
                "bytes": notice.stat().st_size,
                "sha256": builder.digest(notice),
            }
        ],
        "sources": [
            {
                "filename": "upstream.tar.gz",
                "url": "https://public.invalid/source",
                "bytes": len(source_bytes),
                "sha256": sha256(source_bytes).hexdigest(),
                "retain_text_sources": True,
            }
        ],
    }
    monkeypatch.setattr(builder, "urlopen", lambda *_args, **_kwargs: io.BytesIO(source_bytes))
    return payload, pin, native, notice


def test_identity_crypto_material_seals_notices_native_bytes_and_source_derivative(
    builder, tmp_path, monkeypatch
):
    payload, pin, _native, notice = _identity_crypto_material(builder, tmp_path, monkeypatch)
    raw = notice.read_bytes().replace(b"\n", b"\r\n")
    notice.write_bytes(raw)
    result = builder.provision_identity_crypto_material(payload, pin)
    retained_notice = result["notices"][0]
    assert (payload / retained_notice["path"]).read_bytes() == raw
    assert retained_notice["sha256"] == sha256(raw).hexdigest()
    source = result["sources"][0]
    assert source["upstream_archive_sha256"] == pin["sources"][0]["sha256"]
    assert source["retained_files"] == 1
    with tarfile.open(payload / source["path"], "r:gz") as retained:
        assert retained.getnames() == ["upstream/native.c"]
        assert (
            retained.extractfile("upstream/native.c").read()
            == b"/* Original complete license */\nsource\n"
        )
    inventory = source["file_inventory"]
    assert builder.digest(payload / inventory["path"]) == inventory["sha256"]


@pytest.mark.parametrize("changed", ["native", "notice"])
def test_identity_crypto_refuses_modified_native_or_incomplete_notice(
    builder, tmp_path, monkeypatch, changed
):
    payload, pin, native, notice = _identity_crypto_material(builder, tmp_path, monkeypatch)
    target = native if changed == "native" else notice
    target.write_bytes(target.read_bytes()[:-1] + b"X")
    with pytest.raises(ValueError, match="identity mismatch"):
        builder.provision_identity_crypto_material(payload, pin)


@pytest.mark.parametrize("name", ["../outside.tar.gz", "..\\outside.tar.gz", "C:outside.tar.gz"])
def test_identity_source_paths_cannot_escape_installed_sources(
    builder, tmp_path, monkeypatch, name
):
    payload, pin, _, _ = _identity_crypto_material(builder, tmp_path, monkeypatch)
    pin["sources"][0]["filename"] = name
    with pytest.raises(ValueError, match="source filename refused"):
        builder.provision_identity_crypto_material(payload, pin)


def test_source_download_rejects_archive_metadata_changes_even_when_source_matches(
    builder, tmp_path, monkeypatch
):
    archives = []
    for timestamp in (1000, 2000):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            item = tarfile.TarInfo("upstream/source.c")
            content = b"unchanged corresponding source\n"
            item.size, item.mtime = len(content), timestamp
            archive.addfile(item, io.BytesIO(content))
        archives.append(output.getvalue())
    assert archives[0] != archives[1]
    for raw in archives:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
            assert archive.extractfile("upstream/source.c").read() == content
    identity = {
        "url": "https://public.invalid/source.tar.gz",
        "bytes": len(archives[0]),
        "sha256": sha256(archives[0]).hexdigest(),
    }
    monkeypatch.setattr(builder, "urlopen", lambda *_args, **_kwargs: io.BytesIO(archives[1]))
    with pytest.raises(ValueError, match="Donor (content identity mismatch|size exceeded)"):
        builder.download(identity, tmp_path / "source.tar.gz")


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
    path.write_text(json.dumps(manifest) + "\n", newline="\n")
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


def test_installed_model_accepts_complete_crlf_manifest_with_canonical_pin(
    builder, tmp_path, monkeypatch
):
    _documents, path = _person_manifest(builder, tmp_path, monkeypatch)
    canonical = path.read_bytes()
    assert b"\r" not in canonical
    assert builder.PERSON_MANIFEST_SHA256 == sha256(canonical).hexdigest()
    windows_text = canonical.replace(b"\n", b"\r\n")
    assert windows_text != canonical
    path.write_bytes(windows_text)
    result = builder.provision_person_model(tmp_path / "payload")
    assert result["manifest_sha256"] == sha256(windows_text).hexdigest()
    assert len(result["artifacts"]) == 2


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


def test_payload_phase_output_accepts_only_fixed_labels(builder, capsys):
    builder.phase("decoder_source_libaom")
    assert json.loads(capsys.readouterr().out) == {"desktop_payload_stage": "decoder_source_libaom"}
    with pytest.raises(ValueError, match="Unknown payload phase"):
        builder.phase("https://private.invalid/supplied-secret")
    assert capsys.readouterr().out == ""


def test_payload_failure_reports_phase_and_exception_type_without_private_values(
    builder, monkeypatch, capsys
):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_desktop_payload.py",
            "--output",
            "private-output",
            "--head-sha",
            "a" * 40,
            "--media-exe",
            "private-media.exe",
            "--gateway-exe",
            "private-gateway.exe",
        ],
    )

    def refused(_arguments):
        builder.phase("decoder_source_libaom")
        raise OSError("https://private.invalid/token supplied-secret private-output")

    monkeypatch.setattr(builder, "build", refused)
    assert builder.main() == 1
    observed = capsys.readouterr()
    assert json.loads(observed.err) == {
        "desktop_payload_refused": True,
        "stage": "decoder_source_libaom",
        "error_type": "OSError",
    }
    assert "private" not in observed.out + observed.err
    assert "supplied-secret" not in observed.out + observed.err


def test_native_worker_bundle_admits_official_archive_executable_and_license(
    builder, tmp_path, monkeypatch
):
    """CI must seal both exact worker binaries and upstream licenses, not URLs."""
    source = tmp_path / "worker.exe"
    source.write_bytes(b"MZ\\x00reviewed worker")
    license_bytes = b"Exact upstream permission notice\\n"
    archive = io.BytesIO()
    with ZipFile(archive, "w") as zipped:
        zipped.writestr("mediamtx.exe", source.read_bytes())
        zipped.writestr("LICENSE", license_bytes)
    zipped_bytes = archive.getvalue()
    pin = {
        "component": "MediaMTX",
        "version": "v1.21.2",
        "executable": "mediamtx.exe",
        "executable_bytes": source.stat().st_size,
        "executable_sha256": sha256(source.read_bytes()).hexdigest(),
        "license_sha256": sha256(license_bytes).hexdigest(),
        "url": "https://example.invalid/reviewed-worker.zip",
        "bytes": len(zipped_bytes),
        "sha256": sha256(zipped_bytes).hexdigest(),
    }
    monkeypatch.setattr(builder, "worker_download", lambda supplied, name: pin)
    monkeypatch.setattr(builder, "urlopen", lambda url, timeout: io.BytesIO(zipped_bytes))
    payload = tmp_path / "payload"
    payload.mkdir()
    result = builder.bundle_worker(payload, source, "mediamtx")
    assert (payload / result["payload_executable"]).read_bytes() == source.read_bytes()
    assert (payload / result["payload_license"]).read_bytes() == license_bytes
    assert result["payload_license_bytes"] == len(license_bytes)


def test_native_worker_bundle_refuses_changed_upstream_license(builder, tmp_path, monkeypatch):
    source = tmp_path / "worker.exe"
    source.write_bytes(b"MZ\\x00reviewed worker")
    official_license = b"Reviewed permission notice"
    archive = io.BytesIO()
    with ZipFile(archive, "w") as zipped:
        zipped.writestr("mediamtx.exe", source.read_bytes())
        zipped.writestr("LICENSE", b"Altered permission notice")
    zipped_bytes = archive.getvalue()
    pin = {
        "component": "MediaMTX",
        "version": "v1.21.2",
        "executable": "mediamtx.exe",
        "executable_bytes": source.stat().st_size,
        "executable_sha256": sha256(source.read_bytes()).hexdigest(),
        "license_sha256": sha256(official_license).hexdigest(),
        "url": "https://example.invalid/reviewed-worker.zip",
        "bytes": len(zipped_bytes),
        "sha256": sha256(zipped_bytes).hexdigest(),
    }
    monkeypatch.setattr(builder, "worker_download", lambda supplied, name: pin)
    monkeypatch.setattr(builder, "urlopen", lambda url, timeout: io.BytesIO(zipped_bytes))
    payload = tmp_path / "payload"
    payload.mkdir()
    with pytest.raises(ValueError, match="Worker archive member identity mismatch"):
        builder.bundle_worker(payload, source, "mediamtx")
