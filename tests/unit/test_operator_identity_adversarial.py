"""Independent cross-boundary identity regressions from security review."""

from __future__ import annotations

import asyncio
import base64
import ctypes
import socket
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request
from test_operator_oidc import _complete, _configure, _flow
from test_operator_oidc import identity as identity  # noqa: F401
from test_operator_oidc import idp as idp  # noqa: F401
from test_operator_oidc import setup as setup  # noqa: F401

from k5vision.domain.users import UserCreate, UserRole
from k5vision.operator_admin_api import install_operator_admin_api
from k5vision.operator_identity_security import ProfileSecretProtector, totp
from k5vision.operator_oidc import OIDCError, fetch_json


def _backup_admin(values):
    http, _, _, account, users, _ = values
    backup, temporary, _ = users.create_with_bootstrap(
        UserCreate(
            username="independent-review-admin",
            display_name="Review administrator",
            role=UserRole.ADMINISTRATOR,
        ),
        actor=account.username,
    )
    assert (
        users.activate_bootstrap_password(
            username=backup.username,
            temporary_credential=temporary,
            new_password="independent-review-password",
            actor=backup.username,
        )
        is not None
    )
    login = http.post(
        "/api/v1/auth/login",
        json={"username": backup.username, "password": "independent-review-password"},
    )
    assert login.status_code == 200
    return {"Authorization": "Bearer " + login.json()["session_token"]}


