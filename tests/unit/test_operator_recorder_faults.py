"""Persistent fault state is independent of browser polling and UTC clock changes."""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_operator_audit import audit_identity as audit_identity_fixture

from k5vision.operator_audit import OperatorAuditStore
from k5vision.operator_recorder_faults import (
    RecorderFaultError,
    RecorderFaultMonitor,
    RecorderFaultStore,
)

audit_identity = audit_identity_fixture


def _observation(identifier=None):
    return {
        "generation": 1,
        "worker_ready": True,
        "storage_allowed": True,
        "cameras": [
            {
                "camera_id": str(identifier or uuid4()),
                "recording_enabled": True,
                "recording_expected": True,
                "progress": {
                    "observed_bytes": 100,
                    "observed": True,
                    "inspection_complete": True,
                    "last_progress_age_seconds": None,
                },
            }
        ],
    }


@pytest.fixture
def scenario(audit_identity):
    audit = OperatorAuditStore(audit_identity.directory, audit_identity.site_id)
    clock = SimpleNamespace(now=1000.0, utc=datetime(2026, 11, 22, tzinfo=UTC))
    observed = _observation()
    service = SimpleNamespace(cached_recording_observations=lambda: copy.deepcopy(observed))
    monitor = RecorderFaultMonitor(
        service, audit, monotonic=lambda: clock.now, utc_now=lambda: clock.utc
    )
    yield SimpleNamespace(
        audit=audit, clock=clock, observed=observed, service=service, monitor=monitor
    )
    audit.close()


def test_no_write_fault_is_background_durable_and_recovers_only_from_new_nonempty_growth(scenario):
    async def run():
        s = scenario
        await s.monitor.sample()
        s.clock.now += 44.999
        await s.monitor.sample()
        assert s.monitor.snapshot()["active"] == []
        s.clock.now += 0.001
        await s.monitor.sample()
        first = s.monitor.snapshot()
        assert first["available"] and len(first["active"]) == 1
        assert first["active"][0]["code"] == "no_new_recording"
        for _ in range(10):
            await s.monitor.sample()
        assert s.monitor.snapshot()["history"] == first["history"]
        progress = s.observed["cameras"][0]["progress"]
        progress.update(last_progress_age_seconds=0, observed_bytes=0)
        await s.monitor.sample()
        assert s.monitor.snapshot()["active"] == first["active"]
        progress["observed_bytes"] = 200
        await s.monitor.sample()
        final = s.monitor.snapshot()
        assert final["active"] == [] and final["history"][-1]["resolution"] == "recording_observed"
        assert RecorderFaultStore(s.audit).load()["history"] == final["history"]

    asyncio.run(run())


def test_persisted_fault_survives_restart_and_needs_fresh_post_restart_growth(scenario):
    async def run():
        s = scenario
        await s.monitor.sample()
        s.clock.now += 50
        await s.monitor.sample()
        original = s.monitor.snapshot()["active"]
        restarted = RecorderFaultMonitor(
            s.service, s.audit, monotonic=lambda: s.clock.now, utc_now=lambda: s.clock.utc
        )
        s.observed["cameras"][0]["progress"]["last_progress_age_seconds"] = 1
        await restarted.sample()
        assert restarted.snapshot()["active"] == original
        s.clock.now += 1
        s.observed["cameras"][0]["progress"]["last_progress_age_seconds"] = 0
        await restarted.sample()
        assert restarted.snapshot()["active"] == []
        assert restarted.snapshot()["history"][-1]["run_id"] != original[0]["run_id"]

    asyncio.run(run())


def test_pause_and_motion_armed_do_not_raise_or_falsely_clear_existing_fault(scenario):
    async def run():
        s = scenario
        camera = s.observed["cameras"][0]
        camera["recording_expected"] = False
        await s.monitor.sample()
        s.clock.now += 3600
        await s.monitor.sample()
        assert s.monitor.snapshot()["active"] == []
        camera["recording_expected"] = True
        await s.monitor.sample()
        s.clock.now += 45
        await s.monitor.sample()
        active = s.monitor.snapshot()["active"]
        assert len(active) == 1
        camera["recording_enabled"] = False
        s.clock.now += 3600
        await s.monitor.sample()
        assert s.monitor.snapshot()["active"] == active
        camera["recording_enabled"] = True
        camera["recording_expected"] = False
        await s.monitor.sample()
        assert s.monitor.snapshot()["active"] == active

    asyncio.run(run())


def test_storage_and_worker_faults_clear_from_fresh_health_without_duplicate_no_write(scenario):
    async def run():
        s = scenario
        s.observed.update(storage_allowed=False, worker_ready=False)
        await s.monitor.sample()
        assert [r["code"] for r in s.monitor.snapshot()["active"]] == ["storage_blocked"]
        s.clock.now += 50
        await s.monitor.sample()
        assert {r["code"] for r in s.monitor.snapshot()["active"]} == {
            "storage_blocked",
            "recorder_unavailable",
        }
        s.observed.update(storage_allowed=True, worker_ready=True)
        s.observed["cameras"][0]["progress"]["last_progress_age_seconds"] = 0
        await s.monitor.sample()
        assert s.monitor.snapshot()["active"] == []
        assert {
            r["resolution"] for r in s.monitor.snapshot()["history"] if r["state"] == "resolved"
        } == {"storage_ready", "worker_ready"}

    asyncio.run(run())


