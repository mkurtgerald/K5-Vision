from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import test_operator_time_admin as time_helpers
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from k5vision import identity_state
from k5vision import legacy_identity_snapshot as migration
from k5vision.operator_identity_security import AccountSecurityStore
from k5vision.operator_oidc import OIDCStore
from k5vision.operator_profile_lock import OperatorProfileLock, OperatorProfileLockError
from k5vision.services.user_registry import UserRegistry

private_profile = time_helpers.private_profile


class _Protector:
    """Two distinct cryptographic contexts; this does not simulate native DPAPI proof."""

    def __init__(self, site_id, key):
        self.site_id, self.key = site_id, key

    def protect(self, secret, purpose):
        nonce = os.urandom(12)
        return (
            b"dpapi-v1:"
            + nonce
            + AESGCM(self.key).encrypt(
                nonce, bytes(secret), (self.site_id + ":" + purpose).encode()
            )
        )

    def unprotect(self, protected, purpose):
        assert protected.startswith(b"dpapi-v1:")
        return AESGCM(self.key).decrypt(
            protected[9:21], protected[21:], (self.site_id + ":" + purpose).encode()
        )


class _Sink:
    def __init__(self, protector):
        self.protector = protector
        self.bound = None
        self.revoked = False
        self.protected = self.verified = 0
        self.buffers = []

    def assert_authenticated(self, binding):
        if self.revoked or (self.bound is not None and binding != self.bound):
            raise RuntimeError("Private principal or transport error")
        self.bound = binding

    def protect(self, binding, purpose, secret):
        self.assert_authenticated(binding)
        self.buffers.append(secret)
        self.protected += 1
        return self.protector.protect(secret, purpose)

    def verify(self, binding, purpose, secret, protected):
        self.assert_authenticated(binding)
        assert self.protector.unprotect(protected, purpose) == secret
        self.verified += 1


def _hashes(root):
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir() if p.is_file()
    }


@pytest.fixture
def legacy(private_profile, monkeypatch):
    state = identity_state.initialize_identity_state(
        private_profile / "legacy", username="legacy-admin", password="legacy-admin-password"
    )
    old = _Protector(state.site_id, AESGCM.generate_key(128))
    new = _Protector(state.site_id, AESGCM.generate_key(128))
    users = UserRegistry(database_path=state.database_path, site_id=state.site_id)
    account, _ = users.verify_password_snapshot(
        username="legacy-admin", password="legacy-admin-password"
    )
    security = AccountSecurityStore(state.directory, state.site_id, protector=old)
    secret, client_secret = b"01234567890123456789", "private-oidc-client-secret"
    assert len(secret) == 20
    security.enable(account.id, secret, 12345)
    oidc = OIDCStore(security)
    oidc.configure(
        enabled=True,
        display_name="Fixture identity",
        issuer="https://id.example.test",
        client_id="fixture-client",
        client_secret=client_secret,
    )
    security.close()
    users.close()
    monkeypatch.setattr(migration, "ProfileSecretProtector", lambda site: old)
    lease = OperatorProfileLock(state.directory)
    lease.acquire()
    sink = _Sink(new)
    fixture = SimpleNamespace(
        source=state.directory,
        destination=private_profile / "snapshot",
        lease=lease,
        sink=sink,
        transfer_id=str(uuid4()),
        old=old,
        new=new,
        user_id=str(account.id),
        secret=secret,
        client_secret=client_secret,
    )
    yield fixture
    lease.close()


def _prepare(legacy, **changes):
    arguments = {"lease": legacy.lease, "sink": legacy.sink, "transfer_id": legacy.transfer_id}
    arguments.update(changes)
    return migration.prepare_identity_snapshot(legacy.source, legacy.destination, **arguments)


