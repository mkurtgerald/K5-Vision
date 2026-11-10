"""Fresh recorded-frame selection from the recorder's bounded private inventory."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from k5vision import identity_state
from k5vision.operator_events import OperatorEventsError, RecordedFrameSampler


@pytest.fixture
def recording_camera(tmp_path, monkeypatch):
    if os.name == "posix":
        original = Path.lstat

        def system_ancestors(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            if path in (Path("/"), Path("/tmp")) and value.st_uid == 65534:
                fields = list(value)
                fields[4] = 0
                return os.stat_result(fields)
            return value

        monkeypatch.setattr(Path, "lstat", system_ancestors)
    state = identity_state.initialize_identity_state(
        tmp_path / "identity", username="cache-admin", password="test-cache-password"
    )
    root = state.directory / "recordings"
    root.mkdir(mode=0o700)
    binding = SimpleNamespace(device_id=uuid4(), path_name="k5_" + "a" * 64)
    directory = root / binding.path_name
    directory.mkdir(mode=0o700)
    return root, directory, SimpleNamespace(binding=binding)


def _signature(directory, path):
    parent, leaf = directory.stat(), path.stat()
    return (
        parent.st_dev,
        parent.st_ino,
        leaf.st_dev,
        leaf.st_ino,
        leaf.st_size,
        leaf.st_mtime_ns,
        leaf.st_ctime_ns,
    )


def _sampler(root, inventory, monkeypatch):
    value = RecordedFrameSampler(root, recording_files=lambda _binding: inventory)
    monkeypatch.setattr(value, "decode", lambda raw, stamp, key: (raw, stamp, key))
    return value


def test_large_cached_history_still_samples_fresh_growth_between_storage_scans(
    recording_camera, monkeypatch
):
    root, directory, source = recording_camera
    active = directory / "2026-10-28_12-00-00-000000_+0000.mp4"
    active.write_bytes(b"baseline")
    inventory = {active.name: _signature(directory, active)}
    parent = directory.stat()
    for index in range(3000):
        stamp = datetime(2026, 10, 1, tzinfo=UTC) + timedelta(minutes=index)
        name = stamp.strftime("%Y-%m-%d_%H-%M-%S-%f_+0000.mp4")
        inventory[name] = (parent.st_dev, parent.st_ino, parent.st_dev, index + 100, 1, 0, 0)
    original = os.scandir
    directory_scans = 0
    permitted_scans = 1

    def bounded_cold_scans(path):
        nonlocal directory_scans
        if Path(path) == directory:
            directory_scans += 1
            if directory_scans > permitted_scans:
                raise AssertionError("Repeated scan of unchanged recording directory")
        return original(path)

    # A single name-only cold scan is required to exclude retained segments
    # missing from the lagging inventory; byte-growth polls must reuse its result.
    monkeypatch.setattr(os, "scandir", bounded_cold_scans)
    sampler = _sampler(root, inventory, monkeypatch)
    assert sampler.capture(source) is None
    assert directory_scans == 1
    active.write_bytes(b"baseline-and-new-frames")
    first = sampler.capture(source)
    assert first[0] == b"baseline-and-new-frames"
    assert first[1] == datetime(2026, 10, 28, 12, tzinfo=UTC)
    # The storage cache remains at its old byte size. A second real write must
    # still be observed immediately, without waiting for that cache refresh.
    active.write_bytes(b"baseline-and-new-frames-and-more")
    assert sampler.capture(source)[0] == b"baseline-and-new-frames-and-more"
    assert sampler.capture(source) is None
    assert directory_scans == 1
    sampler.prune(set())
    permitted_scans = 2
    assert sampler.capture(source) is None
    assert directory_scans == 2


def test_writer_missing_from_cold_inventory_is_watched_without_admitting_old_bytes(
    recording_camera, monkeypatch
):
    """A pre-existing active segment can grow without changing directory mtime."""
    root, directory, source = recording_camera
    cached = directory / "2026-10-28_12-00-00-000000_+0000.mp4"
    cached.write_bytes(b"old-cached-history")
    writer = directory / "2026-10-28_12-01-00-000000_+0000.mp4"
    writer.write_bytes(b"preexisting-writer-bytes")
    inventory = {cached.name: _signature(directory, cached)}
    sampler = _sampler(root, inventory, monkeypatch)

    # The cold baseline sees this writer only in the private directory,
    # not in the storage-maintenance cache. Neither initial bytes nor an
    # unchanged file may become a detection.
    assert sampler.capture(source) is None
    assert writer.name in sampler._active_files[source.binding.device_id]
    assert sampler.capture(source) is None
    assert sampler._capture_outcome == "no_growth"

    # Only actual post-baseline byte growth is eligible, even while the
    # maintenance inventory and directory mtime remain unchanged.
    initial_mtime = directory.stat().st_mtime_ns
    writer.write_bytes(b"preexisting-writer-bytes-fresh-frames")
    assert directory.stat().st_mtime_ns == initial_mtime
    sampled = sampler.capture(source)
    assert sampled is not None
    assert sampled[0] == b"preexisting-writer-bytes-fresh-frames"
    assert sampled[2] == writer.name
    assert sampler.capture(source) is None


def test_uncached_retained_recording_never_emits_without_new_growth(recording_camera, monkeypatch):
    root, directory, source = recording_camera
    retained = directory / "2026-10-28_12-01-00-000000_+0000.mp4"
    retained.write_bytes(b"retained")
    sampler = _sampler(root, {}, monkeypatch)
    assert sampler.capture(source) is None
    assert retained.name in sampler._active_files[source.binding.device_id]
    for _ in range(3):
        assert sampler.capture(source) is None
        assert sampler._capture_outcome == "no_growth"


def test_cached_new_recording_after_clock_rollback_uses_its_explicit_utc(
    recording_camera, monkeypatch
):
    root, directory, source = recording_camera
    future = directory / "2026-10-28_12-00-00-000000_+0000.mp4"
    future.write_bytes(b"retained-future")
    inventory = {future.name: _signature(directory, future)}
    sampler = _sampler(root, inventory, monkeypatch)
    assert sampler.capture(source) is None
    current = directory / "2026-10-26_12-00-00-000000_+0000.mp4"
    current.write_bytes(b"new-video-after-clock-correction")
    inventory[current.name] = _signature(directory, current)
    sample = sampler.capture(source)
    assert sample[0] == b"new-video-after-clock-correction"
    assert sample[1] == datetime(2026, 10, 26, 12, tzinfo=UTC)
    assert sample[2] == current.name
    assert sampler.capture(source) is None


def test_new_zero_byte_segment_remains_watched_with_lagging_inventory(
    recording_camera, monkeypatch
):
    root, directory, source = recording_camera
    retained = directory / "2026-10-28_12-00-00-000000_+0000.mp4"
    retained.write_bytes(b"retained-content")
    inventory = {retained.name: _signature(directory, retained)}
    sampler = _sampler(root, inventory, monkeypatch)
    assert sampler.capture(source) is None

    # A recorder can create its next segment before writing its first bytes.
    # The storage cache still lists only the retained file. The new empty
    # segment must be watched, not marked seen and permanently forgotten.
    fresh = directory / "2026-10-26_12-00-00-000000_+0000.mp4"
    fresh.write_bytes(b"")
    assert sampler.capture(source) is None
    assert sampler._active_files[source.binding.device_id][0] == fresh.name

    fresh.write_bytes(b"fresh-recorded-frames")
    sample = sampler.capture(source)
    assert sample is not None
    assert sample[0] == b"fresh-recorded-frames"
    assert sample[1] == datetime(2026, 10, 26, 12, tzinfo=UTC)
    assert sample[2] == fresh.name
    assert sampler.capture(source) is None


def test_incomplete_cache_waits_and_replaced_camera_directory_is_refused(
    recording_camera, monkeypatch
):
    root, directory, source = recording_camera
    sampler = RecordedFrameSampler(root, recording_files=lambda _binding: None)
    assert sampler.capture(source) is None
    active = directory / "2026-10-28_12-00-00-000000_+0000.mp4"
    active.write_bytes(b"current")
    signature = _signature(directory, active)
    inventory = {active.name: (signature[0], signature[1] + 1, *signature[2:])}
    sampler = _sampler(root, inventory, monkeypatch)
    with pytest.raises(OperatorEventsError, match="inventory refused"):
        sampler.capture(source)


def test_cached_paths_never_authorize_a_linked_recording(recording_camera, monkeypatch):
    if os.name != "posix":
        pytest.skip("POSIX symbolic-link admission case")
    root, directory, source = recording_camera
    active = directory / "2026-10-28_12-00-00-000000_+0000.mp4"
    active.write_bytes(b"baseline")
    inventory = {active.name: _signature(directory, active)}
    sampler = _sampler(root, inventory, monkeypatch)
    assert sampler.capture(source) is None
    active.unlink()
    active.symlink_to(root.parent / "users.sqlite3")
    with pytest.raises(identity_state.IdentityStateError, match="linked identity paths"):
        sampler.capture(source)
