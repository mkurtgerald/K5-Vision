"""Installed audit ordering must not put password login back on the media pool."""

from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from test_operator_audit import audit_identity, audit_store  # noqa: F401

from k5vision.domain.users import UserCreate, UserRole
from k5vision.main import (
    CONTROL_PLANE_SITE_ENV,
    CONTROL_PLANE_TOKEN_ENV,
    DEVICE_DB_PATH_ENV,
    create_app,
)
from k5vision.operator_audit_middleware import OperatorAuditMiddleware
from k5vision.user_admin_api import USER_ADMIN_TOKEN_ENV, USER_DB_PATH_ENV


@pytest.fixture
def audited_login(audit_store, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv(CONTROL_PLANE_SITE_ENV, audit_store.site_id)
    monkeypatch.setenv(CONTROL_PLANE_TOKEN_ENV, "generated-device-control-token")
    monkeypatch.setenv(DEVICE_DB_PATH_ENV, str(tmp_path / "devices.sqlite3"))
    monkeypatch.setenv(USER_ADMIN_TOKEN_ENV, "generated-user-administration-token")
    monkeypatch.setenv(USER_DB_PATH_ENV, str(tmp_path / "users.sqlite3"))
    app = create_app()
    account = app.state.user_registry.bootstrap_administrator(
        UserCreate(
            username="generated-owner",
            display_name="Generated owner",
            role=UserRole.ADMINISTRATOR,
        ),
        password="generated complete login password",
    )
    app.add_middleware(OperatorAuditMiddleware, store=audit_store)
    return SimpleNamespace(
        app=app,
        store=audit_store,
        account=account,
        payload={"username": account.username, "password": "generated complete login password"},
    )


def test_audited_password_login_and_denial_finish_while_default_worker_is_occupied(audited_login):
    async def exercise():
        loop = asyncio.get_running_loop()
        entered, released = asyncio.Event(), Event()

        def occupy_default_worker():
            loop.call_soon_threadsafe(entered.set)
            assert released.wait(12)

        shared_executor = ThreadPoolExecutor(max_workers=1)
        loop.set_default_executor(shared_executor)
        async with audited_login.app.router.lifespan_context(audited_login.app):
            blocked = loop.run_in_executor(None, occupy_default_worker)
            try:
                await asyncio.wait_for(entered.wait(), timeout=2)
                async with AsyncClient(
                    transport=ASGITransport(app=audited_login.app), base_url="http://testserver"
                ) as client:
                    health = await asyncio.wait_for(client.get("/api/v1/health"), timeout=2)
                    assert health.status_code == 200
                    issued = await asyncio.wait_for(
                        client.post("/api/v1/auth/login", json=audited_login.payload), timeout=4
                    )
                    assert issued.status_code == 200
                    assert issued.json()["session_token"]
                    assert issued.headers["Cache-Control"] == "no-store"
                    denied = await asyncio.wait_for(
                        client.post(
                            "/api/v1/auth/login",
                            json={**audited_login.payload, "password": "incorrect"},
                        ),
                        timeout=4,
                    )
                    assert denied.status_code == 401
                    assert not released.is_set() and not blocked.done()
                    rows = audited_login.store.events(action="auth.login")["events"]
                    assert [row["outcome"] for row in rows] == [
                        "denied",
                        "started",
                        "success",
                        "started",
                    ]
                    assert rows[2]["actor_id"] == str(audited_login.account.id)
                    assert rows[0]["actor_id"] is None
                    assert rows[0]["request_id"] == rows[1]["request_id"]
                    assert rows[2]["request_id"] == rows[3]["request_id"]
                    assert rows[0]["request_id"] != rows[2]["request_id"]
            finally:
                released.set()
                await blocked
                shared_executor.shutdown(wait=True)

    asyncio.run(exercise())


@pytest.mark.parametrize("refused_outcome", ["started", "success"])
def test_login_audit_failure_prevents_following_authentication(
    audited_login, monkeypatch, refused_outcome
):
    connection = sqlite3.connect(audited_login.store._path)
    connection.execute(
        "CREATE TRIGGER refuse_login_audit BEFORE INSERT ON audit_events "
        f"WHEN NEW.outcome='{refused_outcome}' "
        "BEGIN SELECT RAISE(ABORT,'generated-storage-failure'); END"
    )
    connection.close()
    registry = audited_login.app.state.user_registry
    original_verify = registry.verify_password_snapshot
    verifications = []

    def verify(**options):
        verifications.append(True)
        return original_verify(**options)

    monkeypatch.setattr(registry, "verify_password_snapshot", verify)

    async def exercise():
        async with audited_login.app.router.lifespan_context(audited_login.app):
            async with AsyncClient(
                transport=ASGITransport(app=audited_login.app), base_url="http://testserver"
            ) as client:
                first = await client.post("/api/v1/auth/login", json=audited_login.payload)
                second = await client.post("/api/v1/auth/login", json=audited_login.payload)
                assert first.status_code == (503 if refused_outcome == "started" else 200)
                assert second.status_code == 503
                assert second.json() == {"detail": "audit_unavailable"}
                assert len(verifications) == (0 if refused_outcome == "started" else 1)
                evidence = audited_login.store.events(action="auth.login")
                assert evidence["logging_available"] is False
                assert [row["outcome"] for row in evidence["events"]] == (
                    [] if refused_outcome == "started" else ["started"]
                )

    asyncio.run(exercise())


def test_cancelled_login_drains_completion_audit_before_shutdown(audited_login, monkeypatch):
    released, completed = Event(), Event()
    original_record = audited_login.store.record

    async def exercise():
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()

        def record(action, **options):
            if options.get("outcome") == "success":
                loop.call_soon_threadsafe(entered.set)
                assert released.wait(8)
                result = original_record(action, **options)
                completed.set()
                return result
            return original_record(action, **options)

        monkeypatch.setattr(audited_login.store, "record", record)
        async with audited_login.app.router.lifespan_context(audited_login.app):
            async with AsyncClient(
                transport=ASGITransport(app=audited_login.app), base_url="http://testserver"
            ) as client:
                request = asyncio.create_task(
                    client.post("/api/v1/auth/login", json=audited_login.payload)
                )
                try:
                    await asyncio.wait_for(entered.wait(), timeout=4)
                    for _ in range(2):
                        request.cancel()
                        await asyncio.sleep(0)
                    assert not request.done() and not completed.is_set()
                finally:
                    released.set()
                    with pytest.raises(asyncio.CancelledError):
                        await request
                assert completed.is_set()
        # Lifespan drains the owned login executor before closing the registry;
        # the cancelled request must leave no detached audit work for teardown.
        rows = audited_login.store.events(action="auth.login")["events"]
        assert [row["outcome"] for row in rows] == ["success", "started"]
        assert rows[0]["actor_id"] == str(audited_login.account.id)
        assert rows[0]["request_id"] == rows[1]["request_id"]

    try:
        asyncio.run(exercise())
    finally:
        released.set()
