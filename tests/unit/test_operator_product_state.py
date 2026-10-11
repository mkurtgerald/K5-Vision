from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from k5vision import identity_state
from k5vision.domain.devices import Device, DeviceCreate, DeviceProtocol
from k5vision.operator_product_state import (
    MAX_OPERATOR_CAMERAS,
    OperatorCameraCapacityError,
    OperatorCameraStore,
    OperatorCameraStoreError,
)
from k5vision.services.device_registry import DeviceRegistry

_SECRET = "source-fixture-private-secret"


@pytest.fixture
def identity(tmp_path, monkeypatch):
    # The execution sandbox can present / and /tmp with nobody ownership. Model
    # ordinary trusted system ancestors only; admission of fixture paths, links,
    # files, modes and ownership below them remains the production implementation.
    if os.name == "posix":
        original_lstat = Path.lstat

        def ordinary_system_ancestor(path, *args, **kwargs):
            info = original_lstat(path, *args, **kwargs)
            if path in (Path("/"), Path("/tmp")) and info.st_uid == 65534:
                values = list(info)
                values[4] = 0
                return os.stat_result(values)
            return info

        monkeypatch.setattr(Path, "lstat", ordinary_system_ancestor)
    return identity_state.initialize_identity_state(
        tmp_path / "identity", username="fixture-admin", password=secrets.token_urlsafe(24)
    )


def _device(index=1) -> Device:
    return Device(
        id=UUID(int=index),
        name=f"Fixture camera {index}",
        host=f"192.0.2.{index}",
        protocols={DeviceProtocol.RTSP},
    )


def _uri(device: Device, suffix="live") -> str:
    return f"rtsp://fixture:{_SECRET}@{device.host}/{suffix}"


def _database(identity) -> Path:
    return identity.directory / "operator-cameras.sqlite3"


def _change(identity, sql, parameters=()) -> None:
    with sqlite3.connect(_database(identity)) as connection:
        connection.execute(sql, parameters)


def test_atomic_upsert_reload_remove_and_independent_device_objects(identity) -> None:
    first = _device()
    second = _device(2)
    store = OperatorCameraStore(identity.directory, identity.site_id)
    assert store.list_sources() == ()
    issued = store.upsert(first, _uri(first))
    first.name = "Caller mutation"
    issued.device.name = "Returned object mutation"
    store.upsert(second, _uri(second))
    assert store.get(first.id).device.name == "Fixture camera 1"
    before = store.get(first.id).binding.path_name
    store.close()

    reopened = OperatorCameraStore(identity.directory, identity.site_id)
    sources = reopened.list_sources()
    assert len(sources) == 2
    assert sources[0].source_uri == _uri(_device())
    assert sources[0].binding.path_name == before
    assert all(_SECRET not in repr(value) for value in (reopened, *sources))
    reopened.remove(first.id)
    reopened.remove(first.id)
    assert reopened.get(first.id) is None
    reopened.close()
    assert _database(identity).exists()
    last = OperatorCameraStore(identity.directory, identity.site_id)
    assert [source.device.id for source in last.list_sources()] == [second.id]
    last.close()


def test_store_checks_the_existing_site_scoped_device_registry(identity, tmp_path) -> None:
    registry = DeviceRegistry(database_path=tmp_path / "devices.sqlite3", site_id=identity.site_id)
    device = registry.enroll(
        DeviceCreate(name="Enrolled", host="192.0.2.10", protocols={DeviceProtocol.RTSP})
    ).device
    store = OperatorCameraStore(identity.directory, identity.site_id, registry=registry)
    store.upsert(device, _uri(device))
    with pytest.raises(OperatorCameraStoreError):
        store.upsert(_device(), _uri(_device()))
    changed = device.model_copy(update={"name": "Unenrolled metadata"})
    with pytest.raises(OperatorCameraStoreError):
        store.upsert(changed, _uri(changed))
    store.close()
    registry.close()

    foreign = DeviceRegistry(database_path=tmp_path / "foreign.sqlite3", site_id="different-site")
    with pytest.raises(OperatorCameraStoreError):
        OperatorCameraStore(identity.directory, identity.site_id, registry=foreign)
    foreign.close()


