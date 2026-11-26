"""Bounded passive observations for an already-failed initial camera wall.

No original request, assertion or deadline changes. Credentials, request/response
bodies, raw URLs, arbitrary interface text and pixels never enter the note.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
from functools import wraps
from urllib.parse import urlsplit

_ASSET = re.compile(r"/operator/(?:app|wall|timeline|admin|analytics|settings)\.(?:js|css)")
_CAMERA = re.compile(r"/api/v1/operator/cameras/[0-9a-f-]{36}/(media-session|history)")
_STATIC = frozenset(
    {
        "/operator/",
        "/api/v1/auth/login",
        "/api/v1/auth/me",
        "/api/v1/auth/oidc",
        "/api/v1/operator/status",
        "/api/v1/operator/cameras",
        "/api/v1/operator/media-session",
    }
)
_STAGES = frozenset(
    {
        "goto_start",
        "goto_done",
        "sign_in_click_start",
        "sign_in_click_done",
        "wall_assert_start",
        "wall_assert_done",
    }
)
_KNOWN_MESSAGES = (
    "",
    "Loading cameras…",
    "Camera list unavailable. Select refresh to try again.",
    "The camera service is unavailable. Try again shortly.",
    "Sign-in is unavailable. Confirm K5 is running on this PC and check its startup message.",
    "Your session has ended. Sign in again.",
    "Username or password is incorrect.",
)
_SNAPSHOT = r"""(() => {
  const finite = n => typeof n === 'number' && Number.isFinite(n)
    && n >= 0 && n <= 1e12 ? n : null;
  const ids = ['sign-in','workspace','wall-panel','live-panel','sign-in-button'];
  const nodes = Object.fromEntries(ids.map(id => {
    const n = document.getElementById(id);
    return [id, n ? {hidden:n.hidden, displayed:getComputedStyle(n).display!=='none',
      disabled:n.disabled===true} : null];
  }));
  const messageIds = ['sign-in-message','camera-list-message','worker-message'];
  const known = __KNOWN_MESSAGES__;
  const messages = Object.fromEntries(messageIds.map(id =>
    [id, known.indexOf(document.getElementById(id)?.textContent||'')]));
  const globals = Object.fromEntries(['K5CameraWall','K5RecordingTimeline',
    'K5Admin','K5Analytics','K5Settings'].map(k =>
    [k,typeof window[k]?.create==='function']));
  const counts = {
    wallVideos:document.querySelectorAll('#wall-panel video').length,
    wallTiles:document.querySelectorAll('#wall-panel .camera-wall-tile').length,
    cameras:document.querySelectorAll('.camera-item').length,
  };
  const validity = Object.fromEntries(['username','password'].map(id =>
    [id,document.getElementById(id)?.validity.valid===true]));
  const asset = /^\/operator\/(?:app|wall|timeline|admin|analytics|settings)\.(?:js|css)$/;
  const scripts = performance.getEntriesByType('resource')
    .filter(e => asset.test(new URL(e.name).pathname)).slice(0,16).map(e => ({
      path:new URL(e.name).pathname, start:finite(e.startTime),
      duration:finite(e.duration), responseEnd:finite(e.responseEnd),
    }));
  return {nodes,messages,globals,counts,validity,scripts,
    readyState:['loading','interactive','complete'].includes(document.readyState)
      ? document.readyState : 'other'};
})()""".replace("__KNOWN_MESSAGES__", json.dumps(_KNOWN_MESSAGES))


def safe_route(url):
    path = urlsplit(url).path
    if path in _STATIC or _ASSET.fullmatch(path):
        return path
    camera = _CAMERA.fullmatch(path)
    return "/api/v1/operator/cameras/{camera}/" + camera[1] if camera else "unlisted"


def safe_dom(value):
    """Whitelist the expected browser projection before adding an exception note."""
    if type(value) is not dict:
        return {"capture_failed": True}

    def fields(name):
        result = value.get(name)
        return result if type(result) is dict else {}

    def boolean(item):
        return item if type(item) is bool else None

    def number(item):
        return (
            item
            if type(item) in {int, float} and 0 <= item <= 1e12 and math.isfinite(item)
            else None
        )

    nodes = fields("nodes")
    projected = {}
    for name in ("sign-in", "workspace", "wall-panel", "live-panel", "sign-in-button"):
        node = nodes.get(name)
        projected[name] = (
            {key: boolean(node.get(key)) for key in ("hidden", "displayed", "disabled")}
            if type(node) is dict
            else None
        )
    messages = fields("messages")
    counts = fields("counts")
    scripts = value.get("scripts")
    resources = []
    for resource in scripts[:16] if type(scripts) is list else []:
        if type(resource) is not dict:
            continue
        path = resource.get("path")
        if type(path) is not str or not _ASSET.fullmatch(path):
            continue
        resources.append(
            {
                "path": path,
                **{key: number(resource.get(key)) for key in ("start", "duration", "responseEnd")},
            }
        )
    ready = value.get("readyState")
    return {
        "nodes": projected,
        "messages": {
            key: messages.get(key)
            if type(messages.get(key)) is int and -1 <= messages[key] < len(_KNOWN_MESSAGES)
            else None
            for key in ("sign-in-message", "camera-list-message", "worker-message")
        },
        "globals": {
            key: boolean(fields("globals").get(key))
            for key in (
                "K5CameraWall",
                "K5RecordingTimeline",
                "K5Admin",
                "K5Analytics",
                "K5Settings",
            )
        },
        "counts": {
            key: counts.get(key)
            if type(counts.get(key)) is int and 0 <= counts[key] <= 10000
            else None
            for key in ("wallVideos", "wallTiles", "cameras")
        },
        "validity": {key: boolean(fields("validity").get(key)) for key in ("username", "password")},
        "scripts": resources,
        "readyState": ready
        if type(ready) is str and ready in {"loading", "interactive", "complete"}
        else "other",
    }


def observation_only(function):
    @wraps(function)
    def guarded(self, *args, **kwargs):
        try:
            return function(self, *args, **kwargs)
        except BaseException:  # noqa: BLE001 - preserve the original route/test result.
            self.observer_failed = True
            return None

    return guarded


def _bounded_session(page, seconds):
    from playwright.sync_api._generated import mapping

    context = page.context
    result = context._sync(asyncio.wait_for(context._impl_obj.new_cdp_session(page=page), seconds))
    return mapping.from_impl(result)


def _bounded_send(session, params, seconds):
    return session._sync(
        asyncio.wait_for(session._impl_obj.send("Runtime.evaluate", params), seconds)
    )


def _bounded_detach(session, seconds):
    return session._sync(asyncio.wait_for(session._impl_obj.detach(), seconds))


class WallBootObserver:
    @observation_only
    def __init__(self, page):
        self.observer_failed = False
        self.page = page
        self.started = time.monotonic()
        self.events, self.markers, self.script_errors, self.handlers = [], [], [], []
        for event, callback in (
            ("request", lambda request: self.event("request", request)),
            (
                "response",
                lambda response: self.event("response", response.request, response.status),
            ),
            ("requestfinished", lambda request: self.event("finished", request)),
            ("requestfailed", lambda request: self.event("failed", request)),
            ("pageerror", self.script_error),
        ):
            guarded = self._callback(callback)
            self.handlers.append((event, guarded))
            page.on(event, guarded)

    def _callback(self, function):
        def guarded(*args):
            try:
                return function(*args)
            except BaseException:  # noqa: BLE001 - event access cannot alter the original result.
                self.observer_failed = True
                return None

        return guarded

    @observation_only
    def observe(self, function, *args):
        return function(*args)

    @observation_only
    def mark(self, stage):
        if stage in _STAGES and len(self.markers) < 16:
            self.markers.append(
                {"stage": stage, "milliseconds": round((time.monotonic() - self.started) * 1000, 3)}
            )

    def intercept(self, original):
        def observed(route):
            self.observe(self.route_event, "route_enter", route)
            try:
                return original(route)
            finally:
                self.observe(self.route_event, "route_done", route)

        return observed

    @observation_only
    def route_event(self, kind, route):
        self.event(kind, route.request)

    @observation_only
    def event(self, kind, request, status=None):
        if len(self.events) >= 128:
            return
        method = request.method
        self.events.append(
            {
                "event": kind
                if kind
                in {"request", "response", "finished", "failed", "route_enter", "route_done"}
                else "other",
                "milliseconds": round((time.monotonic() - self.started) * 1000, 3),
                "route": safe_route(request.url),
                "method": method if method in {"GET", "POST", "DELETE"} else "other",
                "status": status if type(status) is int and 100 <= status <= 599 else None,
            }
        )

    @observation_only
    def script_error(self, error):
        if len(self.script_errors) >= 8:
            return
        name = getattr(error, "name", "")
        message = str(getattr(error, "message", ""))[:4096]
        stack = str(getattr(error, "stack", ""))[:16384]
        locations = re.findall(
            r"(/operator/(?:app|wall|timeline|admin|analytics|settings)\.js):(\d{1,6}):(\d{1,6})",
            stack,
        )[:8]
        self.script_errors.append(
            {
                "name": name
                if name in {"Error", "SyntaxError", "TypeError", "ReferenceError", "RangeError"}
                else "other",
                "message_sha256": hashlib.sha256(message.encode()).hexdigest(),
                "locations": [
                    {"path": path, "line": int(line), "column": int(column)}
                    for path, line, column in locations
                ],
            }
        )

    @observation_only
    def annotate(self, error):
        # No debugger session or evaluation occurs unless the original assertion failed.
        deadline = time.monotonic() + 2.5
        session, dom = None, {"capture_failed": True}
        try:
            session = _bounded_session(self.page, min(0.5, deadline - time.monotonic()))
            result = _bounded_send(
                session,
                {"expression": _SNAPSHOT, "returnByValue": True, "timeout": 1000},
                min(1.1, deadline - time.monotonic()),
            )
            dom = safe_dom(result.get("result", {}).get("value"))
        except BaseException:  # noqa: BLE001 - a diagnostic failure cannot replace the assertion.
            self.observer_failed = True
        finally:
            if session is not None:
                try:
                    _bounded_detach(session, max(0.001, min(0.5, deadline - time.monotonic())))
                except BaseException:  # noqa: BLE001 - preserve the original test cleanup.
                    self.observer_failed = True
        payload = {
            "scope": "failed-wall-boot-diagnostic-only",
            "qualification": False,
            "assertion_deadline_changed": False,
            "request_retries_added": 0,
            "observer_failed": self.observer_failed,
            "events": self.events,
            "markers": self.markers,
            "script_errors": self.script_errors,
            "dom": dom,
        }
        note = json.dumps(payload, sort_keys=True, allow_nan=False)
        if len(note.encode()) > 32768:
            payload["events"] = self.events[-32:]
            payload["event_note_truncated"] = True
            note = json.dumps(payload, sort_keys=True, allow_nan=False)
        error.add_note("K5 initial wall observations: " + note)

    @observation_only
    def close(self):
        for event, callback in self.handlers:
            self.page.remove_listener(event, callback)
