"""Qualify the narrow native GStreamer ABI used by the playback decoder bridge.

Only logical library identities and required export names are retained. Runtime
paths, DLL search paths, host identity, media, and credentials are never evidence.
"""

from __future__ import annotations

import argparse
import ctypes
import enum
import json
import os
import pathlib
import typing
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, Field, field_validator

_RUNTIME_VERSION = "1.28.7"


class NativeAbiErrorCode(enum.StrEnum):
    ROOT_UNAVAILABLE = "root_unavailable"
    LIBRARY_MISSING = "library_missing"
    LIBRARY_LOAD_FAILURE = "library_load_failure"
    SYMBOL_MISSING = "symbol_missing"


class NativeAbiError(RuntimeError):
    """Sanitized native-ABI qualification failure."""

    def __init__(self, code: NativeAbiErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class NativeLibraryEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    library: typing.Literal["gstreamer-core", "gstreamer-app"]
    runtime_filename: str = Field(min_length=1, max_length=96)
    required_exports: tuple[str, ...]

    @field_validator("runtime_filename")
    @classmethod
    def _basename_only(cls, value: str) -> str:
        if pathlib.PurePath(value).name != value or "/" in value or "\\" in value:
            raise ValueError("runtime filename must be a basename")
        return value


class NativeAbiEvidence(BaseModel):
    """Path-free exact-revision ABI evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    runtime_family: typing.Literal["gstreamer"] = "gstreamer"
    runtime_version: typing.Literal["1.28.7"] = _RUNTIME_VERSION
    bridge: typing.Literal["ctypes-native-abi"] = "ctypes-native-abi"
    libraries: tuple[NativeLibraryEvidence, ...]

    @field_validator("libraries")
    @classmethod
    def _exact_library_set(
        cls,
        value: tuple[NativeLibraryEvidence, ...],
    ) -> tuple[NativeLibraryEvidence, ...]:
        if tuple(item.library for item in value) != ("gstreamer-core", "gstreamer-app"):
            raise ValueError("native ABI evidence has an unexpected library set")
        return value


_CORE_FILENAMES = ("gstreamer-1.0-0.dll", "libgstreamer-1.0-0.dll")
_APP_FILENAMES = ("gstapp-1.0-0.dll", "libgstapp-1.0-0.dll")

_CORE_EXPORTS = (
    "gst_init_check",
    "gst_parse_launch",
    "gst_bin_get_by_name",
    "gst_element_set_state",
    "gst_object_unref",
    "gst_buffer_new_allocate",
    "gst_buffer_fill",
    "gst_buffer_get_size",
    "gst_buffer_map",
    "gst_buffer_unmap",
    "gst_sample_get_buffer",
    "gst_sample_get_caps",
    "gst_caps_get_structure",
    "gst_structure_get_int",
    "gst_mini_object_unref",
)

_APP_EXPORTS = (
    "gst_app_src_push_buffer",
    "gst_app_src_end_of_stream",
    "gst_app_sink_try_pull_sample",
    "gst_app_sink_is_eos",
)


def _find_library(bin_root: pathlib.Path, names: tuple[str, ...]) -> pathlib.Path:
    for name in names:
        candidate = bin_root / name
        if candidate.is_file():
            return candidate
    raise NativeAbiError(
        NativeAbiErrorCode.LIBRARY_MISSING,
        "required native decoder library is unavailable",
    )


def _load_library(
    path: pathlib.Path,
    loader: Callable[[str], object],
) -> object:
    try:
        return loader(str(path))
    except (OSError, ValueError):
        raise NativeAbiError(
            NativeAbiErrorCode.LIBRARY_LOAD_FAILURE,
            "required native decoder library failed to load",
        ) from None


def _verify_exports(library: object, exports: tuple[str, ...]) -> None:
    for symbol in exports:
        try:
            getattr(library, symbol)
        except AttributeError:
            raise NativeAbiError(
                NativeAbiErrorCode.SYMBOL_MISSING,
                "required native decoder ABI export is unavailable",
            ) from None


def qualify_native_abi(
    revision: str,
    runtime_root: pathlib.Path,
    *,
    loader: Callable[[str], object] | None = None,
) -> NativeAbiEvidence:
    """Qualify the exact native ABI from the isolated K5 GStreamer root."""
    root = runtime_root.expanduser().resolve(strict=False)
    bin_root = root / "bin"
    if not root.is_dir() or not bin_root.is_dir():
        raise NativeAbiError(
            NativeAbiErrorCode.ROOT_UNAVAILABLE,
            "isolated decoder runtime root is unavailable",
        )

    selected_loader = loader
    if selected_loader is None:
        selected_loader = getattr(ctypes, "WinDLL", None)
        if selected_loader is None:
            raise NativeAbiError(
                NativeAbiErrorCode.LIBRARY_LOAD_FAILURE,
                "native Windows decoder ABI loader is unavailable",
            )

    core_path = _find_library(bin_root, _CORE_FILENAMES)
    app_path = _find_library(bin_root, _APP_FILENAMES)
    core = _load_library(core_path, selected_loader)
    app = _load_library(app_path, selected_loader)
    _verify_exports(core, _CORE_EXPORTS)
    _verify_exports(app, _APP_EXPORTS)

    return NativeAbiEvidence(
        revision=revision,
        libraries=(
            NativeLibraryEvidence(
                library="gstreamer-core",
                runtime_filename=core_path.name,
                required_exports=_CORE_EXPORTS,
            ),
            NativeLibraryEvidence(
                library="gstreamer-app",
                runtime_filename=app_path.name,
                required_exports=_APP_EXPORTS,
            ),
        ),
    )


def write_evidence(path: pathlib.Path, evidence: NativeAbiEvidence) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(evidence.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Qualify the Stage-19 native decoder ABI")
    parser.add_argument("--revision", required=True)
    parser.add_argument("--runtime-root", type=pathlib.Path)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()

    runtime_root = args.runtime_root
    if runtime_root is None:
        value = os.environ.get("K5_GSTREAMER_ROOT", "").strip()
        if not value:
            return 2
        runtime_root = pathlib.Path(value)

    try:
        evidence = qualify_native_abi(args.revision, runtime_root)
        write_evidence(args.output, evidence)
    except (NativeAbiError, ValueError):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
