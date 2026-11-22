from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import test_operator_time_admin as time_helpers

from k5vision import identity_state
from k5vision import legacy_media_stage as media
from k5vision.operator_identity_security import AccountSecurityStore
from k5vision.operator_profile_lock import OperatorProfileLock
from k5vision.operator_storage_settings import OperatorStorageSettings

private_profile = time_helpers.private_profile


def _write(path, value):
    path.write_bytes(value)
    path.chmod(0o600)


def _hashes(root):
    return {
        path.relative_to(root).as_posix(): (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            path.stat().st_mtime_ns,
        )
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.fixture
def legacy(private_profile, monkeypatch):
    base = private_profile / "legacy-site"
    base.mkdir(mode=0o700)
    state = identity_state.initialize_identity_state(
        base / "identity", username="legacy-admin", password="legacy-media-test-password"
    )
    AccountSecurityStore(state.directory, state.site_id).close()
    recordings = base / "recordings"
    recordings.mkdir(mode=0o700)
    camera = recordings / ("k5_" + "1" * 64)
    camera.mkdir(mode=0o700)
    # Byte-transfer tests deliberately make no decoder or native MP4 claim.
    first = camera / "2026-11-22_02-00-00-000000_+0530.mp4"
    second = camera / "2026-11-22_02-01-00-000000_-0400.mp4"
    _write(first, b"retained first segment" * 100)
    _write(second, b"retained second segment" * 100)
    os.utime(first, ns=(1760000000123400000, 1760000000123400000))
    exports = state.directory / "exports"
    exports.mkdir(mode=0o700)
    identifier, owner, device = str(uuid4()), str(uuid4()), str(uuid4())
    completed = exports / (identifier + ".mp4")
    _write(completed, b"retained completed export" * 100)
    document = {
        "schema_version": 1,
        "site_id": state.site_id,
        "id": identifier,
        "owner": owner,
        "camera": device,
        "start": "2026-11-22T02:00:00+05:30",
        "duration": 3.0,
        "created": "2026-11-22T02:02:00Z",
        "bytes": completed.stat().st_size,
        "sha256": hashlib.sha256(completed.read_bytes()).hexdigest(),
    }
    _write(exports / (identifier + ".json"), json.dumps(document).encode())
    lease = OperatorProfileLock(state.directory)
    lease.acquire()
    fixture = SimpleNamespace(
        base=base,
        identity=state.directory,
        site_id=state.site_id,
        recordings=recordings,
        camera=camera,
        exports=exports,
        first=first,
        second=second,
        completed=completed,
        metadata=exports / (identifier + ".json"),
        lease=lease,
        destination=private_profile / "media-stage",
        transfer_id=str(uuid4()),
        revoked=False,
    )

    def authorize():
        if fixture.revoked:
            raise RuntimeError("PRIVATE revoked transport detail")

    fixture.authorize = authorize
    monkeypatch.setattr(
        media.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=8 * 1024**4, total=8 * 1024**4, used=0),
    )
    yield fixture
    lease.close()


def _inspect(legacy, **changes):
    source_digest = changes.pop("source_digest", None)
    if source_digest is None:
        source_digest = media._source_digest(legacy.identity, lambda: None)[0]
    arguments = {
        "lease": legacy.lease,
        "transfer_id": legacy.transfer_id,
        "source_digest": source_digest,
        "authority_check": legacy.authorize,
    }
    arguments.update(changes)
    return media.inspect_media_source(legacy.identity, **arguments)


def _prepare(legacy, inventory=None, **changes):
    arguments = {"lease": legacy.lease, "authority_check": legacy.authorize}
    arguments.update(changes)
    return media.prepare_media_stage(inventory or _inspect(legacy), legacy.destination, **arguments)


def test_copy_preserves_every_retained_byte_offset_filename_metadata_and_source(legacy):
    original = _hashes(legacy.base)
    assert not (legacy.identity / "operator-storage.json").exists()
    inventory = _inspect(legacy)
    receipt = _prepare(legacy, inventory)
    assert receipt["phase"] == "media_prepared" and receipt["activation_allowed"] is False
    assert receipt["file_count"] == 4 and receipt["total_bytes"] == sum(
        e.bytes for e in inventory.entries
    )
    for entry in inventory.entries:
        source = media._entry_source(inventory, entry)
        target = legacy.destination / entry.section / entry.relative
        assert target.read_bytes() == source.read_bytes()
        assert target.stat().st_mtime_ns == source.stat().st_mtime_ns
    assert _hashes(legacy.base) == original
    assert not (legacy.identity / "operator-storage.json").exists()
    assert not (legacy.destination / "identity.json").exists()


