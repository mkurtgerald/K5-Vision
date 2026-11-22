"""Opt-in real Win32 I/O qualification on the installed disposable test site.

This is deliberately one named test: the native workflow must admit its PASS,
not merely a successful pytest process containing a platform/gate skip. It uses
the elevated original profile owner, never a simulated service token.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import shutil
from contextlib import contextmanager
from uuid import uuid4

import pytest

from k5vision import legacy_migration_copy as copy

_GATE = "K5_NATIVE_MIGRATION_COPY_ACCEPTANCE"
_STAMP = 1760000000123456700  # Deliberately retains sub-microsecond, 100 ns data.
_NAME = "2026-11-22_02-00-00-000000_+0530.mp4"
_CAMERA = "k5_" + "a" * 64


def _mkdir(stage, path, *, owner, protected=True, foreign=False, service_access=True):
    """Create a new owned fixture directory; never change an existing ACL."""
    assert not os.path.lexists(path), "The native fixture must not adopt an existing path"
    trustees = (copy._SY, copy._BA)
    if service_access:
        trustees += (stage.service,)
    if owner not in trustees:
        trustees += (owner,)
    dacl = "D:P" if protected else "D:"
    sddl = "O:" + owner + dacl + "".join(f"(A;OICI;FA;;;{sid})" for sid in trustees)
    if foreign:
        sddl += "(A;OICI;FR;;;BU)"
    descriptor = ctypes.c_void_p()
    assert stage.security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, 1, ctypes.byref(descriptor), None
    ), "Fixture descriptor construction failed"
    try:
        attributes = copy._SecurityAttributes(
            ctypes.sizeof(copy._SecurityAttributes), descriptor, False
        )
        assert stage.kernel.CreateDirectoryW(str(path), ctypes.byref(attributes)), (
            "Fixture CREATE_NEW directory failed",
            ctypes.get_last_error(),
        )
    finally:
        stage.kernel.LocalFree(descriptor)
    return stage._path_key(path, directory=True)


def _same_key(stage, path, key):
    return stage._path_key(path, directory=True) == key


def _remove_owned_tree(stage, path, key):
    assert _same_key(stage, path, key), "Refusing cleanup after fixture identity changed"
    shutil.rmtree(path)
    assert not os.path.lexists(path)


def _metadata(source, *, area="recordings", relative=None):
    info = source.stat()
    return {
        "area": area,
        "relative": relative or (_CAMERA + "/" + source.name),
        "size": info.st_size,
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "mtime_ns": info.st_mtime_ns,
    }


@contextmanager
def _owned_native_fixture():
    assert os.environ.get("GITHUB_ACTIONS") == "true", (
        "Only the disposable GitHub workflow may opt in"
    )
    assert os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted", (
        "Self-hosted or production machines must never run this qualification"
    )
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    shell.IsUserAnAdmin.argtypes = []
    shell.IsUserAnAdmin.restype = ctypes.c_int
    assert shell.IsUserAnAdmin(), "The configured qualification requires an elevated original owner"
    assert ctypes.sizeof(ctypes.c_void_p) == 8, "Qualification requires native x64 Python"
    # Real constructor checks the elevated token, exact service-account lookup,
    # fixed known-folder profile and actual current legacy identity owner.
    stage = copy._WindowsStage()
    stage._admit_directory(stage.root)
    legacy = copy._known_folder(copy._LOCAL_DATA) / "K5VisionOperatorState"
    assert legacy.is_dir() and (legacy / "identity").is_dir(), "Generated old GUI site is required"
    root_key = stage._path_key(stage.root, directory=True)
    legacy_key = stage._path_key(legacy, directory=True)
    transfer = str(uuid4())
    container = stage.root / ".migration"
    destination = container / transfer
    source_root = legacy / ("native-copy-fixture-" + transfer)
    source_key = destination_key = container_key = None
    made_container = False
    reparse = None

    def track_link(path):
        nonlocal reparse
        reparse = path

    try:
        if not os.path.lexists(container):
            container_key = _mkdir(stage, container, owner=copy._BA, protected=False)
            made_container = True
        stage._admit_directory(container)
        destination_key = _mkdir(stage, destination, owner=copy._BA, protected=False)
        for area in ("identity", "recordings"):
            _mkdir(stage, destination / area, owner=copy._BA, protected=False)
        source_key = _mkdir(
            stage, source_root, owner=stage.native.current_user(), service_access=False
        )
        # Source admission must remain the ordinary original-user policy.
        source = source_root / _NAME
        source.write_bytes((b"native retained surveillance bytes\x00" * 65536) + b"END")
        with source.open("r+b", buffering=0) as stream:
            stage.set_mtime(stream, _STAMP)
            os.fsync(stream.fileno())
        assert source.stat().st_mtime_ns == _STAMP
        yield stage, transfer, destination, source_root, source, track_link
    finally:
        if reparse is not None and os.path.lexists(reparse):
            os.rmdir(reparse)
        if source_key is not None:
            _remove_owned_tree(stage, source_root, source_key)
        if destination_key is not None:
            # Python removes directory links/junctions themselves rather than
            # walking their target; the test also removes its link explicitly.
            _remove_owned_tree(stage, destination, destination_key)
        if made_container:
            assert container_key is not None and _same_key(stage, container, container_key)
            assert not tuple(container.iterdir()), "Preserve a container populated by another actor"
            container.rmdir()
        assert _same_key(stage, stage.root, root_key), "Existing service root identity changed"
        assert _same_key(stage, legacy, legacy_key), "Existing legacy base identity changed"


@pytest.mark.skipif(os.name != "nt", reason="Requires real Windows security and file APIs")
def test_native_migration_copy_windows_actual_security_and_io(record_property):
    if os.environ.get(_GATE) != "1":
        pytest.skip("Requires explicit disposable native migration copy acceptance")
    with _owned_native_fixture() as (stage, transfer, destination, source_root, source, track_link):
        entry = _metadata(source)
        original = source.read_bytes()
        before = copy._signature(source.stat())
        source_acl = stage.native.descriptor(source)
        # Explicit private root + genuine inherited descendant descriptors.
        owner, entries, protected = stage._descriptor(stage.root)
        copy._target_policy(
            owner,
            entries,
            stage.service,
            protected=protected,
            require_protected=True,
            directory=True,
        )
        child_owner, child_entries, child_protected = stage._descriptor(destination)
        assert child_owner == copy._BA and not child_protected
        assert any(ace.flags & 0x10 for ace in child_entries), (
            "Actual inherited ACLs must be exercised"
        )
        copy._target_policy(
            child_owner,
            child_entries,
            stage.service,
            protected=False,
            require_protected=False,
            directory=True,
        )
        copy.copy_to_service_stage(source, entry, transfer)
        target = destination / "recordings" / entry["relative"]
        assert target.read_bytes() == original
        assert target.stat().st_mtime_ns == _STAMP
        for path, directory in ((target.parent, True), (target, False)):
            actual_owner, actual_entries, actual_protected = stage._descriptor(path)
            assert actual_owner == copy._BA and actual_protected
            copy._target_policy(
                actual_owner,
                actual_entries,
                stage.service,
                protected=actual_protected,
                require_protected=True,
                directory=directory,
            )
        # Existing complete files resume without write/replace or metadata edits.
        target_before = copy._signature(target.stat())
        copy.copy_to_service_stage(source, entry, transfer)
        assert copy._signature(target.stat()) == target_before
        with pytest.raises(OSError), stage.open_new(target):
            pytest.fail("CREATE_NEW must refuse an existing destination")
        assert target.read_bytes() == original
        # The real pinned directory handle denies DELETE access independently
        # of ACLs; the same DELETE open succeeds once the pins are released.
        with stage.pin(transfer, "recordings", entry["relative"].split("/")):
            with pytest.raises(OSError) as locked:
                stage._open_handle(target.parent, access=0x10000, share=7, create=3, directory=True)
            assert locked.value.errno == 32, "Expected the Win32 sharing violation"
        delete_handle = stage._open_handle(
            target.parent, access=0x10000, share=7, create=3, directory=True
        )
        assert stage.kernel.CloseHandle(delete_handle)
        # An interrupted file is preserved and cannot be resumed as complete.
        partial_name = "2026-11-22_02-01-00-000000_+0530.mp4"
        partial_source = source_root / partial_name
        partial_source.write_bytes(original)
        partial_entry = _metadata(partial_source)
        partial = target.parent / partial_name
        with stage.open_new(partial) as stream:
            stream.write(b"partial")
            os.fsync(stream.fileno())
        partial_before = copy._signature(partial.stat())
        with pytest.raises(copy.MigrationCopyError):
            copy.copy_to_service_stage(partial_source, partial_entry, transfer)
        assert partial.read_bytes() == b"partial"
        assert copy._signature(partial.stat()) == partial_before
        # Even a foreign read ACE in the stage is rejected, without ACL repair.
        foreign = destination / "exports"
        _mkdir(stage, foreign, owner=copy._BA, foreign=True)
        foreign_before = stage._descriptor(foreign)
        export = source_root / (str(uuid4()) + ".mp4")
        export.write_bytes(b"completed native export fixture")
        with pytest.raises(copy.MigrationCopyError):
            copy.copy_to_service_stage(
                export, _metadata(export, area="exports", relative=export.name), transfer
            )
        assert stage._descriptor(foreign) == foreign_before and not tuple(foreign.iterdir())
        # Actual Windows directory reparse points must fail before file writes.
        reparse = destination / "recordings" / ("k5_" + "b" * 64)
        os.symlink(source_root, reparse, target_is_directory=True)
        track_link(reparse)
        try:
            assert reparse.lstat().st_file_attributes & 0x400
            linked_entry = {**entry, "relative": reparse.name + "/" + source.name}
            with pytest.raises(copy.MigrationCopyError):
                copy.copy_to_service_stage(source, linked_entry, transfer)
            assert source.read_bytes() == original
        finally:
            os.rmdir(reparse)
        assert source.read_bytes() == original and copy._signature(source.stat()) == before
        assert stage.native.descriptor(source) == source_acl
        record_property("native_copy_security_and_io", "real Win32 APIs; elevated original owner")
        record_property("native_copy_service_token", "not exercised by this adapter test")
        record_property("native_copy_exact_mtime_ns", str(_STAMP))