def test_utc_rollback_does_not_reorder_faults_or_advance_no_write_threshold(scenario):
    async def run():
        s = scenario
        await s.monitor.sample()
        s.clock.utc += timedelta(days=2)
        await s.monitor.sample()
        assert not s.monitor.snapshot()["active"]
        s.clock.now += 45
        await s.monitor.sample()
        first = s.monitor.snapshot()["active"][0]
        s.clock.utc -= timedelta(days=4)
        s.clock.now += 1
        s.observed["cameras"][0]["progress"]["last_progress_age_seconds"] = 0
        await s.monitor.sample()
        latest = s.monitor.snapshot()["history"][-1]
        assert latest["id"] > first["id"] and latest["occurred_at"] < first["occurred_at"]
        assert latest["elapsed_ms"] == 46000

    asyncio.run(run())


def test_audit_chain_counters_survive_additive_fault_metadata_and_concurrent_writers(scenario):
    async def fault():
        await scenario.monitor.sample()
        scenario.observed["storage_allowed"] = False
        await scenario.monitor.sample()

    s = scenario
    assert s.audit.record("system.change") == 1
    with ThreadPoolExecutor(max_workers=2) as pool:
        background = pool.submit(lambda: asyncio.run(fault()))
        audit = pool.submit(lambda: [s.audit.record("auth.login") for _ in range(20)])
        background.result()
        assert audit.result() == list(range(2, 22))
    original = s.audit.events()
    assert original["last_id"] == 21 and len(original["events"]) == 21
    root, site = s.audit._root, s.audit.site_id
    s.audit.close()
    reopened = OperatorAuditStore(root, site)
    try:
        assert reopened.events() == original
        assert RecorderFaultStore(reopened).load()["active"][0]["code"] == "storage_blocked"
        assert reopened.record("system.change") == 22
    finally:
        reopened.close()


def test_additive_schema_rolls_back_both_columns_on_interrupted_initialization(scenario):
    s = scenario
    calls = []

    def authorize(action, a, b, *_):
        if action == sqlite3.SQLITE_ALTER_TABLE:
            calls.append(a)
            if len(calls) == 2:
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    s.audit._connection.set_authorizer(authorize)
    try:
        with pytest.raises(RecorderFaultError, match="^Recorder alert persistence unavailable$"):
            RecorderFaultStore(s.audit).load()
    finally:
        s.audit._connection.set_authorizer(None)
    columns = {r[1] for r in s.audit._connection.execute("PRAGMA table_info(audit_state)")}
    assert not {"recorder_fault_version", "recorder_fault_json"} & columns
    assert not s.audit._connection.in_transaction and not s.audit._failed
    assert s.audit.record("system.change") == 1
    assert RecorderFaultStore(s.audit).load()["sequence"] == 0


def test_initialized_fault_corruption_is_not_reset_or_reflected_and_audit_stays_independent(
    scenario,
):
    s = scenario
    store = RecorderFaultStore(s.audit)
    store.load()
    s.audit._connection.execute(
        "UPDATE audit_state SET recorder_fault_json=?", ('{"private":"rtsp://secret"}',)
    )
    with pytest.raises(RecorderFaultError) as error:
        store.load()
    assert "rtsp" not in str(error.value)
    assert (
        s.audit._connection.execute("SELECT recorder_fault_json FROM audit_state").fetchone()[0]
        == '{"private":"rtsp://secret"}'
    )
    assert s.audit.record("system.change") == 1 and not s.audit._failed


def test_failed_fault_commit_keeps_visible_fault_then_flushes_after_persistence_recovers(scenario):
    async def run():
        s = scenario
        await s.monitor.sample()

        def deny(action, a, b, *_):
            return (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_UPDATE and a == "audit_state"
                else sqlite3.SQLITE_OK
            )

        s.audit._connection.set_authorizer(deny)
        try:
            s.observed["storage_allowed"] = False
            await s.monitor.sample()
            failed = s.monitor.snapshot()
            assert not failed["available"] and failed["active"][0]["code"] == "storage_blocked"
            assert not s.audit._failed and not s.audit._connection.in_transaction
        finally:
            s.audit._connection.set_authorizer(None)
        await s.monitor.sample()
        assert s.monitor.snapshot()["available"]
        assert RecorderFaultStore(s.audit).load()["active"] == failed["active"]

    asyncio.run(run())