def test_absent_default_media_roots_are_preserved_without_initializing_old_directories(legacy):
    shutil.rmtree(legacy.recordings)
    shutil.rmtree(legacy.exports)
    original = _hashes(legacy.base)
    inventory = _inspect(legacy)
    assert inventory.entries == () and inventory.total_bytes == 0
    assert _prepare(legacy, inventory)["file_count"] == 0
    assert not legacy.recordings.exists() and not legacy.exports.exists()
    assert _hashes(legacy.base) == original


def test_dangling_default_reparse_root_cannot_be_mistaken_for_absent_footage(legacy, monkeypatch):
    if os.name == "nt":
        pytest.skip("The separate native flow must exercise actual Windows junction creation")
    shutil.rmtree(legacy.recordings)
    legacy.recordings.symlink_to(legacy.base / "unavailable-volume", target_is_directory=True)
    original = Path.is_symlink

    def junction_shape(path):
        # Windows directory junctions need not report is_symlink(). The real
        # no-link path admission must inspect lstat/reparse state independently.
        return False if path == legacy.recordings else original(path)

    monkeypatch.setattr(Path, "is_symlink", junction_shape)
    assert not legacy.recordings.exists() and not legacy.recordings.is_symlink()
    with pytest.raises(media.LegacyMediaError):
        _inspect(legacy)
    assert not legacy.destination.exists()


def test_zero_byte_retained_segment_still_emits_an_explicit_end_of_file(legacy):
    empty = legacy.camera / "2026-11-22_02-02-00-000000_Z.mp4"
    _write(empty, b"")
    inventory = _inspect(legacy)
    messages = []

    def emit(note, block):
        if note["relative"].endswith(empty.name):
            messages.append((note, block))

    media.stream_media_source(
        inventory, lease=legacy.lease, authority_check=legacy.authorize, emit=emit
    )
    assert len(messages) == 1 and messages[0][0]["eof"] is True
    assert messages[0][0]["offset"] == 0 and messages[0][1] == b""


def test_completed_stage_resume_verifies_without_overwriting_or_reauthenticating_paths(legacy):
    inventory = _inspect(legacy)
    _prepare(legacy, inventory)
    before = _hashes(legacy.destination)
    ids = {
        p.relative_to(legacy.destination): p.stat().st_ino for p in legacy.destination.rglob("*")
    }
    assert _prepare(legacy, _inspect(legacy))["phase"] == "media_prepared"
    assert _hashes(legacy.destination) == before
    assert {
        p.relative_to(legacy.destination): p.stat().st_ino for p in legacy.destination.rglob("*")
    } == ids


def test_stream_has_bounded_frames_contiguous_offsets_real_eof_and_no_absolute_paths(legacy):
    inventory, received, offsets, eof = _inspect(legacy), {}, {}, []
    original = _hashes(legacy.base)

    def emit(note, block):
        assert len(block) <= 111 and str(legacy.base) not in json.dumps(note)
        index = note["index"]
        assert note["offset"] == offsets.get(index, 0)
        if note["eof"]:
            assert block == b""
            eof.append(index)
        else:
            received.setdefault(index, bytearray()).extend(block)
            offsets[index] = offsets.get(index, 0) + len(block)

    result = media.stream_media_source(
        inventory, lease=legacy.lease, authority_check=legacy.authorize, emit=emit, chunk_bytes=111
    )
    assert result["phase"] == "media_streamed" and result["activation_allowed"] is False
    assert eof == list(range(len(inventory.entries)))
    for index, entry in enumerate(inventory.entries):
        assert bytes(received[index]) == media._entry_source(inventory, entry).read_bytes()
    assert _hashes(legacy.base) == original and not legacy.destination.exists()


def test_saved_storage_policy_selects_only_its_admitted_active_directory(legacy, private_profile):
    target = private_profile / "old-custom-storage"
    settings = OperatorStorageSettings(legacy.identity, legacy.site_id, legacy.recordings)
    settings.configure(expected_revision=0, recording_directory=str(target))
    settings.migrate_pending()
    original = _hashes(legacy.base)
    inventory = _inspect(legacy)
    assert inventory.recordings == target
    assert _prepare(legacy, inventory)["file_count"] == 4
    assert _hashes(legacy.base) == original


