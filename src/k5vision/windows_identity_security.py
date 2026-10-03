"""Read-only, conservative Windows admission for durable local identity state.

This is an allowlist, not a general Windows effective-permission evaluator.
Unsupported ACLs/storage layouts are refused; permissions are never repaired.
"""

from __future__ import annotations

import ctypes
import os
import re
import struct
from dataclasses import dataclass
from pathlib import PureWindowsPath

_ADMINISTRATORS = "S-1-5-32-544"
_SYSTEM = "S-1-5-18"
_OWNER_RIGHTS = "S-1-3-4"
_CREATOR_OWNER = "S-1-3-0"
_TRUSTED_INSTALLER = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
_ANCESTOR_MUTATION = 0x010D0150  # DELETE, WRITE_DAC/OWNER, DELETE_CHILD, WRITE_EA/ATTRIBUTES, SACL
_FILE_ALL = 0x001F01FF
_GENERIC = (
    (0x80000000, 0x00120089),
    (0x40000000, 0x00120116),
    (0x20000000, 0x001200A0),
    (0x10000000, _FILE_ALL),
)


class WindowsIdentitySecurityError(RuntimeError):
    """Native admission could not establish the required local/private boundary."""


@dataclass(frozen=True)
class AccessEntry:
    kind: int
    flags: int
    mask: int
    sid: str


def _sid_bytes(data: bytes) -> str:
    if len(data) < 8 or data[0] != 1 or data[1] > 15 or len(data) != 8 + data[1] * 4:
        raise WindowsIdentitySecurityError("unsupported Windows security identifier")
    authority = int.from_bytes(data[2:8], "big")
    subauthorities = struct.unpack(f"<{data[1]}I", data[8:])
    return "-".join(map(str, ("S", 1, authority, *subauthorities)))


def _acl_entries(data: bytes) -> tuple[AccessEntry, ...]:
    if len(data) < 8:
        raise WindowsIdentitySecurityError("invalid Windows identity ACL")
    revision, reserved, size, count, reserved2 = struct.unpack_from("<BBHHH", data)
    if (
        revision not in (2, 4)
        or reserved
        or reserved2
        or size != len(data)
        or size % 4
        or count > 4096
    ):
        raise WindowsIdentitySecurityError("unsupported Windows identity ACL")
    entries = []
    offset = 8
    for _ in range(count):
        if offset + 16 > size:
            raise WindowsIdentitySecurityError("invalid Windows identity ACL entry")
        kind, flags, length, mask = struct.unpack_from("<BBHI", data, offset)
        if (
            kind not in (0, 1)
            or flags & ~0x1F
            or length < 16
            or length % 4
            or offset + length > size
            or flags & 0x08
            and not flags & 0x03
        ):
            raise WindowsIdentitySecurityError("unsupported Windows identity ACL entry")
        if mask & ~(_FILE_ALL | 0xF1000000):
            raise WindowsIdentitySecurityError("unsupported Windows identity access mask")
        sid = _sid_bytes(data[offset + 8 : offset + length])
        entries.append(AccessEntry(kind, flags, mask, sid))
        offset += length
    return tuple(entries)


def _expanded_mask(mask: int) -> int:
    for generic, specific in _GENERIC:
        if mask & generic:
            mask = (mask & ~generic) | specific
    return mask


