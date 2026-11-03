from __future__ import annotations

import os
import time
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest

from k5vision import identity_state
from k5vision import operator_recording_storage as storage_module
from k5vision.operator_recording_storage import RecordingStorage, RecordingStorageError


@pytest.fixture
def root(tmp_path, monkeypatch):
    # Model ordinary trusted root/tmp ancestry when the execution sandbox maps
    # those system directory owners to nobody. All recording paths and file
    # ownership, modes, link checks and inode comparisons remain real.
    if os.name == "posix":
        original_lstat = Path.lstat

        def ordinary_ancestor(path, *args, **kwargs):
            info = original_lstat(path, *args, **kwargs)
            if path in (Path("/"), Path("/tmp")) and info.st_uid == 65534:
                values = list(info)
                values[4] = 0
                return os.stat_result(values)
            return info

        monkeypatch.setattr(Path, "lstat", ordinary_ancestor)
    path = tmp_path / "recordings"
    path.mkdir(mode=0o700)
    return path


def _segment(root, index, *, size=10, age=30, camera=1, offset="Z") -> Path:
    directory = root / ("k5_" + f"{camera:064x}")
    directory.mkdir(exist_ok=True, mode=0o700)
    suffix = "" if offset is None else "_" + offset
    path = directory / f"2026-10-11_12-00-{index:02d}-000000{suffix}.mp4"
    path.write_bytes(b"x" * size)
    modified = time.time() - age
    os.utime(path, (modified, modified))
    return path


def _free(monkeypatch, count):
    monkeypatch.setattr(storage_module.shutil, "disk_usage", lambda _: SimpleNamespace(free=count))


def test_inspect_is_aggregate_immutable_and_does_not_delete(root, monkeypatch) -> None:
    _free(monkeypatch, 500)
    files = [_segment(root, 1, size=12, age=60), _segment(root, 2, size=8, age=30)]
    status = RecordingStorage(root, max_bytes=20, min_free_bytes=100).inspect()
    assert status.total_bytes == 20
    assert status.free_bytes == 500
    assert status.recording_allowed
    assert status.deleted_segments == 0
    assert all(path.exists() for path in files)
    assert str(root) not in repr(status)
    with pytest.raises(FrozenInstanceError):
        status.total_bytes = 0


def test_prune_oldest_closed_segments_to_hysteresis_and_preserves_newest(root, monkeypatch) -> None:
    _free(monkeypatch, 500)
    oldest = _segment(root, 1, size=10, age=100)
    middle = _segment(root, 2, size=10, age=60)
    newest = _segment(root, 3, size=10, age=30)
    status = RecordingStorage(root, max_bytes=25, min_free_bytes=100).prune()
    assert status.total_bytes == 20
    assert status.deleted_segments == 1
    assert status.recording_allowed
    assert not oldest.exists()
    assert middle.exists() and newest.exists()


def test_newest_per_camera_and_young_segments_are_never_pruned(root, monkeypatch) -> None:
    _free(monkeypatch, 500)
    old = _segment(root, 1, size=10, age=30)
    young = _segment(root, 2, size=10, age=5)
    newest = _segment(root, 3, size=10, age=1)
    sole_other = _segment(root, 1, size=10, age=100, camera=2)
    status = RecordingStorage(root, max_bytes=15, min_free_bytes=100).prune()
    assert not old.exists()
    assert all(path.exists() for path in (young, newest, sole_other))
    assert status.total_bytes == 30
    assert status.deleted_segments == 1
    assert not status.recording_allowed


def test_foreign_files_directories_and_nested_recordings_are_untouched(root, monkeypatch) -> None:
    _free(monkeypatch, 500)
    closed = _segment(root, 1, size=10, age=60)
    newest = _segment(root, 2, size=5, age=30)
    foreign_root = root / "owner-notes.txt"
    foreign_root.write_bytes(b"do not remove" * 100)
    foreign_file = closed.parent / "unrelated.mp4"
    foreign_file.write_bytes(b"do not remove" * 100)
    nested = closed.parent / "backup"
    nested.mkdir()
    preserved = nested / closed.name
    preserved.write_bytes(b"do not remove")
    status = RecordingStorage(root, max_bytes=10, min_free_bytes=100).prune()
    assert status.total_bytes == 5
    assert status.recording_allowed
    assert not closed.exists() and newest.exists()
    assert foreign_root.exists() and foreign_file.exists() and preserved.exists()
    assert nested.is_dir()


