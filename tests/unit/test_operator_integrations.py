from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from k5vision import identity_state
from k5vision.operator_events import OperatorEventStore
from k5vision.operator_integration_bridge import K5LabBridgeClient
from k5vision.operator_integration_contracts import (
    IntegrationContractError,
    analytics_lab_event,
    k5_lab_request,
    k5_lab_response,
    negotiate_k5_lab,
)
from k5vision.operator_integrations import OperatorIntegrationError, OperatorIntegrationStore
from k5vision.operator_integrations_api import install_operator_integrations_api


@pytest.fixture
def identity(tmp_path, monkeypatch):
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
        tmp_path / "identity", username="integration-admin", password="integration-test-password"
    )


@pytest.fixture
def store(identity):
    result = OperatorIntegrationStore(identity.directory, identity.site_id)
    yield result
    result.close()


def _connection(store, camera, kind="neural", **extra):
    result = store.create(
        name="Generated provider", kind=kind, enabled=True, camera_ids=[str(camera)], **extra
    )
    return UUID(result["id"]), result["setup_token"]


def _analytic(camera):
    # Independent fixture using the exact published Analytics Lab draft0.1 fields.
    return {
        "schema_version": "0.1",
        "event_id": str(uuid4()),
        "analytic_id": "person_down",
        "event_type": "person_down_candidate",
        "source_id": str(camera),
        "start_time": "2026-09-14T22:31:08Z",
        "end_time": "2026-09-14T22:37:31Z",
        "confidence": 0.96,
        "severity": "high",
        "status": "candidate",
        "objects": [],
        "zones": [],
        "observations": [],
        "evidence": [],
        "relationships": [],
        "recommended_actions": [{"action": "never_execute"}],
        "metadata": {"private": "excluded"},
    }


def _response(request, status="recommendation"):
    return {k: request[k] for k in ("contract_version", "request_id", "partition", "record_id")} | {
        "status": status,
        "reason_code": "generated_review",
        "operation": "review" if status == "recommendation" else None,
        "confidence": 0.7 if status == "recommendation" else None,
        "authorized": False,
    }


def _person(store, camera, index):
    store.configure(camera, enabled=True, confidence_threshold=0.65)
    return store.record(
        camera,
        frame_key=f"{index:064x}",
        occurred_at=datetime(2026, 9, 14, 12, 0, index, tzinfo=UTC),
        confidence=0.8,
        person_count=2,
        emit=True,
    )


def test_exact_lab_v2_negotiation_correlation_and_non_authorization():
    assert negotiate_k5_lab(["1", "2"]) == "2"
    for versions in (["1"], [], ["2", "2"], "2", [True], ["v.2"]):
        with pytest.raises(IntegrationContractError):
            negotiate_k5_lab(versions)
    request = k5_lab_request("a" * 64, "b" * 64)
    assert set(request) == {
        "contract_version",
        "request_id",
        "partition",
        "record_id",
        "timeout_ms",
    }
    assert k5_lab_response(request, _response(request))["authorized"] is False
    for change in (
        {"authorized": True},
        {"record_id": "other"},
        {"contract_version": "1"},
        {"extra": "forbidden"},
        {"operation": "a.b"},
        {"confidence": float("nan")},
    ):
        with pytest.raises(IntegrationContractError):
            k5_lab_response(request, _response(request) | change)
    for change in ({"operation": "review"}, {"confidence": 0.2}):
        with pytest.raises(IntegrationContractError):
            k5_lab_response(request, _response(request, "abstain") | change)


def test_actual_analytics_schema_mapping_preserves_utc_excludes_private_metadata():
    camera, connection = uuid4(), uuid4()
    raw = _analytic(camera)
    mapped = analytics_lab_event(
        raw,
        connection_id=connection,
        site_id="generated",
        ingested_at=datetime(2026, 10, 31, tzinfo=UTC),
    )
    assert mapped.observed_at.isoformat() == "2026-09-14T22:31:08+00:00"
    assert mapped.status == "candidate"
    encoded = mapped.model_dump_json()
    assert "private" not in encoded and "never_execute" not in encoded
    assert mapped.extensions["authority"] == "observe_only"
    assert (
        analytics_lab_event(
            raw,
            connection_id=connection,
            site_id="generated",
            ingested_at=datetime(2026, 11, 1, tzinfo=UTC),
        ).event_id
        == mapped.event_id
    )
    with pytest.raises(IntegrationContractError, match="source_clock_ahead"):
        analytics_lab_event(
            raw,
            connection_id=connection,
            site_id="generated",
            ingested_at=datetime(2026, 9, 1, tzinfo=UTC),
        )
    with pytest.raises(IntegrationContractError):
        analytics_lab_event(
            raw | {"site_id": "foreign"},
            connection_id=connection,
            site_id="generated",
            ingested_at=datetime.now(UTC),
        )


