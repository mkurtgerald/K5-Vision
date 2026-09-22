"""Shared attempt ownership for local recording publication.

A logical recording keeps the historical deterministic ``.part`` pathname as an
exclusive ownership marker so older writers and repaired writers still conflict.
Actual payload bytes live in an unguessable per-attempt staging file.  Cleanup is
therefore scoped to the attempt that successfully acquired the marker.
"""

from __future__ import annotations

import os
import pathlib
import secrets
import typing

FileIdentity = tuple[int, int]


class RecordingAttempt:
    """Own one logical recording attempt and its private staging object."""

    def __init__(
        self,
        root: pathlib.Path,
        recording_id: str,
        *,
        lock_suffix: str,
        staging_suffix: str,
    ) -> None:
        self._root = pathlib.Path(root)
        self._token = secrets.token_hex(16)
        self._lock_path = self._root / f".{recording_id}{lock_suffix}"
        self._staging_path = self._root / f".{recording_id}.{self._token}{staging_suffix}"
        self._owns_lock = False
        self._owns_staging = False

    @staticmethod
    def _exclusive_flags() -> int:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        return flags

    @staticmethod
    def _write_all(fd: int, payload: bytes) -> None:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError
            view = view[written:]

    def _release_lock(self) -> None:
        if not self._owns_lock:
            return
        try:
            marker = self._lock_path.read_bytes()
        except FileNotFoundError:
            self._owns_lock = False
            return
        if marker != self._token.encode("ascii"):
            raise OSError
        self._lock_path.unlink()
        self._owns_lock = False

    def _remove_staging(self) -> None:
        if not self._owns_staging:
            return
        try:
            self._staging_path.unlink()
        except FileNotFoundError:
            pass
        self._owns_staging = False

    def acquire_staging(self) -> typing.BinaryIO:
        """Acquire the logical ID and return this attempt's private staging file."""
        self._root.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(self._lock_path, self._exclusive_flags(), 0o600)
        self._owns_lock = True
        try:
            self._write_all(lock_fd, self._token.encode("ascii"))
            os.fsync(lock_fd)
        except BaseException:
            try:
                os.close(lock_fd)
            finally:
                try:
                    self._release_lock()
                except OSError:
                    pass
            raise
        os.close(lock_fd)

        try:
            staging_fd = os.open(self._staging_path, self._exclusive_flags(), 0o600)
        except BaseException:
            self._release_lock()
            raise
        self._owns_staging = True
        try:
            return os.fdopen(staging_fd, "wb", buffering=0)
        except BaseException:
            os.close(staging_fd)
            try:
                self.cleanup()
            except OSError:
                pass
            raise

    def cleanup(self) -> None:
        """Remove only resources acquired by this attempt."""
        first_error: OSError | None = None
        try:
            self._remove_staging()
        except OSError as exc:
            first_error = exc
        try:
            self._release_lock()
        except OSError as exc:
            if first_error is None:
                first_error = exc
        if first_error is not None:
            raise first_error

    @staticmethod
    def _identity(path: pathlib.Path) -> FileIdentity:
        stat = path.stat()
        return (int(stat.st_dev), int(stat.st_ino))

    @staticmethod
    def rollback_published(final_path: pathlib.Path, identity: FileIdentity) -> None:
        """Remove a late publication only when it is still this attempt's inode."""
        try:
            current = RecordingAttempt._identity(final_path)
        except FileNotFoundError:
            return
        if current != identity:
            raise OSError
        final_path.unlink()

    def publish(self, final_path: pathlib.Path) -> FileIdentity:
        """Atomically publish this attempt and relinquish logical ownership."""
        if not self._owns_lock or not self._owns_staging:
            raise OSError
        identity = self._identity(self._staging_path)
        os.link(self._staging_path, final_path)
        published = True
        try:
            self._staging_path.unlink()
            self._owns_staging = False
            self._release_lock()
        except BaseException:
            if published:
                try:
                    self.rollback_published(final_path, identity)
                except OSError:
                    pass
            try:
                self.cleanup()
            except OSError:
                pass
            raise
        return identity
