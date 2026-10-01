"""Portable native-adapter/policy regressions; never change Windows permissions."""

from __future__ import annotations

import ctypes
import os
import secrets
import stat
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from k5vision import identity_state as identity
from k5vision import windows_identity_security as security

USER = "S-1-5-21-1-2-3-1001"
FOREIGN = "S-1-5-21-1-2-3-1002"
EVERYONE = "S-1-1-0"
FULL = 0x001F01FF
ERROR = security.WindowsIdentitySecurityError


def sid_bytes(sid):
    parts = sid.split("-")
    values = [int(value) for value in parts[3:]]
    return (
        bytes((1, len(values)))
        + int(parts[2]).to_bytes(6, "big")
        + struct.pack(f"<{len(values)}I", *values)
    )


def ace(sid=USER, mask=FULL, flags=3, kind=0):
    return security.AccessEntry(kind, flags, mask, sid)


def acl_bytes(*entries):
    rows = []
    for entry in entries:
        sid = sid_bytes(entry.sid)
        rows.append(struct.pack("<BBHI", entry.kind, entry.flags, len(sid) + 8, entry.mask) + sid)
    body = b"".join(rows)
    return struct.pack("<BBHHH", 2, 0, len(body) + 8, len(rows), 0) + body


def check(entries=None, owner=USER, private=True, inherit=True, installer=False):
    security._check_policy(
        owner,
        tuple(entries if entries is not None else [ace()]),
        USER,
        private=private,
        require_file_inheritance=inherit,
        trusted_installer_verified=installer,
    )


@pytest.mark.parametrize("owner", [USER, security._ADMINISTRATORS, security._SYSTEM])
def test_current_user_admin_system_and_owner_rights_are_private(owner):
    check([ace(security._OWNER_RIGHTS)], owner=owner)


@pytest.mark.parametrize("owner", [FOREIGN, EVERYONE, "S-1-5-80-1-2-3-4-5"])
@pytest.mark.parametrize("private", [False, True])
def test_foreign_owner_cannot_borrow_owner_rights(owner, private):
    with pytest.raises(ERROR):
        check([ace(security._OWNER_RIGHTS)], owner=owner, private=private, installer=True)


@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("verified", [False, True])
def test_trusted_installer_exception_is_ancestor_only_and_verified(private, verified):
    kwargs = dict(
        owner=security._TRUSTED_INSTALLER, private=private, inherit=False, installer=verified
    )
    if not private and verified:
        check([ace(security._TRUSTED_INSTALLER)], **kwargs)
    else:
        with pytest.raises(ERROR):
            check([ace(security._TRUSTED_INSTALLER)], **kwargs)


@pytest.mark.parametrize("flags", [0, 3, 0x13, 0x0B])
@pytest.mark.parametrize("mask", [1, 2, 4, 0x40, 0x80, 0x10000, 0x40000, 0x10000000])
def test_foreign_effective_or_inheritable_private_grants_fail(flags, mask):
    with pytest.raises(ERROR):
        check([ace(), ace(EVERYONE, mask=mask, flags=flags)])


def test_deny_does_not_cancel_foreign_allow_or_grant_file_inheritance():
    with pytest.raises(ERROR):
        check([ace(EVERYONE, kind=1), ace(EVERYONE)])
    with pytest.raises(ERROR):
        check([ace(kind=1)])


@pytest.mark.parametrize(
    "mask", [0x10, 0x40, 0x100, 0x10000, 0x40000, 0x80000, 0x40000000, 0x10000000, 0x01000000]
)
def test_ancestor_replacement_and_permission_writes_fail(mask):
    with pytest.raises(ERROR):
        check([ace(EVERYONE, mask=mask)], private=False, inherit=False)


def test_nonempty_ancestor_add_child_read_and_inherit_only_are_distinct():
    check([ace(EVERYONE, mask=0x001200AF), ace(EVERYONE, flags=0x0B)], private=False, inherit=False)
    with pytest.raises(ERROR):
        check([ace(), ace(EVERYONE, mask=2)])  # Immediate private parent cannot grant ADD_FILE.


