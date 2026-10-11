"""Human-authorized camera administration and same-origin media admission."""

from __future__ import annotations

import asyncio
import ipaddress
import math
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlsplit
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, IPvAnyAddress
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from k5vision.domain.devices import Device, DeviceCreate, DeviceProtocol
from k5vision.domain.users import UserAccount, UserRole
from k5vision.media.mediamtx_config import MediaMTXSource
from k5vision.media.mediamtx_provider import (
    MediaMTXBinding,
    MediaWorkerError,
    MediaWorkerErrorCode,
)
from k5vision.operator_product_state import OperatorCameraCapacityError
from k5vision.services.device_registry import (
    DeviceRegistryCapacityError,
    DeviceRegistryConflictError,
    DeviceRegistryStorageError,
)
from k5vision.services.user_registry import UserRegistryStorageError

if TYPE_CHECKING:
    from k5vision.operator_product_service import OperatorProductService
    from k5vision.operator_product_state import OperatorCameraStore

_PREFIX = "/api/v1/operator"
_COOKIE = "k5_operator_media"
_BODY_LIMIT = 16 * 1024
_READ_ROLES = frozenset(UserRole)
_HLS_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.(?:m3u8|mp4|m4s|ts)")
_RFC3339 = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})")


def _product_path(path: str) -> bool:
    return (
        path == _PREFIX + "/cameras"
        or path.startswith(_PREFIX + "/cameras/")
        or path
        in {
            _PREFIX + "/media-auth",
            _PREFIX + "/media-session",
        }
    )


class _BoundedProductBody:
    """Buffer at most one small mutation body before FastAPI parses credentials."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") not in {"POST", "DELETE", "PATCH", "PUT"}
            or not _product_path(scope.get("path", ""))
        ):
            await self.app(scope, receive, send)
            return
        lengths = [value for name, value in scope.get("headers", ()) if name == b"content-length"]
        if len(lengths) > 1:
            await JSONResponse({"detail": "Invalid request length"}, status_code=400)(
                scope, receive, send
            )
            return
        if lengths:
            try:
                length = int(lengths[0])
                if length < 0:
                    raise ValueError
            except ValueError:
                await JSONResponse({"detail": "Invalid request length"}, status_code=400)(
                    scope, receive, send
                )
                return
            if length > _BODY_LIMIT:
                await JSONResponse({"detail": "Request body too large"}, status_code=413)(
                    scope, receive, send
                )
                return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] != "http.request":
                await JSONResponse({"detail": "Invalid request body"}, status_code=400)(
                    scope, receive, send
                )
                return
            piece = message.get("body", b"")
            if len(body) + len(piece) > _BODY_LIMIT:
                await JSONResponse({"detail": "Request body too large"}, status_code=413)(
                    scope, receive, send
                )
                return
            body.extend(piece)
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


class _CameraEnrollment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    host: IPvAnyAddress
    source_uri: str = Field(min_length=1, max_length=4096, repr=False)


def _unauthorized() -> HTTPException:
    return HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Bearer"})


def _timestamp(value: str) -> datetime:
    if not isinstance(value, str) or len(value) > 40 or _RFC3339.fullmatch(value) is None:
        raise ValueError
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError
    return result.astimezone(UTC)


def _interval(start: str | None, end: str | None) -> tuple[datetime, datetime]:
    last = datetime.now(UTC) if end is None else _timestamp(end)
    first = last - timedelta(hours=24) if start is None else _timestamp(start)
    if not timedelta(0) < last - first <= timedelta(hours=24):
        raise ValueError
    return first, last


def _query(value: str, *, allowed: set[str], required: set[str]) -> dict[str, str]:
    pairs = parse_qsl(value, keep_blank_values=True, strict_parsing=True, max_num_fields=8)
    result: dict[str, str] = {}
    for key, item in pairs:
        if key not in allowed or key in result or not item:
            raise ValueError
        result[key] = item
    if not required.issubset(result):
        raise ValueError
    return result


def _media_selection(uri: str) -> str:
    """Accept only the generated worker's bounded read routes and query grammar."""
    if (
        not uri
        or len(uri) > 2048
        or any(ord(char) <= 32 or ord(char) == 127 for char in uri)
        or "\\" in uri
        or re.search(r"%(?![0-9A-Fa-f]{2})", uri)
    ):
        raise ValueError
    parsed = urlsplit(uri)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        raise ValueError
    if parsed.path.startswith("/media/live/"):
        parts = parsed.path.split("/")
        if len(parts) != 5 or _HLS_FILE.fullmatch(parts[4]) is None or ".." in parts[4]:
            raise ValueError
        params = _query(
            parsed.query, allowed={"_HLS_msn", "_HLS_part", "_HLS_skip"}, required=set()
        )
        for key, value in params.items():
            if key == "_HLS_skip":
                if value not in {"YES", "v2"}:
                    raise ValueError
            elif not value.isascii() or not value.isdecimal() or len(value) > 19:
                raise ValueError
            elif int(value) > 2**63 - 1:
                raise ValueError
        path = parts[3]
    elif parsed.path == "/media/playback":
        params = _query(
            parsed.query,
            allowed={"path", "start", "duration", "format"},
            required={"path", "start", "duration"},
        )
        start = _timestamp(params["start"])
        duration = float(params["duration"])
        if not math.isfinite(duration) or not 1 <= duration <= 300:
            raise ValueError
        # This also rejects calendar overflow before handing an interval to the worker.
        end = start + timedelta(seconds=duration)
        if not timedelta(0) < end - start <= timedelta(hours=24):
            raise ValueError
        if params.get("format", "fmp4") not in {"mp4", "fmp4"}:
            raise ValueError
        path = params["path"]
    else:
        raise ValueError
    if re.fullmatch(r"k5_[0-9a-f]{64}", path) is None:
        raise ValueError
    return path


