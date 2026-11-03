"""OIDC authorization-code/PKCE adapter for explicitly linked local accounts.

The maintained PyJWT/cryptography implementation verifies RS256. Endpoint URLs
come from the configured issuer's verified discovery document, without request
redirects; OIDC permits that trusted document to name other endpoint origins.
No external claim creates a user or changes K5 authority. Access and identity
tokens are transient and never enter browser URLs or durable storage.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID, uuid4

from k5vision.operator_identity_security import AccountSecurityError

_MAX_JSON = 1024 * 1024
_MAX_PENDING = 256
_HTTP_REQUEST_SECONDS = 15.0


class OIDCError(RuntimeError):
    """Finite federation failure, without endpoint, code or token disclosure."""


def _url(value: str, *, endpoint=False) -> str:
    if type(value) is not str or len(value) > 2048 or value.strip() != value:
        raise ValueError("Invalid identity provider URL")
    if not value.isascii() or any(ord(c) < 33 for c in value) or "\\" in value:
        raise ValueError("Invalid identity provider URL")
    parsed = urlsplit(value)
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Invalid identity provider URL")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    if parsed.hostname.casefold() in {"metadata.google.internal", "metadata"}:
        raise ValueError("Invalid identity provider URL")
    if address is not None and (
        address.is_link_local or address.is_unspecified or address.is_multicast
    ):
        raise ValueError("Invalid identity provider URL")
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and address is not None and address.is_loopback
    ):
        raise ValueError("Identity provider requires TLS or numeric loopback")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("Invalid identity provider URL")
    if "%" in parsed.netloc or (not endpoint and "%" in parsed.path):
        raise ValueError("Invalid identity provider URL")
    return value


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _document(pairs):
    output = {}
    for key, value in pairs:
        if key in output:
            raise ValueError
        output[key] = value
    return output


def _reject_constant(_):
    raise ValueError("Non-finite identity provider value")


def _fetch_json_in_process(
    url: str, *, form: dict | None = None, headers: dict | None = None
) -> dict:
    """Bound status, bytes, socket time, total read time, and redirects."""
    try:
        _url(url, endpoint=True)
        body = None if form is None else urlencode(form).encode("ascii")
        request = Request(
            url,
            data=body,
            headers={"Accept": "application/json", **(headers or {})},
        )
        if form is not None:
            request.add_header("Content-Type", "application/x-www-form-urlencoded")
        deadline = time.monotonic() + 12
        with build_opener(_NoRedirect()).open(request, timeout=5) as response:
            if response.status != 200:
                raise ValueError
            data = bytearray()
            while True:
                if time.monotonic() > deadline:
                    raise ValueError
                chunk = response.read(min(65536, _MAX_JSON + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > _MAX_JSON:
                    raise ValueError
        value = json.loads(data, object_pairs_hook=_document, parse_constant=_reject_constant)
        if type(value) is not dict:
            raise ValueError
        return value
    except HTTPError as error:
        error.close()
        raise OIDCError("Identity provider request failed") from None
    except Exception:
        raise OIDCError("Identity provider request failed") from None


def fetch_json(url: str, *, form: dict | None = None, headers: dict | None = None) -> dict:
    """Own a hidden request process through DNS, headers and the complete body.

    A socket inactivity timeout cannot stop a slowly trickling header/body. An
    external elapsed deadline kills and reaps this process regardless of which
    resolver, TLS, proxy, or read operation is blocked. Request secrets cross
    only private stdin, after Windows Job Object assignment.
    """
    from k5vision.operator_native_process import _WindowsJob

    process = job = None
    try:
        _url(url, endpoint=True)
        request = json.dumps(
            {"url": url, "form": form, "headers": headers}, ensure_ascii=True
        ).encode("ascii")
        if len(request) > 65536:
            raise ValueError
        job = _WindowsJob() if os.name == "nt" else None
        process = subprocess.Popen(
            [sys.executable, "-B", "-m", "k5vision.operator_oidc_request"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            start_new_session=os.name != "nt",
            close_fds=True,
        )
        if job is not None:
            job.assign(process)
        output, _ = process.communicate(request, timeout=_HTTP_REQUEST_SECONDS)
        if process.returncode != 0 or not 1 <= len(output) <= _MAX_JSON:
            raise ValueError
        value = json.loads(output, object_pairs_hook=_document, parse_constant=_reject_constant)
        if type(value) is not dict:
            raise ValueError
        return value
    except Exception:
        raise OIDCError("Identity provider request failed") from None
    finally:
        try:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=2)
        finally:
            if job is not None:
                job.close()
            if process is not None:
                for stream in (process.stdin, process.stdout):
                    if stream is not None:
                        stream.close()


class OIDCStore:
    """Share the mandatory private account-security database and admission."""

    def __init__(self, security_store):
        self._store = security_store

        def initialize(connection):
            connection.executescript(
                "BEGIN IMMEDIATE;"
                "CREATE TABLE IF NOT EXISTS oidc_provider (id INTEGER PRIMARY KEY CHECK(id=1),"
                "enabled INTEGER NOT NULL,display_name TEXT NOT NULL,issuer TEXT NOT NULL,"
                "client_id TEXT NOT NULL,client_secret BLOB NOT NULL,groups_claim TEXT NOT NULL,"
                "revision TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS oidc_links (id TEXT PRIMARY KEY,issuer TEXT NOT NULL,"
                "subject TEXT NOT NULL,user_id TEXT NOT NULL,required_group TEXT,"
                "revision TEXT NOT NULL,"
                "UNIQUE(issuer,subject));"
                "COMMIT;"
            )

        self._store._execute(initialize)
        self.provider()
        self.links()

    def provider(self, *, private=False) -> dict:
        def read(connection):
            row = connection.execute("SELECT * FROM oidc_provider WHERE id=1").fetchone()
            if row is None:
                return {"configured": False, "enabled": False, "display_name": "Single sign-on"}
            _url(row[3])
            if (
                row[1] not in (0, 1)
                or not re.fullmatch("[a-f0-9]{32}", row[7])
                or not 1 <= len(row[2]) <= 80
                or not 1 <= len(row[4]) <= 256
                or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", row[6])
                or any(ord(c) < 32 for c in row[2] + row[4])
            ):
                raise ValueError
            secret = self._store._protector.unprotect(row[5], "oidc-client-secret").decode("utf-8")
            if len(secret) > 4096:
                raise ValueError
            value = {
                "configured": True,
                "enabled": bool(row[1]),
                "display_name": row[2],
                "issuer": row[3],
                "client_id": row[4],
                "has_client_secret": bool(secret),
                "groups_claim": row[6],
                "revision": row[7],
            }
            if private:
                value["client_secret"] = secret
            return value

        return self._store._execute(read)

    def configure(
        self,
        *,
        enabled: bool,
        display_name: str,
        issuer: str,
        client_id: str,
        groups_claim: str = "groups",
        client_secret: str | None = None,
    ) -> dict:
        _url(issuer)
        if (
            type(enabled) is not bool
            or not 1 <= len(display_name) <= 80
            or not 1 <= len(client_id) <= 256
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", groups_claim)
            or any(ord(c) < 32 for c in display_name + client_id)
        ):
            raise ValueError("Identity provider configuration was refused")
        previous = self.provider(private=True)
        if client_secret is None:
            client_secret = (
                previous.get("client_secret", "")
                if previous.get("issuer") == issuer and previous.get("client_id") == client_id
                else ""
            )
        if type(client_secret) is not str or len(client_secret) > 4096:
            raise ValueError("Identity provider configuration was refused")
        protected = self._store._protector.protect(client_secret.encode(), "oidc-client-secret")

        def save(connection):
            connection.execute(
                "INSERT INTO oidc_provider VALUES(1,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "enabled=excluded.enabled,display_name=excluded.display_name,issuer=excluded.issuer,"
                "client_id=excluded.client_id,client_secret=excluded.client_secret,"
                "groups_claim=excluded.groups_claim,revision=excluded.revision",
                (
                    int(enabled),
                    display_name,
                    issuer,
                    client_id,
                    protected,
                    groups_claim,
                    secrets.token_hex(16),
                ),
            )

        self._store._execute(save)
        return self.provider()

    def links(self) -> list[dict]:
        def read(connection):
            rows = connection.execute(
                "SELECT * FROM oidc_links ORDER BY rowid LIMIT 1025"
            ).fetchall()
            if len(rows) > 1024:
                raise ValueError
            values = []
            for row in rows:
                UUID(row[0]), UUID(row[3])
                _url(row[1])
                if not 1 <= len(row[2]) <= 256 or not re.fullmatch("[a-f0-9]{32}", row[5]):
                    raise ValueError
                values.append(
                    dict(
                        zip(
                            ("id", "issuer", "subject", "user_id", "required_group", "revision"),
                            row,
                            strict=True,
                        )
                    )
                )
            return values

        return self._store._execute(read)

    def link(self, *, subject: str, user_id: UUID, required_group: str | None = None) -> dict:
        config = self.provider()
        if (
            not config["configured"]
            or type(subject) is not str
            or not 1 <= len(subject) <= 256
            or any(ord(c) < 32 for c in subject)
            or (
                required_group is not None
                and (
                    not 1 <= len(required_group) <= 128 or any(ord(c) < 32 for c in required_group)
                )
            )
        ):
            raise ValueError("Identity mapping was refused")
        identifier = str(uuid4())

        def save(connection):
            if connection.execute("SELECT count(*) FROM oidc_links").fetchone()[0] >= 1024:
                raise ValueError
            connection.execute(
                "INSERT INTO oidc_links VALUES(?,?,?,?,?,?)",
                (
                    identifier,
                    config["issuer"],
                    subject,
                    str(user_id),
                    required_group,
                    secrets.token_hex(16),
                ),
            )

        self._store._execute(save)
        return next(row for row in self.links() if row["id"] == identifier)

    def unlink(self, identifier: UUID) -> None:
        self._store._execute(
            lambda connection: connection.execute(
                "DELETE FROM oidc_links WHERE id=?", (str(identifier),)
            )
        )


@dataclass(frozen=True)
class _Flow:
    revision: str
    state: str
    nonce: str
    verifier: str
    redirect_uri: str
    browser_digest: bytes
    deadline: float
    authority_epoch: int


@dataclass(frozen=True)
class _Completion:
    account: object
    credential_version: bytes
    link_id: str
    link_revision: str
    provider_revision: str
    browser_digest: bytes
    deadline: float
    authority_epoch: int


class OIDCService:
    def __init__(self, *, store: OIDCStore, users, fetch=fetch_json, elapsed=time.monotonic):
        self.store, self.users, self._fetch = store, users, fetch
        self._elapsed = elapsed
        self._flows: dict[str, _Flow] = {}
        self._completions: dict[str, _Completion] = {}
        self._lock = threading.RLock()
        self._closed = False
        self._security = None

    def bind_security(self, security):
        with self._lock:
            if self._security is not None and self._security is not security:
                raise ValueError("Identity provider security is already bound")
            self._security = security

    def _admit_open(self):
        with self._lock:
            if self._closed:
                raise OIDCError("Identity provider is closed")

    def _prune(self):
        now = self._elapsed()
        for values in (self._flows, self._completions):
            for key in tuple(values):
                if values[key].deadline <= now:
                    del values[key]

    def _metadata(self, config):
        value = self._fetch(config["issuer"].rstrip("/") + "/.well-known/openid-configuration")
        if (
            value.get("issuer") != config["issuer"]
            or "code" not in value.get("response_types_supported", [])
            or "S256" not in value.get("code_challenge_methods_supported", [])
            or "RS256" not in value.get("id_token_signing_alg_values_supported", [])
        ):
            raise OIDCError("Identity provider contract is incompatible")
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            _url(value.get(key), endpoint=True)
        methods = value.get("token_endpoint_auth_methods_supported", ["client_secret_basic"])
        if ("client_secret_basic" if config["client_secret"] else "none") not in methods:
            raise OIDCError("Identity provider client authentication is incompatible")
        return value

    def test(self):
        try:
            self._admit_open()
            config = self.store.provider(private=True)
            if not config["configured"]:
                raise ValueError
            self._metadata(config)
            return {
                "reachable": True,
                "compatible": True,
                "issuer_verified": True,
                "pkce_s256": True,
                "signing_algorithm": "RS256",
            }
        except Exception:
            raise OIDCError("Identity provider test failed") from None

    def start(self, redirect_uri: str, browser_token: str) -> str:
        try:
            self._admit_open()
            authority_epoch = 0 if self._security is None else self._security.primary_started()
            _url(redirect_uri, endpoint=True)
            config = self.store.provider(private=True)
            if not config["configured"] or not config["enabled"]:
                raise ValueError
            metadata = self._metadata(config)
            state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .rstrip(b"=")
                .decode()
            )
            flow = _Flow(
                config["revision"],
                state,
                nonce,
                verifier,
                redirect_uri,
                hashlib.sha256(browser_token.encode()).digest(),
                self._elapsed() + 300,
                authority_epoch,
            )
            with self._lock:
                self._admit_open()
                self._prune()
                if len(self._flows) >= _MAX_PENDING:
                    raise ValueError
                self._flows[state] = flow
            return (
                metadata["authorization_endpoint"]
                + "?"
                + urlencode(
                    {
                        "response_type": "code",
                        "client_id": config["client_id"],
                        "redirect_uri": redirect_uri,
                        "scope": "openid",
                        "state": state,
                        "nonce": nonce,
                        "code_challenge": challenge,
                        "code_challenge_method": "S256",
                    }
                )
            )
        except Exception:
            raise OIDCError("Identity provider sign-in could not start") from None

    def callback(self, *, state: str, code: str, browser_token: str) -> str:
        with self._lock:
            self._admit_open()
            self._prune()
            flow = self._flows.pop(state, None)
        try:
            if (
                flow is None
                or not hmac.compare_digest(
                    flow.browser_digest, hashlib.sha256(browser_token.encode()).digest()
                )
                or type(code) is not str
                or not 1 <= len(code) <= 4096
            ):
                raise ValueError
            config = self.store.provider(private=True)
            if not config["enabled"] or config["revision"] != flow.revision:
                raise ValueError
            metadata = self._metadata(config)
            form = {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": flow.redirect_uri,
                "code_verifier": flow.verifier,
                "client_id": config["client_id"],
            }
            headers = {}
            if config["client_secret"]:
                basic = (
                    quote(config["client_id"], safe="")
                    + ":"
                    + quote(config["client_secret"], safe="")
                )
                headers["Authorization"] = "Basic " + base64.b64encode(basic.encode()).decode()
            tokens = self._fetch(metadata["token_endpoint"], form=form, headers=headers)
            encoded = tokens.get("id_token")
            if type(encoded) is not str or not 1 <= len(encoded) <= 32768:
                raise ValueError
            claims = self._verify(encoded, config, metadata, flow.nonce, tokens, code)
            links = [
                row
                for row in self.store.links()
                if row["issuer"] == config["issuer"] and row["subject"] == claims["sub"]
            ]
            if len(links) != 1:
                raise ValueError
            link = links[0]
            if link["required_group"] is not None:
                groups = claims.get(config["groups_claim"])
                if (
                    type(groups) is not list
                    or len(groups) > 128
                    or any(type(group) is not str or len(group) > 128 for group in groups)
                    or link["required_group"] not in groups
                ):
                    raise ValueError
            verified = self.users.active_session_snapshot(UUID(link["user_id"]))
            if verified is None:
                raise ValueError
            if self._security is not None:
                self._security.primary_current(*verified, flow.authority_epoch)
            completion = _Completion(
                *verified,
                link["id"],
                link["revision"],
                config["revision"],
                flow.browser_digest,
                self._elapsed() + 60,
                flow.authority_epoch,
            )
            local_code = secrets.token_urlsafe(32)
            with self._lock:
                self._admit_open()
                self._prune()
                if len(self._completions) >= _MAX_PENDING:
                    raise ValueError
                self._completions[local_code] = completion
            return local_code
        except Exception:
            raise OIDCError("Identity provider sign-in was not accepted") from None

    def _verify(self, encoded, config, metadata, nonce, tokens, code):
        import jwt

        header = jwt.get_unverified_header(encoded)
        if (
            header.get("alg") != "RS256"
            or header.get("crit")
            or header.get("typ", "JWT") != "JWT"
            or type(header.get("kid")) is not str
            or not 1 <= len(header["kid"]) <= 128
        ):
            raise ValueError
        jwks = self._fetch(metadata["jwks_uri"])
        keys = jwks.get("keys")
        if type(keys) is not list or not 1 <= len(keys) <= 20:
            raise ValueError
        selected = [key for key in keys if type(key) is dict and key.get("kid") == header["kid"]]
        if len(selected) != 1:
            raise ValueError
        key = selected[0]
        if (
            key.get("kty") != "RSA"
            or key.get("use", "sig") != "sig"
            or key.get("alg", "RS256") != "RS256"
            or any(name in key for name in ("d", "p", "q", "dp", "dq", "qi"))
            or type(key.get("n")) is not str
            or len(key["n"]) > 1024
            or type(key.get("e")) is not str
            or re.fullmatch(r"[A-Za-z0-9_-]{1,12}", key["e"]) is None
            or ("key_ops" in key and key["key_ops"] != ["verify"])
        ):
            raise ValueError
        public_key = jwt.PyJWK.from_dict(key, algorithm="RS256").key
        exponent = public_key.public_numbers().e
        if (
            not 2048 <= public_key.key_size <= 4096
            or not 3 <= exponent <= 2**32 - 1
            or exponent % 2 != 1
        ):
            raise ValueError
        claims = jwt.decode(
            encoded,
            public_key,
            algorithms=["RS256"],
            audience=config["client_id"],
            issuer=config["issuer"],
            leeway=30,
            options={"require": ["iss", "sub", "aud", "exp", "iat", "nonce"]},
        )
        if (
            type(claims.get("sub")) is not str
            or not 1 <= len(claims["sub"]) <= 256
            or type(claims.get("nonce")) is not str
            or not hmac.compare_digest(claims["nonce"], nonce)
            or type(claims["iat"]) is not int
            or type(claims["exp"]) is not int
            or not 0 < claims["exp"] - claims["iat"] <= 3600
        ):
            raise ValueError
        audience = claims["aud"] if type(claims["aud"]) is list else [claims["aud"]]
        if (
            not 1 <= len(audience) <= 8
            or any(type(value) is not str for value in audience)
            or (len(audience) > 1 and claims.get("azp") != config["client_id"])
            or ("azp" in claims and claims["azp"] != config["client_id"])
        ):
            raise ValueError
        for name, source in (("at_hash", tokens.get("access_token")), ("c_hash", code)):
            if name in claims:
                if type(source) is not str or not 1 <= len(source) <= 32768:
                    raise ValueError
                expected = (
                    base64.urlsafe_b64encode(hashlib.sha256(source.encode("ascii")).digest()[:16])
                    .rstrip(b"=")
                    .decode()
                )
                if type(claims[name]) is not str or not hmac.compare_digest(claims[name], expected):
                    raise ValueError
        return claims

    def _primary_current(self, pending):
        self._admit_open()
        config = self.store.provider()
        if not config["enabled"] or config["revision"] != pending.provider_revision:
            return None
        links = [
            value
            for value in self.store.links()
            if value["id"] == pending.link_id and value["revision"] == pending.link_revision
        ]
        if len(links) != 1 or links[0]["user_id"] != str(pending.account.id):
            return None
        if self._security is not None:
            return self._security.primary_current(
                pending.account, pending.credential_version, pending.authority_epoch
            )
        return self.users.resolve_session(pending.account, pending.credential_version)

    def _primary_authorized(self, pending):
        try:
            return self._primary_current(pending) is not None
        except Exception:
            return False

    def complete(self, code: str, browser_token: str):
        with self._lock:
            self._admit_open()
            self._prune()
            pending = self._completions.pop(code, None)
            if pending is None:
                raise OIDCError("Identity provider sign-in expired")
        try:
            if not hmac.compare_digest(
                pending.browser_digest, hashlib.sha256(browser_token.encode()).digest()
            ):
                raise ValueError
            current = self._primary_current(pending)
            if current is None:
                raise ValueError
            return current, pending.credential_version, lambda: self._primary_authorized(pending)
        except (AccountSecurityError, ValueError):
            raise OIDCError("Identity provider sign-in was not accepted") from None

    def close(self):
        with self._lock:
            self._closed = True
            self._flows.clear()
            self._completions.clear()
