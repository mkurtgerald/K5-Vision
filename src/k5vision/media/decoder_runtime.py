"""Source-free qualification for the Stage-18 concrete H.264 decoder runtime.

The qualification process inventories only normalized GStreamer element metadata.
Raw ``gst-inspect`` output, executable paths, host identity, and media sources are
never written to the retained evidence contract.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import shutil
import subprocess
import typing
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, field_validator

_RUNTIME_VERSION = "1.28.7"
_DECODER = "d3d11h264dec"
_MAX_INSPECT_BYTES = 1_048_576


class DecoderRuntimeErrorCode(typing.StrEnum):
    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    INSPECT_FAILURE = "inspect_failure"
    METADATA_INVALID = "metadata_invalid"
    ELEMENT_MISSING = "element_missing"
    VERSION_MISMATCH = "version_mismatch"
    LICENSE_UNAPPROVED = "license_unapproved"
    PROVENANCE_MISMATCH = "provenance_mismatch"


class DecoderRuntimeError(RuntimeError):
    """Sanitized runtime-qualification failure."""

    def __init__(self, code: DecoderRuntimeErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ElementRequirement:
    element: str
    plugin: str
    source_module: str
    role: str


_REQUIREMENTS: tuple[ElementRequirement, ...] = (
    ElementRequirement("appsrc", "app", "gst-plugins-base", "packet-source"),
    ElementRequirement("rtph264depay", "rtp", "gst-plugins-good", "rtp-depayloader"),
    ElementRequirement("h264parse", "videoparsersbad", "gst-plugins-bad", "h264-parser"),
    ElementRequirement(_DECODER, "d3d11", "gst-plugins-bad", "h264-decoder"),
    ElementRequirement("videoconvert", "videoconvertscale", "gst-plugins-base", "converter"),
    ElementRequirement("appsink", "app", "gst-plugins-base", "frame-sink"),
)


class DecoderElementEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    element: str = Field(min_length=1, max_length=64)
    role: str = Field(min_length=1, max_length=64)
    plugin: str = Field(min_length=1, max_length=128)
    source_module: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=32)
    license: str = Field(min_length=1, max_length=64)


class DecoderRuntimeEvidence(BaseModel):
    """Retained exact-revision decoder-runtime evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    runtime_family: typing.Literal["gstreamer"] = "gstreamer"
    runtime_version: typing.Literal["1.28.7"] = _RUNTIME_VERSION
    selected_decoder: typing.Literal["d3d11h264dec"] = _DECODER
    installer_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    elements: tuple[DecoderElementEvidence, ...]

    @field_validator("elements")
    @classmethod
    def _exact_element_set(
        cls,
        value: tuple[DecoderElementEvidence, ...],
    ) -> tuple[DecoderElementEvidence, ...]:
        expected = tuple(item.element for item in _REQUIREMENTS)
        actual = tuple(item.element for item in value)
        if actual != expected:
            raise ValueError("decoder runtime evidence has an unexpected element set")
        return value


_METADATA_PATTERNS = {
    "plugin": re.compile(r"^\s*Name\s+(.+?)\s*$", re.MULTILINE),
    "version": re.compile(r"^\s*Version\s+(.+?)\s*$", re.MULTILINE),
    "license": re.compile(r"^\s*License\s+(.+?)\s*$", re.MULTILINE),
    "source_module": re.compile(r"^\s*Source module\s+(.+?)\s*$", re.MULTILINE),
}


def parse_element_metadata(
    requirement: ElementRequirement,
    output: str,
) -> DecoderElementEvidence:
    """Parse only normalized provenance fields from one gst-inspect response."""
    values: dict[str, str] = {}
    for key, pattern in _METADATA_PATTERNS.items():
        match = pattern.search(output)
        if match is None:
            raise DecoderRuntimeError(
                DecoderRuntimeErrorCode.METADATA_INVALID,
                "decoder runtime element metadata is incomplete",
            )
        values[key] = match.group(1).strip()

    if values["plugin"] != requirement.plugin:
        raise DecoderRuntimeError(
            DecoderRuntimeErrorCode.PROVENANCE_MISMATCH,
            "decoder runtime plugin identity did not match the reviewed requirement",
        )
    if values["source_module"] != requirement.source_module:
        raise DecoderRuntimeError(
            DecoderRuntimeErrorCode.PROVENANCE_MISMATCH,
            "decoder runtime source module did not match the reviewed requirement",
        )
    if values["version"] != _RUNTIME_VERSION:
        raise DecoderRuntimeError(
            DecoderRuntimeErrorCode.VERSION_MISMATCH,
            "decoder runtime element version did not match the pinned runtime",
        )
    if values["license"] != "LGPL":
        raise DecoderRuntimeError(
            DecoderRuntimeErrorCode.LICENSE_UNAPPROVED,
            "decoder runtime element license is outside the Stage-18 reviewed set",
        )

    return DecoderElementEvidence(
        element=requirement.element,
        role=requirement.role,
        plugin=values["plugin"],
        source_module=values["source_module"],
        version=values["version"],
        license=values["license"],
    )


def _inspect_element(executable: str, requirement: ElementRequirement) -> DecoderElementEvidence:
    try:
        result = subprocess.run(
            [executable, requirement.element],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise DecoderRuntimeError(
            DecoderRuntimeErrorCode.INSPECT_FAILURE,
            "decoder runtime inspection failed",
        ) from None

    if result.returncode != 0:
        raise DecoderRuntimeError(
            DecoderRuntimeErrorCode.ELEMENT_MISSING,
            "required decoder runtime element is unavailable",
        )
    if len(result.stdout) > _MAX_INSPECT_BYTES:
        raise DecoderRuntimeError(
            DecoderRuntimeErrorCode.METADATA_INVALID,
            "decoder runtime inspection output exceeded the bounded limit",
        )
    try:
        output = result.stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise DecoderRuntimeError(
            DecoderRuntimeErrorCode.METADATA_INVALID,
            "decoder runtime inspection output was not valid UTF-8",
        ) from None
    return parse_element_metadata(requirement, output)


def qualify_decoder_runtime(
    revision: str,
    *,
    installer_sha256: str | None = None,
    inspect_executable: str | None = None,
) -> DecoderRuntimeEvidence:
    """Qualify the exact pinned Windows playback decode surface."""
    executable = inspect_executable or shutil.which("gst-inspect-1.0")
    if not executable:
        raise DecoderRuntimeError(
            DecoderRuntimeErrorCode.RUNTIME_UNAVAILABLE,
            "reviewed decoder runtime is unavailable",
        )

    elements = tuple(_inspect_element(executable, item) for item in _REQUIREMENTS)
    return DecoderRuntimeEvidence(
        revision=revision,
        installer_sha256=installer_sha256,
        elements=elements,
    )


def write_evidence(path: pathlib.Path, evidence: DecoderRuntimeEvidence) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(evidence.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Qualify the Stage-18 decoder runtime surface")
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--installer-sha256")
    args = parser.parse_args()

    try:
        evidence = qualify_decoder_runtime(
            args.revision,
            installer_sha256=args.installer_sha256,
        )
        write_evidence(args.output, evidence)
    except (DecoderRuntimeError, ValueError):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
