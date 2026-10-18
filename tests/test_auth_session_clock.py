"""Human login lifetime follows elapsed time through host-clock corrections."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from k5vision import auth_sessions
from k5vision.domain.users import UserAccount, UserRole


@pytest.fixture
def clocks(monkeypatch):
    state = {"wall": datetime(2026, 10, 18, 12, tzinfo=UTC), "steady": 1000.0}

    class WallClock:
        @staticmethod
        def now(_timezone):
            return state["wall"]

    monkeypatch.setattr(auth_sessions, "datetime", WallClock)
    monkeypatch.setattr(auth_sessions, "monotonic", lambda: state["steady"])
    return state


@pytest.fixture
def account():
    return UserAccount(
        username="clock-admin", display_name="Clock admin", role=UserRole.ADMINISTRATOR
    )


@pytest.mark.parametrize("wall_step", [timedelta(days=-366), timedelta(days=366)])
def test_wall_correction_cannot_shorten_or_extend_elapsed_login(clocks, account, wall_step):
    async def lifetime():
        manager = auth_sessions.UserSessionManager(ttl_seconds=300)
        issued_wall = clocks["wall"]
        token, display_expiry = await manager.issue(account)
        assert display_expiry == issued_wall + timedelta(seconds=300)
        assert manager.ttl_seconds == 300

        clocks["wall"] += wall_step
        clocks["steady"] = 1299.999
        assert await manager.resolve(token) == account
        clocks["steady"] = 1300.0
        assert await manager.resolve(token) is None
        # Even another backward correction cannot revive an elapsed session.
        clocks["wall"] -= timedelta(days=732)
        assert await manager.resolve(token) is None

    asyncio.run(lifetime())


def test_reissue_starts_new_elapsed_lifetime_after_wall_step(clocks, account):
    async def lifetime():
        manager = auth_sessions.UserSessionManager(ttl_seconds=300)
        first, _ = await manager.issue(account)
        clocks["steady"] = 1100.0
        clocks["wall"] -= timedelta(days=366)
        second, display_expiry = await manager.issue(account)
        assert second != first
        assert display_expiry == clocks["wall"] + timedelta(seconds=300)
        assert await manager.resolve(first) is None

        clocks["wall"] += timedelta(days=732)
        clocks["steady"] = 1300.0  # The first session's old deadline.
        assert await manager.resolve(second) == account
        clocks["steady"] = 1399.999
        assert await manager.resolve(second) == account
        clocks["steady"] = 1400.0
        assert await manager.resolve(second) is None

    asyncio.run(lifetime())


def test_explicit_revocation_remains_immediate_during_clock_correction(clocks, account):
    async def lifetime():
        manager = auth_sessions.UserSessionManager(ttl_seconds=300)
        token, _ = await manager.issue(account)
        clocks["wall"] -= timedelta(days=366)
        assert await manager.revoke(token) is True
        assert await manager.resolve(token) is None
        assert await manager.revoke(token) is False

        token, _ = await manager.issue(account)
        clocks["wall"] += timedelta(days=732)
        await manager.revoke_user(account.id)
        assert await manager.resolve(token) is None

    asyncio.run(lifetime())


@pytest.mark.parametrize("ttl", [299, 86401, True, 300.0])
def test_advertised_session_duration_must_be_a_bounded_integer(ttl):
    with pytest.raises(ValueError):
        auth_sessions.UserSessionManager(ttl_seconds=ttl)