def test_capacity_limits_creates_but_allows_replacement(identity) -> None:
    store = OperatorCameraStore(identity.directory, identity.site_id)
    for index in range(1, MAX_OPERATOR_CAMERAS + 1):
        device = _device(index)
        store.upsert(device, _uri(device))
    extra = _device(MAX_OPERATOR_CAMERAS + 1)
    with pytest.raises(OperatorCameraCapacityError):
        store.upsert(extra, _uri(extra))
    assert len(store.list_sources()) == MAX_OPERATOR_CAMERAS
    device = _device()
    store.upsert(device, _uri(device, "replacement"))
    assert store.get(device.id).source_uri.endswith("/replacement")
    store.close()


def test_two_store_connections_preserve_concurrent_atomic_updates(identity) -> None:
    stores = [OperatorCameraStore(identity.directory, identity.site_id) for _ in range(2)]

    def update(index):
        device = _device(index)
        stores[index % 2].upsert(device, _uri(device))

    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(update, range(1, 9)))
        assert len(stores[0].list_sources()) == 8
        assert stores[0].list_sources() == stores[1].list_sources()
    finally:
        for store in stores:
            store.close()


def test_volatile_journal_commits_before_another_store_admits_files(identity, monkeypatch) -> None:
    writer = OperatorCameraStore(identity.directory, identity.site_id)
    reader = OperatorCameraStore(identity.directory, identity.site_id)
    commit_started = threading.Event()
    finish_commit = threading.Event()
    reader_started = threading.Event()
    reader_admitting = threading.Event()
    journal_observed = threading.Event()
    journal = Path(str(_database(identity)) + "-journal")

    def hold_commit(statement):
        if statement == "COMMIT":
            if journal.exists():
                journal_observed.set()
            commit_started.set()
            finish_commit.wait(5)

    original_admit = reader._admit_files

    def observed_admit():
        reader_admitting.set()
        return original_admit()

    def read():
        reader_started.set()
        return reader.list_sources()

    writer._connection.set_trace_callback(hold_commit)
    monkeypatch.setattr(reader, "_admit_files", observed_admit)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            write_future = executor.submit(writer.upsert, _device(), _uri(_device()))
            try:
                assert commit_started.wait(5)
                assert journal_observed.is_set()
                read_future = executor.submit(read)
                assert reader_started.wait(5)
                # The real journal exists until the writer is allowed to COMMIT.
                # Its deletion cannot occur between the reader's stat/ACL probes.
                assert not reader_admitting.wait(0.05)
            finally:
                finish_commit.set()
            committed = write_future.result(timeout=5)
            assert read_future.result(timeout=5) == (committed,)
            assert not journal.exists()
            assert reader_admitting.is_set()
    finally:
        finish_commit.set()
        writer._connection.set_trace_callback(None)
        writer.close()
        reader.close()


def test_failed_transaction_keeps_previous_source_and_has_no_raw_error_context(identity) -> None:
    store = OperatorCameraStore(identity.directory, identity.site_id)
    device = _device()
    store.upsert(device, _uri(device))
    _change(
        identity,
        "CREATE TRIGGER refuse_update BEFORE UPDATE ON camera_sources "
        "BEGIN SELECT RAISE(ABORT, 'source-fixture-private-secret'); END",
    )
    with pytest.raises(OperatorCameraStoreError) as raised:
        store.upsert(device, _uri(device, "changed"))
    assert raised.value.__context__ is None
    assert _SECRET not in str(raised.value)
    assert store.get(device.id).source_uri == _uri(device)
    store.close()
    reopened = OperatorCameraStore(identity.directory, identity.site_id)
    assert reopened.get(device.id).source_uri == _uri(device)
    reopened.close()


