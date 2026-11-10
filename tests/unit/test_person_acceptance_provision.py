"""Pinned development fixture transport retries must retain byte admission."""

from __future__ import annotations

import hashlib
import io
import socket
from urllib.error import URLError

import pytest

from scripts import provision_operator_person_acceptance as provision


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