def install_operator_product_api(
    app: FastAPI,
    *,
    cameras: OperatorCameraStore,
    service: OperatorProductService,
    public_port: int,
    analytics=None,
) -> None:
    """Install the local product contract without exposing execution credentials."""
    if type(public_port) is not int or not 1 <= public_port <= 65535:
        raise ValueError("operator public port is invalid")
    registry = app.state.device_registry
    users = app.state.user_registry
    sessions = app.state.user_session_manager
    if registry is None or users is None or registry.site_id != service.site_id:
        raise ValueError("operator identity state is unavailable or belongs to another site")
    if users.site_id != service.site_id:
        raise ValueError("operator accounts belong to another site")
    mutations = asyncio.Lock()
    seen_live: set[UUID] = set()
    app.add_middleware(_BoundedProductBody)
    previous_validation_handler = app.exception_handlers.get(
        RequestValidationError, request_validation_exception_handler
    )

    @app.exception_handler(RequestValidationError)
    async def product_validation_error(request: Request, exc: RequestValidationError):
        if _product_path(request.url.path):
            return JSONResponse({"detail": "Invalid operator request"}, status_code=422)
        return await previous_validation_handler(request, exc)

    async def authenticate(token: str) -> tuple[UserAccount, str]:
        principal = await sessions.resolve(token)
        if principal is None:
            raise _unauthorized()
        try:
            current = users.get(principal.id)
        except UserRegistryStorageError:
            raise HTTPException(503, "Operator identity unavailable") from None
        if current is None or not current.enabled or current.role not in _READ_ROLES:
            raise _unauthorized()
        return current, token

    async def require_session(request: Request) -> tuple[UserAccount, str]:
        values = request.headers.getlist("authorization")
        value = values[0] if len(values) == 1 else ""
        scheme, separator, token = value.partition(" ")
        if not separator or scheme.lower() != "bearer" or len(token) > 128:
            raise _unauthorized()
        return await authenticate(token)

    def admit_origin(request: Request) -> None:
        values = request.headers.getlist("origin")
        if not values:
            return
        if len(values) != 1:
            raise HTTPException(403, "Invalid operator origin")
        try:
            origin = urlsplit(values[0])
            host = origin.hostname
            loopback = host == "localhost" or ipaddress.ip_address(host or "").is_loopback
            valid = (
                origin.scheme in {"http", "https"}
                and loopback
                and origin.port == public_port
                and not origin.username
                and not origin.password
                and not origin.path
                and not origin.query
                and not origin.fragment
            )
        except ValueError:
            valid = False
        if not valid:
            raise HTTPException(403, "Invalid operator origin")

    async def require_admin(request: Request) -> tuple[UserAccount, str]:
        principal, token = await require_session(request)
        if principal.role is not UserRole.ADMINISTRATOR:
            raise HTTPException(403, "Insufficient camera administration permission")
        admit_origin(request)
        return principal, token

    def selections() -> tuple[MediaMTXSource, ...]:
        try:
            selected = cameras.list_sources()
            if len(selected) > 64:
                raise ValueError
            for source in selected:
                if source.binding.site_id != service.site_id:
                    raise ValueError
                device = registry.get(source.binding.device_id)
                if device is None or device.model_dump() != source.device.model_dump():
                    raise ValueError
            return selected
        except (RuntimeError, OSError, ValueError):
            raise HTTPException(503, "Camera state unavailable") from None

    def selection(device_id: UUID) -> MediaMTXSource:
        for source in selections():
            if source.binding.device_id == device_id:
                return source
        raise HTTPException(404, "Camera not found")

    async def camera_view(source: MediaMTXSource) -> dict:
        result = {
            "device_id": str(source.binding.device_id),
            "name": source.device.name,
            "host": str(source.device.host),
            "status": "unavailable",
            "recording": False,
            "analytics_status": "disabled",
        }
        if service.status() not in {"ready", "disk_full"}:
            return result
        try:
            state = await service.provider.path_state(source.binding)
            configuration = await service.provider.path_configuration(source.binding)
            if state.online and state.available:
                seen_live.add(source.binding.device_id)
                result["status"] = "live"
                result["recording"] = configuration.recording_enabled
            elif source.binding.device_id in seen_live or state.available:
                result["status"] = "disconnected"
            else:
                result["status"] = "connecting"
        except MediaWorkerError as exc:
            if exc.code is MediaWorkerErrorCode.NOT_FOUND:
                result["status"] = "connecting"
        return result

    read_session = Depends(require_session)
    admin_session = Depends(require_admin)

    @app.get(_PREFIX + "/cameras", tags=["operator"])
    async def list_cameras(_session: tuple[UserAccount, str] = read_session):
        selected = selections()
        semaphore = asyncio.Semaphore(4)

        async def view(source):
            async with semaphore:
                return await camera_view(source)

        return {
            "cameras": await asyncio.gather(*(view(source) for source in selected)),
            "worker_status": service.status(),
        }

    @app.post(_PREFIX + "/cameras", status_code=201, tags=["operator"])
    async def enroll_camera(
        payload: _CameraEnrollment, _session: tuple[UserAccount, str] = admin_session
    ):
        try:
            request = DeviceCreate(
                name=payload.name, host=payload.host, protocols={DeviceProtocol.RTSP}
            )
            provisional = Device(**request.model_dump())
            MediaMTXSource(
                MediaMTXBinding(service.site_id, provisional.id), provisional, payload.source_uri
            )
        except ValueError:
            raise HTTPException(422, "Invalid camera selection") from None
        async with mutations:
            try:
                existing = selections()
                if len(existing) >= 64 and not any(
                    source.device.host == request.host
                    and source.device.management_port == request.management_port
                    for source in existing
                ):
                    raise HTTPException(409, "Camera capacity reached")
                device = registry.enroll(request).device
                source = cameras.upsert(device, payload.source_uri)
                await service.apply_sources(selections())
            except DeviceRegistryConflictError:
                raise HTTPException(409, "Camera endpoint already enrolled") from None
            except DeviceRegistryCapacityError:
                raise HTTPException(409, "Camera capacity reached") from None
            except OperatorCameraCapacityError:
                raise HTTPException(409, "Camera capacity reached") from None
            except (DeviceRegistryStorageError, RuntimeError, OSError, ValueError):
                raise HTTPException(503, "Camera configuration unavailable") from None
        return await camera_view(source)

    @app.delete(_PREFIX + "/cameras/{device_id}", status_code=204, tags=["operator"])
    async def remove_camera(device_id: UUID, _session: tuple[UserAccount, str] = admin_session):
        async with mutations:
            selection(device_id)
            try:
                cameras.remove(device_id)
                seen_live.discard(device_id)
                await service.apply_sources(selections())
            except (RuntimeError, OSError, ValueError):
                raise HTTPException(503, "Camera configuration unavailable") from None
        return Response(status_code=204)

    @app.get(_PREFIX + "/cameras/{device_id}/history", tags=["operator"])
    async def camera_history(
        device_id: UUID,
        start: str | None = None,
        end: str | None = None,
        _session: tuple[UserAccount, str] = read_session,
    ):
        source = selection(device_id)
        try:
            first, last = _interval(start, end)
        except (ValueError, OverflowError):
            raise HTTPException(
                422, "Playback requires an aware interval of at most 24 hours"
            ) from None
        if service.status() not in {"ready", "disk_full"}:
            raise HTTPException(503, "Media unavailable")
        try:
            spans = await service.provider.playback_spans(source.binding, start=first, end=last)
        except MediaWorkerError:
            raise HTTPException(503, "Playback inventory unavailable") from None
        return {
            "spans": [
                {
                    "recording_id": span.recording_id,
                    "started_at": span.started_at,
                    "duration_seconds": span.duration_seconds,
                }
                for span in spans
            ]
        }

    @app.post(_PREFIX + "/cameras/{device_id}/media-session", tags=["operator"])
    async def media_session(
        device_id: UUID,
        request: Request,
        response: Response,
        session: tuple[UserAccount, str] = read_session,
    ):
        admit_origin(request)
        source = selection(device_id)
        response.set_cookie(
            _COOKIE,
            session[1],
            max_age=8 * 60 * 60,
            path="/media",
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
        )
        return {
            "live_url": f"/media/live/{source.binding.path_name}/index.m3u8",
            "playback_url": "/media/playback?" + urlencode({"path": source.binding.path_name}),
            "analytics_url": None,
        }

    @app.delete(_PREFIX + "/media-session", status_code=204, tags=["operator"])
    async def clear_media_session(request: Request):
        admit_origin(request)
        response = Response(status_code=204)
        response.delete_cookie(
            _COOKIE,
            path="/media",
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
        )
        return response

    @app.get(_PREFIX + "/media-auth", tags=["operator"])
    async def media_authorization(request: Request):
        cookie_headers = request.headers.getlist("cookie")
        if len(cookie_headers) != 1 or len(cookie_headers[0]) > 4096:
            raise _unauthorized()
        tokens = []
        for part in cookie_headers[0].split(";"):
            name, separator, value = part.strip().partition("=")
            if name == _COOKIE and separator:
                tokens.append(value)
        if len(tokens) != 1:
            raise _unauthorized()
        await authenticate(tokens[0])
        methods = request.headers.getlist("x-forwarded-method")
        uris = request.headers.getlist("x-forwarded-uri")
        if methods != ["GET"] or len(uris) != 1:
            raise _unauthorized()
        try:
            path = _media_selection(uris[0])
        except (ValueError, OverflowError):
            raise _unauthorized() from None
        if not any(source.binding.path_name == path for source in selections()):
            raise _unauthorized()
        if service.status() not in {"ready", "disk_full"}:
            raise HTTPException(503, "Media unavailable")
        return Response(status_code=200)
