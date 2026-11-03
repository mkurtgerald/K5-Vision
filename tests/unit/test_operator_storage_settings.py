from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from k5vision.identity_state import initialize_identity_state
from k5vision.operator_storage_settings import (
    OperatorStorageSettings,
    StorageDirectoryError,
    StorageSettingsConflict,
    StorageSettingsError,
    prepare_operator_recording_storage,
)


@pytest.fixture
def storage(tmp_path, monkeypatch):
    if os.name == "posix":
        original = Path.lstat

        def trusted_system_ancestor(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            if path in (Path("/"), Path("/tmp")) and value.st_uid == 65534:
                fields = list(value)
                fields[4] = 0
                return os.stat_result(fields)
            return value

        monkeypatch.setattr(Path, "lstat", trusted_system_ancestor)
    root, recordings = tmp_path / "state", tmp_path / "recordings"
    identity = initialize_identity_state(
        root, username="storage-admin", password="storage-test-password"
    )
    recordings.mkdir(mode=0o700)
    settings = OperatorStorageSettings(root, identity.site_id, recordings)
    return settings, root, recordings, tmp_path


def _recordings(root):
    camera = root / ("k5_" + "1" * 64)
    camera.mkdir(mode=0o700)
    first = camera / "2026-10-31_12-00-00-000000_Z.mp4"
    second = camera / "2026-10-31_12-01-00-000000_Z.mp4"
    first.write_bytes(b"retained first recording" * 100)
    second.write_bytes(b"retained second recording" * 75)
    return first, second


def test_policy_is_private_site_bound_and_survives_restart(storage):
    settings, root, recordings, _ = storage
    assert settings.snapshot()["revision"] == 0
    assert not (root / "operator-storage.json").exists()
    changed = settings.configure(
        expected_revision=0,
        quota_bytes=42 * 1024**3,
        minimum_free_bytes=2 * 1024**3,
        retention_seconds=7 * 86400,
    )
    assert changed["revision"] == 1
    assert changed["retention_basis"] == "elapsed_stable_time"
    assert OperatorStorageSettings(root, settings._site_id, recordings).snapshot() == changed
    if os.name == "posix":
        assert (root / "operator-storage.json").stat().st_mode & 0o077 == 0
    with pytest.raises(StorageSettingsError):
        OperatorStorageSettings(root, "another-site", recordings)


@pytest.mark.parametrize(
    "changes",
    [
        {"quota_bytes": True},
        {"quota_bytes": 0},
        {"quota_bytes": 4096 * 1024**3 + 1},
        {"minimum_free_bytes": -1},
        {"retention_seconds": 30 * 86400 + 1},
        {"retention_seconds": 0.0},
        {"unknown": 123},
        {"quota_bytes": None},
        {},
    ],
)
def test_invalid_policy_never_mutates_existing_state(storage, changes):
    settings, _, _, _ = storage
    before = settings.snapshot()
    with pytest.raises((ValueError, StorageSettingsError)):
        settings.configure(expected_revision=0, **changes)
    assert settings.snapshot() == before


def test_impossible_free_space_target_is_refused_before_any_pruning(storage, monkeypatch):
    settings, _, _, _ = storage
    monkeypatch.setattr(
        "k5vision.operator_storage_settings.shutil.disk_usage",
        lambda _path: SimpleNamespace(total=2 * 1024**3, free=1024**3),
    )
    with pytest.raises(ValueError):
        settings.configure(expected_revision=0, minimum_free_bytes=3 * 1024**3)
    assert settings.snapshot()["revision"] == 0


def test_revision_prevents_lost_updates_and_replaced_settings_fail_closed(storage):
    settings, root, _, _ = storage
    settings.configure(expected_revision=0, retention_seconds=0)
    with pytest.raises(StorageSettingsConflict):
        settings.configure(expected_revision=0, quota_bytes=20)
    original = root / "operator-storage.json"
    replacement = root / "replacement.json"
    replacement.write_bytes(original.read_bytes())
    replacement.chmod(0o600)
    original.unlink()
    replacement.rename(original)
    with pytest.raises(StorageSettingsConflict):
        settings.snapshot()


def test_duplicate_and_foreign_site_settings_are_rejected(storage):
    settings, root, recordings, _ = storage
    settings.configure(expected_revision=0, retention_seconds=0)
    path = root / "operator-storage.json"
    document = path.read_text()
    path.write_text(document.replace('"site_id":', '"site_id":"foreign","site_id":'))
    with pytest.raises(StorageSettingsError):
        OperatorStorageSettings(root, settings._site_id, recordings)


@pytest.mark.parametrize("kind", ["relative", "ancestor", "inside_recordings", "identity_root"])
def test_unsafe_locations_never_become_pending(storage, kind):
    settings, root, recordings, _ = storage
    value = {
        "relative": "relative-path",
        "ancestor": str(recordings.parent),
        "inside_recordings": str(recordings / "nested"),
        "identity_root": str(root),
    }[kind]
    with pytest.raises(StorageDirectoryError):
        settings.configure(expected_revision=0, recording_directory=value)
    assert settings.snapshot()["pending_recording_directory"] is None


def test_linked_target_is_rejected_without_following_it(storage):
    settings, _, _, parent = storage
    foreign, link = parent / "foreign", parent / "target-link"
    foreign.mkdir(mode=0o700)
    try:
        link.symlink_to(foreign, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation requires Windows privilege")
    with pytest.raises(StorageDirectoryError):
        settings.configure(expected_revision=0, recording_directory=str(link))
    assert list(foreign.iterdir()) == []


def test_staging_location_preserves_active_history_until_verified_restart(storage):
    settings, root, recordings, parent = storage
    originals = _recordings(recordings)
    before = {path.relative_to(recordings): path.read_bytes() for path in originals}
    target = parent / "new-recordings"
    staged = settings.configure(expected_revision=0, recording_directory=str(target))
    assert staged["recording_directory"] == str(recordings)
    assert staged["location_status"] == "restart_required"
    assert not target.exists()
    progress = []
    active = settings.migrate_pending(progress=progress.append)
    assert active == target
    assert settings.snapshot()["location_status"] == "active"
    assert settings.snapshot()["pending_recording_directory"] is None
    for relative, payload in before.items():
        assert (recordings / relative).read_bytes() == payload
        assert (target / relative).read_bytes() == payload
        assert (target / relative).stat().st_nlink == 1
    assert progress[-1]["copied_bytes"] == progress[-1]["total_bytes"]
    assert (
        OperatorStorageSettings(root, settings._site_id, recordings).recording_directory == target
    )


def test_foreign_destination_is_preserved_and_old_history_remains_active(storage):
    settings, _, recordings, parent = storage
    originals = _recordings(recordings)
    target = parent / "not-empty"
    target.mkdir(mode=0o700)
    foreign = target / "owner-notes.txt"
    foreign.write_bytes(b"do not overwrite or remove")
    settings.configure(expected_revision=0, recording_directory=str(target))
    assert settings.migrate_pending() == recordings
    assert settings.snapshot()["migration_error"] == "destination_conflict"
    assert settings.snapshot()["location_status"] == "migration_required"
    assert foreign.read_bytes() == b"do not overwrite or remove"
    assert all(path.exists() for path in originals)


def test_failed_copy_is_resumable_and_never_activates_partial_history(storage, monkeypatch):
    settings, _, recordings, parent = storage
    originals = _recordings(recordings)
    target = parent / "retry-recordings"
    settings.configure(expected_revision=0, recording_directory=str(target))
    original_hash = settings._hash
    calls = 0

    def interrupted_hash(path, expected, deadline):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise TimeoutError
        return original_hash(path, expected, deadline)

    monkeypatch.setattr(settings, "_hash", interrupted_hash)
    assert settings.migrate_pending() == recordings
    assert settings.snapshot()["location_status"] == "migration_required"
    copied = next(target.rglob("*.mp4"))
    copied_identity = copied.stat().st_ino
    monkeypatch.setattr(settings, "_hash", original_hash)
    assert settings.migrate_pending() == target
    assert copied.stat().st_ino == copied_identity
    assert all(path.exists() for path in originals)


def test_source_change_during_migration_preserves_old_directory(storage):
    settings, _, recordings, parent = storage
    originals = _recordings(recordings)
    target = parent / "changing-recordings"
    settings.configure(expected_revision=0, recording_directory=str(target))

    def mutate_source(_progress):
        originals[0].write_bytes(b"new source content")

    assert settings.migrate_pending(progress=mutate_source) == recordings
    assert settings.snapshot()["migration_error"] == "source_changed"
    assert settings.recording_directory == recordings


def test_destination_change_after_verification_never_activates_directory(storage):
    settings, _, recordings, parent = storage
    _recordings(recordings)
    target = parent / "modified-destination"
    settings.configure(expected_revision=0, recording_directory=str(target))

    def mutate_destination(update):
        if update["phase"] == "copying":
            next(target.rglob("*.mp4")).write_bytes(b"changed destination content")

    assert settings.migrate_pending(progress=mutate_destination) == recordings
    assert settings.snapshot()["migration_error"] == "destination_conflict"
    assert settings.recording_directory == recordings


def test_restart_hook_activates_verified_directory_before_service_composition(storage):
    settings, root, recordings, parent = storage
    originals = _recordings(recordings)
    target = parent / "startup-target"
    settings.configure(expected_revision=0, recording_directory=str(target))
    policy = prepare_operator_recording_storage(
        state_root=root,
        site_id=settings._site_id,
        default_recording_directory=recordings,
    )
    assert policy["recording_directory"] == str(target)
    assert policy["pending_recording_directory"] is None
    assert all(path.exists() for path in originals)


def test_missing_persisted_policy_is_refused_instead_of_returning_defaults(storage):
    settings, root, _, _ = storage
    settings.configure(expected_revision=0, retention_seconds=0)
    (root / "operator-storage.json").unlink()
    with pytest.raises(StorageSettingsError):
        settings.snapshot()
    with pytest.raises(StorageSettingsError):
        OperatorStorageSettings(
            root, settings._site_id, Path(settings._document["recording_directory"])
        )


def test_existing_destination_mismatch_never_overwrites_either_copy(storage):
    settings, _, recordings, parent = storage
    originals = _recordings(recordings)
    target = parent / "conflicting-recordings"
    target.mkdir(mode=0o700)
    marker = target / ".k5-storage-target.json"
    marker.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "site_id": settings._site_id,
                "source_directory": str(recordings),
            }
        )
    )
    marker.chmod(0o600)
    directory = target / originals[0].parent.name
    directory.mkdir(mode=0o700)
    conflict = directory / originals[0].name
    conflict.write_bytes(b"unrelated prior destination")
    settings.configure(expected_revision=0, recording_directory=str(target))
    assert settings.migrate_pending() == recordings
    assert conflict.read_bytes() == b"unrelated prior destination"
    assert settings.snapshot()["migration_error"] == "destination_conflict"


