from __future__ import annotations

import ctypes
import os
from pathlib import Path

import pytest
import test_operator_storage_settings as storage_helpers

from k5vision import identity_state
from k5vision.operator_storage_directory import (
    StorageContainerError,
    prepare_private_recording_container,
)

storage = storage_helpers.storage


def _readable_parent(path: Path):
    if os.name == "posix":
        path.mkdir(mode=0o755)
        path.chmod(0o755)
        return
    from ctypes import wintypes

    from k5vision.windows_identity_security import _NativeSecurity

    class Attributes(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.DWORD),
            ("descriptor", ctypes.c_void_p),
            ("inherit", wintypes.BOOL),
        ]

    native = _NativeSecurity()
    sid = native.current_user()
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
    kernel.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(Attributes)]
    kernel.CreateDirectoryW.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    descriptor = ctypes.c_void_p()
    sddl = f"O:{sid}D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;{sid})(A;OICI;0x1200a9;;;BU)"
    assert security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, 1, ctypes.byref(descriptor), None
    )
    try:
        assert kernel.CreateDirectoryW(
            str(path), ctypes.byref(Attributes(ctypes.sizeof(Attributes), descriptor, False))
        )
    finally:
        kernel.LocalFree(descriptor)


def _permission_identity(path):
    if os.name == "posix":
        value = path.stat()
        return value.st_uid, value.st_gid, value.st_mode
    from k5vision.windows_identity_security import _NativeSecurity

    return _NativeSecurity().descriptor(str(path))


def test_readable_parent_gets_new_private_child_without_acl_or_unknown_file_changes(storage):
    settings, _root, recordings, parent = storage
    selected = parent / "readable shared folder"
    _readable_parent(selected)
    unknown = selected / "unrelated.txt"
    unknown.write_text("preserve unrelated data")
    original_permissions = _permission_identity(selected)
    changed = settings.configure(expected_revision=0, recording_directory=str(selected))
    target = Path(changed["pending_recording_directory"])
    assert target.is_dir() and target.name == "recordings"
    assert target.parent.parent == selected and target.parent.name.startswith("K5Vision-")
    assert _permission_identity(selected) == original_permissions
    assert unknown.read_text() == "preserve unrelated data"
    assert (
        settings.recording_directory == recordings
        and changed["location_status"] == "restart_required"
    )
    identity_state._checked_path(target)
    identity_state._windows_admission(target, private=True, directory=True)
    if os.name == "posix":
        assert target.stat().st_mode & 0o077 == 0 and target.parent.stat().st_mode & 0o077 == 0


def test_new_named_folder_under_readable_parent_is_created_private_and_activates_copy_first(
    storage,
):
    settings, _root, recordings, parent = storage
    original_files = storage_helpers._recordings(recordings)
    selected = parent / "readable parent"
    _readable_parent(selected)
    requested = selected / "New recording location"
    changed = settings.configure(expected_revision=0, recording_directory=str(requested))
    target = Path(changed["pending_recording_directory"])
    assert target == requested / "recordings" and target.is_dir()
    assert settings.migrate_pending() == target
    for original in original_files:
        assert (
            original.is_file()
            and (target / original.relative_to(recordings)).read_bytes() == original.read_bytes()
        )


def test_bad_policy_does_not_create_a_private_container_as_side_effect(storage):
    settings, _root, _recordings, parent = storage
    selected = parent / "readable parent"
    _readable_parent(selected)
    requested = selected / "must remain absent"
    with pytest.raises(ValueError):
        settings.configure(expected_revision=0, recording_directory=str(requested), quota_bytes=-1)
    assert not requested.exists()


def test_foreign_writable_or_linked_parent_is_refused_without_creation(storage):
    settings, _root, _recordings, parent = storage
    selected = parent / "unsafe parent"
    if os.name == "posix":
        selected.mkdir(mode=0o777)
        selected.chmod(0o777)
    else:
        pytest.skip("POSIX foreign-writer regression; Windows ACL mutation is separately admitted")
    with pytest.raises(StorageContainerError):
        prepare_private_recording_container(
            str(selected / "recordings"), settings._site_id, protected=()
        )
    assert list(selected.iterdir()) == []


def test_no_alternate_container_for_private_directory_or_protected_overlap(storage):
    settings, root, recordings, parent = storage
    selected = parent / "already private"
    selected.mkdir(mode=0o700)
    assert (
        prepare_private_recording_container(
            str(selected), settings._site_id, protected=(root, recordings)
        )
        is None
    )
    for value in (root, recordings, recordings / "nested"):
        with pytest.raises(StorageContainerError):
            prepare_private_recording_container(
                str(value), settings._site_id, protected=(root, recordings)
            )
    assert list(selected.iterdir()) == []


def test_protected_container_failure_preserves_unknown_or_raced_contents(storage, monkeypatch):
    import k5vision.operator_storage_directory as directories

    settings, _root, _recordings, parent = storage
    selected = parent / "readable parent"
    _readable_parent(selected)
    requested = selected / "new container"
    original = directories._create_private

    def create(path):
        original(path)
        if path == requested:
            (path / "unknown.keep").write_text("never erase")
        else:
            raise OSError("simulated allocation failure")

    monkeypatch.setattr(directories, "_create_private", create)
    with pytest.raises(StorageContainerError):
        prepare_private_recording_container(str(requested), settings._site_id, protected=())
    assert (requested / "unknown.keep").read_text() == "never erase"


def test_loading_pending_target_is_read_only_and_does_not_prepare_an_alternate_container(
    storage, monkeypatch
):
    settings, root, recordings, parent = storage
    selected = parent / "readable parent"
    _readable_parent(selected)
    changed = settings.configure(expected_revision=0, recording_directory=str(selected))
    before = set(selected.iterdir())
    from k5vision.operator_storage_settings import OperatorStorageSettings

    restored = OperatorStorageSettings(root, settings._site_id, recordings)
    assert restored.snapshot() == changed and set(selected.iterdir()) == before
