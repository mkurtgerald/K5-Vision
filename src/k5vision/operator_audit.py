"""Private, bounded operational audit with durable append order.

Audit timestamps are observed host UTC. Append IDs, request IDs and per-run
elapsed time preserve order when UTC changes. Bodies, credentials, network
addresses and arbitrary provider messages never cross this boundary.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import threading
import time
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from k5vision import identity_state
from k5vision.operator_component_state import ComponentStateAnchor

MAX_AUDIT_ROWS = 100_000
MAX_DATABASE_BYTES = 128 * 1024**2
MAX_EXPORT_ROWS = 10_000
MAX_EXPORT_BYTES = 8 * 1024**2
_DATABASE = "operator-audit.sqlite3"
_ZERO = "0" * 64

ACTIONS = frozenset(
    {
        "auth.login",
        "auth.logout",
        "auth.activate",
        "auth.mfa",
        "auth.sso",
        "camera.enroll",
        "camera.remove",
        "camera.reconnect",
        "camera.discover",
        "camera.probe",
        "camera.video.read",
        "camera.video.configure",
        "recording.configure",
        "export.prepare",
        "export.completed",
        "export.download",
        "export.failed",
        "analytics.configure",
        "storage.configure",
        "time.configure",
        "time.resync",
        "time.camera.configure",
        "time.camera.sync",
        "integration.configure",
        "integration.test",
        "integration.connect",
        "integration.disconnect",
        "integration.create",
        "integration.update",
        "integration.delete",
        "integration.handshake",
        "integration.response",
        "integration.event",
        "security.configure",
        "security.access",
        "security.mfa.configure",
        "security.sso.configure",
        "security.mfa_enabled",
        "security.mfa_disabled",
        "security.mfa_recovery_replaced",
        "security.mfa_authenticated",
        "security.sso_configured",
        "security.sso_linked",
        "security.sso_unlinked",
        "security.sso_authenticated",
        "user.create",
        "user.configure",
        "user.camera_access",
        "user.reset_password",
        "role.configure",
        "role.remove",
        "audit.export",
        "system.change",
    }
)
OUTCOMES = frozenset({"started", "success", "denied", "failure", "interrupted"})
TARGET_KINDS = frozenset(
    {
        "camera",
        "user",
        "export",
        "storage",
        "time",
        "integration",
        "security",
        "role",
        "session",
        "system",
        "site",
    }
)
CHANGE_FIELDS = frozenset(
    {
        "display_name",
        "role",
        "enabled",
        "camera_access",
        "password",
        "mfa",
        "sso",
        "permissions",
        "name",
        "quota_bytes",
        "retention_seconds",
        "minimum_free_bytes",
        "recording_directory",
        "pending_recording_directory",
        "confidence_threshold",
        "ntp_sources",
        "ntp_enabled",
        "time_zone",
        "utc",
        "provider",
        "endpoint",
        "credentials",
        "capabilities",
        "configuration",
        "recording_enabled",
    }
)
_request_context: ContextVar[dict | None] = ContextVar("operator_audit_request", default=None)


class OperatorAuditError(RuntimeError):
    """Finite failure without private filesystem, SQL or supplied values."""


def _identifier(value, *, optional=True):
    if value is None and optional:
        return None
    if not isinstance(value, (UUID, str)):
        value = getattr(value, "id", None)
    if not isinstance(value, (UUID, str)):
        raise ValueError("Audit identifier was refused")
    try:
        return str(UUID(str(value)))
    except (ValueError, AttributeError):
        raise ValueError("Audit identifier was refused") from None


def set_audit_actor(account) -> None:
    """Attribute the current request after authenticating its present account."""
    context = _request_context.get()
    if context is not None:
        context["actor_id"] = _identifier(account, optional=False)


def _utc(value):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("Audit UTC was refused")
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _digest(previous, event):
    body = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256((previous + "\n" + body).encode("ascii")).hexdigest()


def _event(row):
    result = dict(row)
    result.pop("previous_digest", None)
    result.pop("digest", None)
    changes, details = result.pop("changes_json"), result.pop("details_json")
    if (
        type(changes) is not str
        or len(changes) > 1024
        or type(details) is not str
        or len(details) > 2048
    ):
        raise ValueError
    result["changes"] = json.loads(changes)
    result["details"] = json.loads(details)
    return result


def _details(value):
    if value is None:
        return {}
    if type(value) is not dict or set(value) - {
        "export_id",
        "clip_start",
        "clip_duration_seconds",
        "bytes",
        "provider_type",
        "response_count",
    }:
        raise ValueError("Audit details were refused")
    result = dict(value)
    if "export_id" in value:
        result["export_id"] = _identifier(value["export_id"], optional=False)
    if "clip_start" in value:
        start = value["clip_start"]
        if isinstance(start, str):
            if len(start) > 40:
                raise ValueError("Audit details were refused")
            start = datetime.fromisoformat(start)
        result["clip_start"] = _utc(start)
    if "clip_duration_seconds" in value:
        duration = value["clip_duration_seconds"]
        if (
            type(duration) not in (int, float)
            or not math.isfinite(duration)
            or not 1 <= duration <= 3600
        ):
            raise ValueError("Audit details were refused")
    if "bytes" in value and (
        type(value["bytes"]) is not int or not 0 <= value["bytes"] <= 256 * 1024**2
    ):
        raise ValueError("Audit details were refused")
    if "provider_type" in value and (
        type(value["provider_type"]) is not str
        or value["provider_type"] not in {"neural", "analytics"}
    ):
        raise ValueError("Audit details were refused")
    if "response_count" in value and (
        type(value["response_count"]) is not int or not 0 <= value["response_count"] <= 128
    ):
        raise ValueError("Audit details were refused")
    return result


def _validate_event(event):
    if (
        type(event.get("id")) is not int
        or not 1 <= event["id"] < 2**63
        or event.get("action") not in ACTIONS
        or event.get("outcome") not in OUTCOMES
        or event.get("target_kind") not in TARGET_KINDS | {None}
        or type(event.get("elapsed_ms")) is not int
        or not 0 <= event["elapsed_ms"] < 2**63
        or event.get("status_code") is not None
        and (type(event["status_code"]) is not int or not 100 <= event["status_code"] <= 599)
        or not isinstance(event.get("changes"), list)
        or len(event["changes"]) > len(CHANGE_FIELDS)
        or any(type(v) is not str or v not in CHANGE_FIELDS for v in event["changes"])
        or len(set(event["changes"])) != len(event["changes"])
    ):
        raise ValueError
    for key in ("actor_id", "target_id"):
        if _identifier(event.get(key)) != event.get(key):
            raise ValueError
    for key in ("run_id", "request_id"):
        if _identifier(event.get(key), optional=False) != event.get(key):
            raise ValueError
    if _utc(datetime.fromisoformat(event["occurred_at"])) != event["occurred_at"]:
        raise ValueError
    if _details(event.get("details")) != event.get("details"):
        raise ValueError


class OperatorAuditStore:
    """Site-bound audit metadata; retention follows append identity, never UTC age.

    A chained content digest detects altered/truncated rows and broken retention
    anchors. This is integrity checking, not protection from a Windows owner who
    can rewrite both the database and application.
    """

    def __init__(self, root: Path, site_id: str, *, utc_now=None, monotonic=None):
        self._lock = threading.RLock()
        self._connection = None
        self._failed = False
        self._utc_now = utc_now or (lambda: datetime.now(UTC))
        self._monotonic = monotonic or time.monotonic
        self._began = self._monotonic()
        self.run_id = str(uuid4())
        self.site_id = site_id
        try:
            if type(self._began) not in (int, float) or not math.isfinite(self._began):
                raise ValueError
            state = identity_state.load_identity_state(root)
            if state.site_id != site_id:
                raise ValueError
            self._root = state.directory
            self._root_identity = self._root.lstat()
            self._path = self._root / _DATABASE
            anchor = ComponentStateAnchor(
                self._root,
                site_id,
                "audit",
                present=self._path.exists() or self._path.is_symlink(),
            )
            created = False
            try:
                fd = os.open(
                    self._path,
                    os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
            except FileExistsError:
                pass
            else:
                os.close(fd)
                created = True
            self._identity = self._private_file(self._path)
            self._admit()
            connection = sqlite3.connect(
                self._path.as_uri() + "?mode=rw",
                uri=True,
                isolation_level=None,
                check_same_thread=False,
                timeout=3,
            )
            self._connection = connection
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA synchronous=FULL")
            if connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                raise ValueError
            page_size = connection.execute("PRAGMA page_size").fetchone()[0]
            connection.execute("PRAGMA max_page_count=" + str(MAX_DATABASE_BYTES // page_size))
            if created:
                connection.executescript(
                    "BEGIN IMMEDIATE;"
                    "CREATE TABLE audit_state(schema_version INTEGER,site_id TEXT,"
                    "anchor_id INTEGER,anchor_digest TEXT,last_id INTEGER,last_digest TEXT);"
                    "CREATE TABLE audit_events(id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "occurred_at TEXT NOT NULL,run_id TEXT NOT NULL,elapsed_ms INTEGER NOT NULL,"
                    "request_id TEXT NOT NULL,actor_id TEXT,action TEXT NOT NULL,"
                    "outcome TEXT NOT NULL,target_kind TEXT,target_id TEXT,status_code INTEGER,"
                    "changes_json TEXT NOT NULL,details_json TEXT NOT NULL,"
                    "previous_digest TEXT NOT NULL,"
                    "digest TEXT NOT NULL);"
                    "CREATE INDEX audit_action_id ON audit_events(action,id);"
                    "CREATE INDEX audit_actor_id ON audit_events(actor_id,id);"
                    "CREATE INDEX audit_target_id ON audit_events(target_id,id);"
                )
                connection.execute(
                    "INSERT INTO audit_state VALUES(1,?,0,?,0,?)", (site_id, _ZERO, _ZERO)
                )
                connection.execute("COMMIT")
            self._verify_chain(connection)
            self._admit()
            anchor.seal()
        except Exception:
            self.close()
            raise OperatorAuditError("Audit state admission failed") from None

    @staticmethod
    def _private_file(path):
        info = identity_state._regular_file(path)
        if os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise ValueError
        if info.st_size > MAX_DATABASE_BYTES:
            raise ValueError
        return info

    def _admit(self):
        identity_state._same_directory(self._root, self._root_identity)
        current = self._private_file(self._path)
        if (current.st_dev, current.st_ino) != (self._identity.st_dev, self._identity.st_ino):
            raise ValueError
        for suffix in ("-journal", "-wal", "-shm"):
            path = Path(str(self._path) + suffix)
            if path.exists() or path.is_symlink():
                self._private_file(path)

    def _state(self, connection):
        rows = connection.execute("SELECT * FROM audit_state LIMIT 2").fetchall()
        if len(rows) != 1:
            raise ValueError
        state = rows[0]
        if (
            state["schema_version"] != 1
            or state["site_id"] != self.site_id
            or type(state["anchor_id"]) is not int
            or type(state["last_id"]) is not int
            or not 0 <= state["anchor_id"] <= state["last_id"] < 2**63
            or state["last_id"] - state["anchor_id"] > MAX_AUDIT_ROWS
            or state["last_id"] > 0
            and state["last_id"] == state["anchor_id"]
            or state["anchor_id"] == 0
            and state["anchor_digest"] != _ZERO
            or state["last_id"] == 0
            and state["last_digest"] != _ZERO
            or any(
                type(state[key]) is not str
                or len(state[key]) != 64
                or any(c not in "0123456789abcdef" for c in state[key])
                for key in ("anchor_digest", "last_digest")
            )
        ):
            raise ValueError
        return state

    def _verify_chain(self, connection):
        state = self._state(connection)
        previous, last = state["anchor_digest"], state["anchor_id"]
        rows = connection.execute(
            "SELECT * FROM audit_events ORDER BY id LIMIT ?", (MAX_AUDIT_ROWS + 1,)
        )
        count = 0
        for row in rows:
            count += 1
            event = _event(row)
            _validate_event(event)
            if (
                count > MAX_AUDIT_ROWS
                or event["id"] != last + 1
                or row["previous_digest"] != previous
                or row["digest"] != _digest(previous, event)
            ):
                raise ValueError
            previous, last = row["digest"], event["id"]
        if (last, previous) != (state["last_id"], state["last_digest"]):
            raise ValueError

    def _execute(self, operation, *, permit_failed=False):
        with self._lock:
            try:
                if self._connection is None or self._failed and not permit_failed:
                    raise ValueError
                self._admit()
                result = operation(self._connection)
                self._admit()
                return result
            except Exception:
                self._failed = True
                if self._connection is not None and self._connection.in_transaction:
                    try:
                        self._connection.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                raise OperatorAuditError("Audit state unavailable") from None

    def check(self):
        """Preflight before a side effect; a failed completion blocks later changes."""
        return self._execute(lambda connection: self._state(connection)["last_id"])

    def record(
        self,
        action,
        *,
        outcome="success",
        actor=None,
        target_id=None,
        target_kind=None,
        request_id=None,
        status_code=None,
        changes=(),
        details=None,
    ):
        context = _request_context.get()
        now = self._monotonic()
        if type(now) not in (int, float) or not math.isfinite(now):
            raise ValueError("Audit elapsed time was refused")
        elapsed = (now - self._began) * 1000
        if not math.isfinite(elapsed) or not 0 <= elapsed < 2**63:
            raise ValueError("Audit elapsed time was refused")
        actor_id = _identifier(actor)
        if actor_id is None and context is not None:
            actor_id = context.get("actor_id")
        request = request_id or (context["request_id"] if context is not None else uuid4())
        if not isinstance(changes, (tuple, list)):
            raise ValueError("Audit changes were refused")
        event = {
            "id": 1,
            "occurred_at": _utc(self._utc_now()),
            "run_id": self.run_id,
            "elapsed_ms": int(elapsed),
            "request_id": _identifier(request, optional=False),
            "actor_id": actor_id,
            "action": action,
            "outcome": outcome,
            "target_kind": target_kind,
            "target_id": _identifier(target_id),
            "status_code": status_code,
            "changes": list(changes),
            "details": _details(details),
        }
        try:
            _validate_event(event)
        except Exception:
            raise ValueError("Audit metadata was refused") from None

        def write(connection):
            connection.execute("BEGIN IMMEDIATE")
            state = self._state(connection)
            if state["last_id"]:
                tail = connection.execute(
                    "SELECT * FROM audit_events WHERE id=?", (state["last_id"],)
                ).fetchone()
                if tail is None:
                    raise ValueError
                prior = _event(tail)
                _validate_event(prior)
                if tail["digest"] != state["last_digest"] or tail["digest"] != _digest(
                    tail["previous_digest"], prior
                ):
                    raise ValueError
            event["id"] = state["last_id"] + 1
            previous = state["last_digest"]
            digest = _digest(previous, event)
            connection.execute(
                "INSERT INTO audit_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(
                    event[k]
                    for k in (
                        "id",
                        "occurred_at",
                        "run_id",
                        "elapsed_ms",
                        "request_id",
                        "actor_id",
                        "action",
                        "outcome",
                        "target_kind",
                        "target_id",
                        "status_code",
                    )
                )
                + (
                    json.dumps(event["changes"], separators=(",", ":")),
                    json.dumps(event["details"], separators=(",", ":")),
                    previous,
                    digest,
                ),
            )
            connection.execute(
                "UPDATE audit_state SET last_id=?,last_digest=?", (event["id"], digest)
            )
            cutoff = event["id"] - MAX_AUDIT_ROWS
            if cutoff > state["anchor_id"]:
                anchor = connection.execute(
                    "SELECT digest FROM audit_events WHERE id=?", (cutoff,)
                ).fetchone()
                if anchor is None:
                    raise ValueError
                connection.execute(
                    "UPDATE audit_state SET anchor_id=?,anchor_digest=?", (cutoff, anchor[0])
                )
                connection.execute("DELETE FROM audit_events WHERE id<=?", (cutoff,))
            connection.execute("COMMIT")
            return event["id"]

        return self._execute(write)

    def events(
        self,
        *,
        limit=50,
        before=None,
        action=None,
        outcome=None,
        actor_id=None,
        target_id=None,
        start=None,
        end=None,
        export=False,
    ):
        maximum = MAX_EXPORT_ROWS if export is True else 100
        if type(limit) is not int or not 1 <= limit <= maximum or type(export) is not bool:
            raise ValueError("Audit event limit was refused")
        if before is not None and (type(before) is not int or not 1 <= before < 2**63):
            raise ValueError("Audit event cursor was refused")
        if (
            action is not None
            and action not in ACTIONS
            or outcome is not None
            and outcome not in OUTCOMES
        ):
            raise ValueError("Audit event filter was refused")
        clauses, args = [], []
        for name, value in (
            ("action", action),
            ("outcome", outcome),
            ("actor_id", _identifier(actor_id)),
            ("target_id", _identifier(target_id)),
        ):
            if value is not None:
                clauses.append(name + "=?")
                args.append(value)
        if before is not None:
            clauses.append("id<?")
            args.append(before)
        if start is not None or end is not None:
            if start is None or end is None or start >= end:
                raise ValueError("Audit event range was refused")
            clauses.extend(("occurred_at>=?", "occurred_at<?"))
            args.extend((_utc(start), _utc(end)))

        def read(connection):
            state = self._state(connection)
            count = connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
            if count != state["last_id"] - state["anchor_id"]:
                raise ValueError
            query = (
                "SELECT * FROM audit_events"
                + (" WHERE " + " AND ".join(clauses) if clauses else "")
                + " ORDER BY id DESC LIMIT ?"
            )
            rows = connection.execute(query, (*args, limit + 1)).fetchall()
            for row in rows:
                event = _event(row)
                _validate_event(event)
                if row["digest"] != _digest(row["previous_digest"], event):
                    raise ValueError
                if row["id"] == state["anchor_id"] + 1:
                    previous = state["anchor_digest"]
                else:
                    prior = connection.execute(
                        "SELECT digest FROM audit_events WHERE id=?", (row["id"] - 1,)
                    ).fetchone()
                    if prior is None:
                        raise ValueError
                    previous = prior[0]
                if row["previous_digest"] != previous:
                    raise ValueError
            events = [_event(row) for row in rows[:limit]]
            return {
                "events": events,
                "has_more": len(rows) > limit,
                "next_before": events[-1]["id"] if len(rows) > limit else None,
                "first_retained_id": state["anchor_id"] + 1 if state["last_id"] else None,
                "last_id": state["last_id"],
                "total_rows": state["last_id"] - state["anchor_id"],
                "logging_available": not self._failed and self._connection is not None,
            }

        return self._execute(read, permit_failed=True)

    def close(self):
        with self._lock:
            connection, self._connection = self._connection, None
            if connection is not None:
                connection.close()
