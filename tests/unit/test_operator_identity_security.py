from __future__ import annotations

import asyncio
import base64
import os
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from k5vision import identity_state
from k5vision.auth_sessions import UserSessionManager
from k5vision.domain.users import UserAccount
from k5vision.main import create_app
from k5vision.operator_identity_security import (
    AccountSecurityError,
    AccountSecurityService,
    AccountSecurityStore,
    ProfileSecretProtector,
    SecondFactorRequired,
    totp,
)
from k5vision.operator_identity_security_api import install_account_security_api
from k5vision.services.user_registry import UserRegistry


@pytest.fixture
def identity(tmp_path, monkeypatch):
    if os.name == "posix":
        original = Path.lstat

        def trusted_system_ancestors(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path in (Path("/"), Path("/tmp")) and info.st_uid == 65534:
                fields = list(info)
                fields[4] = 0
                return os.stat_result(fields)
            return info

        monkeypatch.setattr(Path, "lstat", trusted_system_ancestors)
    return identity_state.initialize_identity_state(
        tmp_path / "identity", username="security-admin", password="security-test-password"
    )


class _Clock:
    utc = 1_800_000_000.0
    elapsed = 10.0


@pytest.fixture
def security(identity):
    clock = _Clock()
    users = UserRegistry(database_path=identity.database_path, site_id=identity.site_id)
    store = AccountSecurityStore(identity.directory, identity.site_id, utc=lambda: clock.utc)
    service = AccountSecurityService(
        store=store, users=users, utc=lambda: clock.utc, elapsed=lambda: clock.elapsed
    )
    account, version = users.verify_password_snapshot(
        username="security-admin", password="security-test-password"
    )
    yield service, store, users, account, version, clock
    service.close()
    users.close()


def _enable(values):
    service, _, _, account, _, clock = values
    enrollment = service.begin_enrollment(account, "security-test-password")
    secret = base64.b32decode(enrollment["secret"])
    codes = service.confirm_enrollment(
        account, enrollment["enrollment_token"], totp(secret, int(clock.utc // 30))
    )
    return secret, codes


def _challenge(values):
    service, _, _, account, version, _ = values
    with pytest.raises(SecondFactorRequired) as required:
        service.password_authenticated(account, version)
    return required.value.challenge["challenge_token"]


@pytest.mark.parametrize(
    "counter,expected",
    [(0, "755224"), (1, "287082"), (2, "359152"), (3, "969429"), (4, "338314")],
)
def test_totp_matches_rfc4226_vectors(counter, expected):
    assert totp(b"12345678901234567890", counter) == expected


def test_factor_requires_reauthentication_and_does_not_enable_until_confirmed(security):
    service, store, _, account, version, _ = security
    assert service.password_authenticated(account, version) == 0
    with pytest.raises(AccountSecurityError):
        service.begin_enrollment(account, "incorrect")
    enrollment = service.begin_enrollment(account, "security-test-password")
    assert enrollment["otpauth_uri"].startswith("otpauth://totp/")
    assert store.status(account.id)["mfa_enabled"] is False
    with pytest.raises(AccountSecurityError):
        service.confirm_enrollment(account, enrollment["enrollment_token"], "nototp")
    assert store.status(account.id)["mfa_enabled"] is False


def test_login_challenge_is_opaque_bounded_single_use_and_not_a_session(security):
    service, store, _, account, _, clock = security
    secret, _ = _enable(security)
    token = _challenge(security)
    assert account.username not in token and len(token) == 43
    assert "secret" not in store.status(account.id)
    with pytest.raises(AccountSecurityError):
        service.complete_challenge(token, "invalid")
    clock.utc += 30
    result, *_ = service.complete_challenge(token, totp(secret, int(clock.utc // 30)))
    assert result.id == account.id
    with pytest.raises(AccountSecurityError):
        service.complete_challenge(token, totp(secret, int(clock.utc // 30)))


def test_totp_replay_and_clock_rollback_cannot_reuse_counter(security):
    service, store, _, account, _, clock = security
    secret, codes = _enable(security)
    clock.utc += 30
    code = totp(secret, int(clock.utc // 30))
    assert store.consume(account.id, code)
    assert not store.consume(account.id, code)
    clock.utc -= 172800
    assert not store.consume(account.id, totp(secret, int(clock.utc // 30)))
    assert store.consume(account.id, codes[0])
    assert not store.consume(account.id, codes[0])
    token = _challenge(security)
    service.complete_challenge(token, codes[1])


def test_recovery_codes_are_hashed_single_use_and_survive_restart(identity, security):
    service, store, _, account, _, clock = security
    secret, codes = _enable(security)
    assert len(codes) == len(set(codes)) == 10
    assert store.consume(account.id, codes[0])
    service.close()
    raw = (identity.directory / "operator-account-security.sqlite3").read_bytes()
    assert all(
        code.encode() not in raw and code.replace("-", "").encode() not in raw for code in codes
    )
    reopened = AccountSecurityStore(identity.directory, identity.site_id, utc=lambda: clock.utc)
    try:
        assert reopened.status(account.id)["recovery_codes_remaining"] == 9
        assert not reopened.consume(account.id, codes[0])
        assert reopened.consume(account.id, codes[1])
        clock.utc += 30
        assert reopened.consume(account.id, totp(secret, int(clock.utc // 30)))
    finally:
        reopened.close()


def test_password_reset_cancels_pending_factor_and_enrollment(identity, security):
    service, _, users, account, _, _ = security
    _, codes = _enable(security)
    token = _challenge(security)
    # Directly replacing the password verifier represents a committed password
    # reset without bypassing production's separate last-admin protection.
    connection = sqlite3.connect(identity.database_path)
    connection.execute("UPDATE user_credentials SET verifier=?", (b"x" * 32,))
    connection.commit()
    connection.close()
    with pytest.raises(AccountSecurityError):
        service.complete_challenge(token, codes[0])
    assert users.get(account.id) is not None


def test_expiry_uses_elapsed_time_and_per_user_budget_spans_new_challenges(security):
    service, _, _, account, _, clock = security
    _, codes = _enable(security)
    token = _challenge(security)
    clock.elapsed += 301
    with pytest.raises(AccountSecurityError):
        service.complete_challenge(token, codes[0])
    for _ in range(10):
        with pytest.raises(AccountSecurityError):
            service.complete_challenge(_challenge(security), "invalid")
    with pytest.raises(AccountSecurityError, match="temporarily limited"):
        service.complete_challenge(_challenge(security), codes[0])
    clock.elapsed += 301
    service.complete_challenge(_challenge(security), codes[0])


def test_changed_factor_generation_invalidates_preexisting_challenge(security):
    service, store, _, account, _, _ = security
    _, codes = _enable(security)
    token = _challenge(security)
    store.disable(account.id)
    _, fresh_codes = _enable(security)
    with pytest.raises(AccountSecurityError):
        service.complete_challenge(token, fresh_codes[0])
    assert not store.consume(account.id, codes[0])


def test_private_database_site_and_replacement_fail_closed(identity, security):
    service, _, _, account, _, _ = security
    with pytest.raises(AccountSecurityError):
        AccountSecurityStore(identity.directory, "wrong-site")
    service.close()
    path = identity.directory / "operator-account-security.sqlite3"
    if os.name == "posix":
        path.chmod(0o644)
        with pytest.raises(AccountSecurityError):
            AccountSecurityStore(identity.directory, identity.site_id)
        path.chmod(0o600)
    value = AccountSecurityStore(identity.directory, identity.site_id)
    original = path.lstat()
    try:
        try:
            path.rename(path.with_suffix(".old"))
        except PermissionError as error:
            assert os.name == "nt" and error.winerror == 32
            assert path.lstat().st_ino == original.st_ino
        else:
            path.write_bytes(b"replacement")
            path.chmod(0o600)
            with pytest.raises(AccountSecurityError):
                value.status(account.id)
    finally:
        value.close()


@pytest.fixture
def client(identity):
    app = create_app(
        control_plane_site_id=identity.site_id,
        device_db_path=identity.directory / "devices.sqlite3",
        user_db_path=identity.database_path,
    )
    users, sessions = app.state.user_registry, app.state.user_session_manager
    clock = _Clock()
    store = AccountSecurityStore(identity.directory, identity.site_id, utc=lambda: clock.utc)
    security = AccountSecurityService(store=store, users=users, utc=lambda: clock.utc)

    async def authenticate(token):
        account = await sessions.resolve(token)
        if account is None:
            raise HTTPException(401)
        return account, token

    async def require_session(request: Request):
        return await authenticate(request.headers.get("authorization", "").removeprefix("Bearer "))

    def admit_origin(request):
        if request.headers.get("origin") not in (None, "http://testserver"):
            raise HTTPException(403)

    install_account_security_api(
        app,
        security=security,
        sessions=sessions,
        require_session=require_session,
        authenticate=authenticate,
        admit_origin=admit_origin,
        mutations=asyncio.Lock(),
    )
    with TestClient(app) as http:
        yield http, security, clock
    security.close()


def _login(http):
    return http.post(
        "/api/v1/auth/login",
        json={"username": "security-admin", "password": "security-test-password"},
    )


def test_http_login_never_issues_session_before_factor_and_enable_revokes_session(client):
    http, _, clock = client
    initial = _login(http)
    assert initial.status_code == 200
    headers = {"Authorization": "Bearer " + initial.json()["session_token"]}
    assert http.get("/api/v1/operator/security", headers=headers).json()["mfa_enabled"] is False
    enrollment = http.post(
        "/api/v1/operator/security/mfa/enroll",
        headers=headers,
        json={"current_password": "security-test-password"},
    ).json()
    secret = base64.b32decode(enrollment["secret"])
    confirmed = http.post(
        "/api/v1/operator/security/mfa/confirm",
        headers=headers,
        json={
            "enrollment_token": enrollment["enrollment_token"],
            "code": totp(secret, int(clock.utc // 30)),
        },
    )
    assert confirmed.status_code == 200 and len(confirmed.json()["recovery_codes"]) == 10
    assert http.get("/api/v1/auth/me", headers=headers).status_code == 401
    challenge = _login(http)
    assert challenge.status_code == 202 and challenge.json()["mfa_required"] is True
    assert "session_token" not in challenge.json() and "set-cookie" not in challenge.headers
    assert (
        http.get(
            "/api/v1/auth/me",
            headers={"Authorization": "Bearer " + challenge.json()["challenge_token"]},
        ).status_code
        == 401
    )
    completed = http.post(
        "/api/v1/auth/mfa",
        json={
            "challenge_token": challenge.json()["challenge_token"],
            "code": confirmed.json()["recovery_codes"][0],
        },
    )
    assert completed.status_code == 200 and "session_token" in completed.json()
    token = completed.json()["session_token"]
    assert (
        http.get("/api/v1/auth/me", headers={"Authorization": "Bearer " + token}).status_code == 200
    )
    assert (
        http.post(
            "/api/v1/auth/mfa",
            json={
                "challenge_token": challenge.json()["challenge_token"],
                "code": confirmed.json()["recovery_codes"][1],
            },
        ).status_code
        == 401
    )


def test_http_mutations_require_session_origin_reauth_and_valid_shapes(client):
    http, _, _ = client
    path = "/api/v1/operator/security/mfa/enroll"
    assert http.post(path, json={"current_password": "security-test-password"}).status_code == 401
    token = _login(http).json()["session_token"]
    headers = {"Authorization": "Bearer " + token, "Origin": "https://evil.example"}
    assert (
        http.post(
            path, headers=headers, json={"current_password": "security-test-password"}
        ).status_code
        == 403
    )
    headers.pop("Origin")
    assert (
        http.post(path, headers=headers, json={"current_password": "incorrect"}).status_code == 403
    )
    assert (
        http.post(
            path,
            headers=headers,
            json={"current_password": "security-test-password", "user_id": str(uuid4())},
        ).status_code
        == 422
    )


def test_simultaneous_recovery_use_has_one_durable_winner(security):
    from concurrent.futures import ThreadPoolExecutor

    _, store, _, account, _, _ = security
    _, codes = _enable(security)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: store.consume(account.id, codes[0]), range(2)))
    assert sorted(results) == [False, True]
    assert store.status(account.id)["recovery_codes_remaining"] == 9


def test_factor_replacement_between_proof_and_session_issue_cannot_grant_session(
    client, monkeypatch
):
    http, service, clock = client
    values = http.post(
        "/api/v1/auth/login",
        json={"username": "security-admin", "password": "security-test-password"},
    ).json()
    headers = {"Authorization": "Bearer " + values["session_token"]}
    enrollment = http.post(
        "/api/v1/operator/security/mfa/enroll",
        headers=headers,
        json={"current_password": "security-test-password"},
    ).json()
    secret = base64.b32decode(enrollment["secret"])
    factor = http.post(
        "/api/v1/operator/security/mfa/confirm",
        headers=headers,
        json={
            "enrollment_token": enrollment["enrollment_token"],
            "code": totp(secret, int(clock.utc // 30)),
        },
    ).json()
    challenge = _login(http).json()["challenge_token"]
    original = service.complete_challenge

    def replace_after_proof(token, code):
        proof = original(token, code)
        service.store.disable(proof[0].id)
        service.store.enable(proof[0].id, b"new-factor-secret-12", int(clock.utc // 30))
        return proof

    monkeypatch.setattr(service, "complete_challenge", replace_after_proof)
    response = http.post(
        "/api/v1/auth/mfa", json={"challenge_token": challenge, "code": factor["recovery_codes"][0]}
    )
    assert response.status_code == 401
    assert "session_token" not in response.json()


def test_previously_initialized_security_state_cannot_be_deleted_to_restore_password_only(
    identity, security
):
    service, _, _, _, _, _ = security
    _enable(security)
    service.close()
    (identity.directory / "operator-account-security.sqlite3").unlink()
    with pytest.raises(AccountSecurityError):
        AccountSecurityStore(identity.directory, identity.site_id)


@pytest.mark.skipif(os.name != "nt", reason="Actual Windows DPAPI qualification")
def test_windows_dpapi_roundtrips_empty_public_oidc_client_secret(identity):
    protector = ProfileSecretProtector(identity.site_id)
    protected = protector.protect(b"", "oidc-client-secret")
    assert protected.startswith(b"dpapi-v1:") and len(protected) > 9
    assert protector.unprotect(protected, "oidc-client-secret") == b""


def test_untrusted_sqlite_sidecar_is_refused_before_recovery(identity, security, tmp_path):
    service, _, _, _, _, _ = security
    service.close()
    foreign = tmp_path / "other-private-file"
    foreign.write_bytes(b"must-not-open-or-modify")
    foreign.chmod(0o600)
    journal = identity.directory / "operator-account-security.sqlite3-journal"
    os.link(foreign, journal)
    with pytest.raises(AccountSecurityError):
        AccountSecurityStore(identity.directory, identity.site_id)
    assert journal.exists() and journal.samefile(foreign)
    assert foreign.read_bytes() == b"must-not-open-or-modify"


def test_revocation_listener_covers_pending_users_but_not_ordinary_session_replacement():
    async def exercise():
        sessions = UserSessionManager()
        user = UserAccount(username="listener-user", display_name="Listener user")
        revoked = []
        remove = sessions.add_revocation_listener(revoked.append)
        await sessions.revoke_user(user.id)
        assert revoked == [user.id]
        token, _ = await sessions.issue(user)
        replacement, _ = await sessions.issue(user)
        assert revoked == [user.id]
        assert not await sessions.revoke(token)
        assert await sessions.revoke(replacement)
        assert revoked == [user.id, user.id]
        remove()
        await sessions.revoke_user(user.id)
        assert revoked == [user.id, user.id]

    asyncio.run(exercise())
