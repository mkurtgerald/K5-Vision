"""Authenticated role profiles, with current authority on every mutation."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from k5vision.domain.users import UserPatch, UserRole
from k5vision.operator_role_policy import BASE_PERMISSIONS, RolePolicyError
from k5vision.services.user_registry import (
    UserRegistryLastAdministratorError,
    UserRegistryStorageError,
)

_REQUEST_PERMISSION = ContextVar("operator_request_permission", default=None)


class OperatorPermissionContext:
    """Keep route authority bound across awaits and streamed responses."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        permission = request_permission(scope.get("path", ""), scope.get("method", ""))
        if scope.get("path") == "/api/v1/operator/media-auth":
            uris = [v for k, v in scope.get("headers", ()) if k == b"x-forwarded-uri"]
            if len(uris) == 1:
                if uris[0].startswith(b"/media/live/"):
                    permission = "cameras.view_live"
                elif uris[0].startswith(b"/media/playback?"):
                    permission = "cameras.playback"
        token = _REQUEST_PERMISSION.set(permission)
        try:
            await self.app(scope, receive, send)
        finally:
            _REQUEST_PERMISSION.reset(token)


def require_current_permission(app, account):
    permission = _REQUEST_PERMISSION.get()
    if permission is not None:
        require_permission(app, account, permission)


def current_request_permission():
    """Return the finite route classification without reading durable state."""
    return _REQUEST_PERMISSION.get()


@dataclass(frozen=True, slots=True)
class CurrentPermissionSnapshot:
    account_id: UUID
    role: UserRole
    required: str
    permissions: frozenset[str]
    policy: object | None
    generation: int | None


class PermissionSnapshotChanged(HTTPException):
    """Only a completed local policy update invalidated this positive read."""

    def __init__(self):
        super().__init__(503, "roles_unavailable")


def current_permission_snapshot(app, account):
    """Read a single request's durable role authority on its owned worker."""
    required = _REQUEST_PERMISSION.get()
    if required is None:
        return None
    policy = getattr(app.state, "operator_roles", None)
    try:
        permissions, generation = (
            (BASE_PERMISSIONS[account.role], None)
            if policy is None
            else policy.permission_snapshot(account)
        )
    except RolePolicyError:
        raise HTTPException(503, "roles_unavailable") from None
    return CurrentPermissionSnapshot(
        account.id, account.role, required, frozenset(permissions), policy, generation
    )


def validate_current_permission_snapshot(app, account, snapshot):
    """Check one completed read after awaits, without file I/O or lock waits."""
    required = _REQUEST_PERMISSION.get()
    if required is None:
        if snapshot is not None:
            raise HTTPException(503, "roles_unavailable")
        return
    policy = getattr(app.state, "operator_roles", None)
    if (
        not isinstance(snapshot, CurrentPermissionSnapshot)
        or snapshot.account_id != account.id
        or snapshot.role is not account.role
        or snapshot.required != required
        or snapshot.policy is not policy
    ):
        raise HTTPException(503, "roles_unavailable")
    if policy is not None:
        generation = policy.permission_generation
        if snapshot.generation != generation:
            if (
                type(snapshot.generation) is int
                and type(generation) is int
                and snapshot.generation < generation
                and generation == policy.completed_permission_generation
                and required in snapshot.permissions
            ):
                raise PermissionSnapshotChanged()
            raise HTTPException(503, "roles_unavailable")
    if required not in snapshot.permissions:
        raise HTTPException(403, "Insufficient operator permission")


class _Role(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=256)
    base_role: UserRole
    permissions: list[str] = Field(max_length=12)

    @model_validator(mode="after")
    def bounded_permissions(self):
        if (
            len(self.permissions) != len(set(self.permissions))
            or not set(self.permissions) <= BASE_PERMISSIONS[self.base_role]
            or any(ord(c) < 32 for c in self.name + self.description)
        ):
            raise ValueError("Role permissions are invalid")
        return self


class _RoleUpdate(_Role):
    expected_revision: int = Field(ge=1, lt=2**63, strict=True)


class _Assignment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role_profile_id: UUID | None
    expected_revision: int | None = Field(default=None, ge=1, lt=2**63, strict=True)


def effective_permissions(app, account):
    policy = getattr(app.state, "operator_roles", None)
    try:
        return BASE_PERMISSIONS[account.role] if policy is None else policy.permissions(account)
    except RolePolicyError:
        raise HTTPException(503, "roles_unavailable") from None


def require_permission(app, account, permission):
    if permission not in effective_permissions(app, account):
        raise HTTPException(403, "Insufficient operator permission")


def require_grant(app, actor, permissions):
    """Delegation cannot create or take over authority the actor does not hold."""
    if not set(permissions) <= effective_permissions(app, actor):
        raise HTTPException(403, "Insufficient authority for requested account or role")


def request_permission(path: str, method: str):
    """Map product routes explicitly; human authentication stays authoritative."""
    prefix = "/api/v1/operator/"
    if not path.startswith(prefix):
        return None
    relative = path[len(prefix) :].strip("/")
    if relative.startswith("admin/"):
        section = relative.split("/", 2)[1]
        if relative.endswith("/role-profile"):
            return "roles.manage"
        return {
            "users": "users.manage",
            "roles": "roles.manage",
            "audit": "audit.read",
            "activity": "audit.read",
            "storage": "storage.manage",
            "time": "time.manage",
            "integrations": "integrations.manage",
            "identity": "users.manage",
            "sso": "users.manage",
        }.get(section)
    parts = relative.split("/")
    if parts[0] == "cameras":
        if len(parts) >= 3:
            operation = parts[2]
            if operation == "exports":
                return "cameras.export"
            if operation in {"history", "events", "integration-events"}:
                return "cameras.playback"
            if operation == "person-view":
                return "cameras.view_live"
            if operation == "recording":
                return "recording.manage"
            if operation == "time":
                return "time.manage"
            if operation == "video-settings":
                return "cameras.manage"
            if operation == "analytics" and method not in {"GET", "HEAD"}:
                return "analytics.manage"
        if method not in {"GET", "HEAD"} and (len(parts) == 1 or parts[-1] != "media-session"):
            return "cameras.manage"
    if relative in {"camera-discovery", "camera-profiles", "camera-stream-probe"}:
        return "cameras.manage"
    return None


