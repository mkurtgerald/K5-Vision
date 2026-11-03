from __future__ import annotations

import asyncio
import hashlib
import json
import threading

import pytest

import k5vision.analytics_runtime as lifecycle
import k5vision.operator_analytics_person as module
from k5vision.media.presentation_frame import PixelFormat, PresentationVideoFrame


@pytest.fixture
def admitted(monkeypatch, tmp_path):
    root = tmp_path / "models"
    root.mkdir()
    files = {"person.xml": b"reviewed-xml", "person.bin": b"reviewed-weights"}
    artifacts = []
    for name, raw in files.items():
        (root / name).write_bytes(raw)
        artifacts.append(
            {
                "relative_path": name,
                "bytes": len(raw),
                "sha384": hashlib.sha384(raw).hexdigest(),
            }
        )
    manifest = {"runtime_versions": {"openvino": "2026.3.1"}, "artifacts": artifacts}
    monkeypatch.setattr(module, "_manifest", lambda: manifest)
    monkeypatch.setattr(module.metadata, "version", lambda _name: "2026.3.1")
    return module.OperatorPersonConfiguration(root), manifest, files


def document(root):
    return {
        "schema_version": 1,
        "provider": module.PERSON_PROVIDER,
        "model_revision": module.MODEL_REVISION,
        "artifact_root": str(root),
    }


def test_absent_optional_config_does_not_admit_or_construct_native(monkeypatch):
    monkeypatch.setattr(module, "validate_person_runtime", lambda _config: pytest.fail("admitted"))
    assert module.load_person_configuration({}) is None


def test_explicit_local_configuration_admits_exact_model_and_dependency_versions(
    admitted, tmp_path
):
    config, _manifest, files = admitted
    path = tmp_path / "person.json"
    path.write_text(json.dumps(document(config.artifact_root)))
    assert module.load_person_configuration({module.PERSON_CONFIG_ENV: str(path)}) == config
    assert module._model_bytes(config) == tuple(files.values())


@pytest.mark.parametrize("change", ["provider", "revision", "bool_schema", "extra", "relative"])
def test_unreviewed_or_nonlocal_configuration_fails_closed(admitted, tmp_path, change):
    config, _manifest, _files = admitted
    value = document(config.artifact_root)
    if change == "provider":
        value["provider"] = "private-unreviewed-provider"
    elif change == "revision":
        value["model_revision"] = "private-unreviewed-revision"
    elif change == "bool_schema":
        value["schema_version"] = True
    elif change == "extra":
        value["source_url"] = "private-credential-source"
    else:
        value["artifact_root"] = "relative/private-model"
    path = tmp_path / "person.json"
    path.write_text(json.dumps(value))
    with pytest.raises(module.OperatorPersonConfigurationError) as error:
        module.load_person_configuration({module.PERSON_CONFIG_ENV: str(path)})
    assert str(error.value) == "Person configuration was refused."


@pytest.mark.parametrize("raw", [b"{", b"a" * 4097, b'{"schema_version":1,"schema_version":1}'])
def test_malformed_oversized_and_duplicate_json_are_refused(tmp_path, raw):
    path = tmp_path / "private-model.json"
    path.write_bytes(raw)
    with pytest.raises(module.OperatorPersonConfigurationError, match="configuration was refused"):
        module.load_person_configuration({module.PERSON_CONFIG_ENV: str(path)})


@pytest.mark.parametrize("change", ["version", "size", "content", "missing", "link"])
def test_runtime_or_model_tampering_is_refused(admitted, monkeypatch, change):
    config, _manifest, _files = admitted
    path = config.artifact_root / "person.bin"
    if change == "version":
        monkeypatch.setattr(module.metadata, "version", lambda _name: "unreviewed")
    elif change == "size":
        path.write_bytes(b"wrong-size")
    elif change == "content":
        path.write_bytes(bytes(len(path.read_bytes())))
    elif change == "missing":
        path.unlink()
    else:
        path.unlink()
        path.symlink_to(config.artifact_root / "person.xml")
    with pytest.raises(module.OperatorPersonConfigurationError) as error:
        module.validate_person_runtime(config)
    assert str(error.value) == "Person runtime admission failed."


