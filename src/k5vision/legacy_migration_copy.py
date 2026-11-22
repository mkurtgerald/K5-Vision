"""Windows-only, Job-owned copying into one fixed private service stage.

Only the authenticated source controller calls this module. Source paths come
from its admitted inventory, never UI, command arguments, environment or logs.
The service creates the stage and independently verifies/registers the result.
This narrow target ACL policy does not change generic identity admission.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import re
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from k5vision import identity_state
from k5vision import legacy_identity_snapshot as snapshot
from k5vision.operator_recording_storage import _PATH_NAME, _segment_name
from k5vision.operator_storage_settings import _descriptor_signature, _signature
from k5vision.windows_identity_security import (
    _FILE_ALL,
    _OWNER_RIGHTS,
    _acl_entries,
    _check_policy,
    _expanded_mask,
    _native_security,
    check_windows_volume,
)

_BA = "S-1-5-32-544"
_SY = "S-1-5-18"
_ACCOUNT = "NT SERVICE\\K5VisionRecorder"
_PROGRAM_DATA = UUID("62ab5d82-fdc1-4dc3-a9dd-070d1d495d97")
_LOCAL_DATA = UUID("f1b32785-6fba-4fcf-9d55-7b8e7f157091")
_IDENTITY = (snapshot._AUTHORITIES - {"identity.json"}) | {
    snapshot._STAGED_MANIFEST,
    snapshot._MARKER,
}
_CHUNK = 1024**2
_SECONDS = 86400
_EPOCH_TICKS = 116444736000000000


class MigrationCopyError(RuntimeError):
    """Finite refusal; no original or incomplete destination is removed."""


def _entry(value, transfer_id):
    if (
        type(value) is not dict
        or set(value) != {"area", "relative", "size", "sha256", "mtime_ns"}
        or type(transfer_id) is not str
        or str(UUID(transfer_id)) != transfer_id
    ):
        raise ValueError
    area, relative = value["area"], value["relative"]
    if type(area) is not str or type(relative) is not str or not 1 <= len(relative) <= 240:
        raise ValueError
    if area == "identity":
        if relative not in _IDENTITY:
            raise ValueError
    elif area == "recordings":
        parts = relative.split("/")
        if len(parts) != 2 or _PATH_NAME.fullmatch(parts[0]) is None or not _segment_name(parts[1]):
            raise ValueError
    elif area == "exports":
        parts = relative.rsplit(".", 1)
        if len(parts) != 2 or parts[1] not in {"json", "mp4"} or str(UUID(parts[0])) != parts[0]:
            raise ValueError
    else:
        raise ValueError
    if (
        type(value["size"]) is not int
        or not 0 <= value["size"] <= 4096 * 1024**3
        or area == "identity"
        and not 1 <= value["size"] <= 128 * 1024**2
        or type(value["mtime_ns"]) is not int
        or not 0 <= value["mtime_ns"] <= 2**63 - 1
        or type(value["sha256"]) is not str
        or re.fullmatch("[a-f0-9]{64}", value["sha256"]) is None
    ):
        raise ValueError
    return area, relative.split("/")


def _target_policy(owner, entries, service, *, protected, require_protected, directory):
    """Pure conservative policy; only the fixed stage adapter may invoke it."""
    trusted = {service, _BA, _SY}
    if (
        type(service) is not str
        or re.fullmatch(r"S-1-5-80-(?:[0-9]+-){4}[0-9]+", service) is None
        or owner not in trusted
        or type(protected) is not bool
        or require_protected
        and not protected
    ):
        raise ValueError
    service_full = service_inheritance = False
    for entry in entries:
        sid = owner if entry.sid == _OWNER_RIGHTS else entry.sid
        if entry.kind != 0 or sid not in trusted or entry.flags & ~0x1F:
            raise ValueError
        mask = _expanded_mask(entry.mask)
        if mask & ~_FILE_ALL:
            raise ValueError
        if sid == service and not entry.flags & 0x08 and mask & _FILE_ALL == _FILE_ALL:
            service_full = True
            service_inheritance |= entry.flags & 0x03 == 0x03
    if not service_full or directory and not service_inheritance:
        raise ValueError


def _ordinary(path, *, directory):
    info = path.lstat()
    kind = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (
        not kind
        or getattr(info, "st_file_attributes", 0) & 0x400
        or not directory
        and info.st_nlink != 1
    ):
        raise ValueError
    return info


def _source_info(path):
    info = identity_state._regular_file(path)
    if os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
        raise ValueError
    return info


def _same_source(path, expected):
    if _signature(_source_info(path)) != expected:
        raise ValueError


def _hash_stream(stream, size, check, *, output=None):
    digest, remaining = hashlib.sha256(), size
    while block := stream.read(min(_CHUNK, remaining + 1)):
        check()
        remaining -= len(block)
        if remaining < 0:
            raise ValueError
        digest.update(block)
        if output is not None and output.write(block) != len(block):
            raise ValueError
    if remaining:
        raise ValueError
    return digest.hexdigest()


def _copy_with_backend(
    source, entry, transfer_id, backend, *, checkpoint=None, maximum_seconds=_SECONDS
):
    """Portable algorithm seam; supplying a fake backend proves no native ACL."""
    area, parts = _entry(entry, transfer_id)
    if type(maximum_seconds) not in (int, float) or not 0 < maximum_seconds <= _SECONDS:
        raise ValueError
    if checkpoint is not None and not callable(checkpoint):
        raise ValueError
    source = Path(source)
    if source.name != parts[-1]:
        raise ValueError
    signature = _signature(_source_info(source))
    if signature[2] != entry["size"] or signature[3] != entry["mtime_ns"]:
        raise ValueError
    deadline = time.monotonic() + maximum_seconds

    def check(*, external=True):
        if time.monotonic() >= deadline:
            raise TimeoutError
        backend.recheck()
        if external and checkpoint is not None and checkpoint() is not None:
            raise ValueError

    with backend.pin(transfer_id, area, parts) as destination:
        check()
        with backend.open_source(source) as incoming:
            opened_source = _descriptor_signature(incoming.fileno(), signature)
            if destination.exists() or destination.is_symlink():
                target = backend.admit_file(destination)
                expected_target = _signature(target)
                if target.st_size != entry["size"] or target.st_mtime_ns != entry["mtime_ns"]:
                    raise ValueError
                if _hash_stream(incoming, entry["size"], check) != entry["sha256"]:
                    raise ValueError
                check()
                with backend.open_existing(destination) as outgoing:
                    opened_target = _descriptor_signature(outgoing.fileno(), expected_target)
                    if (
                        _hash_stream(outgoing, entry["size"], lambda: check(external=False))
                        != entry["sha256"]
                    ):
                        raise ValueError
                    if _descriptor_signature(outgoing.fileno(), expected_target) != opened_target:
                        raise ValueError
                if _signature(backend.admit_file(destination)) != expected_target:
                    raise ValueError
            else:
                with backend.open_new(destination) as outgoing:
                    if (
                        _hash_stream(incoming, entry["size"], check, output=outgoing)
                        != entry["sha256"]
                    ):
                        raise ValueError
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
                    check()
                    outgoing.seek(0)
                    if (
                        _hash_stream(outgoing, entry["size"], lambda: check(external=False))
                        != entry["sha256"]
                    ):
                        raise ValueError
                    backend.set_mtime(outgoing, entry["mtime_ns"])
                    os.fsync(outgoing.fileno())
                    info = backend.admit_file(destination, created=True)
                    if info.st_size != entry["size"] or info.st_mtime_ns != entry["mtime_ns"]:
                        raise ValueError
                    _descriptor_signature(outgoing.fileno(), _signature(info))
                final = backend.admit_file(destination, created=True)
                if _signature(final) != _signature(info):
                    raise ValueError
            if _descriptor_signature(incoming.fileno(), signature) != opened_source:
                raise ValueError
            _same_source(source, signature)
            check(external=False)


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_uint32),
        ("descriptor", ctypes.c_void_p),
        ("inherit", ctypes.c_int32),
    ]


class _FileID(ctypes.Structure):
    _fields_ = [("volume", ctypes.c_uint64), ("identifier", ctypes.c_ubyte * 16)]


class _FileAttributes(ctypes.Structure):
    _fields_ = [("attributes", ctypes.c_uint32), ("tag", ctypes.c_uint32)]


def _known_folder(identifier):
    shell, ole = (
        ctypes.WinDLL("shell32", use_last_error=True),
        ctypes.WinDLL("ole32", use_last_error=True),
    )
    shell.SHGetKnownFolderPath.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    shell.SHGetKnownFolderPath.restype = ctypes.c_int32
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    result = ctypes.c_void_p()
    try:
        identifier = ctypes.create_string_buffer(identifier.bytes_le)
        if (
            shell.SHGetKnownFolderPath(identifier, 0, None, ctypes.byref(result))
            or not result.value
        ):
            raise ValueError
        path = Path(ctypes.wstring_at(result.value))
        if not path.is_absolute():
            raise ValueError
        return path
    finally:
        if result.value:
            ole.CoTaskMemFree(result)


class _WindowsStage:
    def __init__(self):
        if os.name != "nt":
            raise ValueError
        from ctypes import wintypes

        self.native = _native_security()
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.security = ctypes.WinDLL("advapi32", use_last_error=True)
        shell = ctypes.WinDLL("shell32", use_last_error=True)
        shell.IsUserAnAdmin.restype = wintypes.BOOL
        if not shell.IsUserAnAdmin():
            raise ValueError
        token, returned, elevated = ctypes.c_void_p(), ctypes.c_uint32(), ctypes.c_uint32()
        if not self.native.security.OpenProcessToken(
            self.native.kernel.GetCurrentProcess(), 0x8, ctypes.byref(token)
        ):
            raise ValueError
        try:
            if (
                not self.native.security.GetTokenInformation(
                    token, 20, ctypes.byref(elevated), 4, ctypes.byref(returned)
                )
                or elevated.value != 1
            ):
                raise ValueError
        finally:
            self.native.kernel.CloseHandle(token)
        sid_size, domain_size, use = ctypes.c_uint32(68), ctypes.c_uint32(256), ctypes.c_uint32()
        sid, domain = ctypes.create_string_buffer(68), ctypes.create_unicode_buffer(256)
        if not self.native.security.LookupAccountNameW(
            None,
            _ACCOUNT,
            sid,
            ctypes.byref(sid_size),
            domain,
            ctypes.byref(domain_size),
            ctypes.byref(use),
        ):
            raise ValueError
        self.service = self.native.sid(ctypes.addressof(sid))
        if re.fullmatch(r"S-1-5-80-(?:[0-9]+-){4}[0-9]+", self.service) is None:
            raise ValueError
        legacy = _known_folder(_LOCAL_DATA) / "K5VisionOperatorState" / "identity"
        identity_state._checked_path(legacy)
        # The fixed current profile uses the established TokenUser/BA/SY policy;
        # an elevated creator's private Python0700 directory can be BA-owned.
        identity_state._windows_admission(legacy, private=True, directory=True)
        self.program_data = _known_folder(_PROGRAM_DATA)
        self.root = self.program_data / "K5Vision"
        self._pins = []
        pointer, dword = ctypes.c_void_p, ctypes.c_uint32
        self.kernel.CreateFileW.argtypes = [
            ctypes.c_wchar_p,
            dword,
            dword,
            pointer,
            dword,
            dword,
            pointer,
        ]
        self.kernel.CreateFileW.restype = pointer
        self.kernel.CloseHandle.argtypes = [pointer]
        self.kernel.CloseHandle.restype = ctypes.c_int32
        self.kernel.GetFileInformationByHandleEx.argtypes = [
            pointer,
            ctypes.c_int32,
            pointer,
            dword,
        ]
        self.kernel.GetFileInformationByHandleEx.restype = ctypes.c_int32
        self.kernel.CreateDirectoryW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.POINTER(_SecurityAttributes),
        ]
        self.kernel.CreateDirectoryW.restype = ctypes.c_int32
        self.kernel.SetFileTime.argtypes = [pointer, pointer, pointer, pointer]
        self.kernel.SetFileTime.restype = ctypes.c_int32
        self.kernel.LocalFree.argtypes = [pointer]
        self.security.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            ctypes.c_wchar_p,
            dword,
            ctypes.POINTER(pointer),
            pointer,
        ]
        self.security.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = ctypes.c_int32
        self.security.GetSecurityDescriptorControl.argtypes = [
            pointer,
            ctypes.POINTER(ctypes.c_uint16),
            ctypes.POINTER(dword),
        ]
        self.security.GetSecurityDescriptorControl.restype = ctypes.c_int32

    def _descriptor(self, path):
        owner, dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        try:
            status = self.native.security.GetNamedSecurityInfoW(
                str(path),
                1,
                0x5,
                ctypes.byref(owner),
                None,
                ctypes.byref(dacl),
                None,
                ctypes.byref(descriptor),
            )
            if (
                status
                or not descriptor.value
                or not owner.value
                or not dacl.value
                or not self.native.security.IsValidAcl(dacl)
            ):
                raise ValueError
            control, revision = ctypes.c_uint16(), ctypes.c_uint32()
            if not self.security.GetSecurityDescriptorControl(
                descriptor, ctypes.byref(control), ctypes.byref(revision)
            ):
                raise ValueError
            header = ctypes.string_at(dacl, 8)
            size = int.from_bytes(header[2:4], "little")
            if not 8 <= size <= 65535:
                raise ValueError
            return (
                self.native.sid(owner.value),
                _acl_entries(ctypes.string_at(dacl, size)),
                bool(control.value & 0x1000),
            )
        finally:
            if descriptor.value:
                self.kernel.LocalFree(descriptor)

    def _admit_directory(self, path):
        info = _ordinary(path, directory=True)
        owner, entries, protected = self._descriptor(path)
        if path == self.root or self.root in path.parents:
            _target_policy(
                owner,
                entries,
                self.service,
                protected=protected,
                require_protected=path == self.root,
                directory=True,
            )
        else:
            _check_policy(
                owner,
                entries,
                self.service,
                private=False,
                require_file_inheritance=False,
                trusted_installer_verified=self.native.trusted_installer(),
                recorder_program_data=path == self.program_data,
            )
        return info

    def admit_file(self, path, *, created=False):
        info = _ordinary(path, directory=False)
        owner, entries, protected = self._descriptor(path)
        _target_policy(
            owner,
            entries,
            self.service,
            protected=protected,
            require_protected=created,
            directory=False,
        )
        return info

    def _handle_key(self, handle, *, directory):
        attributes, identifier = _FileAttributes(), _FileID()
        if not self.kernel.GetFileInformationByHandleEx(
            handle, 9, ctypes.byref(attributes), ctypes.sizeof(attributes)
        ) or not self.kernel.GetFileInformationByHandleEx(
            handle, 18, ctypes.byref(identifier), ctypes.sizeof(identifier)
        ):
            raise ValueError
        if bool(attributes.attributes & 0x10) != directory or attributes.attributes & 0x400:
            raise ValueError
        return identifier.volume, bytes(identifier.identifier)

    def _open_handle(self, path, *, access, share, create, directory=False, attributes=None):
        handle = self.kernel.CreateFileW(
            str(path),
            access,
            share,
            attributes,
            create,
            0x00200000 | (0x02000000 if directory else 0x80),
            None,
        )
        if not handle or handle == ctypes.c_void_p(-1).value:
            raise OSError(ctypes.get_last_error(), "Migration file access was refused")
        return handle

    def _path_key(self, path, *, directory):
        handle = self._open_handle(path, access=0x80, share=3, create=3, directory=directory)
        try:
            return self._handle_key(handle, directory=directory)
        finally:
            if not self.kernel.CloseHandle(handle):
                raise ValueError

    @contextmanager
    def _attributes(self, *, directory):
        descriptor = ctypes.c_void_p()
        flags = "OICI" if directory else ""
        sddl = f"O:BAD:P(A;{flags};FA;;;SY)(A;{flags};FA;;;BA)(A;{flags};FA;;;{self.service})"
        if not self.security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(descriptor), None
        ):
            raise ValueError
        try:
            yield _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
        finally:
            self.kernel.LocalFree(descriptor)

    @contextmanager
    def pin(self, transfer, area, parts):
        check_windows_volume(self.root)
        destination = self.root / ".migration" / transfer / area
        for part in parts:
            destination /= part
        paths = tuple(reversed((destination.parent, *destination.parent.parents)))
        try:
            for path in paths:
                if not path.exists():
                    if area != "recordings" or path != destination.parent or len(parts) != 2:
                        raise ValueError
                    with self._attributes(directory=True) as attributes:
                        if not self.kernel.CreateDirectoryW(str(path), ctypes.byref(attributes)):
                            raise ValueError
                self._admit_directory(path)
                handle = self._open_handle(path, access=0x20080, share=3, create=3, directory=True)
                try:
                    key = self._handle_key(handle, directory=True)
                except BaseException:
                    self.kernel.CloseHandle(handle)
                    raise
                self._pins.append((path, handle, key))
                self.recheck()
            yield destination
            self.recheck()
        finally:
            failed = False
            for _path, handle, _key in reversed(self._pins):
                failed |= not self.kernel.CloseHandle(handle)
            self._pins.clear()
            if failed:
                raise ValueError

    def recheck(self):
        for path, handle, key in self._pins:
            self._admit_directory(path)
            if (
                self._handle_key(handle, directory=True) != key
                or self._path_key(path, directory=True) != key
            ):
                raise ValueError

    @contextmanager
    def _stream(self, path, *, create=False, source=False):
        import msvcrt

        handle, descriptor = None, None
        try:
            if source:
                handle = self._open_handle(path, access=0x80000000, share=1, create=3)
            elif create:
                with self._attributes(directory=False) as attributes:
                    handle = self._open_handle(
                        path,
                        access=0xC0000000,
                        share=1,
                        create=1,
                        attributes=ctypes.byref(attributes),
                    )
            else:
                self.admit_file(path)
                handle = self._open_handle(path, access=0x80000000, share=1, create=3)
            key = self._handle_key(handle, directory=False)
            descriptor = msvcrt.open_osfhandle(
                handle, os.O_BINARY | (os.O_RDWR if create else os.O_RDONLY)
            )
            handle = None
            stream = os.fdopen(descriptor, "w+b" if create else "rb", buffering=0)
            descriptor = None
            with stream:
                if self._path_key(path, directory=False) != key:
                    raise ValueError
                yield stream
                if self._path_key(path, directory=False) != key:
                    raise ValueError
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if handle is not None and not self.kernel.CloseHandle(handle):
                raise ValueError

    def open_source(self, path):
        return self._stream(path, source=True)

    def open_new(self, path):
        return self._stream(path, create=True)

    def open_existing(self, path):
        return self._stream(path)

    def set_mtime(self, stream, value):
        import msvcrt

        if value % 100:
            raise ValueError
        ticks = value // 100 + _EPOCH_TICKS
        stamp = (ctypes.c_uint32 * 2)(ticks & 0xFFFFFFFF, ticks >> 32)
        if not self.kernel.SetFileTime(
            msvcrt.get_osfhandle(stream.fileno()), None, None, ctypes.byref(stamp)
        ):
            raise ValueError


def copy_to_service_stage(source, entry, transfer_id, *, checkpoint=None) -> None:
    """Copy under the elevated original owner; only fixed service paths exist."""
    failed = False
    try:
        backend = _WindowsStage()
        _copy_with_backend(source, entry, transfer_id, backend, checkpoint=checkpoint)
        return
    except Exception:
        failed = True
    if failed:
        raise MigrationCopyError("Legacy migration copy is unavailable")