@pytest.mark.parametrize(
    "condition", ["unheld", "wrong_root", "source_digest", "transfer", "revoked", "boolean"]
)
def test_unbound_or_revoked_authority_cannot_inspect_or_create_stage(
    legacy, private_profile, condition
):
    arguments = {}
    if condition == "unheld":
        legacy.lease.close()
    elif condition == "wrong_root":
        arguments["lease"] = OperatorProfileLock(private_profile / "other")
    elif condition == "source_digest":
        arguments["source_digest"] = "f" * 64
    elif condition == "transfer":
        arguments["transfer_id"] = "not-a-transfer"
    elif condition == "revoked":
        legacy.revoked = True
    else:
        arguments["authority_check"] = lambda: True
    with pytest.raises(media.LegacyMediaError) as error:
        _inspect(legacy, **arguments)
    assert str(error.value) == "Legacy media transfer is unavailable"
    assert error.value.__cause__ is None and error.value.__context__ is None
    assert not legacy.destination.exists()


@pytest.mark.parametrize(
    "condition", ["lost_policy", "pending", "wrong_site", "invalid_anchor", "closed_wal"]
)
def test_policy_read_never_repairs_or_initializes_invalid_legacy_state(legacy, condition):
    settings = OperatorStorageSettings(legacy.identity, legacy.site_id, legacy.recordings)
    settings.configure(expected_revision=0, quota_bytes=2 * 1024**3)
    original_digest = media._source_digest(legacy.identity, lambda: None)[0]
    path = legacy.identity / "operator-storage.json"
    if condition == "lost_policy":
        path.unlink()
    elif condition in {"pending", "wrong_site"}:
        value = json.loads(path.read_bytes())
        value["pending_recording_directory" if condition == "pending" else "site_id"] = (
            str(legacy.base / "pending") if condition == "pending" else "install-" + uuid4().hex
        )
        _write(path, json.dumps(value).encode())
    else:
        with sqlite3.connect(legacy.identity / "users.sqlite3") as connection:
            if condition == "closed_wal":
                assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
            else:
                connection.execute(
                    "UPDATE operator_component_anchors SET schema_version=2 "
                    "WHERE component='storage'"
                )
        connection.close()
    original = _hashes(legacy.base)
    with pytest.raises(media.LegacyMediaError):
        if condition == "closed_wal":
            _inspect(legacy, source_digest=original_digest)
        else:
            _inspect(legacy)
    assert _hashes(legacy.base) == original and not legacy.destination.exists()


@pytest.mark.parametrize(
    "condition",
    [
        "part_export",
        "orphan_export",
        "bad_export_sha",
        "wrong_export_site",
        "unknown_recording",
        "symlink",
        "hardlink",
    ],
)
def test_unknown_incomplete_or_unbound_media_is_preserved_and_refused(legacy, condition):
    if condition == "part_export":
        _write(legacy.exports / (str(uuid4()) + ".part"), b"interrupted export")
    elif condition == "orphan_export":
        _write(legacy.exports / (str(uuid4()) + ".mp4"), b"orphan bytes")
    elif condition in {"bad_export_sha", "wrong_export_site"}:
        value = json.loads(legacy.metadata.read_bytes())
        value["sha256" if condition == "bad_export_sha" else "site_id"] = "0" * 64
        _write(legacy.metadata, json.dumps(value).encode())
    elif condition == "unknown_recording":
        _write(legacy.camera / "unknown-data.bin", b"unclaimed bytes")
    elif condition == "symlink":
        if os.name == "nt":
            pytest.skip("Creating symlinks needs a separately qualified Windows privilege")
        (legacy.camera / "2026-11-22_03-00-00-000000_Z.mp4").symlink_to(legacy.first)
    else:
        os.link(legacy.first, legacy.camera / "2026-11-22_03-00-00-000000_Z.mp4")
    original = _hashes(legacy.base)
    with pytest.raises(media.LegacyMediaError):
        _inspect(legacy)
    assert _hashes(legacy.base) == original and not legacy.destination.exists()


