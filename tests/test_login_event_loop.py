"""Password verification must not starve the installed operator HTTP loop."""

from __future__ import annotations

import asyncio
import threading
from time import monotonic

import httpx

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
        assert password == "generated-password"
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
