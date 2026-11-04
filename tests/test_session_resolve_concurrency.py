"""Synchronous account authority must not stall other sessions or revive revoked grants."""

from __future__ import annotations

import asyncio
import threading

from k5vision.auth_sessions import UserSessionManager
from k5vision.domain.users import UserAccount, UserRole


def _account(username: str) -> UserAccount:
    return UserAccount(username=username, display_name="Generated account", role=UserRole.VIEWER)


def test_blocked_registry_read_does_not_hold_session_lock():
    account = _account("generated-viewer")
    other = _account("generated-operator")
    version = b"v" * 32
    entered, release = threading.Event(), threading.Event()

    def slow_authority(current, supplied_version):
        assert current.id == account.id and supplied_version == version
        entered.set()
        assert release.wait(timeout=4)
        return current

    manager = UserSessionManager(resolve_account=slow_authority)

    async def exercise():
        token, _ = await manager.issue(account, credential_version=version)
        reading = asyncio.create_task(manager.resolve(token))
        try:
            assert await asyncio.to_thread(entered.wait, 1.5)
            # Both actions must complete while the registry reader remains
            # blocked, without permitting the old authorization to return.
            await asyncio.wait_for(manager.revoke_user(account.id), timeout=1.5)
            other_token, _ = await asyncio.wait_for(manager.issue(other), timeout=1.5)
            assert await asyncio.wait_for(manager.resolve(other_token), timeout=1.5) == other
        finally:
            release.set()
        assert await asyncio.wait_for(reading, timeout=2) is None

    try:
        asyncio.run(exercise())
    finally:
        release.set()


def test_account_relogin_cannot_revive_older_inflight_resolution():
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
        older = asyncio.create_task(manager.resolve(old_token))
        try:
            assert await asyncio.to_thread(entered.wait, 1.5)
            new_token, _ = await asyncio.wait_for(
                manager.issue(account, credential_version=b"b" * 32), timeout=1.5
            )
        finally:
            release.set()
        assert await asyncio.wait_for(older, timeout=2) is None
        assert await asyncio.wait_for(manager.resolve(new_token), timeout=2) == account

    try:
        asyncio.run(exercise())
    finally:
        release.set()
