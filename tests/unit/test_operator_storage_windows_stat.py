"""Windows birth-time/path versus change-time/descriptor regression coverage."""

from __future__ import annotations

import os
import stat
from types import SimpleNamespace

import pytest
import test_operator_storage_settings as settings_helpers

from k5vision import operator_storage_settings as settings_module

storage = settings_helpers.storage


def _copy_stat(value, **changes):
    fields = {name: getattr(value, name) for name in dir(value) if name.startswith("st_")}
    fields.update(changes)
    return SimpleNamespace(**fields)


def test_windows_birth_time_path_and_descriptor_change_time_allow_unchanged_reload(
    storage, monkeypatch
):
    settings, root, recordings, _ = storage
    expected = settings.configure(expected_revision=0, retention_seconds=0)
    path = root / "operator-storage.json"
    identity = path.stat().st_dev, path.stat().st_ino
    original = os.fstat

    def distinct_change_time(descriptor):
        value = original(descriptor)
        if (value.st_dev, value.st_ino) == identity:
            return _copy_stat(value, st_ctime_ns=value.st_ctime_ns + 10**9)
        return value

    monkeypatch.setattr(settings_module.os, "fstat", distinct_change_time)
    restored = settings_module.OperatorStorageSettings(root, settings._site_id, recordings)
    assert restored.snapshot() == expected


def test_windows_birth_time_path_and_descriptor_change_time_allow_verified_migration(
    storage, monkeypatch
):
    settings, _, recordings, parent = storage
    originals = settings_helpers._recordings(recordings)
    target = parent / "windows-compatible-target"
    settings.configure(expected_revision=0, recording_directory=str(target))
    original = os.fstat

    def distinct_change_time(descriptor):
        value = original(descriptor)
        if stat.S_ISREG(value.st_mode):
            return _copy_stat(value, st_ctime_ns=value.st_ctime_ns + 10**9)
        return value

    monkeypatch.setattr(settings_module.os, "fstat", distinct_change_time)
    assert settings.migrate_pending() == target
    for path in originals:
        assert (target / path.relative_to(recordings)).read_bytes() == path.read_bytes()


@pytest.mark.parametrize("kind", ["inode", "size", "not_regular", "hardlinked"])
def test_descriptor_identity_type_size_and_link_checks_remain_mandatory(storage, monkeypatch, kind):
    settings, root, _, _ = storage
    settings.configure(expected_revision=0, retention_seconds=0)
    path = root / "operator-storage.json"
    identity = path.stat().st_dev, path.stat().st_ino
    original = os.fstat

    def changed(descriptor):
        value = original(descriptor)
        if (value.st_dev, value.st_ino) != identity:
            return value
        changes = {
            "inode": {"st_ino": value.st_ino + 1},
            "size": {"st_size": value.st_size + 1},
            "not_regular": {"st_mode": stat.S_IFDIR | 0o700},
            "hardlinked": {"st_nlink": 2},
        }[kind]
        return _copy_stat(value, **changes)

    monkeypatch.setattr(settings_module.os, "fstat", changed)
    with pytest.raises(settings_module.StorageSettingsError):
        settings_module._read(path)


def test_full_descriptor_signature_change_during_settings_read_is_refused(storage, monkeypatch):
    settings, root, _, _ = storage
    settings.configure(expected_revision=0, retention_seconds=0)
    path = root / "operator-storage.json"
    identity = path.stat().st_dev, path.stat().st_ino
    original = os.fstat
    count = 0

    def changing(descriptor):
        nonlocal count
        value = original(descriptor)
        if (value.st_dev, value.st_ino) != identity:
            return value
        count += 1
        return _copy_stat(value, st_ctime_ns=value.st_ctime_ns + 10**9 + count)

    monkeypatch.setattr(settings_module.os, "fstat", changing)
    with pytest.raises(settings_module.StorageSettingsError):
        settings_module._read(path)


def test_full_path_signature_change_during_settings_read_is_refused(storage, monkeypatch):
    settings, root, _, _ = storage
    settings.configure(expected_revision=0, retention_seconds=0)
    path = root / "operator-storage.json"
    original = settings_module._file
    count = 0

    def changing(value):
        nonlocal count
        result = original(value)
        if value == path:
            count += 1
            if count > 1:
                return _copy_stat(result, st_mtime_ns=result.st_mtime_ns + 1)
        return result

    monkeypatch.setattr(settings_module, "_file", changing)
    with pytest.raises(settings_module.StorageSettingsError):
        settings_module._read(path)


@pytest.mark.parametrize("change_during", ["hash", "copy"])
def test_full_descriptor_change_during_hash_or_copy_keeps_old_history_active(
    storage, monkeypatch, change_during
):
    settings, _, recordings, parent = storage
    originals = settings_helpers._recordings(recordings)
    identities = {(path.stat().st_dev, path.stat().st_ino) for path in originals}
    target = parent / "rejected-mutating-copy"
    settings.configure(expected_revision=0, recording_directory=str(target))
    original = os.fstat
    counts = {}

    def changing(descriptor):
        value = original(descriptor)
        identity = value.st_dev, value.st_ino
        if identity not in identities:
            return value
        counts[identity] = counts.get(identity, 0) + 1
        changed_at = 2 if change_during == "hash" else 4
        return _copy_stat(
            value,
            st_ctime_ns=value.st_ctime_ns + 10**9 + (counts[identity] >= changed_at),
        )

    monkeypatch.setattr(settings_module.os, "fstat", changing)
    assert settings.migrate_pending() == recordings
    assert settings.snapshot()["migration_error"] == "source_changed"
    assert all(path.exists() for path in originals)