def test_copy_first_rewrap_preserves_identity_and_original_authorities(legacy):
    for name in (
        "operator-cameras.sqlite3",
        "operator-storage.json",
        "operator-recording-policy.json",
    ):
        path = legacy.source / name
        path.write_bytes(b"opaque original camera credentials, paths and policy anchors")
        path.chmod(0o600)
    original = _hashes(legacy.source)
    receipt = _prepare(legacy)
    assert _hashes(legacy.source) == original
    assert receipt["phase"] == "identity_prepared" and receipt["activation_allowed"] is False
    assert receipt["media_transfer_required"] and receipt["native_activation_required"]
    with pytest.raises(identity_state.IdentityStateError):
        identity_state.load_identity_state(legacy.destination)
    for name, expected in receipt["files"].items():
        if name != migration._SECURITY:
            assert receipt["staged_files"][name] == expected
    with sqlite3.connect(legacy.destination / migration._SECURITY) as target:
        mfa = target.execute("SELECT * FROM mfa").fetchone()
        oidc = target.execute("SELECT * FROM oidc_provider").fetchone()
    with sqlite3.connect(legacy.source / migration._SECURITY) as source:
        original_mfa = source.execute("SELECT * FROM mfa").fetchone()
        original_oidc = source.execute("SELECT * FROM oidc_provider").fetchone()
    assert mfa[:2] + mfa[3:] == original_mfa[:2] + original_mfa[3:]
    assert oidc[:5] + oidc[6:] == original_oidc[:5] + original_oidc[6:]
    assert legacy.new.unprotect(mfa[2], legacy.user_id) == legacy.secret
    assert legacy.new.unprotect(oidc[5], "oidc-client-secret").decode() == legacy.client_secret
    with pytest.raises(InvalidTag):
        legacy.old.unprotect(mfa[2], legacy.user_id)
    assert legacy.sink.protected == 2
    assert all(bytes(buffer) == bytes(len(buffer)) for buffer in legacy.sink.buffers)
    for path in legacy.destination.iterdir():
        assert legacy.secret not in path.read_bytes()
        assert legacy.client_secret.encode() not in path.read_bytes()
    assert not (legacy.destination / "operator.lock").exists()


@pytest.mark.parametrize("condition", ["closed", "other-root", "not-a-lease"])
def test_unheld_or_differently_bound_source_lease_cannot_stage(legacy, condition):
    if condition == "closed":
        legacy.lease.close()
        lease = legacy.lease
    elif condition == "other-root":
        lease = OperatorProfileLock(legacy.destination)
    else:
        lease = SimpleNamespace(_root=legacy.source, assert_held=lambda site: None)
    with pytest.raises(migration.LegacySnapshotError):
        _prepare(legacy, lease=lease)
    assert not legacy.destination.exists()


def test_another_recorder_owner_is_excluded_throughout_secret_transfer(legacy):
    original_protect = legacy.sink.protect

    def protect(binding, purpose, secret):
        other = OperatorProfileLock(legacy.source)
        with pytest.raises(OperatorProfileLockError):
            other.acquire()
        return original_protect(binding, purpose, secret)

    legacy.sink.protect = protect
    _prepare(legacy)


def test_unauthenticated_sink_refuses_before_creating_a_stage(legacy):
    legacy.sink.revoked = True
    with pytest.raises(migration.LegacySnapshotError) as failure:
        _prepare(legacy)
    assert not legacy.destination.exists()
    assert failure.value.__context__ is None and failure.value.__cause__ is None
    assert "Private principal" not in str(failure.value)


def test_revoked_transfer_rolls_back_ciphertext_changes_and_can_resume(legacy):
    original = _hashes(legacy.source)
    protect = legacy.sink.protect

    def revoke_after_protect(binding, purpose, secret):
        result = protect(binding, purpose, secret)
        legacy.sink.revoked = True
        return result

    legacy.sink.protect = revoke_after_protect
    with pytest.raises(migration.LegacySnapshotError):
        _prepare(legacy)
    assert _hashes(legacy.source) == original
    assert migration._digest(legacy.destination / migration._SECURITY) == migration._digest(
        legacy.source / migration._SECURITY
    )
    legacy.sink.protect = protect
    legacy.sink.revoked = False
    assert _prepare(legacy)["phase"] == "identity_prepared"


def test_completed_resume_verifies_service_ciphertexts_without_rewrapping(legacy):
    receipt = _prepare(legacy)
    staged = _hashes(legacy.destination)
    assert _prepare(legacy) == receipt
    assert _hashes(legacy.destination) == staged
    assert legacy.sink.protected == 2 and legacy.sink.verified == 4


