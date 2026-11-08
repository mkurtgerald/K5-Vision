"""Readiness suspension cannot retain revoked authority or extend its deadline."""

import asyncio
import threading
from datetime import UTC, datetime
from urllib.parse import urlencode
from uuid import UUID

import pytest
import test_operator_product_api as helpers
from fastapi import HTTPException

from k5vision.domain.users import UserCreate, UserPatch, UserRole
from k5vision.media.mediamtx_provider import MediaMTXBinding
from k5vision.operator_product_api import _await_media_ready

product = helpers.product


@pytest.mark.parametrize("product", [{"roles": True}], indirect=True)
@pytest.mark.parametrize("ready_probe", [1, 2])
@pytest.mark.parametrize("ready_status", ["ready", "disk_full"])
@pytest.mark.parametrize(
    ("route", "revocation"),
    [
        (route, kind)
        for route in ("live", "history", "mp4")
        for kind in ("session", "expiry", "user", "role", "camera")
    ]
    + [("mp4", "export")],
)
def test_readiness_probe_rechecks_current_authority(
    product, monkeypatch, ready_probe, ready_status, route, revocation
):
    identifier = product.enroll()
    account = product.users.create(
        UserCreate(username="readiness-reader", display_name="Reader", role=UserRole.OPERATOR),
        actor="test",
    )
    token, _ = helpers._initialized_session(product, account)
    policy = product.app.state.operator_roles
    role = policy.upsert(
        product.users,
        {
            "name": "Readiness reader",
            "description": "Readiness authority fixture",
            "base_role": "operator",
            "permissions": ["cameras.view_live", "cameras.playback", "cameras.export"],
        },
    )
    policy.assign(product.users, account.id, role["id"])
    calls = 0

    def status_after_authority_change():
        nonlocal calls
        calls += 1
        if calls != ready_probe:
            return "unavailable"
        if revocation == "session":
            asyncio.run(product.sessions.revoke(token))
        elif revocation == "expiry":
            from k5vision.auth_sessions import monotonic

            expired = monotonic() + 86400
            monkeypatch.setattr("k5vision.auth_sessions.monotonic", lambda: expired)
        elif revocation == "user":
            product.users.update(account.id, UserPatch(enabled=False), actor="test")
        elif revocation == "role":
            product.users.update(account.id, UserPatch(role=UserRole.VIEWER), actor="test")
        elif revocation == "camera":
            product.users.set_camera_access(
                account.id, mode="selected", camera_ids=[], actor="test"
            )
        else:
            policy.upsert(
                product.users,
                {
                    "name": role["name"],
                    "description": role["description"],
                    "base_role": role["base_role"],
                    "permissions": ["cameras.view_live", "cameras.playback"],
                },
                role_id=role["id"],
            )
        return ready_status

    monkeypatch.setattr(product.service, "status", status_after_authority_change)
    if route == "history":
        response = product.client.get(
            f"/api/v1/operator/cameras/{identifier}/history",
            headers={"Authorization": "Bearer " + token},
        )
    else:
        path = MediaMTXBinding(product.service.site_id, UUID(identifier)).path_name
        uri = f"/media/live/{path}/index.m3u8"
        if route == "mp4":
            uri = "/media/playback?" + urlencode(
                {
                    "path": path,
                    "start": datetime.now(UTC).isoformat(),
                    "duration": 2,
                    "format": "mp4",
                }
            )
        response = product.client.get(
            "/api/v1/operator/media-auth", headers=product.media_headers(uri, token=token)
        )
    expected = 403 if revocation == "export" else 401
    if route == "history" and revocation == "camera":
        expected = 404
    assert response.status_code == expected
    assert calls == ready_probe
    assert not product.service.provider.history_queries


@pytest.mark.parametrize("ready_probe", [1, 2])
@pytest.mark.parametrize("ready_status", ["ready", "disk_full"])
def test_readiness_result_at_deadline_is_rejected(ready_probe, ready_status):
    elapsed = 0.0
    calls = 0

    def status():
        nonlocal elapsed, calls
        calls += 1
        if calls == ready_probe:
            elapsed = 2.0
            return ready_status
        return "unavailable"

    async def pause(seconds):
        nonlocal elapsed
        elapsed += seconds

    with pytest.raises(HTTPException) as failure:
        asyncio.run(_await_media_ready(status, now=lambda: elapsed, pause=pause))
    assert failure.value.status_code == 503
    assert calls == ready_probe


def test_readiness_does_not_start_another_probe_after_sleep_exhausts_budget():
    elapsed = 0.0
    calls = 0

    def status():
        nonlocal calls
        calls += 1
        return "unavailable" if calls == 1 else "ready"

    async def pause(_seconds):
        nonlocal elapsed
        elapsed = 2.1

    with pytest.raises(HTTPException) as failure:
        asyncio.run(_await_media_ready(status, now=lambda: elapsed, pause=pause))
    assert failure.value.status_code == 503
    assert calls == 1


@pytest.mark.parametrize("blocked_probe", [1, 2])
def test_readiness_timeout_does_not_wait_for_native_probe(monkeypatch, blocked_probe):
    monkeypatch.setattr("k5vision.operator_product_api._MEDIA_RECOVERY_GRACE_SECONDS", 1.0)
    monkeypatch.setattr("k5vision.operator_product_api._MEDIA_RECOVERY_POLL_SECONDS", 0.01)
    entered, released = threading.Event(), threading.Event()
    calls = 0

    def status():
        nonlocal calls
        calls += 1
        if calls != blocked_probe:
            return "unavailable"
        entered.set()
        assert released.wait(3), "Test did not release its native probe"
        return "ready"

    async def exercise():
        operation = asyncio.create_task(_await_media_ready(status))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            with pytest.raises(HTTPException) as failure:
                await asyncio.wait_for(operation, timeout=2)
            assert failure.value.status_code == 503
            assert not released.is_set()
            assert calls == blocked_probe
        finally:
            released.set()

    asyncio.run(exercise())


def test_readiness_cancellation_does_not_retry_or_grant():
    entered, released = threading.Event(), threading.Event()
    calls = 0

    def status():
        nonlocal calls
        calls += 1
        entered.set()
        assert released.wait(3), "Test did not release its native probe"
        return "ready"

    async def exercise():
        operation = asyncio.create_task(_await_media_ready(status))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            operation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await operation
        finally:
            released.set()

    asyncio.run(exercise())
    assert calls == 1
