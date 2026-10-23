from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path
from threading import Event
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


@pytest.fixture
def clock(monkeypatch):
    value = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(storage_module.time, "monotonic", lambda: value.now)
    return value


def _settled_storage(root, clock, **bounds):
    storage = RecordingStorage(root, **bounds)
    storage.inspect()
    clock.now += storage_module.MIN_CLOSED_AGE_SECONDS
    return storage


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


def test_prune_oldest_closed_segments_to_hysteresis_and_preserves_newest(
    root, monkeypatch, clock
) -> None:
    _free(monkeypatch, 500)
    oldest = _segment(root, 1, size=10, age=100)
    middle = _segment(root, 2, size=10, age=60)
    newest = _segment(root, 3, size=10, age=30)
    status = _settled_storage(root, clock, max_bytes=25, min_free_bytes=100).prune()
    assert status.total_bytes == 20
    assert status.deleted_segments == 1
    assert status.recording_allowed
    assert not oldest.exists()
    assert middle.exists() and newest.exists()


def test_newest_per_camera_and_young_segments_are_never_pruned(root, monkeypatch, clock) -> None:
    _free(monkeypatch, 500)
    old = _segment(root, 1, size=10, age=30)
    young = _segment(root, 2, size=10, age=5)
    newest = _segment(root, 3, size=10, age=1)
    sole_other = _segment(root, 1, size=10, age=100, camera=2)
    storage = RecordingStorage(root, max_bytes=15, min_free_bytes=100)
    storage.inspect()
    clock.now += 9
    changed = time.time() - 5
    os.utime(young, (changed, changed))
    storage.inspect()
    clock.now += 1
    status = storage.prune()
    assert not old.exists()
    assert all(path.exists() for path in (young, newest, sole_other))
    assert status.total_bytes == 30
    assert status.deleted_segments == 1
    assert not status.recording_allowed


def test_foreign_files_directories_and_nested_recordings_are_untouched(
    root, monkeypatch, clock
) -> None:
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
    status = _settled_storage(root, clock, max_bytes=10, min_free_bytes=100).prune()
    assert status.total_bytes == 5
    assert status.recording_allowed
    assert not closed.exists() and newest.exists()
    assert foreign_root.exists() and foreign_file.exists() and preserved.exists()
    assert nested.is_dir()


def test_low_free_space_only_retires_owned_closed_segments(root, monkeypatch, clock) -> None:
    closed = _segment(root, 1, size=10, age=60)
    newest = _segment(root, 2, size=5, age=30)
    foreign = root / "foreign.bin"
    foreign.write_bytes(b"preserve")
    monkeypatch.setattr(
        storage_module.shutil,
        "disk_usage",
        lambda _: SimpleNamespace(free=0 if closed.exists() else 200),
    )
    status = _settled_storage(root, clock, max_bytes=100, min_free_bytes=100).prune()
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
    root, monkeypatch, clock
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
    status = _settled_storage(root, clock, max_bytes=10, min_free_bytes=100).prune()
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


def test_segment_changed_after_scan_is_not_deleted(root, monkeypatch, clock) -> None:
    _free(monkeypatch, 500)
    closed = _segment(root, 1, size=20, age=60)
    _segment(root, 2, size=5, age=30)
    storage = _settled_storage(root, clock, max_bytes=10, min_free_bytes=100)
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


def test_entry_budget_is_shared_between_prune_and_final_rescan(root, monkeypatch, clock) -> None:
    _free(monkeypatch, 500)
    closed = _segment(root, 1, size=20, age=60)
    newest = _segment(root, 2, size=5, age=30)
    storage = _settled_storage(root, clock, max_bytes=10, min_free_bytes=100)
    monkeypatch.setattr(storage_module, "MAX_STORAGE_ENTRIES", 4)
    status = storage.prune()
    assert not status.recording_allowed
    assert status.deleted_segments == 1
    assert not closed.exists() and newest.exists()