@pytest.mark.parametrize("change", ["source", "target", "transfer", "schema"])
def test_resume_refuses_changed_source_target_or_transfer(legacy, change):
    _prepare(legacy)
    if change == "source":
        with sqlite3.connect(legacy.source / migration._SECURITY) as connection:
            connection.execute("UPDATE mfa SET last_counter=last_counter+1")
    elif change == "target":
        with sqlite3.connect(legacy.destination / migration._SECURITY) as connection:
            connection.execute("UPDATE mfa SET last_counter=last_counter+1")
    elif change == "schema":
        with sqlite3.connect(legacy.destination / migration._SECURITY) as connection:
            connection.execute("CREATE TABLE extra_authority(value)")
    else:
        legacy.transfer_id = str(uuid4())
    with pytest.raises(migration.LegacySnapshotError):
        _prepare(legacy)


@pytest.mark.parametrize(
    "name", ["unknown-authority.json", "users.sqlite3-wal", ".roles-aborted.tmp"]
)
def test_unknown_authority_journals_or_interrupted_saves_fail_before_copy(legacy, name):
    path = legacy.source / name
    path.write_bytes(b"preserve interrupted state")
    path.chmod(0o600)
    original = _hashes(legacy.source)
    with pytest.raises(migration.LegacySnapshotError):
        _prepare(legacy)
    assert not legacy.destination.exists() and _hashes(legacy.source) == original


def test_existing_or_partial_unmarked_target_is_never_adopted_or_overwritten(legacy):
    legacy.destination.mkdir(mode=0o700)
    file = legacy.destination / "original-site-marker"
    file.write_bytes(b"existing target site")
    with pytest.raises(migration.LegacySnapshotError):
        _prepare(legacy)
    assert file.read_bytes() == b"existing target site"
    assert len(list(legacy.destination.iterdir())) == 1


def test_pending_time_capability_is_never_copied_or_replayed(legacy):
    requests = legacy.source / "time-admin"
    requests.mkdir(mode=0o700)
    (requests / "pending.json").write_text(json.dumps({"nonce": "private-capability"}))
    with pytest.raises(migration.LegacySnapshotError):
        _prepare(legacy)
    assert not legacy.destination.exists()


@pytest.mark.parametrize(
    "change", ["directory", "copied-file", "marker", "extra-file", "lease", "deadline"]
)
def test_changed_stage_or_lifetime_during_transfer_cannot_prepare(legacy, monkeypatch, change):
    original = _hashes(legacy.source)
    protect = legacy.sink.protect
    now = [0.0]
    monkeypatch.setattr(migration, "time", SimpleNamespace(monotonic=lambda: now[0]))

    def alter_after_protect(binding, purpose, secret):
        result = protect(binding, purpose, secret)
        if change == "directory":
            moved = legacy.destination.with_name("moved-stage")
            legacy.destination.rename(moved)
            shutil.copytree(moved, legacy.destination)
        elif change == "copied-file":
            with (legacy.destination / migration._STAGED_MANIFEST).open("ab") as target:
                target.write(b"tampered after copy")
        elif change == "lease":
            legacy.lease.close()
        elif change == "marker":
            with (legacy.destination / migration._MARKER).open("ab") as target:
                target.write(b"changed transfer marker")
        elif change == "extra-file":
            (legacy.destination / "unknown-stage-file").write_bytes(b"unexpected state")
        else:
            now[0] = 301.0
        return result

    legacy.sink.protect = alter_after_protect
    with pytest.raises(migration.LegacySnapshotError):
        _prepare(legacy)
    assert _hashes(legacy.source) == original


@pytest.mark.parametrize("result", [None, b"unprotected-plaintext", b"dpapi-v1:x"])
def test_bad_protection_reply_cannot_change_original_or_commit(legacy, result):
    original = _hashes(legacy.source)
    legacy.sink.protect = lambda binding, purpose, secret: result
    with pytest.raises(migration.LegacySnapshotError):
        _prepare(legacy)
    assert _hashes(legacy.source) == original
    assert migration._digest(legacy.destination / migration._SECURITY) == migration._digest(
        legacy.source / migration._SECURITY
    )


