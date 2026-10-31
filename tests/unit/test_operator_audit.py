from __future__ import annotations

import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from k5vision import identity_state
from k5vision.operator_audit import OperatorAuditError, OperatorAuditStore


@pytest.fixture
def audit_identity(tmp_path, monkeypatch):
    if os.name == "posix":
        original = Path.lstat

        def system_ancestors(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path in (Path("/"), Path("/tmp")) and info.st_uid == 65534:
                fields = list(info)
                fields[4] = 0
                return os.stat_result(fields)
            return info

        monkeypatch.setattr(Path, "lstat", system_ancestors)
    return identity_state.initialize_identity_state(
        tmp_path / "audit-identity", username="audit-admin", password="audit-test-password"
    )


@pytest.fixture
def audit_store(audit_identity):
    store = OperatorAuditStore(audit_identity.directory, audit_identity.site_id)
    yield store
    store.close()


def test_append_order_and_elapsed_time_survive_utc_rollback_and_restart(audit_identity):
    clock = SimpleNamespace(utc=datetime(2026, 10, 31, 12, tzinfo=UTC), elapsed=100.0)
    store = OperatorAuditStore(
        audit_identity.directory,
        audit_identity.site_id,
        utc_now=lambda: clock.utc,
        monotonic=lambda: clock.elapsed,
    )
    actor, camera, request = uuid4(), uuid4(), uuid4()
    assert (
        store.record(
            "camera.enroll", actor=actor, target_kind="camera", target_id=camera, request_id=request
        )
        == 1
    )
    clock.utc -= timedelta(hours=48)
    clock.elapsed += 2.5
    assert (
        store.record("recording.configure", actor=actor, target_id=camera, changes=("enabled",))
        == 2
    )
    data = store.events(limit=1)
    assert data["has_more"] and data["next_before"] == 2
    latest = data["events"][0]
    assert latest["id"] == 2 and latest["elapsed_ms"] == 2500
    assert latest["occurred_at"] == "2026-10-29T12:00:00.000Z"
    first = store.events(before=2)["events"][0]
    assert first["request_id"] == str(request) and first["actor_id"] == str(actor)
    assert first["target_id"] == str(camera) and first["elapsed_ms"] == 0
    old_run = first["run_id"]
    store.close()
    reopened = OperatorAuditStore(audit_identity.directory, audit_identity.site_id)
    try:
        assert reopened.events()["events"] == [latest, first]
        assert reopened.record("auth.login", actor=actor) == 3
        assert reopened.events()["events"][0]["run_id"] != old_run
    finally:
        reopened.close()


def test_retention_is_append_bounded_and_retains_a_valid_chain(audit_identity, monkeypatch):
    import k5vision.operator_audit as module

    monkeypatch.setattr(module, "MAX_AUDIT_ROWS", 3)
    store = OperatorAuditStore(audit_identity.directory, audit_identity.site_id)
    for _ in range(8):
        store.record("export.prepare")
    result = store.events()
    assert [item["id"] for item in result["events"]] == [8, 7, 6]
    assert result["first_retained_id"] == 6 and result["last_id"] == 8
    assert result["total_rows"] == 3
    store.close()
    reopened = OperatorAuditStore(audit_identity.directory, audit_identity.site_id)
    try:
        assert reopened.events() == result
        assert reopened.record("audit.export") == 9
    finally:
        reopened.close()


def test_concurrent_writers_get_single_ordered_durable_chain(audit_identity, audit_store):
    def append(index):
        return audit_store.record("auth.login", outcome="denied", actor=uuid4())

    with ThreadPoolExecutor(max_workers=8) as executor:
        identifiers = list(executor.map(append, range(40)))
    assert sorted(identifiers) == list(range(1, 41))
    audit_store.close()
    reopened = OperatorAuditStore(audit_identity.directory, audit_identity.site_id)
    try:
        assert reopened.events()["total_rows"] == 40
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "https://private.example/password"},
        {"actor": "private-password"},
        {"target_id": "rtsp://camera:password@private"},
        {"request_id": "private-token"},
        {"outcome": "private-error-trace"},
        {"target_kind": "private/path"},
        {"status_code": True},
        {"status_code": 600},
        {"changes": ("private-password",)},
        {"changes": {"credentials": "private-password"}},
        {"changes": ("enabled", "enabled")},
        {"details": {"uri": "private-password"}},
        {"details": {"clip_start": "private-password"}},
        {"details": {"clip_duration_seconds": True}},
        {"details": {"bytes": 257 * 1024**2}},
        {"details": {"provider_type": "private-provider"}},
    ],
)
def test_private_unstructured_and_invalid_metadata_never_reaches_database(audit_store, arguments):
    arguments = {"action": "security.configure", **arguments}
    with pytest.raises(ValueError):
        audit_store.record(**arguments)
    assert audit_store.events()["total_rows"] == 0
    assert b"private" not in audit_store._path.read_bytes()


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_events SET action='camera.remove' WHERE id=1",
        "DELETE FROM audit_events WHERE id=1",
        "UPDATE audit_state SET site_id='another-site'",
        "UPDATE audit_state SET last_digest='" + "0" * 64 + "'",
    ],
)
def test_modified_or_truncated_state_is_refused_on_reopen(audit_identity, audit_store, statement):
    audit_store.record("camera.enroll")
    audit_store.close()
    connection = sqlite3.connect(audit_store._path)
    connection.execute(statement)
    connection.commit()
    connection.close()
    with pytest.raises(OperatorAuditError, match="Audit state admission failed"):
        OperatorAuditStore(audit_identity.directory, audit_identity.site_id)