def test_healthy_storage_requires_only_one_bounded_scan(root, monkeypatch, clock) -> None:
    _free(monkeypatch, 500)
    files = [_segment(root, 1, age=60), _segment(root, 2, age=30)]
    storage = _settled_storage(root, clock, max_bytes=100, min_free_bytes=100)
    monkeypatch.setattr(storage_module, "MAX_STORAGE_ENTRIES", 3)
    status = storage.prune()
    assert status.recording_allowed and status.deleted_segments == 0
    assert all(path.exists() for path in files)


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


@pytest.mark.parametrize("jump", [-365 * 86400, 365 * 86400])
def test_wall_clock_year_step_never_ages_observed_segments_early(
    root, monkeypatch, clock, jump
) -> None:
    _free(monkeypatch, 500)
    closed = _segment(root, 1, age=365 * 86400)
    newest = _segment(root, 2, age=30)
    storage = RecordingStorage(root, max_bytes=100, min_free_bytes=100, retention_seconds=20)
    storage.inspect()
    previous_wall = time.time()
    monkeypatch.setattr(storage_module.time, "time", lambda: previous_wall + jump)
    for elapsed in (0, 9, 10, 19):
        clock.now = 1000 + elapsed
        status = storage.prune()
        assert status.recording_allowed and status.deleted_segments == 0
        assert closed.exists() and newest.exists()
    clock.now = 1020
    status = storage.prune()
    assert status.deleted_segments == 1 and not closed.exists() and newest.exists()


def test_default_age_retention_expires_under_quota_after_observed_day(
    root, monkeypatch, clock
) -> None:
    _free(monkeypatch, 500)
    closed = _segment(root, 1, age=60)
    newest = _segment(root, 2, age=30)
    storage = RecordingStorage(root, max_bytes=100, min_free_bytes=100)
    storage.inspect()
    clock.now += 86400 - 1
    assert storage.prune().deleted_segments == 0
    clock.now += 1
    status = storage.prune()
    assert status.recording_allowed and status.total_bytes == newest.stat().st_size
    assert status.deleted_segments == 1 and not closed.exists() and newest.exists()


def test_quota_waits_for_monotonic_closed_guard_despite_old_mtime_and_wall_step(
    root, monkeypatch, clock
) -> None:
    _free(monkeypatch, 500)
    closed = _segment(root, 1, size=20, age=365 * 86400)
    newest = _segment(root, 2, size=5, age=30)
    storage = RecordingStorage(root, max_bytes=10, min_free_bytes=100)
    previous_wall = time.time()
    monkeypatch.setattr(storage_module.time, "time", lambda: previous_wall + 365 * 86400)
    assert not storage.prune().recording_allowed
    assert closed.exists()
    clock.now += 9
    assert storage.prune().deleted_segments == 0
    clock.now += 1
    status = storage.prune()
    assert status.recording_allowed and status.deleted_segments == 1
    assert not closed.exists() and newest.exists()


def test_cold_restart_restarts_retention_age_but_quota_still_reclaims_after_closed_guard(
    root, monkeypatch, clock
) -> None:
    _free(monkeypatch, 500)
    closed = _segment(root, 1, size=20, age=60)
    newest = _segment(root, 2, size=5, age=30)
    storage = RecordingStorage(root, max_bytes=100, min_free_bytes=100, retention_seconds=20)
    storage.inspect()
    clock.now += 19
    restarted = RecordingStorage(root, max_bytes=100, min_free_bytes=100, retention_seconds=20)
    restarted.inspect()
    clock.now += 1
    assert restarted.prune().deleted_segments == 0 and closed.exists()
    restarted_again = RecordingStorage(root, max_bytes=10, min_free_bytes=100, retention_seconds=20)
    assert not restarted_again.prune().recording_allowed
    clock.now += 10
    status = restarted_again.prune()
    assert status.recording_allowed and status.deleted_segments == 1
    assert not closed.exists() and newest.exists()


