"""Strict production-admission witnesses; no filesystem/identity admission shims."""

import multiprocessing
import os
from contextlib import contextmanager
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from test_operator_integrations import _app, _person

from k5vision import identity_state
from k5vision.operator_events import OperatorEventStore
from k5vision.operator_integrations import OperatorIntegrationError, OperatorIntegrationStore


def _receive(pipe):
    assert pipe.poll(10), "worker did not reach its bounded checkpoint"
    return pipe.recv()


def _worker(root, site, identifier, token, pipe):
    service = None
    try:
        service = OperatorIntegrationStore(root, site)
        service.handshake(identifier, token, ["2"])
        pipe.send("ready")
        operation, payload, gate = _receive(pipe)
        fired = False

        def trace(sql):
            nonlocal fired
            # Gate immediately before first transaction/write admission. On the
            # baseline this UPDATE occurs after credentials and rows were read.
            if not fired and (sql == "BEGIN IMMEDIATE" or sql.startswith("UPDATE deliveries")):
                fired = True
                pipe.send("admission")
                if gate == "admission":
                    assert _receive(pipe) == "release"

        def known(camera):
            if gate == "camera":
                pipe.send("camera")
                assert _receive(pipe) == "release"
            return True

        service._db.set_trace_callback(trace)
        try:
            if operation == "pull":
                result = service.pull(identifier, token, known_camera=known)
            else:
                result = service.update(identifier, payload)
            pipe.send(("ok", result))
        except Exception as error:
            pipe.send(("error", type(error).__name__, str(error)))
        pipe.send(("transaction", service._db.in_transaction))
    except Exception as error:
        pipe.send(("startup_error", type(error).__name__, str(error)))
    finally:
        if service is not None:
            service.close()
        pipe.close()


@contextmanager
def _process(state, identifier, token):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(
        target=_worker, args=(state.directory, state.site_id, identifier, token, child)
    )
    process.start()
    child.close()
    try:
        assert _receive(parent) == "ready"
        yield parent
    finally:
        process.join(2)
        if process.is_alive():
            process.terminate()
            process.join(2)
        parent.close()
        assert not process.is_alive()
        assert process.exitcode == 0


@pytest.fixture
def queued(tmp_path):
    if os.name == "posix":
        info = tmp_path.stat()
        assert info.st_uid == os.geteuid() and info.st_mode & 0o077 == 0
    state = identity_state.initialize_identity_state(
        tmp_path / "identity", username="synthetic-admin", password="synthetic-test-password"
    )
    service = OperatorIntegrationStore(state.directory, state.site_id)
    person = OperatorEventStore(state.directory, state.site_id)
    try:
        camera = uuid4()
        created = service.create(
            name="Synthetic retrieval", kind="neural", enabled=True, camera_ids=[str(camera)]
        )
        identifier, token = UUID(created["id"]), created["setup_token"]
        _person(person, camera, 1)
        service.capture_person_events(person, known_camera=lambda _: True)
        service.handshake(identifier, token, ["2"])
        yield state, service, identifier, token, camera
    finally:
        person.close()
        service.close()


def _change(revocation):
    return {
        "disable": {"enabled": False},
        "rotate": {"token": "replacement-" + "x" * 40},
        "scope": {"camera_ids": [str(uuid4())]},
    }[revocation]


@pytest.mark.parametrize("revocation", ["disable", "rotate", "scope", "delete"])
def test_revocation_committed_before_pull_admission_discloses_nothing(queued, revocation):
    state, service, identifier, token, _ = queued
    with _process(state, identifier, token) as worker:
        worker.send(("pull", None, "admission"))
        assert _receive(worker) == "admission"
        if revocation == "delete":
            service.delete(identifier)
        else:
            service.update(identifier, _change(revocation))
        worker.send("release")
        result = _receive(worker)
        assert _receive(worker) == ("transaction", False)
    if revocation == "scope":
        assert result == ("ok", {"requests": [], "authority": "observe_only"})
    else:
        assert result == ("error", "OperatorIntegrationError", "integration_unauthorized")
    assert service._db.execute("SELECT count(*) FROM deliveries WHERE attempt>0").fetchone()[0] == 0


@pytest.mark.parametrize("revocation", ["disable", "scope"])
def test_pull_admitted_first_may_finish_before_revocation(queued, revocation):
    state, service, identifier, token, _ = queued
    with (
        _process(state, identifier, token) as puller,
        _process(state, identifier, token) as revoker,
    ):
        puller.send(("pull", None, "camera"))
        checkpoint = _receive(puller)
        if checkpoint == "admission":
            checkpoint = _receive(puller)
        assert checkpoint == "camera"
        revoker.send(("update", _change(revocation), None))
        assert _receive(revoker) == "admission"
        blocked = not revoker.poll(0.1)
        puller.send("release")
        result = _receive(puller)
        if result == "admission":
            result = _receive(puller)
        assert _receive(puller) == ("transaction", False)
        revoked = _receive(revoker)
        assert _receive(revoker) == ("transaction", False)
    assert blocked, "revocation committed while retrieval still used old authority"
    assert result[0] == "ok" and len(result[1]["requests"]) == 1
    assert result[1]["authority"] == "observe_only"
    assert revoked[0] == "ok"
    if revocation == "disable":
        with pytest.raises(OperatorIntegrationError, match="integration_unauthorized"):
            service.pull(identifier, token, known_camera=lambda _: True)
    else:
        assert service.pull(identifier, token, known_camera=lambda _: True)["requests"] == []


def test_partial_batch_failure_rolls_back_attempts_and_leases(queued):
    state, service, identifier, token, camera = queued
    person = OperatorEventStore(state.directory, state.site_id)
    try:
        _person(person, camera, 2)
        service.capture_person_events(person, known_camera=lambda _: True)
    finally:
        person.close()
    visits = 0

    def failing(camera):
        nonlocal visits
        visits += 1
        if visits == 2:
            raise RuntimeError("synthetic second-row failure")
        return True

    with pytest.raises(OperatorIntegrationError, match="integration_unavailable"):
        service.pull(identifier, token, known_camera=failing)
    assert not service._db.in_transaction
    assert service._leases == {}
    assert [tuple(row) for row in service._db.execute("SELECT attempt,sent FROM deliveries")] == [
        (0, 0),
        (0, 0),
    ]
    result = service.pull(identifier, token, known_camera=lambda _: True)
    assert len(result["requests"]) == 2
    assert all(item["delivery"]["attempt"] == 1 for item in result["requests"])
    assert service.pull(identifier, token, known_camera=lambda _: True)["requests"] == []


def test_http_observation_retrieval_refuses_completed_disable(queued):
    _, service, identifier, token, camera = queued
    client = TestClient(_app(service, camera))
    service.update(identifier, {"enabled": False})
    result = client.get(
        f"/api/v1/operator/integrations/{identifier}/observations",
        headers={"X-Integration-Token": token},
    )
    assert result.status_code == 401
    assert result.json() == {"detail": "integration_unauthorized"}
