from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import threading
from concurrent.futures import Future
from types import ModuleType, SimpleNamespace

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


def test_windows_native_cpu_dispatch_caps_amx_and_bf16_before_inference(monkeypatch):
    # GitHub-hosted Windows has previously hard-exited inside OpenVINO infer.
    # Keep actual detection and model admission unchanged while constraining
    # unsupported native kernel selection to the OpenVINO 2026 AVX2 floor.
    monkeypatch.setattr(module, "system", lambda: "Windows")
    for name in ("ONEDNN_MAX_CPU_ISA", "OV_CPU_MAX_ISA"):
        monkeypatch.setenv(name, "AVX512_CORE_AMX")
    module._cap_windows_native_cpu_isa()
    for name in ("ONEDNN_MAX_CPU_ISA", "OV_CPU_MAX_ISA"):
        assert module.os.environ[name] == "AVX2"

    monkeypatch.setattr(module, "system", lambda: "Linux")
    for name in ("ONEDNN_MAX_CPU_ISA", "OV_CPU_MAX_ISA"):
        monkeypatch.setenv(name, "AVX512_CORE_AMX")
    module._cap_windows_native_cpu_isa()
    for name in ("ONEDNN_MAX_CPU_ISA", "OV_CPU_MAX_ISA"):
        assert module.os.environ[name] == "AVX512_CORE_AMX"


def test_absent_optional_config_does_not_admit_or_construct_native(monkeypatch):
    monkeypatch.setattr(module, "validate_person_runtime", lambda _config: pytest.fail("admitted"))
    assert module.load_person_configuration({}) is None


def test_absent_installed_model_preserves_source_only_installations(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "__file__", str(tmp_path / "operator_analytics_person.py"))
    monkeypatch.setattr(module, "validate_person_runtime", lambda _config: pytest.fail("admitted"))
    assert module.load_installed_person_configuration({}) is None


def test_installed_model_is_admitted_without_environment_or_external_configuration(
    admitted, tmp_path, monkeypatch
):
    _config, _manifest, files = admitted
    root = tmp_path / "data" / "person-models"
    root.mkdir(parents=True)
    for name, content in files.items():
        (root / name).write_bytes(content)
    monkeypatch.setattr(module, "__file__", str(tmp_path / "operator_analytics_person.py"))
    assert module.load_installed_person_configuration({}) == module.OperatorPersonConfiguration(
        root
    )
    (root / "person.bin").write_bytes(b"tampered")
    with pytest.raises(module.OperatorPersonConfigurationError, match="admission"):
        module.load_installed_person_configuration({})


def test_explicit_engineering_configuration_takes_precedence_over_installed_model(
    admitted, tmp_path, monkeypatch
):
    config, _manifest, _files = admitted
    path = tmp_path / "explicit-person.json"
    path.write_text(json.dumps(document(config.artifact_root)))
    monkeypatch.setattr(module, "__file__", str(tmp_path / "operator_analytics_person.py"))
    assert (
        module.load_installed_person_configuration({module.PERSON_CONFIG_ENV: str(path)}) == config
    )


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