def test_credentials_are_write_only_scope_revoked_and_configuration_survives_restart(
    identity, store
):
    camera = uuid4()
    identifier, token = _connection(store, camera)
    assert store.list()["connections"][0]["status"] == "awaiting_provider"
    assert token not in json.dumps(store.list())
    assert token.encode() not in (identity.directory / "operator-integrations.sqlite3").read_bytes()
    with pytest.raises(OperatorIntegrationError, match="unauthorized"):
        store.authenticate(identifier, "human-session-token-" + "x" * 32)
    with pytest.raises(OperatorIntegrationError, match="unauthorized"):
        store.authenticate(uuid4(), token)
    store.handshake(identifier, token, ["2"])
    assert store.test(identifier)["compatible"] is True
    replacement = "new-machine-key-" + "x" * 40
    store.update(identifier, {"token": replacement})
    with pytest.raises(OperatorIntegrationError, match="unauthorized"):
        store.authenticate(identifier, token)
    assert store.test(identifier)["status"] == "awaiting_provider"
    store.close()
    reopened = OperatorIntegrationStore(identity.directory, identity.site_id)
    try:
        assert reopened.authenticate(identifier, replacement)["camera_ids"] == [str(camera)]
        assert reopened.test(identifier)["compatible"] is False
        reopened.update(identifier, {"enabled": False})
        with pytest.raises(OperatorIntegrationError):
            reopened.authenticate(identifier, replacement)
    finally:
        reopened.close()


def test_exact_analytics_ingress_idempotency_current_camera_scope_and_restart(identity, store):
    camera, other = uuid4(), uuid4()
    identifier, token = _connection(store, camera, "analytics")
    store.handshake(identifier, token, ["0.1"])
    event = _analytic(camera)
    assert (
        store.ingest(identifier, token, event, known_camera=lambda c: c == camera)["duplicate"]
        is False
    )
    assert (
        store.ingest(identifier, token, event, known_camera=lambda c: c == camera)["duplicate"]
        is True
    )
    with pytest.raises(IntegrationContractError, match="event_conflict"):
        store.ingest(
            identifier, token, event | {"confidence": 0.7}, known_camera=lambda c: c == camera
        )
    for callback, payload in ((lambda c: False, event), (lambda c: True, _analytic(other))):
        with pytest.raises(OperatorIntegrationError, match="camera_forbidden"):
            store.ingest(identifier, token, payload, known_camera=callback)
    assert len(store.events(camera_id=camera)["events"]) == 1
    assert store.events(camera_id=other)["events"] == []
    store.close()
    reopened = OperatorIntegrationStore(identity.directory, identity.site_id)
    try:
        result = reopened.events(camera_id=camera)["events"][0]
        assert result["playback_at"].startswith("2026-09-14T22:31:08")
        assert result["status"] == "candidate"
    finally:
        reopened.close()


def test_durable_observation_delivery_retry_restart_clock_rollback_and_receipt_idempotency(
    identity, store
):
    camera = uuid4()
    person = OperatorEventStore(identity.directory, identity.site_id)
    try:
        identifier, token = _connection(store, camera)
        assert _person(person, camera, 1)
        store.capture_person_events(person, known_camera=lambda c: c == camera)
        store.handshake(identifier, token, ["2"])
        first = store.pull(identifier, token, known_camera=lambda c: c == camera)["requests"][0]
        assert first["observation"]["observed_at"].startswith("2026-09-14T12:00:01")
        assert first["delivery"]["attempt"] == 1 and not first["delivery"]["replay"]
        assert store.pull(identifier, token, known_camera=lambda c: True)["requests"] == []
        store.close()
        reopened = OperatorIntegrationStore(identity.directory, identity.site_id)
        try:
            reopened.handshake(identifier, token, ["2"])
            second = reopened.pull(identifier, token, known_camera=lambda c: True)["requests"][0]
            assert second["request"] == first["request"]
            assert second["observation"] == first["observation"]
            assert second["delivery"]["attempt"] == 2 and second["delivery"]["replay"]
            message = {"request": second["request"], "response": _response(second["request"])}
            assert (
                reopened.respond(identifier, token, message, known_camera=lambda c: True)[
                    "authorized"
                ]
                is False
            )
            assert (
                reopened.respond(identifier, token, message, known_camera=lambda c: True)[
                    "duplicate"
                ]
                is True
            )
            with pytest.raises(IntegrationContractError, match="response_conflict"):
                reopened.respond(
                    identifier,
                    token,
                    message | {"response": _response(second["request"]) | {"operation": "other"}},
                    known_camera=lambda c: True,
                )
            reopened.capture_person_events(person, known_camera=lambda c: True)
            assert reopened.pull(identifier, token, known_camera=lambda c: True)["requests"] == []
            assert reopened.list()["connections"][0]["delivered_events"] == 1
        finally:
            reopened.close()
    finally:
        person.close()


