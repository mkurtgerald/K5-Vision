from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from k5vision.media import decoder_runtime
from k5vision.media.decoder_runtime import (
    DecoderElementEvidence,
    DecoderRuntimeError,
    DecoderRuntimeErrorCode,
    DecoderRuntimeEvidence,
    ElementRequirement,
    parse_element_metadata,
    qualify_decoder_runtime,
    write_evidence,
)

_REVISION = "a" * 40
_INSTALLER_SHA = "b" * 64


def _inspect_text(
    *,
    plugin: str = "app",
    version: str = "1.28.7",
    license_name: str = "LGPL",
    source_module: str = "gst-plugins-base",
) -> str:
    return f"""
Factory Details:
  Rank                     none (0)
Plugin Details:
  Name                     {plugin}
  Description              Application helper plugin
  Filename                 C:\\private\\do-not-retain.dll
  Version                  {version}
  License                  {license_name}
  Source module            {source_module}
  Binary package           GStreamer Base Plug-ins
"""


def test_parse_metadata_returns_only_normalized_fields() -> None:
    requirement = ElementRequirement("appsrc", "app", "gst-plugins-base", "packet-source")
    evidence = parse_element_metadata(requirement, _inspect_text())

    assert evidence == DecoderElementEvidence(
        element="appsrc",
        role="packet-source",
        plugin="app",
        source_module="gst-plugins-base",
        version="1.28.7",
        license="LGPL",
    )
    serialized = json.dumps(evidence.model_dump())
    assert "private" not in serialized
    assert "Filename" not in serialized


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"version": "1.28.6"}, DecoderRuntimeErrorCode.VERSION_MISMATCH),
        ({"license_name": "GPL"}, DecoderRuntimeErrorCode.LICENSE_UNAPPROVED),
        ({"plugin": "other"}, DecoderRuntimeErrorCode.PROVENANCE_MISMATCH),
        ({"source_module": "other"}, DecoderRuntimeErrorCode.PROVENANCE_MISMATCH),
    ],
)
def test_metadata_mismatch_fails_closed(
    kwargs: dict[str, str],
    code: DecoderRuntimeErrorCode,
) -> None:
    requirement = ElementRequirement("appsrc", "app", "gst-plugins-base", "packet-source")
    with pytest.raises(DecoderRuntimeError) as caught:
        parse_element_metadata(requirement, _inspect_text(**kwargs))
    assert caught.value.code == code
    assert "private" not in str(caught.value)


def test_incomplete_metadata_fails_closed() -> None:
    requirement = ElementRequirement("appsrc", "app", "gst-plugins-base", "packet-source")
    with pytest.raises(DecoderRuntimeError) as caught:
        parse_element_metadata(requirement, "Plugin Details:\n  Name app\n")
    assert caught.value.code == DecoderRuntimeErrorCode.METADATA_INVALID


def _metadata_for_element(element: str) -> bytes:
    requirements = {item.element: item for item in decoder_runtime._REQUIREMENTS}
    item = requirements[element]
    return _inspect_text(plugin=item.plugin, source_module=item.source_module).encode()


def test_qualification_uses_argv_only_and_retains_source_free_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["stderr"] is subprocess.DEVNULL
        assert kwargs["stdout"] is subprocess.PIPE
        assert kwargs["check"] is False
        return subprocess.CompletedProcess(args, 0, stdout=_metadata_for_element(args[1]))

    monkeypatch.setattr(decoder_runtime.subprocess, "run", fake_run)
    evidence = qualify_decoder_runtime(
        _REVISION,
        installer_sha256=_INSTALLER_SHA,
        inspect_executable="gst-inspect-1.0",
    )

    assert [call[1] for call in calls] == [
        "appsrc",
        "rtph264depay",
        "h264parse",
        "d3d11h264dec",
        "videoconvert",
        "appsink",
    ]
    assert evidence.selected_decoder == "d3d11h264dec"
    assert evidence.runtime_version == "1.28.7"
    assert evidence.installer_sha256 == _INSTALLER_SHA
    serialized = evidence.model_dump_json()
    assert "private" not in serialized
    assert "runner" not in serialized.lower()


def test_missing_runtime_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(decoder_runtime.shutil, "which", lambda _name: None)
    with pytest.raises(DecoderRuntimeError) as caught:
        qualify_decoder_runtime(_REVISION)
    assert caught.value.code == DecoderRuntimeErrorCode.RUNTIME_UNAVAILABLE


def test_missing_element_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args, 1, stdout=b"")

    monkeypatch.setattr(decoder_runtime.subprocess, "run", fake_run)
    with pytest.raises(DecoderRuntimeError) as caught:
        qualify_decoder_runtime(_REVISION, inspect_executable="gst-inspect-1.0")
    assert caught.value.code == DecoderRuntimeErrorCode.ELEMENT_MISSING


def test_inspection_timeout_is_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(args, 10)

    monkeypatch.setattr(decoder_runtime.subprocess, "run", fake_run)
    with pytest.raises(DecoderRuntimeError) as caught:
        qualify_decoder_runtime(_REVISION, inspect_executable="C:/private/gst-inspect.exe")
    assert caught.value.code == DecoderRuntimeErrorCode.INSPECT_FAILURE
    assert "private" not in str(caught.value)


def _run_with_output(
    output: bytes,
):
    def fake_run(
        args: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args, 0, stdout=output)

    return fake_run


def test_non_utf8_or_oversized_output_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    outputs = [b"\xff", b"x" * (decoder_runtime._MAX_INSPECT_BYTES + 1)]

    for output in outputs:
        monkeypatch.setattr(decoder_runtime.subprocess, "run", _run_with_output(output))
        with pytest.raises(DecoderRuntimeError) as caught:
            qualify_decoder_runtime(_REVISION, inspect_executable="gst-inspect-1.0")
        assert caught.value.code == DecoderRuntimeErrorCode.METADATA_INVALID


def test_evidence_requires_exact_ordered_surface() -> None:
    item = DecoderElementEvidence(
        element="appsrc",
        role="packet-source",
        plugin="app",
        source_module="gst-plugins-base",
        version="1.28.7",
        license="LGPL",
    )
    with pytest.raises(ValidationError):
        DecoderRuntimeEvidence(revision=_REVISION, elements=(item,))


def test_evidence_write_contains_no_output_path(tmp_path: Path) -> None:
    elements = tuple(
        DecoderElementEvidence(
            element=item.element,
            role=item.role,
            plugin=item.plugin,
            source_module=item.source_module,
            version="1.28.7",
            license="LGPL",
        )
        for item in decoder_runtime._REQUIREMENTS
    )
    evidence = DecoderRuntimeEvidence(
        revision=_REVISION,
        installer_sha256=_INSTALLER_SHA,
        elements=elements,
    )
    output = tmp_path / "private-folder" / "evidence.json"
    write_evidence(output, evidence)

    content = output.read_text(encoding="utf-8")
    assert _REVISION in content
    assert _INSTALLER_SHA in content
    assert str(tmp_path) not in content
    assert "private-folder" not in content
