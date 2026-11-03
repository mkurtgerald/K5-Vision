"""Bounded maintenance of owned MediaMTX recording segments.

Age retention remains MediaMTX's responsibility. This boundary enforces a byte
budget and free-space admission without traversing or deleting unrelated files.
Call it from the application's single maintenance worker, outside the event loop.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import stat
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from k5vision import identity_state

MAX_STORAGE_ENTRIES = 100_000
MAX_STORAGE_SECONDS = 5.0
MIN_CLOSED_AGE_SECONDS = 10.0
_PATH_NAME = re.compile(r"k5_[0-9a-f]{64}")
_SEGMENT_NAME = re.compile(
    r"(?P<stamp>[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{6})"
    r"(?P<offset>_(?:Z|[+-][0-9]{4}))?\.mp4"
)


class RecordingStorageError(RuntimeError):
    """The configured recording root cannot be admitted safely."""


@dataclass(frozen=True, slots=True)
class StorageStatus:
    total_bytes: int
    free_bytes: int
    recording_allowed: bool
    deleted_segments: int = 0


@dataclass(frozen=True, slots=True)
class _Segment:
    path: Path = field(repr=False)
    directory: Path = field(repr=False)
    directory_identity: os.stat_result = field(repr=False)
    identity: os.stat_result = field(repr=False)


@dataclass(frozen=True, slots=True)
class _Scan:
    total_bytes: int
    free_bytes: int
    candidates: tuple[_Segment, ...] = field(repr=False)
    complete: bool


class _IncompleteStorage(Exception):
    pass


def _budget(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise _IncompleteStorage


def _segment_name(name: str) -> bool:
    match = _SEGMENT_NAME.fullmatch(name)
    if match is None:
        return False
    stamp = match["stamp"]
    offset = match["offset"]
    if offset is None:
        datetime.strptime(stamp, "%Y-%m-%d_%H-%M-%S-%f")
    else:
        datetime.strptime(stamp + offset, "%Y-%m-%d_%H-%M-%S-%f_%z")
    return True


def _file_info(path: Path) -> os.stat_result:
    # The parent was admitted independently. lstat never follows the leaf;
    # Windows admission also checks the actual owner/DACL before use.
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or getattr(info, "st_file_attributes", 0) & 0x400
        or info.st_size < 0
        or not math.isfinite(info.st_mtime)
    ):
        raise _IncompleteStorage
    if os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o022):
        raise _IncompleteStorage
    identity_state._windows_admission(path, private=True)
    return info


def _free_bytes(root: Path) -> int:
    free = shutil.disk_usage(root).free
    if type(free) is not int or free < 0:
        raise _IncompleteStorage
    return free


class RecordingStorage:
    """Inspect and prune only closed segments in an admitted private root."""

    def __init__(
        self,
        root: Path,
        *,
        max_bytes: int = 20 * 1024**3,
        min_free_bytes: int = 1024**3,
    ) -> None:
        if (
            type(max_bytes) is not int
            or not 1 <= max_bytes <= 2**63 - 1
            or type(min_free_bytes) is not int
            or not 0 <= min_free_bytes <= 2**63 - 1
        ):
            raise ValueError("Recording storage byte bounds are invalid")
        failed = False
        try:
            if not isinstance(root, Path):
                raise ValueError
            self._root = identity_state._checked_path(root)
            self._root_identity = self._root.lstat()
            self._admit_root()
        except Exception:
            failed = True
        if failed:
            raise RecordingStorageError("Recording storage root admission failed")
        self._max_bytes = max_bytes
        self._min_free_bytes = min_free_bytes
        self._lock = threading.RLock()

    def _admit_root(self) -> None:
        identity_state._same_directory(self._root, self._root_identity)
        info = self._root.lstat()
        if os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise _IncompleteStorage

    @staticmethod
    def _directory(path: Path) -> os.stat_result:
        identity_state._checked_path(path)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise _IncompleteStorage
        identity_state._windows_admission(path, private=True, directory=True)
        return info

    def _scan(self, deadline: float, remaining_entries: list[int]) -> _Scan:
        total = 0
        free = 0
        candidates = []
        try:
            _budget(deadline)
            self._admit_root()
            free = _free_bytes(self._root)
            now = time.time()
            with os.scandir(self._root) as directories:
                for entry in directories:
                    _budget(deadline)
                    remaining_entries[0] -= 1
                    if remaining_entries[0] < 0:
                        raise _IncompleteStorage
                    if _PATH_NAME.fullmatch(entry.name) is None:
                        continue
                    directory = self._root / entry.name
                    directory_identity = self._directory(directory)
                    segments = []
                    with os.scandir(directory) as files:
                        for file in files:
                            _budget(deadline)
                            remaining_entries[0] -= 1
                            if remaining_entries[0] < 0:
                                raise _IncompleteStorage
                            if not _segment_name(file.name):
                                continue
                            path = directory / file.name
                            info = _file_info(path)
                            total += info.st_size
                            segments.append(_Segment(path, directory, directory_identity, info))
                    identity_state._same_directory(directory, directory_identity)
                    if segments:
                        newest = max(
                            segments,
                            key=lambda segment: (segment.identity.st_mtime_ns, segment.path.name),
                        )
                        candidates.extend(
                            segment
                            for segment in segments
                            if segment is not newest
                            and now - segment.identity.st_mtime >= MIN_CLOSED_AGE_SECONDS
                        )
            self._admit_root()
            _budget(deadline)
            ordered = tuple(
                sorted(
                    candidates,
                    key=lambda segment: (segment.identity.st_mtime_ns, str(segment.path)),
                )
            )
            _budget(deadline)
            return _Scan(total, free, ordered, True)
        except Exception:
            return _Scan(total, free, (), False)

    def _status(self, scan: _Scan, deleted: int = 0) -> StorageStatus:
        return StorageStatus(
            total_bytes=scan.total_bytes,
            free_bytes=scan.free_bytes,
            recording_allowed=(
                scan.complete
                and scan.total_bytes <= self._max_bytes
                and scan.free_bytes >= self._min_free_bytes
            ),
            deleted_segments=deleted,
        )

    def inspect(self) -> StorageStatus:
        """Report bounded, source-free recording-space admission without deleting."""
        deadline = time.monotonic() + MAX_STORAGE_SECONDS
        with self._lock:
            return self._status(self._scan(deadline, [MAX_STORAGE_ENTRIES]))

    def prune(self) -> StorageStatus:
        """Free owned closed segments oldest-first, protecting each path's newest.

        A complete scan is required before any deletion. After an over-quota scan,
        prune toward 90% of the configured byte budget to reduce repeated churn.
        Low free space can also retire closed owned segments. Failed/shared-open
        deletions are skipped; the final admission reflects the actual filesystem.
        """
        deadline = time.monotonic() + MAX_STORAGE_SECONDS
        with self._lock:
            remaining_entries = [MAX_STORAGE_ENTRIES]
            scan = self._scan(deadline, remaining_entries)
            if not scan.complete or self._status(scan).recording_allowed:
                return self._status(scan)
            target = (
                self._max_bytes * 9 // 10 if scan.total_bytes > self._max_bytes else self._max_bytes
            )
            total = scan.total_bytes
            free = scan.free_bytes
            deleted = 0
            for segment in scan.candidates:
                if total <= target and free >= self._min_free_bytes:
                    break
                try:
                    _budget(deadline)
                    self._admit_root()
                    identity_state._same_directory(segment.directory, segment.directory_identity)
                    current = _file_info(segment.path)
                    if (
                        current.st_dev,
                        current.st_ino,
                        current.st_size,
                        current.st_mtime_ns,
                    ) != (
                        segment.identity.st_dev,
                        segment.identity.st_ino,
                        segment.identity.st_size,
                        segment.identity.st_mtime_ns,
                    ):
                        continue
                    if time.time() - current.st_mtime < MIN_CLOSED_AGE_SECONDS:
                        continue
                    _budget(deadline)
                    segment.path.unlink()
                except OSError:
                    # Windows sharing violations and independent age-retention
                    # removals are ordinary races; do not delete another target.
                    continue
                except Exception:
                    return StorageStatus(total, free, False, deleted)
                deleted += 1
                total -= current.st_size
                try:
                    free = _free_bytes(self._root)
                except Exception:
                    return StorageStatus(total, free, False, deleted)
            return self._status(self._scan(deadline, remaining_entries), deleted)
