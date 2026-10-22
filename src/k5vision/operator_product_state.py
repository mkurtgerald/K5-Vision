"""Protected, site-owned camera sources for the installed operator product.

Canonical device metadata remains in DeviceRegistry. Recoverable camera source
credentials live only in this separate private store beneath an admitted identity
directory. No source URI is included in diagnostic messages or representations.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import weakref
from pathlib import Path
from uuid import UUID

from k5vision import identity_state
from k5vision.domain.devices import Device, DeviceProtocol
from k5vision.media.mediamtx_config import MediaMTXSource
from k5vision.media.mediamtx_provider import MediaMTXBinding
from k5vision.services.device_registry import DeviceRegistry, DeviceRegistryConflictError

MAX_OPERATOR_CAMERAS = 64
_DATABASE = "operator-cameras.sqlite3"
_MAX_DEVICE_JSON_BYTES = 32_768
_STORE_LOCKS_GUARD = threading.Lock()
_STORE_LOCKS: weakref.WeakValueDictionary[tuple[int, int, str], threading.RLock] = (
    weakref.WeakValueDictionary()
)


class OperatorCameraStoreError(RuntimeError):
    """Private source state is absent, inconsistent or unavailable."""


class OperatorCameraCapacityError(OperatorCameraStoreError):
    """The bounded private source store cannot accept another camera."""


def _device_json(device: Device) -> str:
    value = device.model_dump(mode="json")
    value["protocols"] = sorted(value["protocols"])
    value["tags"] = sorted(value["tags"])
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _store_lock(root_identity: os.stat_result, site_id: str) -> threading.RLock:
    """Serialize custody checks and SQLite writes for an admitted in-process store.

    SQLite deletes its volatile journal during COMMIT. An independent admission
    probe must not overlap another store instance's journal lifecycle. The product
    profile lease separately excludes another product process; SQLite retains its
    transaction locks as well. Weak values retain no unused installation inventory.
    """
    key = (root_identity.st_dev, root_identity.st_ino, site_id)
    with _STORE_LOCKS_GUARD:
        lock = _STORE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _STORE_LOCKS[key] = lock
        return lock


class OperatorCameraStore:
    """Persist at most 64 private sources with atomic SQLite transactions.

    The root must already contain a complete identity installation. Passing the
    application's existing registry additionally checks that every stored source
    still belongs to an enrolled device; this class never creates a device registry.
    Existing corrupt/partial databases are refused rather than repaired or reset.
    """

    def __init__(
        self,
        root: Path,
        site_id: str,
        *,
        registry: DeviceRegistry | None = None,
    ) -> None:
        self._connection: sqlite3.Connection | None = None
        self._closed = False
        self._registry = registry
        failed = False
        connection = None
        try:
            if not isinstance(root, Path):
                raise ValueError
            state = identity_state.load_identity_state(root)
            if state.site_id != site_id:
                raise ValueError
            if registry is not None and (
                not isinstance(registry, DeviceRegistry) or registry.site_id != site_id
            ):
                raise ValueError
            self._root = state.directory
            self._site_id = state.site_id
            self._root_identity = self._root.lstat()
            self._lock = _store_lock(self._root_identity, self._site_id)
            self._database = self._root / _DATABASE
            with self._lock:
                self._admit_root()
                created = False
                try:
                    descriptor = os.open(
                        self._database,
                        os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                        0o600,
                    )
                except FileExistsError:
                    pass
                else:
                    os.close(descriptor)
                    created = True
                self._database_identity = self._private_file(self._database)
                self._admit_files()
                connection = sqlite3.connect(
                    self._database.as_uri() + "?mode=rw",
                    uri=True,
                    timeout=3.0,
                    isolation_level=None,
                    check_same_thread=False,
                )
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA synchronous = FULL")
                if created:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        "CREATE TABLE source_state "
                        "(schema_version INTEGER NOT NULL, site_id TEXT NOT NULL)"
                    )
                    connection.execute("INSERT INTO source_state VALUES (?, ?)", (1, self._site_id))
                    connection.execute(
                        """CREATE TABLE camera_sources (
                            device_id TEXT PRIMARY KEY,
                            device_json TEXT NOT NULL,
                            source_uri TEXT NOT NULL
                        )"""
                    )
                    connection.execute("COMMIT")
                self._admit_files()
                self._read_sources(connection)
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS camera_recording "
                        "(device_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL "
                        "CHECK (enabled IN (0, 1)))"
                    )
                    connection.execute("COMMIT")
                finally:
                    self._rollback(connection)
                self._admit_files()
                self._read_sources(connection)
                self._connection = connection
        except Exception:
            failed = True
            if connection is not None:
                with self._lock:
                    connection.close()
        if failed:
            self._closed = True
            raise OperatorCameraStoreError("Private camera state admission failed")

    @staticmethod
    def _private_file(path: Path) -> os.stat_result:
        info = identity_state._regular_file(path)
        if os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise identity_state.IdentityStateError("Camera state file must be private")
        return info

    def _admit_root(self) -> None:
        identity_state._same_directory(self._root, self._root_identity)
        state = identity_state.load_identity_state(self._root)
        if state.site_id != self._site_id:
            raise identity_state.IdentityStateError("Camera state belongs to a different site")

    def _admit_files(self) -> None:
        current = self._private_file(self._database)
        if (current.st_dev, current.st_ino) != (
            self._database_identity.st_dev,
            self._database_identity.st_ino,
        ):
            raise identity_state.IdentityStateError("Camera state changed during use")
        for suffix in ("-journal", "-wal", "-shm"):
            sidecar = Path(str(self._database) + suffix)
            try:
                sidecar.lstat()
            except FileNotFoundError:
                continue
            self._private_file(sidecar)

    def _admit(self) -> sqlite3.Connection:
        failed = False
        try:
            if self._closed or self._connection is None:
                raise ValueError
            self._admit_root()
            self._admit_files()
        except Exception:
            failed = True
        if failed:
            raise OperatorCameraStoreError("Private camera state is unavailable")
        return self._connection

    def _validated_source(
        self, device: Device, source_uri: str, recording_enabled: bool = True
    ) -> MediaMTXSource:
        failed = False
        try:
            if not isinstance(device, Device):
                raise ValueError
            # Device models are mutable; store and return an independent copy.
            admitted = Device.model_validate(device.model_dump(mode="json"))
            source = MediaMTXSource(
                MediaMTXBinding(self._site_id, admitted.id), admitted, source_uri, recording_enabled
            )
            if len(_device_json(admitted).encode("utf-8")) > _MAX_DEVICE_JSON_BYTES:
                raise ValueError
        except Exception:
            failed = True
        if failed:
            raise OperatorCameraStoreError("Camera source selection is invalid")
        return source

    def _source(
        self, device: Device, source_uri: str, recording_enabled: bool = True
    ) -> MediaMTXSource:
        source = self._validated_source(device, source_uri, recording_enabled)
        failed = False
        try:
            if self._registry is not None:
                enrolled = self._registry.get(source.device.id)
                if enrolled is None or enrolled.model_dump() != source.device.model_dump():
                    raise ValueError
        except Exception:
            failed = True
        if failed:
            raise OperatorCameraStoreError("Camera source selection is invalid")
        return source

    def _read_sources(self, connection: sqlite3.Connection) -> tuple[MediaMTXSource, ...]:
        failed = False
        try:
            states = connection.execute("SELECT * FROM source_state LIMIT 2").fetchall()
            if (
                len(states) != 1
                or type(states[0]["schema_version"]) is not int
                or states[0]["schema_version"] != 1
                or states[0]["site_id"] != self._site_id
            ):
                raise ValueError
            rows = connection.execute(
                "SELECT * FROM camera_sources ORDER BY device_id LIMIT ?",
                (MAX_OPERATOR_CAMERAS + 1,),
            ).fetchall()
            if len(rows) > MAX_OPERATOR_CAMERAS:
                raise ValueError
            sources = []
            preferences = {}
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='camera_recording'"
            ).fetchone():
                settings = connection.execute(
                    "SELECT device_id, enabled FROM camera_recording LIMIT ?",
                    (MAX_OPERATOR_CAMERAS + 1,),
                ).fetchall()
                if len(settings) > MAX_OPERATOR_CAMERAS:
                    raise ValueError
                for setting in settings:
                    identifier, enabled = setting["device_id"], setting["enabled"]
                    if (
                        str(UUID(identifier)) != identifier
                        or type(enabled) is not int
                        or enabled not in (0, 1)
                    ):
                        raise ValueError
                    preferences[identifier] = bool(enabled)
            for row in rows:
                device_json = row["device_json"]
                if (
                    not isinstance(device_json, str)
                    or len(device_json.encode("utf-8")) > _MAX_DEVICE_JSON_BYTES
                ):
                    raise ValueError
                value = json.loads(device_json, object_pairs_hook=identity_state._unique_object)
                device = Device.model_validate(value)
                if str(device.id) != row["device_id"]:
                    raise ValueError
                sources.append(
                    self._source(device, row["source_uri"], preferences.get(str(device.id), True))
                )
            result = tuple(sources)
        except Exception:
            failed = True
        if failed:
            raise OperatorCameraStoreError("Private camera source state is invalid")
        return result

    def list_sources(self) -> tuple[MediaMTXSource, ...]:
        """Return admitted execution sources only to the private application runtime."""
        with self._lock:
            return self._read_sources(self._admit())

    def get(self, device_id: UUID) -> MediaMTXSource | None:
        """Select one private source by its enrolled UUID."""
        if not isinstance(device_id, UUID):
            raise OperatorCameraStoreError("Camera selection is invalid")
        return next(
            (source for source in self.list_sources() if source.binding.device_id == device_id),
            None,
        )

    def set_recording_enabled(self, device_id: UUID, enabled: bool) -> MediaMTXSource:
        """Persist operator intent without changing the camera or removing its history."""
        if not isinstance(device_id, UUID) or type(enabled) is not bool:
            raise OperatorCameraStoreError("Recording preference is invalid")
        with self._lock:
            connection = self._admit()
            try:
                connection.execute("BEGIN IMMEDIATE")
                source = next(
                    value
                    for value in self._read_sources(connection)
                    if value.binding.device_id == device_id
                )
                connection.execute(
                    "INSERT INTO camera_recording (device_id, enabled) VALUES (?, ?) "
                    "ON CONFLICT(device_id) DO UPDATE SET enabled=excluded.enabled",
                    (str(device_id), int(enabled)),
                )
                self._admit()
                connection.execute("COMMIT")
                return MediaMTXSource(source.binding, source.device, source.source_uri, enabled)
            except Exception:
                self._rollback(connection)
                raise OperatorCameraStoreError("Recording preference could not be saved") from None

    @staticmethod
    def _rollback(connection: sqlite3.Connection) -> None:
        try:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    def upsert(self, device: Device, source_uri: str) -> MediaMTXSource:
        """Atomically create or replace one admitted camera's private source."""
        failure = None
        with self._lock:
            connection = self._admit()
            source = self._source(device, source_uri)
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = self._read_sources(connection)
                if (
                    source.binding.device_id not in {item.binding.device_id for item in existing}
                    and len(existing) >= MAX_OPERATOR_CAMERAS
                ):
                    raise OperatorCameraCapacityError
                enabled = next(
                    (
                        item.recording_enabled
                        for item in existing
                        if item.binding.device_id == source.binding.device_id
                    ),
                    True,
                )
                source = MediaMTXSource(source.binding, source.device, source.source_uri, enabled)
                connection.execute(
                    """INSERT INTO camera_sources (device_id, device_json, source_uri)
                        VALUES (?, ?, ?) ON CONFLICT(device_id) DO UPDATE
                        SET device_json = excluded.device_json, source_uri = excluded.source_uri""",
                    (str(source.device.id), _device_json(source.device), source.source_uri),
                )
                connection.execute("COMMIT")
            except OperatorCameraCapacityError:
                failure = OperatorCameraCapacityError
                self._rollback(connection)
            except Exception:
                failure = OperatorCameraStoreError
                self._rollback(connection)
        if failure is not None:
            raise failure("Private camera update failed")
        return source

    @staticmethod
    def _registry_files(path: Path, expected: os.stat_result | None = None) -> os.stat_result:
        """Admit canonical metadata without treating it as a credential store."""
        current = identity_state._regular_file(path)
        if os.name == "posix" and (current.st_uid != os.geteuid() or current.st_mode & 0o022):
            raise OperatorCameraStoreError("Canonical camera state is unavailable")
        if expected is not None and (current.st_dev, current.st_ino) != (
            expected.st_dev,
            expected.st_ino,
        ):
            raise OperatorCameraStoreError("Canonical camera state changed during use")
        for suffix in ("-journal", "-wal", "-shm"):
            sibling = Path(str(path) + suffix)
            try:
                sibling.lstat()
            except FileNotFoundError:
                continue
            info = identity_state._regular_file(sibling)
            if os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o022):
                raise OperatorCameraStoreError("Canonical camera state is unavailable")
        return current

    def reconnect_onvif(
        self,
        device_id: UUID,
        source_uri: str,
        management_port: int,
        secure: bool,
    ) -> MediaMTXSource:
        """Migrate an enrolled endpoint and source in one durable SQLite transaction.

        Attaching the canonical file to the private rollback-journal database
        lets SQLite's super-journal commit or recover both files together. The
        UUID and recording-path binding survive a management-port change.
        """
        failure = None
        source = None
        with self._lock:
            try:
                if (
                    not isinstance(device_id, UUID)
                    or type(management_port) is not int
                    or not 1 <= management_port <= 65535
                    or type(secure) is not bool
                    or self._registry is None
                ):
                    raise ValueError
                connection = self._admit()
                main = next(
                    row
                    for row in connection.execute("PRAGMA database_list")
                    if row["name"] == "main"
                )
                if not main["file"] or Path(main["file"]) != self._database:
                    raise ValueError
                registry = self._registry
                with registry.locked_operator_database(self._root / "devices.sqlite3") as path:
                    registry_identity = self._registry_files(path)
                    existing_device = registry.get(device_id)
                    if existing_device is None:
                        raise ValueError
                    data = existing_device.model_dump(mode="json")
                    data["management_port"] = management_port
                    data["protocols"] = sorted(
                        existing_device.protocols | {DeviceProtocol.ONVIF, DeviceProtocol.RTSP}
                    )
                    tags = existing_device.tags - {"k5-onvif-https"}
                    if secure:
                        tags.add("k5-onvif-https")
                    data["tags"] = sorted(tags)
                    # Validate the replacement and its private URI before a write begins.
                    previous = self.get(device_id)
                    source = self._validated_source(
                        Device.model_validate(data),
                        source_uri,
                        True if previous is None else previous.recording_enabled,
                    )
                    if connection.in_transaction or connection.execute(
                        "PRAGMA main.journal_mode"
                    ).fetchone()[0].lower() not in {"delete", "truncate", "persist"}:
                        raise ValueError
                    attached = False
                    try:
                        connection.execute(
                            "ATTACH DATABASE ? AS operator_registry", (path.as_uri() + "?mode=rw",)
                        )
                        attached = True
                        if connection.execute("PRAGMA operator_registry.journal_mode").fetchone()[
                            0
                        ].lower() not in {"delete", "truncate", "persist"}:
                            raise ValueError
                        connection.execute("PRAGMA operator_registry.synchronous = FULL")
                        connection.execute("BEGIN IMMEDIATE")
                        selections = self._read_sources(connection)
                        old_source = connection.execute(
                            "SELECT device_json, source_uri FROM camera_sources "
                            "WHERE device_id = ?",
                            (str(device_id),),
                        ).fetchone()
                        if old_source is None and len(selections) >= MAX_OPERATOR_CAMERAS:
                            raise OperatorCameraCapacityError
                        row = connection.execute(
                            """SELECT id, name, host, management_port, kind,
                                      protocols_json, tags_json
                               FROM operator_registry.devices WHERE site_id = ? AND id = ?""",
                            (self._site_id, str(device_id)),
                        ).fetchone()
                        if row is None or registry._decode_device(row) != existing_device:
                            raise ValueError
                        try:
                            updated = connection.execute(
                                """UPDATE operator_registry.devices
                                   SET management_port = ?, protocols_json = ?, tags_json = ?
                                   WHERE site_id = ? AND id = ? AND name = ? AND host = ?
                                     AND management_port = ? AND kind = ?
                                     AND protocols_json = ? AND tags_json = ?""",
                                (
                                    management_port,
                                    registry._protocols_json(source.device),
                                    registry._tags_json(source.device),
                                    self._site_id,
                                    str(device_id),
                                    row["name"],
                                    row["host"],
                                    row["management_port"],
                                    row["kind"],
                                    row["protocols_json"],
                                    row["tags_json"],
                                ),
                            )
                        except sqlite3.IntegrityError as exc:
                            if exc.sqlite_errorname == "SQLITE_CONSTRAINT_UNIQUE":
                                raise DeviceRegistryConflictError from None
                            raise
                        if updated.rowcount != 1:
                            raise ValueError
                        if old_source is None:
                            connection.execute(
                                "INSERT INTO camera_sources VALUES (?, ?, ?)",
                                (str(device_id), _device_json(source.device), source.source_uri),
                            )
                        else:
                            updated = connection.execute(
                                """UPDATE camera_sources SET device_json = ?, source_uri = ?
                                   WHERE device_id = ? AND device_json = ? AND source_uri = ?""",
                                (
                                    _device_json(source.device),
                                    source.source_uri,
                                    str(device_id),
                                    old_source["device_json"],
                                    old_source["source_uri"],
                                ),
                            )
                            if updated.rowcount != 1:
                                raise ValueError
                        self._admit()
                        self._registry_files(path, registry_identity)
                        connection.execute("COMMIT")
                    finally:
                        self._rollback(connection)
                        if attached:
                            try:
                                connection.execute("DETACH DATABASE operator_registry")
                            except sqlite3.Error:
                                self.close()
                                raise
            except DeviceRegistryConflictError:
                failure = DeviceRegistryConflictError
            except OperatorCameraCapacityError:
                failure = OperatorCameraCapacityError
            except Exception:
                failure = OperatorCameraStoreError
        if failure is not None:
            raise failure("Camera endpoint reconnection failed")
        return source

    def remove(self, device_id: UUID) -> None:
        """Remove one source transactionally; absence is an idempotent success."""
        if not isinstance(device_id, UUID):
            raise OperatorCameraStoreError("Camera selection is invalid")
        failed = False
        with self._lock:
            connection = self._admit()
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._read_sources(connection)
                connection.execute(
                    "DELETE FROM camera_sources WHERE device_id = ?", (str(device_id),)
                )
                connection.execute(
                    "DELETE FROM camera_recording WHERE device_id = ?", (str(device_id),)
                )
                connection.execute("COMMIT")
            except Exception:
                failed = True
                self._rollback(connection)
        if failed:
            raise OperatorCameraStoreError("Private camera removal failed")

    def close(self) -> None:
        """Close the owned handle without deleting persistent configuration."""
        with self._lock:
            connection = self._connection
            self._connection = None
            self._closed = True
            if connection is not None:
                connection.close()
