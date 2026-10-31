"""Audit finite operational routes without reading request bodies or headers."""

from __future__ import annotations

import asyncio
import re
from uuid import UUID, uuid4

from starlette.responses import JSONResponse

from k5vision.operator_audit import OperatorAuditError, _request_context

_PREFIX = "/api/v1/operator"
_ID = r"(?P<target>[0-9a-fA-F-]{36})"
_RULES = (
    ("POST", r"/api/v1/auth/login", "auth.login", "session"),
    ("POST", r"/api/v1/auth/logout", "auth.logout", "session"),
    ("POST", r"/api/v1/auth/bootstrap-password", "auth.activate", "user"),
    ("POST", r"/api/v1/auth/mfa", "auth.mfa", "session"),
    ("POST", r"/api/v1/auth/oidc/(?:start|complete)", "auth.sso", "session"),
    ("GET", r"/api/v1/auth/oidc/callback", "auth.sso", "session"),
    (
        "POST",
        _PREFIX + r"/security/mfa/(?:enroll|confirm|disable|recovery-codes)",
        "security.mfa.configure",
        "user",
    ),
    ("PUT", _PREFIX + r"/admin/sso/provider", "security.sso.configure", "security"),
    ("POST", _PREFIX + r"/admin/sso/provider/test", "security.sso.configure", "security"),
    ("POST", _PREFIX + r"/admin/sso/links", "security.sso.configure", "security"),
    ("DELETE", _PREFIX + "/admin/sso/links/" + _ID, "security.sso.configure", "user"),
    ("POST", _PREFIX + r"/camera-discovery", "camera.discover", "camera"),
    ("POST", _PREFIX + r"/camera-profiles", "camera.probe", "camera"),
    ("POST", _PREFIX + r"/camera-stream-probe", "camera.probe", "camera"),
    ("POST", _PREFIX + r"/cameras(?:/connect|/stream-connect)?", "camera.enroll", "camera"),
    ("POST", _PREFIX + "/cameras/" + _ID + r"/reconnect", "camera.reconnect", "camera"),
    ("DELETE", _PREFIX + "/cameras/" + _ID, "camera.remove", "camera"),
    ("PUT", _PREFIX + "/cameras/" + _ID + r"/recording", "recording.configure", "camera"),
    ("PATCH", _PREFIX + "/cameras/" + _ID + r"/analytics", "analytics.configure", "camera"),
    ("POST", _PREFIX + "/cameras/" + _ID + r"/exports", "export.prepare", "camera"),
    (
        "GET",
        _PREFIX + "/cameras/" + _ID + r"/exports/[0-9a-fA-F-]{36}/file",
        "export.download",
        "camera",
    ),
    ("GET", r"/media/exports/" + _ID + r"/[0-9a-fA-F-]{36}\.mp4", "export.download", "camera"),
    ("POST", _PREFIX + r"/admin/users", "user.create", "user"),
    ("PATCH", _PREFIX + "/admin/users/" + _ID, "user.configure", "user"),
    ("PUT", _PREFIX + "/admin/users/" + _ID + r"/camera-access", "user.camera_access", "user"),
    ("POST", _PREFIX + "/admin/users/" + _ID + r"/reset-password", "user.reset_password", "user"),
    ("GET", _PREFIX + r"/admin/activity/export", "audit.export", "site"),
)
_RULES = tuple(
    (method, re.compile(pattern + "$"), action, kind) for method, pattern, action, kind in _RULES
)


def _classify(method, path):
    for expected, pattern, action, kind in _RULES:
        if method == expected and (match := pattern.fullmatch(path)):
            target = match.groupdict().get("target")
            try:
                return action, kind, None if target is None else UUID(target)
            except ValueError:
                return None
    if method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    for prefix, action, kind in (
        ("/admin/storage", "storage.configure", "storage"),
        ("/admin/time", "time.configure", "time"),
        ("/admin/integrations", "integration.configure", "integration"),
        ("/admin/security", "security.configure", "security"),
        ("/admin/roles", "role.configure", "role"),
    ):
        if path == _PREFIX + prefix or path.startswith(_PREFIX + prefix + "/"):
            if kind == "time" and path.endswith(("/resync", "/synchronize")):
                action = "time.resync"
            if kind == "integration" and path.endswith("/test"):
                action = "integration.test"
            if kind == "role" and method == "DELETE":
                action = "role.remove"
            return action, kind, None
    camera = re.fullmatch(_PREFIX + "/cameras/" + _ID + r"/time(?:/[^/]{1,32})?", path)
    if camera:
        try:
            return (
                "time.camera.sync"
                if path.endswith(("/sync", "/synchronize"))
                else "time.camera.configure",
                "camera",
                UUID(camera["target"]),
            )
        except ValueError:
            pass
    if path.startswith("/api/v1/auth/mfa/"):
        return "auth.mfa", "session", None
    if path.startswith("/api/v1/auth/sso/"):
        return "auth.sso", "session", None
    return None


class OperatorAuditMiddleware:
    """Persist attempts before side effects and final outcomes after transfer.

    A started row surviving without a completion exposes an interrupted action.
    An unavailable audit prevents subsequent audited operations from executing;
    ordinary viewing and recording are independent of this administrative log.
    """

    def __init__(self, app, *, store):
        self.app, self.store = app, store

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        classification = _classify(scope.get("method"), path) if scope["type"] == "http" else None
        attempt = classification is not None
        if (
            classification is None
            and scope["type"] == "http"
            and scope.get("method") == "GET"
            and (path.startswith(_PREFIX + "/") or path == "/api/v1/auth/me")
        ):
            camera = re.match(_PREFIX + "/cameras/" + _ID + r"(?:/|$)", path)
            target = None
            if camera is not None:
                try:
                    target = UUID(camera["target"])
                except ValueError:
                    pass
            classification = "security.access", "camera" if target else "site", target
        if classification is None:
            await self.app(scope, receive, send)
            return
        action, kind, target = classification
        context = {"request_id": str(uuid4()), "actor_id": None}
        token = _request_context.set(context)
        status, interrupted = None, False
        try:
            if attempt:
                try:
                    await asyncio.to_thread(
                        self.store.record,
                        action,
                        outcome="started",
                        target_kind=kind,
                        target_id=target,
                        request_id=context["request_id"],
                    )
                except (OperatorAuditError, ValueError):
                    await JSONResponse(
                        {"detail": "audit_unavailable"},
                        status_code=503,
                        headers={"Cache-Control": "no-store"},
                    )(scope, receive, send)
                    return

            async def observed_send(message):
                nonlocal status
                if message["type"] == "http.response.start":
                    status = message["status"]
                await send(message)

            try:
                await self.app(scope, receive, observed_send)
            except BaseException:
                interrupted = True
                raise
            finally:
                outcome = (
                    "interrupted"
                    if interrupted
                    else "denied"
                    if status in {401, 403}
                    else "started"
                    if status == 202 or status is not None and 300 <= status < 400
                    else "success"
                    if status is not None and status < 400
                    else "failure"
                )
                if attempt or status is not None and status >= 400:
                    try:
                        await asyncio.shield(
                            asyncio.to_thread(
                                self.store.record,
                                action,
                                outcome=outcome,
                                actor=context["actor_id"],
                                target_kind=kind,
                                target_id=target,
                                request_id=context["request_id"],
                                status_code=status,
                            )
                        )
                    except (OperatorAuditError, ValueError):
                        # The durable attempt remains visible; store latches failed
                        # and rejects the next mutation before its side effects.
                        pass
        finally:
            _request_context.reset(token)