@pytest.mark.parametrize("change", ["size", "mtime", "identity"])
def test_changed_segment_restarts_monotonic_age_before_expiration(
    root, monkeypatch, clock, change
) -> None:
    _free(monkeypatch, 500)
    closed = _segment(root, 1, size=10, age=60)
    newest = _segment(root, 2, size=5, age=30)
    original_mtime = closed.stat().st_mtime_ns
    storage = RecordingStorage(root, max_bytes=100, min_free_bytes=100, retention_seconds=20)
    storage.inspect()
    clock.now += 20
    if change == "identity":
        replacement = closed.with_name("owned-replacement.tmp")
        replacement.write_bytes(b"replacement")
        os.replace(replacement, closed)
    elif change == "size":
        closed.write_bytes(b"changed-size-recording")
    else:
        original_mtime -= 1_000_000_000
    os.utime(closed, ns=(original_mtime, original_mtime))
    status = storage.prune()
    assert status.deleted_segments == 0 and closed.exists() and newest.exists()
    clock.now += 19
    # A later observed segment replaces the changed clip's active/newest
    # protection. Its old timestamp also models a backwards host clock step.
    successor = _segment(root, 3, size=5, age=120)
    assert storage.prune().deleted_segments == 0
    clock.now += 1
    assert storage.prune().deleted_segments == 1
    assert not closed.exists() and newest.exists() and successor.exists()


@pytest.mark.parametrize("change", ["created", "modified"])
def test_backward_clock_step_preserves_latest_observed_recording_under_quota_pressure(
    root, monkeypatch, clock, change
) -> None:
    _free(monkeypatch, 500)
    pre_step = _segment(root, 1, size=10, age=-3600)
    if change == "modified":
        post_step = _segment(root, 2, size=5, age=60)
    storage = RecordingStorage(root, max_bytes=15, min_free_bytes=100, retention_seconds=0)
    storage.inspect()
    clock.now += 15
    if change == "created":
        post_step = _segment(root, 2, size=10, age=3600)
    else:
        post_step.write_bytes(b"newestdata")
        modified = time.time() - 3600
        os.utime(post_step, (modified, modified))
    storage.inspect()
    clock.now += 15
    status = storage.prune()
    assert status.deleted_segments == 0 and not status.recording_allowed
    assert pre_step.exists() and post_step.exists()
    # Once a subsequent segment is observed, the former active clip can be
    # reclaimed while both wall-newest and observed-newest remain protected.
    successor = _segment(root, 3, size=5, age=7200)
    storage.inspect()
    clock.now += 10
    status = storage.prune()
    assert status.deleted_segments == 1 and status.recording_allowed
    assert not post_step.exists() and pre_step.exists() and successor.exists()


def test_simultaneously_observed_changes_are_all_protected_without_scan_order_guessing(
    root, monkeypatch, clock
) -> None:
    _free(monkeypatch, 500)
    pre_step = _segment(root, 1, size=10, age=-3600)
    storage = RecordingStorage(root, max_bytes=15, min_free_bytes=100, retention_seconds=0)
    storage.inspect()
    clock.now += 15
    changed = [_segment(root, 2, age=3600), _segment(root, 3, age=7200)]
    storage.inspect()
    clock.now += 15
    status = storage.prune()
    assert status.deleted_segments == 0 and not status.recording_allowed
    assert pre_step.exists() and all(path.exists() for path in changed)


def test_complete_scans_prune_observation_map_and_incomplete_scan_discards_age_confidence(
    root, monkeypatch, clock
) -> None:
    _free(monkeypatch, 500)
    closed = _segment(root, 1, age=60)
    newest = _segment(root, 2, age=30)
    storage = RecordingStorage(root, max_bytes=100, min_free_bytes=100, retention_seconds=20)
    storage.inspect()
    assert set(storage._observations) == {closed, newest}
    closed.unlink()
    storage.inspect()
    assert set(storage._observations) == {newest}
    closed = _segment(root, 1, age=60)
    storage.inspect()
    clock.now += 20
    with monkeypatch.context() as limited:
        limited.setattr(storage_module, "MAX_STORAGE_ENTRIES", 2)
        status = storage.prune()
    assert not status.recording_allowed and status.deleted_segments == 0
    assert storage._observations == {} and closed.exists()
    assert storage.prune().deleted_segments == 0
    clock.now += 20
    assert storage.prune().deleted_segments == 1 and not closed.exists() and newest.exists()


