"""Password verification must not starve the installed operator HTTP loop."""

from __future__ import annotations

import asyncio
import threading
from time import monotonic

import httpx
import pytest

from k5vision.domain.users import UserCreate, UserRole
from k5vision.main import create_app


def test_slow_password_verification_keeps_health_responsive(monkeypatch, tmp_path):
    # All identities are generated. No real home camera, token or source URI
    # participates in this acceptance boundary.
    for name in (
        "K5_CONTROL_PLANE_TOKEN",
        "K5_CONTROL_PLANE_READ_TOKEN",
        "K5_CONTROL_PLANE_ADMIN_TOKEN",
        "K5_USER_DB_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    app = create_app(
        control_plane_site_id="generated-login-site",
        user_db_path=tmp_path / "users.sqlite3",
        device_db_path=tmp_path / "devices.sqlite3",
    )
    registry = app.state.user_registry
    assert registry is not None
    started = threading.Event()
    release = threading.Event()

    def slow_verification(*, username, password):
        assert username == "generated-operator"
        assert isinstance(password, str) and 1 <= len(password) <= 256
        started.set()
        assert release.wait(timeout=4.0)
        # Unknown account still fails closed after the expensive verification.
        return None

    monkeypatch.setattr(registry, "verify_password_snapshot", slow_verification)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            began = monotonic()
            login = asyncio.create_task(
                client.post(
                    "/api/v1/auth/login",
                    json={"username": "generated-operator", "password": "generated-password"},
                )
            )
            try:
                assert await asyncio.to_thread(started.wait, 1.0)
                assert monotonic() - began < 1.5, "Login monopolized the operator event loop"
                health = await asyncio.wait_for(client.get("/api/v1/health"), timeout=1.0)
                assert health.status_code == 200
                assert health.json()["status"] == "ok"
                assert not login.done(), "The slow password verifier did not remain in flight"
            finally:
                release.set()
            assert (await login).status_code == 401

    try:
        asyncio.run(exercise())
    finally:
        release.set()
        registry.close()
        app.state.device_registry.close()


@pytest.mark.parametrize("slow_phase", ["primary_proof", "issued_session_proof"])
def test_slow_security_authority_checks_do_not_block_http_health(monkeypatch, tmp_path, slow_phase):
    """MFA and durable post-login checks must remain enforced without blocking HTTP."""
    for name in (
        "K5_CONTROL_PLANE_TOKEN",
        "K5_CONTROL_PLANE_READ_TOKEN",
        "K5_CONTROL_PLANE_ADMIN_TOKEN",
        "K5_USER_DB_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    app = create_app(
        control_plane_site_id="generated-security-site",
        user_db_path=tmp_path / "users.sqlite3",
        device_db_path=tmp_path / "devices.sqlite3",
    )
    registry = app.state.user_registry
    assert registry is not None
    username, password = "generated-admin", "generated-local-test-password"
    account, temporary, _ = registry.create_with_bootstrap(
        UserCreate(
            username=username,
            display_name="Generated administrator",
            role=UserRole.ADMINISTRATOR,
            enabled=True,
        ),
        actor="setup-admin",
    )
    assert (
        registry.activate_bootstrap_password(
            username=username,
            temporary_credential=temporary,
            new_password=password,
            actor="bootstrap-user",
        )
        == account
    )

    started, release = threading.Event(), threading.Event()
    security_calls = []

    def block_worker():
        started.set()
        assert release.wait(timeout=5.0)

    class SecurityStore:
        @staticmethod
        def status(account_id):
            assert account_id == account.id
            return {"mfa_enabled": False}

    class SecurityProof:
        store = SecurityStore()

        @staticmethod
        def primary_started():
            return 0

        @staticmethod
        def password_authenticated(current, version, *, started_epoch):
            assert current.id == account.id and isinstance(version, bytes)
            assert started_epoch == 0
            if slow_phase == "primary_proof":
                block_worker()
            security_calls.append("primary")
            return 0

        @staticmethod
        def validate_primary_proof(current, version, epoch):
            assert current.id == account.id and isinstance(version, bytes)
            assert epoch == 0
            if slow_phase == "issued_session_proof":
                block_worker()
            security_calls.append("issued")

    app.state.operator_identity_security = SecurityProof()

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            login = asyncio.create_task(
                client.post(
                    "/api/v1/auth/login",
                    json={"username": username, "password": password},
                )
            )
            try:
                assert await asyncio.to_thread(started.wait, 2.5)
                health = await asyncio.wait_for(client.get("/api/v1/health"), timeout=1.0)
                assert health.status_code == 200
                assert health.json()["status"] == "ok"
                assert not login.done(), "The security authority check was not still in flight"
            finally:
                release.set()
            logged_in = await asyncio.wait_for(login, timeout=3.0)
            assert logged_in.status_code == 200
            assert logged_in.json()["account"]["id"] == str(account.id)

    try:
        asyncio.run(exercise())
        assert security_calls == ["primary", "issued"]
    finally:
        release.set()
        registry.close()
        app.state.device_registry.close()