def test_low_free_space_only_retires_owned_closed_segments(root, monkeypatch) -> None:
    closed = _segment(root, 1, size=10, age=60)
    newest = _segment(root, 2, size=5, age=30)
    foreign = root / "foreign.bin"
    foreign.write_bytes(b"preserve")
    monkeypatch.setattr(
        storage_module.shutil,
        "disk_usage",
        lambda _: SimpleNamespace(free=0 if closed.exists() else 200),
    )
    status = RecordingStorage(root, max_bytes=100, min_free_bytes=100).prune()
    assert status.recording_allowed and status.deleted_segments == 1
    assert newest.exists() and foreign.exists() and not closed.exists()


def test_low_free_space_without_closed_owned_files_pauses_without_foreign_deletion(
    root, monkeypatch
) -> None:
    _free(monkeypatch, 0)
    newest = _segment(root, 1, size=5)
    foreign = root / "foreign.bin"
    foreign.write_bytes(b"preserve")
    status = RecordingStorage(root, max_bytes=100, min_free_bytes=100).prune()
    assert not status.recording_allowed
    assert status.deleted_segments == 0
    assert newest.exists() and foreign.exists()


def test_windows_open_file_sharing_failure_is_skipped_without_false_quota_success(
    root, monkeypatch
) -> None:
    _free(monkeypatch, 500)
    closed = _segment(root, 1, size=20, age=60)
    newest = _segment(root, 2, size=5, age=30)
    original = Path.unlink

    def shared(path, *args, **kwargs):
        if path == closed:
            raise PermissionError("private native sharing failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", shared)
    status = RecordingStorage(root, max_bytes=10, min_free_bytes=100).prune()
    assert not status.recording_allowed
    assert status.total_bytes == 25 and status.deleted_segments == 0
    assert closed.exists() and newest.exists()


@pytest.mark.parametrize("offset", [None, "Z", "+0530", "-0400"])
def test_legacy_and_offset_donor_filename_formats_are_recognized(root, monkeypatch, offset) -> None:
    _free(monkeypatch, 500)
    _segment(root, 1, size=8, offset=offset)
    status = RecordingStorage(root, max_bytes=10, min_free_bytes=100).inspect()
    assert status.total_bytes == 8 and status.recording_allowed


def test_invalid_timestamp_file_causes_incomplete_admission_before_any_deletion(
    root, monkeypatch
) -> None:
    _free(monkeypatch, 500)
    closed = _segment(root, 1, size=20, age=60)
    _segment(root, 2, size=5, age=30)
    invalid = closed.parent / "2026-99-40_12-00-03-000000_Z.mp4"
    invalid.write_bytes(b"not a donor timestamp")
    status = RecordingStorage(root, max_bytes=10, min_free_bytes=100).prune()
    assert not status.recording_allowed and status.deleted_segments == 0
    assert closed.exists() and invalid.exists()


@pytest.mark.skipif(os.name == "nt", reason="Native reparse admission has existing Windows tests")
@pytest.mark.parametrize("target", ["directory", "segment"])
def test_links_refuse_scan_without_following_or_deleting_foreign_targets(
    root, monkeypatch, tmp_path, target
) -> None:
    _free(monkeypatch, 500)
    closed = _segment(root, 1, size=20, age=60)
    newest = _segment(root, 2, size=5, age=30)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    protected = foreign / "preserve.bin"
    protected.write_bytes(b"preserve")
    if target == "directory":
        link = root / ("k5_" + "f" * 64)
        link.symlink_to(foreign, target_is_directory=True)
    else:
        link = closed.parent / "2026-10-11_12-00-03-000000_Z.mp4"
        link.symlink_to(protected)
    status = RecordingStorage(root, max_bytes=10, min_free_bytes=100).prune()
    assert not status.recording_allowed and status.deleted_segments == 0
    assert link.is_symlink() and protected.exists()
    assert closed.exists() and newest.exists()


def test_hardlinked_segment_is_never_deleted(root, monkeypatch, tmp_path) -> None:
    _free(monkeypatch, 500)
    closed = _segment(root, 1, size=20, age=60)
    _segment(root, 2, size=5, age=30)
    protected = tmp_path / "hardlink.bin"
    os.link(closed, protected)
    status = RecordingStorage(root, max_bytes=10, min_free_bytes=100).prune()
    assert not status.recording_allowed and status.deleted_segments == 0
    assert closed.exists() and protected.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX private mode admission")
def test_private_root_permissions_are_checked_on_every_operation(root, monkeypatch) -> None:
    _free(monkeypatch, 500)
    storage = RecordingStorage(root, max_bytes=10, min_free_bytes=100)
    root.chmod(0o755)
    try:
        assert not storage.inspect().recording_allowed
        assert not storage.prune().recording_allowed
        with pytest.raises(RecordingStorageError):
            RecordingStorage(root)
    finally:
        root.chmod(0o700)


