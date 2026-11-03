"""Keep camera credentials and stream addresses behind short-lived opaque handles."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import math
import re
import secrets
import socket
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime
from time import monotonic
from uuid import UUID

import psutil
from fastapi import Depends, FastAPI, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, IPvAnyAddress, StrictBool, StrictInt

from k5vision.domain.users import UserAccount, UserRole

_PREFIX = "/api/v1/operator"
_PROBE_TTL_SECONDS = 120
_MAX_PROBES = 16
_MAX_PROFILES = 8
_MAX_DISCOVERED = 64
_CLOCK_FIELDS = frozenset(
    {
        "status",
        "offset_seconds",
        "measured_at",
        "uncertainty_seconds",
        "reported_mode",
        "sync_verified",
    }
)
_OBSERVED_UTC = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z\Z"
)


def _public_clock(reading: object) -> dict:
    """Publish a read-only sample, never raw camera time or a synchronization claim."""
    unavailable = {
        "status": "unavailable",
        "offset_seconds": None,
        "measured_at": None,
        "uncertainty_seconds": None,
        "reported_mode": "unknown",
        "sync_verified": False,
    }
    try:
        publish = getattr(reading, "public", None)
        if not callable(publish):
            return unavailable
        public = publish()
        if (
            type(public) is not dict
            or public.keys() != _CLOCK_FIELDS
            or public["status"] not in {"measured", "unavailable"}
            or public["reported_mode"] not in {"ntp", "manual", "unknown"}
            or public["sync_verified"] is not False
        ):
            return unavailable
        observed = public["measured_at"]
        if observed is not None:
            if type(observed) is not str or not _OBSERVED_UTC.fullmatch(observed):
                return unavailable
            datetime.fromisoformat(observed.replace("Z", "+00:00"))
        offset, uncertainty = public["offset_seconds"], public["uncertainty_seconds"]
        if public["status"] == "measured":
            if (
                observed is None
                or type(offset) not in (int, float)
                or not math.isfinite(offset)
                or abs(offset) > 315576000000
                or type(uncertainty) not in (int, float)
                or not math.isfinite(uncertainty)
                or not 0 <= uncertainty <= 15
            ):
                return unavailable
        elif offset is not None or uncertainty is not None:
            return unavailable
        return dict(public)
    except Exception:
        return unavailable


def _camera_ipv4_interfaces() -> tuple[str, ...]:
    """Try each active local camera network instead of only the default route."""
    try:
        addresses, statuses = psutil.net_if_addrs(), psutil.net_if_stats()
        selected = set()
        for name, values in addresses.items():
            if not getattr(statuses.get(name), "isup", False):
                continue
            for value in values:
                if value.family != socket.AF_INET:
                    continue
                address = ipaddress.IPv4Address(value.address)
                if address.is_private and not (
                    address.is_loopback or address.is_unspecified or address.is_multicast
                ):
                    selected.add(str(address))
        return tuple(sorted(selected, key=lambda value: int(ipaddress.IPv4Address(value)))[:16])
    except Exception:
        # The connector falls back to the OS default interface when enumeration
        # is unavailable, retaining manual numeric-address connection as well.
        return ()


class _DiscoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _ProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: IPvAnyAddress
    port: StrictInt = Field(default=80, ge=1, le=65535)
    secure: StrictBool = False
    username: str = Field(default="", max_length=256, repr=False)
    password: str = Field(default="", max_length=256, repr=False)


class _ConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    probe_id: str = Field(min_length=32, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    profile_id: str = Field(min_length=32, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=128)


@dataclass
class _Probe:
    owner: tuple[UUID, bytes]
    host: str
    port: int
    secure: bool
    expires: float
    profiles: dict[str, object] = field(default_factory=dict, repr=False)
    connecting: bool = False


class _ProbeVault:
    """One event-loop-owned, finite cache; raw session tokens are never retained."""

    def __init__(self):
        self._probes: dict[str, _Probe] = {}

    @staticmethod
    def owner(session: tuple[UserAccount, str]) -> tuple[UUID, bytes]:
        return session[0].id, hashlib.sha256(session[1].encode("ascii")).digest()

    def prune(self) -> None:
        now = monotonic()
        for key, value in tuple(self._probes.items()):
            if value.expires <= now:
                del self._probes[key]

    def reserve(
        self, session: tuple[UserAccount, str], host: str, port: int, secure: bool
    ) -> tuple[str, _Probe]:
        self.prune()
        if len(self._probes) >= _MAX_PROBES:
            raise HTTPException(429, "camera_busy")
        identifier = secrets.token_urlsafe(24)
        probe = _Probe(self.owner(session), host, port, secure, monotonic() + _PROBE_TTL_SECONDS)
        self._probes[identifier] = probe
        return identifier, probe

    def get(self, identifier: str, session: tuple[UserAccount, str]) -> _Probe:
        self.prune()
        probe = self._probes.get(identifier)
        if probe is None or probe.owner != self.owner(session):
            raise HTTPException(410, "camera_probe_expired")
        return probe

    def claimed(self, identifier: str, probe: _Probe, session: tuple[UserAccount, str]) -> None:
        if self.get(identifier, session) is not probe or not probe.connecting:
            raise HTTPException(410, "camera_probe_expired")

    def discard(self, identifier: str, probe: _Probe) -> None:
        if self._probes.get(identifier) is probe:
            del self._probes[identifier]

    def clear_token(self, token: str) -> None:
        digest = hashlib.sha256(token.encode("ascii")).digest()
        for key, value in tuple(self._probes.items()):
            if value.owner[1] == digest:
                del self._probes[key]

    def clear(self) -> None:
        self._probes.clear()


def _public_name(value: object, fallback: str, *, private: tuple[str, ...] = ()) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 128
        or "://" in value
        or any(ord(character) < 32 for character in value)
        or any(secret and secret in value for secret in private)
    ):
        return fallback
    return value.strip()


def _connector_failure(error: Exception, *, discovery: bool = False) -> HTTPException:
    code = getattr(error, "code", "invalid_response")
    if code == "busy":
        status, detail = 429, "camera_busy"
    elif discovery:
        status, detail = 503, "discovery_unavailable"
    else:
        status, detail = {
            "auth_failed": (422, "camera_auth_failed"),
            "unreachable": (503, "camera_unreachable"),
            "unsupported": (422, "camera_unsupported"),
            "invalid_response": (502, "camera_invalid_response"),
        }.get(code, (502, "camera_invalid_response"))
    public_detail = (
        detail
        if discovery
        else {"code": detail, "clock": _public_clock(getattr(error, "clock", None))}
    )
    return HTTPException(status, public_detail, headers={"Cache-Control": "no-store"})


def install_operator_onvif_api(
    app: FastAPI,
    *,
    require_admin: Callable,
    authenticate: Callable[[str], Awaitable[tuple[UserAccount, str]]],
    enroll: Callable[..., Awaitable[dict]],
) -> _ProbeVault:
    """Install owner-authorized discovery/probe/connect without public stream URLs."""
    from k5vision.operator_onvif import OnvifConnector

    connector = OnvifConnector(interface_provider=_camera_ipv4_interfaces)
    if getattr(app.state, "operator_onvif_connector", None) is None:
        app.state.operator_onvif_connector = connector
    vault = _ProbeVault()
    app.state.operator_onvif_probes = vault
    admin_session = Depends(require_admin)

    async def current_admin(session: tuple[UserAccount, str]) -> tuple[UserAccount, str]:
        current = await authenticate(session[1])
        if current[0].id != session[0].id or current[0].role is not UserRole.ADMINISTRATOR:
            raise HTTPException(403, "Insufficient camera administration permission")
        return current

    @app.post(_PREFIX + "/camera-discovery", tags=["operator"])
    async def discover(
        _payload: _DiscoveryRequest,
        response: Response,
        session: tuple[UserAccount, str] = admin_session,
    ):
        response.headers["Cache-Control"] = "no-store"
        try:
            candidates = await app.state.operator_onvif_connector.discover()
            if not isinstance(candidates, (list, tuple)) or len(candidates) > _MAX_DISCOVERED:
                raise ValueError
            public = []
            for candidate in candidates:
                host = str(ipaddress.ip_address(candidate["host"]))
                port, secure = candidate["port"], candidate["secure"]
                if type(port) is not int or not 1 <= port <= 65535 or type(secure) is not bool:
                    raise ValueError
                public.append(
                    {
                        "host": host,
                        "port": port,
                        "secure": secure,
                        "name": _public_name(candidate["name"], "Network camera"),
                    }
                )
        except Exception as error:
            raise _connector_failure(error, discovery=True) from None
        await current_admin(session)
        return {"cameras": public, "status": "complete"}

    @app.post(_PREFIX + "/camera-profiles", tags=["operator"])
    async def profiles(
        payload: _ProfileRequest,
        response: Response,
        session: tuple[UserAccount, str] = admin_session,
    ):
        response.headers["Cache-Control"] = "no-store"
        identifier, probe = vault.reserve(session, str(payload.host), payload.port, payload.secure)
        try:
            try:
                selected = await app.state.operator_onvif_connector.probe(
                    host=probe.host,
                    port=probe.port,
                    secure=payload.secure,
                    username=payload.username,
                    password=payload.password,
                )
                if (
                    not isinstance(selected, (list, tuple))
                    or not 1 <= len(selected) <= _MAX_PROFILES
                ):
                    raise ValueError
                public = []
                clock = _public_clock(getattr(selected[0], "clock", None))
                for index, profile in enumerate(selected, 1):
                    if (
                        profile.codec not in {"H264", "H265", "MJPEG", "UNKNOWN"}
                        or type(profile.width) is not int
                        or type(profile.height) is not int
                        or not 0 <= profile.width <= 16384
                        or not 0 <= profile.height <= 16384
                    ):
                        raise ValueError
                    profile_id = secrets.token_urlsafe(24)
                    probe.profiles[profile_id] = profile
                    public.append(
                        {
                            "id": profile_id,
                            "name": _public_name(
                                profile.name,
                                f"Profile {index}",
                                private=(payload.username, payload.password, profile.token),
                            ),
                            "codec": profile.codec,
                            "width": profile.width,
                            "height": profile.height,
                            "supported": profile.codec == "H264",
                        }
                    )
            except Exception as error:
                # A private network operation may outlive the human session.
                # Recheck failed probes before releasing even safe diagnostics.
                await current_admin(session)
                raise _connector_failure(error) from None
            await current_admin(session)
            if vault.get(identifier, session) is not probe:
                raise HTTPException(410, "camera_probe_expired")
            return {
                "probe_id": identifier,
                "expires_in_seconds": max(0, int(probe.expires - monotonic())),
                "profiles": public,
                "clock": clock,
            }
        except BaseException:
            vault.discard(identifier, probe)
            raise

    @app.post(_PREFIX + "/cameras/connect", status_code=201, tags=["operator"])
    async def connect(
        payload: _ConnectRequest,
        response: Response,
        session: tuple[UserAccount, str] = admin_session,
    ):
        response.headers["Cache-Control"] = "no-store"
        probe = vault.get(payload.probe_id, session)
        if probe.connecting:
            raise HTTPException(409, "camera_busy")
        profile = probe.profiles.get(payload.profile_id)
        if profile is None:
            raise HTTPException(410, "camera_probe_expired")
        if profile.codec != "H264":
            raise HTTPException(422, "camera_profile_unsupported")
        probe.connecting = True
        try:
            result = await enroll(
                name=payload.name,
                host=probe.host,
                management_port=probe.port,
                management_secure=probe.secure,
                source_uri=profile.source_uri,
                session=session,
                onvif=True,
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

            task = asyncio.create_task(expire(), name="operator-camera-connection-expiry")
            try:
                yield
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                vault.clear()

    app.router.lifespan_context = lifespan
    return vault
