"""Synthetic, spawned-process regressions against the production SQLite store."""

import multiprocessing
import os
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from test_operator_integrations import (
    _analytic,
    _connection,
    _person,
    _response,
)
from test_operator_integrations import (
    identity as identity,
)
from test_operator_integrations import (
    store as store,
)

from k5vision.operator_events import OperatorEventStore
from k5vision.operator_integrations import OperatorIntegrationError, OperatorIntegrationStore


def _worker(root, site, identifier, token, kind, pipe):
    # Match the existing identity fixture's synthetic container-root ownership.
    # No production admission code or filesystem permission is changed.
    if os.name == "posix":
        original = Path.lstat

        def system_ancestors(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path in (Path("/"), Path("/tmp")) and info.st_uid == 65534:
                fields = list(info)
                fields[4] = 0
                return os.stat_result(fields)
            return info

        Path.lstat = system_ancestors
    service = OperatorIntegrationStore(root, site)
    try:
        service.handshake(identifier, token, ["2" if kind == "neural" else "0.1"])
        pipe.send("ready")
        operation, payload, gate = pipe.recv()
        fired = False

        def trace(sql):
            nonlocal fired
            if sql == "BEGIN IMMEDIATE" and not fired:
                fired = True
                pipe.send("begin")
                if gate == "begin":
                    # sqlite trace callbacks swallow exceptions: assert in the parent too.
                    if not pipe.poll(10) or pipe.recv() != "release":
                        raise RuntimeError("test gate expired")

        def known(camera):
            if gate == "camera":
                pipe.send("camera")
                assert pipe.poll(10), "test camera gate expired"
                assert pipe.recv() == "release"
            return True

        service._db.set_trace_callback(trace)
        try:
            if operation == "update":
                result = service.update(identifier, payload)
            else:
                result = getattr(service, operation)(identifier, token, payload, known_camera=known)
            pipe.send(("ok", result))
        except Exception as error:
            pipe.send(("error", type(error).__name__, str(error)))
        pipe.send(("transaction", service._db.in_transaction))
    finally:
        service.close()
        pipe.close()


def _receive(pipe):
    assert pipe.poll(10), "spawned store failed to reach its bounded checkpoint"
    return pipe.recv()


@contextmanager
def _process(identity, identifier, token, kind="neural"):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(
        target=_worker,
        args=(identity.directory, identity.site_id, identifier, token, kind, child),
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


def _setup(identity, store, kind):
    camera = uuid4()
    identifier, token = _connection(store, camera, kind)
    if kind == "analytics":
        return identifier, token, camera, _analytic(camera)
    person = OperatorEventStore(identity.directory, identity.site_id)
    try:
        _person(person, camera, 1)
        store.capture_person_events(person, known_camera=lambda _: True)
    finally:
        person.close()
    store.handshake(identifier, token, ["2"])
    request = store.pull(identifier, token, known_camera=lambda _: True)["requests"][0]["request"]
    return identifier, token, camera, {"request": request, "response": _response(request)}


@pytest.mark.parametrize("kind,operation", [("neural", "respond"), ("analytics", "ingest")])
@pytest.mark.parametrize("revocation", ["disable", "rotate", "scope", "delete"])
def test_completed_revocation_before_sqlite_admission(identity, store, kind, operation, revocation):
    identifier, token, camera, payload = _setup(identity, store, kind)
    with _process(identity, identifier, token, kind) as worker:
        worker.send((operation, payload, "begin"))
        assert _receive(worker) == "begin"
        if revocation == "delete":
            store.delete(identifier)
        else:
            change = {
                "disable": {"enabled": False},
                "rotate": {"token": "replacement-" + "x" * 40},
                "scope": {"camera_ids": [str(uuid4())]},
            }[revocation]
            store.update(identifier, change)
        worker.send("release")
        result = _receive(worker)
        transaction = _receive(worker)
    assert result[0] == "error", result
    assert result[2] in {
        "integration_unauthorized",
        "integration_camera_forbidden",
        "integration_correlation_mismatch",
    }
    assert transaction == ("transaction", False)
    assert store.events(camera_id=camera)["events"] == []
    assert (
        store._db.execute("SELECT count(*) FROM deliveries WHERE response IS NOT NULL").fetchone()[
            0
        ]
        == 0
    )


@pytest.mark.parametrize("kind,operation", [("neural", "respond"), ("analytics", "ingest")])
def test_admitted_response_or_event_serializes_before_revocation(identity, store, kind, operation):
    identifier, token, camera, payload = _setup(identity, store, kind)
    with (
        _process(identity, identifier, token, kind) as first,
        _process(identity, identifier, token, kind) as second,
    ):
        first.send((operation, payload, "camera"))
        checkpoint = _receive(first)
        if checkpoint == "begin":
            checkpoint = _receive(first)
        assert checkpoint == "camera"
        second.send(("update", {"enabled": False}, None))
        assert _receive(second) == "begin"
        blocked = not second.poll(0.1)
        first.send("release")
        result = _receive(first)
        if result == "begin":
            result = _receive(first)
        assert _receive(first) == ("transaction", False)
        revoked = _receive(second)
        assert _receive(second) == ("transaction", False)
    assert blocked, "revocation completed while an admitted operation was still pending"
    assert result[0] == "ok" and result[1]["accepted"] is True
    assert revoked[0] == "ok" and revoked[1]["enabled"] is False
    with pytest.raises(OperatorIntegrationError, match="integration_unauthorized"):
        getattr(store, operation)(identifier, token, payload, known_camera=lambda _: True)


@pytest.mark.parametrize("duplicate", [False, True])
def test_simultaneous_responses_have_one_durable_winner(identity, store, duplicate):
    identifier, token, _, payload = _setup(identity, store, "neural")
    other = (
        payload
        if duplicate
        else payload | {"response": payload["response"] | {"operation": "other"}}
    )
    with (
        _process(identity, identifier, token) as first,
        _process(identity, identifier, token) as second,
    ):
        first.send(("respond", payload, "camera"))
        checkpoint = _receive(first)
        if checkpoint == "begin":
            checkpoint = _receive(first)
        assert checkpoint == "camera"
        second.send(("respond", other, None))
        assert _receive(second) == "begin"
        first.send("release")
        one = _receive(first)
        if one == "begin":
            one = _receive(first)
        assert _receive(first) == ("transaction", False)
        two = _receive(second)
        assert _receive(second) == ("transaction", False)
    assert one[0] == "ok" and one[1]["authorized"] is False
    assert one[1]["duplicate"] is False
    if duplicate:
        assert two[0] == "ok" and two[1]["duplicate"] is True
        assert two[1]["authorized"] is False
    else:
        assert two == ("error", "IntegrationContractError", "integration_response_conflict")
    assert (
        store.respond(identifier, token, payload, known_camera=lambda _: True)["duplicate"] is True
    )


@pytest.mark.parametrize("kind,operation", [("neural", "respond"), ("analytics", "ingest")])
def test_process_storage_failure_rolls_back_partial_admission(identity, store, kind, operation):
    identifier, token, camera, payload = _setup(identity, store, kind)
    # Install after handshake; fail after the journal write, before the health update.
    with _process(identity, identifier, token, kind) as worker:
        store._db.execute(
            "CREATE TRIGGER reject_admission BEFORE UPDATE ON connections "
            "WHEN NEW.last_status='ready' BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END"
        )
        worker.send((operation, payload, None))
        assert _receive(worker) == "begin"
        result = _receive(worker)
        assert _receive(worker) == ("transaction", False)
    assert result == ("error", "OperatorIntegrationError", "integration_unavailable")
    assert store.events(camera_id=camera)["events"] == []
    assert (
        store._db.execute("SELECT count(*) FROM deliveries WHERE response IS NOT NULL").fetchone()[
            0
        ]
        == 0
    )
    store._db.execute("DROP TRIGGER reject_admission")
    store.handshake(identifier, token, ["2" if kind == "neural" else "0.1"])
    result = getattr(store, operation)(identifier, token, payload, known_camera=lambda _: True)
    assert result["accepted"] is True and result["duplicate"] is False


@pytest.mark.parametrize("invalid", ["correlation", "authority", "camera"])
def test_process_response_validation_fails_closed_and_releases_transaction(
    identity, store, invalid
):
    identifier, token, _, payload = _setup(identity, store, "neural")
    if invalid == "correlation":
        payload = payload | {"request": payload["request"] | {"partition": "foreign"}}
    elif invalid == "authority":
        payload = payload | {"response": payload["response"] | {"authorized": True}}
    else:
        # Keep the delivery so that current scope, rather than correlation, rejects it.
        store._db.execute("UPDATE connections SET grants='[]' WHERE id=?", (str(identifier),))
    with _process(identity, identifier, token) as worker:
        worker.send(("respond", payload, None))
        assert _receive(worker) == "begin"
        result = _receive(worker)
        assert _receive(worker) == ("transaction", False)
    assert result[0] == "error"
    assert result[2] in {
        "integration_correlation_mismatch",
        "integration_invalid_response",
        "integration_camera_forbidden",
    }
    assert (
        store._db.execute("SELECT count(*) FROM deliveries WHERE response IS NOT NULL").fetchone()[
            0
        ]
        == 0
    )
