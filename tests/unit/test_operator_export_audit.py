from __future__ import annotations

import asyncio
import sqlite3
import threading
from uuid import uuid4

import pytest
import test_operator_audit as audit_helpers
import test_operator_exports as export_helpers

from k5vision.media.mediamtx_provider import MediaMTXBinding
from k5vision.operator_exports import ExportError

audit_identity = audit_helpers.audit_identity
audit_store = audit_helpers.audit_store
private_root = export_helpers.private_root


def _manager(root, store):
    service = export_helpers._service()
    service.site_id = store.site_id
    return export_helpers._manager(root, service, audit=store)


def test_actual_completed_export_and_transfer_audit_preserve_selected_evidence(
    private_root, audit_store
):
    manager = _manager(private_root, audit_store)
    owner, camera = uuid4(), uuid4()

    async def run():
        job = await manager.create(
            owner=owner,
            binding=MediaMTXBinding(manager.service.site_id, camera),
            start=export_helpers._START,
            duration=120,
        )
        await export_helpers._finish(manager)
        assert job.status == "ready"
        manager.audit_download(job, outcome="success")
        await manager.aclose()
        return job

    job = asyncio.run(run())
    entries = audit_store.events()["events"]
    assert [item["action"] for item in entries] == ["export.download", "export.completed"]
    for item in entries:
        assert item["actor_id"] == str(owner) and item["target_id"] == str(camera)
        assert item["outcome"] == "success"
        assert item["details"] == {
            "export_id": str(job.id),
            "clip_start": "2026-10-28T12:30:00.000Z",
            "clip_duration_seconds": 120.0,
            "bytes": len(export_helpers._CONTENT),
        }


def test_native_preparation_failure_is_audited_without_exception_values(private_root, audit_store):
    manager = _manager(private_root, audit_store)

    def fail(_path, _duration):
        raise RuntimeError("private-url private-password")

    manager._validator = fail

    async def run():
        job = await manager.create(
            owner=uuid4(),
            binding=MediaMTXBinding(manager.service.site_id, uuid4()),
            start=export_helpers._START,
            duration=120,
        )
        await export_helpers._finish(manager)
        await manager.aclose()
        assert job.status == "failed" and list(private_root.iterdir()) == []

    asyncio.run(run())
    event = audit_store.events()["events"][0]
    assert event["action"] == "export.failed" and event["outcome"] == "failure"
    assert b"private" not in audit_store._path.read_bytes()


def test_cancellation_joins_native_work_and_audits_interruption(private_root, audit_store):
    manager = _manager(private_root, audit_store)
    entered, exited = threading.Event(), threading.Event()

    def blocked(_service, _binding, _start, _duration, path, progress, stopped):
        export_helpers._download(_service, _binding, _start, _duration, path, progress, stopped)
        entered.set()
        assert stopped.wait(5)
        exited.set()

    manager._downloader = blocked

    async def run():
        job = await manager.create(
            owner=uuid4(),
            binding=MediaMTXBinding(manager.service.site_id, uuid4()),
            start=export_helpers._START,
            duration=120,
        )
        assert await asyncio.to_thread(entered.wait, 5)
        task = next(iter(manager._tasks))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await manager.aclose()
        assert exited.is_set() and job.status == "failed"
        assert list(private_root.iterdir()) == []

    asyncio.run(run())
    entries = audit_store.events()["events"]
    assert len(entries) == 1
    assert entries[0]["action"] == "export.failed" and entries[0]["outcome"] == "interrupted"


def test_audit_commit_failure_never_offers_an_unaudited_ready_export(private_root, audit_store):
    manager = _manager(private_root, audit_store)
    connection = sqlite3.connect(audit_store._path)
    connection.execute(
        "CREATE TRIGGER refuse_export BEFORE INSERT ON audit_events "
        "BEGIN SELECT RAISE(ABORT,'test-storage-failure'); END"
    )
    connection.close()

    async def run():
        binding = MediaMTXBinding(manager.service.site_id, uuid4())
        job = await manager.create(
            owner=uuid4(), binding=binding, start=export_helpers._START, duration=120
        )
        await export_helpers._finish(manager)
        assert job.status == "failed" and job.public()["download_url"] is None
        assert job.error == "export_unavailable" and list(private_root.iterdir()) == []
        with pytest.raises(ExportError, match="export_unavailable"):
            await manager.create(
                owner=uuid4(), binding=binding, start=export_helpers._START, duration=120
            )
        await manager.aclose()

    asyncio.run(run())
    assert audit_store.events()["total_rows"] == 0


def test_foreign_site_or_replacement_sink_is_refused(private_root, audit_store):
    manager = export_helpers._manager(private_root)
    with pytest.raises(ValueError, match="Export audit was refused"):
        manager.set_audit(audit_store)
    manager.service.site_id = audit_store.site_id
    manager.set_audit(audit_store)
    manager.set_audit(audit_store)
    with pytest.raises(ValueError, match="Export audit was refused"):
        manager.set_audit(object())
