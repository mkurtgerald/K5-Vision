"""Current-human-authorized settings and recorded person-event queries."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Callable
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
    on_analytics_enabled: Callable[[], None] | None = None,
):
    """Reuse the product's current credential, role, origin and camera authority."""
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

    @app.patch(_PREFIX + "/{camera_id}/analytics", tags=["operator analytics"])
    async def configure_analytics(
        camera_id: UUID,
        payload: _AnalyticsSettings,
        response: Response,
        session=admin_dependency,
    ):
        response.headers["Cache-Control"] = "no-store"
        async with mutations:
            account, _ = await authenticate(session[1])
            if account.role is not UserRole.ADMINISTRATOR:
                raise HTTPException(403, "Insufficient camera administration permission")
            authorized_selection(account, camera_id)
            try:
                configured = service().configure(
                    camera_id,
                    enabled=payload.enabled,
                    confidence_threshold=payload.confidence_threshold,
                )
            except (OperatorEventsError, ValueError):
                raise HTTPException(503, "analytics_unavailable") from None
            if payload.enabled and on_analytics_enabled is not None:
                try:
                    on_analytics_enabled()
                except Exception:
                    # Optional media-free compilation cannot invalidate a
                    # successful recorder configuration; the view retains
                    # its original bounded cold-initialization path.
                    pass
            return configured

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
