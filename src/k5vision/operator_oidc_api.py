"""Graphical OIDC configuration, explicit identity links and browser sign-in."""

from __future__ import annotations

import asyncio
import ipaddress
import secrets
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from k5vision.auth_sessions import UserSessionIssue
from k5vision.operator_audit import set_audit_actor
from k5vision.operator_identity_security import (
    AccountSecurityError,
    AccountSecurityUnavailable,
    SecondFactorRequired,
)
from k5vision.operator_oidc import OIDCError
from k5vision.user_admin_api import _UserRateLimiter

_ADMIN = "/api/v1/operator/admin/sso"
_AUTH = "/api/v1/auth/oidc"
_COOKIE = "k5_oidc_flow"


class _Provider(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    enabled: StrictBool
    display_name: str = Field(min_length=1, max_length=80)
    issuer: str = Field(min_length=1, max_length=2048)
    client_id: str = Field(min_length=1, max_length=256)
    client_secret: str | None = Field(default=None, max_length=4096, repr=False)
    groups_claim: str = Field(default="groups", min_length=1, max_length=64)


class _Link(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str = Field(min_length=1, max_length=256)
    user_id: UUID
    required_group: str | None = Field(default=None, min_length=1, max_length=128)


class _Complete(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=32, max_length=128, pattern=r"^[A-Za-z0-9_-]+$", repr=False)


def install_oidc_api(
    app: FastAPI,
    *,
    oidc,
    security,
    users,
    sessions,
    require_admin,
    authenticate,
    admit_origin,
    public_port: int,
    mutations,
):
    if oidc is None or security is None:
        raise ValueError("Account security must precede federation")
    oidc.bind_security(security)
    app.state.operator_oidc = oidc
    admin_dependency = Depends(require_admin)
    limiter = _UserRateLimiter(limit=30, window_seconds=60)
    capacity = asyncio.Semaphore(2)

    async def origin(request: Request):
        admit_origin(request)

    origin_dependencies = [Depends(origin)]
    previous_validation = app.exception_handlers.get(
        RequestValidationError, request_validation_exception_handler
    )

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, error: RequestValidationError):
        if request.url.path.startswith((_ADMIN, _AUTH)):
            return JSONResponse(
                {"detail": "sso_invalid_request"},
                status_code=422,
                headers={"Cache-Control": "no-store"},
            )
        return await previous_validation(request, error)

    def callback_uri(request: Request):
        try:
            host = request.url.hostname
            if host != "localhost" and not ipaddress.ip_address(host or "").is_loopback:
                raise ValueError
            effective_port = request.url.port or (443 if request.url.scheme == "https" else 80)
            if request.url.scheme not in {"http", "https"} or effective_port != public_port:
                raise ValueError
            return str(request.base_url).rstrip("/") + _AUTH + "/callback"
        except ValueError:
            raise HTTPException(400, "sso_invalid_origin") from None

    def audit(action, actor, *, target_id=None, outcome="success"):
        recorder = getattr(app.state, "operator_audit", None)
        if recorder is not None:
            recorder.record(
                action=action,
                actor=actor,
                outcome=outcome,
                target_id=target_id,
                target_kind="security",
            )

    async def budget():
        retry = await limiter.consume()
        if retry is not None:
            raise HTTPException(429, "sso_rate_limit", headers={"Retry-After": str(retry)})

    @app.get(_ADMIN + "/provider", tags=["single sign-on"])
    async def provider(request: Request, response: Response, session=admin_dependency):
        response.headers["Cache-Control"] = "no-store"
        await authenticate(session[1])
        try:
            return {**oidc.store.provider(), "redirect_uri": callback_uri(request)}
        except AccountSecurityError:
            raise HTTPException(503, "sso_unavailable") from None

    @app.put(_ADMIN + "/provider", tags=["single sign-on"])
    async def configure(payload: _Provider, response: Response, session=admin_dependency):
        response.headers["Cache-Control"] = "no-store"
        async with mutations:
            current, _ = await authenticate(session[1])
            try:
                value = oidc.store.configure(**payload.model_dump())
            except (AccountSecurityError, ValueError):
                raise HTTPException(422, "sso_configuration_refused") from None
            # Existing federated users are ordinary currently scoped K5 users;
            # provider changes must revoke already-issued sessions too.
            for link in oidc.store.links():
                await sessions.revoke_user(UUID(link["user_id"]))
            audit("security.sso_configured", current)
            return value

    @app.post(_ADMIN + "/provider/test", tags=["single sign-on"])
    async def test_provider(response: Response, session=admin_dependency):
        response.headers["Cache-Control"] = "no-store"
        async with capacity:
            try:
                value = await asyncio.to_thread(oidc.test)
            except (OIDCError, AccountSecurityError):
                raise HTTPException(422, "sso_provider_test_failed") from None
        await authenticate(session[1])
        return value

    @app.get(_ADMIN + "/links", tags=["single sign-on"])
    async def links(response: Response, session=admin_dependency):
        response.headers["Cache-Control"] = "no-store"
        await authenticate(session[1])
        try:
            return {"links": oidc.store.links()}
        except AccountSecurityError:
            raise HTTPException(503, "sso_unavailable") from None

    @app.post(_ADMIN + "/links", status_code=201, tags=["single sign-on"])
    async def link_user(payload: _Link, response: Response, session=admin_dependency):
        response.headers["Cache-Control"] = "no-store"
        async with mutations:
            current, _ = await authenticate(session[1])
            if users.active_session_snapshot(payload.user_id) is None:
                raise HTTPException(422, "sso_user_must_be_active")
            try:
                value = oidc.store.link(**payload.model_dump())
            except (AccountSecurityError, ValueError):
                raise HTTPException(422, "sso_link_refused") from None
            audit("security.sso_linked", current, target_id=payload.user_id)
            return value

    @app.delete(_ADMIN + "/links/{identifier}", status_code=204, tags=["single sign-on"])
    async def unlink_user(identifier: UUID, session=admin_dependency):
        async with mutations:
            current, _ = await authenticate(session[1])
            try:
                found = next(
                    (row for row in oidc.store.links() if row["id"] == str(identifier)), None
                )
                oidc.store.unlink(identifier)
                if found is not None:
                    await sessions.revoke_user(UUID(found["user_id"]))
            except AccountSecurityError:
                raise HTTPException(503, "sso_unavailable") from None
            audit(
                "security.sso_unlinked",
                current,
                target_id=None if found is None else UUID(found["user_id"]),
            )
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    @app.get(_AUTH, tags=["authentication"])
    async def available(response: Response):
        response.headers["Cache-Control"] = "no-store"
        try:
            value = oidc.store.provider()
            return {
                "available": value["configured"] and value["enabled"],
                "display_name": value["display_name"],
            }
        except AccountSecurityError:
            return {"available": False, "display_name": "Single sign-on"}

    @app.post(_AUTH + "/start", tags=["authentication"], dependencies=origin_dependencies)
    async def start(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        await budget()
        browser_token = secrets.token_urlsafe(32)
        async with capacity:
            try:
                url = await asyncio.to_thread(oidc.start, callback_uri(request), browser_token)
            except (OIDCError, AccountSecurityError):
                raise HTTPException(503, "sso_signin_unavailable") from None
        # No wall-clock Expires/MaxAge: elapsed state in the service is the
        # authority, preserving the application's clock-correction behavior.
        response.set_cookie(
            _COOKIE,
            browser_token,
            httponly=True,
            samesite="lax",
            path=_AUTH,
            secure=request.url.scheme == "https",
        )
        return {"authorization_url": url}

    @app.get(_AUTH + "/callback", tags=["authentication"])
    async def callback(
        request: Request,
        state: str = Query(min_length=32, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"),
        code: str = Query(min_length=1, max_length=4096),
    ):
        await budget()
        async with capacity:
            try:
                local_code = await asyncio.to_thread(
                    oidc.callback,
                    state=state,
                    code=code,
                    browser_token=request.cookies.get(_COOKIE, ""),
                )
            except (OIDCError, AccountSecurityError):
                audit("security.sso_authenticated", None, outcome="denied")
                return RedirectResponse(
                    "/operator/?sso_error=not_accepted",
                    status_code=303,
                    headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
                )
        return RedirectResponse(
            "/operator/?sso_code=" + local_code,
            status_code=303,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    @app.post(_AUTH + "/complete", tags=["authentication"], dependencies=origin_dependencies)
    async def complete(payload: _Complete, request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        await budget()
        async with mutations:
            try:
                account, version, authority_check = oidc.complete(
                    payload.code, request.cookies.get(_COOKIE, "")
                )
                set_audit_actor(account)
                primary_epoch = security.password_authenticated(
                    account, version, authority_check=authority_check
                )
            except SecondFactorRequired as challenge:
                return JSONResponse(
                    challenge.challenge, status_code=202, headers={"Cache-Control": "no-store"}
                )
            except AccountSecurityUnavailable:
                raise HTTPException(503, "sso_unavailable") from None
            except (OIDCError, AccountSecurityError):
                raise HTTPException(401, "sso_signin_not_accepted") from None
            token, expires = await sessions.issue(account, credential_version=version)
            try:
                current = await sessions.resolve(token)
                security.validate_primary_proof(account, version, primary_epoch, authority_check)
                factor_enabled = security.store.status(account.id)["mfa_enabled"]
                authority_current = authority_check()
            except AccountSecurityUnavailable:
                await sessions.revoke(token)
                raise HTTPException(503, "sso_unavailable") from None
            except AccountSecurityError:
                await sessions.revoke(token)
                raise HTTPException(401, "sso_signin_not_accepted") from None
            except Exception:
                await sessions.revoke(token)
                raise HTTPException(503, "sso_unavailable") from None
            if current is None or factor_enabled or not authority_current:
                await sessions.revoke(token)
                raise HTTPException(401, "sso_signin_not_accepted")
            response.delete_cookie(_COOKIE, path=_AUTH)
            audit("security.sso_authenticated", current)
            return UserSessionIssue(
                account=current,
                session_token=token,
                expires_at=expires,
                expires_in_seconds=sessions.ttl_seconds,
            )
