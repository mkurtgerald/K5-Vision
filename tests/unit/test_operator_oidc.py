from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from k5vision import identity_state
from k5vision.main import create_app
from k5vision.operator_identity_security import AccountSecurityService, AccountSecurityStore, totp
from k5vision.operator_identity_security_api import install_account_security_api
from k5vision.operator_oidc import OIDCError, OIDCService, OIDCStore, fetch_json
from k5vision.operator_oidc_api import install_oidc_api


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
        tmp_path / "identity", username="federation-admin", password="federation-test-password"
    )


class _IdP:
    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.codes = {}
        self.changes = {}
        self.header_changes = {}
        self.metadata_changes = {}
        self.key_changes = {}
        self.subject = "external-subject-123"
        self.pkce_verified = False
        self.token_requests = 0
        self.expected_basic = None
        self.wrong_signature = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, document):
                raw = json.dumps(document).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path == "/.well-known/openid-configuration":
                    self.respond(
                        {
                            "issuer": outer.issuer,
                            "authorization_endpoint": outer.issuer + "/authorize",
                            "token_endpoint": outer.issuer + "/token",
                            "jwks_uri": outer.issuer + "/keys",
                            "response_types_supported": ["code"],
                            "code_challenge_methods_supported": ["S256"],
                            "id_token_signing_alg_values_supported": ["RS256"],
                            "token_endpoint_auth_methods_supported": [
                                "none",
                                "client_secret_basic",
                            ],
                            **outer.metadata_changes,
                        }
                    )
                elif self.path == "/keys":
                    key = jwt.algorithms.RSAAlgorithm.to_jwk(outer.key.public_key(), as_dict=True)
                    self.respond(
                        {
                            "keys": [
                                {
                                    **key,
                                    "kid": "fixture-key",
                                    "use": "sig",
                                    "alg": "RS256",
                                    **outer.key_changes,
                                }
                            ]
                        }
                    )
                elif self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", outer.issuer + "/keys")
                    self.end_headers()
                elif self.path == "/duplicate":
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'{"keys":[],"keys":[]}')
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_POST(self):
                assert self.path == "/token"
                outer.token_requests += 1
                assert self.headers.get("Authorization") == outer.expected_basic
                values = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
                code = values["code"][0]
                authorization = outer.codes.pop(code)
                verifier = values["code_verifier"][0]
                challenge = (
                    base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                    .rstrip(b"=")
                    .decode()
                )
                assert challenge == authorization["code_challenge"][0]
                assert values["redirect_uri"] == authorization["redirect_uri"]
                assert values["client_id"] == authorization["client_id"]
                outer.pkce_verified = True
                now = int(time.time())
                claims = {
                    "iss": outer.issuer,
                    "sub": outer.subject,
                    "aud": authorization["client_id"][0],
                    "iat": now,
                    "exp": now + 300,
                    "nonce": authorization["nonce"][0],
                    "groups": ["camera-operators"],
                    "role": "administrator",
                    **outer.changes,
                }
                for name, value in tuple(claims.items()):
                    if value is None:
                        del claims[name]
                signer = (
                    rsa.generate_private_key(public_exponent=65537, key_size=2048)
                    if outer.wrong_signature
                    else outer.key
                )
                encoded = jwt.encode(
                    claims,
                    signer,
                    algorithm="RS256",
                    headers={"kid": "fixture-key", **outer.header_changes},
                )
                self.respond(
                    {
                        "id_token": encoded,
                        "access_token": "unused-fixture-access-token",
                        "token_type": "Bearer",
                    }
                )

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.issuer = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def authorize(self, url):
        values = parse_qs(urlsplit(url).query)
        assert values["response_type"] == ["code"]
        assert values["scope"] == ["openid"]
        assert values["code_challenge_method"] == ["S256"]
        code = secrets.token_urlsafe(24)
        self.codes[code] = values
        return values["state"][0], code

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


@pytest.fixture
def idp():
    value = _IdP()
    yield value
    value.close()


