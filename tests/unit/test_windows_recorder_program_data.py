"""The observed shared-container metadata grant never admits private data writers."""

from __future__ import annotations

import ctypes
from pathlib import PureWindowsPath
from types import SimpleNamespace

import pytest

from k5vision import windows_identity_security as security

SERVICE = "S-1-5-80-1-2-3-4-5"
OTHER = "S-1-5-21-1-2-3-1001"
PROGRAM_DATA = PureWindowsPath(r"C:\ProgramData")
ERROR = security.WindowsIdentitySecurityError


def _entry(sid, mask, flags=0x02):
    return security.AccessEntry(0, flags, mask, sid)


def _observed():
    # Exact offending effective Users ACE captured from the actual Windows 2025
    # VM: mask278 (WRITE_DATA|APPEND_DATA|WRITE_EA|WRITE_ATTRIBUTES), CI, not IO.
    return (
        _entry(security._SYSTEM, security._FILE_ALL, 3),
        _entry(security._ADMINISTRATORS, security._FILE_ALL, 3),
        _entry(security._USERS, 0x116),
    )


def _policy(entries, *, scoped=False, private=False):
    security._check_policy(
        security._SYSTEM,
        entries,
        SERVICE,
        private=private,
        require_file_inheritance=private,
        trusted_installer_verified=False,
        recorder_program_data=scoped,
    )


def test_actual_observed_container_ace_requires_explicit_recorder_scope():
    with pytest.raises(ERROR):
        _policy(_observed())
    _policy(_observed(), scoped=True)


@pytest.mark.parametrize("flags", [0, 1, 2, 3, 0x0B])
def test_private_identity_and_immediate_parent_never_accept_users_metadata(flags):
    entries = (*_observed()[:2], _entry(security._USERS, 0x116, flags))
    with pytest.raises(ERROR):
        _policy(entries, scoped=True, private=True)


@pytest.mark.parametrize(
    "mask", [0x40, 0x10000, 0x40000, 0x80000, 0x01000000, 0x40000000, 0x10000000]
)
def test_scoped_container_still_rejects_namespace_security_and_generic_writes(mask):
    entries = (*_observed()[:2], _entry(security._USERS, 0x116 | mask))
    with pytest.raises(ERROR):
        _policy(entries, scoped=True)


@pytest.mark.parametrize("sid", ["S-1-1-0", "S-1-5-11", OTHER])
@pytest.mark.parametrize("mask", [0x10, 0x100])
def test_other_principals_do_not_borrow_users_metadata_exception(sid, mask):
    with pytest.raises(ERROR):
        _policy((*_observed(), _entry(sid, mask)), scoped=True)


@pytest.fixture
def native(monkeypatch):
    state = SimpleNamespace(
        user=SERVICE, resolved=SERVICE, lookup=True, calls=[], entries=_observed()
    )

    def lookup(system, account, *_args):
        state.calls.append((system, account))
        return state.lookup

    api = SimpleNamespace(
        security=SimpleNamespace(LookupAccountNameW=lookup),
        sid=lambda _pointer: state.resolved,
        current_user=lambda: state.user,
        descriptor=lambda _path: (security._SYSTEM, state.entries),
        trusted_installer=lambda: False,
    )
    monkeypatch.setattr(security, "_native_security", lambda: api)
    monkeypatch.setattr(security, "_known_program_data", lambda: PROGRAM_DATA)
    return state


def test_public_admission_uses_exact_virtual_token_and_native_known_folder(native, monkeypatch):
    monkeypatch.setenv("ProgramData", r"C:\UntrustedContainer")
    security.check_windows_security(str(PROGRAM_DATA), private=False)
    assert native.calls == [(None, "NT SERVICE\\K5VisionRecorder")]
    for path in (
        r"C:\UntrustedContainer",
        r"C:\AnotherContainer",
        r"C:\ProgramData\K5Vision",
        r"C:\ProgramData\K5Vision\identity",
        r"C:\ProgramData\..\ProgramData",
    ):
        with pytest.raises(ERROR):
            security.check_windows_security(path, private=False)


@pytest.mark.parametrize("identity", [OTHER, "S-1-5-19", "S-1-5-18", "S-1-5-80-9-8-7-6-5"])
def test_group_or_different_account_cannot_enable_the_native_scope(native, identity):
    native.user = identity
    with pytest.raises(ERROR):
        security.check_windows_security(str(PROGRAM_DATA), private=False)


def test_private_admission_never_queries_for_a_container_exception(native):
    with pytest.raises(ERROR):
        security.check_windows_security(
            str(PROGRAM_DATA), private=True, require_file_inheritance=True
        )
    assert native.calls == []


def test_unresolved_principal_or_known_folder_remains_strict(native, monkeypatch):
    native.lookup = False
    with pytest.raises(ERROR):
        security.check_windows_security(str(PROGRAM_DATA), private=False)
    native.lookup = True

    def unavailable():
        raise ERROR("native detail must not establish trust")

    monkeypatch.setattr(security, "_known_program_data", unavailable)
    with pytest.raises(ERROR):
        security.check_windows_security(str(PROGRAM_DATA), private=False)


class _Function:
    def __init__(self, invoke):
        self.invoke = invoke

    def __call__(self, *args):
        return self.invoke(*args)


def test_native_known_folder_is_read_only_and_frees_the_returned_buffer(monkeypatch):
    value = ctypes.create_unicode_buffer(str(PROGRAM_DATA))
    released = []

    def known(identifier, flags, token, result):
        assert bytes(identifier)[:16] == bytes.fromhex("825dab62c1fdc34da9dd070d1d495d97")
        assert flags == 0 and token is None
        result._obj.value = ctypes.addressof(value)
        return 0

    shell = SimpleNamespace(SHGetKnownFolderPath=_Function(known))
    ole = SimpleNamespace(CoTaskMemFree=_Function(lambda pointer: released.append(pointer.value)))
    monkeypatch.setattr(
        ctypes, "WinDLL", lambda name, **_kwargs: shell if name == "shell32" else ole, raising=False
    )
    assert security._known_program_data() == PROGRAM_DATA
    assert released == [ctypes.addressof(value)]
