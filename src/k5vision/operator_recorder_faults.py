"""Bounded durable recorder alerts, independent of operator browser polling.

Only cached recorder observations enter this boundary. The existing audit file
owns persistence, identity and custody; its append chain and counters are untouched.
"""

from __future__ import annotations

import asyncio
import copy
import json
import math
import sqlite3
import threading
import time
from datetime import UTC, datetime
from uuid import UUID, uuid4

CODES = frozenset({"recorder_unavailable", "storage_blocked", "no_new_recording"})
RESOLUTIONS = frozenset({"worker_ready", "storage_ready", "recording_observed", "camera_removed"})
MAX_ACTIVE = 66
MAX_HISTORY = 128
MAX_BYTES = 128 * 1024
NO_WRITE_SECONDS = 45.0
WORKER_GRACE_SECONDS = 10.0
SAMPLE_SECONDS = 5.0


class RecorderFaultError(RuntimeError):
    """Fault persistence is unavailable; never include operating-system details."""


def _uuid(value):
    return type(value) is str and str(UUID(value)) == value


def _utc(value):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("Recorder alert timestamp unavailable")
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _empty(site_id):
    return {"schema_version": 1, "site_id": site_id, "sequence": 0, "active": [], "history": []}


def _key(event):
    return event["code"], event["camera_id"]


def _validate(value, site_id):
    if (
        type(value) is not dict
        or set(value) != {"schema_version", "site_id", "sequence", "active", "history"}
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["site_id"] != site_id
        or type(value["sequence"]) is not int
        or not 0 <= value["sequence"] < 2**63
        or type(value["active"]) is not list
        or type(value["history"]) is not list
        or len(value["active"]) > MAX_ACTIVE
        or len(value["history"]) > MAX_HISTORY
    ):
        raise ValueError
    for row in value["active"] + value["history"]:
        if (
            type(row) is not dict
            or set(row)
            != {
                "id",
                "code",
                "camera_id",
                "state",
                "resolution",
                "occurred_at",
                "run_id",
                "elapsed_ms",
            }
            or type(row["id"]) is not int
            or not 1 <= row["id"] <= value["sequence"]
            or row["code"] not in CODES
            or row["state"] not in {"active", "resolved"}
            or row["state"] == "active"
            and row["resolution"] is not None
            or row["state"] == "resolved"
            and row["resolution"] not in RESOLUTIONS
            or type(row["elapsed_ms"]) is not int
            or not 0 <= row["elapsed_ms"] < 2**63
            or not _uuid(row["run_id"])
            or row["code"] == "no_new_recording"
            and not _uuid(row["camera_id"])
            or row["code"] != "no_new_recording"
            and row["camera_id"] is not None
            or type(row["occurred_at"]) is not str
            or len(row["occurred_at"]) > 40
            or _utc(datetime.fromisoformat(row["occurred_at"])) != row["occurred_at"]
        ):
            raise ValueError
    if (
        any(row["state"] != "active" for row in value["active"])
        or len({_key(row) for row in value["active"]}) != len(value["active"])
        or [row["id"] for row in value["history"]]
        != sorted({row["id"] for row in value["history"]})
        or value["sequence"]
        and (not value["history"] or value["history"][-1]["id"] != value["sequence"])
        or not value["sequence"]
        and (value["active"] or value["history"])
    ):
        raise ValueError
    if len(json.dumps(value, separators=(",", ":")).encode()) > MAX_BYTES:
        raise ValueError
    return value