def _check_policy(
    owner: str,
    entries: tuple[AccessEntry, ...],
    current_user: str,
    *,
    private: bool,
    require_file_inheritance: bool,
    trusted_installer_verified: bool,
) -> None:
    trusted = {current_user, _ADMINISTRATORS, _SYSTEM}
    if not private and trusted_installer_verified:
        trusted.add(_TRUSTED_INSTALLER)
    if owner not in trusted:
        raise WindowsIdentitySecurityError("Windows identity path has unsupported ownership")
    file_inheritance = False
    for entry in entries:
        if entry.kind == 1:  # Denials never cancel a foreign allow for this conservative policy.
            continue
        inherit_only = bool(entry.flags & 0x08)
        if inherit_only and not private:
            continue
        sid = entry.sid
        if sid == _OWNER_RIGHTS:
            sid = owner
        elif sid == _CREATOR_OWNER and inherit_only:
            sid = current_user  # Future files are created by this process, not an unknown owner.
        mask = _expanded_mask(entry.mask)
        if sid not in trusted and mask & (0xFFFFFFFF if private else _ANCESTOR_MUTATION):
            raise WindowsIdentitySecurityError("Windows identity path grants unsafe access")
        if sid in trusted and entry.flags & 0x01 and mask & 0x03 == 0x03:
            file_inheritance = True
    if require_file_inheritance and not file_inheritance:
        raise WindowsIdentitySecurityError(
            "Windows identity directory lacks private file inheritance"
        )