def test_file_inheritance_must_be_present_private_and_usable():
    for entry in (ace(flags=0), ace(flags=2), ace(mask=1), ace(mask=0)):
        with pytest.raises(ERROR):
            check([entry])
    check([ace(mask=0x10000000)])
    check([ace(security._CREATOR_OWNER, flags=0x0B)])
    with pytest.raises(ERROR):
        check([ace(security._CREATOR_OWNER)])
    with pytest.raises(ERROR):
        check([ace("S-1-3-1", flags=0x0B)])


def test_valid_acl_roundtrip_and_generic_mapping():
    entries = (ace(), ace(EVERYONE, kind=1), ace(security._OWNER_RIGHTS))
    assert security._acl_entries(acl_bytes(*entries)) == entries
    assert security._expanded_mask(0x80000000) == 0x00120089
    assert security._expanded_mask(0x40000000) == 0x00120116
    assert security._expanded_mask(0x20000000) == 0x001200A0
    assert security._expanded_mask(0x10000000) == FULL


@pytest.mark.parametrize(
    "data", [b"", b"\0" * 7, b"\2\0" + b"\0" * 6, b"\1\20" + b"\0" * 64, b"\1\1" + b"\0" * 6]
)
def test_malformed_sid_refused(data):
    with pytest.raises(ERROR):
        security._sid_bytes(data)


@pytest.mark.parametrize(
    "offset,value",
    [(0, 1), (1, 1), (2, 7), (4, 2), (6, 1), (8, 5), (9, 0x80), (10, 15), (16, 2), (17, 16)],
)
def test_malformed_acl_and_unsupported_aces_refused(offset, value):
    data = bytearray(acl_bytes(ace()))
    data[offset] = value
    with pytest.raises(ERROR):
        security._acl_entries(bytes(data))


def test_unsupported_masks_flags_padding_and_truncation_refused():
    for data in (
        acl_bytes(ace(mask=0x02000000)),
        acl_bytes(ace(flags=8)),
        acl_bytes(ace())[:-1],
        b"",
        b"\0" * 7,
    ):
        with pytest.raises(ERROR):
            security._acl_entries(data)


class Function:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


def set_pointer(target, value):
    ctypes.cast(target, ctypes.POINTER(ctypes.c_void_p))[0] = value


def set_dword(target, value):
    ctypes.cast(target, ctypes.POINTER(ctypes.c_uint32))[0] = value


@pytest.fixture
def native(monkeypatch):
    state = SimpleNamespace(
        drive=3,
        mapping=r"\Device\HarddiskVolume4",
        lookup=True,
        descriptor_error=0,
        null_dacl=False,
        valid_acl=True,
        valid_sid=True,
        open_token=True,
        read_token=True,
        token_size=16,
        query=True,
        freed=[],
        closed=[],
    )
    user = ctypes.create_string_buffer(sid_bytes(USER))
    installer = ctypes.create_string_buffer(sid_bytes(security._TRUSTED_INSTALLER))
    acl = ctypes.create_string_buffer(acl_bytes(ace()))
    state.owner = ctypes.addressof(user)
    state.sid_lengths = {ctypes.addressof(user): len(sid_bytes(USER))}

    def token_info(token, kind, buffer, size, length):
        set_dword(length, state.token_size)
        if buffer is None or not state.read_token:
            return 0
        ctypes.memmove(
            buffer, struct.pack("P", ctypes.addressof(user)), ctypes.sizeof(ctypes.c_void_p)
        )
        return 1

    def open_token(process, rights, token):
        set_pointer(token, 123)
        return state.open_token

    def named(path, kind, flags, owner, group, dacl, sacl, descriptor):
        set_pointer(owner, state.owner)
        set_pointer(dacl, None if state.null_dacl else ctypes.addressof(acl))
        set_pointer(descriptor, 456)
        return state.descriptor_error

    def lookup(system, account, sid, sid_size, domain, domain_size, use):
        assert system is None and account == "NT SERVICE\\TrustedInstaller"
        ctypes.memmove(sid, installer, len(sid_bytes(security._TRUSTED_INSTALLER)))
        state.sid_lengths[ctypes.addressof(sid)] = 8 + sid.raw[1] * 4
        return state.lookup

    def mapping(drive, buffer, length):
        assert drive == "C:"
        buffer.value = state.mapping
        return len(state.mapping) + 2 if state.query else 0

    kernel = SimpleNamespace(
        GetCurrentProcess=Function(lambda: -1),
        CloseHandle=Function(lambda x: state.closed.append(x.value) or 1),
        LocalFree=Function(lambda x: state.freed.append(x.value)),
        GetDriveTypeW=Function(lambda root: state.drive),
        QueryDosDeviceW=Function(mapping),
    )
    advapi = SimpleNamespace(
        OpenProcessToken=Function(open_token),
        GetTokenInformation=Function(token_info),
        IsValidSid=Function(lambda p: state.valid_sid),
        GetLengthSid=Function(lambda p: state.sid_lengths.get(p, 8)),
        IsValidAcl=Function(lambda p: state.valid_acl),
        GetNamedSecurityInfoW=Function(named),
        LookupAccountNameW=Function(lookup),
    )
    monkeypatch.setattr(security, "os", SimpleNamespace(name="nt", fspath=os.fspath))
    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda name, **kwargs: kernel if name == "kernel32" else advapi,
        raising=False,
    )
    state.api = security._NativeSecurity()
    state.buffers = (user, installer, acl)
    return state


