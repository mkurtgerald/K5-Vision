"""Current-human-authorized settings and recorded person-event queries."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from k5vision.domain.users import UserRole
from k5vision.operator_events import OperatorEventsError

_PREFIX = "/api/v1/operator/cameras"


class _AnalyticsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool
    confidence_threshold: float = Field(ge=0.5, le=0.99, allow_inf_nan=False, strict=True)


def _range(start: str | None, end: str | None):
    if start is None and end is None:
        return None, None
    try:
        if start is None or end is None or max(len(start), len(end)) > 40:
            raise ValueError
        first, last = datetime.fromisoformat(start), datetime.fromisoformat(end)
        if first.utcoffset() is None or last.utcoffset() is None:
            raise ValueError
        if not timedelta(0) < last - first <= timedelta(hours=24):
            raise ValueError
        return first.astimezone(UTC), last.astimezone(UTC)
    except (ValueError, OverflowError):
        raise HTTPException(422, "analytics_invalid_request") from None


def install_operator_events_api(
    app: FastAPI,
    *,
    events,
    require_session,
    require_admin,
    authenticate,
    authorized_selection,
    selections,
    mutations,
):
    """Reuse the product's current credential, role, origin and camera authority."""
    if events is not None:
        events.bind_mutations(mutations)
    session_dependency = Depends(require_session)
    admin_dependency = Depends(require_admin)

    def service():
        if events is None:
            raise HTTPException(503, "analytics_unavailable")
        return events

    async def current_selection(token, camera_id: UUID) -> None:
        """Authenticate and check current camera rights without caching a denial."""
        try:
            account, _ = await authenticate(token)
            await asyncio.to_thread(authorized_selection, account, camera_id)
        except HTTPException as error:
            # FastAPI's exception handler creates a new response, so headers
            # placed on the endpoint's Response are not used for revoked access.
            raise HTTPException(
                error.status_code,
                error.detail,
                headers={**(error.headers or {}), "Cache-Control": "no-store"},
            ) from None

    @app.get(_PREFIX + "/{camera_id}/analytics", tags=["operator analytics"])
    async def analytics_status(camera_id: UUID, response: Response, session=session_dependency):
        response.headers["Cache-Control"] = "no-store"
        await current_selection(session[1], camera_id)
        try:
            # Durable settings and summary reads use SQLite; keep the ASGI
            # loop available for live playback and unrelated operator traffic.
            snapshot = await asyncio.to_thread(service().status, camera_id)
        except OperatorEventsError:
            raise HTTPException(503, "analytics_unavailable") from None
        # A camera grant or session may have been revoked during the read.
        await current_selection(session[1], camera_id)
        return snapshot

    async def current_administrator(token):
        account, _ = await authenticate(token)
        if account.role is not UserRole.ADMINISTRATOR:
            raise HTTPException(403, "Insufficient camera administration permission")
        return account

    @app.patch(_PREFIX + "/{camera_id}/analytics", tags=["operator analytics"])
    async def configure_analytics(
        camera_id: UUID,
        payload: _AnalyticsSettings,
        response: Response,
        session=admin_dependency,
    ):
        response.headers["Cache-Control"] = "no-store"
        async with mutations:
            events_service = service()
            account = await current_administrator(session[1])
            await asyncio.to_thread(authorized_selection, account, camera_id)
            try:
                previous = await events_service._durable(events_service.store.settings, camera_id)
            except (OperatorEventsError, ValueError):
                raise HTTPException(
                    503, "analytics_unavailable", headers={"Cache-Control": "no-store"}
                ) from None
            # Selection and private-state reads both yield. Their old principal
            # cannot authorize a later write after logout or permission change.
            await current_administrator(session[1])
            requested = {
                "enabled": payload.enabled,
                "confidence_threshold": payload.confidence_threshold,
            }
            started = False
            try:
                started = True
                snapshot = await events_service.configure_async(camera_id, **requested)
                account = await current_administrator(session[1])
                await asyncio.to_thread(authorized_selection, account, camera_id)
                await current_administrator(session[1])
                return snapshot
            except BaseException as error:
                cancellation = error if isinstance(error, asyncio.CancelledError) else None
                rollback_failed = False
                if started:

                    async def restore():
                        current = await events_service._durable(
                            events_service.store.settings, camera_id
                        )
                        if current == previous:
                            return
                        if current != requested:
                            # Never overwrite a third state whose ownership
                            # cannot be established, even under this lock.
                            raise OperatorEventsError("Analytics compensation unavailable")
                        await events_service.configure_async(camera_id, **previous)
                        readback = await events_service._durable(
                            events_service.store.settings, camera_id
                        )
                        if readback != previous:
                            raise OperatorEventsError("Analytics compensation unavailable")

                    rollback = asyncio.create_task(restore(), name="operator-analytics-restore")
                    # Keep both the mutation lock and the actual write owner
                    # until compensation finishes, despite repeated cancellation.
                    while True:
                        try:
                            await asyncio.shield(rollback)
                        except asyncio.CancelledError as cancelled:
                            if rollback.cancelled():
                                rollback_failed = True
                                break
                            if cancellation is None:
                                cancellation = cancelled
                        except Exception:
                            rollback_failed = True
                            break
                        else:
                            break
                if rollback_failed:
                    events_service.mark_configuration_unavailable()
                if cancellation is not None:
                    raise cancellation from None
                if rollback_failed or isinstance(error, (OperatorEventsError, ValueError)):
                    raise HTTPException(
                        503, "analytics_unavailable", headers={"Cache-Control": "no-store"}
                    ) from None
                if isinstance(error, HTTPException):
                    raise HTTPException(
                        error.status_code,
                        error.detail,
                        headers={**(error.headers or {}), "Cache-Control": "no-store"},
                    ) from None
                raise

    @app.get(_PREFIX + "/{camera_id}/events", tags=["operator analytics"])
    async def person_events(
        camera_id: UUID,
        response: Response,
        start: str | None = None,
        end: str | None = None,
        limit: int = Query(default=50, ge=1, le=100),
        before: int | None = Query(default=None, ge=1, le=2**63 - 1),
        session=session_dependency,
    ):
        response.headers["Cache-Control"] = "no-store"
        await current_selection(session[1], camera_id)
        first, last = _range(start, end)
        try:
            # Bounded recorded-event history performs SQLite reads that can
            # otherwise block camera streaming and live sample inference.
            snapshot = await asyncio.to_thread(
                service().store.events,
                camera_id,
                start=first,
                end=last,
                limit=limit,
                before=before,
            )
        except OperatorEventsError:
            raise HTTPException(503, "analytics_unavailable") from None
        await current_selection(session[1], camera_id)
        return snapshot
