"""Copy-first identity staging; deliberately has no activation or public API.

The caller remains in the authenticated legacy GUI user's Windows context.
Only a separately admitted, live service channel may implement SecretSink.
This module does not authenticate a Python callback or grant service authority.
It never changes the source, its storage policy, or recording-directory ACLs.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

from k5vision import identity_state
from k5vision.operator_identity_security import ProfileSecretProtector
from k5vision.operator_profile_lock import OperatorProfileLock, _same_file

_AUTHORITIES = frozenset(
    {
        "identity.json",
        "users.sqlite3",
        "devices.sqlite3",
        "operator-cameras.sqlite3",
        "operator-account-security.sqlite3",
        "operator-audit.sqlite3",
        "operator-events.sqlite3",
        "operator-integrations.sqlite3",
        "role-profiles.json",
        "operator-storage.json",
        "operator-recording-policy.json",
    }
)
_SECURITY = "operator-account-security.sqlite3"
_MARKER = "migration-snapshot.json"
_STAGED_MANIFEST = "identity.json.snapshot"
_TABLE = "k5_legacy_snapshot"
_MAX_FILE = 128 * 1024**2
_MAX_SECONDS = 300


class LegacySnapshotError(RuntimeError):
    """Finite refusal: existing profiles and partial stages remain untouched."""


@dataclass(frozen=True)
class SnapshotBinding:
    site_id: str
    transfer_id: str
    source_digest: str


class SecretSink(Protocol):
    """Contract for a future native authenticated, issuer-bound private channel.

    assert_authenticated must reject disconnected/revoked/expired peers and a
    different site, transfer, source digest or authenticated legacy principal.
    protect sends one bounded in-memory value to that exact service account,
    returning ONLY its current-user DPAPI ciphertext. Neither endpoint may log
    or persist the supplied cleartext. A plain callable/boolean is insufficient
    authentication; the native channel implementation remains a release gate.
    """

    def assert_authenticated(self, binding: SnapshotBinding) -> None: ...

    def protect(self, binding: SnapshotBinding, purpose: str, secret: memoryview) -> bytes: ...

    def verify(
        self, binding: SnapshotBinding, purpose: str, secret: memoryview, protected: bytes
    ) -> None: ...


def _authenticated(sink, binding):
    if sink.assert_authenticated(binding) is not None:
        raise ValueError


def _staged_path(destination, name):
    # Normal product admission must not treat a prepared stage as a live site.
    return destination / (_STAGED_MANIFEST if name == "identity.json" else name)


def _private(path):
    info = identity_state._regular_file(path)
    if info.st_size > _MAX_FILE or (
        os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077)
    ):
        raise ValueError
    return info


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _digest(path, checkpoint=lambda: None, *, require_rollback_journal=False):
    before = _private(path)
    with path.open("rb") as stream:
        if _identity(os.fstat(stream.fileno())) != _identity(before):
            raise ValueError
        if require_rollback_journal:
            header = stream.read(20)
            if header[:16] != b"SQLite format 3\x00" or header[18:] != b"\x01\x01":
                # Even closed WAL-mode databases can create original -wal/-shm
                # files when ordinary identity admission opens mode=ro.
                raise ValueError
            stream.seek(0)
        digest = hashlib.sha256()
        remaining = before.st_size
        while block := stream.read(min(1024**2, remaining + 1)):
            checkpoint()
            remaining -= len(block)
            if remaining < 0:
                raise ValueError
            digest.update(block)
        if remaining:
            raise ValueError
    if _identity(_private(path)) != _identity(before):
        raise ValueError
    return {"bytes": before.st_size, "sha256": digest.hexdigest()}


def _lease(source, lease, root_identity):
    if not isinstance(lease, OperatorProfileLock) or lease._root != source:
        raise ValueError
    with lease._guard:
        if lease._descriptor is None:
            raise ValueError
        _same_file(lease._descriptor, source / "operator.lock")
    identity_state._same_directory(source, root_identity)


def _inventory(source, checkpoint=lambda: None):
    files, omitted = {}, []
    for index, path in enumerate(source.iterdir()):
        if index >= 64:
            raise ValueError
        name = path.name
        if name in _AUTHORITIES:
            files[name] = _digest(
                path, checkpoint, require_rollback_journal=name == "users.sqlite3"
            )
        elif name in {"operator.lock", "operator-mediamtx.json", "operator-gateway.json"}:
            _private(path)
            omitted.append(name)
        elif name in {"time-admin", "exports", "analytics-samples"}:
            info = path.lstat()
            identity_state._same_directory(path, info)
            if name == "time-admin" and next(path.iterdir(), None) is not None:
                raise ValueError
            omitted.append(name)
        else:
            # Includes journals, unknown authorities and interrupted saves.
            raise ValueError
    if not {"identity.json", "users.sqlite3", _SECURITY} <= files.keys():
        raise ValueError
    return dict(sorted(files.items())), sorted(omitted)


def _copy(source, destination, expected, checkpoint):
    if destination.exists() or destination.is_symlink():
        if _digest(destination, checkpoint) != expected:
            raise ValueError
        return
    before = _private(source)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as target, source.open("rb") as original:
        if _identity(os.fstat(original.fileno())) != _identity(before):
            raise ValueError
        remaining = before.st_size
        while block := original.read(min(1024**2, remaining + 1)):
            checkpoint()
            remaining -= len(block)
            if remaining < 0:
                raise ValueError
            target.write(block)
        if remaining:
            raise ValueError
        target.flush()
        os.fsync(target.fileno())
    if (
        _identity(_private(source)) != _identity(before)
        or _digest(destination, checkpoint) != expected
    ):
        raise ValueError


def _connect(path, *, readonly=False):
    _private(path)
    mode = "ro&immutable=1" if readonly else "rw"
    connection = sqlite3.connect(path.as_uri() + "?mode=" + mode, uri=True)
    try:
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError
    except BaseException:
        connection.close()
        raise
    return connection


def _security_digest(connection):
    """Bind every original nonsecret row and every replacement ciphertext."""
    content, protected = hashlib.sha256(), hashlib.sha256()
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if not {"security_state", "mfa"} <= tables or tables - {
        "security_state",
        "mfa",
        "oidc_provider",
        "oidc_links",
        _TABLE,
    }:
        raise ValueError
    if ("oidc_provider" in tables) != ("oidc_links" in tables):
        raise ValueError
    for row in connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE tbl_name!=? ORDER BY type,name",
        (_TABLE,),
    ):
        content.update(json.dumps(list(row), ensure_ascii=True).encode())
    for table, order, secret_index in (
        ("security_state", "site_id", None),
        ("mfa", "user_id", 2),
        ("oidc_provider", "id", 5),
        ("oidc_links", "id", None),
    ):
        if table not in tables:
            continue
        for index, row in enumerate(connection.execute(f"SELECT * FROM {table} ORDER BY {order}")):
            if index >= 100_000:
                raise ValueError
            values = list(row)
            if secret_index is not None:
                secret = values[secret_index]
                if (
                    type(secret) is not bytes
                    or not secret.startswith(b"dpapi-v1:")
                    or not 10 <= len(secret) <= 65545
                ):
                    raise ValueError
                protected.update(json.dumps([table, str(row[0]), secret.hex()]).encode())
                values[secret_index] = None
            content.update(
                json.dumps(
                    [
                        table,
                        [
                            {"blob": value.hex()} if type(value) is bytes else value
                            for value in values
                        ],
                    ],
                    ensure_ascii=True,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode()
            )
    return content.hexdigest(), protected.hexdigest()


def _secret_rows(connection):
    for index, row in enumerate(
        connection.execute("SELECT user_id,secret FROM mfa ORDER BY user_id")
    ):
        if index >= 100_000 or str(UUID(row[0])) != row[0]:
            raise ValueError
        yield "mfa", "user_id", "secret", row[0], row[1], row[0]
    if connection.execute("SELECT name FROM sqlite_master WHERE name='oidc_provider'").fetchone():
        for index, row in enumerate(
            connection.execute("SELECT id,client_secret FROM oidc_provider")
        ):
            if index != 0 or row[0] != 1:
                raise ValueError
            yield "oidc_provider", "id", "client_secret", row[0], row[1], "oidc-client-secret"


def _bookkeeping(connection, binding, seal):
    if connection.execute(
        "SELECT type,name FROM sqlite_master WHERE tbl_name=?", (_TABLE,)
    ).fetchall() != [("table", _TABLE)]:
        raise ValueError
    columns = connection.execute(f"PRAGMA table_info({_TABLE})").fetchall()
    if columns != [
        (index, name, "", 0, None, 0)
        for index, name in enumerate(
            ("site_id", "transfer_id", "source_digest", "content", "protected")
        )
    ]:
        raise ValueError
    if connection.execute(f"SELECT * FROM {_TABLE} LIMIT 2").fetchall() != [
        (binding.site_id, binding.transfer_id, binding.source_digest, *seal)
    ]:
        raise ValueError


def _sealed_digest(path, binding, seal, checkpoint):
    before = _identity(_private(path))
    with closing(_connect(path, readonly=True)) as connection:
        if _security_digest(connection) != seal:
            raise ValueError
        _bookkeeping(connection, binding, seal)
    digest = _digest(path, checkpoint)
    if _identity(_private(path)) != before:
        raise ValueError
    return digest


def _rewrap(source, path, binding, sink, checkpoint):
    connection = _connect(path)
    try:
        with closing(_connect(source, readonly=True)) as original_connection:
            if original_connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name=? OR tbl_name=?", (_TABLE, _TABLE)
            ).fetchone():
                raise ValueError
            original, _ = _security_digest(original_connection)
            if original_connection.execute("SELECT * FROM security_state LIMIT 2").fetchall() != [
                (1, binding.site_id)
            ]:
                raise ValueError
        content, protected = _security_digest(connection)
        prior_seal = content, protected
        if content != original:
            raise ValueError
        resuming = False
        if connection.execute("SELECT name FROM sqlite_master WHERE name=?", (_TABLE,)).fetchone():
            _bookkeeping(connection, binding, (original, protected))
            resuming = True
        protector = ProfileSecretProtector(binding.site_id)
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("BEGIN IMMEDIATE")
        with closing(_connect(source, readonly=True)) as original_connection:
            for table, key, field, identifier, old, purpose in _secret_rows(original_connection):
                checkpoint()
                _authenticated(sink, binding)
                secret = bytearray(protector.unprotect(old, purpose))
                try:
                    if (table == "mfa" and len(secret) != 20) or (
                        table != "mfa"
                        and (len(secret) > 16384 or len(secret.decode("utf-8")) > 4096)
                    ):
                        raise ValueError
                    if resuming:
                        protected = connection.execute(
                            f"SELECT {field} FROM {table} WHERE {key}=?", (identifier,)
                        ).fetchone()[0]
                    else:
                        protected = sink.protect(binding, purpose, memoryview(secret))
                        if (
                            type(protected) is not bytes
                            or not protected.startswith(b"dpapi-v1:")
                            or not 10 <= len(protected) <= 65545
                            or protected == old
                        ):
                            raise ValueError
                    if sink.verify(binding, purpose, memoryview(secret), protected) is not None:
                        raise ValueError
                finally:
                    secret[:] = bytes(len(secret))
                checkpoint()
                _authenticated(sink, binding)
                if not resuming:
                    connection.execute(
                        f"UPDATE {table} SET {field}=? WHERE {key}=?", (protected, identifier)
                    )
        if resuming:
            return prior_seal
        content, protected = _security_digest(connection)
        if content != original:
            raise ValueError
        connection.execute(
            f"CREATE TABLE {_TABLE}(site_id,transfer_id,source_digest,content,protected)"
        )
        connection.execute(
            f"INSERT INTO {_TABLE} VALUES(?,?,?,?,?)",
            (
                binding.site_id,
                binding.transfer_id,
                binding.source_digest,
                content,
                protected,
            ),
        )
        checkpoint()
        _authenticated(sink, binding)
        connection.commit()
        return content, protected
    finally:
        connection.close()


def prepare_identity_snapshot(
    source: Path,
    destination: Path,
    *,
    lease: OperatorProfileLock,
    sink: SecretSink,
    transfer_id: str,
) -> dict:
    """Prepare a private, nonactivatable identity snapshot without changing its source.

    Resume is allowed only for this same transfer and an unchanged source.
    Completed copied files and the atomic rewrap transaction are reusable;
    a partial/tampered copy refuses instead of overwriting anything. Footage,
    exports, changed absolute paths and native service activation are separate
    mandatory stages; the returned receipt explicitly forbids activation.
    """
    failed = False
    try:
        deadline = time.monotonic() + _MAX_SECONDS
        if str(UUID(transfer_id)) != transfer_id:
            raise ValueError
        source = identity_state._checked_path(source)
        destination = identity_state._checked_path(destination)
        if source == destination or source in destination.parents or destination in source.parents:
            raise ValueError
        root_identity = source.lstat()
        destination_identity = None

        def checkpoint():
            if time.monotonic() >= deadline:
                raise ValueError
            _lease(source, lease, root_identity)
            if destination_identity is not None:
                identity_state._same_directory(destination, destination_identity)

        checkpoint()
        # Refuse journals BEFORE any ordinary SQLite reader can create a -shm
        # file or recover a source journal. Admission must remain copy-only.
        files, omitted = _inventory(source, checkpoint)
        state = identity_state.load_identity_state(source)
        lease.assert_held(state.site_id)
        fingerprint = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
        binding = SnapshotBinding(state.site_id, transfer_id, fingerprint)
        _authenticated(sink, binding)
        marker = {
            "schema_version": 1,
            "site_id": state.site_id,
            "transfer_id": transfer_id,
            "source_digest": fingerprint,
            "files": files,
            "activation_allowed": False,
        }
        if destination.exists() or destination.is_symlink():
            destination_identity = destination.lstat()
            checkpoint()
            if _private(destination / _MARKER).st_size > 16384:
                raise ValueError
            if (
                json.loads(
                    (destination / _MARKER).read_bytes(),
                    object_pairs_hook=identity_state._unique_object,
                )
                != marker
            ):
                raise ValueError
        else:
            identity_state.validate_new_identity_directory(destination)
            destination.mkdir(mode=0o700)
            destination_identity = destination.lstat()
            checkpoint()
            descriptor = os.open(destination / _MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as target:
                json.dump(marker, target, sort_keys=True)
                target.flush()
                os.fsync(target.fileno())
        staged = {path.name for path in destination.iterdir()}
        allowed_staged = {_staged_path(destination, name).name for name in files} | {_MARKER}
        if staged - allowed_staged:
            raise ValueError
        marker_digest = _digest(destination / _MARKER, checkpoint)
        for name, expected in files.items():
            checkpoint()
            _authenticated(sink, binding)
            if name == _SECURITY and (destination / name).exists():
                with closing(_connect(destination / name, readonly=True)) as connection:
                    if connection.execute(
                        "SELECT name FROM sqlite_master WHERE name=?", (_TABLE,)
                    ).fetchone():
                        continue
            _copy(source / name, _staged_path(destination, name), expected, checkpoint)
        seal = _rewrap(source / _SECURITY, destination / _SECURITY, binding, sink, checkpoint)
        checkpoint()
        # No sink callback may follow these final source/stage integrity checks.
        _authenticated(sink, binding)
        if _inventory(source, checkpoint) != (files, omitted):
            raise ValueError
        staged_files = {
            name: (
                _sealed_digest(destination / name, binding, seal, checkpoint)
                if name == _SECURITY
                else _digest(_staged_path(destination, name), checkpoint)
            )
            for name in files
        }
        if any(
            staged_files[name] != expected for name, expected in files.items() if name != _SECURITY
        ):
            raise ValueError
        if _digest(destination / _MARKER, checkpoint) != marker_digest:
            raise ValueError
        if {path.name for path in destination.iterdir()} != allowed_staged:
            raise ValueError
        checkpoint()
        lease.assert_held(state.site_id)
        return {
            **marker,
            "staged_files": staged_files,
            "omitted_source_entries": omitted,
            "phase": "identity_prepared",
            "media_transfer_required": True,
            "native_activation_required": True,
        }
    except Exception:
        failed = True
    if failed:
        raise LegacySnapshotError("Legacy identity snapshot is unavailable")
