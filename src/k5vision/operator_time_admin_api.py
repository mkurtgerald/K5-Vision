"""Current administrator controls for Windows and ONVIF time management."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from k5vision.domain.users import UserRole
from k5vision.operator_time_admin import TimeAdministrationError, normalize_servers
from k5vision.user_admin_api import _UserRateLimiter

_PREFIX = "/api/v1/operator"


class _HostConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["manual", "domain"]
    ntp_servers: list[str] = Field(default_factory=list, max_length=4)

    @field_validator("ntp_servers")
    @classmethod
    def servers(cls, value):
        return normalize_servers(value, allow_empty=True)


class _ManagementCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str | None = Field(default=None, max_length=256, repr=False, strict=True)
    password: str | None = Field(default=None, max_length=256, repr=False, strict=True)

    @model_validator(mode="after")
    def paired_credentials(self):
        if (self.username is None) != (self.password is None):
            raise ValueError("Management credentials must be supplied together")
        if any(ord(value) < 32 for value in (self.username or "") + (self.password or "")):
            raise ValueError("Management credentials refused")
        return self


class _CameraConfiguration(_ManagementCredentials):
    from_dhcp: StrictBool
    ntp_servers: list[str] = Field(default_factory=list, max_length=4)

    @field_validator("ntp_servers")
    @classmethod
    def servers(cls, value):
        return normalize_servers(value, allow_empty=True)


class _HostSynchronize(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _CameraSynchronize(_ManagementCredentials):
    mode: Literal["host"]


def install_operator_time_admin_api(
    app: FastAPI,
    *,
    time_admin,
    require_admin,
    authenticate,
    authorized_selection,
    mutations,
):
    """Reuse existing human authentication, origin checks and camera authority."""
    admin_dependency = Depends(require_admin)
    limiter = _UserRateLimiter(limit=12, window_seconds=60)

    async def current(session):
        account, token = await authenticate(session[1])
        if account.id != session[0].id or account.role is not UserRole.ADMINISTRATOR:
            raise HTTPException(403, "Insufficient time administration permission")
        return account, token

    async def writing(session):
        retry = await limiter.consume()
        if retry is not None:
            raise HTTPException(429, "time_busy", headers={"Retry-After": str(retry)})
        return await current(session)

    def service():
        if time_admin is None:
            raise HTTPException(503, "time_unavailable")
        return time_admin

    def failed(error):
        if isinstance(error, ValueError):
            return HTTPException(422, "time_invalid_request")
        code = error.code if isinstance(error, TimeAdministrationError) else "time_operation_failed"
        status = (
            429
            if code == "time_busy"
            else 422
            if code
            in {
                "time_unsupported",
                "camera_time_unsupported",
                "camera_time_auth_failed",
                "time_elevation_cancelled",
            }
            else 503
        )
        return HTTPException(status, code)

    @app.get(_PREFIX + "/admin/time", tags=["operator time administration"])
    async def host_status(response: Response, session=admin_dependency):
        response.headers["Cache-Control"] = "no-store"
        await current(session)
        try:
            result = await service().host_status()
            await current(session)
            return result
        except TimeAdministrationError as error:
            raise failed(error) from None

    @app.patch(_PREFIX + "/admin/time", tags=["operator time administration"])
    async def configure_host(
        payload: _HostConfiguration, response: Response, session=admin_dependency
    ):
        response.headers["Cache-Control"] = "no-store"
        await writing(session)
        if payload.mode == "manual" and not payload.ntp_servers:
            raise HTTPException(422, "time_invalid_request")
        try:
            async with mutations:
                await current(session)
                result = await service().host_operation("configure", **payload.model_dump())
                await current(session)
                return result
        except (TimeAdministrationError, ValueError) as error:
            raise failed(error) from None

    @app.post(_PREFIX + "/admin/time/synchronize", tags=["operator time administration"])
    async def synchronize_host(
        payload: _HostSynchronize, response: Response, session=admin_dependency
    ):
        response.headers["Cache-Control"] = "no-store"
        await writing(session)
        try:
            async with mutations:
                await current(session)
                result = await service().host_operation("resync")
                await current(session)
                return result
        except TimeAdministrationError as error:
            raise failed(error) from None

    @app.get(_PREFIX + "/cameras/{camera_id}/time", tags=["operator time administration"])
    async def camera_status(camera_id: UUID, response: Response, session=admin_dependency):
        response.headers["Cache-Control"] = "no-store"
        account, _ = await current(session)
        source = authorized_selection(account, camera_id)
        try:
            result = await service().camera(source)
            account, _ = await current(session)
            if authorized_selection(account, camera_id) != source:
                raise HTTPException(409, "camera_configuration_changed")
            return result
        except TimeAdministrationError as error:
            raise failed(error) from None

    @app.post(_PREFIX + "/cameras/{camera_id}/time/status", tags=["operator time administration"])
    async def camera_status_with_credentials(
        camera_id: UUID,
        payload: _ManagementCredentials,
        response: Response,
        session=admin_dependency,
    ):
        response.headers["Cache-Control"] = "no-store"
        account, _ = await current(session)
        source = authorized_selection(account, camera_id)
        try:
            result = await service().camera(source, "read", **payload.model_dump(exclude_none=True))
            account, _ = await current(session)
            if authorized_selection(account, camera_id) != source:
                raise HTTPException(409, "camera_configuration_changed")
            return result
        except TimeAdministrationError as error:
            raise failed(error) from None

    async def mutate_camera(session, camera_id, action, options):
        async with mutations:
            account, _ = await writing(session)
            source = authorized_selection(account, camera_id)
            try:
                result = await service().camera(source, action, **options)
                account, _ = await current(session)
                if authorized_selection(account, camera_id) != source:
                    raise HTTPException(409, "camera_configuration_changed")
                return result
            except (TimeAdministrationError, ValueError) as error:
                raise failed(error) from None

    @app.patch(_PREFIX + "/cameras/{camera_id}/time", tags=["operator time administration"])
    async def configure_camera(
        camera_id: UUID, payload: _CameraConfiguration, response: Response, session=admin_dependency
    ):
        response.headers["Cache-Control"] = "no-store"
        if not payload.from_dhcp and not payload.ntp_servers:
            raise HTTPException(422, "time_invalid_request")
        return await mutate_camera(
            session, camera_id, "configure", payload.model_dump(exclude_none=True)
        )

    @app.post(
        _PREFIX + "/cameras/{camera_id}/time/synchronize", tags=["operator time administration"]
    )
    async def synchronize_camera(
        camera_id: UUID, payload: _CameraSynchronize, response: Response, session=admin_dependency
    ):
        response.headers["Cache-Control"] = "no-store"
        return await mutate_camera(
            session,
            camera_id,
            "synchronize",
            payload.model_dump(exclude={"mode"}, exclude_none=True),
        )
