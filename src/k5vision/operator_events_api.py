"""Current-human-authorized settings and recorded person-event queries."""

from __future__ import annotations

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
    session_dependency = Depends(require_session)
    admin_dependency = Depends(require_admin)

    def service():
        if events is None:
            raise HTTPException(503, "analytics_unavailable")
        return events

    @app.get(_PREFIX + "/{camera_id}/analytics", tags=["operator analytics"])
    async def analytics_status(camera_id: UUID, response: Response, session=session_dependency):
        response.headers["Cache-Control"] = "no-store"
        account, _ = await authenticate(session[1])
        authorized_selection(account, camera_id)
        try:
            return service().status(camera_id)
        except OperatorEventsError:
            raise HTTPException(503, "analytics_unavailable") from None

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
                return service().configure(
                    camera_id,
                    enabled=payload.enabled,
                    confidence_threshold=payload.confidence_threshold,
                )
            except (OperatorEventsError, ValueError):
                raise HTTPException(503, "analytics_unavailable") from None

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
        account, _ = await authenticate(session[1])
        authorized_selection(account, camera_id)
        first, last = _range(start, end)
        try:
            return service().store.events(
                camera_id, start=first, end=last, limit=limit, before=before
            )
        except OperatorEventsError:
            raise HTTPException(503, "analytics_unavailable") from None
