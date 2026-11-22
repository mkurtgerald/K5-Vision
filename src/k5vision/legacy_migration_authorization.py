"""Live old-profile administrator authorization for a stopped migration.

This module authenticates against the original private identity in its owner's
Windows context. A permit is an in-process object, not a native peer credential
or a serialized approval. Native routing must independently bind that owner,
the fixed source profile and the service before using it.
"""

from __future__ import annotations

import math
import re
import threading
import time
from collections import deque
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from k5vision import identity_state
from k5vision import legacy_identity_snapshot as snapshot
from k5vision.domain.users import UserAccount, UserRole
from k5vision.operator_identity_security import AccountSecurityStore
from k5vision.operator_profile_lock import OperatorProfileLock
from k5vision.services.user_registry import UserRegistry

_FAILURE = "Legacy migration authorization was not accepted"
_PERMIT_SECONDS = 3600
_MAX_PERMIT_SECONDS = 86400
_ATTEMPTS = 10
_ATTEMPT_SECONDS = 300


class LegacyMigrationAuthorizationError(RuntimeError):
    """Source-free failure without usernames, credentials, paths or native errors."""


@dataclass(frozen=True)
class _Proof:
    account: UserAccount = field(repr=False)
    credential_version: bytes = field(repr=False)
    mfa_generation: str | None
    expires_at: float
    epoch: int


class LegacyMigrationPermit:
    """One revocable, transfer-bound, nonserializable in-process authority proof."""

    __slots__ = ("_owner", "_proof")

    def __init__(self, owner, proof):
        self._owner, self._proof = owner, proof

    @property
    def site_id(self) -> str:
        return self._owner.site_id

    @property
    def transfer_id(self) -> str:
        return self._owner.transfer_id

    @property
    def administrator_id(self) -> UUID:
        return self._proof.account.id

    def assert_current(self, *, site_id: str, transfer_id: str) -> None:
        self._owner._assert_permit(self, site_id, transfer_id)

    def close(self) -> None:
        self._owner._revoke_permit(self)

    def __bool__(self):
        raise TypeError("A migration permit requires a live authority check")

    def __reduce__(self):
        raise TypeError("A migration permit cannot be serialized")

    def __enter__(self):
        self.assert_current(site_id=self.site_id, transfer_id=self.transfer_id)
        return self

    def __exit__(self, *_arguments):
        self.close()


