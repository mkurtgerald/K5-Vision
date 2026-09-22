"""Bounded administrator API over the durable K5 user registry.

This is an interim control-plane containment boundary, not a full human session
implementation. A dedicated administrator bearer credential may perform only the
bounded user-administration operations exposed here. New accounts receive a
short-lived one-time bootstrap credential and must replace it before password
verification can succeed.
"""

from __future__ import annotations

import asyncio
from collections import deque
from hmac import compare_digest
from math import ceil
from os import environ
from pathlib import Path
from time import monotonic
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response, status
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from k5vision.domain.users import (
    UserAccount,
    UserAuditEvent,
    UserBootstrapIssue,
    UserBootstrapPasswordChange,
    UserCreate,
    UserPatch,
)
from k5vision.services.user_registry import (
    DEFAULT_BOOTSTRAP_TTL_SECONDS,
    DEFAULT_USER_CAPACITY,
    MAX_BOOTSTRAP_TTL_SECONDS,
    MAX_USER_AUDIT_PAGE_SIZE,
    MAX_USER_CAPACITY,
    MAX_USER_PAGE_SIZE,
    MIN_BOOTSTRAP_TTL_SECONDS,
    UserRegistry,
    UserRegistryCapacityError,
    UserRegistryConflictError,
    UserRegistryStorageError,
)

USER_ADMIN_TOKEN_ENV = "K5_CONTROL_PLANE_ADMIN_TOKEN"
USER_DB_PATH_ENV = "K5_USER_DB_PATH"
MAX_USER_REQUEST_BYTES = 16_384
DEFAULT_USER_ADMIN_RATE_LIMIT = 120
DEFAULT_BOOTSTRAP_RATE_LIMIT = 30
DEFAULT_USER_ADMIN_RATE_WINDOW_SECONDS = 60.0
MAX_USER_ADMIN_RATE_LIMIT = 10_000
MAX_USER_ADMIN_RATE_WINDOW_SECONDS = 3_600.0
_USER_AUDIT_ACTOR = "control-plane-admin"
_BOOTSTRAP_AUDIT_ACTOR = "bootstrap-user"


class _UserRequestBodyTooLarge(Exception):
    pass