@pytest.fixture
def setup(identity, idp):
    app = create_app(
        control_plane_site_id=identity.site_id,
        user_db_path=identity.database_path,
        device_db_path=identity.directory / "devices.sqlite3",
    )
    users, sessions = app.state.user_registry, app.state.user_session_manager
    store = AccountSecurityStore(identity.directory, identity.site_id)
    security = AccountSecurityService(store=store, users=users)
    oidc_store = OIDCStore(store)
    oidc = OIDCService(store=oidc_store, users=users)
    account, _ = users.verify_password_snapshot(
        username="federation-admin", password="federation-test-password"
    )

    async def authenticate(token):
        account = await sessions.resolve(token)
        if account is None:
            raise HTTPException(401)
        return account, token

    async def require_session(request: Request):
        return await authenticate(request.headers.get("authorization", "").removeprefix("Bearer "))

    async def require_admin(request: Request):
        account, token = await require_session(request)
        if account.role.value != "administrator":
            raise HTTPException(403)
        admit_origin(request)
        return account, token

    def admit_origin(request):
        if request.headers.get("origin") not in (None, "http://127.0.0.1:40123"):
            raise HTTPException(403)

    mutations = asyncio.Lock()
    install_account_security_api(
        app,
        security=security,
        sessions=sessions,
        require_session=require_session,
        authenticate=authenticate,
        admit_origin=admit_origin,
        mutations=mutations,
    )
    install_oidc_api(
        app,
        oidc=oidc,
        security=security,
        users=users,
        sessions=sessions,
        require_admin=require_admin,
        authenticate=authenticate,
        admit_origin=admit_origin,
        public_port=40123,
        mutations=mutations,
    )
    with TestClient(app, base_url="http://127.0.0.1:40123") as http:
        yield http, oidc, security, account, users, sessions
    oidc.close()
    security.close()


def _configure(setup, idp, *, group=None):
    _, oidc, _, account, _, _ = setup
    oidc.store.configure(
        enabled=True,
        display_name="Fixture identity",
        issuer=idp.issuer,
        client_id="k5-native-fixture",
    )
    return oidc.store.link(subject=idp.subject, user_id=account.id, required_group=group)


def _flow(setup, idp):
    http, _, _, _, _, _ = setup
    response = http.post("/api/v1/auth/oidc/start")
    assert response.status_code == 200
    assert "httponly" in response.headers["set-cookie"].lower()
    assert "samesite=lax" in response.headers["set-cookie"].lower()
    assert "expires=" not in response.headers["set-cookie"].lower()
    assert "max-age=" not in response.headers["set-cookie"].lower()
    state, code = idp.authorize(response.json()["authorization_url"])
    return http.get(
        "/api/v1/auth/oidc/callback", params={"state": state, "code": code}, follow_redirects=False
    )


def _complete(http, callback):
    parsed = parse_qs(urlsplit(callback.headers["location"]).query)
    assert "sso_code" in parsed
    assert "id_token" not in callback.headers["location"]
    return http.post("/api/v1/auth/oidc/complete", json={"code": parsed["sso_code"][0]})


def test_real_http_oidc_code_pkce_signed_token_explicit_link_and_final_session(setup, idp):
    http, oidc, _, account, _, _ = setup
    _configure(setup, idp, group="camera-operators")
    assert oidc.test()["issuer_verified"] is True
    callback = _flow(setup, idp)
    assert callback.status_code == 303 and idp.pkce_verified
    response = _complete(http, callback)
    assert response.status_code == 200 and response.json()["account"]["id"] == str(account.id)
    assert (
        http.get(
            "/api/v1/auth/me",
            headers={"Authorization": "Bearer " + response.json()["session_token"]},
        ).status_code
        == 200
    )
    assert _complete(http, callback).status_code == 401


@pytest.mark.parametrize(
    "change",
    [
        {"iss": "https://wrong.example"},
        {"aud": "different-client"},
        {"nonce": "wrong"},
        {"nonce": None},
        {"sub": None},
        {"azp": "other-client"},
        {"aud": ["k5-native-fixture", "other-client"]},
        {"iat": 1_000_000_000, "exp": 1_000_000_300},
        {"iat": 4_000_000_000, "exp": 4_000_000_300},
        {"at_hash": "bad-hash"},
        {"c_hash": "bad-hash"},
    ],
)
def test_signed_identity_claims_fail_closed(setup, idp, change):
    _configure(setup, idp)
    idp.changes = change
    callback = _flow(setup, idp)
    assert callback.headers["location"] == "/operator/?sso_error=not_accepted"
    assert "set-cookie" not in callback.headers


