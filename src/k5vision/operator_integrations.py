"""Private downstream-owned lab handoff, with scoped machine credentials.

Research providers connect to K5's transport. No upstream HTTP service or inference
is invented here, and a lab recommendation never executes a camera action.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from k5vision import identity_state
from k5vision.operator_component_state import ComponentStateAnchor
from k5vision.operator_integration_contracts import (
    IntegrationContractError,
    analytics_lab_event,
    k5_lab_request,
    k5_lab_response,
    negotiate_k5_lab,
    recorded_person_observation,
)

MAX_CONNECTIONS = 16
MAX_PENDING = 1000
MAX_RETAINED = 10_000
MAX_ATTEMPTS = 8
MAX_DATABASE_BYTES = 64 * 1024 * 1024
_HANDSHAKE_SECONDS = 60.0
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_STATUSES = {
    "disabled",
    "awaiting_provider",
    "ready",
    "incompatible",
    "unavailable",
    "capacity",
    "source_clock_ahead",
}


class OperatorIntegrationError(RuntimeError):
    """Finite integration failure without source values or credentials."""


def _stamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _origin(value: str) -> str:
    if value == "":
        return value
    try:
        import ipaddress

        if type(value) is not str or len(value) > 256 or any(c.isspace() for c in value):
            raise ValueError
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            address = None
        if address is not None and (
            address.is_link_local
            or address.is_multicast
            or address.is_unspecified
            or (address.is_reserved and not address.is_loopback)
        ):
            raise ValueError
        if parsed.scheme == "http" and not (address is not None and address.is_loopback):
            raise ValueError
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError
        # Declaration only: K5 never fetches this URL or forwards credentials to it.
        return value.rstrip("/")
    except (ValueError, TypeError):
        raise IntegrationContractError("integration_invalid_request") from None


def _grants(values) -> list[str]:
    if type(values) is not list or len(values) > 64:
        raise IntegrationContractError("integration_invalid_request")
    try:
        result = [str(UUID(value)) for value in values]
    except (ValueError, TypeError, AttributeError):
        raise IntegrationContractError("integration_invalid_request") from None
    if len(set(result)) != len(result):
        raise IntegrationContractError("integration_invalid_request")
    return sorted(result)


def _name(value) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value.strip()) <= 80
        or any(ord(c) < 32 for c in value)
    ):
        raise IntegrationContractError("integration_invalid_request")
    return value.strip()


def _digest(token: str) -> str:
    if type(token) is not str or not _TOKEN.fullmatch(token):
        raise IntegrationContractError("integration_invalid_request")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


class OperatorIntegrationStore:
    """Bounded site-owned configuration, event inbox and durable delivery journal."""

    def __init__(self, root: Path, site_id: str):
        self._lock = threading.RLock()
        self._db = None
        self._handshakes: dict[str, float] = {}
        self._leases: dict[tuple[str, int], float] = {}
        self._clock = time.monotonic
        try:
            state = identity_state.load_identity_state(root)
            if state.site_id != site_id:
                raise ValueError
            self.site_id = site_id
            self._root = state.directory
            self._root_identity = self._root.lstat()
            self._path = self._root / "operator-integrations.sqlite3"
            anchor = ComponentStateAnchor(
                self._root,
                site_id,
                "integrations",
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
            self._identity = self._file(self._path)
            self._admit()
            db = sqlite3.connect(
                self._path.as_uri() + "?mode=rw",
                uri=True,
                check_same_thread=False,
                isolation_level=None,
                timeout=3,
            )
            self._db = db
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA synchronous=FULL")
            if db.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                raise ValueError
            page_size = db.execute("PRAGMA page_size").fetchone()[0]
            db.execute("PRAGMA max_page_count=" + str(MAX_DATABASE_BYTES // page_size))
            if created:
                db.executescript(
                    "BEGIN IMMEDIATE;"
                    "CREATE TABLE integration_state(schema_version INTEGER,site_id TEXT);"
                    "CREATE TABLE connections(id TEXT PRIMARY KEY,name TEXT,kind TEXT,origin TEXT,"
                    "token_hash TEXT,enabled INTEGER,grants TEXT,generation INTEGER,"
                    "last_status TEXT,last_test_utc TEXT,last_observation_utc TEXT,"
                    "last_response_utc TEXT,source_cursor INTEGER DEFAULT 0);"
                    "CREATE TABLE deliveries(sequence INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "connection_id TEXT,observation_id TEXT,payload TEXT,attempt INTEGER DEFAULT 0,"
                    "response TEXT,sent INTEGER DEFAULT 0,UNIQUE(connection_id,observation_id));"
                    "CREATE TABLE received_events(sequence INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "connection_id TEXT,event_id TEXT,camera_id TEXT,payload TEXT,"
                    "UNIQUE(connection_id,event_id));"
                    "CREATE INDEX received_camera ON received_events(camera_id,sequence);"
                )
                db.execute("INSERT INTO integration_state VALUES(1,?)", (site_id,))
                db.execute("COMMIT")
            if [tuple(row) for row in db.execute("SELECT * FROM integration_state")] != [
                (1, site_id)
            ]:
                raise ValueError
            if db.execute("SELECT count(*) FROM connections").fetchone()[0] > MAX_CONNECTIONS:
                raise ValueError
            for row in db.execute("SELECT * FROM connections"):
                UUID(row["id"])
                _name(row["name"])
                _origin(row["origin"])
                _grants(json.loads(row["grants"]))
                if (
                    row["kind"] not in {"neural", "analytics"}
                    or row["enabled"] not in (0, 1)
                    or row["last_status"] not in _STATUSES
                    or not re.fullmatch(r"[0-9a-f]{64}", row["token_hash"])
                ):
                    raise ValueError
            self._admit()
            anchor.seal()
        except Exception:
            self.close()
            raise OperatorIntegrationError("integration_unavailable") from None

    @staticmethod
    def _file(path):
        info = identity_state._regular_file(path)
        if info.st_size > MAX_DATABASE_BYTES:
            raise ValueError
        if os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise ValueError
        return info

    def _admit(self):
        identity_state._same_directory(self._root, self._root_identity)
        current = self._file(self._path)
        if (current.st_dev, current.st_ino) != (self._identity.st_dev, self._identity.st_ino):
            raise ValueError
        for suffix in ("-journal", "-wal", "-shm"):
            path = Path(str(self._path) + suffix)
            if path.exists() or path.is_symlink():
                self._file(path)

    def _execute(self, operation):
        with self._lock:
            try:
                if self._db is None:
                    raise ValueError
                self._admit()
                result = operation(self._db)
                self._admit()
                return result
            except (OperatorIntegrationError, IntegrationContractError):
                if self._db is not None and self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise
            except Exception:
                if self._db is not None and self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise OperatorIntegrationError("integration_unavailable") from None

    @staticmethod
    def _row(db, identifier):
        row = db.execute("SELECT * FROM connections WHERE id=?", (str(identifier),)).fetchone()
        if row is None:
            raise OperatorIntegrationError("integration_not_found")
        return row

    def _public(self, db, row):
        identifier = row["id"]
        status = (
            "disabled"
            if not row["enabled"]
            else (
                row["last_status"]
                if row["last_status"] in {"incompatible", "capacity", "source_clock_ahead"}
                else "ready"
                if self._handshakes.get(identifier, 0) > self._clock()
                else "awaiting_provider"
            )
        )
        counts = db.execute(
            "SELECT count(*),sum(response IS NOT NULL),"
            "sum(response IS NULL AND attempt>=8) FROM deliveries WHERE connection_id=?",
            (identifier,),
        ).fetchone()
        if row["enabled"] and counts[2]:
            status = "unavailable"
        return {
            "id": identifier,
            "name": row["name"],
            "kind": row["kind"],
            "origin": row["origin"],
            "enabled": bool(row["enabled"]),
            "camera_ids": json.loads(row["grants"]),
            "credential_configured": True,
            "status": status,
            "last_test_utc": row["last_test_utc"],
            "last_observation_utc": row["last_observation_utc"],
            "last_response_utc": row["last_response_utc"],
            "capabilities": ["observations:read", "recommendations:write"]
            if row["kind"] == "neural"
            else ["analytic-events:write"],
            "queued_events": counts[0] - (counts[1] or 0),
            "delivered_events": counts[1] or 0,
            "exhausted_events": counts[2] or 0,
            "contract_version": "2" if row["kind"] == "neural" else "0.1",
            "authority": "observe_only",
        }

    def list(self):
        return self._execute(
            lambda db: {
                "connections": [
                    self._public(db, row)
                    for row in db.execute("SELECT * FROM connections ORDER BY rowid")
                ],
                "supported_contract": "2",
                "neural_authority": "observe_only",
            }
        )

    def create(self, *, name, kind, origin="", token=None, enabled=False, camera_ids=None):
        name, origin, grants = _name(name), _origin(origin), _grants(camera_ids or [])
        if kind not in {"neural", "analytics"} or type(enabled) is not bool:
            raise IntegrationContractError("integration_invalid_request")
        if enabled and not grants:
            raise IntegrationContractError("integration_camera_scope_required")
        generated = token is None
        token = secrets.token_urlsafe(32) if generated else token
        token_hash = _digest(token)
        identifier = str(uuid4())

        def write(db):
            if db.execute("SELECT count(*) FROM connections").fetchone()[0] >= MAX_CONNECTIONS:
                raise OperatorIntegrationError("integration_capacity")
            db.execute(
                "INSERT INTO connections VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)",
                (
                    identifier,
                    name,
                    kind,
                    origin,
                    token_hash,
                    int(enabled),
                    json.dumps(grants),
                    1,
                    "awaiting_provider",
                    None,
                    None,
                    None,
                ),
            )
            result = self._public(db, self._row(db, identifier))
            if generated:
                result["setup_token"] = token
            return result

        return self._execute(write)

    def update(self, identifier, changes: dict):
        if not changes or not changes.keys() <= {
            "name",
            "origin",
            "token",
            "enabled",
            "camera_ids",
        }:
            raise IntegrationContractError("integration_invalid_request")
        values = dict(changes)
        if "name" in values:
            values["name"] = _name(values["name"])
        if "origin" in values:
            values["origin"] = _origin(values["origin"])
        if "token" in values:
            values["token_hash"] = _digest(values.pop("token"))
        if "camera_ids" in values:
            values["grants"] = json.dumps(_grants(values.pop("camera_ids")))
        if "enabled" in values and type(values["enabled"]) is not bool:
            raise IntegrationContractError("integration_invalid_request")

        def write(db):
            row = self._row(db, identifier)
            grants = json.loads(values.get("grants", row["grants"]))
            if values.get("enabled", row["enabled"]) and not grants:
                raise IntegrationContractError("integration_camera_scope_required")
            db.execute("BEGIN IMMEDIATE")
            for field, value in values.items():
                db.execute(f"UPDATE connections SET {field}=? WHERE id=?", (value, str(identifier)))
            db.execute(
                "UPDATE connections SET generation=generation+1,last_status='awaiting_provider' "
                "WHERE id=?",
                (str(identifier),),
            )
            # Preserve current-scope queued identities across token rotation.
            # Discard only records whose camera grant was explicitly removed.
            for pending in db.execute(
                "SELECT sequence,payload FROM deliveries WHERE connection_id=? "
                "AND response IS NULL",
                (str(identifier),),
            ).fetchall():
                if json.loads(pending["payload"])["source_id"] not in grants:
                    db.execute("DELETE FROM deliveries WHERE sequence=?", (pending["sequence"],))
            db.execute("COMMIT")
            self._handshakes.pop(str(identifier), None)
            self._leases = {k: v for k, v in self._leases.items() if k[0] != str(identifier)}
            return self._public(db, self._row(db, identifier))

        return self._execute(write)

    def delete(self, identifier):
        def write(db):
            self._row(db, identifier)
            db.execute("BEGIN IMMEDIATE")
            for table in ("deliveries", "received_events", "connections"):
                key = "id" if table == "connections" else "connection_id"
                db.execute(f"DELETE FROM {table} WHERE {key}=?", (str(identifier),))
            db.execute("COMMIT")
            self._handshakes.pop(str(identifier), None)
            self._leases = {k: v for k, v in self._leases.items() if k[0] != str(identifier)}

        self._execute(write)

    def authenticate(self, identifier, token, *, kind=None):
        try:
            digest = _digest(token)
        except IntegrationContractError:
            raise OperatorIntegrationError("integration_unauthorized") from None

        def read(db):
            try:
                row = self._row(db, identifier)
            except OperatorIntegrationError:
                raise OperatorIntegrationError("integration_unauthorized") from None
            if (
                not row["enabled"]
                or not hmac.compare_digest(row["token_hash"], digest)
                or kind is not None
                and row["kind"] != kind
            ):
                raise OperatorIntegrationError("integration_unauthorized")
            return {
                "id": row["id"],
                "kind": row["kind"],
                "generation": row["generation"],
                "camera_ids": json.loads(row["grants"]),
            }

        return self._execute(read)

    def handshake(self, identifier, token, versions):
        self.authenticate(identifier, token)

        def write(db):
            row = self._row(db, identifier)
            selected = (
                negotiate_k5_lab(versions)
                if row["kind"] == "neural"
                else ("0.1" if type(versions) is list and versions == ["0.1"] else None)
            )
            if selected is None:
                raise IntegrationContractError("integration_incompatible")
            self._handshakes[str(identifier)] = self._clock() + _HANDSHAKE_SECONDS
            db.execute(
                "UPDATE connections SET last_status=CASE WHEN last_status='source_clock_ahead' "
                "THEN last_status ELSE 'ready' END,last_test_utc=? WHERE id=?",
                (_stamp(), str(identifier)),
            )
            return {
                "accepted": True,
                "selected_version": selected,
                "authority": "observe_only",
                "provider_lease_seconds": int(_HANDSHAKE_SECONDS),
            }

        try:
            return self._execute(write)
        except IntegrationContractError:
            self._execute(
                lambda db: db.execute(
                    "UPDATE connections SET last_status='incompatible',last_test_utc=? WHERE id=?",
                    (_stamp(), str(identifier)),
                )
            )
            self._handshakes.pop(str(identifier), None)
            raise

    def test(self, identifier):
        def read(db):
            row = self._row(db, identifier)
            result = self._public(db, row)
            result["compatible"] = result["status"] == "ready"
            return result

        return self._execute(read)

    def retry(self, identifier):
        """Explicit operator retry keeps every original observation/request identity."""

        def write(db):
            self._row(db, identifier)
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE deliveries SET attempt=0 WHERE connection_id=? AND response IS NULL",
                (str(identifier),),
            )
            db.execute(
                "UPDATE connections SET last_status='awaiting_provider' WHERE id=?",
                (str(identifier),),
            )
            db.execute("COMMIT")
            self._leases = {k: v for k, v in self._leases.items() if k[0] != str(identifier)}
            self._handshakes.pop(str(identifier), None)
            return self._public(db, self._row(db, identifier))

        return self._execute(write)

    def _machine(self, db, identifier, token, *, kind):
        # This lock is reentrant; keep credential/grant admission in the same
        # transaction as every read/write, including after provider awaits.
        authority = self.authenticate(identifier, token, kind=kind)
        if self._handshakes.get(str(identifier), 0) <= self._clock():
            raise OperatorIntegrationError("integration_handshake_required")
        return authority

    def ingest(self, identifier, token, payload: dict, *, known_camera):
        def write(db):
            authority = self._machine(db, identifier, token, kind="analytics")
            try:
                event = analytics_lab_event(
                    payload,
                    connection_id=UUID(str(identifier)),
                    site_id=self.site_id,
                    ingested_at=datetime.now(UTC),
                )
            except IntegrationContractError as error:
                if str(error) == "integration_source_clock_ahead":
                    db.execute(
                        "UPDATE connections SET last_status='source_clock_ahead' WHERE id=?",
                        (str(identifier),),
                    )
                raise
            if event.source_id not in authority["camera_ids"] or not known_camera(
                UUID(event.source_id)
            ):
                raise OperatorIntegrationError("integration_camera_forbidden")
            body = event.model_dump_json()
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT payload FROM received_events WHERE connection_id=? AND event_id=?",
                (str(identifier), event.event_id),
            ).fetchone()
            if existing is not None:
                previous = json.loads(existing[0])
                current = json.loads(body)
                for field in ("ingested_at", "processed_at"):
                    previous.pop(field, None)
                    current.pop(field, None)
                if previous != current:
                    raise IntegrationContractError("integration_event_conflict")
                db.execute("COMMIT")
                return {"accepted": True, "duplicate": True, "event_id": event.event_id}
            db.execute(
                "INSERT INTO received_events(connection_id,event_id,camera_id,payload) "
                "VALUES(?,?,?,?)",
                (str(identifier), event.event_id, event.source_id, body),
            )
            db.execute(
                "DELETE FROM received_events WHERE sequence NOT IN "
                "(SELECT sequence FROM received_events ORDER BY sequence DESC LIMIT ?)",
                (MAX_RETAINED,),
            )
            db.execute(
                "UPDATE connections SET last_observation_utc=?,last_status='ready' WHERE id=?",
                (_stamp(), str(identifier)),
            )
            db.execute("COMMIT")
            return {"accepted": True, "duplicate": False, "event_id": event.event_id}

        return self._execute(write)

    def events(self, *, camera_id, before=None, limit=50):
        if (
            type(limit) is not int
            or not 1 <= limit <= 100
            or (before is not None and (type(before) is not int or before < 1))
        ):
            raise IntegrationContractError("integration_invalid_request")

        def read(db):
            rows = db.execute(
                "SELECT sequence,payload FROM received_events WHERE camera_id=? "
                "AND sequence<? ORDER BY sequence DESC LIMIT ?",
                (str(camera_id), before or 2**63 - 1, limit + 1),
            ).fetchall()
            items = [
                {
                    "sequence": row["sequence"],
                    **json.loads(row["payload"]),
                    "playback_at": json.loads(row["payload"])["observed_at"],
                }
                for row in rows
            ]
            return {
                "events": items[:limit],
                "has_more": len(items) > limit,
                "next_before": items[limit - 1]["sequence"] if len(items) > limit else None,
            }

        return self._execute(read)

    def capture_person_events(self, person_store, *, known_camera):
        """Persist a bounded committed metadata batch, never video or camera secrets."""

        def write(db):
            for connection in db.execute(
                "SELECT * FROM connections WHERE kind='neural' AND enabled=1"
            ).fetchall():
                identifier = connection["id"]
                grants = set(json.loads(connection["grants"]))
                rows = person_store._execute(
                    lambda source, cursor=connection["source_cursor"]: [
                        dict(row)
                        for row in source.execute(
                            "SELECT id,camera_id,occurred_at,confidence,person_count "
                            "FROM person_events "
                            "WHERE id>? ORDER BY id LIMIT 50",
                            (cursor,),
                        )
                    ]
                )
                for event in rows:
                    if event["camera_id"] in grants and known_camera(UUID(event["camera_id"])):
                        count = db.execute(
                            "SELECT count(*) FROM deliveries WHERE connection_id=? "
                            "AND response IS NULL",
                            (identifier,),
                        ).fetchone()[0]
                        if count >= MAX_PENDING:
                            db.execute(
                                "UPDATE connections SET last_status='capacity' WHERE id=?",
                                (identifier,),
                            )
                            break
                        observation = recorded_person_observation(self.site_id, event)
                        db.execute(
                            "INSERT OR IGNORE INTO deliveries"
                            "(connection_id,observation_id,payload) "
                            "VALUES(?,?,?)",
                            (identifier, observation.observation_id, observation.model_dump_json()),
                        )
                    db.execute(
                        "UPDATE connections SET source_cursor=? WHERE id=?",
                        (event["id"], identifier),
                    )

        self._execute(write)

    def pull(self, identifier, token, *, known_camera, limit=10):
        if type(limit) is not int or not 1 <= limit <= 20:
            raise IntegrationContractError("integration_invalid_request")

        def read(db):
            authority = self._machine(db, identifier, token, kind="neural")
            rows = db.execute(
                "SELECT * FROM deliveries WHERE connection_id=? AND response IS NULL "
                "AND attempt<? ORDER BY sequence LIMIT 100",
                (str(identifier), MAX_ATTEMPTS),
            ).fetchall()
            result = []
            for row in rows:
                key = (str(identifier), row["sequence"])
                if self._leases.get(key, 0) > self._clock():
                    continue
                observation = json.loads(row["payload"])
                if observation["source_id"] not in authority["camera_ids"] or not known_camera(
                    UUID(observation["source_id"])
                ):
                    continue
                partition = hashlib.sha256(
                    (self.site_id + ":" + str(identifier)).encode()
                ).hexdigest()
                request = k5_lab_request(row["observation_id"], partition)
                attempt = row["attempt"] + 1
                db.execute(
                    "UPDATE deliveries SET attempt=?,sent=1 WHERE sequence=?",
                    (attempt, row["sequence"]),
                )
                self._leases[key] = self._clock() + min(60, 2**attempt)
                db.execute(
                    "UPDATE connections SET last_observation_utc=? WHERE id=?",
                    (_stamp(), str(identifier)),
                )
                result.append(
                    {
                        "request": request,
                        "observation": observation,
                        "delivery": {
                            "sequence": row["sequence"],
                            "attempt": attempt,
                            "replay": bool(row["sent"]),
                        },
                    }
                )
                if len(result) == limit:
                    break
            return {"requests": result, "authority": "observe_only"}

        return self._execute(read)

    def respond(self, identifier, token, payload, *, known_camera):
        def write(db):
            authority = self._machine(db, identifier, token, kind="neural")
            if type(payload) is not dict or payload.keys() != {"request", "response"}:
                raise IntegrationContractError("integration_invalid_response")
            request = payload["request"]
            if type(request) is not dict or type(request.get("record_id")) is not str:
                raise IntegrationContractError("integration_invalid_response")
            row = db.execute(
                "SELECT * FROM deliveries WHERE connection_id=? AND observation_id=?",
                (str(identifier), request["record_id"]),
            ).fetchone()
            if row is None or row["attempt"] < 1:
                raise IntegrationContractError("integration_correlation_mismatch")
            observation = json.loads(row["payload"])
            if observation["source_id"] not in authority["camera_ids"] or not known_camera(
                UUID(observation["source_id"])
            ):
                raise OperatorIntegrationError("integration_camera_forbidden")
            partition = hashlib.sha256((self.site_id + ":" + str(identifier)).encode()).hexdigest()
            expected = k5_lab_request(row["observation_id"], partition)
            if request != expected:
                raise IntegrationContractError("integration_correlation_mismatch")
            response = k5_lab_response(request, payload["response"])
            body = json.dumps(response, sort_keys=True, separators=(",", ":"))
            if row["response"] is not None and row["response"] != body:
                raise IntegrationContractError("integration_response_conflict")
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE deliveries SET response=? WHERE sequence=?", (body, row["sequence"]))
            db.execute(
                "UPDATE connections SET last_response_utc=?,last_status='ready' WHERE id=?",
                (_stamp(), str(identifier)),
            )
            db.execute(
                "DELETE FROM deliveries WHERE response IS NOT NULL AND sequence NOT IN "
                "(SELECT sequence FROM deliveries ORDER BY sequence DESC LIMIT ?)",
                (MAX_RETAINED,),
            )
            db.execute("COMMIT")
            self._leases.pop((str(identifier), row["sequence"]), None)
            return {
                "accepted": True,
                "duplicate": row["response"] is not None,
                "authorized": False,
                "status": response["status"],
            }

        return self._execute(write)

    def close(self):
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None
            self._handshakes.clear()
            self._leases.clear()
