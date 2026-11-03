from pathlib import Path

import pytest

from k5vision.domain.devices import DeviceCreate, DeviceProtocol
from k5vision.services.device_registry import (
    DeviceRegistry,
    DeviceRegistryConflictError,
    DeviceRegistryStorageError,
)


def test_registry_defaults_to_onvif() -> None:
    registry = DeviceRegistry()
    request = DeviceCreate(name="Camera 1", host="10.0.0.10")

    device = registry.register(request)

    assert DeviceProtocol.ONVIF in device.protocols
    assert registry.get(device.id) == device
    registry.close()


def test_registry_persists_idempotent_identity_and_conflict_state(tmp_path: Path) -> None:
    database = tmp_path / "devices.sqlite3"
    request = DeviceCreate(name="Camera 1", host="10.0.0.10")

    first_registry = DeviceRegistry(database_path=database, site_id="site-a")
    first = first_registry.enroll(request)
    repeated = first_registry.enroll(request)
    first_registry.close()

    second_registry = DeviceRegistry(database_path=database, site_id="site-a")
    restored = second_registry.get(first.device.id)
    with pytest.raises(DeviceRegistryConflictError):
        second_registry.enroll(DeviceCreate(name="Renamed", host="10.0.0.10"))
    listed = second_registry.list()
    second_registry.close()

    assert first.created is True
    assert repeated.created is False
    assert repeated.device.id == first.device.id
    assert restored == first.device
    assert listed == [first.device]


def test_registry_scopes_identical_endpoint_to_site(tmp_path: Path) -> None:
    database = tmp_path / "devices.sqlite3"
    request = DeviceCreate(name="Camera 1", host="10.0.0.10")

    site_a = DeviceRegistry(database_path=database, site_id="site-a")
    site_a_device = site_a.register(request)
    site_a.close()

    site_b = DeviceRegistry(database_path=database, site_id="site-b")
    assert site_b.get(site_a_device.id) is None
    site_b_device = site_b.register(request)
    assert site_b_device.id != site_a_device.id
    site_b.close()


def test_registry_rejects_invalid_scope_and_storage_configuration(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        DeviceRegistry(site_id="site with spaces")
    with pytest.raises(ValueError):
        DeviceRegistry(database_path=" ")
    with pytest.raises(DeviceRegistryStorageError):
        DeviceRegistry(database_path=tmp_path / "missing" / "devices.sqlite3")


def test_closed_registry_fails_without_deleting_durable_state(tmp_path: Path) -> None:
    database = tmp_path / "devices.sqlite3"
    registry = DeviceRegistry(database_path=database, site_id="site-a")
    device = registry.register(DeviceCreate(name="Camera 1", host="10.0.0.10"))
    registry.close()

    with pytest.raises(DeviceRegistryStorageError):
        registry.get(device.id)

    reopened = DeviceRegistry(database_path=database, site_id="site-a")
    assert reopened.get(device.id) == device
    reopened.close()


def test_name_host_lookup_is_exact_site_scoped_and_reports_bounded_ambiguity(
    tmp_path: Path,
) -> None:
    database = tmp_path / "devices.sqlite3"
    registry = DeviceRegistry(database_path=database, site_id="site-a")
    foreign = DeviceRegistry(database_path=database, site_id="site-b")
    try:
        devices = [
            registry.register(DeviceCreate(name="Camera", host="10.0.0.10", management_port=port))
            for port in (80, 8080, 8443)
        ]
        foreign.register(DeviceCreate(name="Camera", host="10.0.0.10"))
        assert registry.find_name_host("Camera", "10.0.0.10") == devices[:2]
        assert registry.find_name_host("camera", "10.0.0.10") == []
        assert registry.find_name_host("Camera", "10.0.0.11") == []
        assert len(foreign.find_name_host("Camera", "10.0.0.10")) == 1
    finally:
        registry.close()
        foreign.close()


def _legacy_registry(database: Path):
    """Create the schema used by the already-distributed Windows installer."""
    import sqlite3
    from uuid import uuid4

    identities = [str(uuid4()), str(uuid4())]
    with sqlite3.connect(database) as connection:
        connection.execute(
            """CREATE TABLE devices (
                row_id INTEGER PRIMARY KEY AUTOINCREMENT,
                site_id TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL,
                host TEXT NOT NULL, management_port INTEGER NOT NULL,
                kind TEXT NOT NULL, protocols_json TEXT NOT NULL, tags_json TEXT NOT NULL,
                UNIQUE(site_id, id), UNIQUE(site_id, host, management_port)
            )"""
        )
        for index, identity in enumerate(identities):
            connection.execute(
                "INSERT INTO devices VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    index + 2,
                    "site-a",
                    identity,
                    f"Camera {index}",
                    f"10.0.0.{index + 1}",
                    80,
                    "camera",
                    '["onvif"]',
                    "[]",
                ),
            )
        # An earlier removed device must not allow its row order to be reused.
        connection.execute("UPDATE sqlite_sequence SET seq = 40 WHERE name = 'devices'")
    return identities