@pytest.mark.parametrize("transition", ["fault", "recovery"])
def test_pending_fault_or_recovery_is_unavailable_until_actual_metadata_commit(
    scenario, monkeypatch, transition
):
    entered, release = threading.Event(), threading.Event()

    async def run():
        s = scenario
        await s.monitor.sample()
        if transition == "recovery":
            s.observed["storage_allowed"] = False
            await s.monitor.sample()
        assert s.monitor.snapshot()["available"] is True
        before = await asyncio.to_thread(RecorderFaultStore(s.audit).load)
        original = s.monitor.store.commit

        def held(value):
            entered.set()
            assert release.wait(3), "Pending recorder metadata commit was not released"
            return original(value)

        monkeypatch.setattr(s.monitor.store, "commit", held)
        s.observed["storage_allowed"] = transition == "recovery"
        writing = asyncio.create_task(s.monitor.sample())
        try:
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(0.005)
            assert entered.is_set() and not writing.done()
            pending = s.monitor.snapshot()
            # Read the real database while the actual metadata write is held.
            # The new in-memory observation must not masquerade as saved state.
            stored = await asyncio.to_thread(RecorderFaultStore(s.audit).load)
            assert stored == before
            assert s.monitor._state["sequence"] == stored["sequence"] + 1
            assert pending["monitoring"] is True
            assert bool(pending["active"]) == (transition == "fault")
            assert pending["available"] is False
        finally:
            release.set()
            await writing
        confirmed = s.monitor.snapshot()
        stored = await asyncio.to_thread(RecorderFaultStore(s.audit).load)
        assert confirmed["available"] is True
        assert stored["sequence"] == before["sequence"] + 1
        assert stored["active"] == confirmed["active"]
        assert stored["history"] == confirmed["history"]

    try:
        asyncio.run(run())
    finally:
        release.set()


def test_monitor_close_joins_a_pending_fault_write_before_audit_can_close(scenario, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = scenario.monitor.store.commit

    def held(value):
        entered.set()
        assert release.wait(3)
        assert scenario.audit._connection is not None
        return original(value)

    monkeypatch.setattr(scenario.monitor.store, "commit", held)
    scenario.observed["storage_allowed"] = False

    async def run():
        scenario.monitor.start()
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.005)
        assert entered.is_set()
        closing = asyncio.create_task(scenario.monitor.aclose())
        await asyncio.sleep(0.02)
        assert not closing.done()
        release.set()
        await closing
        assert scenario.monitor._write_task is None and scenario.monitor._task.done()
        assert RecorderFaultStore(scenario.audit).load()["active"][0]["code"] == "storage_blocked"

    try:
        asyncio.run(run())
    finally:
        release.set()


def test_bounded_history_never_discards_an_older_still_active_fault(scenario):
    async def run():
        s = scenario
        await s.monitor.sample()
        s.clock.now += 45
        await s.monitor.sample()
        first = s.monitor.snapshot()["active"][0]
        for index in range(132):
            s.observed["storage_allowed"] = bool(index % 2)
            await s.monitor.sample()
        saved = RecorderFaultStore(s.audit).load()
        assert len(saved["history"]) == 128
        assert saved["history"][0]["id"] > first["id"]
        assert first in saved["active"] and first in s.monitor.snapshot()["active"]
        assert saved["sequence"] == 133

    asyncio.run(run())


def test_missing_fresh_observation_never_clears_saved_fault_or_reports_monitoring_healthy(scenario):
    async def run():
        s = scenario
        s.observed["storage_allowed"] = False
        await s.monitor.sample()
        original = s.monitor.snapshot()["active"]
        s.clock.now += 16
        stale = s.monitor.snapshot()
        assert not stale["available"] and not stale["monitoring"]
        assert stale["active"] == original
        s.observed["storage_allowed"] = None
        restarted = RecorderFaultMonitor(s.service, s.audit, monotonic=lambda: s.clock.now)
        await restarted.sample()
        assert restarted.snapshot()["active"] == original
        s.observed["storage_allowed"] = True
        await restarted.sample()
        assert restarted.snapshot()["active"] == []

    asyncio.run(run())


def test_other_site_fault_metadata_is_refused_without_reset_or_audit_counter_change(scenario):
    s = scenario
    store = RecorderFaultStore(s.audit)
    value = store.load()
    value["site_id"] = str(uuid4())
    encoded = json.dumps(value)
    s.audit._connection.execute("UPDATE audit_state SET recorder_fault_json=?", (encoded,))
    with pytest.raises(RecorderFaultError, match="^Recorder alert persistence unavailable$"):
        store.load()
    assert (
        s.audit._connection.execute("SELECT recorder_fault_json FROM audit_state").fetchone()[0]
        == encoded
    )
    assert s.audit.record("system.change") == 1


def test_cached_audit_failure_cannot_leave_fault_persistence_healthy_without_a_new_transition(
    scenario,
):
    async def run():
        s = scenario
        await s.monitor.sample()
        assert s.monitor.snapshot()["available"] is True
        s.audit._failed = True
        await s.monitor.sample()  # No transition means no new metadata write.
        assert s.monitor.snapshot()["monitoring"] is True
        assert s.monitor.snapshot()["available"] is False

    asyncio.run(run())
