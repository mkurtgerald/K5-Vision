"""Human-authorized recording policies and immutable, short-lived zone previews."""

from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from time import monotonic
from uuid import UUID

from fastapi import Depends, HTTPException, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from pydantic import Field, StrictInt
from starlette.responses import JSONResponse

from k5vision.domain.users import UserRole
from k5vision.operator_camera_settings_api import _joined
from k5vision.operator_motion_process import sampling_diagnostic
from k5vision.operator_recording_policy import (
    MotionPolicy,
    MotionReference,
    RecordingPolicyAuthorizationChanged,
    validate_motion_policy,
)

_PREFIX = "/api/v1/operator/cameras"
_TTL = 120
_CAPACITY = 8
_JPEG_LIMIT = 1024 * 1024
_OPAQUE = re.compile(r"^[A-Za-z0-9_-]{32,64}$")
_REVISION = re.compile(r"^[0-9a-f]{64}$")
_STATUSES = frozenset(
    {
        "continuous",
        "armed",
        "recording",
        "post_capture",
        "paused",
        "unavailable",
        "storage_blocked",
        "zones_need_review",
    }
)
_ERRORS = {
    "motion_invalid_request": 422,
    "motion_policy_conflict": 409,
    "motion_source_changed": 409,
    "motion_preview_expired": 410,
    "motion_preview_unavailable": 503,
    "motion_zones_empty": 422,
    "motion_policy_unavailable": 503,
    "motion_busy": 429,
    "motion_permission_changed": 403,
}


def _failure(code="motion_policy_unavailable"):
    if code not in _ERRORS:
        code = "motion_policy_unavailable"
    return HTTPException(_ERRORS[code], code, headers={"Cache-Control": "no-store"})


def _source_key(source):
    """Execution-only comparison; display labels and master Pause are unrelated."""
    return (
        source.binding,
        str(source.device.host),
        source.device.management_port,
        tuple(sorted(source.device.protocols)),
        source.source_uri,
        getattr(source, "profile_key", ""),
    )


def _observed(value):
    if value is None:
        return None
    try:
        if type(value) is not dict or set(value) != {
            "width",
            "height",
            "rotation",
            "source_revision",
            "generation",
        }:
            raise ValueError
        reference = MotionReference.model_validate(
            {k: v for k, v in value.items() if k != "generation"}
        )
        generation = value["generation"]
        if type(generation) is not int or not 1 <= generation < 2**63:
            raise ValueError
        return {**reference.model_dump(mode="json"), "generation": generation}
    except (ValueError, TypeError):
        raise _failure() from None


def _public(value, source):
    try:
        if type(value) is not dict:
            raise ValueError
        policy = validate_motion_policy({k: value[k] for k in MotionPolicy.model_fields})
        if value.get("camera_id") != str(source.binding.device_id):
            raise ValueError
        revision = value["revision"]
        if type(revision) is not int or not 0 <= revision < 2**63:
            raise ValueError
        for name in ("source_revision", "policy_source_revision"):
            if type(value.get(name)) is not str or not _REVISION.fullmatch(value[name]):
                raise ValueError
        if type(value.get("pre_capture_seconds")) is not int or value["pre_capture_seconds"] != 0:
            raise ValueError
        if value.get("status") not in _STATUSES:
            raise ValueError
        return {
            "camera_id": value["camera_id"],
            "revision": revision,
            "source_revision": value["source_revision"],
            "policy_source_revision": value["policy_source_revision"],
            "pre_capture_seconds": 0,
            "status": value["status"],
            "observed_reference": _observed(value.get("observed_reference")),
            "sampling": sampling_diagnostic(value["sampling"])
            if value.get("sampling") is not None
            else None,
            **policy,
        }
    except (KeyError, TypeError, ValueError, RuntimeError):
        raise _failure() from None


class _Apply(MotionPolicy):
    expected_revision: StrictInt = Field(ge=0, lt=2**63 - 1)
    preview_id: str | None = Field(
        default=None, min_length=32, max_length=64, pattern=_OPAQUE.pattern
    )

    def policy(self):
        return self.model_dump(mode="json", exclude={"expected_revision", "preview_id"})


@dataclass(eq=False)
class _Preview:
    owner: tuple[UUID, bytes]
    camera_id: UUID
    source: object = field(repr=False)
    policy_revision: int
    expires: float
    reference: dict | None = None
    jpeg: bytes | None = field(default=None, repr=False)
    phase: str = "capturing"
    invalid: bool = False