def test_legacy_migration_preserves_identity_order_sequence_and_endpoint_semantics(tmp_path):
    import sqlite3

    database = tmp_path / "devices.sqlite3"
    identities = _legacy_registry(database)
    before = database.stat()
    registry = DeviceRegistry(database_path=database, site_id="site-a")
    try:
        assert [str(device.id) for device in registry.list()] == identities
        repeated = registry.enroll(DeviceCreate(name="Camera 0", host="10.0.0.1"))
        assert repeated.created is False and str(repeated.device.id) == identities[0]
        with pytest.raises(DeviceRegistryConflictError):
            registry.enroll(DeviceCreate(name="Conflicting", host="10.0.0.1"))
        registry.enroll(DeviceCreate(name="New", host="10.0.0.3"))
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT row_id FROM devices ORDER BY row_id").fetchall() == [
                (2,),
                (3,),
                (41,),
            ]
            assert connection.execute("SELECT stream_key FROM devices").fetchall() == [
                ("",),
                ("",),
                ("",),
            ]
            assert (
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE name='devices_before_streams'"
                ).fetchone()
                is None
            )
        assert database.stat().st_ino == before.st_ino
    finally:
        registry.close()


def test_stream_channels_share_real_endpoint_and_keep_private_identity_on_reopen(tmp_path):
    database = tmp_path / "devices.sqlite3"
    first_request = DeviceCreate(
        name="Recorder channel 1",
        host="10.0.0.10",
        management_port=554,
        protocols={DeviceProtocol.RTSP},
    )
    second_request = first_request.model_copy(update={"name": "Recorder channel 2"})
    registry = DeviceRegistry(database_path=database, site_id="site-a")
    first = registry.enroll_stream(first_request, "a" * 64)
    second = registry.enroll_stream(second_request, "b" * 64)
    assert first.created and second.created and first.device.id != second.device.id
    assert first.device.management_port == second.device.management_port == 554
    assert registry.enroll_stream(first_request, "a" * 64).created is False
    with pytest.raises(DeviceRegistryConflictError):
        registry.enroll_stream(second_request, "a" * 64)
    assert "stream_key" not in first.device.model_dump()
    # Ordinary endpoint identity remains independent and still rejects conflicts.
    ordinary = registry.enroll(DeviceCreate(name="ONVIF", host="10.0.0.10", management_port=554))
    assert ordinary.created
    with pytest.raises(DeviceRegistryConflictError):
        registry.enroll(DeviceCreate(name="Other ONVIF", host="10.0.0.10", management_port=554))
    registry.close()
    reopened = DeviceRegistry(database_path=database, site_id="site-a")
    try:
        assert reopened.enroll_stream(first_request, "a" * 64).device == first.device
        assert reopened.enroll_stream(second_request, "b" * 64).device == second.device
    finally:
        reopened.close()


@pytest.mark.parametrize("key", ["", "a" * 63, "a" * 65, "A" * 64, "rtsp://camera/private", None])
def test_stream_registry_rejects_nonopaque_identity(key):
    registry = DeviceRegistry()
    try:
        with pytest.raises(ValueError):
            registry.enroll_stream(
                DeviceCreate(name="Stream", host="10.0.0.1", protocols={DeviceProtocol.RTSP}), key
            )
        assert registry.list() == []
    finally:
        registry.close()


def test_stream_registry_scopes_capacity_and_preserves_idempotence_when_full(tmp_path):
    from k5vision.services.device_registry import DeviceRegistryCapacityError

    database = tmp_path / "devices.sqlite3"
    request = DeviceCreate(
        name="Stream", host="10.0.0.1", management_port=554, protocols={DeviceProtocol.RTSP}
    )
    first = DeviceRegistry(database_path=database, site_id="site-a", capacity=1)
    second = DeviceRegistry(database_path=database, site_id="site-b", capacity=1)
    try:
        own = first.enroll_stream(request, "a" * 64)
        foreign = second.enroll_stream(request, "a" * 64)
        assert own.device.id != foreign.device.id
        assert first.enroll_stream(request, "a" * 64).created is False
        with pytest.raises(DeviceRegistryCapacityError):
            first.enroll_stream(request, "b" * 64)
        assert first.list() == [own.device]
    finally:
        first.close()
        second.close()