def test_explicit_disabled_age_retention_keeps_stable_files_under_quota(
    root, monkeypatch, clock
) -> None:
    _free(monkeypatch, 500)
    files = [_segment(root, 1, age=60), _segment(root, 2, age=30)]
    storage = RecordingStorage(root, max_bytes=100, min_free_bytes=100, retention_seconds=0)
    storage.inspect()
    clock.now += 31 * 86400
    assert storage.prune().deleted_segments == 0
    assert all(path.exists() for path in files)


@pytest.mark.parametrize("retention", [True, -1, 30 * 86400 + 1, 1.5])
def test_retention_bounds_are_explicit_and_finite(root, retention):
    with pytest.raises(ValueError):
        RecordingStorage(root, retention_seconds=retention)


def test_progress_waits_for_complete_baseline_and_real_growth(root, monkeypatch, clock):
    _free(monkeypatch, 500)
    file = _segment(root, 1, size=10)
    path_name = file.parent.name
    storage = RecordingStorage(root, max_bytes=100, min_free_bytes=0, retention_seconds=0)
    assert storage.recording_progress(path_name) == {
        "observed_bytes": 0,
        "last_progress_age_seconds": None,
        "inspection_complete": False,
        "observed": False,
    }
    storage.inspect()
    assert storage.recording_progress(path_name) == {
        "observed_bytes": 10,
        "last_progress_age_seconds": None,
        "inspection_complete": True,
        "observed": True,
    }
    clock.now += 20
    storage.inspect()
    assert storage.recording_progress(path_name)["last_progress_age_seconds"] is None
    file.write_bytes(b"x" * 20)
    clock.now += 1
    storage.inspect()
    assert storage.recording_progress(path_name)["last_progress_age_seconds"] == 0
    clock.now += 5
    storage.inspect()
    assert storage.recording_progress(path_name)["last_progress_age_seconds"] == 5

    def no_scan(*args, **kwargs):
        raise AssertionError("Cached progress must not inspect the filesystem")

    monkeypatch.setattr(storage_module.os, "scandir", no_scan)
    monkeypatch.setattr(Path, "lstat", no_scan)
    clock.now += 3
    sample = storage.recording_progress(path_name)
    assert sample["observed_bytes"] == 20 and sample["last_progress_age_seconds"] == 8
    assert str(root) not in repr(sample) and file.name not in repr(sample)


def test_progress_unknown_path_uses_global_inspection_without_claiming_observation(
    root, monkeypatch, clock
):
    _free(monkeypatch, 500)
    path_name = "k5_" + "a" * 64
    storage = RecordingStorage(root, min_free_bytes=0)
    storage.inspect()
    assert storage.recording_progress(path_name) == {
        "observed_bytes": 0,
        "last_progress_age_seconds": None,
        "inspection_complete": True,
        "observed": False,
    }
    _segment(root, 1, size=10, camera=int("a" * 64, 16))
    clock.now += 1
    storage.inspect()
    sample = storage.recording_progress(path_name)
    assert sample["observed"] is True and sample["observed_bytes"] == 10
    assert sample["last_progress_age_seconds"] is None


def test_progress_includes_active_segment_outside_prune_candidates(root, monkeypatch, clock):
    _free(monkeypatch, 500)
    active = _segment(root, 1, size=10)
    storage = RecordingStorage(root, max_bytes=5, min_free_bytes=0, retention_seconds=0)
    storage.inspect()
    with active.open("ab") as writer:
        writer.write(b"growing")
        writer.flush()
        clock.now += 15
        result = storage.prune()
    assert result.deleted_segments == 0 and active.exists()
    assert storage.recording_progress(active.parent.name) == {
        "observed_bytes": 17,
        "last_progress_age_seconds": 0,
        "inspection_complete": True,
        "observed": True,
    }


