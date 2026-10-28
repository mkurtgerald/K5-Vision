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
from time import monotonic
from uuid import UUID

from k5vision.domain.users import UserAccount, UserAuditEvent, UserCreate, UserPatch, UserRole

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
    """Raised when a user mutation conflicts with existing identity or authority."""


class UserRegistryLastAdministratorError(UserRegistryConflictError):
    """Raised when no other enabled password-initialized administrator would remain."""


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
        self._bootstrap_deadlines: dict[UUID, tuple[bytes, float]] = {}
        self._open()

    @property
    def site_id(self) -> str:
        """The immutable authority scope used by every account lookup."""
        return self._site_id

    def get(self, user_id: UUID) -> UserAccount | None:
        """Read current authority-bearing metadata within this registry's site."""
        with self._lock:
            connection = self._require_connection()
            try:
                row = connection.execute(
                    """
                    SELECT id, username, display_name, role, enabled
                    FROM users WHERE site_id = ? AND id = ?
                    """,
                    (self._site_id, str(user_id)),
                ).fetchone()
            except sqlite3.Error:
                raise UserRegistryStorageError("user registry read failed") from None
        return None if row is None else self._decode_user(row)

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
            connection.execute(
                """CREATE TABLE IF NOT EXISTS user_camera_access (
                       site_id TEXT NOT NULL,
                       user_id TEXT NOT NULL,
                       mode TEXT NOT NULL,
                       camera_ids_json TEXT NOT NULL,
                       PRIMARY KEY(site_id, user_id)
                   )"""
            )
            connection.execute("BEGIN IMMEDIATE")
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(user_credentials)")
            }
            for name in ("issued_at", "last_checked_at"):
                if name not in columns:
                    connection.execute(f"ALTER TABLE user_credentials ADD COLUMN {name} TEXT")
            connection.execute("COMMIT")
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

    def bootstrap_administrator(self, request: UserCreate, *, password: str) -> UserAccount:
        """Initialize a new local installation, never recover or replace existing access.

        This is a local setup operation, deliberately not an HTTP endpoint. The
        empty-registry check, ordinary password verifier, account and audit are
        committed together. Any prior identity, credential or audit closes it.
        """
        if request.role is not UserRole.ADMINISTRATOR or not request.enabled:
            raise ValueError("local setup requires an enabled administrator")
        if type(password) is not str or not 12 <= len(password) <= 256:
            raise ValueError("password must be between 12 and 256 characters")
        salt = secrets.token_bytes(_SALT_BYTES)
        verifier = _derive_credential(password, salt)
        actor = "local-admin-setup"
        with self._lock:
            connection = self._require_connection()
            try:
                connection.execute("BEGIN IMMEDIATE")
                for table in ("users", "user_credentials", "user_audit"):
                    if connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                        raise UserRegistryConflictError("local administrator setup is closed")
                user = self._insert_user(connection, request, actor=actor)
                connection.execute(
                    """
                    INSERT INTO user_credentials (
                        site_id, user_id, scheme, credential_kind, salt, verifier, expires_at
                    ) VALUES (?, ?, ?, 'password', ?, ?, NULL)
                    """,
                    (self._site_id, str(user.id), _CREDENTIAL_SCHEME, salt, verifier),
                )
                self._append_audit(
                    connection,
                    user_id=user.id,
                    action="password-initialized",
                    actor=actor,
                    changed_fields=("credential_state",),
                )
                connection.execute("COMMIT")
                return user
            except UserRegistryConflictError:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            except sqlite3.Error as exc:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise UserRegistryStorageError("local administrator setup failed") from exc

    def create_with_bootstrap(
        self,
        request: UserCreate,
        *,
        actor: str,
        ttl_seconds: int = DEFAULT_BOOTSTRAP_TTL_SECONDS,
        camera_access: dict | None = None,
    ) -> tuple[UserAccount, str, datetime]:
        """Create one user plus a short-lived credential without storing its plaintext."""
        normalized_actor = self._validate_actor(actor)
        if (
            type(ttl_seconds) is not int
            or not MIN_BOOTSTRAP_TTL_SECONDS <= ttl_seconds <= MAX_BOOTSTRAP_TTL_SECONDS
        ):
            raise ValueError(
                f"ttl_seconds must be between {MIN_BOOTSTRAP_TTL_SECONDS} "
                f"and {MAX_BOOTSTRAP_TTL_SECONDS}"
            )

        temporary_credential = secrets.token_urlsafe(32)
        salt = secrets.token_bytes(_SALT_BYTES)
        verifier = _derive_credential(temporary_credential, salt)
        issued_at = datetime.now(UTC)
        expires_at = issued_at + timedelta(seconds=ttl_seconds)

        with self._lock:
            connection = self._require_connection()
            try:
                connection.execute("BEGIN IMMEDIATE")
                user = self._insert_user(connection, request, actor=normalized_actor)
                if camera_access is not None:
                    self._write_camera_access(connection, user, camera_access, normalized_actor)
                connection.execute(
                    """
                    INSERT INTO user_credentials (
                        site_id,
                        user_id,
                        scheme,
                        credential_kind,
                        salt,
                        verifier,
                        expires_at, issued_at, last_checked_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self._site_id,
                        str(user.id),
                        _CREDENTIAL_SCHEME,
                        "bootstrap",
                        salt,
                        verifier,
                        expires_at.isoformat(),
                        issued_at.isoformat(),
                        issued_at.isoformat(),
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
                self._bootstrap_deadlines[user.id] = (verifier, monotonic() + ttl_seconds)
                return user, temporary_credential, expires_at
            except (UserRegistryCapacityError, UserRegistryConflictError, ValueError):
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            except sqlite3.Error as exc:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise UserRegistryStorageError("user registry update failed") from exc

    @staticmethod
    def _camera_access_value(user: UserAccount, access: dict) -> dict:
        if not isinstance(access, dict) or set(access) != {"mode", "camera_ids"}:
            raise ValueError("camera access requires mode and camera_ids")
        mode, identifiers = access["mode"], access["camera_ids"]
        if mode not in {"all", "selected"} or type(identifiers) not in {list, tuple}:
            raise ValueError("camera access selection is invalid")
        if len(identifiers) > 64:
            raise ValueError("camera access selection exceeds capacity")
        try:
            cameras = [item if isinstance(item, UUID) else UUID(item) for item in identifiers]
        except (ValueError, TypeError, AttributeError):
            raise ValueError("camera access selection is invalid") from None
        if len(set(cameras)) != len(cameras) or (mode == "all" and cameras):
            raise ValueError("camera access selection is invalid")
        if user.role is UserRole.ADMINISTRATOR and mode != "all":
            raise ValueError("administrators require access to all cameras")
        return {"mode": mode, "camera_ids": sorted(cameras, key=str)}

    def _read_camera_access(self, connection: sqlite3.Connection, user: UserAccount) -> dict:
        row = connection.execute(
            "SELECT mode, camera_ids_json FROM user_camera_access "
            "WHERE site_id = ? AND user_id = ?",
            (self._site_id, str(user.id)),
        ).fetchone()
        if row is None or user.role is UserRole.ADMINISTRATOR:
            return {"mode": "all", "camera_ids": []}
        try:
            return self._camera_access_value(
                user, {"mode": row["mode"], "camera_ids": json.loads(row["camera_ids_json"])}
            )
        except (ValueError, TypeError, json.JSONDecodeError):
            raise UserRegistryStorageError("user camera access contains invalid state") from None

    def _write_camera_access(
        self, connection: sqlite3.Connection, user: UserAccount, access: dict, actor: str
    ) -> dict:
        admitted = self._camera_access_value(user, access)
        previous = self._read_camera_access(connection, user)
        connection.execute(
            """INSERT INTO user_camera_access (site_id, user_id, mode, camera_ids_json)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(site_id, user_id) DO UPDATE SET
                 mode = excluded.mode, camera_ids_json = excluded.camera_ids_json""",
            (
                self._site_id,
                str(user.id),
                admitted["mode"],
                json.dumps([str(item) for item in admitted["camera_ids"]], separators=(",", ":")),
            ),
        )
        if admitted != previous:
            self._append_audit(
                connection,
                user_id=user.id,
                action="camera-access-updated",
                actor=actor,
                changed_fields=("camera_access",),
            )
        return admitted

    def get_camera_access(self, user_id: UUID) -> dict | None:
        """Return bounded site-owned camera grants; legacy accounts retain all access."""
        with self._lock:
            user = self.get(user_id)
            if user is None:
                return None
            try:
                return self._read_camera_access(self._require_connection(), user)
            except sqlite3.Error:
                raise UserRegistryStorageError("user camera access is unavailable") from None

    def set_camera_access(
        self, user_id: UUID, *, mode: str, camera_ids: list[UUID], actor: str
    ) -> dict | None:
        """Commit an administrator's explicit camera selection and audit together."""
        actor = self._validate_actor(actor)
        with self._lock:
            connection = self._require_connection()
            try:
                connection.execute("BEGIN IMMEDIATE")
                user = self.get(user_id)
                if user is None:
                    connection.execute("COMMIT")
                    return None
                result = self._write_camera_access(
                    connection, user, {"mode": mode, "camera_ids": camera_ids}, actor
                )
                connection.execute("COMMIT")
                return result
            except (ValueError, UserRegistryStorageError):
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            except sqlite3.Error:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise UserRegistryStorageError("user camera access update failed") from None

    def _other_administrator(self, connection: sqlite3.Connection, user_id: UUID) -> bool:
        return (
            connection.execute(
                """SELECT 1 FROM users JOIN user_credentials
                 ON user_credentials.site_id = users.site_id AND user_credentials.user_id = users.id
               WHERE users.site_id = ? AND users.id != ? AND users.role = ? AND users.enabled = 1
                 AND user_credentials.credential_kind = 'password'
                 AND user_credentials.scheme = ? AND user_credentials.expires_at IS NULL LIMIT 1""",
                (self._site_id, str(user_id), UserRole.ADMINISTRATOR.value, _CREDENTIAL_SCHEME),
            ).fetchone()
            is not None
        )

    def reset_bootstrap(
        self, user_id: UUID, *, actor: str, ttl_seconds: int = DEFAULT_BOOTSTRAP_TTL_SECONDS
    ) -> tuple[UserAccount, str, datetime] | None:
        """Replace access with one new expiring credential; preserve another usable admin."""
        actor = self._validate_actor(actor)
        if (
            type(ttl_seconds) is not int
            or not MIN_BOOTSTRAP_TTL_SECONDS <= ttl_seconds <= MAX_BOOTSTRAP_TTL_SECONDS
        ):
            raise ValueError("bootstrap credential lifetime is invalid")
        temporary = secrets.token_urlsafe(32)
        salt = secrets.token_bytes(_SALT_BYTES)
        verifier = _derive_credential(temporary, salt)
        issued = datetime.now(UTC)
        expires = issued + timedelta(seconds=ttl_seconds)
        with self._lock:
            connection = self._require_connection()
            try:
                connection.execute("BEGIN IMMEDIATE")
                user = self.get(user_id)
                if user is None:
                    connection.execute("COMMIT")
                    return None
                if (
                    user.enabled
                    and user.role is UserRole.ADMINISTRATOR
                    and self.credential_status(user.id) == "active"
                    and not self._other_administrator(connection, user.id)
                ):
                    raise UserRegistryLastAdministratorError(
                        "Activate another enabled administrator before resetting this administrator"
                    )
                connection.execute(
                    """INSERT INTO user_credentials
                         (site_id, user_id, scheme, credential_kind, salt, verifier,
                          expires_at, issued_at, last_checked_at)
                       VALUES (?, ?, ?, 'bootstrap', ?, ?, ?, ?, ?)
                       ON CONFLICT(site_id, user_id) DO UPDATE SET
                         scheme = excluded.scheme, credential_kind = excluded.credential_kind,
                         salt = excluded.salt, verifier = excluded.verifier,
                         expires_at = excluded.expires_at, issued_at = excluded.issued_at,
                         last_checked_at = excluded.last_checked_at""",
                    (
                        self._site_id,
                        str(user.id),
                        _CREDENTIAL_SCHEME,
                        salt,
                        verifier,
                        expires.isoformat(),
                        issued.isoformat(),
                        issued.isoformat(),
                    ),
                )
                self._append_audit(
                    connection,
                    user_id=user.id,
                    action="password-reset",
                    actor=actor,
                    changed_fields=("credential_state",),
                )
                connection.execute("COMMIT")
                self._bootstrap_deadlines[user.id] = (verifier, monotonic() + ttl_seconds)
                return user, temporary, expires
            except (UserRegistryConflictError, UserRegistryStorageError, ValueError):
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            except sqlite3.Error:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise UserRegistryStorageError("password reset failed") from None

    def _bootstrap_eligible(
        self, connection: sqlite3.Connection, row: sqlite3.Row, now: datetime
    ) -> bool:
        try:
            expiry = datetime.fromisoformat(row["expires_at"])
            issued = datetime.fromisoformat(row["issued_at"]) if row["issued_at"] else None
            checked = (
                datetime.fromisoformat(row["last_checked_at"]) if row["last_checked_at"] else issued
            )
            if any(
                value is not None and (value.tzinfo is None or value.utcoffset() is None)
                for value in (expiry, issued, checked)
            ):
                raise ValueError
            user_id, verifier = UUID(row["id"]), bytes(row["verifier"])
            known = self._bootstrap_deadlines.get(user_id)
            reconstruct = known is None or known[0] != verifier
            if reconstruct:
                lifetime = (
                    MAX_BOOTSTRAP_TTL_SECONDS
                    if issued is None
                    else (expiry - issued).total_seconds()
                )
                remaining = min((expiry - now).total_seconds(), lifetime, MAX_BOOTSTRAP_TTL_SECONDS)
                known = (verifier, monotonic() + max(0, remaining))
                self._bootstrap_deadlines[user_id] = known
            expired = known[1] <= monotonic() or (
                reconstruct and (expiry <= now or (checked is not None and now < checked))
            )
            if expired:
                connection.execute(
                    """UPDATE user_credentials SET credential_kind = 'expired-bootstrap'
                       WHERE site_id = ? AND user_id = ? AND credential_kind = 'bootstrap'
                         AND verifier = ?""",
                    (self._site_id, str(user_id), verifier),
                )
                self._bootstrap_deadlines.pop(user_id, None)
                return False
            connection.execute(
                """UPDATE user_credentials SET last_checked_at = ?
                   WHERE site_id = ? AND user_id = ? AND credential_kind = 'bootstrap'
                     AND verifier = ?""",
                (
                    max(now, checked).isoformat() if checked is not None else now.isoformat(),
                    self._site_id,
                    str(user_id),
                    verifier,
                ),
            )
            return True
        except (ValueError, TypeError, OverflowError):
            raise UserRegistryStorageError(
                "user credential registry contains invalid state"
            ) from None

    def credential_status(self, user_id: UUID) -> str | None:
        """Return only the provisioning state, never verifiers or plaintext credentials."""
        with self._lock:
            connection = self._require_connection()
            try:
                row = connection.execute(
                    """SELECT users.id, user_credentials.* FROM users LEFT JOIN user_credentials
                         ON user_credentials.site_id = users.site_id
                           AND user_credentials.user_id = users.id
                       WHERE users.site_id = ? AND users.id = ?""",
                    (self._site_id, str(user_id)),
                ).fetchone()
                if row is None:
                    return None
                if row["scheme"] is None:
                    return "uninitialized"
                if row["scheme"] != _CREDENTIAL_SCHEME:
                    raise UserRegistryStorageError(
                        "user credential registry contains invalid state"
                    )
                if row["credential_kind"] == "password" and row["expires_at"] is None:
                    return "active"
                if row["credential_kind"] == "expired-bootstrap":
                    return "expired"
                if row["credential_kind"] == "bootstrap":
                    return (
                        "pending"
                        if self._bootstrap_eligible(connection, row, datetime.now(UTC))
                        else "expired"
                    )
                raise UserRegistryStorageError("user credential registry contains invalid state")
            except sqlite3.Error:
                raise UserRegistryStorageError("user credential state is unavailable") from None

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
        if type(new_password) is not str or not 12 <= len(new_password) <= 256:
            raise ValueError("new_password must be between 12 and 256 characters")
        if compare_digest(new_password.encode(), temporary_credential.encode()):
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
                        user_credentials.expires_at,
                        user_credentials.issued_at,
                        user_credentials.last_checked_at
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
                        eligible = bool(row["enabled"]) and self._bootstrap_eligible(
                            connection, row, now
                        )

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
                    SET scheme = ?, credential_kind = ?, salt = ?, verifier = ?, expires_at = NULL,
                        issued_at = NULL, last_checked_at = NULL
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
                self._bootstrap_deadlines.pop(user.id, None)
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
        verified = self.verify_password_snapshot(username=username, password=password)
        return None if verified is None else verified[0]

    def verify_password_snapshot(
        self, *, username: str, password: str
    ) -> tuple[UserAccount, bytes] | None:
        """Bind a login to the exact password verifier that passed authentication."""
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
        return self._decode_user(row), hashlib.sha256(salt + expected).digest()

    def resolve_session(
        self, account: UserAccount, credential_version: bytes | None
    ) -> UserAccount | None:
        """Read current authority and reject a session after any password replacement."""
        if type(credential_version) is not bytes or len(credential_version) != 32:
            return None
        with self._lock:
            try:
                row = (
                    self._require_connection()
                    .execute(
                        """SELECT users.id, users.username, users.display_name,
                                  users.role, users.enabled,
                              user_credentials.scheme, user_credentials.credential_kind,
                              user_credentials.salt, user_credentials.verifier,
                              user_credentials.expires_at
                       FROM users JOIN user_credentials ON user_credentials.site_id = users.site_id
                         AND user_credentials.user_id = users.id
                       WHERE users.site_id = ? AND users.id = ?""",
                        (self._site_id, str(account.id)),
                    )
                    .fetchone()
                )
                if (
                    row is None
                    or not row["enabled"]
                    or row["scheme"] != _CREDENTIAL_SCHEME
                    or row["credential_kind"] != "password"
                    or row["expires_at"] is not None
                ):
                    return None
                current = self._decode_user(row)
                if current.role != account.role:
                    return None
                version = hashlib.sha256(bytes(row["salt"]) + bytes(row["verifier"])).digest()
                return current if compare_digest(version, credential_version) else None
            except (sqlite3.Error, ValueError, TypeError):
                raise UserRegistryStorageError("user session authority is unavailable") from None

    def update(
        self, user_id: UUID, patch: UserPatch, *, actor: str, camera_access: dict | None = None
    ) -> UserAccount | None:
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
                if camera_access is not None:
                    self._camera_access_value(updated, camera_access)
                if (
                    current.enabled
                    and current.role is UserRole.ADMINISTRATOR
                    and (not updated.enabled or updated.role is not UserRole.ADMINISTRATOR)
                ):
                    # BEGIN IMMEDIATE serializes this authority check with every
                    # update, including other registry instances on the same DB.
                    # A rejected mutation changes neither account nor audit.
                    administrators = connection.execute(
                        """
                        SELECT COUNT(*) FROM users
                        JOIN user_credentials
                          ON user_credentials.site_id = users.site_id
                         AND user_credentials.user_id = users.id
                        WHERE users.site_id = ? AND users.id != ?
                          AND users.role = ? AND users.enabled = 1
                          AND user_credentials.credential_kind = 'password'
                          AND user_credentials.scheme = ?
                          AND user_credentials.expires_at IS NULL
                        """,
                        (
                            self._site_id,
                            str(current.id),
                            UserRole.ADMINISTRATOR.value,
                            _CREDENTIAL_SCHEME,
                        ),
                    ).fetchone()[0]
                    if administrators == 0:
                        raise UserRegistryLastAdministratorError(
                            "Activate another enabled administrator "
                            "before removing this administrator"
                        )
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
                if camera_access is not None:
                    self._write_camera_access(connection, updated, camera_access, normalized_actor)
                elif (
                    updated.role is UserRole.ADMINISTRATOR
                    and current.role is not UserRole.ADMINISTRATOR
                ):
                    self._write_camera_access(
                        connection, updated, {"mode": "all", "camera_ids": []}, normalized_actor
                    )
                connection.execute("COMMIT")
                return updated
            except (UserRegistryLastAdministratorError, ValueError, UserRegistryStorageError):
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
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
        newest_first: bool = False,
    ) -> list[UserAuditEvent]:
        """Return a bounded page of credential-free administrative audit records."""
        if offset < 0:
            raise ValueError("offset must be non-negative")
        if not 1 <= limit <= MAX_USER_AUDIT_PAGE_SIZE:
            raise ValueError(f"limit must be between 1 and {MAX_USER_AUDIT_PAGE_SIZE}")
        if type(newest_first) is not bool:
            raise ValueError("audit ordering must be boolean")
        with self._lock:
            connection = self._require_connection()
            try:
                rows = connection.execute(
                    """
                    SELECT event_id, user_id, action, actor, changed_fields_json, occurred_at
                    FROM user_audit
                    WHERE site_id = ?
                    ORDER BY event_id """
                    + ("DESC" if newest_first else "ASC")
                    + " LIMIT ? OFFSET ?",
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
            self._bootstrap_deadlines.clear()