def test_native_adapter_queries_and_releases_only_owned_allocations(native):
    assert native.api.current_user() == USER
    assert native.closed == [123]
    assert native.api.descriptor(r"C:\private") == (USER, (ace(),))
    assert native.freed == [456]
    assert native.api.trusted_installer()
    security.check_windows_volume(r"C:\private\identity")
    security.check_windows_security(r"C:\private", private=True, require_file_inheritance=True)
    native.owner = ctypes.addressof(native.buffers[1])
    native.sid_lengths[native.owner] = len(sid_bytes(security._TRUSTED_INSTALLER))
    security.check_windows_security("C:\\", private=False)
    with pytest.raises(ERROR):
        security.check_windows_security(r"C:\private", private=True)


@pytest.mark.parametrize(
    "field,value",
    [("descriptor_error", 5), ("null_dacl", True), ("valid_acl", False), ("valid_sid", False)],
)
def test_native_descriptor_failure_is_closed_and_freed(native, field, value):
    setattr(native, field, value)
    with pytest.raises(ERROR):
        native.api.descriptor(r"C:\private")
    assert native.freed == [456]


@pytest.mark.parametrize(
    "field,value",
    [("open_token", False), ("read_token", False), ("token_size", 0), ("token_size", 10000)],
)
def test_native_token_failure_is_closed(native, field, value):
    setattr(native, field, value)
    with pytest.raises(ERROR):
        native.api.current_user()
    assert native.closed == ([] if field == "open_token" else [123])


def test_native_unresolved_wrong_service_and_invalid_sid_fail(native):
    native.lookup = False
    assert not native.api.trusted_installer()
    native.lookup = True
    native.buffers[1].raw = sid_bytes(FOREIGN).ljust(len(native.buffers[1]), b"\0")
    assert not native.api.trusted_installer()
    with pytest.raises(ERROR):
        native.api.sid(None)
    native.sid_lengths[123] = 1000
    with pytest.raises(ERROR):
        native.api.sid(123)


@pytest.mark.parametrize(
    "path",
    [
        "relative",
        r"C:relative",
        r"\relative",
        r"\\server\share\identity",
        r"\\?\C:\identity",
        r"C:\parent\..\identity",
        r"C:\private\NUL",
        r"C:\NUL\identity",
        "C:\\private\\trailing ",
        r"C:\private\file:stream",
    ],
)
def test_ambiguous_paths_fail_before_native_queries(monkeypatch, path):
    monkeypatch.setattr(security, "_NativeSecurity", lambda: pytest.fail("native query attempted"))
    with pytest.raises(ERROR):
        security.check_windows_volume(path)


@pytest.mark.parametrize("drive", [0, 1, 2, 4, 5, 6])
def test_nonfixed_drives_refused(native, drive):
    native.drive = drive
    with pytest.raises(ERROR):
        security.check_windows_volume(r"C:\identity")


