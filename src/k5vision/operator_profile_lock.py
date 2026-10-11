"""One OS-backed owner for an admitted, complete operator identity profile.

The fixed lock file stays in place after close. Keeping its inode/name prevents
ordinary stop/start races from creating a second independent lock object.
"""

from __future__ import annotations

import os
import stat
import threading
from pathlib import Path

from k5vision import identity_state

_LOCK_NAME = "operator.lock"
_LOCK_BYTE = b"1"
_FAILURE = "Operator profile is already in use or unavailable."


class OperatorProfileLockError(RuntimeError):
    """Source-free refusal of an unavailable or unsafe profile lease."""


def _private_file(path: Path) -> os.stat_result:
    info = identity_state._regular_file(path)
    if os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
        raise ValueError
    return info


def _same_file(descriptor: int, path: Path) -> os.stat_result:
    current = _private_file(path)
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
    ):
        raise ValueError
    return opened


def _exclusive_lock(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
    elif os.name == "posix":
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    else:
        raise ValueError


class OperatorProfileLock:
    """Hold one noninheritable file descriptor until explicit, idempotent close.

    Each acquisition revalidates the complete durable identity. No existing
    directory, file mode, ACL, or lock content is repaired. An empty interrupted
    lock-file creation may receive the same constant byte without truncation.
    """

    def __init__(self, root: Path) -> None:
        if not isinstance(root, Path):
            raise OperatorProfileLockError(_FAILURE)
        self._root = root
        self._descriptor: int | None = None
        self._guard = threading.RLock()

    def acquire(self) -> None:
        with self._guard:
            if self._descriptor is not None:
                raise OperatorProfileLockError(_FAILURE)
            descriptor = None
            failed = False
            try:
                state = identity_state.load_identity_state(self._root)
                root = state.directory
                directory_identity = root.lstat()
                identity_state._same_directory(root, directory_identity)
                path = root / _LOCK_NAME
                flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
                existing_identity = None
                try:
                    descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    # Admit before opening as well as checking the actual handle.
                    existing_identity = _private_file(path)
                    descriptor = os.open(path, flags)
                os.set_inheritable(descriptor, False)
                identity_state._same_directory(root, directory_identity)
                opened = _same_file(descriptor, path)
                if existing_identity is not None and (opened.st_dev, opened.st_ino) != (
                    existing_identity.st_dev,
                    existing_identity.st_ino,
                ):
                    raise ValueError
                if opened.st_size == 0:
                    # No O_TRUNC: a second opener can never discard a held lease.
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    if os.write(descriptor, _LOCK_BYTE) != 1:
                        raise ValueError
                    os.fsync(descriptor)
                elif opened.st_size != 1:
                    raise ValueError
                os.lseek(descriptor, 0, os.SEEK_SET)
                if os.read(descriptor, 1) != _LOCK_BYTE:
                    raise ValueError
                _exclusive_lock(descriptor)
                identity_state._same_directory(root, directory_identity)
                if _same_file(descriptor, path).st_size != 1:
                    raise ValueError
                os.lseek(descriptor, 0, os.SEEK_SET)
                if os.read(descriptor, 1) != _LOCK_BYTE:
                    raise ValueError
                # Transfer ownership only after both native locking and admission.
                self._descriptor, descriptor = descriptor, None
            except Exception:
                failed = True
            finally:
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        failed = True
            if failed:
                # Raise outside the private exception handler: no raw path/error
                # survives in the exported exception's context or cause.
                raise OperatorProfileLockError(_FAILURE)

    def close(self) -> None:
        with self._guard:
            descriptor, self._descriptor = self._descriptor, None
            failed = False
            if descriptor is not None:
                try:
                    # Closing releases flock and Windows byte locks. Never unlink.
                    os.close(descriptor)
                except OSError:
                    failed = True
            if failed:
                raise OperatorProfileLockError(_FAILURE)