def test_resume_rejects_blob_to_text_recovery_change(legacy):
    _prepare(legacy)
    with sqlite3.connect(legacy.destination / migration._SECURITY) as connection:
        value = connection.execute("SELECT recovery FROM mfa").fetchone()[0]
        connection.execute("UPDATE mfa SET recovery=?", (value.hex(),))
    with pytest.raises(migration.LegacySnapshotError):
        _prepare(legacy)


def test_resume_rejects_trigger_sharing_bookkeeping_name(legacy):
    _prepare(legacy)
    with sqlite3.connect(legacy.destination / migration._SECURITY) as connection:
        connection.execute(
            "CREATE TRIGGER k5_legacy_snapshot AFTER UPDATE ON mfa BEGIN SELECT 1; END"
        )
    with pytest.raises(migration.LegacySnapshotError):
        _prepare(legacy)


def test_boolean_authentication_callback_cannot_admit_a_transfer(legacy):
    legacy.sink.assert_authenticated = lambda binding: False
    with pytest.raises(migration.LegacySnapshotError):
        _prepare(legacy)
    assert not legacy.destination.exists()


def test_hardlinked_source_authority_is_refused_without_touching_its_peer(legacy):
    peer = legacy.source.parent / "external-security-link"
    os.link(legacy.source / migration._SECURITY, peer)
    expected = peer.read_bytes()
    with pytest.raises(migration.LegacySnapshotError):
        _prepare(legacy)
    assert peer.read_bytes() == expected and not legacy.destination.exists()


def test_growing_source_is_read_only_to_original_size_plus_one(legacy, monkeypatch):
    path = legacy.source / "operator-cameras.sqlite3"
    path.write_bytes(b"old")
    path.chmod(0o600)
    original_open, requested = Path.open, []

    class GrowingReader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *arguments):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def read(self, count):
            requested.append(count)
            with original_open(path, "ab") as writer:
                writer.write(b"growing source" * 1000)
            return self.stream.read(count)

    def growing_open(current, mode="r", *arguments, **keywords):
        stream = original_open(current, mode, *arguments, **keywords)
        return GrowingReader(stream) if current == path and mode == "rb" else stream

    monkeypatch.setattr(Path, "open", growing_open)
    with pytest.raises(migration.LegacySnapshotError):
        _prepare(legacy)
    assert requested == [4] and not legacy.destination.exists()


@pytest.mark.parametrize("changed", ["counter", "ciphertext", "bookkeeping", "source"])
def test_postcommit_last_admission_cannot_change_a_prepared_snapshot(legacy, changed):
    admission = legacy.sink.assert_authenticated
    changed_after_commit = []

    def tamper_after_commit(binding):
        admission(binding)
        path = legacy.destination / migration._SECURITY
        if not path.exists() or changed_after_commit:
            return
        with sqlite3.connect(path) as connection:
            if not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name=?", (migration._TABLE,)
            ).fetchone():
                return
            changed_after_commit.append(changed)
            if changed == "counter":
                connection.execute("UPDATE mfa SET last_counter=last_counter+1")
            elif changed == "ciphertext":
                connection.execute("UPDATE mfa SET secret=?", (b"dpapi-v1:changed",))
            elif changed == "bookkeeping":
                connection.execute(f"UPDATE {migration._TABLE} SET transfer_id='changed'")
            else:
                with sqlite3.connect(legacy.source / migration._SECURITY) as original:
                    original.execute("UPDATE mfa SET last_counter=last_counter+1")

    legacy.sink.assert_authenticated = tamper_after_commit
    with pytest.raises(migration.LegacySnapshotError):
        _prepare(legacy)
    assert changed_after_commit == [changed]


def test_closed_wal_header_refuses_before_identity_reader_can_create_original_sidecars(legacy):
    with sqlite3.connect(legacy.source / "users.sqlite3") as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    connection.close()
    assert not (legacy.source / "users.sqlite3-wal").exists()
    assert not (legacy.source / "users.sqlite3-shm").exists()
    original = _hashes(legacy.source)
    with pytest.raises(migration.LegacySnapshotError):
        _prepare(legacy)
    assert _hashes(legacy.source) == original
    assert not legacy.destination.exists()