class RecorderFaultStore:
    """An additive optional metadata field in the existing private audit authority.

    The audit lock serializes both producers. These transactions never change
    audit event IDs, hashes or retention anchors. Fault failure does not latch
    ordinary audit unavailable or stop recording. Missing/corrupt initialized
    fault metadata is refused instead of replaced with an empty history.
    """

    def __init__(self, audit):
        self.audit, self.site_id = audit, audit.site_id

    def _run(self, operation):
        with self.audit._lock:
            connection = self.audit._connection
            try:
                if connection is None or self.audit._failed or connection.in_transaction:
                    raise ValueError
                self.audit._admit()
                result = operation(connection)
                self.audit._admit()
                return result
            except Exception:
                if connection is not None and connection.in_transaction:
                    try:
                        connection.rollback()
                    except sqlite3.Error:
                        pass
                raise RecorderFaultError("Recorder alert persistence unavailable") from None

    def load(self):
        def read(connection):
            columns = {
                row[1]: row[2] for row in connection.execute("PRAGMA table_info(audit_state)")
            }
            present = {
                name
                for name in ("recorder_fault_version", "recorder_fault_json")
                if name in columns
            }
            if not present:
                # Both additive fields and their first value commit together.
                self.audit._state(connection)
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "ALTER TABLE audit_state ADD COLUMN recorder_fault_version INTEGER"
                )
                connection.execute("ALTER TABLE audit_state ADD COLUMN recorder_fault_json TEXT")
                connection.execute(
                    "UPDATE audit_state SET recorder_fault_version=1,recorder_fault_json=? "
                    "WHERE site_id=?",
                    (json.dumps(_empty(self.site_id), separators=(",", ":")), self.site_id),
                )
                connection.execute("COMMIT")
            elif present != {"recorder_fault_version", "recorder_fault_json"} or (
                columns["recorder_fault_version"],
                columns["recorder_fault_json"],
            ) != ("INTEGER", "TEXT"):
                raise ValueError
            rows = connection.execute(
                "SELECT site_id,recorder_fault_version,recorder_fault_json FROM audit_state LIMIT 2"
            ).fetchall()
            if len(rows) != 1 or tuple(rows[0])[:2] != (self.site_id, 1):
                raise ValueError
            raw = rows[0][2]
            if type(raw) is not str or len(raw.encode()) > MAX_BYTES:
                raise ValueError
            return _validate(json.loads(raw), self.site_id)

        return self._run(read)

    def commit(self, value):
        value = _validate(copy.deepcopy(value), self.site_id)
        raw = json.dumps(value, separators=(",", ":"))

        def write(connection):
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT site_id,recorder_fault_version,recorder_fault_json FROM audit_state LIMIT 2"
            ).fetchall()
            if len(rows) != 1 or tuple(rows[0])[:2] != (self.site_id, 1):
                raise ValueError
            current = _validate(json.loads(rows[0][2]), self.site_id)
            if current["sequence"] > value["sequence"]:
                raise ValueError
            if current["sequence"] == value["sequence"] and current != value:
                raise ValueError
            connection.execute(
                "UPDATE audit_state SET recorder_fault_json=? "
                "WHERE site_id=? AND recorder_fault_version=1",
                (raw, self.site_id),
            )
            self.audit._admit()
            connection.execute("COMMIT")

        self._run(write)


