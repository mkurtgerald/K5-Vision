"""Windows ACL adapter lifetime regressions; real admissions are never cached."""

from __future__ import annotations

import hashlib
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from k5vision import windows_identity_security as security


def test_concurrent_admissions_reuse_native_bindings_but_reinspect_each_acl(monkeypatch):
    user = "S-1-5-21-1000"
    started = threading.Barrier(12)
    constructions = []
    checks = []
    current_acl = [security.AccessEntry(0, 0x01, security._FILE_ALL, user)]

    class Native:
        def descriptor(self, path):
            checks.append(path)
            return user, tuple(current_acl)

        def current_user(self):
            return user

        def trusted_installer(self):
            raise AssertionError("Private ACL does not use TrustedInstaller")

    def make_native():
        # Concurrent first admission must not race initialization.
        time.sleep(0.025)
        constructions.append(Native())
        return constructions[-1]

    monkeypatch.setattr(security, "_NativeSecurity", make_native)
    monkeypatch.setattr(security, "_NATIVE_SECURITY_INSTANCE", None)

    def admit():
        started.wait(timeout=5)
        for _ in range(3):
            security.check_windows_security(
                "C:\\private\\identity", private=True, require_file_inheritance=True
            )

    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(admit) for _ in range(12)]
        for future in futures:
            future.result(timeout=5)

    assert len(constructions) == 1
    assert len(checks) == 36

    # The native library binding can be cached, but never an ACL, owner, SID
    # policy result, or successful security decision.
    current_acl[:] = [security.AccessEntry(0, 0, 1, "S-1-1-0")]
    with pytest.raises(security.WindowsIdentitySecurityError, match="unsafe access"):
        security.check_windows_security("C:\\private\\identity", private=True)
    assert len(checks) == 37


def test_failed_native_binding_initialization_is_not_cached(monkeypatch):
    attempts = []

    def refuse():
        attempts.append(1)
        raise security.WindowsIdentitySecurityError("native Windows binding unavailable")

    monkeypatch.setattr(security, "_NativeSecurity", refuse)
    monkeypatch.setattr(security, "_NATIVE_SECURITY_INSTANCE", None)
    for _ in range(2):
        with pytest.raises(security.WindowsIdentitySecurityError, match="binding unavailable"):
            security.check_windows_security("C:\\private\\identity", private=True)
    assert len(attempts) == 2


def test_native_install_and_uninstall_pins_match_reviewed_helper_bytes():
    # The PowerShell installer and uninstaller execute a checkout/installed
    # security helper only after matching one of the reviewed byte hashes.
    # CI should detect source drift before a native Windows installer fails.
    root = Path(__file__).resolve().parents[2]
    source = (root / "src/k5vision/windows_identity_security.py").read_bytes()
    normalized = source.replace(b"\r\n", b"\n")
    expected = {
        hashlib.sha256(normalized).hexdigest(),
        hashlib.sha256(normalized.replace(b"\n", b"\r\n")).hexdigest(),
    }
    pattern = r"helper_pins=\{'([a-f0-9]{64})',\s*'([a-f0-9]{64})'\}"
    for name in ("Install-K5Operator.ps1", "Uninstall-K5Operator.ps1"):
        script = (root / "scripts" / name).read_text(encoding="utf-8")
        pins = re.search(pattern, script)
        assert pins is not None, f"{name} has no explicit reviewed helper pins"
        assert set(pins.groups()) == expected, f"{name} does not pin the reviewed helper"
