"""Pinned development fixture transport retries must retain byte admission."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import socket
from pathlib import Path
from urllib.error import URLError

import pytest

# This script is a development-only entry point, not a distributable package.
# pytest's console-script invocation does not necessarily add the checkout
# root to sys.path; load the exact checked-out script without changing sys.path.
_script = (
    Path(__file__).resolve().parents[2] / "scripts" / "provision_operator_person_acceptance.py"
)
_spec = importlib.util.spec_from_file_location("provision_operator_person_acceptance", _script)
assert _spec is not None and _spec.loader is not None
provision = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(provision)


def test_transient_dns_retry_still_requires_admitted_hash(tmp_path, monkeypatch):
    payload = b"synthetic public fixture bytes"
    attempts = []

    def fake_open(url, timeout):
        attempts.append((url, timeout))
        if len(attempts) < 3:
            raise URLError(socket.gaierror(11002, "fixture DNS temporarily unavailable"))
        return io.BytesIO(payload)

    monkeypatch.setattr(provision, "urlopen", fake_open)
    monkeypatch.setattr(provision.time, "sleep", lambda _: None)
    destination = tmp_path / "fixture.bin"
    provision.download(
        "https://fixture.invalid/immutable",
        destination,
        len(payload),
        "sha256",
        hashlib.sha256(payload).hexdigest(),
    )
    assert destination.read_bytes() == payload
    assert len(attempts) == 3
    assert all(timeout == 120 for _, timeout in attempts)


def test_exhausted_dns_refuses_without_creating_artifact(tmp_path, monkeypatch):
    attempts = []

    def fail_dns(url, timeout):
        attempts.append(timeout)
        raise URLError(socket.gaierror(11002, "fixture DNS temporarily unavailable"))

    monkeypatch.setattr(provision, "urlopen", fail_dns)
    monkeypatch.setattr(provision.time, "sleep", lambda _: None)
    destination = tmp_path / "fixture.bin"
    with pytest.raises(RuntimeError, match="Pinned development artifact transport refused"):
        provision.download("https://fixture.invalid/immutable", destination, 3, "sha256", "0" * 64)
    assert not destination.exists()
    assert attempts == [120, 120, 120]


def test_successful_transport_with_wrong_digest_still_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(provision, "urlopen", lambda url, timeout: io.BytesIO(b"abc"))
    destination = tmp_path / "fixture.bin"
    with pytest.raises(ValueError, match="identity was refused"):
        provision.download("https://fixture.invalid/immutable", destination, 3, "sha256", "0" * 64)
    assert not destination.exists()
