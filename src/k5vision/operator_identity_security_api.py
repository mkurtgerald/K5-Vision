"""Current-user second-factor controls and pre-session challenge completion."""

from __future__ import annotations

import asyncio

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from k5vision.auth_sessions import UserSessionIssue
from k5vision.operator_audit import set_audit_actor
from k5vision.operator_identity_security import AccountSecurityError, AccountSecurityUnavailable
from k5vision.user_admin_api import _UserRateLimiter

_PREFIX = "/api/v1/operator/security"


class _Password(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(min_length=1, max_length=256, repr=False)


class _Factor(_Password):
    code: str = Field(min_length=1, max_length=64, repr=False)


class _Enrollment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enrollment_token: str = Field(
        min_length=32, max_length=128, pattern=r"^[A-Za-z0-9_-]+$", repr=False
    )
    code: str = Field(min_length=6, max_length=6, pattern=r"^[0-9]{6}$", repr=False)


class _Challenge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    challenge_token: str = Field(
        min_length=32, max_length=128, pattern=r"^[A-Za-z0-9_-]+$", repr=False
    )
    code: str = Field(min_length=1, max_length=64, repr=False)


def install_account_security_api(
    app: FastAPI, *, security, sessions, require_session, authenticate, admit_origin, mutations
) -> None:
    """Install mandatory state before enabling the password-login MFA hook."""
    if security is None:
        raise ValueError("Account security state must be initialized")
    security.bind_sessions(sessions)
    app.state.operator_identity_security = security
    session_dependency = Depends(require_session)
    limiter = _UserRateLimiter(limit=60, window_seconds=60)
    challenge_mutations = asyncio.Lock()

    async def origin(request: Request):
        admit_origin(request)

    mutation_dependencies = [Depends(origin)]
    previous_validation_handler = app.exception_handlers.get(
        RequestValidationError, request_validation_exception_handler
    )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        if request.url.path.startswith(_PREFIX) or request.url.path == "/api/v1/auth/mfa":
            return JSONResponse(
                {"detail": "account_security_invalid_request"},
                status_code=422,
                headers={"Cache-Control": "no-store"},
            )
        return await previous_validation_handler(request, error)

    def audit(action, actor, outcome="success"):
        recorder = getattr(app.state, "operator_audit", None)
        if recorder is not None:
            recorder.record(action=action, actor=actor, outcome=outcome)

    def reject(error):
        if isinstance(error, AccountSecurityUnavailable):
            return HTTPException(503, "account_security_unavailable")
        return HTTPException(403, "account_security_not_accepted")

    @app.get(_PREFIX, tags=["account security"])
    async def status(response: Response, session=session_dependency):
        response.headers["Cache-Control"] = "no-store"
        current, _ = await authenticate(session[1])
        try:
            value = security.store.status(current.id)
            value.pop("generation", None)
            oidc = getattr(app.state, "operator_oidc", None)
            sso_available = oidc is not None and oidc.store.provider().get("enabled") is True
            return {**value, "sso_available": sso_available}
        except AccountSecurityError:
            raise HTTPException(503, "account_security_unavailable") from None

    @app.post(
        _PREFIX + "/mfa/enroll", tags=["account security"], dependencies=mutation_dependencies
    )
    async def enroll(payload: _Password, response: Response, session=session_dependency):
        response.headers["Cache-Control"] = "no-store"
        async with mutations:
            current, _ = await authenticate(session[1])
            try:
                return security.begin_enrollment(current, payload.current_password)
            except AccountSecurityError as error:
                raise reject(error) from None

    @app.post(
        _PREFIX + "/mfa/confirm", tags=["account security"], dependencies=mutation_dependencies
    )
    async def confirm(payload: _Enrollment, response: Response, session=session_dependency):
        response.headers["Cache-Control"] = "no-store"
        async with mutations:
            current, _ = await authenticate(session[1])
            try:
                codes = security.confirm_enrollment(current, payload.enrollment_token, payload.code)
            except AccountSecurityError as error:
                raise reject(error) from None
            await sessions.revoke_user(current.id)
            audit("security.mfa_enabled", current)
            return {"mfa_enabled": True, "recovery_codes": codes, "reauthentication_required": True}

    @app.post(
        _PREFIX + "/mfa/disable", tags=["account security"], dependencies=mutation_dependencies
    )
    async def disable(payload: _Factor, response: Response, session=session_dependency):
        response.headers["Cache-Control"] = "no-store"
        async with mutations:
            current, _ = await authenticate(session[1])
            try:
                security.reauthenticate_factor(current, payload.current_password, payload.code)
                security.store.disable(current.id)
            except AccountSecurityError as error:
                raise reject(error) from None
            await sessions.revoke_user(current.id)
            audit("security.mfa_disabled", current)
            return {"mfa_enabled": False, "reauthentication_required": True}

    @app.post(
        _PREFIX + "/mfa/recovery-codes",
        tags=["account security"],
        dependencies=mutation_dependencies,
    )
    async def recovery_codes(payload: _Factor, response: Response, session=session_dependency):
        response.headers["Cache-Control"] = "no-store"
        async with mutations:
            current, _ = await authenticate(session[1])
            try:
                security.reauthenticate_factor(current, payload.current_password, payload.code)
                codes = security.store.replace_recovery(current.id)
            except AccountSecurityError as error:
                raise reject(error) from None
            audit("security.mfa_recovery_replaced", current)
            return {"recovery_codes": codes}

    @app.post(
        "/api/v1/auth/mfa",
        response_model=UserSessionIssue,
        tags=["authentication"],
        dependencies=mutation_dependencies,
    )
    async def complete(payload: _Challenge, response: Response):
        response.headers["Cache-Control"] = "no-store"
        retry = await limiter.consume()
        if retry is not None:
            raise HTTPException(
                429, "account_security_rate_limit", headers={"Retry-After": str(retry)}
            )
        async with challenge_mutations:
            try:
                account, version, generation, epoch, authority_check = security.complete_challenge(
                    payload.challenge_token, payload.code
                )
                set_audit_actor(account)
            except AccountSecurityUnavailable:
                raise HTTPException(503, "account_security_unavailable") from None
            except AccountSecurityError:
                raise HTTPException(401, "account_security_not_accepted") from None
            token, expires = await sessions.issue(account, credential_version=version)
            try:
                current = await sessions.resolve(token)
                security.validate_issued_factor(
                    account, version, generation, epoch, authority_check
                )
                unchanged = current is not None
            except AccountSecurityUnavailable:
                await sessions.revoke(token)
                raise HTTPException(503, "account_security_unavailable") from None
            except AccountSecurityError:
                await sessions.revoke(token)
                raise HTTPException(401, "account_security_not_accepted") from None
            except Exception:
                await sessions.revoke(token)
                raise HTTPException(503, "account_security_unavailable") from None
            if current is None or not unchanged:
                await sessions.revoke(token)
                raise HTTPException(401, "account_security_not_accepted")
            audit("security.mfa_authenticated", current)
            return UserSessionIssue(
                account=current,
                session_token=token,
                expires_at=expires,
                expires_in_seconds=sessions.ttl_seconds,
            )