class RecorderFaultMonitor:
    """Monotonic fault detection from cached observations; no discovery/media I/O."""

    def __init__(self, service, audit, *, monotonic=None, utc_now=None):
        self.service = service
        self.store = RecorderFaultStore(audit)
        self._now = monotonic or time.monotonic
        self._utc_now = utc_now or (lambda: datetime.now(UTC))
        self.run_id = str(uuid4())
        self._began = self._now()
        self._state = _empty(audit.site_id)
        self._loaded = False
        self._durable_sequence = -1
        self._persistence_available = False
        self._last_sample = None
        self._generation = None
        self._expected = {}
        self._worker_bad_since = None
        self._lock = threading.RLock()
        self._task = None
        self._write_task = None
        self._closing = False

    def _update(self, observations):
        now = self._now()
        if type(now) not in (int, float) or not math.isfinite(now) or now < self._began:
            raise ValueError
        if type(observations) is not dict or len(observations["cameras"]) > 64:
            raise ValueError
        if observations["generation"] != self._generation:
            self._expected.clear()
            self._generation = observations["generation"]
        active = {_key(row): row for row in self._state["active"]}
        changes = {}
        cameras = observations["cameras"]
        identifiers = {row["camera_id"] for row in cameras}
        if len(identifiers) != len(cameras) or any(not _uuid(value) for value in identifiers):
            raise ValueError
        monitoring_needed = any(row["recording_enabled"] for row in cameras)
        worker_ready = observations["worker_ready"]
        if type(worker_ready) is not bool or type(observations["storage_allowed"]) not in {
            bool,
            type(None),
        }:
            raise ValueError
        if monitoring_needed and not worker_ready:
            self._worker_bad_since = (
                now if self._worker_bad_since is None else self._worker_bad_since
            )
            if now - self._worker_bad_since >= WORKER_GRACE_SECONDS:
                changes[("recorder_unavailable", None)] = (True, None)
        else:
            self._worker_bad_since = None
            if worker_ready:
                changes[("recorder_unavailable", None)] = (False, "worker_ready")
        storage = observations["storage_allowed"]
        if storage is not None:
            changes[("storage_blocked", None)] = (not storage, "storage_ready" if storage else None)
        for key in active:
            if key[1] is not None and key[1] not in identifiers:
                changes[key] = (False, "camera_removed")
                self._expected.pop(key[1], None)
        for camera in cameras:
            identifier = camera["camera_id"]
            key = "no_new_recording", identifier
            expected = camera["recording_enabled"] and camera["recording_expected"]
            if not expected:
                self._expected.pop(identifier, None)
                # Paused/motion-armed never raises a new fault or invents recovery.
                continue
            began = self._expected.setdefault(identifier, now)
            progress = camera["progress"]
            age = progress["last_progress_age_seconds"]
            size = progress["observed_bytes"]
            if type(size) is not int or not 0 <= size < 2**63:
                raise ValueError
            if age is not None and (
                type(age) not in (int, float) or not math.isfinite(age) or age < 0
            ):
                raise ValueError
            fresh = (
                progress["inspection_complete"] is True
                and progress.get("observed") is True
                and size > 0
                and age is not None
                and now - age >= began
            )
            if fresh and age <= NO_WRITE_SECONDS:
                changes[key] = (False, "recording_observed")
            elif storage is True and worker_ready and now - began >= NO_WRITE_SECONDS:
                changes[key] = (True, None)
        for key, (failed, resolution) in changes.items():
            if failed == (key in active):
                continue
            self._state["sequence"] += 1
            event = {
                "id": self._state["sequence"],
                "code": key[0],
                "camera_id": key[1],
                "state": "active" if failed else "resolved",
                "resolution": resolution,
                "occurred_at": _utc(self._utc_now()),
                "run_id": self.run_id,
                "elapsed_ms": int((now - self._began) * 1000),
            }
            if failed:
                active[key] = event
            else:
                del active[key]
            self._state["history"].append(event)
        self._state["active"] = list(active.values())
        self._state["history"] = self._state["history"][-MAX_HISTORY:]
        _validate(self._state, self.store.site_id)
        self._last_sample = now

    async def sample(self):
        # File admission/SQLite work stays off the recorder's event loop.
        if not self._loaded:
            try:
                value = await self._persist(self.store.load)
            except RecorderFaultError:
                with self._lock:
                    self._persistence_available = False
                return
            with self._lock:
                self._state = value
                self._durable_sequence = value["sequence"]
                self._loaded = True
        observations = self.service.cached_recording_observations()
        with self._lock:
            self._update(observations)
            value = copy.deepcopy(self._state)
        try:
            if value["sequence"] != self._durable_sequence:
                await self._persist(self.store.commit, value)
                self._durable_sequence = value["sequence"]
            with self._lock:
                self._persistence_available = True
        except RecorderFaultError:
            with self._lock:
                self._persistence_available = False

    async def _persist(self, operation, *args):
        task = asyncio.create_task(asyncio.to_thread(operation, *args))
        self._write_task = task
        cancelled = False
        try:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancelled = True
                except Exception:
                    break
            result = task.result()
            if cancelled:
                raise asyncio.CancelledError
            return result
        finally:
            self._write_task = None

    async def _run(self):
        while not self._closing:
            try:
                await self.sample()
            except asyncio.CancelledError:
                raise
            except Exception:
                with self._lock:
                    self._last_sample = None
                    self._persistence_available = False
            await asyncio.sleep(SAMPLE_SECONDS)

    def start(self):
        if self._task is not None or self._closing:
            raise RecorderFaultError("Recorder alert monitor unavailable")
        self._task = asyncio.create_task(self._run(), name="operator-recorder-alerts")

    def snapshot(self):
        with self._lock:
            now = self._now()
            fresh = (
                self._last_sample is not None and 0 <= now - self._last_sample <= 3 * SAMPLE_SECONDS
            )
            return {
                "available": self._loaded
                and self._persistence_available
                and self._state["sequence"] == self._durable_sequence
                and not self.store.audit._failed
                and self.store.audit._connection is not None
                and fresh,
                "monitoring": bool(fresh),
                "active": copy.deepcopy(self._state["active"]),
                "history": copy.deepcopy(self._state["history"]),
            }

    async def aclose(self):
        self._closing = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        with self._lock:
            self._last_sample = None