@dataclass(eq=False)
class _Mutation:
    owner: tuple[UUID, bytes]
    invalid: bool = False


class MotionPreviewVault:
    """Bound in-flight work and private pixels to eight current human leases."""

    def __init__(self):
        self._leases = {}
        self._tasks = set()
        self._closed = False
        self._read_slots = asyncio.Semaphore(4)
        self._expiry = None
        self._mutations = set()

    @staticmethod
    def owner(session):
        return session[0].id, hashlib.sha256(session[1].encode("ascii")).digest()

    def discard(self, identifier, lease):
        lease.invalid = True
        lease.jpeg = None
        if lease.phase == "idle" and self._leases.get(identifier) is lease:
            del self._leases[identifier]

    def prune(self):
        for identifier, lease in tuple(self._leases.items()):
            if lease.expires <= monotonic():
                self.discard(identifier, lease)

    def reserve(self, session, camera_id, source, revision):
        self.prune()
        if self._closed:
            raise _failure()
        owner = self.owner(session)
        for identifier, lease in tuple(self._leases.items()):
            if lease.owner == owner and lease.camera_id == camera_id:
                if lease.phase != "idle" and not lease.invalid:
                    raise _failure("motion_busy")
                self.discard(identifier, lease)
        if len(self._leases) >= _CAPACITY:
            raise _failure("motion_busy")
        identifier = secrets.token_urlsafe(24)
        lease = _Preview(owner, camera_id, _source_key(source), revision, monotonic() + _TTL)
        self._leases[identifier] = lease
        return identifier, lease

    def get(self, identifier, session, camera_id):
        self.prune()
        lease = self._leases.get(identifier)
        if (
            self._closed
            or lease is None
            or lease.invalid
            or lease.jpeg is None
            or lease.owner != self.owner(session)
            or lease.camera_id != camera_id
        ):
            raise _failure("motion_preview_expired")
        return lease

    def release(self, identifier, lease):
        lease.phase = "idle"
        self.discard(identifier, lease)

    def clear_token(self, token):
        digest = hashlib.sha256(token.encode("ascii")).digest()
        for operation in tuple(self._mutations):
            if operation.owner[1] == digest:
                operation.invalid = True
        for identifier, lease in tuple(self._leases.items()):
            if lease.owner[1] == digest:
                self.discard(identifier, lease)

    def clear_user(self, user_id):
        for operation in tuple(self._mutations):
            if operation.owner[0] == user_id:
                operation.invalid = True
        for identifier, lease in tuple(self._leases.items()):
            if lease.owner[0] == user_id:
                self.discard(identifier, lease)

    def clear_camera(self, camera_id):
        for identifier, lease in tuple(self._leases.items()):
            if lease.camera_id == camera_id:
                self.discard(identifier, lease)

    def task(self, awaitable):
        task = asyncio.create_task(awaitable)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def read(self, operation):
        async with self._read_slots:
            result, cancelled = await _joined(self.task(asyncio.to_thread(operation)))
            if cancelled:
                raise asyncio.CancelledError
            return result

    async def aclose(self):
        self._closed = True
        for operation in tuple(self._mutations):
            operation.invalid = True
        if self._expiry is not None and self._expiry is not asyncio.current_task():
            self._expiry.cancel()
            with suppress(asyncio.CancelledError):
                await self._expiry
            self._expiry = None
        for identifier, lease in tuple(self._leases.items()):
            self.discard(identifier, lease)
        for task in tuple(self._tasks):
            with suppress(Exception, asyncio.CancelledError):
                await _joined(task)
        for identifier, lease in tuple(self._leases.items()):
            self.release(identifier, lease)