@pytest.mark.parametrize(
    "uri",
    [
        f"rtsp://fixture:{_SECRET}@192.0.2.2/live",
        f"http://fixture:{_SECRET}@192.0.2.1/live",
        f"rtsp://fixture:{_SECRET}@192.0.2.1/live#fragment",
        f"rtsp://fixture:{_SECRET}@192.0.2.1:0/live",
    ],
)
def test_invalid_uri_is_rejected_before_persistence_without_secret_diagnostics(
    identity, uri
) -> None:
    store = OperatorCameraStore(identity.directory, identity.site_id)
    with pytest.raises(OperatorCameraStoreError) as raised:
        store.upsert(_device(), uri)
    assert raised.value.__context__ is None
    assert _SECRET not in str(raised.value)
    assert store.list_sources() == ()
    store.close()


@pytest.mark.parametrize(
    "sql,parameters",
    [
        ("UPDATE source_state SET site_id = 'different-site'", ()),
        ("UPDATE source_state SET schema_version = 2", ()),
        ("INSERT INTO source_state SELECT * FROM source_state", ()),
        ("DELETE FROM source_state", ()),
        ("UPDATE camera_sources SET device_json = ?", ('{"id": "bad"}',)),
        ("UPDATE camera_sources SET device_json = ?", ('{"name":"one","name":"two"}',)),
        ("UPDATE camera_sources SET source_uri = ?", (f"rtsp://u:{_SECRET}@192.0.2.2/live",)),
        ("UPDATE camera_sources SET device_id = ?", (str(uuid4()),)),
    ],
)
def test_corrupt_site_device_and_source_state_fail_closed_without_reset(
    identity, sql, parameters
) -> None:
    store = OperatorCameraStore(identity.directory, identity.site_id)
    device = _device()
    store.upsert(device, _uri(device))
    _change(identity, sql, parameters)
    before = _database(identity).read_bytes()
    with pytest.raises(OperatorCameraStoreError) as raised:
        store.list_sources()
    assert raised.value.__context__ is None
    assert _SECRET not in repr(raised.value)
    store.close()
    with pytest.raises(OperatorCameraStoreError):
        OperatorCameraStore(identity.directory, identity.site_id)
    assert _database(identity).read_bytes() == before


def test_metadata_json_cannot_smuggle_unknown_public_device_fields(identity) -> None:
    store = OperatorCameraStore(identity.directory, identity.site_id)
    device = _device()
    store.upsert(device, _uri(device))
    value = device.model_dump(mode="json")
    value["source_uri"] = _uri(device)
    _change(identity, "UPDATE camera_sources SET device_json = ?", (json.dumps(value),))
    with pytest.raises(OperatorCameraStoreError):
        store.list_sources()
    store.close()


def test_foreign_identity_and_relative_root_do_not_create_store(identity) -> None:
    with pytest.raises(OperatorCameraStoreError):
        OperatorCameraStore(identity.directory, "different-site")
    with pytest.raises(OperatorCameraStoreError):
        OperatorCameraStore(Path("relative-identity"), identity.site_id)
    assert not _database(identity).exists()


def test_existing_empty_or_non_database_file_is_not_adopted(identity) -> None:
    database = _database(identity)
    database.write_bytes(b"incomplete-private-state")
    database.chmod(0o600)
    with pytest.raises(OperatorCameraStoreError):
        OperatorCameraStore(identity.directory, identity.site_id)
    assert database.read_bytes() == b"incomplete-private-state"
    database.write_bytes(b"")
    with pytest.raises(OperatorCameraStoreError):
        OperatorCameraStore(identity.directory, identity.site_id)
    assert database.read_bytes() == b""


