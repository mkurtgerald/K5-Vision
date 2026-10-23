"""Human-authorized direct RTSP test/connect with short-lived private handles."""

from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from time import monotonic
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from k5vision.domain.users import UserAccount, UserRole
from k5vision.operator_onvif_api import _ProbeVault, _public_clock
from k5vision.operator_rtsp import RtspConnector, RtspError, stream_selection

_PREFIX = "/api/v1/operator"


class _StreamRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_uri: str = Field(min_length=1, max_length=4096, repr=False)
    username: str = Field(default="", max_length=256, repr=False)
    password: str = Field(default="", max_length=256, repr=False)


class _ConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    probe_id: str = Field(min_length=32, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    profile_id: str = Field(min_length=32, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=128)
    device_id: UUID | None = None


@dataclass(frozen=True)
class _Profile:
    source_uri: str = field(repr=False)
    stream_key: str


def install_operator_rtsp_api(app: FastAPI, *, require_admin, authenticate, enroll):
    from hashlib import sha256

    if getattr(app.state, "operator_rtsp_connector", None) is None:
        app.state.operator_rtsp_connector = RtspConnector()
    vault = _ProbeVault()
    app.state.operator_rtsp_probes = vault
    admin_session = Depends(require_admin)

    async def current_admin(session):
        current = await authenticate(session[1])
        if current[0].id != session[0].id or current[0].role is not UserRole.ADMINISTRATOR:
            raise HTTPException(403, "Insufficient camera administration permission")

    @app.post(_PREFIX + "/camera-stream-probe", tags=["operator"])
    async def probe_stream(
        payload: _StreamRequest,
        response: Response,
        session: tuple[UserAccount, str] = admin_session,
    ):
        response.headers["Cache-Control"] = "no-store"
        identifier = probe = None
        try:
            selected = stream_selection(payload.source_uri, payload.username, payload.password)
            identifier, probe = vault.reserve(
                session, selected.host, selected.port, selected.secure
            )
            try:
                await app.state.operator_rtsp_connector.probe(selected)
            except Exception:
                await current_admin(session)
                raise
            await current_admin(session)
            if vault.get(identifier, session) is not probe:
                raise HTTPException(410, "camera_probe_expired")
            profile_id = secrets.token_urlsafe(24)
            probe.profiles[profile_id] = _Profile(
                selected.source_uri, sha256(selected.request_uri.encode()).hexdigest()
            )
            return {
                "probe_id": identifier,
                "expires_in_seconds": max(0, int(probe.expires - monotonic())),
                "profiles": [
                    {
                        "id": profile_id,
                        "name": "Direct RTSP stream",
                        "codec": "H264",
                        "width": 0,
                        "height": 0,
                        "supported": True,
                    }
                ],
                "clock": _public_clock(None),
            }
        except BaseException as error:
            if probe is not None:
                vault.discard(identifier, probe)
            if isinstance(error, RtspError):
                status, code = {
                    "invalid_stream": (422, "stream_invalid"),
                    "auth_failed": (422, "stream_auth_failed"),
                    "unsupported": (422, "stream_unsupported"),
                    "unreachable": (503, "stream_unreachable"),
                }.get(error.code, (502, "stream_invalid_response"))
                raise HTTPException(status, code, headers={"Cache-Control": "no-store"}) from None
            if isinstance(error, HTTPException | asyncio.CancelledError):
                raise
            raise HTTPException(502, "stream_invalid_response") from None

    @app.post(_PREFIX + "/cameras/stream-connect", status_code=201, tags=["operator"])
    async def connect_stream(
        payload: _ConnectRequest,
        response: Response,
        session: tuple[UserAccount, str] = admin_session,
    ):
        response.headers["Cache-Control"] = "no-store"
        probe = vault.get(payload.probe_id, session)
        if probe.connecting:
            raise HTTPException(409, "camera_busy")
        profile = probe.profiles.get(payload.profile_id)
        if not isinstance(profile, _Profile):
            raise HTTPException(410, "camera_probe_expired")
        probe.connecting = True
        try:
            result = await enroll(
                name=payload.name,
                host=probe.host,
                management_port=probe.port,
                source_uri=profile.source_uri,
                session=session,
                reconnect_id=payload.device_id,
                stream_key=profile.stream_key,
                before_store=lambda: vault.claimed(payload.probe_id, probe, session),
            )
            vault.discard(payload.probe_id, probe)
            return result
        finally:
            probe.connecting = False

    previous_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with previous_lifespan(application):

            async def expire():
                while True:
                    await asyncio.sleep(5)
                    vault.prune()

            task = asyncio.create_task(expire(), name="operator-rtsp-connection-expiry")
            try:
                yield
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                vault.clear()

    app.router.lifespan_context = lifespan
    return vault
