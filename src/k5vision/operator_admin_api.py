"""Human administrator account controls for the installed Windows workspace."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from k5vision.domain.users import UserAccount, UserCreate, UserPatch, UserRole
from k5vision.services.device_registry import DeviceRegistryStorageError
from k5vision.services.user_registry import (
    UserRegistryCapacityError,
    UserRegistryConflictError,
    UserRegistryLastAdministratorError,
    UserRegistryStorageError,
)
from k5vision.user_admin_api import _UserRateLimiter

_PREFIX = "/api/v1/operator/admin"


class _CameraAccess(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["all", "selected"]
    camera_ids: list[UUID] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def coherent(self):
        if len(set(self.camera_ids)) != len(self.camera_ids) or (
            self.mode == "all" and self.camera_ids
        ):
            raise ValueError("Invalid camera access")
        return self


class _CreateUser(UserCreate):
    enabled: StrictBool = True
    camera_access: _CameraAccess | None = None


class _UpdateUser(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    role: UserRole | None = None
    enabled: StrictBool | None = None
    camera_access: _CameraAccess | None = None

    @model_validator(mode="after")
    def has_change(self):
        if all(
            getattr(self, name) is None
            for name in ("display_name", "role", "enabled", "camera_access")
        ):
            raise ValueError("A user change is required")
        return self


class _ResetUser(BaseModel):
    model_config = ConfigDict(extra="forbid")


def install_operator_admin_api(
    app: FastAPI, *, users, sessions, registry, require_admin, authenticate, mutations
):
    """Use the canonical registry, audit and session authority; no service token UI."""
    admin_session = Depends(require_admin)
    limiter = _UserRateLimiter(limit=120, window_seconds=60)

    async def current_admin(session):
        current, token = await authenticate(session[1])
        if current.id != session[0].id or current.role is not UserRole.ADMINISTRATOR:
            raise HTTPException(403, "Insufficient user administration permission")
        return current, token

    async def mutation_admin(session):
        retry = await limiter.consume()
        if retry is not None:
            raise HTTPException(429, "admin_rate_limit", headers={"Retry-After": str(retry)})
        return await current_admin(session)

    def row(account):
        access = users.get_camera_access(account.id)
        if access is None:
            raise HTTPException(404, "admin_user_not_found")
        return {
            **account.model_dump(mode="json"),
            "credential_status": users.credential_status(account.id),
            "camera_access": {
                "mode": access["mode"],
                "camera_ids": [str(value) for value in access["camera_ids"]],
            },
        }

    def policy(value, role):
        access = value or _CameraAccess(
            mode="all" if role is UserRole.ADMINISTRATOR else "selected"
        )
        if role is UserRole.ADMINISTRATOR and access.mode != "all":
            raise HTTPException(422, "admin_invalid_request")
        try:
            known = all(registry.get(identifier) is not None for identifier in access.camera_ids)
        except DeviceRegistryStorageError:
            raise HTTPException(503, "admin_unavailable") from None
        if not known:
            raise HTTPException(422, "admin_invalid_request")
        return {"mode": access.mode, "camera_ids": list(access.camera_ids)}

    def registry_error(error):
        if isinstance(error, UserRegistryLastAdministratorError):
            return HTTPException(409, "admin_last_administrator")
        if isinstance(error, UserRegistryCapacityError):
            return HTTPException(409, "admin_capacity")
        if isinstance(error, UserRegistryConflictError):
            return HTTPException(409, "admin_username_exists")
        if isinstance(error, ValueError):
            return HTTPException(422, "admin_invalid_request")
        return HTTPException(503, "admin_unavailable")

    def target(user_id):
        try:
            account = users.get(user_id)
        except UserRegistryStorageError:
            raise HTTPException(503, "admin_unavailable") from None
        if account is None:
            raise HTTPException(404, "admin_user_not_found")
        return account

    def access_identity(value):
        return value["mode"], frozenset(value["camera_ids"])

    @app.get(_PREFIX + "/users", tags=["operator administration"])
    async def list_users(
        response: Response,
        offset: int = Query(default=0, ge=0, le=100000),
        limit: int = Query(default=25, ge=1, le=100),
        _session: tuple[UserAccount, str] = admin_session,
    ):
        response.headers["Cache-Control"] = "no-store"
        try:
            return {
                "users": [row(account) for account in users.list(offset=offset, limit=limit)],
                "has_more": bool(users.list(offset=offset + limit, limit=1)),
                "offset": offset,
                "limit": limit,
            }
        except UserRegistryStorageError:
            raise HTTPException(503, "admin_unavailable") from None

    @app.get(_PREFIX + "/audit", tags=["operator administration"])
    async def audit(
        response: Response,
        offset: int = Query(default=0, ge=0, le=10000000),
        limit: int = Query(default=25, ge=1, le=100),
        _session: tuple[UserAccount, str] = admin_session,
    ):
        response.headers["Cache-Control"] = "no-store"
        try:
            events = users.audit_events(offset=offset, limit=limit, newest_first=True)
            entries = []
            for event in events:
                account = users.get(event.user_id)
                entries.append(
                    {
                        **event.model_dump(mode="json"),
                        "username": None if account is None else account.username,
                    }
                )
            return {
                "events": entries,
                "has_more": bool(
                    users.audit_events(offset=offset + limit, limit=1, newest_first=True)
                ),
                "offset": offset,
                "limit": limit,
            }
        except UserRegistryStorageError:
            raise HTTPException(503, "admin_unavailable") from None

    @app.post(_PREFIX + "/users", status_code=201, tags=["operator administration"])
    async def create_user(
        payload: _CreateUser,
        response: Response,
        session: tuple[UserAccount, str] = admin_session,
    ):
        response.headers["Cache-Control"] = "no-store"
        async with mutations:
            actor, _ = await mutation_admin(session)
            access = policy(payload.camera_access, payload.role)
            try:
                request = UserCreate.model_validate(payload.model_dump(exclude={"camera_access"}))
                account, credential, expires = users.create_with_bootstrap(
                    request, actor=actor.username, camera_access=access
                )
                return {
                    "account": row(account),
                    "temporary_credential": credential,
                    "expires_at": expires,
                }
            except (
                UserRegistryStorageError,
                UserRegistryCapacityError,
                UserRegistryConflictError,
                ValueError,
            ) as error:
                raise registry_error(error) from None

    @app.patch(_PREFIX + "/users/{user_id}", tags=["operator administration"])
    async def update_user(
        user_id: UUID,
        payload: _UpdateUser,
        response: Response,
        session: tuple[UserAccount, str] = admin_session,
    ):
        response.headers["Cache-Control"] = "no-store"
        async with mutations:
            actor, _ = await mutation_admin(session)
            account = target(user_id)
            previous_account = account
            access = (
                policy(payload.camera_access, payload.role or account.role)
                if payload.camera_access is not None
                else None
            )
            try:
                previous_access = users.get_camera_access(user_id)
                fields = payload.model_dump(exclude={"camera_access"}, exclude_none=True)
                if fields:
                    account = users.update(
                        user_id,
                        UserPatch.model_validate(fields),
                        actor=actor.username,
                        camera_access=access,
                    )
                else:
                    users.set_camera_access(user_id, actor=actor.username, **access)
                if account is None:
                    raise HTTPException(404, "admin_user_not_found")
                changed_authority = (
                    account.role != previous_account.role
                    or account.enabled != previous_account.enabled
                    or access_identity(users.get_camera_access(user_id))
                    != access_identity(previous_access)
                )
                if changed_authority:
                    await sessions.revoke_user(user_id)
                    if user_id != actor.id:
                        await current_admin(session)
                return row(account)
            except (
                UserRegistryStorageError,
                UserRegistryLastAdministratorError,
                UserRegistryConflictError,
                ValueError,
            ) as error:
                raise registry_error(error) from None

    @app.put(_PREFIX + "/users/{user_id}/camera-access", tags=["operator administration"])
    async def set_camera_access(
        user_id: UUID,
        payload: _CameraAccess,
        response: Response,
        session: tuple[UserAccount, str] = admin_session,
    ):
        response.headers["Cache-Control"] = "no-store"
        async with mutations:
            actor, _ = await mutation_admin(session)
            account = target(user_id)
            access = policy(payload, account.role)
            try:
                previous_access = users.get_camera_access(user_id)
                users.set_camera_access(user_id, actor=actor.username, **access)
                if access_identity(previous_access) != access_identity(access):
                    await sessions.revoke_user(user_id)
                    if user_id != actor.id:
                        await current_admin(session)
                return row(account)
            except (UserRegistryStorageError, ValueError) as error:
                raise registry_error(error) from None

    @app.post(_PREFIX + "/users/{user_id}/reset-password", tags=["operator administration"])
    async def reset_password(
        user_id: UUID,
        response: Response,
        payload: _ResetUser | None = None,
        session: tuple[UserAccount, str] = admin_session,
    ):
        response.headers["Cache-Control"] = "no-store"
        async with mutations:
            actor, _ = await mutation_admin(session)
            try:
                result = users.reset_bootstrap(user_id, actor=actor.username)
                if result is None:
                    raise HTTPException(404, "admin_user_not_found")
                account, credential, expires = result
                await sessions.revoke_user(user_id)
                if user_id != actor.id:
                    await current_admin(session)
                return {
                    "account": row(account),
                    "temporary_credential": credential,
                    "expires_at": expires,
                }
            except (
                UserRegistryStorageError,
                UserRegistryLastAdministratorError,
                ValueError,
            ) as error:
                raise registry_error(error) from None

    return row
