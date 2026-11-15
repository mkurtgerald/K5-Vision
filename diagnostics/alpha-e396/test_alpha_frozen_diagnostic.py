"""Privacy and behavior checks for the supplemental diagnostic wrapper only."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import k5_alpha_frozen_diagnostic as plugin
import pytest

SOURCE = Path(os.environ["K5_ALPHA_DIAGNOSTIC_SOURCE_ROOT"]).resolve()
SPEC = importlib.util.spec_from_file_location(
    "_frozen_alpha_for_projection_tests",
    SOURCE / "scripts/installed_alpha_launcher_witness.py",
)
alpha = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(alpha)
PRIVATE = "generated-private-source-token-and-path"


def failure():
    record = alpha.alpha_diagnostic(
        "invalid_config",
        "child_process",
        failure_code="child_timeout",
        observations={
            "timed_out": True,
            "gate_state": "started",
            "launcher_requested": True,
        },
    )
    error = alpha.AlphaWitnessError(record)
    error.diagnostic = {"private": PRIVATE}
    return error


def test_original_error_projection_is_strict_and_preserves_exception_identity(capsys):
    error = failure()
    calls = []

    def original(*args, **kwargs):
        calls.append((args, kwargs))
        raise error

    with pytest.raises(alpha.AlphaWitnessError) as caught:
        plugin.observed_invoke(alpha, original)(PRIVATE, selected=PRIVATE)
    assert caught.value is error
    assert calls == [((PRIVATE,), {"selected": PRIVATE})]
    output = capsys.readouterr().out
    assert output.startswith("K5_ALPHA_LAUNCHER_DIAGNOSTIC=")
    projected = json.loads(output.split("=", 1)[1])
    alpha.validate_alpha_diagnostic(projected)
    assert projected == error.alpha_diagnostic
    assert projected["failure_code"] == "child_timeout" and projected["timed_out"] is True
    assert PRIVATE not in output


def test_success_return_is_identical_without_diagnostic(capsys):
    result = object()
    calls = []

    def original(*args, **kwargs):
        calls.append((args, kwargs))
        return result

    assert plugin.observed_invoke(alpha, original)(PRIVATE) is result
    assert calls == [((PRIVATE,), {})]
    assert capsys.readouterr().out == ""


def test_unrelated_private_exception_is_unchanged_and_not_projected(capsys):
    error = OSError(PRIVATE)
    calls = []

    def original():
        calls.append(True)
        raise error

    with pytest.raises(OSError) as caught:
        plugin.observed_invoke(alpha, original)()
    assert caught.value is error and calls == [True]
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "change",
    [
        {"private": PRIVATE},
        {"gate_state": PRIVATE},
        {"timed_out": PRIVATE},
        {"failure_code": PRIVATE},
    ],
)
def test_malformed_projection_cannot_expose_data_or_replace_original(change, capsys):
    error = failure()
    error.alpha_diagnostic.update(change)

    def original():
        raise error

    with pytest.raises(alpha.AlphaWitnessError) as caught:
        plugin.observed_invoke(alpha, original)()
    assert caught.value is error
    output = capsys.readouterr().out
    assert output == "K5_ALPHA_SUPPLEMENTAL_DIAGNOSTIC_REFUSED\n"
    assert PRIVATE not in output


def test_emission_failure_cannot_replace_original(monkeypatch, capsys):
    error = failure()

    def original():
        raise error

    def unavailable(*args):
        raise OSError(PRIVATE)

    monkeypatch.setattr(alpha, "emit_alpha_diagnostic", unavailable)
    with pytest.raises(alpha.AlphaWitnessError) as caught:
        plugin.observed_invoke(alpha, original)()
    assert caught.value is error
    assert capsys.readouterr().out == "K5_ALPHA_SUPPLEMENTAL_DIAGNOSTIC_REFUSED\n"


def test_broken_stdout_cannot_replace_original(monkeypatch):
    error = failure()

    def original():
        raise error

    def broken(*args):
        raise BrokenPipeError(PRIVATE)

    monkeypatch.setattr("builtins.print", broken)
    with pytest.raises(alpha.AlphaWitnessError) as caught:
        plugin.observed_invoke(alpha, original)()
    assert caught.value is error


@pytest.mark.parametrize("interrupted", [False, True])
def test_hook_restores_the_collected_modules_original_function(monkeypatch, interrupted):
    module = SimpleNamespace(
        __file__=SOURCE / "tests/test_windows_alpha_preflight_boundary.py", alpha=alpha
    )
    original = alpha.invoke_start
    monkeypatch.setattr(plugin, "_source_root", lambda: SOURCE)
    hook = plugin.pytest_runtest_call(SimpleNamespace(module=module, nodeid=plugin.NODE))
    next(hook)
    assert alpha.invoke_start is not original
    try:
        if interrupted:
            with pytest.raises(RuntimeError):
                hook.throw(RuntimeError(PRIVATE))
        else:
            with pytest.raises(StopIteration):
                next(hook)
    finally:
        hook.close()
    assert alpha.invoke_start is original


def test_collection_scope_requires_only_the_original_test():
    plugin.pytest_collection_modifyitems([SimpleNamespace(nodeid=plugin.NODE)])
    for items in ([], [SimpleNamespace(nodeid="different")], [None, None]):
        with pytest.raises(pytest.UsageError):
            plugin.pytest_collection_modifyitems(items)


def test_real_source_is_exact_and_unmodified():
    assert plugin._source_root() == SOURCE