def _app(store, camera):
    app = FastAPI()
    admin_account = SimpleNamespace(id=uuid4())
    viewer_account = SimpleNamespace(id=uuid4())

    async def authenticate(token):
        if token not in ("human-admin", "human-viewer"):
            raise HTTPException(401)
        return (admin_account if token == "human-admin" else viewer_account), token

    async def human(request: Request):
        return await authenticate(request.headers.get("authorization", "").removeprefix("Bearer "))

    async def admin(request: Request):
        result = await human(request)
        if result[0] is not admin_account:
            raise HTTPException(403)
        return result

    def selection(account, selected):
        if selected != camera:
            raise HTTPException(403)

    install_operator_integrations_api(
        app,
        integrations=store,
        registry=SimpleNamespace(get=lambda value: object() if value == camera else None),
        require_admin=admin,
        require_session=human,
        authenticate=authenticate,
        authorized_selection=selection,
        mutations=asyncio.Lock(),
    )
    return app


def test_http_separate_machine_auth_grants_and_human_event_access(store):
    camera = uuid4()
    client = TestClient(_app(store, camera))
    admin = {"Authorization": "Bearer human-admin"}
    viewer = {"Authorization": "Bearer human-viewer"}
    path = "/api/v1/operator/admin/integrations"
    assert client.get(path, headers=viewer).status_code == 403
    created = client.post(
        path,
        headers=admin,
        json={
            "name": "Producer",
            "kind": "analytics",
            "enabled": True,
            "camera_ids": [str(camera)],
        },
    ).json()
    identifier, token = created["id"], created["setup_token"]
    machine = {"X-Integration-Token": token}
    prefix = "/api/v1/operator/integrations/" + identifier
    assert (
        client.post(
            prefix + "/handshake", headers=admin, json={"supported_versions": ["0.1"]}
        ).status_code
        == 401
    )
    assert (
        client.post(
            prefix + "/handshake", headers=machine | admin, json={"supported_versions": ["0.1"]}
        ).status_code
        == 401
    )
    assert (
        client.post(
            prefix + "/handshake", headers=machine, json={"supported_versions": ["0.1"]}
        ).status_code
        == 200
    )
    assert (
        client.post(prefix + "/events", headers=machine, json=_analytic(camera)).status_code == 200
    )
    assert (
        client.post(prefix + "/events", headers=machine, json=_analytic(uuid4())).status_code == 403
    )
    assert (
        len(
            client.get(
                f"/api/v1/operator/cameras/{camera}/integration-events", headers=viewer
            ).json()["events"]
        )
        == 1
    )
    assert (
        client.get(
            f"/api/v1/operator/cameras/{uuid4()}/integration-events", headers=viewer
        ).status_code
        == 403
    )
    assert client.delete(path + "/" + identifier, headers=admin).status_code == 204
    assert (
        client.post(
            prefix + "/handshake", headers=machine, json={"supported_versions": ["0.1"]}
        ).status_code
        == 401
    )


def test_real_native_http_bridge_with_actual_v2_and_analytics_contracts(identity, store):
    camera = uuid4()
    neural_id, neural_key = _connection(store, camera)
    analytic_id, analytic_key = _connection(store, camera, "analytics")
    person = OperatorEventStore(identity.directory, identity.site_id)
    _person(person, camera, 1)
    store.capture_person_events(person, known_camera=lambda c: True)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(_app(store, camera), log_level="error", access_log=False)
    )
    worker = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and worker.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        bridge = K5LabBridgeClient(f"http://127.0.0.1:{port}", neural_id, neural_key)
        assert bridge.handshake()["selected_version"] == "2"
        message = bridge.observations()["requests"][0]
        assert (
            bridge.respond(message["request"], _response(message["request"]))["authorized"] is False
        )
        analytic = K5LabBridgeClient(f"http://127.0.0.1:{port}", analytic_id, analytic_key)
        assert analytic.analytics_handshake()["selected_version"] == "0.1"
        assert analytic.ingest_analytic(_analytic(camera))["accepted"] is True
        with pytest.raises(IntegrationContractError, match="source_clock_ahead"):
            analytic.ingest_analytic(
                _analytic(camera)
                | {"start_time": "2099-01-01T12:00:00Z", "end_time": "2099-01-01T12:01:00Z"}
            )
    finally:
        server.should_exit = True
        worker.join(timeout=5)
        listener.close()
        person.close()
    assert not worker.is_alive()


