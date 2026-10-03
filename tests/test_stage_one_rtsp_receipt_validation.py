"""Generated-JSON checks for the retained RTSP joined receipt publication boundary."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "scripts" / "validate_stage_one_rtsp_witness.py"
_REVISION = "a" * 40
_ANALYTICS_REVISION = "b" * 40
_COUNTERS = (
    "delivered_frames",
    "presentations",
    "analytics_provider_calls",
    "analytics_tracked_detections",
    "analytics_provider_submissions",
    "analytics_provider_completions",
    "analytics_rendered_boxes",
)
_FLAGS = ("windows_live_launch_completed", "analytics_enabled", "rtsp_tcp_joined")
_ENVIRONMENT = (
    "K5_STAGE_ONE_RTSP_JOINED_ANALYTICS_OUTPUT",
    "K5_STAGE_ONE_REVISION",
    "ANALYTICS_LAB_SHA",
)
_PASS = "Stage One RTSP joined receipt validation passed\n"
_FAIL = "Stage One RTSP joined receipt validation failed\n"


def _load(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("generated_rtsp_receipt_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _receipt() -> dict[str, object]:
    return {
        "schema_version": "1",
        "revision": _REVISION,
        "analytics_revision": _ANALYTICS_REVISION,
        "execution_context": "reviewed-video-loopback-rtsp-windows-x64",
        "analytics_failures": 0,
        **dict.fromkeys(_FLAGS, True),
        **dict.fromkeys(_COUNTERS, 1),
    }


@pytest.fixture
def validator(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    path = tmp_path / "generated-receipt.json"
    path.write_text(json.dumps(_receipt()), encoding="utf-8")
    for name, value in zip(_ENVIRONMENT, (str(path), _REVISION, _ANALYTICS_REVISION), strict=True):
        monkeypatch.setenv(name, value)
    return SimpleNamespace(module=_load(_SCRIPT), path=path)


def _validate(validator: SimpleNamespace, document: object) -> None:
    validator.path.write_text(json.dumps(document), encoding="utf-8")
    validator.module.validate_receipt(
        validator.path, revision=_REVISION, analytics_revision=_ANALYTICS_REVISION
    )


@pytest.mark.parametrize("counter", [1, 1_000_000])
def test_accepts_existing_scalar_schema_without_rewriting(
    validator: SimpleNamespace, counter: int
) -> None:
    document = {**_receipt(), **dict.fromkeys(_COUNTERS, counter)}
    _validate(validator, document)
    assert validator.path.read_text(encoding="utf-8") == json.dumps(document)


def test_accepts_generated_receipt_from_existing_producer(validator: SimpleNamespace) -> None:
    producer = _load(_ROOT / "tests/integration/test_stage_one_joined_analytics_physical.py")
    metrics = SimpleNamespace(**_receipt())
    document = producer._source_free_evidence(
        metrics,
        provider_calls=1,
        tracked_detections=1,
        revision=_REVISION,
        analytics_revision=_ANALYTICS_REVISION,
    )
    document["execution_context"] = "reviewed-video-loopback-rtsp-windows-x64"
    document["rtsp_tcp_joined"] = True
    _validate(validator, document)


@pytest.mark.parametrize("document", [None, [], [_receipt()], "generated", 0, True])
def test_rejects_nonobjects(validator: SimpleNamespace, document: object) -> None:
    with pytest.raises(ValueError):
        _validate(validator, document)


@pytest.mark.parametrize("field", _receipt())
def test_rejects_each_missing_field(validator: SimpleNamespace, field: str) -> None:
    document = _receipt()
    del document[field]
    with pytest.raises(ValueError):
        _validate(validator, document)


@pytest.mark.parametrize("value", ["generated private detail", {}, [], None])
def test_rejects_unknown_fields(validator: SimpleNamespace, value: object) -> None:
    with pytest.raises(ValueError):
        _validate(validator, {**_receipt(), "source_uri": value})


@pytest.mark.parametrize("field", _receipt())
@pytest.mark.parametrize("value", [{"generated": 1}, [1]])
def test_rejects_nested_fields(validator: SimpleNamespace, field: str, value: object) -> None:
    with pytest.raises(ValueError):
        _validate(validator, {**_receipt(), field: value})


@pytest.mark.parametrize("field", _receipt())
def test_rejects_each_duplicate_field(validator: SimpleNamespace, field: str) -> None:
    document = _receipt()
    raw = json.dumps(document)[:-1] + f", {json.dumps(field)}: {json.dumps(document[field])}}}"
    validator.path.write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError, match="^duplicate receipt field$"):
        validator.module.validate_receipt(
            validator.path, revision=_REVISION, analytics_revision=_ANALYTICS_REVISION
        )


@pytest.mark.parametrize("field", _COUNTERS)
@pytest.mark.parametrize("value", [0, -1, 1_000_001, True, False, 1.0, "1", None])
def test_rejects_nonpositive_unbounded_or_noninteger_counters(
    validator: SimpleNamespace, field: str, value: object
) -> None:
    with pytest.raises(ValueError):
        _validate(validator, {**_receipt(), field: value})


@pytest.mark.parametrize("field", _FLAGS)
@pytest.mark.parametrize("value", [False, 0, 1, 1.0, "true", None])
def test_requires_strict_true_flags(validator: SimpleNamespace, field: str, value: object) -> None:
    with pytest.raises(ValueError):
        _validate(validator, {**_receipt(), field: value})


@pytest.mark.parametrize("value", [1, -1, False, True, 0.0, "0", None])
def test_requires_strict_integer_zero_failures(validator: SimpleNamespace, value: object) -> None:
    with pytest.raises(ValueError):
        _validate(validator, {**_receipt(), "analytics_failures": value})


@pytest.mark.parametrize("field", ["schema_version", "execution_context"])
@pytest.mark.parametrize("value", [1, True, None, "", "reviewed-video-windows-x64", "2"])
def test_requires_exact_schema_and_execution_context(
    validator: SimpleNamespace, field: str, value: object
) -> None:
    with pytest.raises(ValueError):
        _validate(validator, {**_receipt(), field: value})


@pytest.mark.parametrize("field", ["revision", "analytics_revision"])
@pytest.mark.parametrize("value", ["c" * 40, "a" * 39, "z" * 40, "main", "", True, None])
def test_requires_exact_receipt_revisions(
    validator: SimpleNamespace, field: str, value: object
) -> None:
    with pytest.raises(ValueError):
        _validate(validator, {**_receipt(), field: value})


def test_normalizes_only_expected_revision_case(validator: SimpleNamespace) -> None:
    validator.module.validate_receipt(
        validator.path,
        revision=_REVISION.upper(),
        analytics_revision=_ANALYTICS_REVISION.upper(),
    )
    with pytest.raises(ValueError):
        _validate(validator, {**_receipt(), "revision": _REVISION.upper()})


@pytest.mark.parametrize("name", _ENVIRONMENT[1:])
@pytest.mark.parametrize("value", ["main", "a" * 39, "z" * 40, "a" * 40 + "\n"])
def test_rejects_invalid_expected_revision_environment(
    validator: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv(name, value)
    assert validator.module.main() == 1
    assert capsys.readouterr() == (_FAIL, "")


@pytest.mark.parametrize("name", _ENVIRONMENT)
@pytest.mark.parametrize("value", [None, ""])
def test_requires_all_environment_inputs(
    validator: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    value: str | None,
) -> None:
    if value is None:
        monkeypatch.delenv(name)
    else:
        monkeypatch.setenv(name, value)
    assert validator.module.main() == 1
    assert capsys.readouterr() == (_FAIL, "")


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"{generated private malformed data",
        b"\xff",
        b"{}{}",
        b"[" * 2_000,
        json.dumps(_receipt())
        .replace('"analytics_failures": 0', '"analytics_failures": NaN')
        .encode(),
        json.dumps(_receipt())
        .replace('"analytics_failures": 0', '"analytics_failures": Infinity')
        .encode(),
        json.dumps(_receipt())
        .replace('"analytics_failures": 0', '"analytics_failures": -Infinity')
        .encode(),
    ],
)
def test_malformed_input_has_only_bounded_source_free_failure_text(
    validator: SimpleNamespace, capsys: pytest.CaptureFixture[str], raw: bytes
) -> None:
    validator.path.write_bytes(raw)
    assert validator.module.main() == 1
    assert capsys.readouterr() == (_FAIL, "")


@pytest.mark.parametrize("extra", [0, 1])
def test_enforces_byte_limit(validator: SimpleNamespace, extra: int) -> None:
    raw = json.dumps(_receipt()).encode()
    validator.path.write_bytes(raw + b" " * (8_192 + extra - len(raw)))
    if extra:
        with pytest.raises(ValueError, match="^receipt exceeds size limit$"):
            validator.module.validate_receipt(
                validator.path, revision=_REVISION, analytics_revision=_ANALYTICS_REVISION
            )
    else:
        validator.module.validate_receipt(
            validator.path, revision=_REVISION, analytics_revision=_ANALYTICS_REVISION
        )


def test_read_is_bounded_before_parsing(
    validator: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads: list[int] = []

    class GeneratedReceipt(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            reads.append(size)
            assert size == 8_193
            return super().read(size)

    stream = GeneratedReceipt(b" " * 16_384)
    monkeypatch.setattr(Path, "open", lambda _path, _mode: stream)
    with pytest.raises(ValueError, match="^receipt exceeds size limit$"):
        validator.module.validate_receipt(
            validator.path, revision=_REVISION, analytics_revision=_ANALYTICS_REVISION
        )
    assert reads == [8_193]
    assert stream.closed


@pytest.mark.parametrize("directory", [False, True])
def test_missing_or_directory_receipt_has_source_free_failure(
    validator: SimpleNamespace, capsys: pytest.CaptureFixture[str], directory: bool
) -> None:
    validator.path.unlink()
    if directory:
        validator.path.mkdir()
    assert validator.module.main() == 1
    assert capsys.readouterr() == (_FAIL, "")


def test_unexpected_exception_is_never_printed(
    validator: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("generated private exception detail")

    monkeypatch.setattr(validator.module, "validate_receipt", fail)
    assert validator.module.main() == 1
    assert capsys.readouterr() == (_FAIL, "")


@pytest.mark.parametrize("valid", [True, False])
def test_cli_uses_environment_and_fixed_exit_status_and_messages(
    validator: SimpleNamespace, valid: bool
) -> None:
    if not valid:
        validator.path.write_text("generated private malformed receipt", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(_SCRIPT)],
        cwd=validator.path.parent,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == (0 if valid else 1)
    assert result.stdout == (_PASS if valid else _FAIL)
    assert result.stderr == ""
