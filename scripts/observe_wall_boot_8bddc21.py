"""Observe one original UI failure without changing its assertions or deadline.

Diagnostic only: this never edits product or test files, qualifies an installer,
retries a request, or emits credentials, response bodies, screenshots or traces.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import time
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit

SOURCE_HEAD = "8bddc213ef7372a2fbc153e2895cebfe5b0f63b0"
SOURCE_TREE = "cdbe56a130246dbc653f22edba23b5a242caed4f"
TEST_FILE = "tests/unit/test_operator_web_ui.py"
TEST_SHA256 = "35d5f79bf4aa28c536ca1cd792d4cd9441ed4a3355fae845dd2c2dd80ac91548"
TARGET = (
    "test_focused_camera_switch_observes_its_own_grant_without_wall_response_ambiguity"
)
FILES = (
    "tests/unit/test_operator_onvif.py",
    "tests/unit/test_operator_camera_settings.py",
    "tests/unit/test_operator_onvif_discovery.py",
    "tests/unit/test_operator_rtsp.py",
    TEST_FILE,
    "tests/unit/test_operator_camera_video_ui.py",
    "tests/unit/test_operator_admin_ui.py",
    "tests/unit/test_operator_camera_wall.py",
    "tests/unit/test_operator_recording_timeline_ui.py",
    "tests/unit/test_operator_analytics_ui.py",
    "tests/unit/test_operator_continuous_playback_ui.py",
    "tests/unit/test_operator_settings_ui.py",
)
OPTIONS = (
    "-o",
    "addopts=",
    "--no-cov",
    "-o",
    "faulthandler_timeout=90",
    "--durations=5",
)
OUTPUT = Path("wall-boot-8bddc21-diagnostic.json")
_ASSET = re.compile(
    r"/operator/(?:app|wall|timeline|admin|analytics|settings)\.(?:js|css)$"
)
_CAMERA = re.compile(
    r"/api/v1/operator/cameras/[0-9a-f-]{36}/(?:media-session|history)$"
)
_STATIC = {
    "/operator/",
    "/api/v1/auth/login",
    "/api/v1/auth/me",
    "/api/v1/auth/oidc",
    "/api/v1/operator/status",
    "/api/v1/operator/cameras",
    "/api/v1/operator/media-session",
}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def safe_path(url):
    path = urlsplit(url).path
    return (
        path
        if path in _STATIC or _ASSET.fullmatch(path) or _CAMERA.fullmatch(path)
        else "unlisted"
    )


def verify_source(root):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=root, text=True).strip()

    if (
        git("rev-parse", "HEAD") != SOURCE_HEAD
        or git("rev-parse", "HEAD^{tree}") != SOURCE_TREE
    ):
        raise RuntimeError("diagnostic_source_identity_refused")
    if git("status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("diagnostic_source_edits_refused")
    if digest((root / TEST_FILE).read_bytes()) != TEST_SHA256:
        raise RuntimeError("diagnostic_test_bytes_refused")


def observation_only(function):
    """An observer failure cannot replace a product or Playwright outcome."""

    @wraps(function)
    def guarded(self, *args, **kwargs):
        try:
            return function(self, *args, **kwargs)
        except BaseException:  # noqa: BLE001 - observations must not alter the test.
            self.observer_failed = True
            return None

    return guarded


class WallBootObserver:
    @observation_only
    def __init__(self, page):
        self.observer_failed = False
        self.page = page
        self.started = time.monotonic()
        self.events = []
        self.markers = []
        self.script_errors = []
        page.on("request", lambda request: self.observe(self.event, "request", request))
        page.on(
            "response",
            lambda response: self.observe(
                self.event, "response", response.request, response.status
            ),
        )
        page.on(
            "requestfinished",
            lambda request: self.observe(self.event, "finished", request),
        )
        page.on(
            "requestfailed", lambda request: self.observe(self.event, "failed", request)
        )
        page.on("pageerror", lambda error: self.observe(self.script_error, error))

    @observation_only
    def observe(self, function, *args):
        return function(*args)

    @observation_only
    def mark(self, stage):
        if len(self.markers) < 16:
            self.markers.append(
                {
                    "stage": stage,
                    "milliseconds": round((time.monotonic() - self.started) * 1000, 3),
                }
            )

    def intercept(self, original):
        def observed(route):
            self.observe(self.event, "route_enter", route.request)
            try:
                return original(route)
            finally:
                self.observe(self.event, "route_done", route.request)

        return observed

    @observation_only
    def event(self, kind, request, status=None):
        if len(self.events) >= 128:
            return
        method = request.method
        self.events.append(
            {
                "event": kind,
                "milliseconds": round((time.monotonic() - self.started) * 1000, 3),
                "path": safe_path(request.url),
                "method": method if method in {"GET", "POST", "DELETE"} else "other",
                "status": status,
            }
        )

    @observation_only
    def script_error(self, error):
        if len(self.script_errors) >= 8:
            return
        name = getattr(error, "name", "")
        message = str(getattr(error, "message", ""))
        stack = str(getattr(error, "stack", ""))
        locations = re.findall(
            r"(/operator/(?:app|wall|timeline|admin|analytics|settings)\.js):(\d{1,6}):(\d{1,6})",
            stack,
        )[:8]
        self.script_errors.append(
            {
                "name": name
                if name
                in {"Error", "SyntaxError", "TypeError", "ReferenceError", "RangeError"}
                else "other",
                "message_sha256": digest(message.encode()),
                "locations": [
                    {"path": p, "line": int(n), "column": int(c)}
                    for p, n, c in locations
                ],
            }
        )

    def capture(self, error):
        # Run only after the unchanged assertion has already failed. Failure to
        # collect observations must never replace the original test exception.
        try:
            expression = """() => {
                  const ids = ['sign-in','workspace','wall-panel','live-panel','sign-in-button'];
                  const nodes = Object.fromEntries(ids.map(id => {const n=document.getElementById(id);return [id,n ? {hidden:n.hidden,displayed:getComputedStyle(n).display!=='none',disabled:n.disabled===true}:null];}));
                  const messageIds=['sign-in-message','camera-list-message','worker-message'];
                  const known=['','Loading cameras…','Camera list unavailable. Select refresh to try again.','The camera service is unavailable. Try again shortly.','Sign-in is unavailable. Confirm K5 is running on this PC and check its startup message.','Your session has ended. Sign in again.','Username or password is incorrect.'];
                  const messages=Object.fromEntries(messageIds.map(id=>[id,known.indexOf(document.getElementById(id)?.textContent||'')]));
                  const globals=Object.fromEntries(['K5CameraWall','K5RecordingTimeline','K5Admin','K5Analytics','K5Settings'].map(k=>[k,typeof window[k]?.create==='function']));
                  const counts={wallVideos:document.querySelectorAll('#wall-panel video').length,wallTiles:document.querySelectorAll('#wall-panel .camera-wall-tile').length,cameras:document.querySelectorAll('.camera-item').length};
                  const validity=Object.fromEntries(['username','password'].map(id=>[id,document.getElementById(id)?.validity.valid===true]));
                  const scripts=performance.getEntriesByType('resource').filter(e=>/^\\/operator\\/(?:app|wall|timeline|admin|analytics|settings)\\.(?:js|css)$/.test(new URL(e.name).pathname)).slice(0,16).map(e=>({path:new URL(e.name).pathname,start:e.startTime,duration:e.duration,responseEnd:e.responseEnd}));
                  return {nodes,messages,globals,counts,validity,scripts,readyState:document.readyState};
                }"""
            session = self.page.context.new_cdp_session(self.page)
            evaluated = session.send(
                "Runtime.evaluate",
                {
                    "expression": f"({expression})()",
                    "returnByValue": True,
                    "timeout": 1000,
                },
            )
            dom = evaluated.get("result", {}).get("value", {"capture_failed": True})
        except BaseException:  # noqa: BLE001 - preserve the original assertion failure.
            dom = {"capture_failed": True}
        error_name = type(error).__name__
        result = {
            "scope": "failed-wall-boot-diagnostic-only",
            "source_head": SOURCE_HEAD,
            "source_tree": SOURCE_TREE,
            "test_sha256": TEST_SHA256,
            "target": TARGET,
            "qualification": False,
            "assertion_deadline_changed": False,
            "request_retries_added": 0,
            "observer_failed": self.observer_failed,
            "error_type": error_name
            if error_name in {"AssertionError", "TimeoutError", "Error"}
            else "other",
            "events": self.events,
            "markers": self.markers,
            "script_errors": self.script_errors,
            "dom": dom,
        }
        try:
            OUTPUT.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        except BaseException:  # noqa: BLE001 - diagnostics cannot replace the failure.
            return  # An unwritable diagnostic destination leaves the test failure intact.


def instrument(original):
    """Wrap only the original function's try body; retain its AST unchanged."""
    function = next(
        node
        for node in ast.parse(original).body
        if isinstance(node, ast.FunctionDef) and node.name == TARGET
    )
    original_try = [node for node in function.body if isinstance(node, ast.Try)]
    if len(original_try) != 1:
        raise RuntimeError("diagnostic_original_shape_refused")
    outer = original_try[0]
    function.body.insert(
        function.body.index(outer), ast.parse("_wall_observer = None").body[0]
    )
    assignments = [
        index
        for index, node in enumerate(outer.body)
        if isinstance(node, ast.Assign)
        and ast.unparse(node) == "page = context.new_page()"
    ]
    if len(assignments) != 1:
        raise RuntimeError("diagnostic_page_shape_refused")
    outer.body.insert(
        assignments[0] + 1,
        ast.parse("_wall_observer = WallBootObserver(page)").body[0],
    )
    observed_body = []
    stages = {
        'page.goto("http://127.0.0.1:8000/operator/")': "goto",
        "page.goto('http://127.0.0.1:8000/operator/')": "goto",
        'page.locator("#sign-in-button").click()': "sign_in_click",
        "page.locator('#sign-in-button').click()": "sign_in_click",
        'expect(page.locator("#wall-panel video")).to_have_count(4)': "wall_assert",
        "expect(page.locator('#wall-panel video')).to_have_count(4)": "wall_assert",
    }
    for node in outer.body:
        text = ast.unparse(node)
        if text == "page.route('**/*', intercept)":
            node.value.args[1] = ast.parse(
                "_wall_observer.intercept(intercept)", mode="eval"
            ).body
        stage = stages.get(text)
        if stage:
            observed_body += ast.parse(f"_wall_observer.mark('{stage}_start')").body
        observed_body.append(node)
        if stage:
            observed_body += ast.parse(f"_wall_observer.mark('{stage}_done')").body
    handler = ast.parse(
        "try:\n    pass\nexcept BaseException as _wall_error:\n"
        "    if _wall_observer is not None:\n"
        "        try:\n"
        "            _wall_observer.capture(_wall_error)\n"
        "        except BaseException:\n"
        "            pass\n"
        "    raise\n"
    ).body[0]
    handler.body = observed_body
    outer.body = [handler]
    return ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))