def test_live_row_tampering_latches_writes_unavailable(audit_store):
    audit_store.record("camera.enroll")
    connection = sqlite3.connect(audit_store._path)
    connection.execute("UPDATE audit_events SET action='camera.remove'")
    connection.commit()
    connection.close()
    with pytest.raises(OperatorAuditError, match="Audit state unavailable"):
        audit_store.events()
    with pytest.raises(OperatorAuditError):
        audit_store.record("auth.login")


def test_lost_audit_database_cannot_silently_recreate_empty_history(audit_identity, audit_store):
    audit_store.record("camera.enroll")
    audit_store.close()
    audit_store._path.unlink()
    with pytest.raises(OperatorAuditError, match="Audit state admission failed"):
        OperatorAuditStore(audit_identity.directory, audit_identity.site_id)
    assert not audit_store._path.exists()


def test_wrong_site_and_linked_audit_database_are_rejected(audit_identity, audit_store, tmp_path):
    with pytest.raises(OperatorAuditError):
        OperatorAuditStore(audit_identity.directory, "foreign-site")
    audit_store.record("auth.login")
    audit_store.close()
    original = audit_store._path
    moved = tmp_path / "original-audit.sqlite3"
    original.rename(moved)
    try:
        original.symlink_to(moved)
    except OSError:
        os.link(moved, original)
    with pytest.raises(OperatorAuditError):
        OperatorAuditStore(audit_identity.directory, audit_identity.site_id)


def test_filters_use_explicit_utc_and_do_not_cross_actor_or_target(audit_store):
    first, second, camera = uuid4(), uuid4(), uuid4()
    audit_store.record("camera.enroll", actor=first, target_id=camera)
    audit_store.record("auth.login", actor=second, outcome="denied")
    selected = audit_store.events(
        actor_id=first, target_id=camera, action="camera.enroll", outcome="success"
    )
    assert [event["id"] for event in selected["events"]] == [1]
    assert audit_store.events(actor_id=uuid4())["events"] == []
    start = datetime.now(UTC) - timedelta(minutes=1)
    assert len(audit_store.events(start=start, end=start + timedelta(minutes=2))["events"]) == 2
    for options in (
        {"limit": 101},
        {"before": 0},
        {"action": "unknown"},
        {"start": start},
        {"limit": 10001, "export": True},
    ):
        with pytest.raises(ValueError):
            audit_store.events(**options)


def test_export_audit_preserves_only_the_selected_evidence_metadata(audit_store):
    job = uuid4()
    audit_store.record(
        "export.download",
        details={
            "export_id": job,
            "clip_start": datetime(2026, 10, 31, 12, tzinfo=UTC),
            "clip_duration_seconds": 120.5,
            "bytes": 123456,
        },
    )
    assert audit_store.events()["events"][0]["details"] == {
        "export_id": str(job),
        "clip_start": "2026-10-31T12:00:00.000Z",
        "clip_duration_seconds": 120.5,
        "bytes": 123456,
    }