def test_failed_legacy_migration_rolls_back_original_schema_and_rows(tmp_path, monkeypatch):
    import sqlite3

    database = tmp_path / "devices.sqlite3"
    identities = _legacy_registry(database)
    real_connect = sqlite3.connect

    class FailingMigration(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if "FROM devices_before_streams ORDER BY row_id" in sql:
                raise sqlite3.OperationalError("simulated interrupted migration")
            return super().execute(sql, parameters)

    with monkeypatch.context() as patched:
        patched.setattr(
            sqlite3,
            "connect",
            lambda *args, **kwargs: real_connect(*args, **kwargs, factory=FailingMigration),
        )
        with pytest.raises(DeviceRegistryStorageError):
            DeviceRegistry(database_path=database, site_id="site-a")
    with real_connect(database) as connection:
        assert [
            row[0] for row in connection.execute("SELECT id FROM devices ORDER BY row_id")
        ] == identities
        assert "stream_key" not in [
            row[1] for row in connection.execute("PRAGMA table_info(devices)")
        ]
        assert connection.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='devices'"
        ).fetchone() == (40,)
    recovered = DeviceRegistry(database_path=database, site_id="site-a")
    assert [str(device.id) for device in recovered.list()] == identities
    recovered.close()


def test_concurrent_startup_migrates_once_without_losing_channels(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    database = tmp_path / "devices.sqlite3"
    identities = _legacy_registry(database)

    def open_channel(index):
        registry = DeviceRegistry(database_path=database, site_id="site-a")
        try:
            return registry.enroll_stream(
                DeviceCreate(
                    name=f"Channel {index}",
                    host="10.0.0.9",
                    management_port=554,
                    protocols={DeviceProtocol.RTSP},
                ),
                str(index) * 64,
            ).device
        finally:
            registry.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        channels = list(pool.map(open_channel, (1, 2)))
    registry = DeviceRegistry(database_path=database, site_id="site-a")
    try:
        assert [str(device.id) for device in registry.list()[:2]] == identities
        assert {device.id for device in registry.list()[2:]} == {channel.id for channel in channels}
    finally:
        registry.close()


def test_legacy_migration_preserves_other_sites_and_remains_idempotent(tmp_path):
    import sqlite3
    from uuid import uuid4

    database = tmp_path / "devices.sqlite3"
    identities = _legacy_registry(database)
    foreign_id = str(uuid4())
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO devices VALUES (41, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("site-b", foreign_id, "Foreign", "10.0.0.1", 80, "camera", '["onvif"]', "[]"),
        )
    first = DeviceRegistry(database_path=database, site_id="site-a")
    first.close()
    foreign = DeviceRegistry(database_path=database, site_id="site-b")
    own = DeviceRegistry(database_path=database, site_id="site-a")
    try:
        assert [str(device.id) for device in own.list()] == identities
        assert [str(device.id) for device in foreign.list()] == [foreign_id]
        assert own.get(foreign.list()[0].id) is None
    finally:
        own.close()
        foreign.close()


def test_empty_legacy_registry_preserves_deleted_sequence_through_migration(tmp_path):
    import sqlite3

    database = tmp_path / "devices.sqlite3"
    _legacy_registry(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM devices")
    registry = DeviceRegistry(database_path=database)
    try:
        registry.enroll(DeviceCreate(name="New", host="10.0.0.1"))
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT row_id FROM devices").fetchone() == (41,)
    finally:
        registry.close()


def test_stream_identity_cannot_be_assigned_to_non_rtsp_device():
    registry = DeviceRegistry()
    try:
        with pytest.raises(ValueError):
            registry.enroll_stream(DeviceCreate(name="ONVIF", host="10.0.0.1"), "a" * 64)
        assert registry.list() == []
    finally:
        registry.close()


def test_private_stream_lookup_requires_exact_endpoint_key_and_site(tmp_path):
    database = tmp_path / "devices.sqlite3"
    request = DeviceCreate(
        name="Channel", host="10.0.0.1", management_port=554, protocols={DeviceProtocol.RTSP}
    )
    own = DeviceRegistry(database_path=database, site_id="site-a")
    foreign = DeviceRegistry(database_path=database, site_id="site-b")
    try:
        first = own.enroll_stream(request, "a" * 64).device
        second = foreign.enroll_stream(request, "a" * 64).device
        assert own.find_stream("10.0.0.1", 554, "a" * 64) == first
        assert foreign.find_stream("10.0.0.1", 554, "a" * 64) == second
        assert first.id != second.id
        assert own.find_stream("10.0.0.2", 554, "a" * 64) is None
        assert own.find_stream("10.0.0.1", 8554, "a" * 64) is None
        assert own.find_stream("10.0.0.1", 554, "b" * 64) is None
        with pytest.raises(ValueError):
            own.find_stream("10.0.0.1", 554, "rtsp://camera/private")
    finally:
        own.close()
        foreign.close()
