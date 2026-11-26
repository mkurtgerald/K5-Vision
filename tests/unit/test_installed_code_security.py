"""Machine code may be readable, but no ordinary user may mutate it."""

from __future__ import annotations

import os
import stat
from pathlib import PureWindowsPath
from types import SimpleNamespace

import pytest

from k5vision import installed_code_security as code
from k5vision import windows_identity_security as security

_ROOT = PureWindowsPath(r"C:\Program Files\K5 Vision")
_FILE = _ROOT / "workers" / "caddy.exe"
_USER = "S-1-5-21-1-2-3-1001"
_SERVICE = "S-1-5-80-123-456-789-111-222"
_READ = 0x001200A9


def _entry(sid, mask, *, flags=0, kind=0):
    return security.AccessEntry(kind, flags, mask, sid)


@pytest.fixture
def installed(monkeypatch):
    state = SimpleNamespace(descriptors={}, stats={}, snapshots={}, fallbacks=[], installer=True)
    for index, path in enumerate((_FILE, *_FILE.parents), start=1):
        state.stats[path] = SimpleNamespace(
            st_dev=4,
            st_ino=index,
            st_mode=(stat.S_IFREG if path == _FILE else stat.S_IFDIR) | 0o755,
            st_nlink=1,
            st_mtime_ns=123,
            st_size=45,
            st_file_attributes=0,
        )
        state.descriptors[path] = (
            security._ADMINISTRATORS,
            (_entry(security._ADMINISTRATORS, security._FILE_ALL), _entry(_USER, _READ)),
        )

    def snapshot(path):
        state.snapshots[path] = state.snapshots.get(path, 0) + 1
        return state.stats[path]

    def private(path):
        state.fallbacks.append(path)
        return "legacy-private-result"

    monkeypatch.setattr(code, "os", SimpleNamespace(name="nt", fspath=os.fspath))
    monkeypatch.setattr(code, "_installed_root", lambda: _ROOT)
    monkeypatch.setattr(code, "_snapshot", snapshot)
    monkeypatch.setattr(code, "check_windows_volume", lambda path: None)
    monkeypatch.setattr(code, "_private_regular_file", private)
    monkeypatch.setattr(
        code,
        "_native_security",
        lambda: SimpleNamespace(
            descriptor=lambda path: state.descriptors[PureWindowsPath(path)],
            trusted_installer=lambda: state.installer,
        ),
    )
    return state


def test_readable_installed_code_is_checked_twice_without_private_policy_or_acl_writes(installed):
    assert code.checked_executable_file(_FILE) is installed.stats[_FILE]
    assert all(count == 2 for count in installed.snapshots.values())
    assert not installed.fallbacks


@pytest.mark.parametrize("owner", [security._SYSTEM, security._ADMINISTRATORS, _USER, _SERVICE])
def test_code_owner_is_machine_authority_not_current_user_or_recorder(installed, owner):
    _, entries = installed.descriptors[_FILE]
    installed.descriptors[_FILE] = owner, entries
    if owner in {security._SYSTEM, security._ADMINISTRATORS}:
        code.checked_executable_file(_FILE)
    else:
        with pytest.raises(security.WindowsIdentitySecurityError):
            code.checked_executable_file(_FILE)
    assert not installed.fallbacks


@pytest.mark.parametrize("verified", [False, True])
def test_exact_trusted_installer_owner_requires_successful_native_resolution(installed, verified):
    installed.installer = verified
    installed.descriptors[_FILE] = (
        security._TRUSTED_INSTALLER,
        (_entry(security._TRUSTED_INSTALLER, security._FILE_ALL),),
    )
    if verified:
        code.checked_executable_file(_FILE)
    else:
        with pytest.raises(security.WindowsIdentitySecurityError):
            code.checked_executable_file(_FILE)


@pytest.mark.parametrize("path", [_FILE, _FILE.parent, _ROOT])
@pytest.mark.parametrize("mask", [2, 4, 0x10, 0x40, 0x10000, 0x40000, 0x40000000, 0x10000000])
def test_foreign_code_mutation_fails_even_with_matching_deny(installed, path, mask):
    owner, entries = installed.descriptors[path]
    installed.descriptors[path] = (
        owner,
        (
            *entries,
            _entry(_USER, mask, kind=1),
            _entry(_USER, mask),
        ),
    )
    with pytest.raises(security.WindowsIdentitySecurityError):
        code.checked_executable_file(_FILE)
    assert not installed.fallbacks


@pytest.mark.parametrize("path", [_FILE, _FILE.parent, _ROOT, _ROOT.parent])
def test_reparse_points_anywhere_in_code_ancestry_fail(installed, path):
    installed.stats[path].st_file_attributes = 0x400
    with pytest.raises(security.WindowsIdentitySecurityError):
        code.checked_executable_file(_FILE)


def test_code_hardlink_and_directory_leaf_fail(installed):
    installed.stats[_FILE].st_nlink = 2
    with pytest.raises(security.WindowsIdentitySecurityError):
        code.checked_executable_file(_FILE)
    installed.stats[_FILE].st_nlink = 1
    installed.stats[_FILE].st_mode = stat.S_IFDIR | 0o755
    with pytest.raises(security.WindowsIdentitySecurityError):
        code.checked_executable_file(_FILE)


@pytest.mark.parametrize("changes", [{"st_ino": 999}, {"st_file_attributes": 0x400}])
def test_leaf_replacement_during_acl_queries_cannot_be_admitted(installed, monkeypatch, changes):
    original = code._snapshot

    def changed(path):
        result = original(path)
        if path == _FILE and installed.snapshots[path] == 2:
            return SimpleNamespace(**{**vars(result), **changes})
        return result

    monkeypatch.setattr(code, "_snapshot", changed)
    with pytest.raises(security.WindowsIdentitySecurityError, match="changed"):
        code.checked_executable_file(_FILE)


@pytest.mark.parametrize(
    "path",
    [r"C:\Program Files\K5 Vision\..\foreign.exe", r"C:\Program Files\K5 Vision\x.exe:stream"],
)
def test_inside_boundary_aliases_fail_without_private_fallback(installed, path):
    with pytest.raises(security.WindowsIdentitySecurityError):
        code.checked_executable_file(PureWindowsPath(path))
    assert not installed.fallbacks


@pytest.mark.parametrize(
    "path", [r"C:\private\worker.exe", r"C:\Program Files\K5 Vision-other\worker.exe"]
)
def test_outside_fixed_installation_keeps_original_private_admission(installed, path):
    value = PureWindowsPath(path)
    assert code.checked_executable_file(value) == "legacy-private-result"
    assert installed.fallbacks == [value] and not installed.snapshots


def test_non_windows_keeps_original_private_admission(installed, monkeypatch):
    monkeypatch.setattr(code, "os", SimpleNamespace(name="posix"))
    assert code.checked_executable_file(_FILE) == "legacy-private-result"