@pytest.mark.parametrize(
    "mapping",
    [
        r"\??\D:\hidden",
        r"\Device\LanmanRedirector\server\share",
        r"\Device\HarddiskVolume4\hidden",
        "",
        r"\Device\HarddiskVolumeX",
    ],
)
def test_alias_remote_and_unknown_mapping_refused(native, mapping):
    native.mapping = mapping
    with pytest.raises(ERROR):
        security.check_windows_volume(r"C:\identity")


def test_volume_query_failure_and_nonwindows_adapter_fail(native, monkeypatch):
    native.query = False
    with pytest.raises(ERROR):
        security.check_windows_volume(r"C:\identity")
    monkeypatch.setattr(security, "os", SimpleNamespace(name="posix"))
    with pytest.raises(ERROR):
        security._NativeSecurity()


def test_identity_native_volume_refuses_before_any_state_access(tmp_path, monkeypatch):
    monkeypatch.setattr(identity, "_WINDOWS", True)
    monkeypatch.setattr(identity, "check_windows_volume", lambda p: (_ for _ in ()).throw(ERROR()))
    with pytest.raises(identity.IdentityStateError):
        identity.validate_new_identity_directory(tmp_path / "absent")
    assert not (tmp_path / "absent").exists()


def test_identity_private_parent_directory_and_files_use_strict_policy(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(identity, "_WINDOWS", True)
    monkeypatch.setattr(identity, "check_windows_volume", lambda p: None)
    monkeypatch.setattr(identity, "check_windows_security", lambda p, **kw: calls.append((p, kw)))
    path = tmp_path / "identity"
    identity.validate_new_identity_directory(path)
    assert calls[-1] == (tmp_path, {"private": True, "require_file_inheritance": True})
    path.mkdir(mode=0o700)
    identity._same_directory(path, path.stat())
    assert calls[-2:] == [
        (tmp_path, {"private": True, "require_file_inheritance": True}),
        (path, {"private": True, "require_file_inheritance": True}),
    ]
    item = path / "empty"
    item.touch()
    identity._regular_file(item)
    assert calls[-1] == (item, {"private": True, "require_file_inheritance": False})


def test_identity_native_security_failure_is_sanitized(tmp_path, monkeypatch):
    monkeypatch.setattr(identity, "_WINDOWS", True)
    monkeypatch.setattr(identity, "check_windows_volume", lambda p: None)
    monkeypatch.setattr(
        identity, "check_windows_security", lambda *a, **kw: (_ for _ in ()).throw(ERROR())
    )
    with pytest.raises(identity.IdentityStateError, match="unsafe or unsupported"):
        identity.validate_new_identity_directory(tmp_path / "absent")
    assert not (tmp_path / "absent").exists()


def test_immediate_parent_refused_before_leaf_probe(tmp_path, monkeypatch):
    path = tmp_path / "absent"
    lstat = Path.lstat

    def inspect_path(value, *args, **kwargs):
        if value == path:
            pytest.fail("untrusted parent was traversed before private admission")
        return lstat(value, *args, **kwargs)

    def inspect_security(value, *, private, **kwargs):
        if value == tmp_path and private:
            raise ERROR()

    monkeypatch.setattr(identity, "_WINDOWS", True)
    monkeypatch.setattr(identity, "check_windows_volume", lambda p: None)
    monkeypatch.setattr(identity, "check_windows_security", inspect_security)
    monkeypatch.setattr(Path, "lstat", inspect_path)
    with pytest.raises(identity.IdentityStateError):
        identity.validate_new_identity_directory(path)


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
def test_dangling_junction_sidecars_refused_before_sqlite_open(tmp_path, monkeypatch, suffix):
    state = identity.initialize_identity_state(
        tmp_path / "identity", username="recovery-admin", password=secrets.token_urlsafe(24)
    )
    sidecar = Path(str(state.database_path) + suffix)
    lstat = Path.lstat

    def inspect_path(value, *args, **kwargs):
        if value == sidecar:
            # A dangling Windows junction exists to lstat but has no stat target
            # and does not set the S_IFLNK bit used by Path.is_symlink().
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_file_attributes=0x400)
        return lstat(value, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", inspect_path)
    monkeypatch.setattr(identity.sqlite3, "connect", lambda *a, **kw: pytest.fail("SQLite opened"))
    with pytest.raises(identity.IdentityStateError, match="linked identity"):
        identity.load_identity_state(state.directory)
