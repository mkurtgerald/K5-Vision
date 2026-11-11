"""Graphical administration and separately scoped machine lab handoff routes."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator
from starlette.concurrency import run_in_threadpool

from k5vision.operator_integration_contracts import IntegrationContractError
from k5vision.operator_integrations import OperatorIntegrationError

_ADMIN = "/api/v1/operator/admin/integrations"
_MACHINE = "/api/v1/operator/integrations"


class _CreateConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    kind: Literal["neural", "analytics"]
    origin: str = Field(default="", max_length=256)
    token: str | None = Field(default=None, min_length=32, max_length=128, repr=False)
    enabled: StrictBool = False
    camera_ids: list[UUID] = Field(default_factory=list, max_length=64)


class _UpdateConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=80)
    origin: str | None = Field(default=None, max_length=256)
    token: str | None = Field(default=None, min_length=32, max_length=128, repr=False)
    enabled: StrictBool | None = None
    camera_ids: list[UUID] | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def change(self):
        if not self.model_fields_set or any(
            getattr(self, key) is None for key in self.model_fields_set
        ):
            raise ValueError("An explicit connection change is required")
        return self


class _Handshake(BaseModel):
    model_config = ConfigDict(extra="forbid")
    supported_versions: list[str] = Field(min_length=1, max_length=8)


def install_operator_integrations_api(
    app: FastAPI,
    *,
    integrations,
    registry,
    require_admin,
    require_session,
    authenticate,
    authorized_selection,
    mutations,
):
    """Never accept human session credentials as a provider authority grant."""
    admin = Depends(require_admin)
    human = Depends(require_session)

    def service():
        if integrations is None:
            raise HTTPException(503, "integration_unavailable")
        return integrations

    def known(camera):
        return registry.get(camera) is not None

    def refuse(error):
        code = str(error)
        status = {
            "integration_not_found": 404,
            "integration_capacity": 409,
            "integration_unauthorized": 401,
            "integration_camera_forbidden": 403,
            "integration_camera_scope_required": 422,
            "integration_handshake_required": 409,
            "integration_incompatible": 409,
            "integration_unavailable": 503,
            "integration_event_conflict": 409,
            "integration_response_conflict": 409,
        }.get(code, 422)
        if code not in {
            "integration_not_found",
            "integration_capacity",
            "integration_unauthorized",
            "integration_camera_forbidden",
            "integration_camera_scope_required",
            "integration_handshake_required",
            "integration_incompatible",
            "integration_unavailable",
            "integration_invalid_event",
            "integration_time_inconsistent",
            "integration_source_clock_ahead",
            "integration_event_conflict",
            "integration_response_conflict",
            "integration_invalid_response",
            "integration_correlation_mismatch",
            "integration_invalid_request",
        }:
            code, status = "integration_unavailable", 503
        return HTTPException(status, code)

    def grants(payload):
        if "camera_ids" in payload and not all(
            known(UUID(value)) for value in payload["camera_ids"]
        ):
            raise HTTPException(422, "integration_invalid_request")

    def machine_token(request):
        values = request.headers.getlist("x-integration-token")
        # A dedicated header prevents accidental human bearer/cookie reuse.
        if len(values) != 1 or request.headers.getlist("authorization"):
            raise HTTPException(401, "integration_unauthorized")
        return values[0]

    def audit(action, actor, identifier, *, outcome="success", status_code=None):
        sink = getattr(app.state, "operator_audit", None)
        if sink is not None:
            sink.record(
                action=action,
                actor=str(actor),
                target_id=str(identifier),
                target_kind="integration",
                outcome=outcome,
                status_code=status_code,
            )

    @app.get(_ADMIN, tags=["operator integrations"])
    async def list_connections(response: Response, session=admin):
        response.headers["Cache-Control"] = "no-store"
        await authenticate(session[1])
        try:
            return service().list()
        except OperatorIntegrationError as error:
            raise refuse(error) from None

    @app.post(_ADMIN, status_code=201, tags=["operator integrations"])
    async def create_connection(payload: _CreateConnection, response: Response, session=admin):
        response.headers["Cache-Control"] = "no-store"
        async with mutations:
            account, _ = await authenticate(session[1])
            values = payload.model_dump(mode="json")
            grants(values)
            try:
                result = service().create(**values)
                audit("integration.create", account.id, result["id"])
                return result
            except (OperatorIntegrationError, IntegrationContractError) as error:
                raise refuse(error) from None

    @app.patch(_ADMIN + "/{connection_id}", tags=["operator integrations"])
    async def update_connection(
        connection_id: UUID, payload: _UpdateConnection, response: Response, session=admin
    ):
        response.headers["Cache-Control"] = "no-store"
        async with mutations:
            account, _ = await authenticate(session[1])
            values = payload.model_dump(mode="json", exclude_unset=True)
            grants(values)
            try:
                result = service().update(connection_id, values)
                audit("integration.update", account.id, connection_id)
                return result
            except (OperatorIntegrationError, IntegrationContractError) as error:
                raise refuse(error) from None

    @app.delete(_ADMIN + "/{connection_id}", status_code=204, tags=["operator integrations"])
    async def delete_connection(connection_id: UUID, response: Response, session=admin):
        response.headers["Cache-Control"] = "no-store"
        async with mutations:
            account, _ = await authenticate(session[1])
            try:
                service().delete(connection_id)
                audit("integration.delete", account.id, connection_id)
            except OperatorIntegrationError as error:
                raise refuse(error) from None
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    @app.post(_ADMIN + "/{connection_id}/test", tags=["operator integrations"])
    async def test_connection(connection_id: UUID, response: Response, session=admin):
        response.headers["Cache-Control"] = "no-store"
        await authenticate(session[1])
        try:
            return service().test(connection_id)
        except OperatorIntegrationError as error:
            raise refuse(error) from None

    @app.post(_ADMIN + "/{connection_id}/retry", tags=["operator integrations"])
    async def retry_connection(connection_id: UUID, response: Response, session=admin):
        response.headers["Cache-Control"] = "no-store"
        async with mutations:
            account, _ = await authenticate(session[1])
            try:
                result = service().retry(connection_id)
                audit("integration.update", account.id, connection_id)
                return result
            except OperatorIntegrationError as error:
                raise refuse(error) from None

    @app.post(_MACHINE + "/{connection_id}/handshake", tags=["lab handoff"])
    async def handshake(
        connection_id: UUID, payload: _Handshake, request: Request, response: Response
    ):
        response.headers["Cache-Control"] = "no-store"
        try:
            # The identity-bound store performs synchronous SQLite and seal
            # admission. Never hold the ASGI event loop during its disk wait:
            # a delayed provider handshake must not starve camera or health APIs.
            result = await run_in_threadpool(
                service().handshake,
                connection_id,
                machine_token(request),
                payload.supported_versions,
            )
            await run_in_threadpool(
                audit, "integration.handshake", connection_id, connection_id
            )
            return result
        except (OperatorIntegrationError, IntegrationContractError) as error:
            raise refuse(error) from None

    @app.get(_MACHINE + "/{connection_id}/observations", tags=["lab handoff"])
    async def observations(
        connection_id: UUID,
        request: Request,
        response: Response,
        limit: int = Query(default=10, ge=1, le=20),
    ):
        response.headers["Cache-Control"] = "no-store"
        try:
            return await run_in_threadpool(
                service().pull,
                connection_id,
                machine_token(request),
                known_camera=known,
                limit=limit,
            )
        except (OperatorIntegrationError, IntegrationContractError) as error:
            raise refuse(error) from None

    @app.post(_MACHINE + "/{connection_id}/responses", tags=["lab handoff"])
    async def provider_response(
        connection_id: UUID, payload: dict, request: Request, response: Response
    ):
        response.headers["Cache-Control"] = "no-store"
        try:
            result = await run_in_threadpool(
                service().respond,
                connection_id,
                machine_token(request),
                payload,
                known_camera=known,
            )
            await run_in_threadpool(
                audit, "integration.response", connection_id, connection_id
            )
            return result
        except (OperatorIntegrationError, IntegrationContractError) as error:
            raise refuse(error) from None

    @app.post(_MACHINE + "/{connection_id}/events", tags=["lab handoff"])
    async def analytic_event(
        connection_id: UUID, payload: dict, request: Request, response: Response
    ):
        response.headers["Cache-Control"] = "no-store"
        try:
            result = await run_in_threadpool(
                service().ingest,
                connection_id,
                machine_token(request),
                payload,
                known_camera=known,
            )
            await run_in_threadpool(
                audit, "integration.event", connection_id, connection_id
            )
            return result
        except (OperatorIntegrationError, IntegrationContractError) as error:
            if str(error) == "integration_source_clock_ahead":
                await run_in_threadpool(
                    audit,
                    "integration.event",
                    connection_id,
                    connection_id,
                    outcome="failure",
                    status_code=422,
                )
            raise refuse(error) from None

    @app.get(
        "/api/v1/operator/cameras/{camera_id}/integration-events", tags=["operator integrations"]
    )
    async def external_events(
        camera_id: UUID,
        response: Response,
        session=human,
        limit: int = Query(default=50, ge=1, le=100),
        before: int | None = Query(default=None, ge=1),
    ):
        response.headers["Cache-Control"] = "no-store"
        account, _ = await authenticate(session[1])
        authorized_selection(account, camera_id)
        try:
            return await run_in_threadpool(
                service().events, camera_id=camera_id, before=before, limit=limit
            )
        except (OperatorIntegrationError, IntegrationContractError) as error:
            raise refuse(error) from None