def _pending_federated_mfa(values, provider):
    http, _, security, account, _, _ = values
    _configure(values, provider)
    enrollment = security.begin_enrollment(account, "federation-test-password")
    secret = base64.b32decode(enrollment["secret"])
    recovery = security.confirm_enrollment(
        account, enrollment["enrollment_token"], totp(secret, int(time.time() // 30))
    )
    response = _complete(http, _flow(values, provider))
    assert response.status_code == 202 and response.json()["mfa_required"] is True
    return {"challenge_token": response.json()["challenge_token"], "code": recovery[0]}


@pytest.mark.parametrize("revocation", ["unlink", "disable_provider"])
def test_pending_sso_mfa_cannot_issue_session_after_its_primary_identity_is_revoked(
    setup, idp, revocation
):
    http, oidc, _, account, _, _ = setup
    administrator = _backup_admin(setup)
    pending = _pending_federated_mfa(setup, idp)
    if revocation == "unlink":
        link = next(value for value in oidc.store.links() if value["user_id"] == str(account.id))
        response = http.delete(
            "/api/v1/operator/admin/sso/links/" + link["id"], headers=administrator
        )
        assert response.status_code == 204
    else:
        response = http.put(
            "/api/v1/operator/admin/sso/provider",
            headers=administrator,
            json={
                "enabled": False,
                "display_name": "Fixture identity",
                "issuer": idp.issuer,
                "client_id": "k5-native-fixture",
            },
        )
        assert response.status_code == 200
    completed = http.post("/api/v1/auth/mfa", json=pending)
    assert completed.status_code == 401
    assert "session_token" not in completed.json()


def _install_real_account_administration(values):
    http, _, _, _, users, sessions = values
    app = http.app

    async def authenticate(token):
        account = await sessions.resolve(token)
        if account is None:
            raise HTTPException(401)
        return account, token

    async def require_admin(request: Request):
        current, token = await authenticate(
            request.headers.get("authorization", "").removeprefix("Bearer ")
        )
        if current.role is not UserRole.ADMINISTRATOR:
            raise HTTPException(403)
        return current, token

    install_operator_admin_api(
        app,
        users=users,
        sessions=sessions,
        registry=app.state.device_registry,
        require_admin=require_admin,
        authenticate=authenticate,
        mutations=asyncio.Lock(),
    )


@pytest.mark.parametrize("transition", ["enabled", "role"])
def test_pending_mfa_is_revoked_across_account_authority_aba(setup, idp, transition):
    http, _, _, account, _, _ = setup
    administrator = _backup_admin(setup)
    _install_real_account_administration(setup)
    pending = _pending_federated_mfa(setup, idp)
    path = "/api/v1/operator/admin/users/" + str(account.id)
    changes = (
        ({"enabled": False}, {"enabled": True})
        if transition == "enabled"
        else ({"role": "viewer"}, {"role": "administrator"})
    )
    for change in changes:
        response = http.patch(path, headers=administrator, json=change)
        assert response.status_code == 200
    completed = http.post("/api/v1/auth/mfa", json=pending)
    assert completed.status_code == 401
    assert "session_token" not in completed.json()


def test_windows_dpapi_unprotect_accepts_empty_oidc_public_client_secret(monkeypatch):
    freed = []
    empty = ctypes.create_string_buffer(b"")

    class Function:
        def __init__(self, operation):
            self.operation = operation

        def __call__(self, *args):
            return self.operation(*args)

    def decrypt(*args):
        result = args[-1]
        result.size = 0
        result.data = ctypes.cast(empty, ctypes.POINTER(ctypes.c_ubyte))
        return True

    crypt = SimpleNamespace(CryptUnprotectData=Function(decrypt))
    kernel = SimpleNamespace(LocalFree=Function(lambda value: freed.append(value)))
    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda name, **kwargs: crypt if name == "crypt32" else kernel,
        raising=False,
    )
    result = ProfileSecretProtector("generated-site")._dpapi(
        b"generated-ciphertext", "oidc-client-secret", decrypt=True
    )
    assert result == b""
    assert len(freed) == 1


@pytest.mark.parametrize("stage", ["headers", "body"])
def test_oidc_total_deadline_closes_slow_peer_and_reaps_request(stage, monkeypatch):
    import k5vision.operator_oidc as oidc_module

    monkeypatch.setattr(oidc_module, "_HTTP_REQUEST_SECONDS", 2.0)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(5)
    port = listener.getsockname()[1]
    stop = threading.Event()
    finished = threading.Event()
    sockets = []
    failures = []

    def peer():
        try:
            connection, _ = listener.accept()
            sockets.append(connection)
            with connection:
                connection.settimeout(5)
                connection.recv(32 * 1024)
                if stage == "body":
                    connection.sendall(
                        b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n"
                        b"Content-Length: 1000000\r\n\r\n"
                    )
                while not stop.wait(0.1):
                    connection.sendall(b"H" if stage == "headers" else b" ")
        except OSError:
            pass

    def fetch():
        try:
            fetch_json(f"http://127.0.0.1:{port}/generated-discovery")
        except OIDCError:
            failures.append("bounded_refusal")
        finally:
            finished.set()

    remote = threading.Thread(target=peer, daemon=True)
    client = threading.Thread(target=fetch, daemon=True)
    remote.start()
    client.start()
    try:
        assert finished.wait(5), (
            "OIDC request worker exceeded its elapsed deadline and kill/reap margin"
        )
        assert failures == ["bounded_refusal"]
    finally:
        stop.set()
        for connection in sockets:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        listener.close()
        remote.join(timeout=3)
        client.join(timeout=3)
    assert not client.is_alive() and not remote.is_alive()


@pytest.mark.parametrize("transition", ["enabled", "role"])
def test_pending_oidc_completion_is_revoked_across_local_account_aba(setup, idp, transition):
    http, _, _, account, _, _ = setup
    administrator = _backup_admin(setup)
    _install_real_account_administration(setup)
    _configure(setup, idp)
    callback = _flow(setup, idp)
    path = "/api/v1/operator/admin/users/" + str(account.id)
    changes = (
        ({"enabled": False}, {"enabled": True})
        if transition == "enabled"
        else ({"role": "viewer"}, {"role": "administrator"})
    )
    for change in changes:
        assert http.patch(path, headers=administrator, json=change).status_code == 200
    response = _complete(http, callback)
    assert response.status_code == 401
    assert "session_token" not in response.json()


def test_password_login_cannot_issue_after_primary_proof_authority_is_revoked_and_restored(
    setup, monkeypatch
):
    http, _, _, account, _, sessions = setup
    administrator = _backup_admin(setup)
    _install_real_account_administration(setup)
    entered, release = threading.Event(), threading.Event()
    issue = sessions.issue
    responses = []

    async def delayed(current, *, credential_version=None):
        if current.id == account.id:
            entered.set()
            assert await asyncio.to_thread(release.wait, 5)
        return await issue(current, credential_version=credential_version)

    monkeypatch.setattr(sessions, "issue", delayed)
    worker = threading.Thread(
        target=lambda: responses.append(
            http.post(
                "/api/v1/auth/login",
                json={"username": "federation-admin", "password": "federation-test-password"},
            )
        ),
        daemon=True,
    )
    worker.start()
    try:
        assert entered.wait(5)
        path = "/api/v1/operator/admin/users/" + str(account.id)
        for enabled in (False, True):
            assert (
                http.patch(path, headers=administrator, json={"enabled": enabled}).status_code
                == 200
            )
    finally:
        release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert len(responses) == 1 and responses[0].status_code == 401
    assert "session_token" not in responses[0].json()
