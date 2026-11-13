"""A completed policy update may refresh authority; denial never becomes a grant."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode
from uuid import UUID

import httpx
import pytest
import test_operator_product_api as helpers

from k5vision.domain.users import UserCreate, UserPatch, UserRole
from k5vision.media.mediamtx_provider import MediaMTXBinding
from k5vision.operator_role_policy import PERMISSIONS, RolePolicyError
from k5vision.operator_role_policy_api import current_request_permission

product = helpers.product


async def _entered(predicate):
    deadline = asyncio.get_running_loop().time() + 2
    while not predicate() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.005)
    assert predicate(), "Controlled authority boundary did not start"


def _media_request(product, camera, *, live=False):
    path = MediaMTXBinding(product.service.site_id, UUID(camera)).path_name
    uri = (
        f"/media/live/{path}/index.m3u8"
        if live
        else "/media/playback?"
        + urlencode(
            {"path": path, "start": "2026-11-13T00:00:00Z", "duration": 120, "format": "fmp4"}
        )
    )
    return "/api/v1/operator/media-auth", product.media_headers(uri)


def _write_unrelated(product, index=0):
    policy = product.app.state.operator_roles
    return policy.upsert(
        product.users,
        {
            "name": f"Unused concurrent role {index}",
            "description": "",
            "base_role": "operator",
            "permissions": ["cameras.view_live", "cameras.playback"],
        },
    )


def _backup(product):
    account = product.users.create(
        UserCreate(username="refresh-backup", display_name="Backup", role=UserRole.ADMINISTRATOR),
        actor="test",
    )
    helpers._initialized_session(product, account)


@pytest.mark.parametrize("product", [{"roles": True}], indirect=True)
@pytest.mark.parametrize("route", ["history", "playback", "live"])
def test_completed_unrelated_role_write_refreshes_forwarded_media_authority(
    product, monkeypatch, route
):
    camera = product.enroll()
    policy = product.app.state.operator_roles
    original = policy.permission_snapshot
    first, release = threading.Event(), threading.Event()
    snapshots, accounts, credentials = [], [], []
    original_account, original_credential = product.users.get, product.users.credential_status

    def account(identifier):
        accounts.append(identifier)
        return original_account(identifier)

    def credential(identifier):
        credentials.append(identifier)
        return original_credential(identifier)

    def snapshot(account):
        value = original(account)
        snapshots.append(current_request_permission())
        if len(snapshots) == 1:
            first.set()
            assert release.wait(5)
        return value

    monkeypatch.setattr(product.users, "get", account)
    monkeypatch.setattr(product.users, "credential_status", credential)
    monkeypatch.setattr(policy, "permission_snapshot", snapshot)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=product.app), base_url="http://127.0.0.1:8080"
        ) as client:
            path, headers = (
                (f"/api/v1/operator/cameras/{camera}/history", product.headers)
                if route == "history"
                else _media_request(product, camera, live=route == "live")
            )
            pending = asyncio.create_task(client.get(path, headers=headers))
            try:
                await _entered(first.is_set)
                _write_unrelated(product)
                release.set()
                assert (await asyncio.wait_for(pending, 2)).status_code == 200
                expected = "cameras.view_live" if route == "live" else "cameras.playback"
                assert snapshots and set(snapshots) == {expected}
                # Fresh durable account and credential checks follow every
                # policy read, including the additional admission attempt.
                assert len(snapshots) >= 3
                assert len(accounts) >= 2 * len(snapshots)
                assert len(credentials) >= 2 * len(snapshots)
            finally:
                release.set()
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    asyncio.run(exercise())


@pytest.mark.parametrize("product", [{"roles": True}], indirect=True)
def test_completed_update_during_final_session_await_restarts_from_current_session(
    product, monkeypatch
):
    camera = product.enroll()
    original = product.sessions.resolve
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def held(token, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
            await release.wait()
        return await original(token, **kwargs)

    monkeypatch.setattr(product.sessions, "resolve", held)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=product.app), base_url="http://127.0.0.1:8080"
        ) as client:
            path, headers = _media_request(product, camera)
            pending = asyncio.create_task(client.get(path, headers=headers))
            try:
                await _entered(entered.is_set)
                _write_unrelated(product)
                release.set()
                assert (await asyncio.wait_for(pending, 2)).status_code == 200
                assert calls >= 6  # Initial, refreshed and route-final session admissions.
            finally:
                release.set()
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    asyncio.run(exercise())


@pytest.mark.parametrize("product", [{"roles": True}], indirect=True)
def test_continuous_completed_policy_changes_stop_at_three_fresh_attempts(product, monkeypatch):
    camera = product.enroll()
    policy, calls = product.app.state.operator_roles, []
    original = policy.permission_snapshot

    def changing(account):
        value = original(account)
        calls.append(True)
        _write_unrelated(product, len(calls))
        return value

    monkeypatch.setattr(policy, "permission_snapshot", changing)
    path, headers = _media_request(product, camera)
    response = product.client.get(path, headers=headers)
    assert response.status_code == 503 and response.json()["detail"] == "roles_unavailable"
    assert len(calls) == 3
    assert not product.service.provider.history_queries


@pytest.mark.parametrize("product", [{"roles": True}], indirect=True)
@pytest.mark.parametrize("change", ["close", "replacement", "failed_write", "malformed"])
def test_unavailable_or_replaced_policy_does_not_trigger_fresh_attempt(
    product, monkeypatch, change
):
    camera = product.enroll()
    policy, calls = product.app.state.operator_roles, []
    original = policy.permission_snapshot

    def unavailable(account):
        calls.append(True)
        if change == "malformed":
            raise RolePolicyError("roles_unavailable")
        value = original(account)
        if change == "close":
            policy.close()
        elif change == "replacement":
            product.app.state.operator_roles = SimpleNamespace()
        else:
            import k5vision.operator_role_policy as module

            original_open = module.os.open

            def failed(path, *args, **kwargs):
                if Path(path).name.startswith(".roles-"):
                    raise OSError("Generated refused write")
                return original_open(path, *args, **kwargs)

            with monkeypatch.context() as patch:
                patch.setattr(module.os, "open", failed)
                with pytest.raises(OSError, match="Generated refused write"):
                    policy._write(policy._read())
        return value

    monkeypatch.setattr(policy, "permission_snapshot", unavailable)
    path, headers = _media_request(product, camera)
    assert product.client.get(path, headers=headers).status_code == 503
    assert len(calls) == 1


@pytest.mark.parametrize("product", [{"roles": True}], indirect=True)
@pytest.mark.parametrize("change", ["logout", "disable", "base_role", "password", "permission"])
def test_authority_loss_during_refreshed_read_still_denies_media(product, monkeypatch, change):
    camera = product.enroll()
    _backup(product)
    policy = product.app.state.operator_roles
    role_value = {
        "name": "Refresh reader",
        "description": "",
        "base_role": "administrator",
        "permissions": sorted(PERMISSIONS),
    }
    role = policy.upsert(product.users, role_value)
    policy.assign(product.users, product.admin.id, role["id"])
    original = policy.permission_snapshot
    entered = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    calls = []

    def held(account):
        value = original(account)
        calls.append(True)
        if len(calls) <= 2:
            entered[len(calls) - 1].set()
            assert release[len(calls) - 1].wait(5)
        return value

    monkeypatch.setattr(policy, "permission_snapshot", held)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=product.app), base_url="http://127.0.0.1:8080"
        ) as client:
            path, headers = _media_request(product, camera)
            pending = asyncio.create_task(client.get(path, headers=headers))
            try:
                await _entered(entered[0].is_set)
                _write_unrelated(product)
                release[0].set()
                await _entered(entered[1].is_set)
                if change == "logout":
                    await product.sessions.revoke(product.token)
                elif change == "disable":
                    product.users.update(product.admin.id, UserPatch(enabled=False), actor="test")
                elif change == "base_role":
                    product.users.update(
                        product.admin.id, UserPatch(role=UserRole.VIEWER), actor="test"
                    )
                elif change == "password":
                    product.users.reset_bootstrap(product.admin.id, actor="test")
                else:
                    policy.upsert(
                        product.users,
                        {**role_value, "permissions": sorted(PERMISSIONS - {"cameras.playback"})},
                        role_id=role["id"],
                    )
                release[1].set()
                response = await asyncio.wait_for(pending, 2)
                assert response.status_code == (403 if change == "permission" else 401)
                assert 2 <= len(calls) <= 3
            finally:
                for event in release:
                    event.set()
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    asyncio.run(exercise())


@pytest.mark.parametrize("product", [{"roles": True}], indirect=True)
def test_cancelled_refresh_keeps_owned_read_until_drain_without_another_attempt(
    product, monkeypatch
):
    camera = product.enroll()
    policy = product.app.state.operator_roles
    original = policy.permission_snapshot
    entered = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    calls = []

    def held(account):
        value = original(account)
        calls.append(True)
        if len(calls) <= 2:
            entered[len(calls) - 1].set()
            assert release[len(calls) - 1].wait(5)
        return value

    monkeypatch.setattr(policy, "permission_snapshot", held)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=product.app), base_url="http://127.0.0.1:8080"
        ) as client:
            path, headers = _media_request(product, camera)
            pending = asyncio.create_task(client.get(path, headers=headers))
            try:
                await _entered(entered[0].is_set)
                _write_unrelated(product)
                release[0].set()
                await _entered(entered[1].is_set)
                pending.cancel()
                await asyncio.sleep(0)
                pending.cancel()
                await asyncio.sleep(0.025)
                assert not pending.done() and len(calls) == 2
                release[1].set()
                with pytest.raises(asyncio.CancelledError):
                    await pending
                assert len(calls) == 2
            finally:
                for event in release:
                    event.set()
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    asyncio.run(exercise())