def test_unknown_identity_and_required_group_never_create_or_elevate_users(setup, idp):
    http, oidc, _, account, users, _ = setup
    oidc.store.configure(
        enabled=True,
        display_name="Fixture identity",
        issuer=idp.issuer,
        client_id="k5-native-fixture",
    )
    assert _flow(setup, idp).headers["location"] == "/operator/?sso_error=not_accepted"
    assert len(users.list()) == 1
    oidc.store.link(subject=idp.subject, user_id=account.id, required_group="different-group")
    assert _flow(setup, idp).headers["location"] == "/operator/?sso_error=not_accepted"
    assert len(users.list()) == 1


def test_federated_login_still_requires_local_mfa_before_any_session(setup, idp):
    http, _, security, account, _, _ = setup
    _configure(setup, idp)
    enrollment = security.begin_enrollment(account, "federation-test-password")
    secret = base64.b32decode(enrollment["secret"])
    recovery = security.confirm_enrollment(
        account, enrollment["enrollment_token"], totp(secret, int(time.time() // 30))
    )
    primary = _complete(http, _flow(setup, idp))
    assert primary.status_code == 202 and primary.json()["mfa_required"] is True
    assert "session_token" not in primary.json() and "set-cookie" not in primary.headers
    completed = http.post(
        "/api/v1/auth/mfa",
        json={"challenge_token": primary.json()["challenge_token"], "code": recovery[0]},
    )
    assert completed.status_code == 200 and "session_token" in completed.json()


def test_browser_cookie_is_required_for_callback_and_completion(setup, idp):
    http, _, _, _, _, _ = setup
    _configure(setup, idp)
    started = http.post("/api/v1/auth/oidc/start")
    state, code = idp.authorize(started.json()["authorization_url"])
    http.cookies.clear()
    assert (
        http.get(
            "/api/v1/auth/oidc/callback",
            params={"state": state, "code": code},
            follow_redirects=False,
        ).headers["location"]
        == "/operator/?sso_error=not_accepted"
    )
    callback = _flow(setup, idp)
    http.cookies.clear()
    assert _complete(http, callback).status_code == 401


@pytest.mark.parametrize("mutation", ["unlink", "disable_provider", "disable_user"])
def test_current_mapping_provider_and_user_authority_rechecked_after_callback(setup, idp, mutation):
    http, oidc, _, account, users, _ = setup
    link = _configure(setup, idp)
    callback = _flow(setup, idp)
    if mutation == "unlink":
        from uuid import UUID

        oidc.store.unlink(UUID(link["id"]))
    elif mutation == "disable_provider":
        oidc.store.configure(
            enabled=False,
            display_name="Fixture identity",
            issuer=idp.issuer,
            client_id="k5-native-fixture",
        )
    else:
        # Internal state change models committed disablement; administrative
        # last-admin policy has its own registry tests and is not bypassed by SSO.
        with users._lock:
            users._connection.execute("UPDATE users SET enabled=0 WHERE id=?", (str(account.id),))
    assert _complete(http, callback).status_code == 401


def test_redirect_duplicate_json_and_incompatible_provider_contract_are_rejected(idp, setup):
    with pytest.raises(OIDCError):
        fetch_json(idp.issuer + "/redirect")
    with pytest.raises(OIDCError):
        fetch_json(idp.issuer + "/duplicate")
    _configure(setup, idp)
    idp.metadata_changes = {"code_challenge_methods_supported": ["plain"]}
    with pytest.raises(OIDCError):
        setup[1].test()


def test_provider_settings_and_links_are_admin_only_secret_free_and_persist(identity, setup, idp):
    http, oidc, _, account, _, _ = setup
    path = "/api/v1/operator/admin/sso/provider"
    assert http.get(path).status_code == 401
    login = http.post(
        "/api/v1/auth/login",
        json={"username": "federation-admin", "password": "federation-test-password"},
    )
    headers = {"Authorization": "Bearer " + login.json()["session_token"]}
    response = http.put(
        path,
        headers=headers,
        json={
            "enabled": True,
            "display_name": "Fixture identity",
            "issuer": idp.issuer,
            "client_id": "k5-native-fixture",
            "client_secret": "fixture-client-secret",
        },
    )
    assert response.status_code == 200 and response.json()["has_client_secret"] is True
    assert "fixture-client-secret" not in response.text
    link = http.post(
        "/api/v1/operator/admin/sso/links",
        headers=headers,
        json={"subject": idp.subject, "user_id": str(account.id)},
    )
    assert link.status_code == 201
    assert (
        http.get(path, headers=headers).json()["redirect_uri"]
        == "http://127.0.0.1:40123/api/v1/auth/oidc/callback"
    )
    assert (
        http.put(
            path,
            headers={**headers, "Origin": "https://evil.example"},
            json={
                "enabled": False,
                "display_name": "Fixture identity",
                "issuer": idp.issuer,
                "client_id": "k5-native-fixture",
            },
        ).status_code
        == 403
    )
    malformed = http.put(path, headers=headers, json={"client_secret": {"secret": "must-not-echo"}})
    assert malformed.status_code == 422 and "must-not-echo" not in malformed.text
    assert oidc.store.provider()["issuer"] == idp.issuer


@pytest.mark.parametrize(
    "issuer",
    [
        "http://example.com",
        "https://169.254.169.254/",
        "https://user:password@example.com",
        "https://example.com/?client_secret=bad",
        "file:///tmp/token",
    ],
)
def test_provider_url_admission_refuses_unsafe_transports_and_credentials(setup, issuer):
    with pytest.raises(ValueError):
        setup[1].store.configure(
            enabled=True, display_name="Identity", issuer=issuer, client_id="k5-client"
        )


def test_invalid_signature_is_rejected_even_with_valid_claims_and_key_id(setup, idp):
    _configure(setup, idp)
    idp.wrong_signature = True
    assert _flow(setup, idp).headers["location"] == "/operator/?sso_error=not_accepted"


def test_client_secret_basic_and_private_provider_settings_survive_restart(identity, setup, idp):
    http, oidc, _, account, _, _ = setup
    oidc.store.configure(
        enabled=True,
        display_name="Private client",
        issuer=idp.issuer,
        client_id="k5-client",
        client_secret="fixture-client-secret",
    )
    oidc.store.link(subject=idp.subject, user_id=account.id)
    idp.expected_basic = "Basic " + base64.b64encode(b"k5-client:fixture-client-secret").decode()
    assert _complete(http, _flow(setup, idp)).status_code == 200
    reopened = AccountSecurityStore(identity.directory, identity.site_id)
    try:
        provider = OIDCStore(reopened)
        assert provider.provider()["has_client_secret"] is True
        assert "client_secret" not in provider.provider()
        assert provider.provider(private=True)["client_secret"] == "fixture-client-secret"
        assert provider.links()[0]["subject"] == idp.subject
    finally:
        reopened.close()


def test_closed_service_refuses_late_flow_and_completed_code(setup, idp):
    http, oidc, _, _, _, _ = setup
    _configure(setup, idp)
    callback = _flow(setup, idp)
    oidc.close()
    assert _complete(http, callback).status_code == 401
    assert http.post("/api/v1/auth/oidc/start").status_code == 503


def test_flow_and_local_completion_expiry_use_elapsed_deadlines(setup, idp):
    _configure(setup, idp)
    elapsed = [10.0]
    oidc = OIDCService(store=setup[1].store, users=setup[4], elapsed=lambda: elapsed[0])
    browser = secrets.token_urlsafe(32)
    uri = "http://127.0.0.1:40123/api/v1/auth/oidc/callback"
    state, code = idp.authorize(oidc.start(uri, browser))
    elapsed[0] += 301
    with pytest.raises(OIDCError):
        oidc.callback(state=state, code=code, browser_token=browser)
    assert idp.token_requests == 0
    state, code = idp.authorize(oidc.start(uri, browser))
    local_code = oidc.callback(state=state, code=code, browser_token=browser)
    elapsed[0] += 61
    with pytest.raises(OIDCError):
        oidc.complete(local_code, browser)
    oidc.close()


@pytest.mark.parametrize(
    "change",
    [
        {"e": "A" * 100000},
        {"e": "AA"},
        {"key_ops": ["sign"]},
        {"n": "A" * 1025},
    ],
)
def test_untrusted_jwk_work_factors_and_operations_are_bounded(setup, idp, change):
    _configure(setup, idp)
    idp.key_changes = change
    assert _flow(setup, idp).headers["location"] == "/operator/?sso_error=not_accepted"
