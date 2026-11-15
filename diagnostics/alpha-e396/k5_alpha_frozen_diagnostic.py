"""Supplemental failure projection for one unchanged, frozen Alpha test."""

from __future__ import annotations

import hashlib
import os
import subprocess
from contextlib import suppress
from functools import wraps
from pathlib import Path

import pytest

CANDIDATE = "e39679357a943174dd29a0c8c5594bcc134146d3"
NODE = (
    "tests/test_windows_alpha_preflight_boundary.py::"
    "test_windows_actual_start_bad_config_owned_observation"
)
# Exact Git LF bytes and their deterministic Windows CRLF checkout forms.
# Arbitrary line-ending normalization or runtime-derived trust is not allowed.
SOURCE_HASHES = {
    "tests/test_windows_alpha_preflight_boundary.py": {
        "31b5e21df0ab00ceeacff81041de9d24b8d4ab89428619c26c2a4339a4a9978d",
        "6784dd3c3810a9d941c16cffeaf7efbf1b01f61f9d8a0ecb0193d34cd7bbfac8",
    },
    "scripts/installed_alpha_launcher_witness.py": {
        "3595a38e468d0be0bc7d81e62233cbafd222a1c1ca1bd5f4a69d4c56cc283da1",
        "2531bc367f6f5669755c7b4d703ebbf89913c29d06488990ed44398b6e16f9ae",
    },
    "scripts/installed_analytics_witness.py": {
        "80e0f11f873b14a39e30b7d4f05bc5443951792645180aefcc531082910ef3a7",
        "273c49887fe2684718bde5d3c555d56e3f37fcfe928ae5ca519bc9e8f31f70ad",
    },
    "scripts/windows_owned_preflight.py": {
        "bdf3b2bbebe39e2916eeeb000c9f925c971aa2356b1cdee714f96a254bd104b0",
        "8d18cd78f540aef761a130a29ed168eaa32de4a3f88238c88ab71ae6992913d5",
    },
    "scripts/windows-alpha/Start-K5VisionAlpha.ps1": {
        "e41c2d85b0a77b833e612e4bc90faf16fefb91a59ac2e08abc6884487d137603",
        "736554ead5bdabfa5ecbdc6b27be30b1716a4ab961d89ec45943bbd0519e31c3",
    },
}


def observed_invoke(alpha, original):
    @wraps(original)
    def observed(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        except alpha.AlphaWitnessError as error:
            try:
                alpha.validate_alpha_diagnostic(error.alpha_diagnostic)
                alpha.emit_alpha_diagnostic(error.alpha_diagnostic)
            except BaseException:  # noqa: BLE001 - diagnostics must preserve the original failure.
                # Projection cannot replace the real failure or expose an
                # unvalidated record, exception, detail, path or child output.
                with suppress(BaseException):
                    print("K5_ALPHA_SUPPLEMENTAL_DIAGNOSTIC_REFUSED")
            raise

    return observed


def _source_root():
    try:
        root = Path(os.environ["K5_ALPHA_DIAGNOSTIC_SOURCE_ROOT"]).resolve(strict=True)
        head = (
            subprocess.check_output(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                timeout=10,
                stderr=subprocess.DEVNULL,
            )
            .decode("ascii")
            .strip()
        )
        dirty = subprocess.check_output(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"],
            timeout=10,
            stderr=subprocess.DEVNULL,
        )
        if head != CANDIDATE or dirty:
            raise ValueError
        for relative, expected in SOURCE_HASHES.items():
            path = root / relative
            if not path.resolve(strict=True).is_relative_to(root):
                raise ValueError
            if hashlib.sha256(path.read_bytes()).hexdigest() not in expected:
                raise ValueError
        return root
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        raise pytest.UsageError("Frozen Alpha diagnostic source admission failed.") from None


def pytest_collection_modifyitems(items):
    if len(items) != 1 or items[0].nodeid != NODE:
        raise pytest.UsageError("Supplemental Alpha diagnostic requires its one exact test.")


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_call(item):
    root = _source_root()
    module = item.module
    alpha = module.alpha
    expected_modules = (
        (module, "tests/test_windows_alpha_preflight_boundary.py"),
        (alpha, "scripts/installed_alpha_launcher_witness.py"),
        (alpha.common, "scripts/installed_analytics_witness.py"),
        (alpha.boundary, "scripts/windows_owned_preflight.py"),
    )
    if any(
        Path(value.__file__).resolve() != (root / name).resolve()
        for value, name in expected_modules
    ):
        raise pytest.UsageError("Frozen Alpha diagnostic module identity failed.")
    original = alpha.invoke_start
    alpha.invoke_start = observed_invoke(alpha, original)
    try:
        with suppress(BaseException):
            print("K5_ALPHA_SUPPLEMENTAL_DIAGNOSTIC_ACTIVE=" + CANDIDATE)
        yield
    finally:
        alpha.invoke_start = original
