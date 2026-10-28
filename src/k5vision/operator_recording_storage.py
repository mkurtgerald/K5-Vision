"""Bounded maintenance of owned MediaMTX recording segments.

This boundary enforces byte/free-space limits and ages unchanged closed files
using monotonic observations. Filenames and wall-clock ages never authorize a
deletion. A cold restart conservatively restarts age observation. Call it from
the application's single maintenance worker, outside the event loop.
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
MAX_PROGRESS_PATHS = 64
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
    stable_since: float


@dataclass(frozen=True, slots=True)
class _Observation:
    signature: tuple[int, ...]
    stable_since: float
    change_generation: int


@dataclass(frozen=True, slots=True)
class _PathProgress:
    directory_identity: tuple[int, int] | None = None
    total_bytes: int = 0
    last_progress: float | None = None
    files: dict[str, tuple[int, ...]] = field(default_factory=dict, repr=False)


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
        retention_seconds: int = 86_400,
    ) -> None:
        if (
            type(max_bytes) is not int
            or not 1 <= max_bytes <= 2**63 - 1
            or type(min_free_bytes) is not int
            or not 0 <= min_free_bytes <= 2**63 - 1
            or type(retention_seconds) is not int
            or not 0 <= retention_seconds <= 30 * 24 * 60 * 60
        ):
            raise ValueError("Recording storage bounds are invalid")
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
        self._retention_seconds = retention_seconds
        self._observations: dict[Path, _Observation] = {}
        self._scan_generation = 0
        self._progress: dict[str, _PathProgress] = {}
        self._progress_complete = False
        self._progress_lock = threading.Lock()
        self._lock = threading.RLock()

    @staticmethod
    def _signature(directory: os.stat_result, file: os.stat_result) -> tuple[int, ...]:
        return (
            directory.st_dev,
            directory.st_ino,
            file.st_dev,
            file.st_ino,
            file.st_size,
            file.st_mtime_ns,
            file.st_ctime_ns,
        )

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
        observed: dict[Path, _Observation] = {}
        # Keep queried paths ahead of historic removed-camera directories. The
        # same entry/time budgets bound both retention and progress observations.
        with self._progress_lock:
            previous_progress_by_path = dict(self._progress)
            previous_progress_complete = self._progress_complete
        progress = {name: _PathProgress() for name in previous_progress_by_path}
        generation = self._scan_generation + 1
        try:
            _budget(deadline)
            self._admit_root()
            free = _free_bytes(self._root)
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
                    identity = (directory_identity.st_dev, directory_identity.st_ino)
                    track_progress = entry.name in progress or len(progress) < MAX_PROGRESS_PATHS
                    previous_progress = previous_progress_by_path.get(entry.name)
                    comparable = (
                        previous_progress_complete
                        and previous_progress is not None
                        and previous_progress.directory_identity == identity
                    )
                    directory_total = 0
                    signatures = {}
                    progressed = False
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
                            now = time.monotonic()
                            signature = self._signature(directory_identity, info)
                            previous = self._observations.get(path)
                            observation = (
                                previous
                                if previous is not None
                                and previous.signature == signature
                                and now >= previous.stable_since
                                else _Observation(
                                    signature,
                                    now,
                                    generation if self._scan_generation else 0,
                                )
                            )
                            observed[path] = observation
                            total += info.st_size
                            directory_total += info.st_size
                            if track_progress:
                                signatures[file.name] = signature
                                previous_signature = (
                                    previous_progress.files.get(file.name) if comparable else None
                                )
                                if (
                                    comparable
                                    and info.st_size > 0
                                    and (
                                        previous_signature is None
                                        or previous_signature[:4] != signature[:4]
                                        or info.st_size > previous_signature[4]
                                    )
                                ):
                                    progressed = True
                            segments.append(
                                _Segment(
                                    path,
                                    directory,
                                    directory_identity,
                                    info,
                                    observation.stable_since,
                                )
                            )
                    identity_state._same_directory(directory, directory_identity)
                    if track_progress:
                        now = time.monotonic()
                        if not math.isfinite(now):
                            raise _IncompleteStorage
                        last_progress = previous_progress.last_progress if comparable else None
                        if progressed:
                            last_progress = now
                        elif last_progress is not None and now < last_progress:
                            last_progress = None
                        progress[entry.name] = _PathProgress(
                            identity, directory_total, last_progress, signatures
                        )
                    if segments:
                        now = time.monotonic()
                        newest = max(
                            segments,
                            key=lambda segment: (segment.identity.st_mtime_ns, segment.path.name),
                        )
                        latest_change = max(
                            observed[segment.path].change_generation for segment in segments
                        )
                        candidates.extend(
                            segment
                            for segment in segments
                            if segment is not newest
                            # File timestamps can run backwards. Also protect
                            # the latest creation/change seen across complete
                            # scans, retaining ties rather than inventing their
                            # order from directory enumeration. A cold baseline
                            # cannot establish creation chronology.
                            and (
                                latest_change == 0
                                or observed[segment.path].change_generation < latest_change
                            )
                            and now - segment.stable_since >= MIN_CLOSED_AGE_SECONDS
                        )
            self._admit_root()
            _budget(deadline)
            ordered = tuple(
                sorted(
                    candidates,
                    key=lambda segment: (
                        segment.stable_since,
                        segment.identity.st_mtime_ns,
                        str(segment.path),
                    ),
                )
            )
            _budget(deadline)
            self._observations = observed
            self._scan_generation = generation
            with self._progress_lock:
                # Queries made while scanning may nominate current cameras in
                # place of historical directories. Preserve those bounded slots.
                updated = {name: progress.get(name, _PathProgress()) for name in self._progress}
                for name, sample in progress.items():
                    if len(updated) >= MAX_PROGRESS_PATHS:
                        break
                    updated.setdefault(name, sample)
                self._progress = updated
                self._progress_complete = True
            return _Scan(total, free, ordered, True)
        except Exception:
            # An interrupted or untrusted scan breaks the observation history.
            self._observations.clear()
            self._scan_generation = 0
            self._invalidate_progress()
            return _Scan(total, free, (), False)

    def _invalidate_progress(self) -> None:
        with self._progress_lock:
            self._progress = {name: _PathProgress() for name in self._progress}
            self._progress_complete = False

    def recording_progress(self, path_name: str) -> dict:
        """Read cached owned-byte growth; never inspect the filesystem here.

        Historical bytes establish a baseline, not evidence of fresh recording.
        Growth or a new nonempty segment across complete scans starts a monotonic
        progress age. Missing/incomplete observations cannot claim progress.
        Queries remember at most 64 camera paths for the next maintenance scan.
        """
        if type(path_name) is not str or _PATH_NAME.fullmatch(path_name) is None:
            raise ValueError("Invalid recording path name")
        with self._progress_lock:
            sample = self._progress.pop(path_name, None)
            if sample is None:
                if len(self._progress) >= MAX_PROGRESS_PATHS:
                    self._progress.pop(next(iter(self._progress)))
                sample = _PathProgress()
            self._progress[path_name] = sample
            age = None
            if sample.last_progress is not None:
                elapsed = time.monotonic() - sample.last_progress
                if math.isfinite(elapsed) and elapsed >= 0:
                    age = elapsed
            return {
                "observed_bytes": sample.total_bytes,
                "last_progress_age_seconds": age,
                "inspection_complete": self._progress_complete,
                "observed": sample.directory_identity is not None,
            }

    def recording_files(self, path_name: str) -> dict[str, tuple[int, ...]] | None:
        """Internal immutable inventory from the last complete owned scan.

        This supplies background frame sampling without rescanning retained media.
        Values are identity/size signatures, never file contents or source URIs.
        Consumers must independently admit and verify any file they actually open.
        """
        self.recording_progress(path_name)  # Nominate this bounded camera slot.
        with self._progress_lock:
            sample = self._progress.get(path_name)
            if not self._progress_complete or sample is None or sample.directory_identity is None:
                return None
            return dict(sample.files)

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
        """Free closed segments, protecting newest and latest observed changes.

        A complete scan and ten seconds of unchanged monotonic observations are
        required before any deletion. Retention expires after the configured
        observed stable age, even under quota. Byte pressure prunes toward 90%
        of the budget. Failed/shared-open deletions are skipped. Restarting this
        object resets its observations instead of inferring age from wall time.
        Additional clips can remain protected after a backwards clock step or
        when multiple files change between complete scans.
        """
        deadline = time.monotonic() + MAX_STORAGE_SECONDS
        with self._lock:
            remaining_entries = [MAX_STORAGE_ENTRIES]
            scan = self._scan(deadline, remaining_entries)
            if not scan.complete:
                return self._status(scan)
            cutoff = time.monotonic() - self._retention_seconds
            if self._status(scan).recording_allowed and (
                self._retention_seconds == 0
                or not any(segment.stable_since <= cutoff for segment in scan.candidates)
            ):
                return self._status(scan)
            target = (
                self._max_bytes * 9 // 10 if scan.total_bytes > self._max_bytes else self._max_bytes
            )
            total = scan.total_bytes
            free = scan.free_bytes
            deleted = 0
            for segment in scan.candidates:
                try:
                    _budget(deadline)
                    expired = (
                        self._retention_seconds > 0
                        and time.monotonic() - segment.stable_since >= self._retention_seconds
                    )
                    if not expired and total <= target and free >= self._min_free_bytes:
                        continue
                    self._admit_root()
                    identity_state._same_directory(segment.directory, segment.directory_identity)
                    current = _file_info(segment.path)
                    signature = self._signature(segment.directory_identity, current)
                    observation = self._observations.get(segment.path)
                    if (
                        observation is None
                        or observation.signature != signature
                        or observation.stable_since != segment.stable_since
                    ):
                        self._observations.pop(segment.path, None)
                        continue
                    if time.monotonic() - observation.stable_since < MIN_CLOSED_AGE_SECONDS:
                        continue
                    _budget(deadline)
                    segment.path.unlink()
                except OSError:
                    # Windows sharing violations and independent removals are
                    # ordinary races; do not delete another target.
                    continue
                except Exception:
                    self._invalidate_progress()
                    return StorageStatus(total, free, False, deleted)
                deleted += 1
                total -= current.st_size
                try:
                    free = _free_bytes(self._root)
                except Exception:
                    self._invalidate_progress()
                    return StorageStatus(total, free, False, deleted)
            return self._status(self._scan(deadline, remaining_entries), deleted)
