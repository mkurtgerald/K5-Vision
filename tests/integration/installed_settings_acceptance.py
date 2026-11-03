"""Actual installed-workspace settings/security acceptance with generated inputs.

This helper never changes host time, provisions an external identity tenant, or
claims that a lab's model executed. Its IdP signs real RS256 tokens and verifies
PKCE over actual loopback HTTP; machine events traverse the installed gateway.
Only bounded counts and outcomes leave private fixture memory.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import struct
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import UUID, uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from k5vision.operator_integration_bridge import K5LabBridgeClient

_PREFIX = "/api/v1/operator/admin"
_GIB = 1024**3


def _require(value, message):
    __tracebackhide__ = True
    if not value:
        pytest.fail(message, pytrace=False)


def _route(response, method, path):
    return response.request.method == method and urlsplit(response.url).path == path


def _result(page, method, path, action, expected=200):
    __tracebackhide__ = True
    with page.expect_response(lambda r: _route(r, method, path)) as completed:
        action()
    _require(
        completed.value.status == expected, "Installed settings GUI returned an unexpected status"
    )
    return None if expected == 204 else completed.value.json()


def _open(page, name):
    __tracebackhide__ = True
    if not page.locator("#settings-panel").is_visible():
        page.locator("#admin-tab").click()
        page.locator("#admin-settings-tab").click()
    page.locator(f"#settings-{name}-tab").click()
    page.locator(f"#settings-{name}-panel").wait_for(state="visible")
    page.wait_for_function(
        """name => document.getElementById('settings-'+name+'-panel')
          .getAttribute('aria-busy') === 'false'""",
        arg=name,
    )


def _canonical_login(page, helpers, origin, account, *, diagnostic=None):
    """Prove the browser inventory came from the *new* authenticated session.

    An in-flight camera poll using the revoked session may finish with 401 as
    the page navigates to login. It is not the new login's inventory response.
    Keep bearer values in memory only; diagnostics contain bounded counts.
    """
    inventory_responses = []
    overflow = False

    def observed(response):
        nonlocal overflow
        if not _route(response, "GET", "/api/v1/operator/cameras"):
            return
        if len(inventory_responses) >= 32:
            overflow = True
            return
        # Playwright's .headers may omit security-related headers; use
        # all_headers without ever serializing the bearer or source URL.
        inventory_responses.append((response.request.all_headers().get("authorization"), response))

    if diagnostic is not None:
        diagnostic["settings_role_phase"] = "relogin_wait_auth_and_inventory"
    page.on("response", observed)
    try:

        def login_progress(phase):
            if diagnostic is not None:
                diagnostic["settings_role_phase"] = "relogin_" + phase

        token = helpers._login(
            page, origin, account["username"], account["password"], progress=login_progress
        )
        if diagnostic is not None:
            diagnostic["settings_role_phase"] = "relogin_await_canonical_inventory"
        wanted = "Bearer " + token
        deadline = time.monotonic() + 12
        inventory = None
        while time.monotonic() < deadline:
            _require(not overflow, "Installed inventory response bound exceeded")
            inventory = next(
                (reply for bearer, reply in inventory_responses if bearer == wanted),
                None,
            )
            if inventory is not None:
                break
            page.wait_for_timeout(100)
        if diagnostic is not None:
            diagnostic["settings_relogin_response_count"] = len(inventory_responses)
            diagnostic["settings_relogin_stale_inventory_responses"] = sum(
                bearer != wanted for bearer, _ in inventory_responses
            )
        _require(inventory is not None, "New session did not load its own camera inventory")
        if diagnostic is not None:
            diagnostic["settings_role_phase"] = "relogin_inventory_response"
            diagnostic["settings_relogin_inventory_http_status"] = inventory.status
            diagnostic["settings_relogin_stale_inventory_responses"] = sum(
                bearer != wanted for bearer, _ in inventory_responses
            )
        _require(inventory.status == 200, "Canonical camera authority was unavailable")
        capabilities = inventory.json().get("capabilities", {})
    finally:
        page.remove_listener("response", observed)
    if diagnostic is not None:
        diagnostic["settings_role_phase"] = "relogin_core_identity_request"
    status, core = helpers._api(page, token, "GET", "/api/v1/auth/me")
    if diagnostic is not None:
        diagnostic["settings_relogin_core_http_status"] = status
    _require(
        status == 200
        and core.get("id") == account["id"]
        and "effective_permissions" not in core
        and "role_profile_id" not in core
        and capabilities.get("account", {}).get("id") == core["id"],
        "Installed GUI permission proof did not use the real core account contract",
    )
    if diagnostic is not None:
        diagnostic["settings_role_phase"] = "relogin_security_tab_visible"
    page.locator("#security-tab").wait_for(state="visible")
    if diagnostic is not None:
        diagnostic["settings_role_phase"] = "relogin_complete"
    return token, capabilities


def _cameras(page):
    if page.locator("#settings-panel").is_visible():
        page.locator("#settings-return-button").click()
    elif page.locator("#admin-panel").is_visible():
        page.locator("#admin-return-button").click()
    page.locator("#wall-tab").click()


def _totp(secret):
    """Independent RFC6238 reference calculation for the displayed enrollment."""
    raw = base64.b32decode(secret + "=" * ((8 - len(secret) % 8) % 8))
    digest = hmac.digest(raw, struct.pack(">Q", int(time.time()) // 30), "sha1")
    offset = digest[-1] & 15
    return f"{(int.from_bytes(digest[offset : offset + 4], 'big') & 0x7FFFFFFF) % 1000000:06d}"


class _SignedIdentityProvider:
    """Temporary real OIDC HTTP server; no external tenant or private identity."""

    def __init__(self, callback):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.callback = callback
        self.subject = "generated-installed-subject-" + secrets.token_hex(8)
        self.client_id = "generated-installed-client"
        self.group = "generated-k5-operators"
        self.codes, self.lock = {}, threading.Lock()
        self.token_requests = 0
        self.pkce_verified = False
        self.failed = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, value, status=200):
                raw = json.dumps(value, allow_nan=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                try:
                    parts = urlsplit(self.path)
                    if parts.path == "/.well-known/openid-configuration":
                        self.respond(
                            {
                                "issuer": outer.issuer,
                                "authorization_endpoint": outer.issuer + "/authorize",
                                "token_endpoint": outer.issuer + "/token",
                                "jwks_uri": outer.issuer + "/keys",
                                "response_types_supported": ["code"],
                                "code_challenge_methods_supported": ["S256"],
                                "id_token_signing_alg_values_supported": ["RS256"],
                                "token_endpoint_auth_methods_supported": ["none"],
                            }
                        )
                    elif parts.path == "/keys":
                        key = jwt.algorithms.RSAAlgorithm.to_jwk(
                            outer.key.public_key(), as_dict=True
                        )
                        self.respond(
                            {
                                "keys": [
                                    {
                                        **key,
                                        "kid": "generated-installed-key",
                                        "use": "sig",
                                        "alg": "RS256",
                                    }
                                ]
                            }
                        )
                    elif parts.path == "/authorize":
                        values = parse_qs(parts.query)
                        if any(
                            values.get(k) != [v]
                            for k, v in {
                                "response_type": "code",
                                "scope": "openid",
                                "client_id": outer.client_id,
                                "redirect_uri": outer.callback,
                                "code_challenge_method": "S256",
                            }.items()
                        ):
                            raise ValueError
                        if any(
                            not 16 <= len(values[k][0]) <= 128
                            for k in ("state", "nonce", "code_challenge")
                        ):
                            raise ValueError
                        code = secrets.token_urlsafe(32)
                        with outer.lock:
                            outer.codes[code] = values
                        self.send_response(303)
                        self.send_header(
                            "Location",
                            outer.callback
                            + "?"
                            + urlencode({"state": values["state"][0], "code": code}),
                        )
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("Referrer-Policy", "no-referrer")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                    else:
                        self.respond({}, 404)
                except Exception:
                    outer.failed = True
                    self.respond({}, 400)

            def do_POST(self):
                try:
                    count = int(self.headers.get("Content-Length", "0"))
                    if (
                        self.path != "/token"
                        or not 1 <= count <= 16384
                        or self.headers.get("Authorization")
                    ):
                        raise ValueError
                    values = parse_qs(self.rfile.read(count).decode())
                    with outer.lock:
                        authorization = outer.codes.pop(values["code"][0])
                        outer.token_requests += 1
                    verifier = values["code_verifier"][0]
                    challenge = (
                        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                        .rstrip(b"=")
                        .decode()
                    )
                    if (
                        values["redirect_uri"] != authorization["redirect_uri"]
                        or values["client_id"] != [outer.client_id]
                        or challenge != authorization["code_challenge"][0]
                    ):
                        raise ValueError
                    outer.pkce_verified = True
                    now = int(time.time())
                    claims = {
                        "iss": outer.issuer,
                        "sub": outer.subject,
                        "aud": outer.client_id,
                        "iat": now,
                        "exp": now + 300,
                        "nonce": authorization["nonce"][0],
                        "groups": [outer.group],
                        "role": "administrator",
                    }
                    token = jwt.encode(
                        claims,
                        outer.key,
                        algorithm="RS256",
                        headers={"kid": "generated-installed-key"},
                    )
                    self.respond(
                        {
                            "id_token": token,
                            "access_token": "unused-generated-access-token",
                            "token_type": "Bearer",
                        }
                    )
                except Exception:
                    outer.failed = True
                    self.respond({}, 400)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.issuer = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        _require(not self.thread.is_alive(), "Generated identity-provider HTTP server did not stop")


def _mfa_login(page, origin, user, code, *, expected=200):
    __tracebackhide__ = True
    page.goto(origin + "/operator/")
    page.locator("#username").fill(user["username"])
    page.locator("#password").fill(user["password"])
    result = _result(
        page,
        "POST",
        "/api/v1/auth/login",
        lambda: page.locator("#sign-in-button").click(),
        expected=202,
    )
    _require(
        result.get("mfa_required") is True and "session_token" not in result,
        "First factor created a session before MFA",
    )
    page.locator("#mfa-sign-in-form").wait_for(state="visible")
    _require(page.locator("#workspace").is_hidden(), "First factor opened the installed workspace")
    page.locator("#mfa-sign-in-code").fill(code)
    result = _result(
        page,
        "POST",
        "/api/v1/auth/mfa",
        lambda: page.locator("#mfa-sign-in-submit").click(),
        expected=expected,
    )
    if expected == 200:
        page.locator("#workspace").wait_for(state="visible")
        _require(
            result.get("account", {}).get("id") == user["id"],
            "MFA authenticated a different account",
        )
    else:
        _require(
            page.locator("#workspace").is_hidden() and "session_token" not in result,
            "Recovery-code replay authenticated a session",
        )
    return result


def _create_provider(page, *, kind, camera_id):
    __tracebackhide__ = True
    _open(page, "integrations")
    page.locator("#integration-cancel").click()
    page.locator("#integration-name").fill("Generated installed " + kind)
    page.locator("#integration-kind").select_option(kind)
    page.locator(f'#integration-camera-choices input[value="{camera_id}"]').check()
    result = _result(
        page,
        "POST",
        _PREFIX + "/integrations",
        lambda: page.locator("#integration-save").click(),
        expected=201,
    )
    identifier, token = result.get("id"), result.get("setup_token")
    _require(
        isinstance(identifier, str) and isinstance(token, str) and 32 <= len(token) <= 128,
        "Installed integration did not issue a one-time provider credential",
    )
    page.locator("#integration-setup-token").wait_for(state="visible")
    _require(
        page.locator("#integration-setup-token-value").inner_text() == token,
        "Installed provider token was not displayed once",
    )
    page.locator("#integration-setup-token-done").click()
    _require(
        page.locator("#integration-setup-token-value").inner_text() == "",
        "Installed provider token remained visible",
    )
    _result(
        page,
        "PATCH",
        _PREFIX + f"/integrations/{identifier}",
        lambda: page.locator(f"#integration-toggle-{identifier}").click(),
    )
    return identifier, token


def run_installed_settings_journey(
    page,
    *,
    client,
    admin_helpers,
    browser_helpers,
    admin_username,
    admin_password,
    camera_ids,
    private_state,
    analytics_event,
    parent,
    diagnostic,
):
    """Exercise the same installed app and restore authority before restart checks."""
    __tracebackhide__ = True
    started = time.monotonic()
    evidence, denials = {}, []
    origin = client.origin
    operator = next(u for u in private_state["users"] if u["role"] == "operator")
    contexts_to_close = []
    idp = None
    provider_ids = []
    secret_values = [admin_password, operator["password"]]
    time_mutations = []

    def observed(request):
        p = urlsplit(request.url).path
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and (
            p == _PREFIX + "/time"
            or p.startswith(_PREFIX + "/time/")
            or p.endswith("/time")
            or p.endswith("/time/synchronize")
        ):
            time_mutations.append(True)

    def read(phase, path):
        """The same bounded browser observer used by the preceding admin journey."""
        diagnostic["settings_read_phase"] = phase
        began = time.monotonic()
        try:
            status, data = admin_helpers._api(page, client.token, "GET", path)
            diagnostic["settings_read_http_status"] = status
            _require(
                status == 200 and isinstance(data, dict),
                "Installed settings read did not return its required object",
            )
            return data
        except BaseException as error:
            diagnostic["settings_read_failure_type"] = (
                type(error).__name__
                if type(error).__name__
                in {
                    "TimeoutError",
                    "Error",
                    "Failed",
                    "AssertionError",
                    "RuntimeError",
                    "ValueError",
                }
                else "other"
            )
            raise
        finally:
            diagnostic["settings_read_seconds"] = round(time.monotonic() - began, 3)

    page.on("request", observed)
    try:
        diagnostic["settings_stage"] = "storage"
        _open(page, "storage")
        before = read("storage_snapshot", _PREFIX + "/storage")
        page.locator("#storage-quota").fill(str(before["quota_bytes"] // _GIB + 1))
        saved = _result(
            page, "PATCH", _PREFIX + "/storage", lambda: page.locator("#storage-save").click()
        )
        _require(
            saved["quota_bytes"] == before["quota_bytes"] + _GIB
            and saved["recording_directory"] == before["recording_directory"]
            and saved["location_status"] == "active",
            "Installed storage policy did not apply live",
        )
        evidence["settings_admin_gui"] = True
        evidence["settings_storage_policy_live"] = True
        page.locator("#storage-quota").fill(str(before["quota_bytes"] / _GIB))
        restored = _result(
            page, "PATCH", _PREFIX + "/storage", lambda: page.locator("#storage-save").click()
        )
        _require(
            all(
                restored[k] == before[k]
                for k in (
                    "quota_bytes",
                    "minimum_free_bytes",
                    "retention_seconds",
                    "recording_directory",
                )
            ),
            "Installed storage policy was not restored",
        )
        evidence["settings_storage_policy_restored"] = True

        diagnostic["settings_stage"] = "roles"
        diagnostic["settings_role_phase"] = "create_operator_browser"
        user_context, user_page = admin_helpers._new_user_page(page)
        contexts_to_close.append(user_context)
        diagnostic["settings_role_phase"] = "authenticate_operator"
        old_token = admin_helpers._login(
            user_page, origin, operator["username"], operator["password"]
        )
        diagnostic["settings_role_phase"] = "open_roles_panel"
        _open(page, "roles")
        diagnostic["settings_role_phase"] = "configure_restricted_role"
        page.locator("#roles-name").fill("Generated export restriction")
        page.locator("#roles-base").select_option("operator")
        page.locator('#roles-permissions input[value="cameras.export"]').uncheck()
        diagnostic["settings_role_phase"] = "create_restricted_role"
        created = _result(
            page,
            "POST",
            _PREFIX + "/roles",
            lambda: page.locator("#roles-save").click(),
            expected=201,
        )
        role_id = created["role"]["id"]
        diagnostic["settings_role_phase"] = "restricted_role_created"
        _cameras(page)
        admin_helpers._open_admin(page)
        row = page.locator(f'#admin-users-list [data-user-id="{operator["id"]}"]')
        row.locator('[data-user-action="edit"]').click()
        page.locator("#admin-role-profile").select_option(role_id)
        _result(
            page,
            "PUT",
            _PREFIX + f"/users/{operator['id']}/role-profile",
            lambda: page.locator("#admin-apply-role-profile").click(),
        )
        status, _ = admin_helpers._api(user_page, old_token, "GET", "/api/v1/operator/capabilities")
        _require(status == 401, "Custom-role assignment did not revoke the previous session")
        denials.append(status)
        token, capabilities = _canonical_login(
            user_page, admin_helpers, origin, operator, diagnostic=diagnostic
        )
        _require(
            capabilities["can_export"] is False
            and user_page.locator("#export-panel").is_hidden()
            and user_page.locator("#download-recording").is_hidden()
            and user_page.locator("#clip-mark-in").is_hidden()
            and user_page.locator("#admin-tab").is_hidden(),
            "Custom-role export restriction did not apply",
        )
        status, _ = admin_helpers._api(
            user_page,
            token,
            "POST",
            f"/api/v1/operator/cameras/{camera_ids[0]}/exports",
            {"start": analytics_event["occurred_at"], "duration": 1},
        )
        _require(status == 403, "Custom-role camera export escaped its narrowed authority")
        denials.append(status)
        evidence.update(
            settings_custom_role_assignment=True,
            settings_custom_role_permission_denied=True,
            settings_role_revoked_sessions=True,
        )
        row.locator('[data-user-action="edit"]').click()
        original_name = page.locator("#admin-display-name").input_value()
        page.locator("#admin-display-name").fill("Generated profiled account")
        edited = _result(
            page,
            "PATCH",
            _PREFIX + f"/users/{operator['id']}",
            lambda: page.locator("#admin-save-user").click(),
        )
        _require(
            edited["display_name"] == "Generated profiled account"
            and edited["role_profile_id"] == role_id,
            "Profile-bound account edit did not preserve authority",
        )
        row.locator('[data-user-action="edit"]').click()
        page.locator("#admin-display-name").fill(original_name)
        _result(
            page,
            "PATCH",
            _PREFIX + f"/users/{operator['id']}",
            lambda: page.locator("#admin-save-user").click(),
        )
        row.locator('[data-user-action="edit"]').click()
        page.locator("#admin-cancel-user").click()
        _open(page, "roles")
        page.locator("#roles-name").fill("Generated time administration only")
        page.locator("#roles-base").select_option("administrator")
        for permission in page.locator("#roles-permissions input[type=checkbox]").all():
            permission.set_checked(permission.get_attribute("value") == "time.manage")
        time_role = _result(
            page,
            "POST",
            _PREFIX + "/roles",
            lambda: page.locator("#roles-save").click(),
            expected=201,
        )["role"]["id"]
        _cameras(page)
        admin_helpers._open_admin(page)
        row.locator('[data-user-action="edit"]').click()
        page.locator("#admin-role-profile").select_option(time_role)
        _result(
            page,
            "PUT",
            _PREFIX + f"/users/{operator['id']}/role-profile",
            lambda: page.locator("#admin-apply-role-profile").click(),
        )
        _, time_capabilities = _canonical_login(
            user_page, admin_helpers, origin, operator, diagnostic=diagnostic
        )
        _require(
            time_capabilities["effective_permissions"] == ["time.manage"]
            and user_page.locator("#admin-tab").inner_text() == "Recorder settings",
            "Time-only administrator did not receive its narrowed GUI authority",
        )
        user_page.locator("#admin-tab").click()
        user_page.locator("#settings-time-panel").wait_for(state="visible")
        user_page.wait_for_function(
            "document.querySelector('#settings-time-panel').getAttribute('aria-busy') === 'false'"
        )
        _require(
            user_page.locator("#admin-users-tab").is_hidden()
            and user_page.locator("#settings-storage-tab").is_hidden()
            and user_page.locator("#settings-roles-tab").is_hidden()
            and user_page.locator("#add-camera-button").is_hidden()
            and user_page.locator("#export-panel").is_hidden()
            and "Windows Time service:" in user_page.locator("#time-host-health").inner_text(),
            "Time-only administrator could not open its first allowed settings panel",
        )
        row.locator('[data-user-action="edit"]').click()
        page.locator("#admin-role-profile").select_option("")
        diagnostic["settings_role_phase"] = "unbind_time_profile"
        with page.expect_response(
            lambda response: _route(response, "GET", _PREFIX + "/users")
        ) as refreshed:
            unbound = _result(
                page,
                "PUT",
                _PREFIX + f"/users/{operator['id']}/role-profile",
                lambda: page.locator("#admin-apply-role-profile").click(),
            )
        _require(
            unbound.get("account", {}).get("role_profile_id") is None
            and unbound["account"].get("role") == "administrator"
            and refreshed.value.status == 200,
            "Installed time profile did not unbind before account restoration",
        )
        fresh_account = next(
            (u for u in refreshed.value.json().get("users", []) if u.get("id") == operator["id"]),
            None,
        )
        _require(
            fresh_account is not None
            and fresh_account.get("role_profile_id") is None
            and fresh_account.get("role") == "administrator",
            "Installed account list did not read back the unbound administrator",
        )
        diagnostic["settings_role_phase"] = "await_unbound_account_refresh"
        page.locator("#admin-user-dialog").wait_for(state="hidden")
        page.wait_for_function(
            """id => { const row = document.querySelector('tr[data-user-id="'+id+'"]');
              return row
                && document.querySelector('#admin-users-list').getAttribute('aria-busy') === 'false'
                && !row.querySelector('[data-user-action="edit"]').disabled; }""",
            arg=operator["id"],
        )
        row.locator('[data-user-action="edit"]').click()
        diagnostic["settings_role_phase"] = "restore_operator_role"
        page.locator("#admin-role").select_option("operator")
        diagnostic["settings_role_phase"] = "restore_operator_grants"
        if operator["camera_access"]["mode"] == "selected":
            admin_helpers._set_access(page, operator["camera_access"]["camera_ids"])
        else:
            page.locator("#admin-access-mode").select_option("all")
        diagnostic["settings_role_phase"] = "save_restored_operator"
        restored_role = _result(
            page,
            "PATCH",
            _PREFIX + f"/users/{operator['id']}",
            lambda: page.locator("#admin-save-user").click(),
        )
        _require(
            restored_role["role"] == "operator" and restored_role["role_profile_id"] is None,
            "Time-only GUI acceptance did not restore the original operator",
        )
        diagnostic["settings_role_phase"] = "restored_operator"
        evidence["settings_role_ui_permissions"] = True
        evidence["settings_profile_bound_edit"] = True
        _open(page, "roles")
        page.locator(f"#roles-edit-{time_role}").click()
        _result(
            page,
            "DELETE",
            _PREFIX + f"/roles/{time_role}",
            lambda: page.locator("#roles-delete").click(),
            expected=204,
        )
        _open(page, "roles")
        page.locator(f"#roles-edit-{role_id}").click()
        _result(
            page,
            "DELETE",
            _PREFIX + f"/roles/{role_id}",
            lambda: page.locator("#roles-delete").click(),
            expected=204,
        )

        diagnostic["settings_stage"] = "time_read_only"
        _open(page, "time")
        _require(
            "Windows Time service:" in page.locator("#time-host-health").inner_text(),
            "Installed time administration was not visible",
        )
        _require(
            not time_mutations, "Installed settings acceptance unexpectedly requested a time write"
        )
        evidence.update(settings_time_status_visible=True, settings_time_no_clock_changes=True)

        diagnostic["settings_stage"] = "mfa"
        admin_helpers._login(user_page, origin, operator["username"], operator["password"])
        user_page.locator("#security-tab").click()
        user_page.locator("#security-password").fill(operator["password"])
        enrollment = _result(
            user_page,
            "POST",
            "/api/v1/operator/security/mfa/enroll",
            lambda: user_page.locator("#security-enroll").click(),
        )
        secret = enrollment["secret"]
        secret_values.append(secret)
        user_page.locator("#security-confirm-code").fill(_totp(secret))
        confirmed = _result(
            user_page,
            "POST",
            "/api/v1/operator/security/mfa/confirm",
            lambda: user_page.locator("#security-confirm").click(),
        )
        recovery_codes = confirmed["recovery_codes"]
        secret_values.extend(recovery_codes)
        _require(
            len(recovery_codes) == 10 and len(set(recovery_codes)) == 10,
            "Installed MFA did not issue ten distinct recovery codes",
        )
        user_page.locator("#security-recovery").wait_for(state="visible")
        _require(
            all(
                code in user_page.locator("#security-recovery-codes").inner_text()
                for code in recovery_codes
            ),
            "Installed MFA recovery codes were not displayed once",
        )
        user_page.locator("#security-recovery-done").click()
        authenticated = _mfa_login(user_page, origin, operator, recovery_codes[0])
        _require(authenticated["account"]["role"] == "operator", "MFA changed the account's role")
        replay_context, replay = admin_helpers._new_user_page(page)
        contexts_to_close.append(replay_context)
        rejected = _mfa_login(replay, origin, operator, recovery_codes[0], expected=401)
        _require("session_token" not in rejected, "A used MFA recovery code issued a session")
        denials.append(401)
        user_page.locator("#security-tab").click()
        user_page.locator("#security-password").fill(operator["password"])
        user_page.locator("#security-code").fill(recovery_codes[1])
        disabled = _result(
            user_page,
            "POST",
            "/api/v1/operator/security/mfa/disable",
            lambda: user_page.locator("#security-disable").click(),
        )
        _require(
            disabled["mfa_enabled"] is False,
            "Installed MFA did not disable with a distinct recovery code",
        )
        admin_helpers._login(user_page, origin, operator["username"], operator["password"])
        evidence.update(
            settings_mfa_enrollment=True,
            settings_mfa_challenge=True,
            settings_mfa_recovery_single_use=True,
            settings_mfa_disabled=True,
            settings_mfa_recovery_codes=len(recovery_codes),
        )
        # These browser contexts have completed every role/MFA assertion.
        # Release their Edge processes before starting the separate signed
        # OIDC callback journey instead of leaving unused sessions polling.
        diagnostic["settings_browser_context_stage"] = "closing_completed_role_mfa"
        replay_context.close()
        contexts_to_close.remove(replay_context)
        user_context.close()
        contexts_to_close.remove(user_context)
        diagnostic["settings_browser_context_stage"] = "role_mfa_closed"

        diagnostic["settings_stage"] = "oidc"
        _open(page, "sso")
        callback = page.locator("#sso-callback-uri").input_value()
        _require(
            callback == origin + "/api/v1/auth/oidc/callback",
            "Installed OIDC callback did not match the local gateway",
        )
        idp = _SignedIdentityProvider(callback)
        page.locator("#sso-display-name").fill("Generated installed identity")
        page.locator("#sso-issuer").fill(idp.issuer)
        page.locator("#sso-client-id").fill(idp.client_id)
        page.locator("#sso-enabled").check()
        _result(page, "PUT", _PREFIX + "/sso/provider", lambda: page.locator("#sso-save").click())
        tested = _result(
            page, "POST", _PREFIX + "/sso/provider/test", lambda: page.locator("#sso-test").click()
        )
        _require(
            tested.get("compatible") is True and tested.get("issuer_verified") is True,
            "Installed OIDC provider test did not verify its issuer",
        )
        page.locator("#sso-link-subject").fill(idp.subject)
        page.locator("#sso-link-user").select_option(operator["id"])
        page.locator("#sso-link-group").fill(idp.group)
        linked = _result(
            page,
            "POST",
            _PREFIX + "/sso/links",
            lambda: page.locator("#sso-link-save").click(),
            expected=201,
        )
        oidc_context, oidc_page = admin_helpers._new_user_page(page)
        contexts_to_close.append(oidc_context)
        oidc_page.goto(origin + "/operator/")
        oidc_page.locator("#sso-sign-in").wait_for(state="visible")
        # Observe only bounded route-class counts. Never retain the generated
        # authorization URL, callback query, credentials, or issued token.
        oidc_route_counts = {
            "start_request": 0,
            "start_response": 0,
            "authorize_request": 0,
            "callback_request": 0,
            "complete_request": 0,
            "complete_response": 0,
        }

        def observe_oidc_request(request):
            route = urlsplit(request.url).path
            key = {
                "/api/v1/auth/oidc/start": "start_request",
                "/authorize": "authorize_request",
                "/api/v1/auth/oidc/callback": "callback_request",
                "/api/v1/auth/oidc/complete": "complete_request",
            }.get(route)
            if key is not None:
                oidc_route_counts[key] = min(32, oidc_route_counts[key] + 1)

        def observe_oidc_response(response):
            route = urlsplit(response.url).path
            key = {
                "/api/v1/auth/oidc/start": "start_response",
                "/api/v1/auth/oidc/complete": "complete_response",
            }.get(route)
            if key is not None:
                oidc_route_counts[key] = min(32, oidc_route_counts[key] + 1)

        oidc_page.on("request", observe_oidc_request)
        oidc_page.on("response", observe_oidc_response)
        diagnostic["settings_oidc_phase"] = "await_signed_browser_completion"
        try:
            issued = _result(
                oidc_page,
                "POST",
                "/api/v1/auth/oidc/complete",
                lambda: oidc_page.locator("#sso-sign-in-button").click(),
            )
        except BaseException:
            diagnostic["settings_oidc_route_counts"] = dict(oidc_route_counts)
            diagnostic["settings_oidc_idp_token_requests"] = min(32, idp.token_requests)
            diagnostic["settings_oidc_idp_pkce_verified"] = bool(idp.pkce_verified)
            diagnostic["settings_oidc_idp_failed"] = bool(idp.failed)
            raise
        finally:
            oidc_page.remove_listener("request", observe_oidc_request)
            oidc_page.remove_listener("response", observe_oidc_response)
        diagnostic["settings_oidc_phase"] = "signed_browser_completion_received"
        oidc_page.locator("#workspace").wait_for(state="visible")
        _require(
            issued.get("account", {}).get("id") == operator["id"]
            and issued["account"]["role"] == "operator"
            and "sso_code" not in oidc_page.url,
            "Installed OIDC did not preserve the explicitly linked role",
        )
        _require(
            idp.pkce_verified and idp.token_requests == 1 and not idp.failed,
            "Installed OIDC did not complete actual signed PKCE exchange",
        )
        _result(
            page,
            "DELETE",
            _PREFIX + f"/sso/links/{linked['id']}",
            lambda: page.locator(f"#sso-unlink-{linked['id']}").click(),
            expected=204,
        )
        page.locator("#sso-enabled").uncheck()
        _result(page, "PUT", _PREFIX + "/sso/provider", lambda: page.locator("#sso-save").click())
        evidence.update(
            settings_oidc_signed_pkce=True,
            settings_oidc_existing_role=True,
            settings_oidc_token_requests=idp.token_requests,
        )
        idp.close()
        idp = None

        diagnostic["settings_stage"] = "integrations"
        neural_id, neural_token = _create_provider(page, kind="neural", camera_id=camera_ids[0])
        provider_ids.append(neural_id)
        secret_values.append(neural_token)
        neural = K5LabBridgeClient(origin, UUID(neural_id), neural_token)
        neural.handshake()
        deadline = time.monotonic() + 10
        requests = []
        while time.monotonic() < deadline and not requests:
            requests = neural.observations()["requests"]
            if not requests:
                page.wait_for_timeout(100)
        _require(
            1 <= len(requests) <= 20,
            "Installed Neural handoff did not expose committed recorded observations",
        )
        item = requests[0]
        request = item["request"]
        response = {
            k: request[k] for k in ("contract_version", "request_id", "partition", "record_id")
        } | {
            "status": "recommendation",
            "reason_code": "generated_review",
            "operation": "review",
            "confidence": 0.7,
            "authorized": False,
        }
        bad = page.request.post(
            origin + f"/api/v1/operator/integrations/{neural_id}/responses",
            headers={"X-Integration-Token": neural_token},
            data={"request": request, "response": response | {"authorized": True}},
            timeout=10000,
        )
        _require(
            bad.status == 422, "Installed Neural handoff accepted unauthorized device authority"
        )
        denials.append(422)
        accepted = neural.respond(request, response)
        _require(
            accepted.get("authorized") is False,
            "Installed Neural response acquired device authority",
        )
        evidence.update(
            settings_neural_v2_roundtrip=True, settings_neural_observations=len(requests)
        )
        analytic_id, analytic_token = _create_provider(
            page, kind="analytics", camera_id=camera_ids[0]
        )
        provider_ids.append(analytic_id)
        secret_values.append(analytic_token)
        analytic = K5LabBridgeClient(origin, UUID(analytic_id), analytic_token)
        analytic.analytics_handshake()
        event_id = str(uuid4())
        stamp = analytics_event["occurred_at"]
        event = {
            "schema_version": "0.1",
            "event_id": event_id,
            "analytic_id": "person_down",
            "event_type": "person_down_candidate",
            "source_id": camera_ids[0],
            "start_time": stamp,
            "end_time": stamp,
            "confidence": 0.96,
            "severity": "high",
            "status": "candidate",
            "objects": [],
            "zones": [],
            "observations": [],
            "evidence": [],
            "relationships": [],
            "recommended_actions": [],
            "metadata": {},
        }
        accepted = analytic.ingest_analytic(event)
        _require(
            accepted.get("accepted") is True,
            "Installed Analytics handoff did not accept its published schema",
        )
        event_id = accepted["event_id"]
        _result(
            page,
            "POST",
            _PREFIX + f"/integrations/{analytic_id}/test",
            lambda: page.locator(f"#integration-test-{analytic_id}").click(),
        )
        _cameras(page)
        browser_helpers._open_camera_watch(page, camera_ids[0])
        page.locator("#history-tab").click()
        button = page.locator(
            f'#analytics-provider-events [data-provider-event-id="{event_id}"] button'
        )
        button.wait_for(state="visible", timeout=15000)
        button.click()
        expected_ms = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() * 1000
        try:
            page.wait_for_function(
                """chosen => {
                  const v=document.querySelector('#playback-video');
                  return Math.abs(Date.parse(v.dataset.utcStart||'')-chosen)<1000 &&
                    v.readyState>=2 && v.getVideoPlaybackQuality().totalVideoFrames>=4;
                }""",
                arg=expected_ms,
                timeout=20000,
            )
        except Exception:
            # Bounded installed GUI state only; never include media source URLs.
            diagnostic["settings_provider_playback_snapshot"] = page.evaluate(
                """chosen => {
                  const v=document.querySelector('#playback-video');
                  const start=Date.parse(v?.dataset.utcStart||'');
                  const from=Date.parse(document.getElementById('history-start')?.value||'');
                  const to=Date.parse(document.getElementById('history-end')?.value||'');
                  return {
                    event_button_visible:!!document.querySelector(
                      '#analytics-provider-events button'),
                    history_visible:!document.getElementById('history-panel')?.hidden,
                    history_intervals:document.querySelectorAll('#history-list button').length,
                    range_contains_event:Number.isFinite(from)&&Number.isFinite(to)&&from<=chosen&&chosen<to,
                    playback_start_delta_ms:Number.isFinite(start)?Math.round(start-chosen):null,
                    playback_ready_state:v?.readyState??null,
                    playback_frames:v?.getVideoPlaybackQuality?.().totalVideoFrames??null,
                    playback_paused:v?.paused??null,
                    playback_error_code:v?.error?.code??null,
                    playback_source_present:!!v?.currentSrc
                  };
                }""",
                expected_ms,
            )
            raise
        provider_frames = page.locator("#playback-video").evaluate(
            "v=>v.getVideoPlaybackQuality().totalVideoFrames"
        )
        provider_events = read(
            "provider_events", f"/api/v1/operator/cameras/{camera_ids[0]}/integration-events"
        )["events"]
        _require(
            any(
                e["event_id"] == event_id
                and e["status"] == "candidate"
                and datetime.fromisoformat(e["observed_at"].replace("Z", "+00:00"))
                == datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                for e in provider_events
            ),
            "Installed provider event changed status or source UTC",
        )
        evidence.update(
            settings_integrations_gui=True,
            settings_analytics_machine_event=True,
            settings_provider_event_playback=True,
            settings_provider_event_count=len(provider_events),
            settings_provider_playback_frames=provider_frames,
        )
        _open(page, "integrations")
        for identifier in reversed(provider_ids):
            _result(
                page,
                "DELETE",
                _PREFIX + f"/integrations/{identifier}",
                lambda identifier=identifier: page.locator(
                    f"#integration-delete-{identifier}"
                ).click(),
                expected=204,
            )
        revoked = page.request.post(
            origin + f"/api/v1/operator/integrations/{neural_id}/handshake",
            headers={"X-Integration-Token": neural_token},
            data={"supported_versions": ["2"]},
            timeout=10000,
        )
        _require(revoked.status == 401, "Removed provider credential retained machine access")
        denials.append(revoked.status)

        diagnostic["settings_stage"] = "audit"
        _open(page, "activity")
        directory = parent / "normal audit download"
        observer = browser_helpers._normal_download_directory(page, directory)
        try:
            with page.expect_download() as downloaded:
                page.locator("#activity-export").click()
            download = downloaded.value
            _require(
                download.failure() is None and download.suggested_filename == "k5-activity.json",
                "Installed audit did not produce its normal named browser download",
            )
            path = directory / download.suggested_filename
            _require(
                path.is_file() and not list(directory.glob("*.crdownload")),
                "Installed audit browser download did not exist as a complete file",
            )
            raw = path.read_bytes()
            _require(
                0 < len(raw) <= 16 * 1024**2, "Installed audit download exceeded its bounded size"
            )
            audit = json.loads(raw)
            _require(
                5 <= len(audit["events"]) <= 5000
                and all(value not in raw.decode() for value in secret_values),
                "Installed audit was empty or exposed a fixture credential",
            )
            evidence.update(settings_audit_download=True, settings_audit_rows=len(audit["events"]))
        finally:
            observer.detach()
        _require(
            denials == [401, 403, 401, 422, 401],
            "Installed settings denial evidence did not match expected authority boundaries",
        )
        evidence["settings_denied_http_statuses"] = denials
        _require(
            read("baseline_storage", _PREFIX + "/storage")["quota_bytes"] == before["quota_bytes"]
            and read("baseline_roles", _PREFIX + "/roles")["roles"] == []
            and read("baseline_integrations", _PREFIX + "/integrations")["connections"] == []
            and read("baseline_sso", _PREFIX + "/sso/provider")["enabled"] is False,
            "Installed settings baseline was not restored before restart",
        )
        status, accounts = admin_helpers._api(page, client.token, "GET", _PREFIX + "/users")
        restored_operator = next(u for u in accounts["users"] if u["id"] == operator["id"])
        _require(
            status == 200
            and restored_operator["role"] == "operator"
            and restored_operator["role_profile_id"] is None
            and restored_operator["camera_access"]["mode"] == operator["camera_access"]["mode"]
            and set(restored_operator["camera_access"]["camera_ids"])
            == set(operator["camera_access"]["camera_ids"]),
            "Installed role profile and camera grants were not restored to baseline",
        )
        evidence["settings_restored_before_restart"] = True
        evidence["settings_journey_seconds"] = round(time.monotonic() - started, 3)
        _require(
            0 < evidence["settings_journey_seconds"] <= 180,
            "Installed settings acceptance exceeded its bounded duration",
        )
        diagnostic["settings_stage"] = "complete"
        return evidence
    except BaseException as error:
        site = None
        tb = error.__traceback__
        while tb is not None:
            if tb.tb_frame.f_code.co_name == "run_installed_settings_journey":
                site = tb.tb_lineno
            tb = tb.tb_next
        if site is not None:
            diagnostic["settings_failure_line"] = site
        diagnostic["settings_failure_type"] = (
            type(error).__name__
            if type(error).__name__
            in {"TimeoutError", "Error", "Failed", "AssertionError", "RuntimeError", "ValueError"}
            else "other"
        )
        raise
    finally:
        diagnostic["settings_elapsed_seconds"] = round(time.monotonic() - started, 3)
        page.remove_listener("request", observed)
        for context in reversed(contexts_to_close):
            context.close()
        if idp is not None:
            idp.close()
        if diagnostic.get("settings_stage") == "complete":
            _cameras(page)
