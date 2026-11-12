"""Synchronous account authority must not stall other sessions or revive revoked grants."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from k5vision.auth_sessions import UserSessionManager
from k5vision.domain.users import UserAccount, UserCreate, UserRole
from k5vision.main import create_app


def _account(username: str) -> UserAccount:
    return UserAccount(username=username, display_name="Generated account", role=UserRole.VIEWER)


@pytest.fixture(params=["shared", "login"])
def authority_executor(request):
    if request.param == "shared":
        yield None
    else:
        with ThreadPoolExecutor(max_workers=2) as executor:
            yield executor


def test_blocked_registry_read_does_not_hold_session_lock(authority_executor):
    account = _account("generated-viewer")
    other = _account("generated-operator")
    version = b"v" * 32
    entered, release = threading.Event(), threading.Event()

    def slow_authority(current, supplied_version):
        if current.id == other.id:
            return current
        assert current.id == account.id and supplied_version == version
        entered.set()
        assert release.wait(timeout=4)
        return current

    manager = UserSessionManager(resolve_account=slow_authority)

    async def exercise():
        token, _ = await manager.issue(account, credential_version=version)
        reading = asyncio.create_task(manager.resolve(token, executor=authority_executor))
        try:
            assert await asyncio.to_thread(entered.wait, 1.5)
            # Both actions must complete while the registry reader remains
            # blocked, without permitting the old authorization to return.
            await asyncio.wait_for(manager.revoke_user(account.id), timeout=1.5)
            other_token, _ = await asyncio.wait_for(manager.issue(other), timeout=1.5)
            assert (
                await asyncio.wait_for(
                    manager.resolve(other_token, executor=authority_executor), timeout=1.5
                )
                == other
            )
        finally:
            release.set()
        assert await asyncio.wait_for(reading, timeout=2) is None

    try:
        asyncio.run(exercise())
    finally:
        release.set()


def test_account_relogin_cannot_revive_older_inflight_resolution(authority_executor):
    account = _account("generated-viewer")
    entered, release = threading.Event(), threading.Event()
    version = b"a" * 32

    def slow_authority(current, supplied_version):
        assert supplied_version in {b"a" * 32, b"b" * 32}
        if supplied_version == version:
            entered.set()
            assert release.wait(timeout=4)
        return current

    manager = UserSessionManager(resolve_account=slow_authority)

    async def exercise():
        old_token, _ = await manager.issue(account, credential_version=version)
        older = asyncio.create_task(manager.resolve(old_token, executor=authority_executor))
        try:
            assert await asyncio.to_thread(entered.wait, 1.5)
            new_token, _ = await asyncio.wait_for(
                manager.issue(account, credential_version=b"b" * 32), timeout=1.5
            )
        finally:
            release.set()
        assert await asyncio.wait_for(older, timeout=2) is None
        assert (
            await asyncio.wait_for(
                manager.resolve(new_token, executor=authority_executor), timeout=2
            )
            == account
        )

    try:
        asyncio.run(exercise())
    finally:
        release.set()


def test_password_login_does_not_share_saturated_camera_authority_slots(tmp_path):
    app = create_app(
        control_plane_site_id="generated-session-slot-site",
        user_db_path=tmp_path / "users.sqlite3",
        device_db_path=tmp_path / "devices.sqlite3",
    )
    registry = app.state.user_registry
    assert registry is not None
    username, password = "generated-owner", "generated login slot isolation password"
    registry.bootstrap_administrator(
        UserCreate(username=username, display_name="Generated owner", role=UserRole.ADMINISTRATOR),
        password=password,
    )
    verified = registry.verify_password_snapshot(username=username, password=password)
    assert verified is not None
    account, version = verified
    manager = app.state.user_session_manager
    release = threading.Event()

    async def exercise():
        loop = asyncio.get_running_loop()
        entered, queued = asyncio.Event(), asyncio.Event()
        shared = ThreadPoolExecutor(max_workers=1)
        loop.set_default_executor(shared)

        def occupy_default_worker():
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(timeout=8)

        blocked = loop.run_in_executor(None, occupy_default_worker)
        submitted = 0
        original_submit = shared.submit

        def observe_submission(*args, **kwargs):
            nonlocal submitted
            future = original_submit(*args, **kwargs)
            submitted += 1
            if submitted == 4:
                queued.set()
            return future

        shared.submit = observe_submission
        readers = []
        try:
            async with app.router.lifespan_context(app):
                token, _ = await manager.issue(account, credential_version=version)
                await asyncio.wait_for(entered.wait(), timeout=1.5)
                readers = [asyncio.create_task(manager.resolve(token)) for _ in range(4)]
                try:
                    # All ordinary authority slots now own reads queued behind
                    # unrelated disk/media work. Login must retain its own
                    # bounded durable-authority admission, not skip the read.
                    await asyncio.wait_for(queued.wait(), timeout=1.5)
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
                    ) as client:
                        issued = await asyncio.wait_for(
                            client.post(
                                "/api/v1/auth/login",
                                json={"username": username, "password": password},
                            ),
                            timeout=2,
                        )
                    assert issued.status_code == 200
                    assert issued.json()["account"]["id"] == str(account.id)
                    assert issued.json()["session_token"] != token
                    assert not blocked.done()
                finally:
                    release.set()
                    await blocked
                    assert await asyncio.gather(*readers) == [None] * 4
        finally:
            release.set()
            await blocked
            shared.shutdown(wait=True)

    asyncio.run(exercise())


def test_cancelled_login_authority_read_retains_capacity_until_worker_finishes():
    release = threading.Event()
    accounts = [_account(f"generated-slot-{index}") for index in range(3)]

    async def exercise():
        loop = asyncio.get_running_loop()
        entered = [asyncio.Event() for _ in accounts]

        def authority(current, version):
            index = next(
                index for index, account in enumerate(accounts) if account.id == current.id
            )
            loop.call_soon_threadsafe(entered[index].set)
            assert release.wait(timeout=5)
            return current

        manager = UserSessionManager(resolve_account=authority)
        tokens = [(await manager.issue(account))[0] for account in accounts]
        # A third worker is available: the manager, not executor saturation,
        # must retain the cancelled read's two-call admission until it ends.
        with ThreadPoolExecutor(max_workers=3) as executor:
            reading = [
                asyncio.create_task(manager.resolve(token, executor=executor))
                for token in tokens[:2]
            ]
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(event.wait() for event in entered[:2])), timeout=1.5
                )
                reading[0].cancel()
                await asyncio.sleep(0)
                assert not reading[0].done()
                reading.append(asyncio.create_task(manager.resolve(tokens[2], executor=executor)))
                await asyncio.sleep(0)
                assert not entered[2].is_set()
                await manager.revoke(tokens[1])
            finally:
                release.set()
                results = await asyncio.gather(*reading, return_exceptions=True)
            assert isinstance(results[0], asyncio.CancelledError)
            assert results[1] is None
            assert results[2] == accounts[2]

    try:
        asyncio.run(exercise())
    finally:
        release.set()