def test_root_replacement_refuses_operations_in_new_directory(root, monkeypatch) -> None:
    _free(monkeypatch, 500)
    storage = RecordingStorage(root, max_bytes=10, min_free_bytes=100)
    preserved = root.with_name("preserved")
    root.rename(preserved)
    root.mkdir(mode=0o700)
    foreign = _segment(root, 1, size=20, age=60)
    _segment(root, 2, size=5, age=30)
    status = storage.prune()
    assert not status.recording_allowed and status.deleted_segments == 0
    assert foreign.exists()


def test_segment_changed_after_scan_is_not_deleted(root, monkeypatch) -> None:
    _free(monkeypatch, 500)
    closed = _segment(root, 1, size=20, age=60)
    _segment(root, 2, size=5, age=30)
    storage = RecordingStorage(root, max_bytes=10, min_free_bytes=100)
    original = storage._scan
    scans = 0

    def changed(deadline, remaining):
        nonlocal scans
        result = original(deadline, remaining)
        scans += 1
        if scans == 1:
            closed.write_bytes(b"new active bytes")
        return result

    monkeypatch.setattr(storage, "_scan", changed)
    status = storage.prune()
    assert not status.recording_allowed and status.deleted_segments == 0
    assert closed.exists()


def test_entry_limit_refuses_incomplete_scan_before_deletion(root, monkeypatch) -> None:
    _free(monkeypatch, 500)
    closed = _segment(root, 1, size=20, age=60)
    newest = _segment(root, 2, size=5, age=30)
    monkeypatch.setattr(storage_module, "MAX_STORAGE_ENTRIES", 2)
    status = RecordingStorage(root, max_bytes=10, min_free_bytes=100).prune()
    assert not status.recording_allowed and status.deleted_segments == 0
    assert closed.exists() and newest.exists()


def test_entry_budget_is_shared_between_prune_and_final_rescan(root, monkeypatch) -> None:
    _free(monkeypatch, 500)
    closed = _segment(root, 1, size=20, age=60)
    newest = _segment(root, 2, size=5, age=30)
    monkeypatch.setattr(storage_module, "MAX_STORAGE_ENTRIES", 4)
    status = RecordingStorage(root, max_bytes=10, min_free_bytes=100).prune()
    assert not status.recording_allowed
    assert status.deleted_segments == 1
    assert not closed.exists() and newest.exists()


def test_time_budget_refuses_scan_without_unbounded_work(root, monkeypatch) -> None:
    _free(monkeypatch, 500)
    _segment(root, 1, size=5)
    storage = RecordingStorage(root, max_bytes=10, min_free_bytes=100)
    clock = iter(range(0, 100, 2))
    monkeypatch.setattr(storage_module.time, "monotonic", lambda: next(clock))
    status = storage.prune()
    assert not status.recording_allowed and status.deleted_segments == 0


def test_native_private_acl_refusal_and_disk_errors_are_sanitized(root, monkeypatch) -> None:
    closed = _segment(root, 1, size=20)
    _segment(root, 2, size=5, age=20)
    storage = RecordingStorage(root, max_bytes=10, min_free_bytes=100)
    original = identity_state._windows_admission

    def refused(path, **kwargs):
        if path == closed:
            raise identity_state.IdentityStateError("private-owner-path")
        return original(path, **kwargs)

    _free(monkeypatch, 500)
    monkeypatch.setattr(identity_state, "_windows_admission", refused)
    status = storage.prune()
    assert not status.recording_allowed and status.deleted_segments == 0
    assert "private-owner-path" not in repr(status)
    monkeypatch.setattr(identity_state, "_windows_admission", original)
    monkeypatch.setattr(storage_module.shutil, "disk_usage", lambda _: SimpleNamespace(free=-1))
    status = storage.inspect()
    assert status.free_bytes == 0 and not status.recording_allowed


def test_root_and_byte_configuration_are_admitted_without_creating_directories(root) -> None:
    missing = root / "missing"
    with pytest.raises(RecordingStorageError):
        RecordingStorage(missing)
    with pytest.raises(RecordingStorageError):
        RecordingStorage(Path("relative-recordings"))
    assert not missing.exists()
    for bounds in ({"max_bytes": 0}, {"max_bytes": True}, {"min_free_bytes": -1}):
        with pytest.raises(ValueError):
            RecordingStorage(root, **bounds)
