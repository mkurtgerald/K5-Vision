"""Private storage policy and copy-first recording-directory migration.

Policy updates never infer file age from wall clocks. Directory migration must
run with the recorder stopped and the application's exclusive profile lease
held. It copies and verifies retained recording bytes before changing the active
directory; original files are never removed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import threading
import time
from collections.abc import Callable
from pathlib import Path

from k5vision import identity_state
from k5vision.operator_component_state import ComponentStateAnchor
from k5vision.operator_recording_storage import (
    _PATH_NAME,
    MAX_STORAGE_ENTRIES,
    _file_info,
    _segment_name,
)

_FILENAME = "operator-storage.json"
_TARGET_MARKER = ".k5-storage-target.json"
_LIMIT = 16 * 1024
MAX_QUOTA_BYTES = 4096 * 1024**3
MAX_RETENTION_SECONDS = 30 * 86400
_MIGRATION_ERRORS = frozenset(
    {
        "migration_incomplete",
        "directory_refused",
        "destination_conflict",
        "source_changed",
        "interrupted_copy_preserved",
    }
)


class StorageSettingsError(RuntimeError):
    """Finite storage-settings failure, without paths or private values."""


class _MigrationSourceChanged(StorageSettingsError):
    pass


class _InterruptedCopyPreserved(StorageSettingsError):
    pass


class StorageSettingsConflict(StorageSettingsError):
    pass


class StorageDirectoryError(StorageSettingsError):
    pass


def validate_policy(quota_bytes: int, minimum_free_bytes: int, retention_seconds: int) -> None:
    if (
        type(quota_bytes) is not int
        or not 1 <= quota_bytes <= MAX_QUOTA_BYTES
        or type(minimum_free_bytes) is not int
        or not 0 <= minimum_free_bytes <= MAX_QUOTA_BYTES
        or type(retention_seconds) is not int
        or not 0 <= retention_seconds <= MAX_RETENTION_SECONDS
    ):
        raise ValueError("Storage policy was refused")


def _directory(path: Path) -> os.stat_result:
    identity_state._checked_path(path)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise StorageDirectoryError("Storage directory was refused")
    if os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
        raise StorageDirectoryError("Storage directory was refused")
    identity_state._windows_admission(path, private=True, directory=True)
    return info


def _file(path: Path) -> os.stat_result:
    info = identity_state._regular_file(path)
    if os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
        raise StorageSettingsError("Storage file was refused")
    return info


def _signature(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read(path: Path) -> dict:
    before = _file(path)
    if not 0 < before.st_size <= _LIMIT:
        raise StorageSettingsError("Storage settings were refused")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        if _signature(os.fstat(stream.fileno())) != _signature(before):
            raise StorageSettingsError("Storage settings changed")
        payload = stream.read(_LIMIT + 1)
        if len(payload) != before.st_size or _signature(os.fstat(stream.fileno())) != _signature(
            before
        ):
            raise StorageSettingsError("Storage settings changed")
    if _signature(_file(path)) != _signature(before):
        raise StorageSettingsError("Storage settings changed")
    document = json.loads(payload.decode("utf-8"), object_pairs_hook=identity_state._unique_object)
    if type(document) is not dict:
        raise StorageSettingsError("Storage settings were refused")
    return document


def _atomic(path: Path, document: dict) -> None:
    parent_identity = _directory(path.parent)
    previous = _signature(_file(path)) if path.exists() else None
    payload = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(payload) > _LIMIT:
        raise StorageSettingsError("Storage settings were refused")
    temporary = path.parent / (".k5-migrate-" + secrets.token_hex(16) + ".tmp")
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _file(temporary)
        identity_state._same_directory(path.parent, parent_identity)
        if (_signature(_file(path)) if path.exists() else None) != previous:
            raise StorageSettingsConflict("Storage settings changed")
        os.replace(temporary, path)
        _file(path)
    finally:
        temporary.unlink(missing_ok=True)


class OperatorStorageSettings:
    """A bounded, site-bound policy, written only under the profile lease."""

    def __init__(
        self,
        root: Path,
        site_id: str,
        recording_directory: Path,
        *,
        quota_bytes: int = 20 * 1024**3,
    ) -> None:
        self._lock = threading.RLock()
        try:
            if type(site_id) is not str or not 1 <= len(site_id) <= 128:
                raise ValueError
            validate_policy(quota_bytes, 1024**3, 86400)
            self._root = identity_state._checked_path(root)
            self._root_identity = _directory(self._root)
            active = identity_state._checked_path(recording_directory)
            _directory(active)
            self._path = self._root / _FILENAME
            self._site_id = site_id
            self._anchor = ComponentStateAnchor(
                self._root,
                site_id,
                "storage",
                present=self._path.exists() or self._path.is_symlink(),
            )
            self._document = {
                "schema_version": 1,
                "site_id": site_id,
                "revision": 0,
                "quota_bytes": quota_bytes,
                "minimum_free_bytes": 1024**3,
                "retention_seconds": 86400,
                "recording_directory": str(active),
                "pending_recording_directory": None,
                "migration_error": None,
            }
            self._identity = None
            if self._path.exists():
                document = _read(self._path)
                self._validate(document)
                self._document = document
                self._identity = _signature(_file(self._path))
                self._anchor.seal()
        except Exception:
            raise StorageSettingsError("Storage settings admission failed") from None

    def _validate(self, value: dict) -> None:
        if (
            set(value) != set(self._document)
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or value["site_id"] != self._site_id
            or type(value["revision"]) is not int
            or not 0 <= value["revision"] <= 2**63 - 1
            or value["migration_error"] not in _MIGRATION_ERRORS | {None}
        ):
            raise ValueError
        validate_policy(
            value["quota_bytes"], value["minimum_free_bytes"], value["retention_seconds"]
        )
        active = identity_state._checked_path(value["recording_directory"])
        _directory(active)
        if value["pending_recording_directory"] is not None:
            self._target(value["pending_recording_directory"], active)

    def _admit(self) -> None:
        try:
            identity_state._same_directory(self._root, self._root_identity)
            if self._identity is None:
                if self._path.exists():
                    raise StorageSettingsConflict("Storage settings changed")
            elif _signature(_file(self._path)) != self._identity:
                raise StorageSettingsConflict("Storage settings changed")
        except StorageSettingsError:
            raise
        except Exception:
            raise StorageSettingsError("Storage settings admission failed") from None

    def _save(self, document: dict) -> None:
        self._admit()
        self._validate(document)
        _atomic(self._path, document)
        self._identity = _signature(_file(self._path))
        self._anchor.seal()
        self._document = document

    def _target(self, value: str, active: Path) -> Path:
        if type(value) is not str or not value or len(value) > 1024 or "\x00" in value:
            raise StorageDirectoryError("Storage directory was refused")
        try:
            target = identity_state._checked_path(value)
            if (
                target == self._root
                or target in self._root.parents
                or target == active
                or target in active.parents
                or active in target.parents
            ):
                raise ValueError
            if target.exists():
                _directory(target)
            else:
                _directory(target.parent)
            return target
        except Exception:
            raise StorageDirectoryError("Storage directory was refused") from None

    def _prepare_target(self, value: str, active: Path) -> Path:
        try:
            return self._target(value, active)
        except StorageDirectoryError:
            from k5vision.operator_storage_directory import (
                StorageContainerError,
                prepare_private_recording_container,
            )

            try:
                prepared = prepare_private_recording_container(
                    value, self._site_id, protected=(self._root, active)
                )
                if prepared is None:
                    raise StorageContainerError("Directory failure was not a private-parent case")
                return self._target(str(prepared), active)
            except StorageContainerError:
                raise StorageDirectoryError("Storage directory was refused") from None

    def snapshot(self) -> dict:
        with self._lock:
            self._admit()
            value = {
                key: item
                for key, item in self._document.items()
                if key not in {"site_id", "schema_version"}
            }
            pending = value["pending_recording_directory"]
            value.update(
                location_status="active"
                if pending is None
                else "migration_required"
                if value["migration_error"]
                else "restart_required",
                retention_basis="elapsed_stable_time",
                old_media_preserved=True,
            )
            return value

    def configure(self, *, expected_revision: int, **changes) -> dict:
        allowed = {"quota_bytes", "minimum_free_bytes", "retention_seconds", "recording_directory"}
        with self._lock:
            self._admit()
            if (
                type(expected_revision) is not int
                or expected_revision != self._document["revision"]
            ):
                raise StorageSettingsConflict("Storage settings changed")
            if (
                not changes
                or not set(changes) <= allowed
                or any(value is None for value in changes.values())
            ):
                raise ValueError("Storage policy was refused")
            document = dict(self._document)
            validate_policy(
                changes.get("quota_bytes", document["quota_bytes"]),
                changes.get("minimum_free_bytes", document["minimum_free_bytes"]),
                changes.get("retention_seconds", document["retention_seconds"]),
            )
            if "minimum_free_bytes" in changes:
                capacity = shutil.disk_usage(Path(document["recording_directory"])).total
                if type(capacity) is not int or changes["minimum_free_bytes"] > capacity:
                    raise ValueError("Storage reserve exceeds volume capacity")
            for key, value in changes.items():
                if key == "recording_directory":
                    if value == "" or value == document["recording_directory"]:
                        document["pending_recording_directory"] = None
                    else:
                        target = self._prepare_target(value, Path(document["recording_directory"]))
                        document["pending_recording_directory"] = str(target)
                    document["migration_error"] = None
                else:
                    document[key] = value
            document["revision"] += 1
            self._save(document)
            return self.snapshot()

    @property
    def recording_directory(self) -> Path:
        return Path(self.snapshot()["recording_directory"])

    def _inventory(
        self, root: Path, deadline: float, *, detect_copy_temps: bool = False
    ) -> dict[Path, tuple[int, ...]]:
        root_identity = _directory(root)
        result = {}
        entries = 0
        with os.scandir(root) as directories:
            for directory in directories:
                if time.monotonic() >= deadline:
                    raise TimeoutError
                entries += 1
                if entries > MAX_STORAGE_ENTRIES:
                    raise ValueError
                if _PATH_NAME.fullmatch(directory.name) is None:
                    continue
                path = root / directory.name
                directory_identity = _directory(path)
                with os.scandir(path) as files:
                    for file in files:
                        entries += 1
                        if entries > MAX_STORAGE_ENTRIES or time.monotonic() >= deadline:
                            raise TimeoutError
                        if detect_copy_temps and re.fullmatch(
                            r"\.k5-migrate-[0-9a-f]{32}\.tmp", file.name
                        ):
                            # A process kill can skip finally cleanup. A name
                            # pattern alone cannot prove that an existing file
                            # is ours, so preserve it and keep the old root active.
                            raise _InterruptedCopyPreserved("Interrupted copy was preserved")
                        if not _segment_name(file.name):
                            continue
                        full = path / file.name
                        result[full.relative_to(root)] = _signature(_file_info(full))
                identity_state._same_directory(path, directory_identity)
        identity_state._same_directory(root, root_identity)
        return result

    @staticmethod
    def _hash(path: Path, expected: tuple[int, ...], deadline: float) -> str:
        digest = hashlib.sha256()
        if _signature(_file_info(path)) != expected:
            raise _MigrationSourceChanged("Recording changed during migration")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            if _signature(os.fstat(stream.fileno())) != expected:
                raise _MigrationSourceChanged("Recording changed during migration")
            while chunk := stream.read(1024 * 1024):
                if time.monotonic() >= deadline:
                    raise TimeoutError
                digest.update(chunk)
            if _signature(os.fstat(stream.fileno())) != expected:
                raise _MigrationSourceChanged("Recording changed during migration")
        if _signature(_file_info(path)) != expected:
            raise _MigrationSourceChanged("Recording changed during migration")
        return digest.hexdigest()

    def migrate_pending(
        self,
        *,
        max_seconds: float = 300,
        progress: Callable[[dict], None] | None = None,
    ) -> Path:
        """Called only with stopped recorder and exclusive profile lease held.

        A bounded failed attempt keeps the old directory active. Verified copies
        can be reused on a later attempt; original files are always preserved.
        The caller must show the returned directory as active, not the requested
        target. Ordinary restart never requires terminal commands or file moves.
        """
        if type(max_seconds) not in (int, float) or not 1 <= max_seconds <= 3600:
            raise ValueError("Storage migration bound was refused")
        with self._lock:
            self._admit()
            document = dict(self._document)
            source = Path(document["recording_directory"])
            if document["pending_recording_directory"] is None:
                return source
            target = None
            error = "migration_incomplete"
            deadline = time.monotonic() + max_seconds
            try:
                if progress is not None:
                    progress({"phase": "preparing", "copied_bytes": 0, "total_bytes": 0})
                source_identity = _directory(source)
                target = self._target(document["pending_recording_directory"], source)
                if not target.exists():
                    target.mkdir(mode=0o700)
                target_identity = _directory(target)
                capacity = shutil.disk_usage(target).total
                if type(capacity) is not int or document["minimum_free_bytes"] > capacity:
                    error = "directory_refused"
                    raise ValueError
                marker = target / _TARGET_MARKER
                marker_value = {
                    "schema_version": 1,
                    "site_id": self._site_id,
                    "source_directory": str(source),
                }
                if marker.exists():
                    if _read(marker) != marker_value:
                        error = "destination_conflict"
                        raise ValueError
                else:
                    with os.scandir(target) as files:
                        if next(files, None) is not None:
                            error = "destination_conflict"
                            raise ValueError
                    _atomic(marker, marker_value)
                inventory = self._inventory(source, deadline)
                existing = self._inventory(target, deadline, detect_copy_temps=True)
                if not set(existing) <= set(inventory):
                    error = "destination_conflict"
                    raise ValueError
                total = sum(value[2] for value in inventory.values())
                done = 0
                verified_destinations = {}
                for relative, signature in inventory.items():
                    if time.monotonic() >= deadline:
                        raise TimeoutError
                    original, destination = source / relative, target / relative
                    source_hash = self._hash(original, signature, deadline)
                    if destination.exists():
                        copied_signature = _signature(_file_info(destination))
                        if (
                            copied_signature[2] != signature[2]
                            or self._hash(destination, copied_signature, deadline) != source_hash
                        ):
                            error = "destination_conflict"
                            raise ValueError
                    else:
                        if not destination.parent.exists():
                            destination.parent.mkdir(mode=0o700)
                        parent_identity = _directory(destination.parent)
                        temporary = destination.parent / (
                            ".k5-migrate-" + secrets.token_hex(16) + ".tmp"
                        )
                        try:
                            with (
                                os.fdopen(
                                    os.open(original, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)),
                                    "rb",
                                ) as input_file,
                                os.fdopen(
                                    os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600),
                                    "wb",
                                ) as output,
                            ):
                                if _signature(os.fstat(input_file.fileno())) != signature:
                                    error = "source_changed"
                                    raise ValueError
                                while chunk := input_file.read(1024 * 1024):
                                    if time.monotonic() >= deadline:
                                        raise TimeoutError
                                    output.write(chunk)
                                output.flush()
                                os.fsync(output.fileno())
                                if _signature(os.fstat(input_file.fileno())) != signature:
                                    error = "source_changed"
                                    raise ValueError
                            copied_signature = _signature(_file(temporary))
                            if (
                                copied_signature[2] != signature[2]
                                or self._hash(temporary, copied_signature, deadline) != source_hash
                            ):
                                raise ValueError
                            identity_state._same_directory(destination.parent, parent_identity)
                            if destination.exists():
                                error = "destination_conflict"
                                raise ValueError
                            # All writers are excluded by the profile lease and
                            # private target ownership. Never overwrite a file.
                            os.link(temporary, destination)
                            temporary.unlink()
                        finally:
                            temporary.unlink(missing_ok=True)
                    verified_destinations[relative] = _signature(_file_info(destination))
                    done += signature[2]
                    if progress is not None:
                        progress({"phase": "copying", "copied_bytes": done, "total_bytes": total})
                    identity_state._same_directory(target, target_identity)
                if self._inventory(source, deadline) != inventory:
                    error = "source_changed"
                    raise ValueError
                if (
                    self._inventory(target, deadline, detect_copy_temps=True)
                    != verified_destinations
                ):
                    error = "destination_conflict"
                    raise ValueError
                identity_state._same_directory(source, source_identity)
                identity_state._same_directory(target, target_identity)
                activated = dict(
                    document,
                    recording_directory=str(target),
                    pending_recording_directory=None,
                    migration_error=None,
                    revision=document["revision"] + 1,
                )
                self._save(activated)
                return target
            except Exception as failure:
                if isinstance(failure, _MigrationSourceChanged):
                    error = "source_changed"
                elif isinstance(failure, _InterruptedCopyPreserved):
                    error = "interrupted_copy_preserved"
                elif isinstance(failure, StorageDirectoryError):
                    error = "directory_refused"
                document.update(migration_error=error, revision=document["revision"] + 1)
                self._save(document)
                return source


def prepare_operator_recording_storage(
    *,
    state_root: Path,
    site_id: str,
    default_recording_directory: Path,
    quota_bytes: int = 20 * 1024**3,
    max_seconds: float = 300,
    progress: Callable[[dict], None] | None = None,
) -> dict:
    """Restart hook, under the exclusive profile lease and before service creation."""
    default = identity_state._checked_path(default_recording_directory)
    if not default.exists():
        parent_identity = _directory(default.parent)
        default.mkdir(mode=0o700)
        identity_state._same_directory(default.parent, parent_identity)
    settings = OperatorStorageSettings(state_root, site_id, default, quota_bytes=quota_bytes)
    settings.migrate_pending(max_seconds=max_seconds, progress=progress)
    return settings.snapshot()