class _BoundedUserRequestBody:
    """Reject oversized user credential and mutation bodies before model parsing."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    @staticmethod
    def _is_user_mutation(scope: Scope) -> bool:
        if scope["type"] != "http":
            return False
        method = scope.get("method")
        path = scope.get("path", "")
        return (
            (method == "POST" and path == "/api/v1/users")
            or (method == "POST" and path == "/api/v1/auth/bootstrap-password")
            or (method == "PATCH" and path.startswith("/api/v1/users/"))
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._is_user_mutation(scope):
            await self.app(scope, receive, send)
            return

        for name, value in scope.get("headers", []):
            if name.lower() != b"content-length":
                continue
            try:
                declared = int(value)
            except ValueError:
                await self._reject(send, status.HTTP_400_BAD_REQUEST, "Invalid request length")
                return
            if declared < 0:
                await self._reject(send, status.HTTP_400_BAD_REQUEST, "Invalid request length")
                return
            if declared > self.max_bytes:
                await self._reject(
                    send,
                    status.HTTP_413_CONTENT_TOO_LARGE,
                    "Request body too large",
                )
                return

        received_bytes = 0
        response_started = False

        async def bounded_receive() -> Message:
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > self.max_bytes:
                    raise _UserRequestBodyTooLarge
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, bounded_receive, tracking_send)
        except _UserRequestBodyTooLarge:
            if response_started:
                raise
            await self._reject(
                send,
                status.HTTP_413_CONTENT_TOO_LARGE,
                "Request body too large",
            )

    @staticmethod
    async def _reject(send: Send, response_status: int, detail: str) -> None:
        body = (f'{{"detail":"{detail}"}}').encode()
        await send(
            {
                "type": "http.response.start",
                "status": response_status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class _UserAdminRateLimiter:
    """Bound user-administration requests without retaining credentials or client identity."""

    def __init__(self, *, limit: int, window_seconds: float) -> None:
        self._limit = limit
        self._window_seconds = window_seconds
        self._events: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def consume(self) -> int | None:
        now = monotonic()
        cutoff = now - self._window_seconds
        async with self._lock:
            while self._events and self._events[0] <= cutoff:
                self._events.popleft()
            if len(self._events) >= self._limit:
                return max(1, ceil(self._window_seconds - (now - self._events[0])))
            self._events.append(now)
        return None


def _resolve_token(explicit_token: str | None) -> tuple[str | None, bool]:
    raw_token = explicit_token if explicit_token is not None else environ.get(USER_ADMIN_TOKEN_ENV)
    if raw_token is None:
        return None, False
    token = raw_token.strip()
    valid = bool(token and token.isascii())
    return (token if valid else None), True


def _resolve_database_path(explicit_path: str | Path | None) -> str | None:
    raw_path = explicit_path if explicit_path is not None else environ.get(USER_DB_PATH_ENV)
    if raw_path is None:
        return None
    path = str(raw_path).strip()
    return path if path and path != ":memory:" else None


def install_user_admin_api(
    application: FastAPI,
    *,
    site_id: str | None,
    reserved_tokens: tuple[str, ...] = (),
    admin_token: str | None = None,
    user_db_path: str | Path | None = None,
    user_capacity: int = DEFAULT_USER_CAPACITY,
    max_request_bytes: int = MAX_USER_REQUEST_BYTES,
    rate_limit: int = DEFAULT_USER_ADMIN_RATE_LIMIT,
    bootstrap_rate_limit: int = DEFAULT_BOOTSTRAP_RATE_LIMIT,
    bootstrap_ttl_seconds: int = DEFAULT_BOOTSTRAP_TTL_SECONDS,
    rate_window_seconds: float = DEFAULT_USER_ADMIN_RATE_WINDOW_SECONDS,
) -> UserRegistry | None:
    """Install a fail-closed, site-scoped user administration surface."""
    if not 1 <= user_capacity <= MAX_USER_CAPACITY:
        raise ValueError(f"user_capacity must be between 1 and {MAX_USER_CAPACITY}")
    if not 1024 <= max_request_bytes <= 1_048_576:
        raise ValueError("max_request_bytes must be between 1024 and 1048576")
    if not 1 <= rate_limit <= MAX_USER_ADMIN_RATE_LIMIT:
        raise ValueError(f"rate_limit must be between 1 and {MAX_USER_ADMIN_RATE_LIMIT}")
    if not 1 <= bootstrap_rate_limit <= MAX_USER_ADMIN_RATE_LIMIT:
        raise ValueError(f"bootstrap_rate_limit must be between 1 and {MAX_USER_ADMIN_RATE_LIMIT}")
    if not MIN_BOOTSTRAP_TTL_SECONDS <= bootstrap_ttl_seconds <= MAX_BOOTSTRAP_TTL_SECONDS:
        raise ValueError(
            f"bootstrap_ttl_seconds must be between {MIN_BOOTSTRAP_TTL_SECONDS} "
            f"and {MAX_BOOTSTRAP_TTL_SECONDS}"
        )
    if not 1.0 <= rate_window_seconds <= MAX_USER_ADMIN_RATE_WINDOW_SECONDS:
        raise ValueError(
            f"rate_window_seconds must be between 1.0 and {MAX_USER_ADMIN_RATE_WINDOW_SECONDS}"
        )

    resolved_token, token_configured = _resolve_token(admin_token)
    token_is_distinct = bool(
        resolved_token is not None
        and all(not compare_digest(resolved_token, reserved) for reserved in reserved_tokens)
    )
    auth_configuration_valid = token_configured and token_is_distinct

    database_path = _resolve_database_path(user_db_path)
    registry: UserRegistry | None = None
    if site_id is not None and database_path is not None:
        try:
            registry = UserRegistry(
                capacity=user_capacity,
                database_path=database_path,
                site_id=site_id,
            )
        except UserRegistryStorageError:
            registry = None

    limiter = _UserAdminRateLimiter(limit=rate_limit, window_seconds=rate_window_seconds)
    bootstrap_limiter = _UserAdminRateLimiter(
        limit=bootstrap_rate_limit,
        window_seconds=rate_window_seconds,
    )
    application.add_middleware(_BoundedUserRequestBody, max_bytes=max_request_bytes)
    application.state.user_registry = registry

    async def require_user_admin(
        authorization: Annotated[list[str] | None, Header()] = None,
    ) -> None:
        if not auth_configuration_valid or resolved_token is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="User administration authentication is not configured",
            )

        header = authorization[0] if authorization and len(authorization) == 1 else ""
        scheme, separator, credential = header.partition(" ")
        credential_valid = bool(
            scheme.lower() == "bearer" and separator and credential and credential.isascii()
        )
        if not credential_valid or not compare_digest(credential, resolved_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if registry is None or site_id is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="User administration state is not configured",
            )
        retry_after = await limiter.consume()
        if retry_after is not None:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="User administration rate limit exceeded",
                headers={"Retry-After": str(retry_after)},
            )

    async def require_bootstrap_budget() -> None:
        if registry is None or site_id is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="User administration state is not configured",
            )
        retry_after = await bootstrap_limiter.consume()
        if retry_after is not None:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Bootstrap request rate limit exceeded",
                headers={"Retry-After": str(retry_after)},
            )

    def require_registry() -> UserRegistry:
        if registry is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="User registry unavailable",
            )
        return registry

    admin_auth = [Depends(require_user_admin)]
    bootstrap_budget = [Depends(require_bootstrap_budget)]

    @application.get(
        "/api/v1/users",
        response_model=list[UserAccount],
        tags=["users"],
        dependencies=admin_auth,
    )
    async def list_users(
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_USER_PAGE_SIZE)] = MAX_USER_PAGE_SIZE,
    ) -> list[UserAccount]:
        try:
            return require_registry().list(offset=offset, limit=limit)
        except UserRegistryStorageError:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="User registry unavailable",
            ) from None

    @application.get(
        "/api/v1/users/audit",
        response_model=list[UserAuditEvent],
        tags=["users"],
        dependencies=admin_auth,
    )
    async def list_user_audit(
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_USER_AUDIT_PAGE_SIZE)] = MAX_USER_AUDIT_PAGE_SIZE,
    ) -> list[UserAuditEvent]:
        try:
            return require_registry().audit_events(offset=offset, limit=limit)
        except UserRegistryStorageError:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="User registry unavailable",
            ) from None

    @application.post(
        "/api/v1/users",
        response_model=UserBootstrapIssue,
        status_code=status.HTTP_201_CREATED,
        tags=["users"],
        dependencies=admin_auth,
    )
    async def create_user(payload: UserCreate) -> UserBootstrapIssue:
        try:
            account, temporary_credential, expires_at = require_registry().create_with_bootstrap(
                payload,
                actor=_USER_AUDIT_ACTOR,
                ttl_seconds=bootstrap_ttl_seconds,
            )
        except UserRegistryCapacityError:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="User registry capacity reached",
            ) from None
        except UserRegistryConflictError:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Username already exists",
            ) from None
        except UserRegistryStorageError:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="User registry unavailable",
            ) from None
        return UserBootstrapIssue(
            account=account,
            temporary_credential=temporary_credential,
            expires_at=expires_at,
        )

    @application.post(
        "/api/v1/auth/bootstrap-password",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["authentication"],
        dependencies=bootstrap_budget,
    )
    async def set_initial_password(payload: UserBootstrapPasswordChange) -> Response:
        try:
            activated = require_registry().activate_bootstrap_password(
                username=payload.username,
                temporary_credential=payload.temporary_credential,
                new_password=payload.new_password,
                actor=_BOOTSTRAP_AUDIT_ACTOR,
            )
        except UserRegistryStorageError:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="User registry unavailable",
            ) from None
        if activated is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired bootstrap credential",
            )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @application.patch(
        "/api/v1/users/{user_id}",
        response_model=UserAccount,
        tags=["users"],
        dependencies=admin_auth,
    )
    async def update_user(user_id: UUID, payload: UserPatch) -> UserAccount:
        try:
            user = require_registry().update(user_id, payload, actor=_USER_AUDIT_ACTOR)
        except UserRegistryStorageError:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="User registry unavailable",
            ) from None
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        return user

    return registry