@pytest.mark.parametrize(
    "changes",
    [
        {"origin": "http://example.com"},
        {"origin": "https://169.254.169.254"},
        {"origin": "https://user:secret@example.com"},
        {"origin": "https://example.com/path"},
        {"token": "short"},
        {"camera_ids": ["not-a-camera"]},
        {"name": "bad\nname"},
    ],
)
def test_configuration_refuses_unscoped_or_unsafe_values(store, changes):
    arguments = {"name": "Provider", "kind": "neural", "camera_ids": [str(uuid4())]} | changes
    with pytest.raises(IntegrationContractError):
        store.create(**arguments)
    assert store.list()["connections"] == []


def test_provider_elapsed_lease_retry_ceiling_capacity_and_grant_change(
    identity, store, monkeypatch
):
    import k5vision.operator_integrations as module

    camera, other = uuid4(), uuid4()
    identifier, token = _connection(store, camera)
    person = OperatorEventStore(identity.directory, identity.site_id)
    now = [1000.0]
    store._clock = lambda: now[0]
    try:
        for index in range(1, 4):
            _person(person, camera, index)
        monkeypatch.setattr(module, "MAX_PENDING", 2)
        store.capture_person_events(person, known_camera=lambda c: True)
        assert store.test(identifier)["status"] == "capacity"
        assert store.list()["connections"][0]["queued_events"] == 2
        store.handshake(identifier, token, ["2"])
        for attempt in range(1, 9):
            rows = store.pull(identifier, token, known_camera=lambda c: True)["requests"]
            assert len(rows) == 2
            assert {row["delivery"]["attempt"] for row in rows} == {attempt}
            now[0] += 61
            store.handshake(identifier, token, ["2"])
        assert store.pull(identifier, token, known_camera=lambda c: True)["requests"] == []
        assert store.test(identifier)["exhausted_events"] == 2
        assert store.test(identifier)["status"] == "unavailable"
        store.retry(identifier)
        store.handshake(identifier, token, ["2"])
        retried = store.pull(identifier, token, known_camera=lambda c: True)["requests"]
        assert retried[0]["request"] == rows[0]["request"]
        assert retried[0]["delivery"]["replay"] is True
        # Explicit camera grant revocation removes old pending observations;
        # an old reply never restores revoked access.
        stale = rows[0]
        store.update(identifier, {"camera_ids": [str(other)]})
        store.handshake(identifier, token, ["2"])
        with pytest.raises(IntegrationContractError, match="correlation"):
            store.respond(
                identifier,
                token,
                {"request": stale["request"], "response": _response(stale["request"])},
                known_camera=lambda c: True,
            )
        store.delete(identifier)
        assert store._leases == {}
    finally:
        person.close()


def test_failed_version_handshake_never_marks_provider_ready(store):
    identifier, token = _connection(store, uuid4())
    with pytest.raises(IntegrationContractError):
        store.handshake(identifier, token, ["1"])
    assert store.test(identifier)["status"] == "incompatible"
    assert store.test(identifier)["compatible"] is False
    with pytest.raises(OperatorIntegrationError, match="handshake_required"):
        store.pull(identifier, token, known_camera=lambda c: True)


def test_private_database_replacement_and_foreign_site_fail_closed(identity, store):
    identifier, token = _connection(store, uuid4())
    with pytest.raises(OperatorIntegrationError):
        OperatorIntegrationStore(identity.directory, str(uuid4()))
    path = identity.directory / "operator-integrations.sqlite3"
    moved = path.with_suffix(".admitted")
    try:
        path.rename(moved)
    except PermissionError as error:
        assert os.name == "nt" and error.winerror == 32
        assert path.exists() and not moved.exists()
        assert store.authenticate(identifier, token)["id"] == str(identifier)
        return
    path.write_bytes(moved.read_bytes())
    if os.name == "posix":
        path.chmod(0o600)
    with pytest.raises(OperatorIntegrationError):
        store.authenticate(identifier, token)


