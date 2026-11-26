"""Actual confidential OIDC exchanges against an installed recorder service.

Only generated credentials are held in memory. This controller neither opens a
protector nor substitutes the installed application's HTTP or identity paths.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from contextlib import closing
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

import pytest

_ADMIN = "/api/v1/operator/admin/sso"
_AUTH = "/api/v1/auth/oidc"
_IDENTITY_FIELDS = ("service_pid", "service_created", "backend_pid", "backend_created")


def _require(value, message):
    __tracebackhide__ = True
    if not value:
        pytest.fail(message, pytrace=False)


class ServiceOIDCProof:
    def __init__(self, *, settings, runtime, origin, profile, diagnostic):
        self.settings, self.runtime = settings, runtime
        self.origin, self.profile, self.diagnostic = origin, profile, diagnostic
        self.provider = self.viewer = self.link = self.issued_client = None
        self.secret = secrets.token_urlsafe(48)
        self.evidence = {
            "actual_gui_secret_configuration": False,
            "secret_write_only": False,
            "wrong_secret_callback_denied": False,
            "wrong_secret_issued_session_denied": False,
            "revoked_session_denied": False,
            "unlinked_identity_callback_denied": False,
            "provider_disabled_after_check": False,
            "idp_owned_cleanup": False,
            "phases": {},
        }

    def _client(self):
        return self.runtime._Client(urlsplit(self.origin).port)

    def _sign_in(self, *, accepted):
        """Follow the actual authorization/callback cookies, then redeem once."""
        __tracebackhide__ = True
        client = self._client()
        start = client.json(_AUTH + "/start", method="POST", data={})
        authorization = start.get("authorization_url", "")
        parts = urlsplit(authorization)
        expected = urlsplit(self.provider.issuer)
        _require(
            parts.scheme == expected.scheme
            and parts.netloc == expected.netloc
            and parts.path == "/authorize",
            "Service OIDC authorization did not use the generated provider",
        )
        # No request URL, authorization code, bearer or response body is retained.
        with client.opener.open(Request(authorization), timeout=10) as response:
            _require(response.status == 200, "Service OIDC callback did not return to the UI")
            final = urlsplit(response.url)
            _require(
                final.scheme == urlsplit(self.origin).scheme
                and final.netloc == urlsplit(self.origin).netloc
                and final.path == "/operator/",
                "Service OIDC callback returned outside the installed operator UI",
            )
            _require(
                len(response.read(262145)) <= 262144, "Service OIDC UI response exceeded bound"
            )
        query = parse_qs(final.query)
        if not accepted:
            _require(
                query == {"sso_error": ["not_accepted"]},
                "Service OIDC accepted an unauthorized generated identity",
            )
            _require(
                client.get("/api/v1/auth/me")[0] == 401,
                "Rejected OIDC callback issued an authenticated session",
            )
            return client
        _require(set(query) == {"sso_code"}, "Service OIDC callback did not issue a completion")
        result = client.json(
            _AUTH + "/complete", method="POST", data={"code": query["sso_code"][0]}
        )
        account, token = result.get("account", {}), result.get("session_token")
        _require(
            account.get("id") == self.viewer["id"]
            and account.get("role") == "viewer"
            and isinstance(token, str)
            and 32 <= len(token) <= 128,
            "Service OIDC did not preserve the explicitly linked viewer authority",
        )
        client.token = token
        _require(
            client.json("/api/v1/auth/me")["id"] == self.viewer["id"]
            and client.get("/api/v1/operator/admin/users")[0] == 403,
            "Generated IdP role claim escaped the linked viewer authority",
        )
        return client

    def _protected_fingerprint(self):
        """Observe ciphertext only as the elevated test administrator."""
        path = self.profile / "identity" / "operator-account-security.sqlite3"
        with closing(
            sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)
        ) as connection:
            row = connection.execute(
                "SELECT client_secret, revision FROM oidc_provider WHERE id=1"
            ).fetchone()
        _require(row is not None, "Installed service OIDC secret was not persisted")
        protected, revision = bytes(row[0]), row[1]
        _require(
            protected.startswith(b"dpapi-v1:")
            and len(protected) > 32
            and self.secret.encode() not in protected,
            "Installed service OIDC secret was stored as plaintext",
        )
        return {
            "protected_secret_sha256": hashlib.sha256(protected).hexdigest(),
            "provider_revision_sha256": hashlib.sha256(revision.encode()).hexdigest(),
        }

    def prepare(self, *, page, administrator, camera_id, service_observation):
        __tracebackhide__ = True
        self.diagnostic["stage"] = "oidc_gui_confidential_provider"
        created = administrator.json(
            "/api/v1/operator/admin/users",
            method="POST",
            expected=201,
            data={
                "username": "generated-service-sso-viewer",
                "display_name": "Generated SSO viewer",
                "role": "viewer",
                "enabled": True,
                "camera_access": {"mode": "selected", "camera_ids": [camera_id]},
            },
        )
        self.viewer = created["account"]
        activated = self._client().get(
            "/api/v1/auth/bootstrap-password",
            method="POST",
            data={
                "username": self.viewer["username"],
                "temporary_credential": created["temporary_credential"],
                "new_password": secrets.token_urlsafe(32),
            },
        )[0]
        _require(activated == 204, "Generated OIDC viewer activation was refused")
        self.settings._open(page, "sso")
        callback = page.locator("#sso-callback-uri").input_value()
        _require(callback == self.origin + _AUTH + "/callback", "Installed OIDC callback differs")
        self.provider = self.settings._SignedIdentityProvider(callback, client_secret=self.secret)
        page.locator("#sso-display-name").fill("Generated unattended identity")
        page.locator("#sso-issuer").fill(self.provider.issuer)
        page.locator("#sso-client-id").fill(self.provider.client_id)
        page.locator("#sso-enabled").check()
        page.locator("#sso-client-secret").fill(secrets.token_urlsafe(48))
        wrong = self.settings._result(
            page, "PUT", _ADMIN + "/provider", lambda: page.locator("#sso-save").click()
        )
        _require(wrong.get("has_client_secret") is True, "Confidential OIDC secret was not saved")
        page.locator("#sso-link-subject").fill(self.provider.subject)
        page.locator("#sso-link-user").select_option(self.viewer["id"])
        page.locator("#sso-link-group").fill(self.provider.group)
        self.link = self.settings._result(
            page,
            "POST",
            _ADMIN + "/links",
            lambda: page.locator("#sso-link-save").click(),
            expected=201,
        )
        self.diagnostic["stage"] = "oidc_wrong_secret_denial"
        self._sign_in(accepted=False)
        stats = self.provider.observations()
        _require(
            stats["token_requests"] == stats["client_auth_rejections"] == 1
            and stats["signed_tokens"] == stats["authenticated_token_requests"] == 0
            and not stats["failed"],
            "Wrong client secret did not fail at the actual token endpoint",
        )
        self.evidence.update(
            wrong_secret_callback_denied=True, wrong_secret_issued_session_denied=True
        )
        page.locator("#sso-client-secret").fill(self.secret)
        saved = self.settings._result(
            page, "PUT", _ADMIN + "/provider", lambda: page.locator("#sso-save").click()
        )
        _require(
            saved.get("has_client_secret") is True
            and "client_secret" not in saved
            and page.locator("#sso-client-secret").input_value() == "",
            "Installed SSO settings echoed or retained the write-only secret",
        )
        self.evidence.update(actual_gui_secret_configuration=True, secret_write_only=True)
        self.exchange("initial", service_observation)

    def exchange(self, phase, service_observation):
        __tracebackhide__ = True
        self.diagnostic["stage"] = "oidc_signed_secret_exchange_" + phase
        if self.issued_client is not None:
            _require(
                self.issued_client.get("/api/v1/auth/me")[0] == 401,
                "Previous recorder generation's OIDC session survived replacement",
            )
        before = self.provider.observations()
        self.issued_client = self._sign_in(accepted=True)
        after = self.provider.observations()
        for field in (
            "token_requests",
            "authenticated_token_requests",
            "pkce_verifications",
            "signed_tokens",
        ):
            _require(
                after[field] == before[field] + 1, "OIDC secret exchange did not actually execute"
            )
        _require(
            after["client_auth_rejections"] == before["client_auth_rejections"] == 1
            and not after["failed"],
            "Actual service OIDC exchange was rejected by the provider",
        )
        self.evidence["phases"][phase] = {
            **{name: service_observation[name] for name in _IDENTITY_FIELDS},
            **self._protected_fingerprint(),
            "signed_pkce_secret_exchange": True,
            "linked_viewer_role_preserved": True,
            "before": before,
            "after": after,
        }

    def revoke(self, administrator):
        __tracebackhide__ = True
        self.diagnostic["stage"] = "oidc_link_revokes_issued_session"
        status = administrator.get(_ADMIN + "/links/" + self.link["id"], method="DELETE")[0]
        _require(status == 204, "Actual administrator could not revoke the generated OIDC link")
        _require(
            self.issued_client.get("/api/v1/auth/me")[0] == 401,
            "Unlinking an identity left its issued viewer session authorized",
        )
        self.evidence["revoked_session_denied"] = True
        self._sign_in(accepted=False)
        self.evidence["unlinked_identity_callback_denied"] = True
        saved = administrator.json(
            _ADMIN + "/provider",
            method="PUT",
            data={
                "enabled": False,
                "display_name": "Generated unattended identity",
                "issuer": self.provider.issuer,
                "client_id": self.provider.client_id,
                "groups_claim": "groups",
            },
        )
        _require(
            saved["enabled"] is False, "Generated SSO provider was not disabled after its test"
        )
        self.evidence["provider_disabled_after_check"] = True
        self.evidence["final_provider_observations"] = self.provider.observations()
        fingerprints = {
            (p["protected_secret_sha256"], p["provider_revision_sha256"])
            for p in self.evidence["phases"].values()
        }
        _require(
            set(self.evidence["phases"]) == {"initial", "backend_recovered", "wrapper_recovered"}
            and len(fingerprints) == 1,
            "Service OIDC credentials were changed instead of retained through actual recovery",
        )

    def close(self):
        if self.provider is not None:
            self.provider.close()
            self.evidence["idp_owned_cleanup"] = True
