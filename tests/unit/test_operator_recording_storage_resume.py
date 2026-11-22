"""Budget exhaustion must pause safely and resume, rather than discard all progress."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_operator_recording_storage import _free, _segment
from test_operator_recording_storage import clock as clock_fixture
from test_operator_recording_storage import root as root_fixture

from k5vision import identity_state
from k5vision import operator_recording_storage as module
from k5vision.operator_recording_storage import RecordingStorage

clock = clock_fixture
root = root_fixture


def _finish(storage, operation="prune", attempts=20):
    results = []
    for _ in range(attempts):
        status = getattr(storage, operation)()
        results.append(status)
        if status.recording_allowed:
            return results
    pytest.fail("A paused recording inventory did not make bounded progress to recovery")


def test_inventory_larger_than_one_entry_slice_eventually_resumes_recording(
    root, monkeypatch, clock
):
    _free(monkeypatch, 500)
    files = [_segment(root, n) for n in range(7)]
    monkeypatch.setattr(module, "MAX_STORAGE_ENTRIES", 3)
    storage = RecordingStorage(root, max_bytes=1000, min_free_bytes=100)
    first = storage.prune()
    assert not first.recording_allowed and first.deleted_segments == 0
    assert not storage.recording_progress(files[0].parent.name)["inspection_complete"]
    results = _finish(storage)
    assert results[-1].total_bytes == 70
    assert sum(value.deleted_segments for value in results) == 0
    assert all(path.exists() for path in files)


def test_paused_large_inventory_can_prune_then_finish_its_bounded_final_recheck(
    root, monkeypatch, clock
):
    _free(monkeypatch, 500)
    files = [_segment(root, n, age=100 - n) for n in range(7)]
    storage = RecordingStorage(root, max_bytes=1000, min_free_bytes=100)
    storage.inspect()
    clock.now += 11
    storage.configure_policy(max_bytes=15, min_free_bytes=100, retention_seconds=0)
    monkeypatch.setattr(module, "MAX_STORAGE_ENTRIES", 3)
    results = _finish(storage)
    assert not results[0].recording_allowed and results[0].deleted_segments == 0
    assert sum(value.deleted_segments for value in results) == 6
    assert results[-1].total_bytes == 10
    assert files[-1].exists()
    assert sum(path.exists() for path in files) == 1


def test_inspect_and_prune_share_the_same_pending_inventory(root, monkeypatch, clock):
    _free(monkeypatch, 500)
    files = [_segment(root, n) for n in range(7)]
    monkeypatch.setattr(module, "MAX_STORAGE_ENTRIES", 3)
    storage = RecordingStorage(root, max_bytes=1000, min_free_bytes=100)
    assert not storage.inspect().recording_allowed
    assert not storage.prune().recording_allowed
    assert storage.inspect().recording_allowed
    assert all(path.exists() for path in files)


def test_unknown_entries_consume_slices_but_are_never_deleted_or_accumulated_as_footage(
    root, monkeypatch, clock
):
    _free(monkeypatch, 500)
    files = [_segment(root, n) for n in range(3)]
    unknown = [root / f"owner-note-{n}" for n in range(10)]
    for path in unknown:
        path.write_bytes(b"preserve owner material")
    monkeypatch.setattr(module, "MAX_STORAGE_ENTRIES", 3)
    storage = RecordingStorage(root, max_bytes=1000, min_free_bytes=100)
    results = _finish(storage)
    assert len(results) > 3
    assert results[-1].total_bytes == 30
    assert all(path.exists() for path in files + unknown)


@pytest.fixture
def iterators(monkeypatch):
    original = module.os.scandir
    live, opened = set(), []

    class Tracked:
        def __init__(self, path):
            self.iterator = original(path)
            self.closed = False
            opened.append(self)
            live.add(self)
            assert len(live) <= 2, "A scan may own only root and current-camera iterators"

        def __iter__(self):
            return self

        def __next__(self):
            return next(self.iterator)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

        def close(self):
            if not self.closed:
                self.iterator.close()
                self.closed = True
                live.remove(self)

    monkeypatch.setattr(module.os, "scandir", Tracked)
    return live, opened


def test_explicit_close_retires_pending_iterator_handles_and_never_reopens(
    root, monkeypatch, clock, iterators
):
    _free(monkeypatch, 500)
    [_segment(root, n) for n in range(7)]
    monkeypatch.setattr(module, "MAX_STORAGE_ENTRIES", 3)
    storage = RecordingStorage(root, max_bytes=1000, min_free_bytes=100)
    assert not storage.prune().recording_allowed
    live, opened = iterators
    assert live
    storage.close()
    assert not live and all(value.closed for value in opened)
    count = len(opened)
    assert not storage.inspect().recording_allowed
    assert not storage.prune().recording_allowed
    assert len(opened) == count
    storage.close()


@pytest.mark.parametrize("replace", ["root", "active_camera"])
def test_replaced_inventory_directory_aborts_pending_cycle_and_retires_handles(
    root, monkeypatch, clock, iterators, replace
):
    _free(monkeypatch, 500)
    files = [_segment(root, n) for n in range(7)]
    monkeypatch.setattr(module, "MAX_STORAGE_ENTRIES", 3)
    storage = RecordingStorage(root, max_bytes=1000, min_free_bytes=100)
    assert not storage.prune().recording_allowed
    original = root if replace == "root" else files[0].parent
    preserved = original.with_name(original.name + "-preserved")
    original.rename(preserved)
    original.mkdir(mode=0o700)
    status = storage.prune()
    assert not status.recording_allowed and status.deleted_segments == 0
    assert not iterators[0]
    assert (
        all((preserved / path.name).exists() for path in files)
        if replace == "active_camera"
        else preserved.exists()
    )
    assert not storage._observations


def test_custody_refusal_while_resuming_aborts_without_deleting_or_preserving_age_confidence(
    root, monkeypatch, clock, iterators
):
    _free(monkeypatch, 500)
    files = [_segment(root, n) for n in range(7)]
    storage = RecordingStorage(root, max_bytes=1000, min_free_bytes=100)
    storage.inspect()
    clock.now += 11
    monkeypatch.setattr(module, "MAX_STORAGE_ENTRIES", 3)
    assert not storage.prune().recording_allowed
    original = identity_state._windows_admission

    def refuse(path, **options):
        if Path(path) == files[0].parent:
            raise identity_state.IdentityStateError("private custody diagnostic")
        return original(path, **options)

    monkeypatch.setattr(identity_state, "_windows_admission", refuse)
    status = storage.prune()
    assert not status.recording_allowed and status.deleted_segments == 0
    assert not storage._observations and not iterators[0]
    assert all(path.exists() for path in files)
    assert "private custody" not in repr(status)


def test_time_exhaustion_resumes_without_increasing_the_per_call_budget(root, monkeypatch, clock):
    _free(monkeypatch, 500)
    [_segment(root, n) for n in range(12)]
    original = module._file_info
    reads = []

    def measured(path):
        result = original(path)
        reads.append(path)
        clock.now += 1
        return result

    monkeypatch.setattr(module, "_file_info", measured)
    storage = RecordingStorage(root, max_bytes=1000, min_free_bytes=100)
    for _attempt in range(10):
        before = len(reads)
        status = storage.prune()
        assert len(reads) - before <= 5
        if status.recording_allowed:
            break
    else:
        pytest.fail("Time-limited inventory never resumed to completion")
    assert _attempt >= 2 and status.total_bytes == 120


def test_directory_completed_in_earlier_slice_is_readmitted_before_inventory_publication(
    root, monkeypatch, clock
):
    _free(monkeypatch, 500)
    first = [_segment(root, n, camera=1) for n in range(3)]
    second = [_segment(root, n, camera=2) for n in range(3)]
    monkeypatch.setattr(module, "MAX_STORAGE_ENTRIES", 3)
    storage = RecordingStorage(root, max_bytes=1000, min_free_bytes=100)
    assert not storage.prune().recording_allowed
    assert not storage.prune().recording_allowed
    original = first[0].parent
    original.rename(original.with_name(original.name + "-preserved"))
    original.mkdir(mode=0o700)
    status = storage.prune()
    assert not status.recording_allowed and status.deleted_segments == 0
    assert all(path.exists() for path in second)


def test_budget_exhausted_after_final_free_read_rechecks_directories_before_publication(
    root, monkeypatch, clock
):
    files = [_segment(root, n) for n in range(3)]
    calls = 0

    def delayed_free(_root):
        nonlocal calls
        calls += 1
        if calls == 2:
            clock.now += module.MAX_STORAGE_SECONDS
        return 500

    monkeypatch.setattr(module, "_free_bytes", delayed_free)
    storage = RecordingStorage(root, max_bytes=1000, min_free_bytes=100)
    assert not storage.prune().recording_allowed
    assert not storage.recording_progress(files[0].parent.name)["inspection_complete"]
    original = files[0].parent
    preserved = original.with_name(original.name + "-preserved")
    original.rename(preserved)
    original.mkdir(mode=0o700)
    status = storage.prune()
    assert not status.recording_allowed and not storage._observations
    assert all((preserved / path.name).exists() for path in files)


def test_final_admission_retries_its_own_budget_without_reenumerating_the_inventory(
    root, monkeypatch, clock
):
    _free(monkeypatch, 500)
    [_segment(root, n, camera=camera) for camera in (1, 2) for n in range(3)]
    original_file = module._file_info
    original_directory = identity_state._same_directory
    reads = []
    validation = {"delay": 3}

    def read(path):
        reads.append(path)
        return original_file(path)

    def check_directory(path, expected):
        original_directory(path, expected)
        if Path(path) != root:
            clock.now += validation["delay"]

    monkeypatch.setattr(module, "_file_info", read)
    storage = RecordingStorage(root, max_bytes=1000, min_free_bytes=100)
    # Exhaust only the final validation, not the generator's directory checks.
    original_publish = storage._publish_inventory

    def publish(inventory, deadline):
        with monkeypatch.context() as delayed:
            delayed.setattr(identity_state, "_same_directory", check_directory)
            return original_publish(inventory, deadline)

    monkeypatch.setattr(storage, "_publish_inventory", publish)
    assert not storage.prune().recording_allowed
    assert len(reads) == 6
    assert not storage.prune().recording_allowed
    assert len(reads) == 6
    validation["delay"] = 0
    assert storage.prune().recording_allowed
    assert len(reads) == 6


def test_resumed_progress_uses_actual_growth_observation_time_not_later_completion(
    root, monkeypatch, clock
):
    _free(monkeypatch, 500)
    files = [_segment(root, n) for n in range(7)]
    storage = RecordingStorage(root, max_bytes=1000, min_free_bytes=100)
    storage.inspect()
    original = module._file_info
    changed = []

    def grow_first_observed(path):
        if not changed:
            path.write_bytes(b"fresh observed growth")
            changed.append(path)
        return original(path)

    monkeypatch.setattr(module, "_file_info", grow_first_observed)
    monkeypatch.setattr(module, "MAX_STORAGE_ENTRIES", 3)
    assert not storage.prune().recording_allowed
    clock.now += 20
    _finish(storage)
    progress = storage.recording_progress(files[0].parent.name)
    assert progress["inspection_complete"]
    assert progress["last_progress_age_seconds"] == 20


def test_actual_pause_confirmation_discards_stale_pre_pause_bytes_once_then_recovers(
    root, monkeypatch, clock, iterators
):
    _free(monkeypatch, 500)
    [_segment(root, n) for n in range(7)]
    original = module._file_info
    read = []

    def observed(path):
        read.append(path)
        return original(path)

    monkeypatch.setattr(module, "_file_info", observed)
    monkeypatch.setattr(module, "MAX_STORAGE_ENTRIES", 3)
    storage = RecordingStorage(root, max_bytes=1000, min_free_bytes=100)
    assert not storage.prune().recording_allowed
    changed = read[0]
    changed.write_bytes(b"x" * 100)
    storage.confirm_writer_pause(paused=True)
    assert not iterators[0]
    statuses = []
    for _ in range(10):
        status = storage.prune()
        statuses.append(status)
        # The unchanged paused gate must not reset every maintenance slice.
        storage.confirm_writer_pause(paused=True)
        if status.recording_allowed:
            break
    else:
        pytest.fail("Repeated pause acknowledgement starved the fresh inventory")
    assert status.total_bytes == 160
    assert not statuses[0].recording_allowed
    assert all(path.exists() for path in read)
    storage.confirm_writer_pause(paused=False)
    assert not storage.prune().recording_allowed
    storage.confirm_writer_pause(paused=True)
    assert not iterators[0]
