"""Persistent, site-scoped user administration service."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from k5vision.domain.users import UserAccount, UserAuditEvent, UserCreate, UserPatch

DEFAULT_USER_CAPACITY = 1024
MAX_USER_CAPACITY = 100_000
MAX_USER_PAGE_SIZE = 100
MAX_USER_AUDIT_PAGE_SIZE = 100
_SITE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ACTOR_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class UserRegistryCapacityError(RuntimeError):
    """Raised when a bounded user registry cannot accept another account."""


class UserRegistryConflictError(RuntimeError):
    """Raised when a username is already present in the same site scope."""


class UserRegistryStorageError(RuntimeError):
    """Raised when durable user state cannot be read or updated safely."""


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

    def create(self, request: UserCreate, *, actor: str) -> UserAccount:
        """Create one user and its audit record in the same durable transaction."""
        normalized_actor = self._validate_actor(actor)
        with self._lock:
            connection = self._require_connection()
            try:
                connection.execute("BEGIN IMMEDIATE")
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
                    actor=normalized_actor,
                    changed_fields=("username", "display_name", "role", "enabled"),
                )
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
