"""Current-administrator activity queries and bounded normal JSON downloads."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Response

from k5vision.domain.users import UserRole
from k5vision.operator_audit import ACTIONS, MAX_EXPORT_BYTES, OUTCOMES, OperatorAuditError

_PREFIX = "/api/v1/operator/admin/activity"


def install_operator_audit_api(app: FastAPI, *, store, require_admin, authenticate):
    """Retain canonical session authority; no credential-bearing download URL."""
    admin = Depends(require_admin)

    async def current(session):
        account, _ = await authenticate(session[1])
        if account.id != session[0].id or account.role is not UserRole.ADMINISTRATOR:
            raise HTTPException(403, "Insufficient audit administration permission")
        return account

    async def query(
        session, *, limit, before, action, outcome, actor_id, target_id, start, end, export
    ):
        await current(session)
        if store is None:
            raise HTTPException(503, "audit_unavailable")
        try:
            first = (
                datetime.fromisoformat(start) if start is not None and len(start) <= 40 else None
            )
            last = datetime.fromisoformat(end) if end is not None and len(end) <= 40 else None
            if (start is not None and first is None) or (end is not None and last is None):
                raise ValueError
            data = await asyncio.to_thread(
                store.events,
                limit=limit,
                before=before,
                action=action,
                outcome=outcome,
                actor_id=actor_id,
                target_id=target_id,
                start=first,
                end=last,
                export=export,
            )
        except (ValueError, TypeError, OverflowError):
            raise HTTPException(422, "audit_invalid_request") from None
        except OperatorAuditError:
            raise HTTPException(503, "audit_unavailable") from None
        await current(session)
        return data

    @app.get(_PREFIX, tags=["operator audit"])
    async def activity(
        response: Response,
        limit: int = Query(default=50, ge=1, le=100),
        before: int | None = Query(default=None, ge=1, lt=2**63),
        action: str | None = Query(default=None, max_length=64),
        outcome: str | None = Query(default=None, max_length=16),
        actor_id: UUID | None = None,
        target_id: UUID | None = None,
        start: str | None = None,
        end: str | None = None,
        session=admin,
    ):
        response.headers["Cache-Control"] = "no-store"
        data = await query(
            session,
            limit=limit,
            before=before,
            action=action,
            outcome=outcome,
            actor_id=actor_id,
            target_id=target_id,
            start=start,
            end=end,
            export=False,
        )
        return {**data, "actions": sorted(ACTIONS), "outcomes": sorted(OUTCOMES)}

    @app.get(_PREFIX + "/export", tags=["operator audit"])
    async def export_activity(
        limit: int = Query(default=5000, ge=1, le=10000),
        before: int | None = Query(default=None, ge=1, lt=2**63),
        action: str | None = Query(default=None, max_length=64),
        outcome: str | None = Query(default=None, max_length=16),
        actor_id: UUID | None = None,
        target_id: UUID | None = None,
        start: str | None = None,
        end: str | None = None,
        session=admin,
    ):
        data = await query(
            session,
            limit=limit,
            before=before,
            action=action,
            outcome=outcome,
            actor_id=actor_id,
            target_id=target_id,
            start=start,
            end=end,
            export=True,
        )
        body = json.dumps(
            {
                "schema_version": 1,
                "site_id": store.site_id,
                "time_basis": "host_observed_utc",
                **data,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
        if len(body) > MAX_EXPORT_BYTES:
            raise HTTPException(409, "audit_export_too_large")
        await current(session)
        return Response(
            body,
            media_type="application/json",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": 'attachment; filename="k5-activity.json"',
                "X-Content-Type-Options": "nosniff",
            },
        )
