from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from k5vision.media import gstreamer_native_abi as abi
from k5vision.media.gstreamer_native_abi import (
    NativeAbiError,
    NativeAbiErrorCode,
    NativeAbiEvidence,
    NativeLibraryEvidence,
    qualify_native_abi,
    write_evidence,
)

_REVISION = "a" * 40


class FakeLibrary:
    def __init__(self, exports: tuple[str, ...]) -> None:
        for symbol in exports:
            setattr(self, symbol, object())


def _runtime(tmp_path: Path) -> Path:
    root = tmp_path / "private-runtime-root"
    bin_root = root / "bin"
    bin_root.mkdir(parents=True)
    (bin_root / "gstreamer-1.0-0.dll").write_bytes(b"core")
    (bin_root / "gstapp-1.0-0.dll").write_bytes(b"app")
    return root


def test_qualification_retains_only_logical_abi_evidence(tmp_path: Path) -> None:
    root = _runtime(tmp_path)
    loads: list[str] = []

    def loader(path: str) -> object:
        loads.append(path)
        if path.endswith("gstreamer-1.0-0.dll"):
            return FakeLibrary(abi._CORE_EXPORTS)
        return FakeLibrary(abi._APP_EXPORTS)

    evidence = qualify_native_abi(_REVISION, root, loader=loader)

    assert len(loads) == 2
    assert evidence.runtime_version == "1.28.7"
    assert evidence.bridge == "ctypes-native-abi"
    assert [item.library for item in evidence.libraries] == [
        "gstreamer-core",
        "gstreamer-app",
    ]
    serialized = evidence.model_dump_json()
    assert str(tmp_path) not in serialized
    assert "private-runtime-root" not in serialized
    assert "gstreamer-1.0-0.dll" in serialized
    assert "gst_app_sink_try_pull_sample" in serialized


def test_lib_prefixed_runtime_filenames_are_supported(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    bin_root = root / "bin"
    bin_root.mkdir(parents=True)
    (bin_root / "libgstreamer-1.0-0.dll").write_bytes(b"core")
    (bin_root / "libgstapp-1.0-0.dll").write_bytes(b"app")

    def loader(path: str) -> object:
        if path.endswith("libgstreamer-1.0-0.dll"):
            return FakeLibrary(abi._CORE_EXPORTS)
        return FakeLibrary(abi._APP_EXPORTS)

    evidence = qualify_native_abi(_REVISION, root, loader=loader)
    assert [item.runtime_filename for item in evidence.libraries] == [
        "libgstreamer-1.0-0.dll",
        "libgstapp-1.0-0.dll",
    ]


def test_missing_runtime_root_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(NativeAbiError) as caught:
        qualify_native_abi(_REVISION, tmp_path / "missing", loader=lambda _path: object())
    assert caught.value.code == NativeAbiErrorCode.ROOT_UNAVAILABLE
    assert str(tmp_path) not in str(caught.value)


def test_missing_library_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    (root / "bin").mkdir(parents=True)
    with pytest.raises(NativeAbiError) as caught:
        qualify_native_abi(_REVISION, root, loader=lambda _path: object())
    assert caught.value.code == NativeAbiErrorCode.LIBRARY_MISSING


def test_library_load_failure_is_sanitized(tmp_path: Path) -> None:
    root = _runtime(tmp_path)

    def loader(_path: str) -> object:
        raise OSError("C:/private/runtime/detail.dll")

    with pytest.raises(NativeAbiError) as caught:
        qualify_native_abi(_REVISION, root, loader=loader)
    assert caught.value.code == NativeAbiErrorCode.LIBRARY_LOAD_FAILURE
    assert "private" not in str(caught.value)


def test_missing_export_fails_closed_without_symbol_or_path_leak(tmp_path: Path) -> None:
    root = _runtime(tmp_path)

    def loader(path: str) -> object:
        if path.endswith("gstreamer-1.0-0.dll"):
            return FakeLibrary(abi._CORE_EXPORTS[:-1])
        return FakeLibrary(abi._APP_EXPORTS)

    with pytest.raises(NativeAbiError) as caught:
        qualify_native_abi(_REVISION, root, loader=loader)
    assert caught.value.code == NativeAbiErrorCode.SYMBOL_MISSING
    assert str(tmp_path) not in str(caught.value)
    assert "gst_mini_object_unref" not in str(caught.value)


def test_evidence_rejects_path_bearing_library_filename() -> None:
    with pytest.raises(ValidationError):
        NativeLibraryEvidence(
            library="gstreamer-core",
            runtime_filename="C:/private/gstreamer-1.0-0.dll",
            required_exports=abi._CORE_EXPORTS,
        )


def test_evidence_requires_exact_library_order() -> None:
    app = NativeLibraryEvidence(
        library="gstreamer-app",
        runtime_filename="gstapp-1.0-0.dll",
        required_exports=abi._APP_EXPORTS,
    )
    core = NativeLibraryEvidence(
        library="gstreamer-core",
        runtime_filename="gstreamer-1.0-0.dll",
        required_exports=abi._CORE_EXPORTS,
    )
    with pytest.raises(ValidationError):
        NativeAbiEvidence(revision=_REVISION, libraries=(app, core))


def test_write_evidence_never_records_destination_path(tmp_path: Path) -> None:
    evidence = NativeAbiEvidence(
        revision=_REVISION,
        libraries=(
            NativeLibraryEvidence(
                library="gstreamer-core",
                runtime_filename="gstreamer-1.0-0.dll",
                required_exports=abi._CORE_EXPORTS,
            ),
            NativeLibraryEvidence(
                library="gstreamer-app",
                runtime_filename="gstapp-1.0-0.dll",
                required_exports=abi._APP_EXPORTS,
            ),
        ),
    )
    output = tmp_path / "private-output" / "evidence.json"
    write_evidence(output, evidence)

    text = output.read_text(encoding="utf-8")
    parsed = json.loads(text)
    assert parsed["revision"] == _REVISION
    assert str(tmp_path) not in text
    assert "private-output" not in text