def test_pending_target_can_be_cancelled_without_removing_files(storage):
    settings, _, recordings, parent = storage
    target = parent / "cancelled-target"
    settings.configure(expected_revision=0, recording_directory=str(target))
    cancelled = settings.configure(expected_revision=1, recording_directory="")
    assert cancelled["location_status"] == "active"
    assert settings.migrate_pending() == recordings
    assert not target.exists()


def test_interrupted_process_copy_preserves_unproven_temp_and_old_active_root(storage):
    settings, _, recordings, parent = storage
    originals = _recordings(recordings)
    target = parent / "interrupted-target"
    target.mkdir(mode=0o700)
    marker = target / ".k5-storage-target.json"
    marker.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "site_id": settings._site_id,
                "source_directory": str(recordings),
            }
        )
    )
    marker.chmod(0o600)
    directory = target / originals[0].parent.name
    directory.mkdir(mode=0o700)
    orphan = directory / (".k5-migrate-" + "a" * 32 + ".tmp")
    orphan.write_bytes(b"preserve incomplete or unknown bytes")
    settings.configure(expected_revision=0, recording_directory=str(target))
    progress = []
    assert settings.migrate_pending(progress=progress.append) == recordings
    assert progress[0] == {"phase": "preparing", "copied_bytes": 0, "total_bytes": 0}
    assert settings.snapshot()["migration_error"] == "interrupted_copy_preserved"
    assert settings.snapshot()["location_status"] == "migration_required"
    assert orphan.read_bytes() == b"preserve incomplete or unknown bytes"
    assert all(path.exists() for path in originals)