def test_person_handoff_preserves_original_utc_and_append_order_across_clock_steps(
    identity, store, monkeypatch
):
    import k5vision.operator_integrations as module

    camera = uuid4()
    identifier, token = _connection(store, camera)
    person = OperatorEventStore(identity.directory, identity.site_id)
    first = datetime(2026, 11, 2, 12, 0, 0, tzinfo=UTC)
    second = datetime(2026, 10, 29, 12, 0, 0, tzinfo=UTC)
    try:
        person.configure(camera, enabled=True, confidence_threshold=0.65)
        for index, observed in enumerate((first, second), 1):
            assert person.record(
                camera,
                frame_key=f"{index:064x}",
                occurred_at=observed,
                confidence=0.9,
                person_count=1,
                emit=True,
            )
        monkeypatch.setattr(module, "_stamp", lambda: "2026-10-31T12:00:00.000Z")
        store.capture_person_events(person, known_camera=lambda c: True)
        store.handshake(identifier, token, ["2"])
        observations = store.pull(identifier, token, known_camera=lambda c: True)["requests"]
        assert [row["observation"]["observed_at"] for row in observations] == [
            "2026-11-02T12:00:00Z",
            "2026-10-29T12:00:00Z",
        ]
        assert observations[0]["delivery"]["sequence"] < observations[1]["delivery"]["sequence"]
        assert [row["observation"]["observation_id"] for row in observations] == [
            row["request"]["record_id"] for row in observations
        ]
    finally:
        person.close()


def test_future_analytic_source_is_rejected_with_health_and_durable_failure_audit(identity, store):
    from k5vision.operator_audit import OperatorAuditStore

    camera = uuid4()
    identifier, token = _connection(store, camera, "analytics")
    app = _app(store, camera)
    audit = OperatorAuditStore(identity.directory, identity.site_id)
    app.state.operator_audit = audit
    client = TestClient(app)
    headers = {"X-Integration-Token": token}
    prefix = f"/api/v1/operator/integrations/{identifier}"
    try:
        assert (
            client.post(
                prefix + "/handshake", headers=headers, json={"supported_versions": ["0.1"]}
            ).status_code
            == 200
        )
        event = _analytic(camera) | {
            "start_time": "2099-01-01T12:00:00Z",
            "end_time": "2099-01-01T12:01:00Z",
        }
        response = client.post(prefix + "/events", headers=headers, json=event)
        assert response.status_code == 422
        assert response.json() == {"detail": "integration_source_clock_ahead"}
        assert store.test(identifier)["status"] == "source_clock_ahead"
        assert store.events(camera_id=camera)["events"] == []
        failures = audit.events(action="integration.event", outcome="failure")["events"]
        assert len(failures) == 1 and failures[0]["status_code"] == 422
        assert failures[0]["target_id"] == str(identifier)
    finally:
        audit.close()


@pytest.mark.parametrize("stage", ["headers", "body"])
def test_native_bridge_elapsed_deadline_stops_slow_peer_without_leaking_token(stage):
    from k5vision.operator_integration_contracts import IntegrationContractError

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(5)
    port = listener.getsockname()[1]
    finished = threading.Event()

    def peer():
        try:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(5)
                connection.recv(16 * 1024)
                if stage == "body":
                    connection.sendall(
                        b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n"
                        b"Content-Length: 1000\r\n\r\n"
                    )
                for _ in range(100):
                    connection.sendall(b"H" if stage == "headers" else b" ")
                    time.sleep(0.1)
        except OSError:
            pass
        finally:
            finished.set()

    worker = threading.Thread(target=peer, daemon=True)
    worker.start()
    token = "private-bridge-credential-" + "x" * 32
    client = K5LabBridgeClient(f"http://127.0.0.1:{port}", uuid4(), token)
    began = time.monotonic()
    try:
        with pytest.raises(
            IntegrationContractError, match="integration_provider_unavailable"
        ) as error:
            client.handshake()
        assert token not in str(error.value)
        assert 2 <= time.monotonic() - began < 5
        assert finished.wait(2)
    finally:
        listener.close()
        worker.join(timeout=2)
    assert not worker.is_alive()


def test_required_state_anchor_prevents_lost_integration_database_silent_reset(identity, store):
    import sqlite3

    identifier, token = _connection(store, uuid4())
    store.close()
    path = identity.directory / "operator-integrations.sqlite3"
    path.unlink()
    with pytest.raises(OperatorIntegrationError, match="integration_unavailable"):
        OperatorIntegrationStore(identity.directory, identity.site_id)
    assert not path.exists()
    with sqlite3.connect(identity.database_path) as authority:
        assert authority.execute(
            "SELECT schema_version FROM operator_component_anchors "
            "WHERE site_id=? AND component='integrations'",
            (identity.site_id,),
        ).fetchone() == (1,)
