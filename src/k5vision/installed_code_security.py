"""Read-only admission for immutable machine-installed executable files.

Only the native Program Files K5 directory receives this code policy. Other
paths retain private-file admission; an installed-code refusal never retries
under a different policy. Identity, credentials and recordings remain private.
"""

from __future__ import annotations

import ctypes
import os
import stat
from pathlib import Path, PureWindowsPath
from uuid import UUID

from k5vision.identity_state import _regular_file as _private_regular_file
from k5vision.windows_identity_security import (
    _ADMINISTRATORS,
    _ANCESTOR_MUTATION,
    _OWNER_RIGHTS,
    _SYSTEM,
    _TRUSTED_INSTALLER,
    WindowsIdentitySecurityError,
    _check_policy,
    _expanded_mask,
    _native_security,
    check_windows_volume,
)

_PROGRAM_FILES = UUID("905e63b6-c1bf-494e-b29c-65b732d3d21a")


def _installed_root() -> PureWindowsPath:
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    ole = ctypes.WinDLL("ole32", use_last_error=True)
    shell.SHGetKnownFolderPath.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    shell.SHGetKnownFolderPath.restype = ctypes.c_int32
    ole.CoTaskMemFree.argtypes, ole.CoTaskMemFree.restype = [ctypes.c_void_p], None
    identifier = ctypes.create_string_buffer(_PROGRAM_FILES.bytes_le)
    result = ctypes.c_void_p()
    try:
        if shell.SHGetKnownFolderPath(identifier, 0, None, ctypes.byref(result)) != 0:
            raise WindowsIdentitySecurityError("Installed code location is unavailable")
        if not result.value:
            raise WindowsIdentitySecurityError("Installed code location is unavailable")
        root = PureWindowsPath(ctypes.wstring_at(result))
        if not root.is_absolute():
            raise WindowsIdentitySecurityError("Installed code location is unsupported")
        return root / "K5 Vision"
    finally:
        if result.value:
            ole.CoTaskMemFree(result)


def _snapshot(path: PureWindowsPath) -> os.stat_result:
    return Path(path).lstat()


def _identity(info: os.stat_result) -> tuple:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_mtime_ns,
        info.st_size,
        getattr(info, "st_file_attributes", 0),
    )


def _code_policy(native, path: PureWindowsPath, *, within: bool) -> None:
    owner, entries = native.descriptor(str(path))
    installer_used = owner == _TRUSTED_INSTALLER or any(
        entry.sid == _TRUSTED_INSTALLER for entry in entries
    )
    installer_verified = installer_used and native.trusted_installer()
    # SYSTEM is a fixed trusted code owner, not the calling user's TokenUser.
    # Passing the current service/user SID here would admit writable code.
    _check_policy(
        owner,
        entries,
        _SYSTEM,
        private=False,
        require_file_inheritance=False,
        trusted_installer_verified=installer_verified,
    )
    if within:
        trusted = {_ADMINISTRATORS, _SYSTEM}
        if installer_verified:
            trusted.add(_TRUSTED_INSTALLER)
        for entry in entries:
            if entry.kind != 0 or entry.flags & 0x08:
                continue
            sid = owner if entry.sid == _OWNER_RIGHTS else entry.sid
            if sid not in trusted and _expanded_mask(entry.mask) & (_ANCESTOR_MUTATION | 0x06):
                raise WindowsIdentitySecurityError("Installed code grants unsafe mutation")


def checked_executable_file(value: Path) -> os.stat_result:
    """Admit a fixed machine code file or preserve legacy private-file checks."""
    if os.name != "nt":
        return _private_regular_file(value)
    path, root = PureWindowsPath(os.fspath(value)), _installed_root()
    if not path.is_relative_to(root):
        return _private_regular_file(value)
    if (
        path == root
        or not path.is_absolute()
        or any(ord(character) < 32 for character in str(path))
        or any(
            part in {".", ".."}
            or part.endswith((".", " "))
            or any(character in '<>:"|?*' for character in part)
            for part in path.parts[1:]
        )
    ):
        raise WindowsIdentitySecurityError("Installed code path is unsupported")
    check_windows_volume(str(path))
    native, checked = _native_security(), []
    for component in reversed((path, *path.parents)):
        before = _snapshot(component)
        if getattr(before, "st_file_attributes", 0) & 0x400 or stat.S_ISLNK(before.st_mode):
            raise WindowsIdentitySecurityError("Installed code links are unsupported")
        if component == path:
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise WindowsIdentitySecurityError("Installed code is not an ordinary single file")
        elif not stat.S_ISDIR(before.st_mode):
            raise WindowsIdentitySecurityError("Installed code ancestor is not a directory")
        _code_policy(native, component, within=component.is_relative_to(root))
        checked.append((component, _identity(before)))
    result = None
    for component, expected in checked:
        current = _snapshot(component)
        if _identity(current) != expected:
            raise WindowsIdentitySecurityError("Installed code changed during admission")
        if component == path:
            result = current
    return result