@pytest.mark.parametrize("newline", ["lf", "crlf", "mixed", "changed"])
def test_manifest_accepts_only_exact_reviewed_lf_or_crlf_bytes(monkeypatch, tmp_path, newline):
    original = module._MANIFEST.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(original).hexdigest() == module.PERSON_MANIFEST_SHA256
    if newline == "crlf":
        raw = original.replace(b"\n", b"\r\n")
        assert hashlib.sha256(raw).hexdigest() == module.PERSON_MANIFEST_CRLF_SHA256
    elif newline == "mixed":
        raw = original.replace(b"\n", b"\r\n", 1)
    elif newline == "changed":
        raw = original.replace(b"Apache-2.0", b"Apache-3.0")
    else:
        raw = original
    path = tmp_path / "model.json"
    path.write_bytes(raw)
    monkeypatch.setattr(module, "_MANIFEST", path)
    if newline in {"lf", "crlf"}:
        assert module._manifest()["source_revision"] == module.MODEL_REVISION
    else:
        with pytest.raises(ValueError):
            module._manifest()


def test_native_model_read_uses_verified_immutable_bytes_after_admission(admitted):
    config, _manifest, _files = admitted
    module.validate_person_runtime(config)
    xml, weights = module._model_bytes(config)
    (config.artifact_root / "person.bin").write_bytes(b"changed-after-admission")
    assert weights == b"reviewed-weights" and xml == b"reviewed-xml"
    with pytest.raises(ValueError, match="bytes were refused"):
        module._model_bytes(config)


def test_model_byte_read_rejects_same_content_replaced_inode(admitted, monkeypatch):
    config, _manifest, _files = admitted
    original = module._local_path
    path = config.artifact_root / "person.xml"
    visits = []

    def changed_path(value, *, directory):
        admitted_path = original(value, directory=directory)
        if admitted_path == path:
            visits.append(1)
            if len(visits) == 2:
                path.rename(path.with_name("moved.xml"))
                path.write_bytes(b"reviewed-xml")
        return admitted_path

    monkeypatch.setattr(module, "_local_path", changed_path)
    with pytest.raises(ValueError, match="bytes were refused"):
        module._model_bytes(config)


def test_independent_factory_does_not_import_or_initialize_analytics_lab(admitted, monkeypatch):
    config, _manifest, _files = admitted
    instances = []

    class Detector:
        def __init__(self, value):
            assert value == config
            instances.append(self)

        def infer(self, *args):
            return ()

    monkeypatch.setattr(module, "_PersonDetector", Detector)
    monkeypatch.setattr(lifecycle, "_DetectorTracker", lambda _: pytest.fail("restricted detector"))

    async def run():
        factory = module.PersonProviderFactory(config)
        providers = [await factory() for _ in range(4)]
        with pytest.raises(RuntimeError, match="capacity"):
            await factory()
        value = PresentationVideoFrame(memoryview(bytes(64)), 4, 4, 16, PixelFormat.BGRX, 1)
        assert await providers[0](value) == ()
        for provider in providers:
            await provider.aclose()
        next_provider = await factory()
        await next_provider.aclose()

    asyncio.run(run())
    assert len(instances) == 5


def test_cancelled_native_person_call_reserves_capacity_until_real_completion(
    admitted, monkeypatch
):
    config, _manifest, _files = admitted
    entered, release = threading.Event(), threading.Event()

    class Detector:
        def __init__(self, _config):
            pass

        def infer(self, *args):
            entered.set()
            assert release.wait(3)
            return ()

    monkeypatch.setattr(module, "_PersonDetector", Detector)

    async def run():
        factory = module.PersonProviderFactory(config)
        factory._capacity = threading.BoundedSemaphore(1)
        provider = await factory()
        value = PresentationVideoFrame(memoryview(bytes(64)), 4, 4, 16, PixelFormat.BGRX, 1)
        pending = asyncio.create_task(provider(value))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            with monkeypatch.context() as unfinished_call:
                unfinished_call.setattr(lifecycle, "_CLOSE_TIMEOUT_SECONDS", 0.01)
                with pytest.raises(RuntimeError, match="cleanup is incomplete"):
                    await provider.aclose()
            with pytest.raises(RuntimeError, match="capacity"):
                await factory()
        finally:
            release.set()
        for _ in range(100):
            if provider._released:
                break
            await asyncio.sleep(0.01)
        assert provider._released
        next_provider = await factory()
        await next_provider.aclose()

    asyncio.run(run())