@pytest.mark.parametrize("precision_status", ["fp32", "rejected", "unavailable"])
@pytest.mark.parametrize("inference_threads", [1, 2])
def test_person_detector_reads_and_compiles_with_one_admitted_openvino_core(
    admitted, monkeypatch, precision_status, inference_threads
):
    """Cold native view compilation must reuse the core and admit FP32 only."""
    config, _manifest, _files = admitted
    calls = []

    class Model:
        inputs = (object(),)
        outputs = (object(),)

        def input(self, name):
            assert name == "data"
            return SimpleNamespace(shape=(1, 3, 320, 544))

        def output(self, index):
            assert index == 0
            return SimpleNamespace(shape=(1, 1, 200, 7))

    model = Model()

    class Core:
        def __init__(self):
            calls.append("core")

        def read_model(self, *, model: str, weights):
            assert model == "reviewed-xml"
            assert weights == b"reviewed-weights"
            calls.append("read")
            return admitted_model

        def compile_model(self, value, device, settings):
            assert value is admitted_model
            assert device == "CPU"
            assert settings == {
                "INFERENCE_NUM_THREADS": inference_threads,
                "NUM_STREAMS": "1",
                "INFERENCE_PRECISION_HINT": openvino_stub.Type.f32,
            }
            calls.append("compile")

            def get_property(name):
                assert name == "INFERENCE_PRECISION_HINT"
                if precision_status == "unavailable":
                    raise RuntimeError("Native details must remain private")
                if precision_status == "rejected":
                    return openvino_stub.Type.u8
                return openvino_stub.Type.f32

            return SimpleNamespace(create_infer_request=lambda: "ready", get_property=get_property)

    admitted_model = model

    class Chain:
        def __getattr__(self, _name):
            return lambda *args, **kwargs: self

    class PrePost:
        def __init__(self, value):
            assert value is admitted_model

        def input(self):
            return Chain()

        def build(self):
            return admitted_model

    numpy_stub = ModuleType("numpy")
    numpy_stub.uint8 = object()
    numpy_stub.frombuffer = lambda raw, dtype: raw
    openvino_stub = ModuleType("openvino")
    openvino_stub.__path__ = []
    openvino_stub.Core = Core
    openvino_stub.Tensor = lambda data: data
    openvino_stub.Type = SimpleNamespace(u8=object(), f32=object())
    openvino_stub.Layout = lambda text: text
    preprocess_stub = ModuleType("openvino.preprocess")
    preprocess_stub.ColorFormat = SimpleNamespace(BGR="BGR")
    preprocess_stub.ResizeAlgorithm = SimpleNamespace(RESIZE_LINEAR="linear")
    preprocess_stub.PrePostProcessor = PrePost
    monkeypatch.setitem(sys.modules, "numpy", numpy_stub)
    monkeypatch.setitem(sys.modules, "openvino", openvino_stub)
    monkeypatch.setitem(sys.modules, "openvino.preprocess", preprocess_stub)
    monkeypatch.setattr(module, "_cap_windows_native_cpu_isa", lambda: None)

    if precision_status == "fp32":
        detector = module._PersonDetector(config, inference_threads=inference_threads)
        assert detector._request == "ready"
    else:
        with pytest.raises(ValueError, match="Person CPU precision was refused"):
            module._PersonDetector(config, inference_threads=inference_threads)
    assert calls == ["core", "read", "compile"]


@pytest.mark.parametrize("threads", [0, 3, True, 1.0])
def test_person_detector_rejects_unapproved_native_cpu_budget(admitted, threads):
    config, _manifest, _files = admitted
    with pytest.raises(ValueError, match="Person CPU thread setting was refused"):
        module._PersonDetector(config, inference_threads=threads)


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


def test_sealed_person_start_has_its_own_bounded_compilation_budget(monkeypatch):
    assert lifecycle._START_TIMEOUT_SECONDS == 15.0
    assert lifecycle._CLOSE_TIMEOUT_SECONDS == 2.0
    assert 15.0 < module._PERSON_START_TIMEOUT_SECONDS < 24.0
    session = object.__new__(module._PersonSession)
    pending = Future()
    session._pending = pending

    async def run():
        # A timed-out waiter must not cancel the native compile Future.
        # The existing worker lifecycle still owns its capacity release.
        with monkeypatch.context() as short:
            short.setattr(module, "_PERSON_START_TIMEOUT_SECONDS", 0.01)
            with pytest.raises(TimeoutError):
                await session.start()
        assert not pending.done()
        pending.set_result(True)
        await session.start()

    asyncio.run(run())


def test_independent_factory_does_not_import_or_initialize_analytics_lab(admitted, monkeypatch):
    config, _manifest, _files = admitted
    instances = []

    class Detector:
        def __init__(self, value, *, inference_threads):
            assert value == config
            assert inference_threads == 1
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
        def __init__(self, _config, *, inference_threads):
            assert inference_threads == 1

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
