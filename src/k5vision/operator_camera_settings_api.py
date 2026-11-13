"""Short-lived human leases for current-camera ONVIF video configuration."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import secrets
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from time import monotonic
from typing import Literal
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator
from starlette.responses import JSONResponse

from k5vision.domain.devices import DeviceProtocol
from k5vision.domain.users import UserRole
from k5vision.operator_role_policy_api import require_permission

_PREFIX = "/api/v1/operator/cameras"
_TTL = 120
_CAPACITY = 16
_FIELDS = (
    "codec",
    "frame_rate",
    "resolution",
    "bitrate_kbps",
    "gop",
    "quality",
    "encoding_interval",
)
_ERRORS = {
    "video_unsupported": 422,
    "video_auth_failed": 422,
    "video_busy": 429,
    "video_unreachable": 503,
    "video_invalid_response": 502,
    "video_configuration_conflict": 409,
    "video_shared_configuration": 409,
    "video_outcome_unverified": 502,
    "video_readback_mismatch": 502,
    "video_closed": 503,
    "video_profile_ambiguous": 422,
    "video_profile_unidentified": 422,
    "video_options_unavailable": 422,
    "video_invalid_request": 422,
    "video_codec_unsupported": 422,
    "video_unsafe_extension": 422,
    "video_recorder_unavailable": 409,
    "video_inspection_expired": 410,
}


def _failure(code):
    if type(code) is not str:
        code = "video_invalid_response"
    return HTTPException(
        _ERRORS.get(code, 502),
        code if code in _ERRORS else "video_invalid_response",
        headers={"Cache-Control": "no-store"},
    )


def _clear(value):
    clear = getattr(value, "clear", None)
    if callable(clear):
        with suppress(Exception):
            clear()


def _fingerprint(source):
    device = source.device.model_dump(mode="json")
    for key in ("tags", "protocols"):
        device[key] = sorted(device[key])
    data = {
        "site": source.binding.site_id,
        "stream_token": source.binding.stream_token,
        "device": device,
        "uri": source.source_uri,
        "recording_enabled": source.recording_enabled,
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).digest()


class _Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str | None = Field(default=None, max_length=256, repr=False)
    password: str | None = Field(default=None, max_length=256, repr=False)

    @model_validator(mode="after")
    def pair(self):
        present = self.model_fields_set & {"username", "password"}
        if present and (
            present != {"username", "password"} or self.username is None or self.password is None
        ):
            raise ValueError("Management credentials require a complete pair")
        return self


class _Resolution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    width: StrictInt = Field(ge=1, le=65535)
    height: StrictInt = Field(ge=1, le=65535)


class _Apply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    settings_handle: str = Field(
        min_length=32, max_length=64, pattern=r"^[A-Za-z0-9_-]+$", repr=False
    )
    codec: Literal["H264"] | None = None
    frame_rate: float | None = Field(
        default=None, gt=0, le=100000, allow_inf_nan=False, strict=True
    )
    resolution: _Resolution | None = None
    bitrate_kbps: StrictInt | None = Field(default=None, ge=0, le=1000000000)
    gop: StrictInt | None = Field(default=None, ge=0, le=1000000000)
    quality: float | None = Field(default=None, ge=0, le=1000000, allow_inf_nan=False, strict=True)
    encoding_interval: StrictInt | None = Field(default=None, ge=1, le=1000000)
    shared_config_ack: StrictBool = False

    @model_validator(mode="after")
    def change(self):
        changed = self.model_fields_set & set(_FIELDS)
        if not changed or any(getattr(self, key) is None for key in changed):
            raise ValueError("Select a video setting")
        return self

    def patch(self):
        result = self.model_dump(
            exclude={"settings_handle", "shared_config_ack"}, exclude_none=True
        )
        result["acknowledge_shared"] = self.shared_config_ack
        return result


def _public(value):
    """Admit only the finite normalized protocol DTO, never camera XML/credentials."""
    data = value.public()
    allowed = {
        "camera_id",
        "status",
        "protocol",
        "current",
        "options",
        "shared_configuration",
        "shared_profile_count",
        "writable",
        "writable_reason",
        "configuration_readback_verified",
        "stream_verified",
        "observed_fps",
        "requested",
    }
    if (
        type(data) is not dict
        or not set(data) <= allowed
        or not {"camera_id", "current", "options", "writable"} <= set(data)
    ):
        raise _failure("video_invalid_response")

    def keys(item, allowed, *, required=()):
        if (
            type(item) is not dict
            or not set(item) <= set(allowed)
            or not set(required) <= set(item)
        ):
            raise _failure("video_invalid_response")

    keys(data["current"], _FIELDS)
    keys(
        data["options"],
        {
            "codecs",
            "resolutions",
            "writable_fields",
            "frame_rate",
            "bitrate_kbps",
            "gop",
            "quality",
            "encoding_interval",
        },
    )
    codecs = {"H264", "H265", "MJPEG", "MPEG4", "UNKNOWN"}
    if data["current"].get("codec") not in codecs:
        raise _failure("video_invalid_response")
    keys(data["current"].get("resolution"), {"width", "height"})
    for option in data["options"].get("codecs", []):
        keys(option, {"codec", "device_supported", "product_supported"})
        if option.get("codec") not in codecs or (
            option.get("codec") != "H264" and option.get("product_supported") is not False
        ):
            raise _failure("video_invalid_response")
    for option in data["options"].get("resolutions", []):
        keys(option, {"width", "height"})
    for name in ("frame_rate", "bitrate_kbps", "gop", "quality", "encoding_interval"):
        option = data["options"].get(name)
        if option is not None:
            keys(option, {"min", "max", "values", "integer_only"})
    if not set(data["options"].get("writable_fields", [])) <= set(_FIELDS):
        raise _failure("video_invalid_response")
    if "requested" in data:
        keys(data["requested"], {*_FIELDS, "acknowledge_shared"})
        if "resolution" in data["requested"]:
            keys(data["requested"]["resolution"], {"width", "height"})
    if data.get("status") != "available" or data.get("protocol") not in {"media1", "media2"}:
        raise _failure("video_invalid_response")

    def admit(item, depth=0):
        if depth > 8:
            raise _failure("video_invalid_response")
        if item is None or type(item) is bool:
            return
        if type(item) in (int, float):
            if not math.isfinite(item) or abs(item) > 2**63 - 1:
                raise _failure("video_invalid_response")
            return
        if type(item) is str:
            if len(item) > 128 or "://" in item or any(ord(c) < 32 for c in item):
                raise _failure("video_invalid_response")
            return
        if type(item) is list and len(item) <= 256:
            for child in item:
                admit(child, depth + 1)
            return
        if type(item) is dict and len(item) <= 32:
            for key, child in item.items():
                if type(key) is not str or len(key) > 64 or not key.replace("_", "").isalnum():
                    raise _failure("video_invalid_response")
                admit(child, depth + 1)
            return
        raise _failure("video_invalid_response")

    admit(data)
    if len(json.dumps(data, allow_nan=False).encode()) > 32768:
        raise _failure("video_invalid_response")
    if data.get("observed_fps") is not None or data.get("stream_verified") is not False:
        raise _failure("video_invalid_response")
    return data


@dataclass(eq=False)
class _Lease:
    owner: tuple[UUID, bytes]
    camera_id: UUID
    source_fingerprint: bytes = field(repr=False)
    expires: float
    phase: str = "reading"
    invalid: bool = False
    inspection: object = field(default=None, repr=False)


class CameraSettingsVault:
    """Bound stored and in-flight private inspections to sixteen one-use leases."""

    def __init__(self, service, *, owned_service=False):
        self._leases = {}
        self._revision_key = secrets.token_bytes(32)
        self._tasks = set()
        self._service = service
        self._owned_service = owned_service
        self._closed = False
        self._service_closed = False

    @staticmethod
    def owner(session):
        return session[0].id, hashlib.sha256(session[1].encode("ascii")).digest()

    def source_revision(self, source):
        return hmac.new(self._revision_key, _fingerprint(source), hashlib.sha256).hexdigest()

    def discard(self, identifier, lease):
        lease.invalid = True
        if lease.phase == "idle":
            _clear(lease.inspection)
            lease.inspection = None
            if self._leases.get(identifier) is lease:
                del self._leases[identifier]

    def prune(self):
        for identifier, lease in tuple(self._leases.items()):
            if lease.expires <= monotonic():
                self.discard(identifier, lease)

    def reserve(self, session, camera_id, source):
        self.prune()
        if self._closed:
            raise _failure("video_closed")
        owner = self.owner(session)
        for identifier, lease in tuple(self._leases.items()):
            if lease.owner == owner and lease.camera_id == camera_id:
                if lease.phase == "applying" and not lease.invalid:
                    raise HTTPException(409, "video_apply_in_progress")
                self.discard(identifier, lease)
        if len(self._leases) >= _CAPACITY:
            raise _failure("video_busy")
        identifier = secrets.token_urlsafe(24)
        lease = _Lease(owner, camera_id, _fingerprint(source), monotonic() + _TTL)
        self._leases[identifier] = lease
        return identifier, lease

    def get(self, identifier, session, camera_id):
        self.prune()
        lease = self._leases.get(identifier)
        if (
            self._closed
            or lease is None
            or lease.invalid
            or lease.owner != self.owner(session)
            or lease.camera_id != camera_id
        ):
            raise HTTPException(
                410, "video_settings_expired", headers={"Cache-Control": "no-store"}
            )
        return lease

    def release(self, identifier, lease):
        lease.phase = "idle"
        self.discard(identifier, lease)

    def clear_token(self, token):
        digest = hashlib.sha256(token.encode("ascii")).digest()
        for identifier, lease in tuple(self._leases.items()):
            if lease.owner[1] == digest:
                self.discard(identifier, lease)

    def clear_user(self, user_id):
        for identifier, lease in tuple(self._leases.items()):
            if lease.owner[0] == user_id:
                self.discard(identifier, lease)

    def clear(self):
        for identifier, lease in tuple(self._leases.items()):
            self.discard(identifier, lease)

    def task(self, awaitable, *, inspection_lease=None):
        task = asyncio.create_task(awaitable)
        self._tasks.add(task)

        def finished(completed):
            if inspection_lease is not None and not completed.cancelled():
                if completed.exception() is None:
                    inspection = completed.result()
                    if self._closed or inspection_lease.invalid:
                        _clear(inspection)
                    else:
                        inspection_lease.inspection = inspection
            self._tasks.discard(completed)

        task.add_done_callback(finished)
        return task

    async def aclose(self):
        self._closed = True
        self.clear()
        for task in tuple(self._tasks):
            with suppress(Exception, asyncio.CancelledError):
                await _joined(task)
        # All native work and internal reconciliation have finished. A request
        # may still be awaiting final authorization; it must not retain private
        # management material beyond this owner's shutdown boundary.
        for identifier, lease in tuple(self._leases.items()):
            self.release(identifier, lease)
        if self._owned_service and not self._service_closed:
            self._service_closed = True
            await self._service.aclose()


async def _joined(task, *, on_cancel=None):
    """A canceled request cannot abandon a started native write or its finalization."""
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            return result, cancelled
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
            if on_cancel is not None:
                on_cancel()
        except BaseException:
            if cancelled:
                raise asyncio.CancelledError from None
            raise


def install_operator_camera_settings_api(
    app: FastAPI,
    *,
    require_admin,
    authenticate,
    selection,
    mutations,
    commit_settings,
    before_apply=None,
    reconcile_failed_write=None,
    service=None,
):
    """Install the lease boundary; the owner supplies targeted durable reconciliation."""
    from k5vision.operator_camera_settings import OnvifVideoSettingsService
    from k5vision.operator_product_api import _BoundedProductBody

    owned = service is None
    service = OnvifVideoSettingsService() if owned else service
    vault = CameraSettingsVault(service, owned_service=owned)
    app.state.operator_camera_settings_vault = vault

    async def settings_admin(request: Request):
        try:
            session = await require_admin(request)
            await current(session)
            return session
        except HTTPException:
            values = request.headers.getlist("authorization")
            if len(values) == 1:
                scheme, _, token = values[0].partition(" ")
                if scheme.lower() == "bearer" and token.isascii() and len(token) <= 128:
                    vault.clear_token(token)
            raise

    dependency = Depends(settings_admin)
    if not any(middleware.cls is _BoundedProductBody for middleware in app.user_middleware):
        app.add_middleware(_BoundedProductBody)
    previous_validation = app.exception_handlers.get(
        RequestValidationError, request_validation_exception_handler
    )

    @app.exception_handler(RequestValidationError)
    async def validation(request: Request, error):
        path = request.url.path
        if path.startswith(_PREFIX + "/") and (
            path.endswith("/video-settings") or path.endswith("/video-settings/read")
        ):
            return JSONResponse(
                {"detail": "video_invalid_request"},
                status_code=422,
                headers={"Cache-Control": "no-store"},
            )
        return await previous_validation(request, error)

    async def current(session):
        account, token = await authenticate(session[1])
        if account.id != session[0].id or account.role is not UserRole.ADMINISTRATOR:
            raise HTTPException(403, "Insufficient camera administration permission")
        require_permission(app, account, "cameras.manage")
        return account, token

    def match(lease, source):
        if lease.source_fingerprint != _fingerprint(source):
            raise HTTPException(409, "video_source_changed", headers={"Cache-Control": "no-store"})

    async def read(camera_id, response, session, credentials):
        response.headers["Cache-Control"] = "no-store"
        account, _ = await current(session)
        source = selection(account, camera_id)
        if DeviceProtocol.ONVIF not in source.device.protocols:
            credentials.username = credentials.password = None
            return {
                "camera_id": str(camera_id),
                "status": "unavailable",
                "writable": False,
                "writable_reason": "video_unsupported",
                "current": None,
                "options": None,
                "shared_configuration": False,
                "shared_profile_count": 0,
                "configuration_readback_verified": False,
                "stream_verified": False,
                "observed_fps": None,
                "source_revision": vault.source_revision(source),
            }
        identifier, lease = vault.reserve(session, camera_id, source)
        inspection = None
        try:
            (inspection, cancelled) = await _joined(
                vault.task(
                    service.inspect(
                        source, username=credentials.username, password=credentials.password
                    ),
                    inspection_lease=lease,
                )
            )
            if cancelled:
                raise asyncio.CancelledError
            vault.get(identifier, session, camera_id)
            account, _ = await current(session)
            match(lease, selection(account, camera_id))
            vault.get(identifier, session, camera_id)
            public = _public(inspection)
            if public.get("camera_id") != str(camera_id):
                raise _failure("video_invalid_response")
            lease.inspection = inspection
            inspection = None
            lease.phase = "idle"
            return {
                **public,
                "settings_handle": identifier,
                "expires_in_seconds": max(0, int(lease.expires - monotonic())),
                "source_revision": vault.source_revision(source),
            }
        except HTTPException:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise _failure(getattr(error, "code", "video_invalid_response")) from None
        finally:
            credentials.username = credentials.password = None
            _clear(inspection)
            if lease.phase != "idle":
                vault.release(identifier, lease)

    @app.get(_PREFIX + "/{camera_id}/video-settings", tags=["camera video settings"])
    async def inspect_stored(camera_id: UUID, response: Response, session=dependency):
        return await read(camera_id, response, session, _Credentials())

    @app.post(_PREFIX + "/{camera_id}/video-settings/read", tags=["camera video settings"])
    async def inspect_credentials(
        camera_id: UUID, payload: _Credentials, response: Response, session=dependency
    ):
        return await read(camera_id, response, session, payload)

    @app.patch(_PREFIX + "/{camera_id}/video-settings", tags=["camera video settings"])
    async def apply_settings(
        camera_id: UUID, payload: _Apply, response: Response, session=dependency
    ):
        response.headers["Cache-Control"] = "no-store"
        await current(session)
        identifier = payload.settings_handle
        lease = vault.get(identifier, session, camera_id)
        if lease.phase != "idle":
            raise HTTPException(409, "video_apply_in_progress")
        lease.phase = "applying"
        dispatch_possible = False
        request_cancelled = False

        def cancelled_before_completion():
            nonlocal request_cancelled
            request_cancelled = True

        try:
            async with mutations:
                account, _ = await current(session)
                vault.get(identifier, session, camera_id)
                source = selection(account, camera_id)
                match(lease, source)
                patch = service.validate_patch(lease.inspection, payload.patch())
                if before_apply is not None:
                    before_apply(source)

                async def before_write():
                    nonlocal dispatch_possible
                    if request_cancelled:
                        raise HTTPException(409, "video_apply_cancelled")
                    actor, _ = await current(session)
                    vault.get(identifier, session, camera_id)
                    match(lease, selection(actor, camera_id))
                    if before_apply is not None:
                        before_apply(source)
                    if request_cancelled:
                        raise HTTPException(409, "video_apply_cancelled")
                    dispatch_possible = True

                async def critical():
                    result = None
                    try:
                        result = await service.apply(
                            lease.inspection, patch, before_write=before_write
                        )
                        verification = await commit_settings(source, lease.inspection, result)
                        if (
                            type(verification) is not dict
                            or type(verification.get("stream_verified")) is not bool
                            or not set(verification)
                            <= {
                                "stream_verified",
                                "stream_status",
                                "recording_state",
                                "reconnect_requested",
                                "verification_pending",
                            }
                            or verification.get("stream_status", "unavailable")
                            not in {"live", "connecting", "unavailable"}
                            or verification.get("recording_state", "waiting")
                            not in {"recording", "waiting", "paused", "error", "stalled"}
                            or any(
                                type(verification[name]) is not bool
                                for name in ("reconnect_requested", "verification_pending")
                                if name in verification
                            )
                        ):
                            raise _failure("video_outcome_unverified")
                        public = _public(result)
                        return public, verification
                    except BaseException as error:
                        if (
                            result is None
                            and dispatch_possible
                            and reconcile_failed_write is not None
                        ):
                            # The camera may have accepted Set even if its reply/readback failed.
                            # Reconnect only the unchanged saved source; never retry Set or claim
                            # that the external change was rolled back or verified.
                            with suppress(Exception):
                                await reconcile_failed_write(source, lease.inspection)
                        if isinstance(error, HTTPException | asyncio.CancelledError):
                            raise
                        raise _failure(getattr(error, "code", "video_outcome_unverified")) from None
                    finally:
                        _clear(result)

                (value, cancelled) = await _joined(
                    vault.task(critical()), on_cancel=cancelled_before_completion
                )
                if cancelled:
                    raise asyncio.CancelledError
                public, verification = value
                if vault._closed:
                    raise _failure("video_closed")
                account, _ = await current(session)
                if vault._closed:
                    raise _failure("video_closed")
                source = selection(account, camera_id)
                return {
                    **public,
                    "verification": verification,
                    "stream_verified": verification["stream_verified"],
                    "source_revision": vault.source_revision(source),
                }
        except HTTPException:
            if dispatch_possible:
                await current(session)
            raise
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise _failure(getattr(error, "code", "video_invalid_request")) from None
        finally:
            vault.release(identifier, lease)

    previous_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with previous_lifespan(application):

            async def expire():
                while True:
                    await asyncio.sleep(5)
                    vault.prune()

            task = asyncio.create_task(expire(), name="operator-video-settings-expiry")
            try:
                yield
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                await vault.aclose()

    app.router.lifespan_context = lifespan
    return vault