class LegacyMigrationAuthorizer:
    """Require a complete stopped legacy identity and its current administrator.

    Password and MFA material are accepted only as bounded in-memory arguments.
    Successful MFA/recovery consumption is an authorized original-profile
    mutation and must happen before the migration captures its source digest.
    Missing stores, schema upgrades and new component initialization are refused.
    The caller owns the profile lease and must retain it through the migration.
    """

    def __init__(
        self,
        source: Path,
        *,
        lease: OperatorProfileLock,
        transfer_id: str,
        permit_seconds: int = _PERMIT_SECONDS,
    ):
        self._lock = threading.RLock()
        self._users = self._security = None
        self._active = None
        self._closed = True
        self._epoch = 0
        self._attempts = deque()
        self._last_elapsed = None
        failed = False
        try:
            if type(permit_seconds) is not int or not 1 <= permit_seconds <= _MAX_PERMIT_SECONDS:
                raise ValueError
            if type(transfer_id) is not str or str(UUID(transfer_id)) != transfer_id:
                raise ValueError
            self._source = identity_state._checked_path(source)
            self._root_identity = self._source.lstat()
            self._lease = lease
            # This preflight must precede ordinary identity/registry readers:
            # a closed WAL database can create original sidecars on read.
            self.preflight(self._source)
            snapshot._lease(self._source, lease, self._root_identity)
            state = identity_state.load_identity_state(self._source)
            lease.assert_held(state.site_id)
            self._site_id, self._transfer_id = state.site_id, transfer_id
            self._seconds = permit_seconds
            self._manifest_digest = snapshot._digest(self._source / "identity.json")
            self._user_path = state.database_path
            self._security_path = self._source / snapshot._SECURITY
            self._file_identities = {
                path: self._file_identity(path) for path in (self._user_path, self._security_path)
            }
            self._require_existing_schema()
            self._users = UserRegistry(database_path=state.database_path, site_id=state.site_id)
            self._security = AccountSecurityStore(self._source, state.site_id)
            self._closed = False
            self._admit_source()
            self._elapsed()
        except Exception:
            failed = True
        if failed:
            self.close()
            raise LegacyMigrationAuthorizationError(_FAILURE)

    @staticmethod
    def _file_identity(path):
        info = snapshot._private(path)
        return info.st_dev, info.st_ino

    @staticmethod
    def preflight(source: Path) -> None:
        """Read-only header/inventory checks before acquiring the real lease.

        The native controller must call this before OperatorProfileLock.acquire,
        whose normal identity reader can otherwise create WAL sidecars. These
        checks do not authenticate an administrator or grant a migration permit.
        """
        failed = False
        try:
            source = identity_state._checked_path(source)
            snapshot._inventory(source)
            snapshot._digest(source / snapshot._SECURITY, require_rollback_journal=True)
        except Exception:
            failed = True
        if failed:
            raise LegacyMigrationAuthorizationError(_FAILURE)

    @property
    def site_id(self) -> str:
        return self._site_id

    @property
    def transfer_id(self) -> str:
        return self._transfer_id

    def _require_existing_schema(self):
        with closing(snapshot._connect(self._user_path, readonly=True)) as connection:
            tables = dict(connection.execute("SELECT name,type FROM sqlite_master"))
            if any(
                tables.get(name) != "table"
                for name in (
                    "users",
                    "user_credentials",
                    "user_audit",
                    "user_camera_access",
                    "operator_component_anchors",
                )
            ):
                raise ValueError
            columns = {row[1] for row in connection.execute("PRAGMA table_info(user_credentials)")}
            if not {"issued_at", "last_checked_at"} <= columns:
                raise ValueError
            if connection.execute(
                "SELECT schema_version FROM operator_component_anchors "
                "WHERE site_id=? AND component='account_security'",
                (self.site_id,),
            ).fetchall() != [(1,)]:
                raise ValueError

    def _elapsed(self):
        now = time.monotonic()
        if (
            type(now) not in (int, float)
            or not math.isfinite(now)
            or now < 0
            or (self._last_elapsed is not None and now < self._last_elapsed)
        ):
            raise ValueError
        self._last_elapsed = now
        return now

    def _admit_source(self):
        if self._closed:
            raise ValueError
        snapshot._lease(self._source, self._lease, self._root_identity)
        if snapshot._digest(self._source / "identity.json") != self._manifest_digest:
            raise ValueError
        for path, expected in self._file_identities.items():
            if self._file_identity(path) != expected:
                raise ValueError

    def _factor_generation(self, account):
        status = self._security.status(account.id)
        enabled, generation = status["mfa_enabled"], status["generation"]
        if (
            type(enabled) is not bool
            or (
                enabled
                and (
                    type(generation) is not str or re.fullmatch("[a-f0-9]{32}", generation) is None
                )
            )
            or (not enabled and generation is not None)
        ):
            raise ValueError
        return generation

    def _current_account(self, account, version):
        current = self._users.resolve_session(account, version)
        if current is None or current.role is not UserRole.ADMINISTRATOR or not current.enabled:
            raise ValueError
        return current

    def authorize(self, username: str, password: str, code: str = "") -> LegacyMigrationPermit:
        failed = False
        result = None
        with self._lock:
            try:
                self._admit_source()
                now = self._elapsed()
                while self._attempts and self._attempts[0] <= now - _ATTEMPT_SECONDS:
                    self._attempts.popleft()
                if len(self._attempts) >= _ATTEMPTS:
                    raise ValueError
                self._attempts.append(now)
                self._active = None
                self._epoch += 1
                if (
                    type(username) is not str
                    or not 3 <= len(username) <= 64
                    or type(password) is not str
                    or not 1 <= len(password) <= 256
                    or type(code) is not str
                    or len(code) > 64
                ):
                    raise ValueError
                verified = self._users.verify_password_snapshot(
                    username=username, password=password
                )
                if verified is None:
                    raise ValueError
                account, version = verified
                account = self._current_account(account, version)
                generation = self._factor_generation(account)
                if generation is not None:
                    if not self._security.consume(account.id, code, generation=generation):
                        raise ValueError
                elif code:
                    raise ValueError
                self._admit_source()
                account = self._current_account(account, version)
                if self._factor_generation(account) != generation:
                    raise ValueError
                proof = _Proof(
                    account, version, generation, self._elapsed() + self._seconds, self._epoch
                )
                result = LegacyMigrationPermit(self, proof)
                self._active = result
            except Exception:
                failed = True
        if failed:
            raise LegacyMigrationAuthorizationError(_FAILURE)
        return result

    def _assert_permit(self, permit, site_id, transfer_id):
        failed = False
        with self._lock:
            try:
                if (
                    type(permit) is not LegacyMigrationPermit
                    or permit is not self._active
                    or site_id != self.site_id
                    or transfer_id != self.transfer_id
                    or permit._proof.epoch != self._epoch
                    or self._elapsed() >= permit._proof.expires_at
                ):
                    raise ValueError
                self._admit_source()
                proof = permit._proof
                current = self._current_account(proof.account, proof.credential_version)
                if self._factor_generation(current) != proof.mfa_generation:
                    raise ValueError
                self._admit_source()
                self._current_account(proof.account, proof.credential_version)
                if self._elapsed() >= proof.expires_at:
                    raise ValueError
            except Exception:
                self._active = None
                failed = True
        if failed:
            raise LegacyMigrationAuthorizationError(_FAILURE)

    def _revoke_permit(self, permit):
        with self._lock:
            if permit is self._active:
                self._active = None
                self._epoch += 1

    def close(self):
        failed = False
        with self._lock:
            self._closed = True
            self._active = None
            self._epoch += 1
            for name in ("_security", "_users"):
                value = getattr(self, name, None)
                setattr(self, name, None)
                if value is not None:
                    try:
                        value.close()
                    except Exception:
                        failed = True
        if failed:
            raise LegacyMigrationAuthorizationError(_FAILURE)

    def __enter__(self):
        return self

    def __exit__(self, *_arguments):
        self.close()
