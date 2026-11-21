"""A held primary-proof read cannot delay synchronous session invalidation."""

from __future__ import annotations

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock, Thread, current_thread
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from test_operator_identity_security import identity as identity  # noqa: F401

from k5vision.main import create_app
from k5vision.operator_identity_security import (
    AccountSecurityError,
    AccountSecurityService,
    AccountSecurityStore,
    SecondFactorRequired,
    totp,
)


@pytest.fixture
def runtime(identity):
    app = create_app(
        control_plane_site_id=identity.site_id,
        device_db_path=identity.directory / "devices.sqlite3",
        user_db_path=identity.database_path,
    )
    users = app.state.user_registry
    clock = SimpleNamespace(utc=1_800_000_000.0)
    store = AccountSecurityStore(identity.directory, identity.site_id, utc=lambda: clock.utc)
    service = AccountSecurityService(store=store, users=users, utc=lambda: clock.utc)
    sessions = app.state.user_session_manager
    service.bind_sessions(sessions)
    account, version = users.verify_password_snapshot(
        username="security-admin", password="security-test-password"
    )
    yield SimpleNamespace(
        app=app,
        users=users,
        store=store,
        service=service,
        sessions=sessions,
        account=account,
        version=version,
        clock=clock,
    )
    service.close()
    app.state.user_login_executor.shutdown(wait=True, cancel_futures=True)
    users.close()
    app.state.device_registry.close()


def _pending_factor(runtime):
    service, account = runtime.service, runtime.account
    enrollment = service.begin_enrollment(account, "security-test-password")
    secret = base64.b32decode(enrollment["secret"])
    recovery = service.confirm_enrollment(
        account, enrollment["enrollment_token"], totp(secret, int(runtime.clock.utc // 30))
    )
    with pytest.raises(SecondFactorRequired) as challenge:
        service.password_authenticated(account, runtime.version)
    return challenge.value.challenge["challenge_token"], recovery[0]


def _exercise(runtime, monkeypatch, *, phase, revoked_check=lambda: None, revoke=True):
    entered, released, phase_started, progress = Event(), Event(), Event(), Event()
    revoked, healthy = Event(), Event()
    trace, trace_lock, thread_errors = [], Lock(), []
    service, account, version = runtime.service, runtime.account, runtime.version
    started_epoch = service.primary_started()

    def mark(value):
        with trace_lock:
            trace.append(value)

    def hold_read():
        mark("read_entered")
        entered.set()
        assert released.wait(5), "Independent read release did not complete"
        mark("read_resumed")

    def authority_check():
        hold_read()
        return True

    if phase != "external_authority":
        target = runtime.store if phase == "password_authenticated" else runtime.users
        name = "status" if phase == "password_authenticated" else "resolve_session"
        original = getattr(target, name)

        def held_read(*args, **kwargs):
            if current_thread().name.startswith("security-proof"):
                hold_read()
            return original(*args, **kwargs)

        monkeypatch.setattr(target, name, held_read)

    def proof():
        if phase == "password_authenticated":
            return service.password_authenticated(account, version, started_epoch=started_epoch)
        if phase == "primary_current":
            return service.primary_current(account, version, started_epoch)
        return service.validate_primary_proof(
            account,
            version,
            started_epoch,
            authority_check if phase == "external_authority" else None,
        )

    def fallback_release():
        try:
            assert phase_started.wait(3), "Async proof phase did not start"
            # A responsive loop releases the read through progress. This wait
            # is only an independent rescue bound for the defective version.
            progress.wait(1)
            mark("read_release")
        except BaseException as error:
            thread_errors.append(type(error).__name__)
        finally:
            released.set()

    async def run():
        token, _ = await runtime.sessions.issue(account, credential_version=version)
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="security-proof")
        fallback = Thread(target=fallback_release, name="security-proof-release", daemon=True)
        worker = None
        try:
            worker = asyncio.get_running_loop().run_in_executor(pool, proof)
            assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 2), timeout=3)
            fallback.start()

            def check_progress():
                if revoked.is_set() and healthy.is_set():
                    progress.set()

            async def revoke_session():
                mark("revoke_started")
                phase_started.set()
                if revoke:
                    assert await runtime.sessions.revoke(token)
                    # Revocation must have cleared pending state before it
                    # returns, while the primary-proof read is still held.
                    assert await runtime.sessions.resolve(token) is None
                    assert service.primary_started() > started_epoch
                    revoked_check()
                mark("revoke_completed")
                revoked.set()
                check_progress()

            async def health_request():
                async with AsyncClient(
                    transport=ASGITransport(app=runtime.app),
                    base_url="http://testserver",
                    trust_env=False,
                ) as client:
                    response = await client.get("/api/v1/health")
                assert response.status_code == 200
                assert response.json()["status"] == "ok"
                mark("health_completed")
                healthy.set()
                check_progress()

            await asyncio.gather(revoke_session(), health_request())
            try:
                await worker
            except SecondFactorRequired:
                outcome = "challenge_issued"
            except AccountSecurityError:
                outcome = "refused"
            else:
                outcome = "accepted"
            fallback.join(timeout=2)
            assert not fallback.is_alive() and not thread_errors
            assert trace.index("revoke_completed") < trace.index("read_release"), trace
            assert trace.index("health_completed") < trace.index("read_release"), trace
            assert outcome == ("refused" if revoke else "accepted")
        finally:
            released.set()
            if worker is not None:
                await asyncio.gather(worker, return_exceptions=True)
            pool.shutdown(wait=True, cancel_futures=True)
            if fallback.is_alive():
                fallback.join(timeout=2)

    asyncio.run(run())


@pytest.mark.parametrize(
    "phase",
    ["password_authenticated", "primary_current", "validate_primary_proof", "external_authority"],
)
def test_revocation_and_http_health_progress_before_primary_read_is_released(
    runtime, monkeypatch, phase
):
    _exercise(runtime, monkeypatch, phase=phase)


@pytest.mark.parametrize("pending", ["factor", "enrollment"])
def test_revocation_clears_pending_authentication_before_held_primary_read_returns(
    runtime, monkeypatch, pending
):
    if pending == "factor":
        token, code = _pending_factor(runtime)

        def revoked_check():
            with pytest.raises(AccountSecurityError, match="expired"):
                runtime.service.complete_challenge(token, code)

    else:
        enrollment = runtime.service.begin_enrollment(runtime.account, "security-test-password")
        secret = base64.b32decode(enrollment["secret"])
        code = totp(secret, int(runtime.clock.utc // 30))

        def revoked_check():
            with pytest.raises(AccountSecurityError, match="expired"):
                runtime.service.confirm_enrollment(
                    runtime.account, enrollment["enrollment_token"], code
                )

    _exercise(runtime, monkeypatch, phase="password_authenticated", revoked_check=revoked_check)


def test_held_primary_read_without_revocation_keeps_http_health_responsive(runtime, monkeypatch):
    _exercise(runtime, monkeypatch, phase="password_authenticated", revoke=False)