def test_progress_detects_growth_even_when_total_bytes_fall(root, monkeypatch, clock):
    _free(monkeypatch, 500)
    closed = _segment(root, 1, size=40)
    active = _segment(root, 2, size=5)
    storage = RecordingStorage(root, max_bytes=100, min_free_bytes=0, retention_seconds=0)
    storage.inspect()
    closed.unlink()
    active.write_bytes(b"x" * 10)
    clock.now += 5
    storage.inspect()
    sample = storage.recording_progress(active.parent.name)
    assert sample["observed_bytes"] == 10
    assert sample["last_progress_age_seconds"] == 0


def test_pruning_is_not_progress_but_a_new_nonempty_segment_is(root, monkeypatch, clock):
    _free(monkeypatch, 500)
    closed = _segment(root, 1, size=20, age=60)
    newest = _segment(root, 2, size=5, age=30)
    storage = RecordingStorage(root, max_bytes=10, min_free_bytes=0, retention_seconds=0)
    storage.inspect()
    clock.now += 10
    assert storage.prune().deleted_segments == 1
    assert not closed.exists()
    sample = storage.recording_progress(newest.parent.name)
    assert sample["observed_bytes"] == 5 and sample["last_progress_age_seconds"] is None
    empty = _segment(root, 3, size=0)
    clock.now += 5
    storage.inspect()
    assert storage.recording_progress(newest.parent.name)["last_progress_age_seconds"] is None
    created = _segment(root, 4, size=2)
    clock.now += 2
    storage.inspect()
    sample = storage.recording_progress(newest.parent.name)
    assert sample["observed_bytes"] == 7 and sample["last_progress_age_seconds"] == 0
    assert empty.exists() and created.exists()


@pytest.mark.parametrize("jump", [-365 * 86400, 365 * 86400])
def test_progress_age_ignores_wall_clock_and_metadata_only_changes(root, monkeypatch, clock, jump):
    _free(monkeypatch, 500)
    active = _segment(root, 1, size=5)
    storage = RecordingStorage(root, min_free_bytes=0, retention_seconds=0)
    storage.inspect()
    active.write_bytes(b"x" * 10)
    clock.now += 2
    storage.inspect()
    shifted = time.time() + jump
    monkeypatch.setattr(storage_module.time, "time", lambda: shifted)
    os.utime(active, (shifted, shifted))
    clock.now += 7
    storage.inspect()
    assert storage.recording_progress(active.parent.name)["last_progress_age_seconds"] == 7


@pytest.mark.parametrize("failure", ["budget", "ownership"])
def test_incomplete_scan_invalidates_progress_until_new_complete_observations(
    root, monkeypatch, clock, failure
):
    _free(monkeypatch, 500)
    active = _segment(root, 1, size=5)
    storage = RecordingStorage(root, min_free_bytes=0, retention_seconds=0)
    storage.inspect()
    active.write_bytes(b"x" * 10)
    clock.now += 1
    storage.inspect()
    assert storage.recording_progress(active.parent.name)["last_progress_age_seconds"] == 0
    with monkeypatch.context() as refused:
        if failure == "budget":
            refused.setattr(storage_module, "MAX_STORAGE_ENTRIES", 1)
        else:
            original = identity_state._windows_admission

            def deny_file(path, **kwargs):
                if path == active:
                    raise identity_state.IdentityStateError("private-ownership-material")
                return original(path, **kwargs)

            refused.setattr(identity_state, "_windows_admission", deny_file)
        assert not storage.inspect().recording_allowed
    sample = storage.recording_progress(active.parent.name)
    assert sample["inspection_complete"] is False and sample["observed"] is False
    assert sample["last_progress_age_seconds"] is None
    storage.inspect()
    sample = storage.recording_progress(active.parent.name)
    assert sample["observed"] is True and sample["observed_bytes"] == 10
    assert sample["last_progress_age_seconds"] is None
    active.write_bytes(b"x" * 11)
    clock.now += 1
    storage.inspect()
    assert storage.recording_progress(active.parent.name)["last_progress_age_seconds"] == 0


