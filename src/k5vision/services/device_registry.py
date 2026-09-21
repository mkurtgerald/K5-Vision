"""Persistent, site-scoped device registration service."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from k5vision.domain.devices import Device, DeviceCreate

DEFAULT_DEVICE_CAPACITY = 1024
MAX_DEVICE_CAPACITY = 100_000
MAX_DEVICE_PAGE_SIZE = 100
_SITE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class DeviceRegistryCapacityError(RuntimeError):
    """Raised when a bounded registry cannot accept another device."""


class DeviceRegistryConflictError(RuntimeError):
    """Raised when an endpoint is already enrolled with conflicting metadata."""


class DeviceRegistryStorageError(RuntimeError):
    """Raised when durable device state cannot be read or updated safely."""


@dataclass(frozen=True)
class DeviceRegistration:
    """Result of one transactional, idempotent enrollment attempt."""

    device: Device
    created: bool


class DeviceRegistry:
    """Persist canonical devices within one bounded site authority scope."""

    def __init__(
        self,
        *,
        capacity: int = DEFAULT_DEVICE_CAPACITY,
        database_path: str | Path = ":memory:",
        site_id: str = "default",
    ) -> None:
        if not 1 <= capacity <= MAX_DEVICE_CAPACITY:
            raise ValueError(f"capacity must be between 1 and {MAX_DEVICE_CAPACITY}")
        normalized_site = site_id.strip()
        if not _SITE_ID_PATTERN.fullmatch(normalized_site):
            raise ValueError("site_id must be a bounded ASCII identifier")

        path = str(database_path).strip()
        if not path:
            raise ValueError("database_path must not be empty")
        if path != ":memory:":
            database_file = Path(path).expanduser()
            if not database_file.parent.is_dir():
                raise DeviceRegistryStorageError("device registry parent directory is unavailable")
            if database_file.exists() and database_file.is_dir():
                raise DeviceRegistryStorageError("device registry path is not a file")
            path = str(database_file)

        self._capacity = capacity
        self._site_id = normalized_site
        self._database_path = path
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None
        self._open()

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def site_id(self) -> str:
        return self._site_id

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
                CREATE TABLE IF NOT EXISTS devices (
                    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    site_id TEXT NOT NULL,
                    id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    host TEXT NOT NULL,
                    management_port INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    protocols_json TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    UNIQUE(site_id, id),
                    UNIQUE(site_id, host, management_port)
                )
                """
            )
        except (OSError, sqlite3.Error) as exc:
            if connection is not None:
                connection.close()
            raise DeviceRegistryStorageError("device registry is unavailable") from exc
        self._connection = connection

    def _require_connection(self) -> sqlite3.Connection:
        connection = self._connection
        if connection is None:
            raise DeviceRegistryStorageError("device registry is closed")
        return connection

    @staticmethod
    def _decode_device(row: sqlite3.Row) -> Device:
        try:
            return Device(
                id=UUID(row["id"]),
                name=row["name"],
                host=row["host"],
                management_port=row["management_port"],
                kind=row["kind"],
                protocols=json.loads(row["protocols_json"]),
                tags=json.loads(row["tags_json"]),
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise DeviceRegistryStorageError("device registry contains invalid state") from exc

    @staticmethod
    def _matches_request(device: Device, request: DeviceCreate) -> bool:
        return device.model_dump(exclude={"id"}) == request.model_dump()

    @staticmethod
    def _protocols_json(device: Device) -> str:
        return json.dumps(
            sorted(protocol.value for protocol in device.protocols),
            separators=(",", ":"),
        )

    @staticmethod
    def _tags_json(device: Device) -> str:
        return json.dumps(sorted(device.tags), separators=(",", ":"))

    def enroll(self, request: DeviceCreate) -> DeviceRegistration:
        """Enroll one endpoint transactionally or return its identical existing record."""
        with self._lock:
            connection = self._require_connection()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT id, name, host, management_port, kind, protocols_json, tags_json
                    FROM devices
                    WHERE site_id = ? AND host = ? AND management_port = ?
                    """,
                    (self._site_id, str(request.host), request.management_port),
                ).fetchone()

                if row is not None:
                    existing = self._decode_device(row)
                    if not self._matches_request(existing, request):
                        raise DeviceRegistryConflictError(
                            "device endpoint is already enrolled with conflicting metadata"
                        )
                    connection.execute("COMMIT")
                    return DeviceRegistration(existing, created=False)

                count = connection.execute(
                    "SELECT COUNT(*) FROM devices WHERE site_id = ?",
                    (self._site_id,),
                ).fetchone()[0]
                if count >= self._capacity:
                    raise DeviceRegistryCapacityError("device registry capacity reached")

                device = Device(**request.model_dump())
                connection.execute(
                    """
                    INSERT INTO devices (
                        site_id,
                        id,
                        name,
                        host,
                        management_port,
                        kind,
                        protocols_json,
                        tags_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self._site_id,
                        str(device.id),
                        device.name,
                        str(device.host),
                        device.management_port,
                        device.kind.value,
                        self._protocols_json(device),
                        self._tags_json(device),
                    ),
                )
                connection.execute("COMMIT")
                return DeviceRegistration(device, created=True)
            except (DeviceRegistryCapacityError, DeviceRegistryConflictError):
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            except sqlite3.Error as exc:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise DeviceRegistryStorageError("device registry update failed") from exc

    def register(self, request: DeviceCreate) -> Device:
        """Compatibility wrapper returning the canonical enrolled device."""
        return self.enroll(request).device

    def list(self, *, offset: int = 0, limit: int = MAX_DEVICE_PAGE_SIZE) -> list[Device]:
        """Return one bounded page for this site in stable insertion order."""
        if offset < 0:
            raise ValueError("offset must be non-negative")
        if not 1 <= limit <= MAX_DEVICE_PAGE_SIZE:
            raise ValueError(f"limit must be between 1 and {MAX_DEVICE_PAGE_SIZE}")

        with self._lock:
            connection = self._require_connection()
            try:
                rows = connection.execute(
                    """
                    SELECT id, name, host, management_port, kind, protocols_json, tags_json
                    FROM devices
                    WHERE site_id = ?
                    ORDER BY row_id
                    LIMIT ? OFFSET ?
                    """,
                    (self._site_id, limit, offset),
                ).fetchall()
            except sqlite3.Error as exc:
                raise DeviceRegistryStorageError("device registry read failed") from exc
        return [self._decode_device(row) for row in rows]

    def get(self, device_id: UUID) -> Device | None:
        """Return one device only when it belongs to this site scope."""
        with self._lock:
            connection = self._require_connection()
            try:
                row = connection.execute(
                    """
                    SELECT id, name, host, management_port, kind, protocols_json, tags_json
                    FROM devices
                    WHERE site_id = ? AND id = ?
                    """,
                    (self._site_id, str(device_id)),
                ).fetchone()
            except sqlite3.Error as exc:
                raise DeviceRegistryStorageError("device registry read failed") from exc
        return None if row is None else self._decode_device(row)

    def close(self) -> None:
        """Close the database handle without deleting durable state."""
        with self._lock:
            connection = self._connection
            self._connection = None
            if connection is not None:
                connection.close()