def test_missing_or_replaced_database_is_never_silently_recreated(identity, tmp_path) -> None:
    store = OperatorCameraStore(identity.directory, identity.site_id)
    source = store.upsert(_device(), _uri(_device()))
    database = _database(identity)
    moved = tmp_path / "preserved.sqlite3"
    if os.name == "nt":
        # SQLite's Windows handle refuses deletion/rename while it owns the DB.
        # Verify this real protection and unchanged access rather than forcing a
        # POSIX-only replacement operation through a live Windows handle.
        before = database.read_bytes()
        with pytest.raises(OSError) as blocked:
            database.rename(moved)
        assert blocked.value.winerror == 32
        assert database.exists() and not moved.exists()
        assert database.read_bytes() == before
        assert store.list_sources() == (source,)
        store.close()
        database.rename(moved)
        database.write_bytes(moved.read_bytes())
        database.chmod(0o600)
    else:
        database.rename(moved)
        with pytest.raises(OperatorCameraStoreError):
            store.list_sources()
        assert not database.exists()
        database.write_bytes(moved.read_bytes())
        database.chmod(0o600)
        with pytest.raises(OperatorCameraStoreError):
            store.list_sources()
        store.close()

    # After handle closure either OS permits replacement. Startup must still
    # refuse a valid SQLite replacement belonging to another identity site.
    _change(identity, "UPDATE source_state SET site_id = 'foreign-site'")
    before_reopen = database.read_bytes()
    with pytest.raises(OperatorCameraStoreError):
        OperatorCameraStore(identity.directory, identity.site_id)
    assert database.read_bytes() == before_reopen


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode admission")
@pytest.mark.parametrize("target", ["directory", "database"])
def test_shared_identity_or_database_permissions_refuse_access(identity, target) -> None:
    store = OperatorCameraStore(identity.directory, identity.site_id)
    path = identity.directory if target == "directory" else _database(identity)
    path.chmod(0o755 if target == "directory" else 0o644)
    try:
        with pytest.raises(OperatorCameraStoreError):
            store.list_sources()
    finally:
        path.chmod(0o700 if target == "directory" else 0o600)
        store.close()


def test_windows_private_admission_refusal_is_propagated_safely(identity, monkeypatch) -> None:
    original = identity_state._windows_admission

    def refused(path, **kwargs):
        if path.name == "operator-cameras.sqlite3":
            raise identity_state.IdentityStateError("native ownership refusal")
        return original(path, **kwargs)

    monkeypatch.setattr(identity_state, "_windows_admission", refused)
    with pytest.raises(OperatorCameraStoreError) as raised:
        OperatorCameraStore(identity.directory, identity.site_id)
    assert raised.value.__context__ is None


@pytest.mark.skipif(
    os.name == "nt", reason="Native reparse refusal has existing Windows qualification"
)
@pytest.mark.parametrize("target", ["database", "sidecar", "root"])
def test_symlinked_database_sidecar_and_identity_roots_are_refused(
    identity, tmp_path, target
) -> None:
    store = OperatorCameraStore(identity.directory, identity.site_id)
    database = _database(identity)
    if target == "root":
        link = tmp_path / "linked-identity"
        link.symlink_to(identity.directory, target_is_directory=True)
        with pytest.raises(OperatorCameraStoreError):
            OperatorCameraStore(link, identity.site_id)
    elif target == "database":
        preserved = tmp_path / "preserved.sqlite3"
        database.rename(preserved)
        database.symlink_to(preserved)
        with pytest.raises(OperatorCameraStoreError):
            store.list_sources()
    else:
        target_file = tmp_path / "other-file"
        target_file.write_text("private")
        Path(str(database) + "-wal").symlink_to(target_file)
        with pytest.raises(OperatorCameraStoreError):
            store.list_sources()
    store.close()


def test_hardlinked_database_is_refused(identity, tmp_path) -> None:
    store = OperatorCameraStore(identity.directory, identity.site_id)
    linked = tmp_path / "hardlink.sqlite3"
    os.link(_database(identity), linked)
    try:
        with pytest.raises(OperatorCameraStoreError):
            store.list_sources()
    finally:
        linked.unlink()
        store.close()


def test_closed_store_has_no_reusable_handle_and_preserves_state(identity) -> None:
    store = OperatorCameraStore(identity.directory, identity.site_id)
    store.upsert(_device(), _uri(_device()))
    store.close()
    store.close()
    for operation in (
        store.list_sources,
        lambda: store.upsert(_device(), _uri(_device())),
        lambda: store.remove(_device().id),
    ):
        with pytest.raises(OperatorCameraStoreError):
            operation()
    assert _database(identity).exists()
