"""Exercise the native qualification's generated IdP over real bounded HTTP."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import secrets
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

import jwt
import pytest


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def _provider(secret):
    path = Path(__file__).parents[1] / "integration/installed_settings_acceptance.py"
    spec = importlib.util.spec_from_file_location("k5_confidential_fixture_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._SignedIdentityProvider(
        "http://127.0.0.1:40123/api/v1/auth/oidc/callback", client_secret=secret
    )


def _request(url, *, form=None, headers=None):
    request = Request(
        url, data=None if form is None else urlencode(form).encode(), headers=headers or {}
    )
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    try:
        response = opener.open(request, timeout=3)
    except HTTPError as error:
        response = error
    with response:
        body = response.read(16385)
        assert len(body) <= 16384
        return response.status, response.headers, json.loads(body) if body else None


def _authorize(provider, verifier):
    state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    status, headers, _ = _request(
        provider.issuer
        + "/authorize?"
        + urlencode(
            {
                "response_type": "code",
                "client_id": provider.client_id,
                "scope": "openid",
                "redirect_uri": provider.callback,
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
    )
    assert status == 303
    values = parse_qs(urlsplit(headers["Location"]).query)
    assert values["state"] == [state]
    return values["code"][0], nonce


def _exchange(provider, code, verifier, secret):
    headers = {}
    if secret is not None:
        basic = quote(provider.client_id, safe="") + ":" + quote(secret, safe="")
        headers["Authorization"] = "Basic " + base64.b64encode(basic.encode()).decode()
    return _request(
        provider.issuer + "/token",
        form={
            "client_id": provider.client_id,
            "redirect_uri": provider.callback,
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
        },
        headers=headers,
    )


@pytest.mark.parametrize("secret", [None, "generated:confidential/secret%with+encoding"])
def test_generated_idp_requires_configured_authentication_and_signs_actual_pkce_exchange(secret):
    provider = _provider(secret)
    try:
        status, _, metadata = _request(provider.issuer + "/.well-known/openid-configuration")
        assert status == 200
        assert metadata["token_endpoint_auth_methods_supported"] == [
            "none" if secret is None else "client_secret_basic"
        ]
        verifier = secrets.token_urlsafe(32)
        code, nonce = _authorize(provider, verifier)
        status, _, result = _exchange(provider, code, verifier, secret)
        assert status == 200
        claims = jwt.decode(
            result["id_token"],
            provider.key.public_key(),
            algorithms=["RS256"],
            audience=provider.client_id,
            issuer=provider.issuer,
        )
        assert claims["nonce"] == nonce and claims["sub"] == provider.subject
        assert provider.observations() == {
            "token_requests": 1,
            "authenticated_token_requests": int(secret is not None),
            "client_auth_rejections": 0,
            "pkce_verifications": 1,
            "signed_tokens": 1,
            "failed": False,
        }
    finally:
        provider.close()
    assert not provider.thread.is_alive()


@pytest.mark.parametrize("submitted", [None, "a-different-generated-secret"])
def test_confidential_generated_idp_rejects_missing_or_wrong_secret_without_issuing_token(
    submitted,
):
    provider = _provider(secrets.token_urlsafe(32))
    try:
        verifier = secrets.token_urlsafe(32)
        code, _ = _authorize(provider, verifier)
        status, _, result = _exchange(provider, code, verifier, submitted)
        assert status == 401 and result == {"error": "invalid_client"}
        assert provider.observations() == {
            "token_requests": 1,
            "authenticated_token_requests": 0,
            "client_auth_rejections": 1,
            "pkce_verifications": 0,
            "signed_tokens": 0,
            "failed": False,
        }
    finally:
        provider.close()


def test_generated_confidential_idp_does_not_count_wrong_pkce_as_secret_exchange_success():
    secret = secrets.token_urlsafe(32)
    provider = _provider(secret)
    try:
        code, _ = _authorize(provider, secrets.token_urlsafe(32))
        status, _, _ = _exchange(provider, code, secrets.token_urlsafe(32), secret)
        assert status == 400
        assert provider.observations() == {
            "token_requests": 1,
            "authenticated_token_requests": 0,
            "client_auth_rejections": 0,
            "pkce_verifications": 0,
            "signed_tokens": 0,
            "failed": True,
        }
    finally:
        provider.close()
