"""Create a protected recording container without changing a selected parent ACL."""

from __future__ import annotations

import ctypes
import hashlib
import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path

from k5vision import identity_state


class StorageContainerError(ValueError):
    """No ordinary protected storage location could be created."""


def _ordinary(path: Path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise StorageContainerError("Storage parent must be an ordinary directory")
    return info


def _admit_path(path: Path):
    """Admit readable ancestors; no foreign directory mutation is allowed."""
    if not path.is_absolute() or ".." in path.parts:
        raise StorageContainerError("Storage parent must be an absolute local directory")
    if os.name == "nt":
        from k5vision.windows_identity_security import check_windows_volume

        check_windows_volume(path)
    for directory in reversed((path, *path.parents)):
        if not directory.exists():
            raise StorageContainerError("Storage parent must already exist")
        info = _ordinary(directory)
        if os.name == "posix" and (
            info.st_uid not in {0, os.geteuid()}
            or info.st_mode & 0o022
            and not info.st_mode & stat.S_ISVTX
        ):
            raise StorageContainerError("Storage ancestors permit foreign mutation")
        identity_state._windows_admission(directory, private=False)


def _private_leaf(path: Path) -> bool:
    """A conservative read-only predicate after general ancestor admission."""
    info = _ordinary(path)
    if os.name == "posix":
        return info.st_uid == os.geteuid() and not info.st_mode & 0o077
    try:
        identity_state._windows_admission(path, private=True, directory=True)
        return True
    except identity_state.IdentityStateError:
        return False


@contextmanager
def _pin_ancestors(path: Path):
    if os.name != "nt":
        yield
        return
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handles = []
    try:
        for directory in reversed((path, *path.parents)):
            _admit_path(directory)
            handle = kernel.CreateFileW(
                str(directory), 0, 3, None, 3, 0x02000000 | 0x00200000, None
            )
            if not handle or handle == ctypes.c_void_p(-1).value:
                raise StorageContainerError("Storage directory could not be pinned")
            handles.append(handle)
            # Ancestors are now held without delete sharing; revalidate before
            # opening the next component or creating a private child.
            _admit_path(directory)
        yield
    finally:
        for handle in reversed(handles):
            kernel.CloseHandle(handle)


def _create_private(path: Path):
    if os.name != "nt":
        path.mkdir(mode=0o700)
        return
    from ctypes import wintypes

    from k5vision.windows_identity_security import _NativeSecurity

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.DWORD),
            ("descriptor", ctypes.c_void_p),
            ("inherit", wintypes.BOOL),
        ]

    security, kernel = (
        ctypes.WinDLL("advapi32", use_last_error=True),
        ctypes.WinDLL("kernel32", use_last_error=True),
    )
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
    ]
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    kernel.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(SecurityAttributes)]
    kernel.CreateDirectoryW.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    sid = _NativeSecurity().current_user()
    descriptor = ctypes.c_void_p()
    sddl = f"O:{sid}D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;{sid})"
    if not security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, 1, ctypes.byref(descriptor), None
    ):
        raise StorageContainerError("Private storage permissions could not be prepared")
    try:
        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        if not kernel.CreateDirectoryW(str(path), ctypes.byref(attributes)):
            raise StorageContainerError("Private storage directory could not be created")
    finally:
        kernel.LocalFree(descriptor)


def prepare_private_recording_container(
    value: str, site_id: str, *, protected: tuple[Path, ...]
) -> Path | None:
    """Prepare only a fresh child when broad read permissions are the blocker.

    Existing directories and ACLs are never changed. General ownership,
    mutation, volume and reparse failures are rejected before creation; a
    failure unrelated to private read access does not get an alternate path.
    """
    created = []
    try:
        if type(value) is not str or not value or len(value) > 1024 or "\x00" in value:
            raise StorageContainerError("Storage directory was refused")
        requested = Path(value)
        if (
            not requested.is_absolute()
            or ".." in requested.parts
            or any(
                requested == root or requested in root.parents or root in requested.parents
                for root in protected
            )
        ):
            raise StorageContainerError("Storage directory overlaps protected data")
        existing = requested.exists()
        parent = requested if existing else requested.parent
        _admit_path(parent)
        if existing:
            if _private_leaf(requested) and _private_leaf(requested.parent):
                return None
        elif _private_leaf(parent):
            return None
        outer = (
            parent
            / (
                "K5Vision-"
                + hashlib.sha256(site_id.encode()).hexdigest()[:16]
                + "-"
                + secrets.token_hex(8)
            )
            if existing
            else requested
        )
        recordings = outer / "recordings"
        with _pin_ancestors(parent):
            _admit_path(parent)
            for directory in (outer, recordings):
                # The directory must be new. Never repair/adopt a raced entry.
                _create_private(directory)
                created.append((directory, _ordinary(directory)))
                if not _private_leaf(directory):
                    raise StorageContainerError("Created storage directory is not private")
            admitted = identity_state._checked_path(recordings)
            identity_state._windows_admission(admitted, private=True, directory=True)
            return admitted
    except Exception:
        # Remove only our still-empty directories with the original identity.
        # Unknown contents or a replaced entry are preserved.
        for directory, expected in reversed(created):
            try:
                current = _ordinary(directory)
                if (current.st_dev, current.st_ino) == (expected.st_dev, expected.st_ino):
                    directory.rmdir()
            except (OSError, ValueError):
                pass
        raise StorageContainerError("Private recording container could not be prepared") from None
