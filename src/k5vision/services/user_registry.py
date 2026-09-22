"""Persistent, site-scoped user administration service."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from hmac import compare_digest
from pathlib import Path
from uuid import UUID

from k5vision.domain.users import UserAccount, UserAuditEvent, UserCreate, UserPatch

DEFAULT_USER_CAPACITY = 1024
MAX_USER_CAPACITY = 100_000
MAX_USER_PAGE_SIZE = 100
MAX_USER_AUDIT_PAGE_SIZE = 100
DEFAULT_BOOTSTRAP_TTL_SECONDS = 900
MIN_BOOTSTRAP_TTL_SECONDS = 60
MAX_BOOTSTRAP_TTL_SECONDS = 86_400
_SITE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ACTOR_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")
_CREDENTIAL_SCHEME = "scrypt-v1"
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SALT_BYTES = 16
_DUMMY_SALT = b"\x00" * _SALT_BYTES
_DUMMY_VERIFIER = b"\x00" * _SCRYPT_DKLEN


class UserRegistryCapacityError(RuntimeError):
    """Raised when a bounded user registry cannot accept another account."""


class UserRegistryConflictError(RuntimeError):
    """Raised when a username is already present in the same site scope."""


class UserRegistryStorageError(RuntimeError):
    """Raised when durable user state cannot be read or updated safely."""


def _derive_credential(secret: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        secret.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )


class UserRegistry:
    """Persist bounded user-account metadata and audit history within one site."""

    def __init__(
        self,
        *,
        capacity: int = DEFAULT_USER_CAPACITY,
        database_path: str | Path = ":memory:",
        site_id: str = "default",
    ) -> None:
        if not 1 <= capacity <= MAX_USER_CAPACITY:
            raise ValueError(f"capacity must be between 1 and {MAX_USER_CAPACITY}")
        normalized_site = site_id.strip()
        if not _SITE_ID_PATTERN.fullmatch(normalized_site):
            raise ValueError("site_id must be a bounded ASCII identifier")

        path = str(database_path).strip()
        if not path:
            raise ValueError("database_path must not be empty")
        if path != ":memory:":
            database_file = Path(path).expanduser()
            if not database_file.parent.is_dir():
                raise UserRegistryStorageError("user registry parent directory is unavailable")
            if database_file.exists() and database_file.is_dir():
                raise UserRegistryStorageError("user registry path is not a file")
            path = str(database_file)

        self._capacity = capacity
        self._site_id = normalized_site
        self._database_path = path
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None
        self._open()

    def _open(self) -> None:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                self._database_path,
                timeout=5.0,
                isolation_level=None,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    site_id TEXT NOT NULL,
                    id TEXT NOT NULL,
                    username TEXT NOT NULL COLLATE NOCASE,
                    display_name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    UNIQUE(site_id, id),
                    UNIQUE(site_id, username)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS user_audit (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    site_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    changed_fields_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS user_credentials (
                    site_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    scheme TEXT NOT NULL,
                    credential_kind TEXT NOT NULL,
                    salt BLOB NOT NULL,
                    verifier BLOB NOT NULL,
                    expires_at TEXT,
                    PRIMARY KEY(site_id, user_id)
                )
                """
            )
        except (OSError, sqlite3.Error) as exc:
            if connection is not None:
                connection.close()
            raise UserRegistryStorageError("user registry is unavailable") from exc
        self._connection = connection

    def _require_connection(self) -> sqlite3.Connection:
        connection = self._connection
        if connection is None:
            raise UserRegistryStorageError("user registry is closed")
        return connection

    @staticmethod
    def _decode_user(row: sqlite3.Row) -> UserAccount:
        try:
            return UserAccount(
                id=UUID(row["id"]),
                username=row["username"],
                display_name=row["display_name"],
                role=row["role"],
                enabled=bool(row["enabled"]),
            )
        except (TypeError, ValueError) as exc:
            raise UserRegistryStorageError("user registry contains invalid state") from exc

    @staticmethod
    def _validate_actor(actor: str) -> str:
        normalized = actor.strip()
        if not _ACTOR_PATTERN.fullmatch(normalized):
            raise ValueError("actor must be a bounded ASCII identifier")
        return normalized

    @staticmethod
    def _validate_username(username: str) -> str:
        normalized = username.strip()
        if not _USERNAME_PATTERN.fullmatch(normalized):
            raise ValueError("username must be a bounded ASCII identifier")
        return normalized

    def _append_audit(
        self,
        connection: sqlite3.Connection,
        *,
        user_id: UUID,
        action: str,
        actor: str,
        changed_fields: tuple[str, ...],
    ) -> None:
        connection.execute(
            """
            INSERT INTO user_audit (
                site_id,
                user_id,
                action,
                actor,
                changed_fields_json,
                occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                self._site_id,
                str(user_id),
                action,
                actor,
                json.dumps(changed_fields, separators=(",", ":")),
                datetime.now(UTC).isoformat(),
            ),
        )

    def _insert_user(
        self,
        connection: sqlite3.Connection,
        request: UserCreate,
        *,
        actor: str,
    ) -> UserAccount:
        existing = connection.execute(
            "SELECT id FROM users WHERE site_id = ? AND username = ?",
            (self._site_id, request.username),
        ).fetchone()
        if existing is not None:
            raise UserRegistryConflictError("username already exists")
        count = connection.execute(
            "SELECT COUNT(*) FROM users WHERE site_id = ?",
            (self._site_id,),
        ).fetchone()[0]
        if count >= self._capacity:
            raise UserRegistryCapacityError("user registry capacity reached")

        user = UserAccount(**request.model_dump())
        connection.execute(
            """
            INSERT INTO users (
                site_id,
                id,
                username,
                display_name,
                role,
                enabled
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                self._site_id,
                str(user.id),
                user.username,
                user.display_name,
                user.role.value,
                int(user.enabled),
            ),
        )
        self._append_audit(
            connection,
            user_id=user.id,
            action="created",
            actor=actor,
            changed_fields=("username", "display_name", "role", "enabled"),
        )
        return user

    def create(self, request: UserCreate, *, actor: str) -> UserAccount:
        """Create one user and its audit record in the same durable transaction."""
        normalized_actor = self._validate_actor(actor)
        with self._lock:
            connection = self._require_connection()
            try:
                connection.execute("BEGIN IMMEDIATE")
                user = self._insert_user(connection, request, actor=normalized_actor)
                connection.execute("COMMIT")
                return user
            except (UserRegistryCapacityError, UserRegistryConflictError):
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            except sqlite3.Error as exc:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise UserRegistryStorageError("user registry update failed") from exc

    def create_with_bootstrap(
        self,
        request: UserCreate,
        *,
        actor: str,
        ttl_seconds: int = DEFAULT_BOOTSTRAP_TTL_SECONDS,
    ) -> tuple[UserAccount, str, datetime]:
        """Create one user plus a short-lived credential without storing its plaintext."""
        normalized_actor = self._validate_actor(actor)
        if not MIN_BOOTSTRAP_TTL_SECONDS <= ttl_seconds <= MAX_BOOTSTRAP_TTL_SECONDS:
            raise ValueError(
                f"ttl_seconds must be between {MIN_BOOTSTRAP_TTL_SECONDS} "
                f"and {MAX_BOOTSTRAP_TTL_SECONDS}"
            )

        temporary_credential = secrets.token_urlsafe(32)
        salt = secrets.token_bytes(_SALT_BYTES)
        verifier = _derive_credential(temporary_credential, salt)
        expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)

        with self._lock:
            connection = self._require_connection()
            try:
                connection.execute("BEGIN IMMEDIATE")
                user = self._insert_user(connection, request, actor=normalized_actor)
                connection.execute(
                    """
                    INSERT INTO user_credentials (
                        site_id,
                        user_id,
                        scheme,
                        credential_kind,
                        salt,
                        verifier,
                        expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self._site_id,
                        str(user.id),
                        _CREDENTIAL_SCHEME,
                        "bootstrap",
                        salt,
                        verifier,
                        expires_at.isoformat(),
                    ),
                )
                self._append_audit(
                    connection,
                    user_id=user.id,
                    action="bootstrap-issued",
                    actor=normalized_actor,
                    changed_fields=("credential_state",),
                )
                connection.execute("COMMIT")
                return user, temporary_credential, expires_at
            except (UserRegistryCapacityError, UserRegistryConflictError):
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            except sqlite3.Error as exc:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise UserRegistryStorageError("user registry update failed") from exc

    def activate_bootstrap_password(
        self,
        *,
        username: str,
        temporary_credential: str,
        new_password: str,
        actor: str,
    ) -> UserAccount | None:
        """Atomically replace a valid one-time bootstrap verifier with a password verifier."""
        normalized_username = self._validate_username(username)
        normalized_actor = self._validate_actor(actor)
        if not 12 <= len(new_password) <= 256:
            raise ValueError("new_password must be between 12 and 256 characters")
        if compare_digest(new_password, temporary_credential):
            raise ValueError("new_password must differ from the bootstrap credential")

        new_salt = secrets.token_bytes(_SALT_BYTES)
        new_verifier = _derive_credential(new_password, new_salt)
        now = datetime.now(UTC)

        with self._lock:
            connection = self._require_connection()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT
                        users.id,
                        users.username,
                        users.display_name,
                        users.role,
                        users.enabled,
                        user_credentials.scheme,
                        user_credentials.credential_kind,
                        user_credentials.salt,
                        user_credentials.verifier,
                        user_credentials.expires_at
                    FROM users
                    LEFT JOIN user_credentials
                      ON user_credentials.site_id = users.site_id
                     AND user_credentials.user_id = users.id
                    WHERE users.site_id = ? AND users.username = ?
                    """,
                    (self._site_id, normalized_username),
                ).fetchone()

                salt = _DUMMY_SALT
                expected = _DUMMY_VERIFIER
                eligible = False
                if row is not None and row["scheme"] == _CREDENTIAL_SCHEME:
                    try:
                        salt = bytes(row["salt"])
                        expected = bytes(row["verifier"])
                    except (TypeError, ValueError) as exc:
                        raise UserRegistryStorageError(
                            "user credential registry contains invalid state"
                        ) from exc
                    if len(salt) != _SALT_BYTES or len(expected) != _SCRYPT_DKLEN:
                        raise UserRegistryStorageError(
                            "user credential registry contains invalid state"
                        )
                    if row["credential_kind"] == "bootstrap":
                        try:
                            expires_at = datetime.fromisoformat(row["expires_at"])
                        except (TypeError, ValueError) as exc:
                            raise UserRegistryStorageError(
                                "user credential registry contains invalid state"
                            ) from exc
                        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
                            raise UserRegistryStorageError(
                                "user credential registry contains invalid state"
                            )
                        eligible = bool(bool(row["enabled"]) and expires_at > now)

                candidate = _derive_credential(temporary_credential, salt)
                if not eligible or not compare_digest(candidate, expected):
                    connection.execute("COMMIT")
                    return None
                if row is None:
                    connection.execute("COMMIT")
                    return None

                user = self._decode_user(row)
                connection.execute(
                    """
                    UPDATE user_credentials
                    SET scheme = ?, credential_kind = ?, salt = ?, verifier = ?, expires_at = NULL
                    WHERE site_id = ? AND user_id = ?
                    """,
                    (
                        _CREDENTIAL_SCHEME,
                        "password",
                        new_salt,
                        new_verifier,
                        self._site_id,
                        str(user.id),
                    ),
                )
                self._append_audit(
                    connection,
                    user_id=user.id,
                    action="bootstrap-consumed",
                    actor=normalized_actor,
                    changed_fields=("credential_state",),
                )
                connection.execute("COMMIT")
                return user
            except UserRegistryStorageError:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            except sqlite3.Error as exc:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise UserRegistryStorageError("user registry update failed") from exc

    def verify_password(self, *, username: str, password: str) -> UserAccount | None:
        """Verify an enabled initialized account without exposing credential metadata."""
        normalized_username = self._validate_username(username)
        if not 1 <= len(password) <= 256:
            return None
        with self._lock:
            connection = self._require_connection()
            try:
                row = connection.execute(
                    """
                    SELECT
                        users.id,
                        users.username,
                        users.display_name,
                        users.role,
                        users.enabled,
                        user_credentials.scheme,
                        user_credentials.credential_kind,
                        user_credentials.salt,
                        user_credentials.verifier
                    FROM users
                    LEFT JOIN user_credentials
                      ON user_credentials.site_id = users.site_id
                     AND user_credentials.user_id = users.id
                    WHERE users.site_id = ? AND users.username = ?
                    """,
                    (self._site_id, normalized_username),
                ).fetchone()
            except sqlite3.Error as exc:
                raise UserRegistryStorageError("user registry read failed") from exc

        salt = _DUMMY_SALT
        expected = _DUMMY_VERIFIER
        eligible = False
        if row is not None and row["scheme"] == _CREDENTIAL_SCHEME:
            try:
                salt = bytes(row["salt"])
                expected = bytes(row["verifier"])
            except (TypeError, ValueError) as exc:
                raise UserRegistryStorageError(
                    "user credential registry contains invalid state"
                ) from exc
            if len(salt) != _SALT_BYTES or len(expected) != _SCRYPT_DKLEN:
                raise UserRegistryStorageError("user credential registry contains invalid state")
            eligible = bool(row["credential_kind"] == "password" and bool(row["enabled"]))

        candidate = _derive_credential(password, salt)
        if not eligible or not compare_digest(candidate, expected) or row is None:
            return None
        return self._decode_user(row)

    def update(self, user_id: UUID, patch: UserPatch, *, actor: str) -> UserAccount | None:
        """Apply a small permitted user change and audit it transactionally."""
        normalized_actor = self._validate_actor(actor)
        with self._lock:
            connection = self._require_connection()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT id, username, display_name, role, enabled
                    FROM users
                    WHERE site_id = ? AND id = ?
                    """,
                    (self._site_id, str(user_id)),
                ).fetchone()
                if row is None:
                    connection.execute("COMMIT")
                    return None

                current = self._decode_user(row)
                values = current.model_dump()
                changed: list[str] = []
                for field_name in ("display_name", "role", "enabled"):
                    value = getattr(patch, field_name)
                    if value is not None and value != getattr(current, field_name):
                        values[field_name] = value
                        changed.append(field_name)
                updated = UserAccount(**values)
                if changed:
                    connection.execute(
                        """
                        UPDATE users
                        SET display_name = ?, role = ?, enabled = ?
                        WHERE site_id = ? AND id = ?
                        """,
                        (
                            updated.display_name,
                            updated.role.value,
                            int(updated.enabled),
                            self._site_id,
                            str(user_id),
                        ),
                    )
                    self._append_audit(
                        connection,
                        user_id=user_id,
                        action="updated",
                        actor=normalized_actor,
                        changed_fields=tuple(changed),
                    )
                connection.execute("COMMIT")
                return updated
            except sqlite3.Error as exc:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise UserRegistryStorageError("user registry update failed") from exc

    def list(self, *, offset: int = 0, limit: int = MAX_USER_PAGE_SIZE) -> list[UserAccount]:
        """Return one bounded page of users for this site."""
        if offset < 0:
            raise ValueError("offset must be non-negative")
        if not 1 <= limit <= MAX_USER_PAGE_SIZE:
            raise ValueError(f"limit must be between 1 and {MAX_USER_PAGE_SIZE}")
        with self._lock:
            connection = self._require_connection()
            try:
                rows = connection.execute(
                    """
                    SELECT id, username, display_name, role, enabled
                    FROM users
                    WHERE site_id = ?
                    ORDER BY row_id
                    LIMIT ? OFFSET ?
                    """,
                    (self._site_id, limit, offset),
                ).fetchall()
            except sqlite3.Error as exc:
                raise UserRegistryStorageError("user registry read failed") from exc
        return [self._decode_user(row) for row in rows]

    def audit_events(
        self,
        *,
        offset: int = 0,
        limit: int = MAX_USER_AUDIT_PAGE_SIZE,
    ) -> list[UserAuditEvent]:
        """Return a bounded page of credential-free administrative audit records."""
        if offset < 0:
            raise ValueError("offset must be non-negative")
        if not 1 <= limit <= MAX_USER_AUDIT_PAGE_SIZE:
            raise ValueError(f"limit must be between 1 and {MAX_USER_AUDIT_PAGE_SIZE}")
        with self._lock:
            connection = self._require_connection()
            try:
                rows = connection.execute(
                    """
                    SELECT event_id, user_id, action, actor, changed_fields_json, occurred_at
                    FROM user_audit
                    WHERE site_id = ?
                    ORDER BY event_id
                    LIMIT ? OFFSET ?
                    """,
                    (self._site_id, limit, offset),
                ).fetchall()
            except sqlite3.Error as exc:
                raise UserRegistryStorageError("user audit read failed") from exc

        events: list[UserAuditEvent] = []
        try:
            for row in rows:
                events.append(
                    UserAuditEvent(
                        event_id=row["event_id"],
                        user_id=UUID(row["user_id"]),
                        action=row["action"],
                        actor=row["actor"],
                        changed_fields=tuple(json.loads(row["changed_fields_json"])),
                        occurred_at=row["occurred_at"],
                    )
                )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise UserRegistryStorageError("user audit contains invalid state") from exc
        return events

    def close(self) -> None:
        """Close the database handle without deleting durable state."""
        with self._lock:
            connection = self._connection
            self._connection = None
            if connection is not None:
                connection.close()
