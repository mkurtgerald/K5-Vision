"""The replay preserves failures and cannot become a normal clock qualification."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def replay():
    return _load(
        "clock_cleanup_replay_unit",
        ROOT / "tests/integration/test_windows_clock_cleanup_diagnostic.py",
    )


@pytest.mark.parametrize("fails", [False, True])
def test_replay_delegates_once_and_preserves_result_or_original_failure(replay, fails):
    calls = []
    properties = []
    result = object()
    error = RuntimeError("PRIVATE-CAMERA-URI-AND-SECRET")

    def original(record):
        calls.append("original")
        record("vm_clock_windows_diagnostic", '{"stage":"graceful_owned_process_cleanup"}')
        if fails:
            raise error
        return result

    source = SimpleNamespace(test_native_windows_vm_clock_qualification=original)

    def recorder(name, value):
        properties.append((name, value))

    if fails:
        with pytest.raises(RuntimeError) as raised:
            replay._run_original(source, recorder)
        assert raised.value is error
    else:
        assert replay._run_original(source, recorder) is result
    assert calls == ["original"]
    assert properties == [
        (
            "diagnostic_only_clock_cleanup_original_vm_clock_windows_diagnostic",
            '{"stage":"graceful_owned_process_cleanup"}',
        )
    ]
    assert "PRIVATE" not in json.dumps(properties)


def test_replay_refuses_unknown_property_before_emitting_it(replay):
    emitted = []

    def original(record):
        record("PRIVATE-UNRECOGNIZED-PROPERTY", "PRIVATE-PAYLOAD")

    source = SimpleNamespace(test_native_windows_vm_clock_qualification=original)
    with pytest.raises(AssertionError, match="Unknown original clock property"):
        replay._run_original(source, lambda *args: emitted.append(args))
    assert emitted == []


@pytest.mark.parametrize("rename_case", [False, True])
def test_unchanged_normal_verifier_refuses_prefixed_replay_even_with_valid_payload(
    tmp_path, replay, rename_case
):
    fixtures = _load(
        "clock_cleanup_original_scoped_fixtures",
        ROOT / "tests/unit/test_windows_vm_clock_scoped_receipt.py",
    )
    bundle = fixtures.valid_scoped_evidence(tmp_path)
    assert fixtures.VERIFIER.verify_scoped(bundle.report, **bundle.kwargs) == bundle.receipt
    report = ET.parse(bundle.report)
    for case in report.iter("testcase"):
        if rename_case:
            case.set("name", "test_diagnostic_only_exact_699_clock_cleanup")
    for prop in report.iter("property"):
        prop.set("name", "diagnostic_only_clock_cleanup_original_" + prop.get("name"))
    report.write(bundle.report, encoding="utf-8", xml_declaration=True)
    with pytest.raises(ValueError):
        fixtures.VERIFIER.verify_scoped(bundle.report, **bundle.kwargs)


def test_replay_pins_the_frozen_helper_and_unchanged_original_guard(replay):
    assert replay.PRODUCT_HEAD == "6993360f0682e41589005b0b97aa391398baa163"
    assert (
        replay.HELPER_SHA256
        == hashlib.sha256(
            (ROOT / "tests/integration/test_windows_desktop_app.py")
            .read_bytes()
            .replace(b"\r\n", b"\n")
        ).hexdigest()
    )
    for relative, expected in replay.SOURCE_PINS.items():
        canonical = (ROOT / relative).read_bytes().replace(b"\r\n", b"\n")
        assert hashlib.sha256(canonical).hexdigest() == expected