class _NativeSecurity:
    """Small ctypes adapter; only native query APIs are bound."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise WindowsIdentitySecurityError("Windows identity inspection is unavailable")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.security = ctypes.WinDLL("advapi32", use_last_error=True)
        pointer, dword, boolean = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int
        pp, pd = ctypes.POINTER(pointer), ctypes.POINTER(dword)
        bindings = (
            (self.kernel, "GetCurrentProcess", [], pointer),
            (self.kernel, "CloseHandle", [pointer], boolean),
            (self.kernel, "LocalFree", [pointer], pointer),
            (self.kernel, "GetDriveTypeW", [ctypes.c_wchar_p], dword),
            (self.kernel, "QueryDosDeviceW", [ctypes.c_wchar_p, ctypes.c_wchar_p, dword], dword),
            (self.security, "OpenProcessToken", [pointer, dword, pp], boolean),
            (self.security, "GetTokenInformation", [pointer, dword, pointer, dword, pd], boolean),
            (self.security, "IsValidSid", [pointer], boolean),
            (self.security, "GetLengthSid", [pointer], dword),
            (self.security, "IsValidAcl", [pointer], boolean),
            (
                self.security,
                "GetNamedSecurityInfoW",
                [ctypes.c_wchar_p, dword, dword, pp, pp, pp, pp, pp],
                dword,
            ),
            (
                self.security,
                "LookupAccountNameW",
                [ctypes.c_wchar_p, ctypes.c_wchar_p, pointer, pd, ctypes.c_wchar_p, pd, pd],
                boolean,
            ),
        )
        for library, name, arguments, result in bindings:
            function = getattr(library, name)
            function.argtypes, function.restype = arguments, result

    def sid(self, pointer: int | None) -> str:
        if not pointer or not self.security.IsValidSid(pointer):
            raise WindowsIdentitySecurityError("invalid native Windows security identifier")
        length = self.security.GetLengthSid(pointer)
        if not 8 <= length <= 68:
            raise WindowsIdentitySecurityError("unsupported native Windows security identifier")
        return _sid_bytes(ctypes.string_at(pointer, length))

    def current_user(self) -> str:
        token = ctypes.c_void_p()
        if not self.security.OpenProcessToken(
            self.kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)
        ):
            raise WindowsIdentitySecurityError("Windows identity token is unavailable")
        try:
            length = ctypes.c_uint32()
            self.security.GetTokenInformation(token, 1, None, 0, ctypes.byref(length))
            if not ctypes.sizeof(ctypes.c_void_p) <= length.value <= 4096:
                raise WindowsIdentitySecurityError("Windows identity token is unsupported")
            buffer = ctypes.create_string_buffer(length.value)
            if not self.security.GetTokenInformation(
                token, 1, buffer, length, ctypes.byref(length)
            ):
                raise WindowsIdentitySecurityError("Windows identity token cannot be read")
            return self.sid(ctypes.c_void_p.from_buffer(buffer).value)
        finally:
            self.kernel.CloseHandle(token)

    def trusted_installer(self) -> bool:
        # Resolve the local service principal, then compare its numeric SID exactly.
        # A display name, arbitrary service SID, or unresolved lookup grants nothing.
        sid_size, domain_size, use = ctypes.c_uint32(68), ctypes.c_uint32(256), ctypes.c_uint32()
        sid, domain = ctypes.create_string_buffer(68), ctypes.create_unicode_buffer(256)
        if not self.security.LookupAccountNameW(
            None,
            "NT SERVICE\\TrustedInstaller",
            sid,
            ctypes.byref(sid_size),
            domain,
            ctypes.byref(domain_size),
            ctypes.byref(use),
        ):
            return False
        return self.sid(ctypes.addressof(sid)) == _TRUSTED_INSTALLER

    def descriptor(self, path: str) -> tuple[str, tuple[AccessEntry, ...]]:
        owner, dacl, descriptor = (ctypes.c_void_p() for _ in range(3))
        try:
            result = self.security.GetNamedSecurityInfoW(
                path,
                1,
                0x00000005,
                ctypes.byref(owner),
                None,
                ctypes.byref(dacl),
                None,
                ctypes.byref(descriptor),
            )
            if result or not descriptor or not dacl or not self.security.IsValidAcl(dacl):
                raise WindowsIdentitySecurityError("Windows identity ACL cannot be established")
            header = ctypes.string_at(dacl, 8)
            size = struct.unpack_from("<H", header, 2)[0]
            if size < 8:
                raise WindowsIdentitySecurityError("Windows identity ACL is invalid")
            return self.sid(owner.value), _acl_entries(ctypes.string_at(dacl, size))
        finally:
            if descriptor:
                self.kernel.LocalFree(descriptor)


def check_windows_volume(path: str | os.PathLike[str]) -> None:
    """Reject remote/ambiguous/aliased volumes before any path/ACL lookup."""
    value = PureWindowsPath(path)
    if (
        not value.is_absolute()
        or not re.fullmatch(r"[A-Za-z]:", value.drive)
        or ".." in value.parts
        or value.is_reserved()
        or any(
            PureWindowsPath(part).is_reserved()
            or re.search(r'[:*?"<>|\x00-\x1f]', part)
            or part.endswith((".", " "))
            for part in value.parts[1:]
        )
    ):
        raise WindowsIdentitySecurityError("Windows identity requires an ordinary local path")
    native = _NativeSecurity()
    if native.kernel.GetDriveTypeW(value.anchor) != 3:
        raise WindowsIdentitySecurityError("Windows identity requires a fixed local volume")
    mapping = ctypes.create_unicode_buffer(32768)
    if not native.kernel.QueryDosDeviceW(value.drive, mapping, len(mapping)):
        raise WindowsIdentitySecurityError("Windows identity volume cannot be established")
    # QueryDosDevice's first MULTI_SZ entry is the current mapping. Historical
    # entries do not grant admission. SUBST hides real ancestors and is refused.
    if not re.fullmatch(r"\\Device\\HarddiskVolume[0-9]+", mapping.value, re.IGNORECASE):
        raise WindowsIdentitySecurityError("Windows identity volume aliases are unsupported")


def check_windows_security(
    path: str | os.PathLike[str],
    *,
    private: bool,
    require_file_inheritance: bool = False,
) -> None:
    """Inspect an existing path; never modify its ownership or security settings."""
    native = _NativeSecurity()
    owner, entries = native.descriptor(os.fspath(path))
    installer_used = owner == _TRUSTED_INSTALLER or any(
        entry.sid == _TRUSTED_INSTALLER for entry in entries
    )
    _check_policy(
        owner,
        entries,
        native.current_user(),
        private=private,
        require_file_inheritance=require_file_inheritance,
        trusted_installer_verified=not private and installer_used and native.trusted_installer(),
    )