@pytest.mark.parametrize(
    "condition",
    ["unmarked", "foreign_transfer", "changed_media", "partial", "unknown", "moved_root"],
)
def test_existing_or_changed_destination_is_never_overwritten(legacy, condition):
    inventory = _inspect(legacy)
    if condition == "unmarked":
        legacy.destination.mkdir(mode=0o700)
    else:
        _prepare(legacy, inventory)
        path = legacy.destination / inventory.entries[0].section / inventory.entries[0].relative
        if condition == "foreign_transfer":
            value = json.loads((legacy.destination / media._MARKER).read_bytes())
            value["transfer_id"] = str(uuid4())
            _write(legacy.destination / media._MARKER, json.dumps(value).encode())
        elif condition == "changed_media":
            with path.open("ab") as stream:
                stream.write(b"changed")
        elif condition == "partial":
            path.write_bytes(path.read_bytes()[:10])
        elif condition == "unknown":
            _write(legacy.destination / "unclaimed.bin", b"unrelated")
        else:
            saved = legacy.destination.with_name("old-stage")
            legacy.destination.rename(saved)
            shutil.copytree(saved, legacy.destination)
    original, target = _hashes(legacy.base), _hashes(legacy.destination)
    with pytest.raises(media.LegacyMediaError):
        _prepare(legacy, inventory)
    assert _hashes(legacy.base) == original and _hashes(legacy.destination) == target


def test_final_authority_callback_cannot_tamper_with_a_completed_stage(legacy):
    inventory = _inspect(legacy)
    changed = []

    def authorize():
        paths = [legacy.destination / entry.section / entry.relative for entry in inventory.entries]
        if not changed and all(
            p.exists() and p.stat().st_size == e.bytes
            for p, e in zip(paths, inventory.entries, strict=True)
        ):
            changed.append(True)
            with paths[0].open("ab") as stream:
                stream.write(b"late tamper")

    with pytest.raises(media.LegacyMediaError):
        _prepare(legacy, inventory, authority_check=authorize)
    assert changed == [True]


def test_stream_revocation_and_source_changes_cannot_complete(legacy):
    inventory = _inspect(legacy)
    original = _hashes(legacy.base)
    count = []

    def emit(_note, _block):
        count.append(True)
        legacy.revoked = True

    with pytest.raises(media.LegacyMediaError):
        media.stream_media_source(
            inventory, lease=legacy.lease, authority_check=legacy.authorize, emit=emit
        )
    assert count and _hashes(legacy.base) == original


def test_native_copy_dispatch_uses_only_admitted_source_paths_and_preserves_source(legacy):
    inventory = _inspect(legacy)
    original = _hashes(legacy.base)
    observed = []

    def copy_entry(source, document):
        observed.append((source, document))
        assert document["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()

    result = media.copy_media_source(
        inventory, lease=legacy.lease, authority_check=legacy.authorize, copy_entry=copy_entry
    )
    assert result["phase"] == "media_copy_dispatched" and result["activation_allowed"] is False
    assert len(observed) == 4
    for source, document in observed:
        assert source == media._entry_source(inventory, inventory.entries[document["index"]])
    assert _hashes(legacy.base) == original and not legacy.destination.exists()


def test_native_copy_dispatch_rejects_source_tampering_by_last_callback(legacy):
    inventory = _inspect(legacy)
    count = []

    def copy_entry(source, _document):
        count.append(True)
        if len(count) == len(inventory.entries):
            with source.open("ab") as stream:
                stream.write(b"changed after copy")

    with pytest.raises(media.LegacyMediaError):
        media.copy_media_source(
            inventory, lease=legacy.lease, authority_check=legacy.authorize, copy_entry=copy_entry
        )
    assert count


@pytest.mark.parametrize("condition", ["bytes", "entries", "space", "expired"])
def test_resource_budgets_refuse_without_changing_originals(legacy, monkeypatch, condition):
    original = _hashes(legacy.base)
    if condition == "bytes":
        with pytest.raises(media.LegacyMediaError):
            _inspect(legacy, max_bytes=1)
    elif condition == "entries":
        monkeypatch.setattr(media, "MAX_STORAGE_ENTRIES", 1)
        with pytest.raises(media.LegacyMediaError):
            _inspect(legacy)
    else:
        inventory = _inspect(legacy)
        if condition == "space":
            monkeypatch.setattr(media.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
        else:
            monkeypatch.setattr(
                media, "time", SimpleNamespace(monotonic=lambda: inventory.deadline + 1)
            )
        with pytest.raises(media.LegacyMediaError):
            _prepare(legacy, inventory)
    assert _hashes(legacy.base) == original