def install_operator_recording_policy_api(
    app,
    *,
    require_admin,
    authenticate,
    authenticate_preview,
    selection,
    mutations,
    service,
    authority_read=None,
    capture_authority_fence=None,
):
    """Install policies; the service owns recording, native work and compensation.

    The supplied preview authenticator must check recording.manage AND
    cameras.view_live against the same fresh authority snapshot. No cached
    preview handle, browser pixel or URL alone authorizes image access.
    """
    vault = MotionPreviewVault()
    app.state.operator_motion_preview_vault = vault
    dependency = Depends(require_admin)
    read = vault.read if authority_read is None else authority_read
    previous_validation = app.exception_handlers.get(
        RequestValidationError, request_validation_exception_handler
    )

    @app.exception_handler(RequestValidationError)
    async def validation(request: Request, error):
        if request.url.path.startswith(_PREFIX + "/") and (
            "/motion-preview" in request.url.path or request.url.path.endswith("/recording-policy")
        ):
            return JSONResponse(
                {"detail": "motion_invalid_request"}, 422, headers={"Cache-Control": "no-store"}
            )
        return await previous_validation(request, error)

    async def current(session, camera_id, *, live=False):
        authenticate_current = authenticate_preview if live else authenticate
        account, token = await authenticate_current(session[1])
        if account.id != session[0].id or account.role is not UserRole.ADMINISTRATOR:
            raise HTTPException(403, "Insufficient recording administration permission")
        source = await read(lambda: selection(account, camera_id))
        final, _ = await authenticate_current(token)
        if final.id != account.id or final.role is not account.role:
            raise HTTPException(403, "Insufficient recording administration permission")
        return source

    async def invoke(awaitable):
        try:
            result, cancelled = await _joined(vault.task(awaitable))
            if cancelled:
                raise asyncio.CancelledError
            return result
        except (HTTPException, asyncio.CancelledError):
            raise
        except Exception as error:
            code = getattr(error, "code", None)
            if type(code) is not str or code not in _ERRORS:
                code = str(error) if str(error) in _ERRORS else "motion_policy_unavailable"
            raise _failure(code) from None

    async def policy_for(source):
        return _public(await invoke(service.get_motion_policy(source)), source)

    def match(lease, source, policy):
        if (
            lease.source != _source_key(source)
            or policy["source_revision"] != lease.reference["source_revision"]
        ):
            raise _failure("motion_source_changed")
        if policy["revision"] != lease.policy_revision:
            raise _failure("motion_policy_conflict")
        if policy["observed_reference"] != lease.reference:
            raise _failure("motion_source_changed")
        reference = getattr(service, "current_motion_reference", None)
        if not callable(reference) or _observed(reference(source)) != lease.reference:
            raise _failure("motion_source_changed")

    async def delivery_fence(session):
        return (
            None
            if capture_authority_fence is None
            else await capture_authority_fence(session[1], live=True)
        )

    @app.get(_PREFIX + "/{camera_id}/recording-policy", tags=["operator"])
    async def get_policy(camera_id: UUID, response: Response, session=dependency):
        response.headers["Cache-Control"] = "no-store"
        source = await current(session, camera_id)
        result = await policy_for(source)
        fresh = await current(session, camera_id)
        if _source_key(fresh) != _source_key(source):
            raise _failure("motion_source_changed")
        return result

    @app.post(_PREFIX + "/{camera_id}/motion-preview", tags=["operator"])
    async def create_preview(camera_id: UUID, response: Response, session=dependency):
        response.headers["Cache-Control"] = "no-store"
        async with mutations:
            source = await current(session, camera_id, live=True)
            initial = await policy_for(source)
            identifier, lease = vault.reserve(session, camera_id, source, initial["revision"])
            try:
                frame = await invoke(service.motion_preview(source))
                if type(frame) is not dict:
                    raise _failure("motion_preview_unavailable")
                jpeg = frame.get("jpeg")
                if (
                    type(jpeg) is not bytes
                    or not 4 <= len(jpeg) <= _JPEG_LIMIT
                    or not jpeg.startswith(b"\xff\xd8\xff")
                    or not jpeg.endswith(b"\xff\xd9")
                ):
                    raise _failure("motion_preview_unavailable")
                reference = _observed(
                    {
                        key: frame.get("reference_generation")
                        if key == "generation"
                        else frame.get(key)
                        for key in ("width", "height", "rotation", "source_revision", "generation")
                    }
                )
                lease.reference, lease.jpeg = reference, jpeg
                result = await policy_for(source)
                fresh = await current(session, camera_id, live=True)
                guard = await delivery_fence(session)
                vault.get(identifier, session, camera_id)
                match(lease, fresh, result)
                if guard is not None:
                    guard()
                lease.phase = "idle"
                return {
                    "preview_id": identifier,
                    "preview_url": f"{_PREFIX}/{camera_id}/motion-preview/{identifier}",
                    "source_revision": reference["source_revision"],
                    "width": reference["width"],
                    "height": reference["height"],
                    "rotation": 0,
                    "expires_in_seconds": max(0, int(lease.expires - monotonic())),
                }
            finally:
                if lease.phase != "idle":
                    vault.release(identifier, lease)

    @app.get(_PREFIX + "/{camera_id}/motion-preview/{preview_id}", tags=["operator"])
    async def preview_frame(camera_id: UUID, preview_id: str, session=dependency):
        if not _OPAQUE.fullmatch(preview_id):
            raise _failure("motion_preview_expired")
        source = await current(session, camera_id, live=True)
        lease = vault.get(preview_id, session, camera_id)
        result = await policy_for(source)
        fresh = await current(session, camera_id, live=True)
        guard = await delivery_fence(session)
        vault.get(preview_id, session, camera_id)
        match(lease, fresh, result)
        if guard is not None:
            guard()
        return Response(
            lease.jpeg,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.put(_PREFIX + "/{camera_id}/recording-policy", tags=["operator"])
    async def apply_policy(
        camera_id: UUID, payload: _Apply, response: Response, session=dependency
    ):
        response.headers["Cache-Control"] = "no-store"
        async with mutations:
            source = await current(session, camera_id)
            previous = await policy_for(source)
            if previous["revision"] != payload.expected_revision:
                raise _failure("motion_policy_conflict")
            policy = payload.policy()
            changed_zones = policy["zones"] != previous["zones"]
            lease = None
            if changed_zones:
                await current(session, camera_id, live=True)
                if payload.preview_id is None:
                    raise _failure("motion_preview_expired")
                lease = vault.get(payload.preview_id, session, camera_id)
                match(lease, source, previous)
                reference = policy["zones"]["reference"]
                if reference is not None and reference != {
                    k: v for k, v in lease.reference.items() if k != "generation"
                }:
                    raise _failure("motion_source_changed")
                lease.phase = "applying"
            cancelled = False
            request_task = asyncio.current_task()
            operation = _Mutation(vault.owner(session))
            vault._mutations.add(operation)

            async def before_commit():
                if cancelled or vault._closed:
                    raise asyncio.CancelledError
                fresh = await current(session, camera_id, live=changed_zones)
                if _source_key(fresh) != _source_key(source):
                    raise _failure("motion_source_changed")
                if lease is not None:
                    vault.get(payload.preview_id, session, camera_id)
                root_guard = (
                    None
                    if capture_authority_fence is None
                    else await capture_authority_fence(session[1], live=changed_zones)
                )

                def guard():
                    # This final fence runs in the owned file worker immediately
                    # before atomic replacement. It performs no durable reads.
                    if cancelled or vault._closed or request_task.cancelling():
                        raise asyncio.CancelledError
                    if operation.invalid:
                        raise RecordingPolicyAuthorizationChanged("motion_permission_changed")
                    if lease is not None and (
                        lease.invalid
                        or lease.expires <= monotonic()
                        or vault._leases.get(payload.preview_id) is not lease
                    ):
                        raise RecordingPolicyAuthorizationChanged("motion_permission_changed")
                    if root_guard is not None:
                        root_guard()

                return guard

            def on_cancel():
                nonlocal cancelled
                cancelled = True
                if lease is not None:
                    vault.discard(payload.preview_id, lease)

            try:
                try:
                    result, was_cancelled = await _joined(
                        vault.task(
                            service.apply_motion_policy(
                                source,
                                payload.expected_revision,
                                policy,
                                before_commit=before_commit,
                                reference_generation=None
                                if lease is None
                                else lease.reference["generation"],
                            )
                        ),
                        on_cancel=on_cancel,
                    )
                except (HTTPException, asyncio.CancelledError):
                    raise
                except Exception as error:
                    code = getattr(error, "code", None)
                    if type(code) is not str or code not in _ERRORS:
                        code = str(error) if str(error) in _ERRORS else "motion_policy_unavailable"
                    raise _failure(code) from None
                if was_cancelled:
                    raise asyncio.CancelledError
                vault.clear_camera(camera_id)
                public = _public(result, source)
                fresh = await current(session, camera_id, live=changed_zones)
                if _source_key(fresh) != _source_key(source):
                    raise _failure("motion_source_changed")
                return public
            finally:
                vault._mutations.discard(operation)
                if lease is not None:
                    vault.release(payload.preview_id, lease)

    previous_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with previous_lifespan(application):

            async def expire():
                while True:
                    await asyncio.sleep(5)
                    vault.prune()

            if not vault._closed:
                vault._expiry = asyncio.create_task(expire(), name="operator-motion-preview-expiry")
            try:
                yield
            finally:
                await vault.aclose()

    app.router.lifespan_context = lifespan
    return vault
