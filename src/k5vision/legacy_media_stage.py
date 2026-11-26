"""Copy or stream retained legacy media without activating or changing its site.

The native controller owns old-user authorization, the service peer and final
activation. Callbacks here are transport contracts, never authentication proofs.
An interrupted partial destination is preserved and refused; only that native
controller may retire its positively owned stage and start another transfer.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path
from uuid import UUID

from k5vision import identity_state
from k5vision import legacy_identity_snapshot as snapshot
from k5vision.operator_component_state import _COMPONENTS
from k5vision.operator_exports import (
    MAX_EXPORT_BYTES,
    MAX_EXPORT_JOBS,
    MAX_RETAINED_BYTES,
    _duration,
    export_start,
)
from k5vision.operator_profile_lock import OperatorProfileLock
from k5vision.operator_recording_storage import _PATH_NAME, MAX_STORAGE_ENTRIES, _segment_name
from k5vision.operator_storage_settings import (
    MAX_QUOTA_BYTES,
    _descriptor_signature,
    _directory,
    _file,
    _signature,
    validate_policy,
)

_MARKER = "media-migration.json"
_STORAGE_MARKER = ".k5-storage-target.json"
_HASH = re.compile(r"[a-f0-9]{64}")
_MAX_SECONDS = 86400
_CHUNK = 1024**2


class LegacyMediaError(RuntimeError):
    """Finite refusal; original media and any interrupted stage stay intact."""


@dataclass(frozen=True, slots=True)
class MediaEntry:
    section: str
    relative: str
    bytes: int
    sha256: str
    modified_ns: int
    accessed_ns: int = field(repr=False)
    signature: tuple[int, ...] = field(repr=False)

    def document(self) -> dict:
        return {
            "section": self.section,
            "relative": self.relative,
            "bytes": self.bytes,
            "sha256": self.sha256,
            "modified_ns": self.modified_ns,
        }


@dataclass(frozen=True, slots=True)
class MediaInventory:
    site_id: str
    transfer_id: str
    source_digest: str
    inventory_sha256: str
    total_bytes: int
    entries: tuple[MediaEntry, ...] = field(repr=False)
    identity: Path = field(repr=False)
    recordings: Path = field(repr=False)
    exports: Path = field(repr=False)
    directories: tuple[tuple[str, str], ...] = field(repr=False)
    binding: str = field(repr=False)
    source_root: os.stat_result = field(repr=False)
    deadline: float = field(repr=False)
    maximum_bytes: int = field(repr=False)
    minimum_free_bytes: int = field(repr=False)

    def manifest(self) -> Iterator[dict]:
        """Bounded entry messages; no absolute source paths or credentials."""
        for index, entry in enumerate(self.entries):
            yield {"index": index, **entry.document()}


def _json_bytes(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _children(path, maximum):
    values = list(islice(path.iterdir(), maximum + 1))
    if len(values) > maximum:
        raise ValueError
    return sorted(values)


def _read_json(path: Path, limit: int) -> dict:
    before = _signature(_file(path))
    if before[2] > limit:
        raise ValueError
    with path.open("rb") as stream:
        opened = _descriptor_signature(stream.fileno(), before)
        raw = stream.read(limit + 1)
        if len(raw) != before[2] or _descriptor_signature(stream.fileno(), before) != opened:
            raise ValueError
    if _signature(_file(path)) != before:
        raise ValueError
    value = json.loads(raw, object_pairs_hook=identity_state._unique_object)
    if type(value) is not dict:
        raise ValueError
    return value


def _authority(check: Callable[[], None]) -> None:
    if not callable(check) or check() is not None:
        raise ValueError


def _checkpoint(identity, root, lease, deadline, authority_check=None):
    if time.monotonic() >= deadline:
        raise TimeoutError
    snapshot._lease(identity, lease, root)
    if authority_check is not None:
        _authority(authority_check)


def _source_digest(identity, checkpoint):
    files, omitted = snapshot._inventory(identity, checkpoint)
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(), files, omitted


def _storage_anchor(connection: sqlite3.Connection, site: str) -> bool:
    tables = connection.execute(
        "SELECT type FROM sqlite_master WHERE name='operator_component_anchors'"
    ).fetchall()
    if not tables:
        return False
    if tables != [("table",)]:
        raise ValueError
    columns = connection.execute("PRAGMA table_info(operator_component_anchors)").fetchall()
    if [(r[1], r[2], r[3], r[5]) for r in columns] != [
        ("site_id", "TEXT", 1, 1),
        ("component", "TEXT", 1, 2),
        ("schema_version", "INTEGER", 1, 0),
    ]:
        raise ValueError
    rows = connection.execute(
        "SELECT site_id,component,schema_version FROM operator_component_anchors LIMIT 7"
    ).fetchall()
    if (
        len(rows) > len(_COMPONENTS)
        or len({r[1] for r in rows}) != len(rows)
        or any(s != site or name not in _COMPONENTS or version != 1 for s, name, version in rows)
    ):
        raise ValueError
    return any(row[1] == "storage" for row in rows)


def _policy(identity: Path):
    manifest = _read_json(identity / "identity.json", 4096)
    site = manifest.get("site_id")
    if (
        set(manifest) != {"schema_version", "site_id"}
        or type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != 1
        or type(site) is not str
    ):
        raise ValueError
    identifier = UUID(hex=site.removeprefix("install-"))
    if identifier.version != 4 or site != "install-" + identifier.hex:
        raise ValueError
    with closing(snapshot._connect(identity / "users.sqlite3", readonly=True)) as connection:
        identity_state._validate_registry(connection, site)
        connection.row_factory = None
        anchored = _storage_anchor(connection, site)
    path = identity / "operator-storage.json"
    if not path.exists() and not path.is_symlink():
        if anchored:
            raise ValueError
        # The legacy installed profile has one fixed default outside identity.
        recordings = identity_state._checked_path(identity.parent / "recordings")
        return site, recordings, 1024**3, "default"
    document = _read_json(path, 16384)
    if (
        set(document)
        != {
            "schema_version",
            "site_id",
            "revision",
            "quota_bytes",
            "minimum_free_bytes",
            "retention_seconds",
            "recording_directory",
            "pending_recording_directory",
            "migration_error",
        }
        or type(document["schema_version"]) is not int
        or document["schema_version"] != 1
        or (
            document["site_id"] != site
            or type(document["revision"]) is not int
            or not 0 <= document["revision"] <= 2**63 - 1
            or document["pending_recording_directory"] is not None
            or document["migration_error"] is not None
        )
    ):
        raise ValueError
    validate_policy(
        document["quota_bytes"], document["minimum_free_bytes"], document["retention_seconds"]
    )
    if (
        type(document["recording_directory"]) is not str
        or len(document["recording_directory"]) > 4096
    ):
        raise ValueError
    recordings = identity_state._checked_path(document["recording_directory"])
    if recordings == identity or recordings in identity.parents or identity in recordings.parents:
        raise ValueError
    _directory(recordings)
    return (
        site,
        recordings,
        document["minimum_free_bytes"],
        hashlib.sha256(_json_bytes(document)).hexdigest(),
    )


def _hash_file(path, signature, checkpoint, *, consume=None, chunk_bytes=_CHUNK):
    if _signature(_file(path)) != signature:
        raise ValueError
    digest, remaining, offset = hashlib.sha256(), signature[2], 0
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        opened = _descriptor_signature(stream.fileno(), signature)
        while block := stream.read(min(chunk_bytes, remaining + 1)):
            checkpoint()
            remaining -= len(block)
            if remaining < 0:
                raise ValueError
            digest.update(block)
            if consume is not None:
                consume(offset, block)
            offset += len(block)
        if remaining or _descriptor_signature(stream.fileno(), signature) != opened:
            raise ValueError
    if _signature(_file(path)) != signature:
        raise ValueError
    return digest.hexdigest()


def _scan(identity, recordings, site, checkpoint, maximum_bytes):
    files, directories, count, total = [], [], 0, 0
    bindings = {
        "recordings_present": recordings.exists(),
        "exports_present": (identity / "exports").exists(),
    }
    if recordings.is_symlink() or (identity / "exports").is_symlink():
        raise ValueError
    if recordings.exists():
        root = _directory(recordings)
        bindings["recordings_identity"] = [root.st_dev, root.st_ino]
        path_ids = []
        for directory in _children(recordings, MAX_STORAGE_ENTRIES):
            checkpoint()
            count += 1
            if count > MAX_STORAGE_ENTRIES:
                raise ValueError
            if directory.name == _STORAGE_MARKER:
                marker = _read_json(directory, 16384)
                if set(marker) != {"schema_version", "site_id", "source_directory"} or (
                    type(marker["schema_version"]) is not int
                    or marker["schema_version"] != 1
                    or marker["site_id"] != site
                    or type(marker["source_directory"]) is not str
                ):
                    raise ValueError
                identity_state._checked_path(marker["source_directory"])
                bindings["storage_marker_sha256"] = _hash_file(
                    directory, _signature(_file(directory)), checkpoint
                )
                continue
            if _PATH_NAME.fullmatch(directory.name) is None:
                raise ValueError
            admitted = _directory(directory)
            directories.append(("recordings", directory.name))
            path_ids.append([directory.name, admitted.st_dev, admitted.st_ino])
            for path in _children(directory, MAX_STORAGE_ENTRIES - count):
                checkpoint()
                count += 1
                if count > MAX_STORAGE_ENTRIES or not _segment_name(path.name):
                    raise ValueError
                info = _file(path)
                total += info.st_size
                if total > maximum_bytes:
                    raise ValueError
                files.append(("recordings", path.relative_to(recordings).as_posix(), path, info))
            identity_state._same_directory(directory, admitted)
        bindings["recording_path_identities"] = path_ids
        identity_state._same_directory(recordings, root)
    exports = identity / "exports"
    if exports.exists():
        root = _directory(exports)
        bindings["exports_identity"] = [root.st_dev, root.st_ino]
        paths = _children(exports, MAX_EXPORT_JOBS * 2)
        index = {path.name: path for path in paths}
        jobs = []
        for path in paths:
            if path.suffix not in {".mp4", ".json"} or str(UUID(path.stem)) != path.stem:
                raise ValueError
            _file(path)
            if path.suffix == ".json":
                jobs.append(path)
        expected, export_bytes = set(), 0
        for path in jobs:
            checkpoint()
            document = _read_json(path, 4096)
            if set(document) != {
                "schema_version",
                "site_id",
                "id",
                "owner",
                "camera",
                "start",
                "duration",
                "created",
                "bytes",
                "sha256",
            }:
                raise ValueError
            if (
                type(document["schema_version"]) is not int
                or document["schema_version"] != 1
                or document["site_id"] != site
            ):
                raise ValueError
            for key in ("id", "owner", "camera"):
                if type(document[key]) is not str or str(UUID(document[key])) != document[key]:
                    raise ValueError
            if (
                document["id"] != path.stem
                or type(document["bytes"]) is not int
                or not 32 <= document["bytes"] <= MAX_EXPORT_BYTES
            ):
                raise ValueError
            if type(document["sha256"]) is not str or _HASH.fullmatch(document["sha256"]) is None:
                raise ValueError
            export_start(document["start"])
            export_start(document["created"])
            _duration(document["duration"])
            media = index.get(path.stem + ".mp4")
            if media is None or _file(media).st_size != document["bytes"]:
                raise ValueError
            expected.update((path.name, media.name))
            export_bytes += document["bytes"]
            if export_bytes > MAX_RETAINED_BYTES:
                raise ValueError
            for current in (path, media):
                info = _file(current)
                total += info.st_size
                if total > maximum_bytes:
                    raise ValueError
                files.append(("exports", current.name, current, info))
        if expected != set(index):
            raise ValueError
        identity_state._same_directory(exports, root)
    result = []
    for section, relative, path, info in sorted(files):
        checkpoint()
        signature = _signature(info)
        digest = _hash_file(path, signature, checkpoint)
        if section == "exports" and path.suffix == ".mp4":
            if _read_json(path.with_suffix(".json"), 4096)["sha256"] != digest:
                raise ValueError
        result.append(
            MediaEntry(
                section,
                relative,
                info.st_size,
                digest,
                info.st_mtime_ns,
                info.st_atime_ns,
                signature,
            )
        )
    bindings["file_identities"] = [[e.section, e.relative, list(e.signature)] for e in result]
    document = {
        "entries": [entry.document() for entry in result],
        "directories": sorted(directories),
        "binding": bindings,
    }
    return (
        tuple(result),
        tuple(sorted(directories)),
        total,
        _json_bytes(bindings).decode(),
        hashlib.sha256(_json_bytes(document)).hexdigest(),
    )


def inspect_media_source(
    identity: Path,
    *,
    lease: OperatorProfileLock,
    transfer_id: str,
    source_digest: str,
    authority_check: Callable[[], None],
    max_seconds: float = 3600,
    max_bytes: int = MAX_QUOTA_BYTES,
) -> MediaInventory:
    failed = False
    try:
        if (
            type(max_seconds) not in (int, float)
            or not math.isfinite(max_seconds)
            or not 1 <= max_seconds <= _MAX_SECONDS
        ):
            raise ValueError
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_QUOTA_BYTES:
            raise ValueError
        if (
            str(UUID(transfer_id)) != transfer_id
            or type(source_digest) is not str
            or _HASH.fullmatch(source_digest) is None
        ):
            raise ValueError
        identity = identity_state._checked_path(identity)
        if identity.name != "identity":
            raise ValueError
        root, deadline = identity.lstat(), time.monotonic() + max_seconds

        def checkpoint():
            return _checkpoint(identity, root, lease, deadline, authority_check)

        checkpoint()
        current, _, _ = _source_digest(identity, checkpoint)
        if current != source_digest:
            raise ValueError
        site, recordings, reserve, policy = _policy(identity)
        if recordings.exists():
            _directory(recordings)
        entries, directories, total, binding, digest = _scan(
            identity, recordings, site, checkpoint, max_bytes
        )
        # No external callback follows the final independent source validation.
        _authority(authority_check)

        def final():
            return _checkpoint(identity, root, lease, deadline)

        if _source_digest(identity, final)[0] != source_digest or _policy(identity) != (
            site,
            recordings,
            reserve,
            policy,
        ):
            raise ValueError
        checked = _scan(identity, recordings, site, final, max_bytes)
        if checked[1:] != (directories, total, binding, digest):
            raise ValueError
        final()
        return MediaInventory(
            site,
            transfer_id,
            source_digest,
            digest,
            total,
            entries,
            identity,
            recordings,
            identity / "exports",
            directories,
            binding,
            root,
            deadline,
            max_bytes,
            reserve,
        )
    except Exception:
        failed = True
    if failed:
        raise LegacyMediaError("Legacy media transfer is unavailable")


def _validate_inventory(inventory, lease, checkpoint):
    if type(inventory) is not MediaInventory:
        raise ValueError
    if _source_digest(inventory.identity, checkpoint)[0] != inventory.source_digest:
        raise ValueError
    site, recordings, reserve, _ = _policy(inventory.identity)
    if (site, recordings, reserve) != (
        inventory.site_id,
        inventory.recordings,
        inventory.minimum_free_bytes,
    ):
        raise ValueError
    current = _scan(inventory.identity, recordings, site, checkpoint, inventory.maximum_bytes)
    if current[1:] != (
        inventory.directories,
        inventory.total_bytes,
        inventory.binding,
        inventory.inventory_sha256,
    ):
        raise ValueError
    if tuple(entry.document() for entry in current[0]) != tuple(
        entry.document() for entry in inventory.entries
    ):
        raise ValueError
    snapshot._lease(inventory.identity, lease, inventory.source_root)


def _entry_source(inventory, entry):
    return (
        inventory.recordings if entry.section == "recordings" else inventory.exports
    ) / entry.relative


def _summary(inventory, phase):
    return {
        "schema_version": 1,
        "site_id": inventory.site_id,
        "transfer_id": inventory.transfer_id,
        "source_digest": inventory.source_digest,
        "inventory_sha256": inventory.inventory_sha256,
        "file_count": len(inventory.entries),
        "total_bytes": inventory.total_bytes,
        "phase": phase,
        "activation_allowed": False,
    }


def stream_media_source(
    inventory: MediaInventory,
    *,
    lease: OperatorProfileLock,
    authority_check: Callable[[], None],
    emit: Callable[[dict, bytes], None],
    chunk_bytes: int = 16384,
) -> dict:
    failed = False
    try:
        if type(chunk_bytes) is not int or not 1 <= chunk_bytes <= 16384 or not callable(emit):
            raise ValueError

        def check():
            return _checkpoint(
                inventory.identity,
                inventory.source_root,
                lease,
                inventory.deadline,
                authority_check,
            )

        _validate_inventory(inventory, lease, check)
        for index, entry in enumerate(inventory.entries):

            def consume(offset, block, index=index, entry=entry):
                if (
                    emit(
                        {"index": index, "offset": offset, "eof": False, **entry.document()}, block
                    )
                    is not None
                ):
                    raise ValueError

            if (
                _hash_file(
                    _entry_source(inventory, entry),
                    entry.signature,
                    check,
                    consume=consume,
                    chunk_bytes=chunk_bytes,
                )
                != entry.sha256
            ):
                raise ValueError
            check()
            if (
                emit({"index": index, "offset": entry.bytes, "eof": True, **entry.document()}, b"")
                is not None
            ):
                raise ValueError
        _authority(authority_check)

        def final():
            return _checkpoint(inventory.identity, inventory.source_root, lease, inventory.deadline)

        _validate_inventory(inventory, lease, final)
        return _summary(inventory, "media_streamed")
    except Exception:
        failed = True
    if failed:
        raise LegacyMediaError("Legacy media transfer is unavailable")


def copy_media_source(
    inventory: MediaInventory,
    *,
    lease: OperatorProfileLock,
    authority_check: Callable[[], None],
    copy_entry: Callable[[Path, dict], None],
) -> dict:
    """Dispatch admitted files to the native controller without duplicating TB.

    The callback receives private paths only inside the authenticated broker;
    it must never place them in arguments, environment, logs or UI. Its native
    copy must impose its own deadline/cancellation and verify the target hash.
    This receipt proves source admission/preservation, never target activation.
    """
    failed = False
    try:
        if not callable(copy_entry):
            raise ValueError

        def check():
            return _checkpoint(
                inventory.identity,
                inventory.source_root,
                lease,
                inventory.deadline,
                authority_check,
            )

        _validate_inventory(inventory, lease, check)
        for index, entry in enumerate(inventory.entries):
            source = _entry_source(inventory, entry)
            if _hash_file(source, entry.signature, check) != entry.sha256:
                raise ValueError
            check()
            if copy_entry(source, {"index": index, **entry.document()}) is not None:
                raise ValueError
            check()
        _authority(authority_check)

        def final():
            return _checkpoint(inventory.identity, inventory.source_root, lease, inventory.deadline)

        _validate_inventory(inventory, lease, final)
        return _summary(inventory, "media_copy_dispatched")
    except Exception:
        failed = True
    if failed:
        raise LegacyMediaError("Legacy media transfer is unavailable")


def _destination_entries(destination, inventory, checkpoint):
    expected = {(entry.section, entry.relative): entry for entry in inventory.entries}
    allowed = {"recordings", "exports", _MARKER}
    if {p.name for p in _children(destination, len(allowed))} - allowed:
        raise ValueError
    observed = {}
    for section in ("recordings", "exports"):
        root = destination / section
        if not root.exists():
            continue
        root_id = _directory(root)
        paths, child_ids = [], []
        limit = (
            len(inventory.directories)
            if section == "recordings"
            else sum(e.section == section for e in inventory.entries)
        )
        for path in _children(root, limit):
            checkpoint()
            if section == "recordings":
                if (section, path.name) not in inventory.directories:
                    raise ValueError
                child_id = _directory(path)
                file_limit = sum(
                    e.section == section and e.relative.startswith(path.name + "/")
                    for e in inventory.entries
                )
                paths.extend(_children(path, file_limit))
                child_ids.append((path, child_id))
            else:
                paths.append(path)
        for path in paths:
            relative = path.relative_to(root).as_posix()
            entry = expected.get((section, relative))
            if entry is None or _file(path).st_mtime_ns != entry.modified_ns:
                raise ValueError
            if _hash_file(path, _signature(_file(path)), checkpoint) != entry.sha256:
                raise ValueError
            observed[(section, relative)] = _signature(_file(path))
        for path, child_id in child_ids:
            identity_state._same_directory(path, child_id)
        identity_state._same_directory(root, root_id)
    return observed


def prepare_media_stage(
    inventory: MediaInventory,
    destination: Path,
    *,
    lease: OperatorProfileLock,
    authority_check: Callable[[], None],
) -> dict:
    failed = False
    try:

        def check():
            return _checkpoint(
                inventory.identity,
                inventory.source_root,
                lease,
                inventory.deadline,
                authority_check,
            )

        _validate_inventory(inventory, lease, check)
        destination = identity_state._checked_path(destination)
        for protected in (inventory.identity, inventory.recordings, inventory.exports):
            if (
                destination == protected
                or destination in protected.parents
                or protected in destination.parents
            ):
                raise ValueError
        created = False
        if destination.exists() or destination.is_symlink():
            target_root = _directory(destination)
        else:
            _directory(destination.parent)
            destination.mkdir(mode=0o700)
            created = True
            target_root = _directory(destination)
        marker = {
            **_summary(inventory, "media_stage"),
            "target_identity": [target_root.st_dev, target_root.st_ino],
        }
        marker_path = destination / _MARKER
        if marker_path.exists() or marker_path.is_symlink():
            if _read_json(marker_path, 16384) != marker:
                raise ValueError
        else:
            if not created or next(destination.iterdir(), None) is not None:
                raise ValueError
            descriptor = os.open(marker_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(_json_bytes(marker))
                stream.flush()
                os.fsync(stream.fileno())
        marker_identity = _signature(_file(marker_path))
        marker_hash = _hash_file(marker_path, _signature(_file(marker_path)), check)
        existing = _destination_entries(destination, inventory, check)
        needed = sum(
            entry.bytes
            for entry in inventory.entries
            if (entry.section, entry.relative) not in existing
        )
        free = shutil.disk_usage(destination).free
        if type(free) is not int or free < needed + inventory.minimum_free_bytes:
            raise ValueError
        for section in ("recordings", "exports"):
            if not (destination / section).exists():
                (destination / section).mkdir(mode=0o700)
            _directory(destination / section)
        for section, name in inventory.directories:
            path = destination / section / name
            if not path.exists():
                path.mkdir(mode=0o700)
            _directory(path)
        for entry in inventory.entries:
            check()
            identity_state._same_directory(destination, target_root)
            target = destination / entry.section / entry.relative
            if (entry.section, entry.relative) in existing:
                continue
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                if (
                    _hash_file(
                        _entry_source(inventory, entry),
                        entry.signature,
                        check,
                        consume=lambda _offset, block: stream.write(block),
                    )
                    != entry.sha256
                ):
                    raise ValueError
                stream.flush()
                os.fsync(stream.fileno())
            os.utime(target, ns=(entry.accessed_ns, entry.modified_ns))
            existing[(entry.section, entry.relative)] = _signature(_file(target))
        _authority(authority_check)

        def final():
            return _checkpoint(inventory.identity, inventory.source_root, lease, inventory.deadline)

        _validate_inventory(inventory, lease, final)
        observed = _destination_entries(destination, inventory, final)
        if set(observed) != {(e.section, e.relative) for e in inventory.entries}:
            raise ValueError
        if observed != existing:
            raise ValueError
        if _hash_file(marker_path, _signature(_file(marker_path)), final) != marker_hash:
            raise ValueError
        if _signature(_file(marker_path)) != marker_identity:
            raise ValueError
        identity_state._same_directory(destination, target_root)
        final()
        return _summary(inventory, "media_prepared")
    except Exception:
        failed = True
    if failed:
        raise LegacyMediaError("Legacy media transfer is unavailable")