def install_operator_role_api(
    app, *, policy, users, sessions, require_admin, authenticate, mutations
):
    dependency = Depends(require_admin)
    prefix = "/api/v1/operator/admin"

    async def actor(session):
        current, _ = await authenticate(session[1])
        require_permission(app, current, "roles.manage")
        return current

    def failure(error):
        if isinstance(error, RolePolicyError):
            code = str(error)
            if code in {"roles_not_found", "roles_user_not_found"}:
                return HTTPException(404, code)
            if code in {
                "roles_revision_conflict",
                "roles_in_use",
                "roles_last_administrator",
                "roles_profile_bound",
                "roles_base_immutable",
                "roles_name_exists",
                "roles_capacity",
            }:
                return HTTPException(409, code)
            return HTTPException(503, "roles_unavailable")
        if isinstance(error, UserRegistryLastAdministratorError):
            return HTTPException(409, "roles_last_administrator")
        if isinstance(error, ValueError):
            return HTTPException(422, "roles_invalid_request")
        return HTTPException(503, "roles_unavailable")

    async def revoke_bound(role_id):
        # Resolve the durable binding after every policy mutation, including
        # stale browser sessions and in-flight media authorization.
        for identifier in policy.bound_user_ids(role_id):
            await sessions.revoke_user(identifier)

    @app.get(prefix + "/roles")
    async def list_roles(response: Response, session=dependency):
        response.headers["Cache-Control"] = "no-store"
        await actor(session)
        try:
            return policy.snapshot()
        except RolePolicyError as error:
            raise failure(error) from None

    @app.post(prefix + "/roles", status_code=201)
    async def create_role(payload: _Role, response: Response, session=dependency):
        response.headers["Cache-Control"] = "no-store"
        async with mutations:
            current = await actor(session)
            try:
                require_grant(app, current, payload.permissions)
                role = policy.upsert(users, payload.model_dump())
                return {"role": role, **policy.snapshot()}
            except (RolePolicyError, ValueError, UserRegistryStorageError) as error:
                raise failure(error) from None

    @app.patch(prefix + "/roles/{role_id}")
    async def update_role(
        role_id: UUID, payload: _RoleUpdate, response: Response, session=dependency
    ):
        response.headers["Cache-Control"] = "no-store"
        async with mutations:
            current = await actor(session)
            try:
                previous = next(
                    (r for r in policy.snapshot()["roles"] if r["id"] == str(role_id)), None
                )
                if previous is None:
                    raise RolePolicyError("roles_not_found")
                require_grant(app, current, previous["permissions"])
                require_grant(app, current, payload.permissions)
                role = policy.upsert(
                    users,
                    payload.model_dump(exclude={"expected_revision"}),
                    role_id=role_id,
                    expected_revision=payload.expected_revision,
                )
                await revoke_bound(role_id)
                return {"role": role, **policy.snapshot()}
            except (RolePolicyError, ValueError, UserRegistryStorageError) as error:
                raise failure(error) from None

    @app.delete(prefix + "/roles/{role_id}", status_code=204)
    async def delete_role(
        role_id: UUID, expected_revision: int = Query(ge=1, lt=2**63), session=dependency
    ):
        async with mutations:
            current = await actor(session)
            try:
                previous = next(
                    (r for r in policy.snapshot()["roles"] if r["id"] == str(role_id)), None
                )
                if previous is None:
                    raise RolePolicyError("roles_not_found")
                require_grant(app, current, previous["permissions"])
                policy.delete(users, role_id, expected_revision=expected_revision)
            except (RolePolicyError, UserRegistryStorageError) as error:
                raise failure(error) from None
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    @app.put(prefix + "/users/{user_id}/role-profile")
    async def assign_role(
        user_id: UUID, payload: _Assignment, response: Response, session=dependency
    ):
        response.headers["Cache-Control"] = "no-store"
        async with mutations:
            current = await actor(session)
            try:
                target = users.get(user_id)
                if target is None:
                    raise RolePolicyError("roles_user_not_found")
                require_grant(app, current, effective_permissions(app, target))
                profile = next(
                    (
                        r
                        for r in policy.snapshot()["roles"]
                        if r["id"] == str(payload.role_profile_id)
                    ),
                    None,
                )
                if payload.role_profile_id is not None and profile is None:
                    raise RolePolicyError("roles_not_found")
                require_grant(
                    app,
                    current,
                    BASE_PERMISSIONS[target.role] if profile is None else profile["permissions"],
                )
                replacement = policy.assign(
                    users,
                    user_id,
                    payload.role_profile_id,
                    expected_revision=payload.expected_revision,
                )
                account = users.get(user_id)
                if account.role != replacement.role:
                    account = users.update(
                        user_id, UserPatch(role=replacement.role), actor=current.username
                    )
                await sessions.revoke_user(user_id)
                return {
                    "account": {
                        **account.model_dump(mode="json"),
                        **policy.account_fields(account),
                    },
                    "revision": policy.snapshot()["revision"],
                }
            except (
                RolePolicyError,
                ValueError,
                UserRegistryStorageError,
                UserRegistryLastAdministratorError,
            ) as error:
                raise failure(error) from None