def test_replaced_camera_directory_requires_a_new_progress_baseline(root, monkeypatch, clock):
    _free(monkeypatch, 500)
    active = _segment(root, 1, size=5)
    storage = RecordingStorage(root, min_free_bytes=0, retention_seconds=0)
    storage.inspect()
    active.write_bytes(b"x" * 10)
    clock.now += 1
    storage.inspect()
    active.parent.rename(root / "preserved-history")
    replacement = _segment(root, 1, size=20)
    clock.now += 1
    storage.inspect()
    sample = storage.recording_progress(replacement.parent.name)
    assert sample["observed_bytes"] == 20 and sample["last_progress_age_seconds"] is None


def test_progress_cache_is_bounded_and_queried_paths_replace_historic_directories(
    root, monkeypatch, clock
):
    _free(monkeypatch, 500)
    files = [_segment(root, 1, size=1, camera=index) for index in range(1, 66)]
    storage = RecordingStorage(root, min_free_bytes=0, retention_seconds=0)
    storage.inspect()
    assert len(storage._progress) == 64
    uncached = next(file for file in files if file.parent.name not in storage._progress)
    assert storage.recording_progress(uncached.parent.name)["observed"] is False
    assert len(storage._progress) == 64
    storage.inspect()
    assert storage.recording_progress(uncached.parent.name)["observed"] is True
    assert storage.recording_progress(uncached.parent.name)["last_progress_age_seconds"] is None
    uncached.write_bytes(b"xx")
    clock.now += 1
    storage.inspect()
    assert storage.recording_progress(uncached.parent.name)["last_progress_age_seconds"] == 0
    assert len(storage._progress) == 64


@pytest.mark.parametrize("path_name", ["../private-source", "k5_" + "a" * 63, None])
def test_progress_refuses_noncanonical_path_names_without_disclosure(root, path_name):
    storage = RecordingStorage(root)
    with pytest.raises(ValueError, match="^Invalid recording path name$"):
        storage.recording_progress(path_name)


def test_cached_progress_remains_available_during_slow_maintenance(root, monkeypatch, clock):
    _free(monkeypatch, 500)
    active = _segment(root, 1, size=10)
    storage = RecordingStorage(root, min_free_bytes=0, retention_seconds=0)
    storage.inspect()
    entered, release = Event(), Event()
    original = storage_module._file_info

    def slow_inspection(path):
        entered.set()
        assert release.wait(5)
        return original(path)

    monkeypatch.setattr(storage_module, "_file_info", slow_inspection)
    with ThreadPoolExecutor(max_workers=2) as pool:
        inspection = pool.submit(storage.inspect)
        try:
            assert entered.wait(3)
            cached = pool.submit(storage.recording_progress, active.parent.name).result(timeout=3)
            assert cached["observed_bytes"] == 10 and cached["inspection_complete"] is True
        finally:
            release.set()
        assert inspection.result(timeout=3).recording_allowed


@pytest.mark.parametrize("next_tick", [float("nan"), float("inf"), 1000.0])
def test_progress_never_returns_nonfinite_or_negative_age(root, monkeypatch, clock, next_tick):
    _free(monkeypatch, 500)
    active = _segment(root, 1, size=5)
    storage = RecordingStorage(root, min_free_bytes=0, retention_seconds=0)
    storage.inspect()
    active.write_bytes(b"x" * 10)
    clock.now += 1
    storage.inspect()
    clock.now = next_tick
    sample = storage.recording_progress(active.parent.name)
    assert sample["observed_bytes"] == 10 and sample["last_progress_age_seconds"] is None