class ObserverPlugin:
    def __init__(self, root):
        self.root = root
        self.instrumented = False

    def pytest_collection_modifyitems(self, items):
        targets = [item for item in items if item.nodeid == TEST_FILE + "::" + TARGET]
        if len(targets) != 1:
            raise RuntimeError("diagnostic_target_collection_refused")
        item = targets[0]
        module = item.module
        module.WallBootObserver = WallBootObserver
        compiled = instrument((self.root / TEST_FILE).read_text(encoding="utf-8"))
        # Only the admitted immutable test AST is compiled; no input text is evaluated.
        exec(compile(compiled, str(self.root / TEST_FILE), "exec"), module.__dict__)  # noqa: S102
        item.obj = module.__dict__[TARGET]
        self.instrumented = True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--isolated-target", action="store_true")
    args = parser.parse_args()
    source = args.source.resolve()
    verify_source(source)
    if args.preflight_only:
        print("Frozen source and unchanged UI test admitted for diagnostic only.")
        return 0
    os.chdir(source)
    import pytest

    plugin = ObserverPlugin(source)
    selected = [TEST_FILE + "::" + TARGET] if args.isolated_target else list(FILES)
    result = pytest.main(
        [*selected, *OPTIONS, "--junitxml=wall-boot-8bddc21-diagnostic.xml"],
        plugins=[plugin],
    )
    if not plugin.instrumented:
        raise RuntimeError("diagnostic_observer_not_installed")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
