"""Actual installed motion recording, backend zones and monotonic clock holds.

Generated live pixels drive the installed independent detector. API state alone
cannot qualify recording: real file growth, real quiet gaps, decoded playback
and a saved MP4 are observed. Camera events and motion decisions are not mocked.
"""

from __future__ import annotations

import hashlib
import http.client
import importlib.util
import json
import os
import platform
import secrets
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from uuid import UUID, uuid4

import psutil
import pytest

from k5vision import identity_state
from k5vision.media.mediamtx_provider import MediaMTXBinding

_INPUTS = (
    "K5_DESKTOP_SETUP",
    "K5_DESKTOP_SETUP_SHA256",
    "K5_DESKTOP_HEAD_SHA",
    "K5_MEDIAMTX_DONOR_EXE",
    "K5_CADDY_DONOR_EXE",
    "K5_DONOR_FFMPEG",
    "K5_DONOR_FFPROBE",
)
pytestmark = pytest.mark.skipif(
    os.name != "nt"
    or os.getenv("K5_NATIVE_MOTION_ACCEPTANCE") != "1"
    or os.getenv("K5_VM_CLOCK_ACCEPTANCE") != "1"
    or not all(os.getenv(name) for name in _INPUTS),
    reason="Motion qualification requires the opted-in disposable Windows VM and exact Setup",
)


_ASSERTION_CODES = {
    "Graphical recording policy was not applied": "policy_put_http_rejected",
    "Recording controls did not retain the selected actual capture policy": "policy_value_mismatch",
    "Default motion zone was not the full frame": "initial_zones_not_whole_frame",
    "Actual recording bytes disagreed with the motion gate": "target_growth_mismatch",
    "Motion policy interrupted the other recorder": "other_camera_growth_mismatch",
    "Motion recorder changed state during observation": "observation_state_changed",
    "Installed motion recorder exited": "installed_recorder_exited",
}


def _require(value, message):
    __tracebackhide__ = True
    if not value:
        pytest.fail(message, pytrace=False)


def _helpers(name):
    spec = importlib.util.spec_from_file_location(
        "k5_motion_acceptance_" + name, Path(__file__).with_name(name + ".py")
    )
    assert spec is not None and spec.loader is not None
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def _policy(client, camera):
    return client.json(f"/api/v1/operator/cameras/{camera}/recording-policy")


def _policy_state(client, camera, wanted, desktop, *, app=None, timeout=20):
    observed = {}

    def ready():
        observed.clear()
        observed.update(_policy(client, camera))
        return observed.get("status") in set(wanted)

    desktop._wait(ready, "Actual motion recorder state", timeout=timeout, process=app)
    return observed


def _observe_recording(
    client,
    camera,
    *,
    paths,
    profile,
    desktop,
    publisher,
    movement,
    state,
    growing,
    app=None,
    seconds=4,
):
    publisher.movement(movement)
    _policy_state(client, camera, state, desktop, app=app)
    if not growing:
        # A completed record-only PATCH closes its fragment. Require actual
        # file quiescence before starting the measured idle interval.
        previous, stable_since = None, time.monotonic()

        def closed():
            nonlocal previous, stable_since
            current = desktop._recording_bytes(profile, paths[0])
            if previous != current:
                previous, stable_since = current, time.monotonic()
            return time.monotonic() - stable_since >= 1.2

        desktop._wait(closed, "Closed motion recording fragment", timeout=10, process=app)
    before = [desktop._recording_bytes(profile, path) for path in paths]
    began, observed_states = time.monotonic(), set()
    while time.monotonic() - began < seconds:
        publisher.check()
        observed_states.add(_policy(client, camera)["status"])
        if app is not None:
            _require(app.poll() is None, "Installed motion recorder exited")
        time.sleep(0.2)
    elapsed = time.monotonic() - began
    after = [desktop._recording_bytes(profile, path) for path in paths]
    _require(
        after[0] > before[0] if growing else after[0] == before[0],
        "Actual recording bytes disagreed with the motion gate",
    )
    _require(after[1] > before[1], "Motion policy interrupted the other recorder")
    _require(observed_states <= set(state), "Motion recorder changed state during observation")
    return {
        "state": sorted(observed_states),
        "seconds": round(elapsed, 3),
        "target_before": before[0],
        "target_after": after[0],
        "other_before": before[1],
        "other_after": after[1],
        "publisher_restarts": publisher.restarts,
    }


def _login(page, client, username, password):
    page.goto(client.origin + "/operator/")
    page.locator("#username").fill(username)
    page.locator("#password").fill(password)
    with page.expect_response(
        lambda r: r.request.method == "POST" and r.url.endswith("/api/v1/auth/login")
    ) as signed_in:
        page.locator("#sign-in-button").click()
    _require(signed_in.value.status == 200, "Installed motion-controls login failed")
    client.token = signed_in.value.json()["session_token"]
    page.locator("#workspace").wait_for(state="visible")


class _GraphicalSaveDiagnostic:
    """Passive, bounded test-only evidence; never changes submission or validation."""

    def __init__(self, page, camera, diagnostic, baseline, *, name="zones"):
        self.page, self.diagnostic, self.name = page, diagnostic, name
        self.route = f"/api/v1/operator/cameras/{camera}/recording-policy"
        self.network = {
            "put_requests": 0,
            "put_responses": 0,
            "put_failed": 0,
            "put_http_statuses": [],
            "console_errors": 0,
            "page_errors": 0,
        }
        self.baseline = baseline
        self.preview_started = None
        self.preview_ttl = None
        self.preview_matches_source = None
        self.handlers = {
            "request": self._request,
            "response": self._response,
            "requestfailed": self._failed,
            "console": self._console,
            "pageerror": self._pageerror,
        }
        for name, callback in self.handlers.items():
            page.on(name, callback)
        page.evaluate("""() => {
          const counters={click:0, trustedClick:0, submit:0, trustedSubmit:0, invalid:0};
          const listener=e=>{
            if(e.type==='click' && e.target.closest?.('#recording-settings-save')) {
              counters.click=Math.min(16,counters.click+1);
              if(e.isTrusted)counters.trustedClick=Math.min(16,counters.trustedClick+1);
            }
            if(e.type==='submit' && e.target.id==='recording-settings-form') {
              counters.submit=Math.min(16,counters.submit+1);
              if(e.isTrusted)counters.trustedSubmit=Math.min(16,counters.trustedSubmit+1);
            }
            if(e.type==='invalid' && e.target.closest?.('#recording-settings-form'))
              counters.invalid=Math.min(16,counters.invalid+1);
          };
          for(const event of ['click','submit','invalid'])
            document.addEventListener(event,listener,true);
          window.__k5MotionDiagnostic={counters,close:()=>{
            for(const event of ['click','submit','invalid'])
              document.removeEventListener(event,listener,true);
            delete window.__k5MotionDiagnostic;
          }};
        }""")

    def _matches(self, request):
        return request.method == "PUT" and urlsplit(request.url).path == self.route

    def _request(self, request):
        if self._matches(request):
            self.network["put_requests"] = min(16, self.network["put_requests"] + 1)

    def _response(self, response):
        if self._matches(response.request):
            self.network["put_responses"] = min(16, self.network["put_responses"] + 1)
            if len(self.network["put_http_statuses"]) < 16:
                self.network["put_http_statuses"].append(response.status)

    def _failed(self, request):
        if self._matches(request):
            self.network["put_failed"] = min(16, self.network["put_failed"] + 1)

    def _console(self, message):
        if message.type == "error":
            self.network["console_errors"] = min(16, self.network["console_errors"] + 1)

    def _pageerror(self, _error):
        self.network["page_errors"] = min(16, self.network["page_errors"] + 1)

    def captured(self, preview):
        self.preview_started = time.monotonic()
        self.preview_ttl = preview["expires_in_seconds"]
        self.preview_matches_source = preview["source_revision"] == self.baseline["source_revision"]

    def snapshot(self, label):
        try:
            value = self.page.evaluate("""() => {
              const q=id=>document.getElementById(id);
              const form=q('recording-settings-form'), save=q('recording-settings-save');
              const validity={};
              for(const id of ['recording-mode','motion-sensitivity',
                'motion-minimum-area','motion-post-capture']) {
                const input=q(id), flags={};
                if(input)for(const key of ['badInput','customError','patternMismatch',
                  'rangeOverflow','rangeUnderflow',
                  'stepMismatch','tooLong','tooShort','typeMismatch','valid','valueMissing'])
                    flags[key]=Boolean(input.validity[key]);
                validity[id]={present:Boolean(input),disabled:Boolean(input?.disabled),
                  will_validate:Boolean(input?.willValidate),flags};
              }
              const messages={
                '':'empty',
                ["The frozen frame expired. Capture a new frame be" +
                 "fore saving zone changes."]:
                  "preview_expired",
                ["The camera connection changed. Refresh recording" +
                 " settings and review the zones."]:
                  "connection_changed",
                ["Check the recording values and finish or cancel " +
                 "the zone being drawn."]:
                  "invalid_request",
                ["The zones leave no area for motion detection. Re" +
                 "move or reduce an exclusion."]:
                  "zones_empty",
                'These settings changed elsewhere. Refresh before saving again.':'policy_conflict',
                ["The camera connection or view changed. Refresh s" +
                 "ettings and review the zones."]:
                  "source_changed",
                ["The save outcome could not be confirmed. Refresh" +
                 " before making another change."]:
                  "save_unconfirmed",
                'Your session ended. Sign in again.':'session_ended',
                ["Your current account cannot change these recordi" +
                 "ng settings or view this frame."]:
                  "permission_denied",
                ["Recording settings saved. This camera uses the s" +
                 "aved mode even when this dialog or browser close" +
                 "s."]:
                  "saved"
              };
              const states={'Continuous recording':'continuous',
                'Armed · Waiting for motion':'armed',
                'Motion recording':'recording','Finishing motion recording':'post_capture',
                'Recording paused':'paused','Storage blocked':'storage_blocked',
                'Motion unavailable':'unavailable',
                'Zones need review':'zones_need_review','Settings need refresh':'needs_refresh',
                'Reading recording settings…':'reading'};
              const svg=q('motion-zone-canvas'), image=svg?.querySelector('image');
              const cap=n=>Math.min(16,n);
              return {
                dialog_open:Boolean(q('recording-settings-dialog')?.open),save_present:Boolean(save),
                save_disabled:Boolean(save?.disabled),form_present:Boolean(form),
                form_no_validate:Boolean(form?.noValidate),
                form_valid:Object.values(validity).every(v=>!v.will_validate || v.flags.valid),
                aria_busy:['true','false'].includes(form?.getAttribute('aria-busy'))?form.getAttribute('aria-busy'):'other',
                validity,message_code:messages[q('recording-settings-message')?.textContent]||'other',
                message_is_error:Boolean(q('recording-settings-message')?.classList.contains('error')),
                status_code:states[q('recording-settings-status')?.textContent]||'other',
                preview_blob_present:Boolean(image?.href?.baseVal?.startsWith('blob:')),
                preview_frame_hidden:Boolean(q('motion-preview-frame')?.hidden),
                include_count:cap(svg?.querySelectorAll('.motion-zone-polygon.include').length||0),
                exclude_count:cap(svg?.querySelectorAll('.motion-zone-polygon.exclude').length||0),
                draft_present:Boolean(svg?.querySelector('.motion-zone-draft')),
                draft_points:cap(svg?.querySelector('.motion-zone-draft')?.points?.numberOfItems||0),
                events:window.__k5MotionDiagnostic?.counters||null
              };
            }""")
            value.update(self.network)
            value["put_http_statuses"] = list(self.network["put_http_statuses"])
            value["preview_matches_initial_source"] = self.preview_matches_source
            if self.preview_started is not None:
                elapsed = time.monotonic() - self.preview_started
                value["preview_elapsed_seconds"] = round(elapsed, 3)
                value["preview_local_ttl_seconds"] = self.preview_ttl
                value["preview_local_ttl_elapsed"] = elapsed >= self.preview_ttl
            self.diagnostic.setdefault("graphical_save", {}).setdefault(self.name, {})[label] = (
                value
            )
        except BaseException:
            self.diagnostic.setdefault("graphical_save", {}).setdefault(self.name, {})[label] = {
                "observer_unavailable": True
            }

    def close(self):
        for name, callback in self.handlers.items():
            self.page.remove_listener(name, callback)
        try:
            self.page.evaluate("() => window.__k5MotionDiagnostic?.close()")
        except BaseException:
            pass


def _original_backend_stack(app, cohort, installed):
    """Reuse the proven bounded sampler; target only the remembered direct child."""
    result = {"scope": "original_owned_backend_stack_only", "product_qualification": False}
    try:
        targets = [
            item
            for item in cohort.snapshot()
            if item.is_running()
            and item.ppid() == app.pid
            and item.name().casefold() == "python.exe"
            and Path(item.exe()).resolve() == (installed / "runtime/python.exe").resolve()
        ]
        result["candidate_count"] = min(16, len(targets))
        if len(targets) != 1:
            result["status"] = "ambiguous_backend"
            return result
        root = Path(__file__).resolve().parents[2]
        spec = importlib.util.spec_from_file_location(
            "k5_owned_stack_sampler", root / "scripts/sample_owned_backend_motion.py"
        )
        assert spec is not None and spec.loader is not None
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        executable = helper.prepare_executable(Path(sys.executable).parent / "Scripts/py-spy.exe")
        index = helper.public_index(root / "src/k5vision")
        samples = []
        for _ in range(3):
            samples.append(helper.sample_once(executable, targets[0], index, timeout=0.8))
            if samples[-1]["outcome"] == "sampler_cleanup_incomplete":
                break
        output = Path.cwd() / "original-backend-diagnostic-output"
        output.mkdir(mode=0o700)
        with (output / "original-backend-stack.json").open("x", encoding="utf-8") as stream:
            json.dump({"product_qualification": False, "samples": samples}, stream, sort_keys=True)
            stream.write("\n")
        result["status"] = "sampled"
        result["outcomes"] = [sample["outcome"] for sample in samples]
        result["sample_cap_seconds"] = 0.8
        result["raw_data_retained"] = False
    except BaseException:
        result["status"] = "sample_unavailable"
    return result


def _failure_read(client, route, *, policy=False):
    """One one-second loopback GET; retain only finite diagnostic fields."""
    began = time.monotonic()
    connection = http.client.HTTPConnection("127.0.0.1", urlsplit(client.origin).port, timeout=1)
    result = {"outcome": "probe_error"}
    try:
        connection.request(
            "GET",
            route,
            headers={"Authorization": "Bearer " + client.token, "Origin": client.origin},
        )
        response = connection.getresponse()
        result = {"outcome": "response", "http_status": response.status}
        if policy and response.status == 200:
            if connection.sock is not None:
                connection.sock.settimeout(max(0.001, 1 - (time.monotonic() - began)))
            raw = response.read(65_537)
            if len(raw) <= 65_536:
                value = json.loads(raw)
                if type(value) is dict:
                    if type(value.get("revision")) is int and 0 <= value["revision"] < 2**63:
                        result["revision"] = value["revision"]
                    if value.get("mode") in {"motion", "continuous"}:
                        result["mode"] = value["mode"]
                    if value.get("status") in {
                        "armed",
                        "recording",
                        "post_capture",
                        "continuous",
                        "paused",
                        "unavailable",
                        "storage_blocked",
                        "zones_need_review",
                    }:
                        result["state"] = value["status"]
                    zones = value.get("zones")
                    if type(zones) is dict:
                        for kind in ("include", "exclude"):
                            if type(zones.get(kind)) is list and len(zones[kind]) <= 8:
                                result[kind + "_count"] = len(zones[kind])
    except TimeoutError:
        result = {"outcome": "timeout"}
    except (OSError, http.client.HTTPException):
        result = {"outcome": "transport_error"}
    except (ValueError, TypeError):
        result = {"outcome": "invalid_response"}
    finally:
        connection.close()
    result["elapsed_seconds"] = round(time.monotonic() - began, 3)
    return result


def _save_graphically(page, camera, *, zones=False, observer=None):
    route = f"/api/v1/operator/cameras/{camera}/recording-policy"
    if observer is not None:
        observer.snapshot("before_click")
    try:
        with page.expect_response(
            lambda r: r.request.method == "PUT" and urlsplit(r.url).path == route,
            timeout=30_000,
        ) as response:
            page.locator("#recording-settings-save").click()
    except BaseException:
        if observer is not None:
            observer.snapshot("original_failure")
        raise
    _require(response.value.status == 200, "Graphical recording policy was not applied")
    value, sent = response.value.json(), response.value.request.post_data_json
    _require(
        value["mode"] == "motion"
        and value["pre_capture_seconds"] == 0
        and value["post_capture_seconds"] == 3,
        "Recording controls did not retain the selected actual capture policy",
    )
    if zones:
        _require(
            len(sent["zones"]["include"]) == len(sent["zones"]["exclude"]) == 1
            and isinstance(sent.get("preview_id"), str),
            "Graphical zones lacked the trusted frozen-frame binding",
        )
    return value


def _draw_zone(page, kind, points):
    page.locator("#motion-zone-" + kind).click()
    canvas = page.locator("#motion-zone-canvas")
    bounds = canvas.bounding_box()
    _require(bounds and bounds["width"] > 0 and bounds["height"] > 0, "Zone canvas was not visible")
    for x, y in points:
        canvas.click(position={"x": x * bounds["width"], "y": y * bounds["height"]})
    page.locator("#motion-zone-finish").click()


def _author_zones(page, camera, *, observer=None):
    preview_route = f"/api/v1/operator/cameras/{camera}/motion-preview"
    with page.expect_response(
        lambda r: r.request.method == "POST" and urlsplit(r.url).path == preview_route,
        timeout=30_000,
    ) as response:
        page.locator("#motion-preview-capture").click()
    _require(response.value.status == 200, "Trusted decoded preview was unavailable")
    preview = response.value.json()
    if observer is not None:
        observer.captured(preview)
    _require(
        preview["width"] == 320 and preview["height"] == 240 and preview["rotation"] == 0,
        "Frozen preview was not bound to the actual decoded source geometry",
    )
    page.locator("#motion-zone-canvas").wait_for(state="visible")
    decoded = page.locator("#motion-zone-canvas").evaluate("""async svg => {
      const image=svg.querySelector('image');
      const url=image?.href?.baseVal || '';
      if(!url.startsWith('blob:'))return null;
      const blob=await (await fetch(url)).blob();
      const bitmap=await createImageBitmap(blob);
      const result={width:bitmap.width,height:bitmap.height,type:blob.type,bytes:blob.size};
      bitmap.close(); return result;
    }""")
    _require(
        decoded
        and decoded["width"] == 320
        and decoded["height"] == 240
        and decoded["type"] == "image/jpeg"
        and 0 < decoded["bytes"] <= 2 * 1024**2,
        "Frozen zone authoring did not decode an actual owned server JPEG",
    )
    _draw_zone(page, "include", [(0.02, 0.02), (0.8, 0.02), (0.8, 0.98), (0.02, 0.98)])
    _draw_zone(page, "exclude", [(0.49, 0.02), (0.8, 0.02), (0.8, 0.98), (0.49, 0.98)])
    saved = _save_graphically(page, camera, zones=True, observer=observer)
    _require(
        saved["zones"]["reference"]
        == {
            "width": 320,
            "height": 240,
            "rotation": 0,
            "source_revision": preview["source_revision"],
        },
        "Saved detector zones lost their source geometry binding",
    )
    return saved, preview


def _clock_holds(client, camera, *, desktop, publisher, app, parent, other_path, profile):
    guard = _helpers("windows_vm_clock").WindowsVmClockGuard(
        parent / "motion-clock-guard.json", watchdog_seconds=90
    )
    phases = []
    with guard:
        for offset in (172800, -172800):
            publisher.movement("included")
            _policy_state(client, camera, {"recording"}, desktop, app=app, timeout=15)
            before = desktop._recording_bytes(profile, other_path)
            publisher.movement("idle")
            _policy_state(client, camera, {"post_capture"}, desktop, app=app, timeout=10)
            began = time.monotonic()
            native = guard.shift(offset)
            _require(native["verified"] is True, "Actual motion clock step was not observed")
            after_step = _policy(client, camera)["status"]
            _require(after_step == "post_capture", "Wall time expired the motion hold early")
            _policy_state(client, camera, {"armed"}, desktop, app=app, timeout=8)
            elapsed = time.monotonic() - began
            _require(0.5 <= elapsed <= 8, "Motion post-capture did not use elapsed time")
            restored = guard.restore()
            after = desktop._recording_bytes(profile, other_path)
            _require(after > before, "Clock correction interrupted the other recorder")
            _require(restored["restored"] is True, "Motion test did not restore actual UTC")
            phases.append(
                {
                    "offset_seconds": offset,
                    "after_step_state": after_step,
                    "final_state": "armed",
                    "post_capture_elapsed_seconds": round(elapsed, 3),
                    "other_recording_grew": True,
                    "utc_restored": True,
                }
            )
    return {"guard": guard.receipt(), "phases": phases}


def _permission_journey(page, client, camera, browser_helpers, *, diagnostic=None):
    diagnostic = {} if diagnostic is None else diagnostic
    admin = _helpers("test_operator_admin_browser")
    policy_route = f"/api/v1/operator/cameras/{camera}/recording-policy"
    preview_route = f"/api/v1/operator/cameras/{camera}/motion-preview"
    statuses = {}
    contexts = []
    try:
        username, password = "motion-operator-" + secrets.token_hex(4), secrets.token_urlsafe(24)
        _, temporary = admin._create_user(page, username, "operator", [camera])
        context, observer = admin._new_user_page(page)
        contexts.append(context)
        token = admin._activate(observer, client.origin, username, temporary, password)
        browser_helpers._open_camera_watch(observer, camera)
        _require(
            observer.locator("#camera-recording-settings-button").is_hidden(),
            "Operator received motion administration controls",
        )
        current = _policy(client, camera)
        payload = {
            "expected_revision": current["revision"],
            **{
                key: current[key]
                for key in (
                    "mode",
                    "post_capture_seconds",
                    "sensitivity",
                    "minimum_area_percent",
                    "zones",
                )
            },
        }
        for name, method, route, body in (
            ("operator_policy_get", "GET", policy_route, None),
            ("operator_policy_put", "PUT", policy_route, payload),
            ("operator_preview_post", "POST", preview_route, None),
        ):
            status, _ = admin._api(observer, token, method, route, body)
            _require(status == 403, "Motion recorder permission was not enforced")
            statuses[name] = status
        admin._return_to_cameras(page)
        username, password = "motion-admin-" + secrets.token_hex(4), secrets.token_urlsafe(24)
        # The existing limited-user helper always edits selected camera grants;
        # administrators correctly have that control disabled and access all.
        admin._open_admin(page)
        page.locator("#admin-create-user").click()
        page.locator("#admin-user-dialog").wait_for(state="visible")
        page.locator("#admin-username").fill(username)
        page.locator("#admin-display-name").fill("Generated motion administrator")
        page.locator("#admin-role").select_option("administrator")
        page.locator("#admin-enabled").set_checked(True)
        _require(
            page.locator("#admin-access-mode").is_disabled(),
            "Administrator camera scope was editable",
        )
        with page.expect_response(
            lambda r: (
                r.request.method == "POST"
                and urlsplit(r.url).path == "/api/v1/operator/admin/users"
            )
        ) as created:
            page.locator('#admin-user-form button[type="submit"]').click()
        _require(created.value.status == 201, "Second administrator creation failed")
        temporary = admin._credential(page, username)
        context, other_admin = admin._new_user_page(page)
        contexts.append(context)
        token = admin._activate(other_admin, client.origin, username, temporary, password)
        status, _ = admin._api(other_admin, token, "GET", policy_route)
        _require(status == 200, "Second administrator lacked current camera authority")
        preview = client.json(preview_route, method="POST")
        owner_status, body, headers = client.get(preview["preview_url"], max_bytes=2 * 1024**2)
        _require(
            owner_status == 200
            and body.startswith(b"\xff\xd8")
            and headers.get("content-type", "").split(";", 1)[0] == "image/jpeg",
            "Preview owner did not receive the real private JPEG",
        )
        status, denied = admin._api(other_admin, token, "GET", preview["preview_url"])
        diagnostic["foreign_preview_http_status"] = status
        code = denied.get("detail") if isinstance(denied, dict) else None
        diagnostic["foreign_preview_api_code"] = (
            code if code == "motion_preview_expired" else "other"
        )
        _require(
            status == 410 and code == "motion_preview_expired",
            "Foreign-session preview did not retain its bounded expired-lease refusal",
        )
        _require(
            client.get(preview["preview_url"])[0] == 200,
            "A foreign lookup invalidated the owner's current preview",
        )
        statuses.update(preview_owner_get=owner_status, foreign_preview_get=status)
        admin._return_to_cameras(page)
        return statuses
    finally:
        for context in contexts:
            context.close()


def _history_export(
    page,
    client,
    camera,
    *,
    idle_middle,
    trigger_start,
    trigger_end,
    desktop,
    browser_helpers,
    video_helpers,
    ffmpeg,
    ffprobe,
    parent,
    helpers,
):
    start, end = idle_middle - timedelta(seconds=2), trigger_end + timedelta(seconds=10)
    route = f"/api/v1/operator/cameras/{camera}/history"
    value = client.json(
        route + "?" + urlencode({"start": start.isoformat(), "end": end.isoformat()})
    )
    spans = value["spans"]
    _require(bool(spans), "Motion recording produced no UTC history")
    windows = [
        (datetime.fromisoformat(s["started_at"].replace("Z", "+00:00")), s["duration_seconds"])
        for s in spans
    ]
    _require(
        not any(
            begin <= idle_middle <= begin + timedelta(seconds=duration)
            for begin, duration in windows
        ),
        "History invented footage during the measured motion-idle gap",
    )
    choices = [
        (begin, duration)
        for begin, duration in windows
        if begin >= trigger_start - timedelta(seconds=2) and duration >= 4
    ]
    _require(bool(choices), "Motion event did not retain a complete decodable clip")
    begin, duration = choices[0]
    chosen = datetime.fromtimestamp(int(begin.timestamp()) + 1, UTC)
    _require(
        chosen + timedelta(seconds=2) < begin + timedelta(seconds=duration),
        "Motion clip exceeded its real span",
    )
    browser_helpers._open_camera_watch(page, camera)
    page.locator("#history-tab").click()
    page.locator("#history-start").fill(browser_helpers._datetime_local_value(start))
    page.locator("#history-end").fill(browser_helpers._datetime_local_value(end))
    with page.expect_response(
        lambda r: r.request.method == "GET" and urlsplit(r.url).path == route
    ):
        page.locator('#history-form button[type="submit"]').click()
    page.locator(".history-item").first.wait_for(state="visible")
    page.wait_for_function("() => !document.getElementById('load-history').disabled")
    page.locator(".history-item").first.click()
    page.wait_for_function("() => !document.getElementById('playback-duration').disabled")
    page.locator("#playback-start").fill(browser_helpers._datetime_local_value(chosen))
    page.locator("#clip-start").fill(browser_helpers._datetime_local_value(chosen))
    page.locator("#playback-duration").fill("2")
    page.locator("#play-recording").click()
    decoded = video_helpers._decoded_geometry(page, "#playback-video", "red", 320, 240)
    export_path = parent / "generated motion recording.mp4"
    evidence = {}
    frames = browser_helpers._download_recording(
        page, ffprobe, export_path, helpers, "red", evidence=evidence
    )
    actual = video_helpers._probe(ffprobe, export_path, network=False)
    _require(
        actual
        and actual["codec"] == "h264"
        and actual["width"] == 320
        and actual["height"] == 240
        and abs(actual["fps"] - 10) < 0.01,
        "Saved motion export did not decode as the actual generated camera",
    )
    return {
        "history_spans": len(spans),
        "playback_frames": decoded,
        "export_frames": frames,
        "export_bytes": evidence["bytes"],
        "export_sha256": evidence["sha256"],
        "idle_middle_utc": idle_middle.isoformat().replace("+00:00", "Z"),
        "clip_start_utc": chosen.isoformat().replace("+00:00", "Z"),
    }


def test_native_windows_motion_recording_and_zones(record_testsuite_property):
    __tracebackhide__ = True
    started, receipt, diagnostic = time.monotonic(), {}, {"stage": "graphical_install"}
    desktop, runtime = (
        _helpers("test_windows_desktop_app"),
        _helpers("test_operator_product_runtime"),
    )
    browser_helpers, video_helpers = (
        _helpers("test_operator_beta_browser"),
        _helpers("test_windows_camera_video_settings"),
    )
    fixture_module, onvif = (
        _helpers("motion_camera_fixture"),
        _helpers("onvif_video_camera_fixture"),
    )
    helpers, browser_api = runtime._helpers(), pytest.importorskip("playwright.sync_api")
    _require(version("playwright") == "1.58.0", "Reviewed Playwright version is absent")
    setup = Path(os.environ["K5_DESKTOP_SETUP"]).resolve(strict=True)
    installer_sha = hashlib.sha256(setup.read_bytes()).hexdigest()
    _require(
        installer_sha == os.environ["K5_DESKTOP_SETUP_SHA256"], "Motion gate received another Setup"
    )
    media, ffmpeg, ffprobe = (
        helpers._tool(x) for x in ("K5_MEDIAMTX_DONOR_EXE", "K5_DONOR_FFMPEG", "K5_DONOR_FFPROBE")
    )
    parent = Path(os.environ["LOCALAPPDATA"]) / ("K5 Motion Proof-" + uuid4().hex)
    identity_state.validate_new_identity_directory(parent)
    parent.mkdir(mode=0o700)
    installed, profile = parent / "installed app with spaces", parent / "private profile"
    api, processes, fixtures, reservations = desktop._Windows(), [], [], []
    cohort = desktop._Cohort(api)
    app = form = playwright = browser = context = page = publisher = None
    failed = cleanup_failed = False
    try:
        desktop._install_gui(setup, installed, api, diagnostic=diagnostic)
        desktop._verify_installed_worker_bundle(installed)
        receipt.update(actual_gui_installer=True, installed_worker_bundle_verified=True)
        reservations, ports = helpers._reserve_ports(3)
        for listener in reservations:
            listener.close()
        app = desktop._start_app(installed, profile, ports[0], cohort)
        form = desktop._wait(lambda: api.form(app.pid), "Motion first-account window", process=app)
        desktop._wait(
            lambda: api.api.IsWindowEnabled(api.control(form, desktop._CREATE)),
            "Motion first-account control",
            process=app,
        )
        username, password = "motion-fixture-admin", secrets.token_urlsafe(32)
        for field, text in (
            (desktop._USERNAME, username),
            (desktop._PASSWORD, password),
            (desktop._CONFIRM, password),
        ):
            api.set_text(form, field, text)
        api.click(api.control(form, desktop._CREATE))
        client = runtime._Client(ports[0])
        desktop._wait(
            lambda: desktop._healthy(client, diagnostic),
            "Motion embedded startup",
            process=app,
            timeout=60,
        )
        native_python = [
            p
            for p in cohort.snapshot()
            if p.is_running() and p.name().casefold() in {"python.exe", "pythonw.exe"}
        ]
        _require(
            native_python
            and all(Path(p.exe()).resolve().is_relative_to(installed) for p in native_python),
            "Motion gate used external Python",
        )
        receipt.update(actual_gui_account_setup=True, embedded_runtime_independent=True)
        sources = []
        for index, color in enumerate(("red", "blue")):
            generated, uri = desktop._generate_fixture(
                parent,
                media,
                ffmpeg,
                ports[index + 1],
                helpers,
                host=f"127.0.0.{index + 1}",
                color=color,
            )
            processes.extend(generated)
            sources.append(uri)
            if index == 0:
                helpers._stop_owned(generated[-1])
                publisher = fixture_module.MotionPublisher(ffmpeg, uri)
            fixtures.append(
                onvif.OnvifVideoCameraFixture(host=f"127.0.0.{index + 1}", stream_uri=uri)
            )
        playwright = browser_api.sync_playwright().start()
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 1000}, accept_downloads=True
        )
        page = context.new_page()
        page.set_default_timeout(25_000)
        _login(page, client, username, password)
        ids = []
        for index, fixture in enumerate(fixtures):
            camera, _ = browser_helpers._onvif_enroll(
                page, name=f"Generated motion camera {index + 1}", fixture=fixture
            )
            ids.append(camera["device_id"])
        target, other = ids
        identity = identity_state.load_identity_state(profile / "identity")
        paths = [
            profile / "recordings" / MediaMTXBinding(identity.site_id, UUID(x)).path_name
            for x in ids
        ]
        browser_helpers._open_camera_watch(page, target)
        video_helpers._decoded_geometry(page, "#live-video", "red", 320, 240)
        receipt.update(
            schema_version=1,
            head_sha=os.environ["K5_DESKTOP_HEAD_SHA"],
            installer_sha256=installer_sha,
            platform=platform.system(),
            browser_channel="msedge",
            browser_version=browser.version,
            playwright_version="1.58.0",
            native_windows_execution=True,
            generated_media_only=True,
            actual_edge_ui=True,
        )
        diagnostic["stage"] = "graphical_motion_mode"
        page.locator("#camera-recording-settings-button").click()
        page.locator("#recording-settings-dialog").wait_for(state="visible")
        page.locator("#recording-mode").select_option("motion")
        page.locator("#motion-post-capture").fill("3")
        page.locator("#motion-minimum-area").fill("0.1")
        observer = _GraphicalSaveDiagnostic(page, target, diagnostic, {}, name="initial_mode")
        try:
            initial = _save_graphically(page, target, observer=observer)
        finally:
            observer.snapshot("after_initial_save")
            observer.close()
        _require(
            initial["zones"] == {"reference": None, "include": [], "exclude": []},
            "Default motion zone was not the full frame",
        )
        page.locator("#recording-settings-cancel").click()
        observations = {}
        diagnostic["stage"] = "full_frame_idle"
        observations["full_frame_idle"] = _observe_recording(
            client,
            target,
            paths=paths,
            profile=profile,
            desktop=desktop,
            publisher=publisher,
            movement="idle",
            state={"armed"},
            growing=False,
            app=app,
        )
        diagnostic["stage"] = "full_frame_motion"
        observations["full_frame_motion"] = _observe_recording(
            client,
            target,
            paths=paths,
            profile=profile,
            desktop=desktop,
            publisher=publisher,
            movement="outside",
            state={"recording"},
            growing=True,
            app=app,
        )
        publisher.movement("idle")
        _policy_state(client, target, {"armed"}, desktop, app=app)
        diagnostic["stage"] = "graphical_zone_authoring"
        page.locator("#camera-recording-settings-button").click()
        observer = _GraphicalSaveDiagnostic(page, target, diagnostic, initial)
        try:
            saved, preview = _author_zones(page, target, observer=observer)
        finally:
            observer.snapshot("after_zone_authoring")
            observer.close()
        page.locator("#recording-settings-cancel").click()
        diagnostic["stage"] = "motion_permissions_and_preview_owner"
        receipt["permission_statuses"] = _permission_journey(
            page, client, target, browser_helpers, diagnostic=diagnostic
        )
        observations["excluded_only"] = _observe_recording(
            client,
            target,
            paths=paths,
            profile=profile,
            desktop=desktop,
            publisher=publisher,
            movement="excluded",
            state={"armed"},
            growing=False,
            app=app,
        )
        observations["outside_inclusion"] = _observe_recording(
            client,
            target,
            paths=paths,
            profile=profile,
            desktop=desktop,
            publisher=publisher,
            movement="outside",
            state={"armed"},
            growing=False,
            app=app,
        )
        publisher.movement("idle")
        observations["zoned_idle"] = _observe_recording(
            client,
            target,
            paths=paths,
            profile=profile,
            desktop=desktop,
            publisher=publisher,
            movement="idle",
            state={"armed"},
            growing=False,
            app=app,
        )
        idle_middle = datetime.now(UTC) - timedelta(seconds=2)
        trigger_start = datetime.now(UTC)
        diagnostic["stage"] = "included_motion_recording"
        observations["included_motion"] = _observe_recording(
            client,
            target,
            paths=paths,
            profile=profile,
            desktop=desktop,
            publisher=publisher,
            movement="included",
            state={"recording"},
            growing=True,
            app=app,
            seconds=6,
        )
        trigger_end = datetime.now(UTC)
        publisher.movement("idle")
        _policy_state(client, target, {"post_capture"}, desktop, app=app)
        observations["post_capture_idle"] = _observe_recording(
            client,
            target,
            paths=paths,
            profile=profile,
            desktop=desktop,
            publisher=publisher,
            movement="idle",
            state={"armed"},
            growing=False,
            app=app,
        )
        retained = video_helpers._closed_subset(sorted(paths[0].glob("*.mp4")))
        diagnostic["stage"] = "browser_closed_motion"
        context.close()
        context = page = None
        observations["browser_closed_recording"] = _observe_recording(
            client,
            target,
            paths=paths,
            profile=profile,
            desktop=desktop,
            publisher=publisher,
            movement="included",
            state={"recording"},
            growing=True,
            app=app,
        )
        observations["browser_closed_idle"] = _observe_recording(
            client,
            target,
            paths=paths,
            profile=profile,
            desktop=desktop,
            publisher=publisher,
            movement="idle",
            state={"armed"},
            growing=False,
            app=app,
        )
        diagnostic["stage"] = "native_motion_restart"
        before_restart = _policy(client, target)
        desktop._stop_app(app, form, api, cohort)
        app = desktop._start_app(installed, profile, ports[0], cohort)
        form = desktop._wait(lambda: api.form(app.pid), "Returning motion window", process=app)
        desktop._wait(
            lambda: desktop._healthy(client, diagnostic),
            "Returning motion backend",
            process=app,
            timeout=60,
        )
        runtime._login(client, username, password)
        after_restart = _policy_state(client, target, {"armed"}, desktop, app=app)
        for field in (
            "revision",
            "source_revision",
            "policy_source_revision",
            "mode",
            "zones",
            "post_capture_seconds",
            "sensitivity",
            "minimum_area_percent",
        ):
            _require(
                after_restart[field] == before_restart[field],
                "Restart lost the saved motion policy",
            )
        observations["restart_idle"] = _observe_recording(
            client,
            target,
            paths=paths,
            profile=profile,
            desktop=desktop,
            publisher=publisher,
            movement="idle",
            state={"armed"},
            growing=False,
            app=app,
        )
        diagnostic["stage"] = "master_pause_override"
        route = f"/api/v1/operator/cameras/{target}/recording"
        _require(
            client.get(route, method="PUT", data={"enabled": False})[0] == 200,
            "Master pause failed",
        )
        observations["master_paused"] = _observe_recording(
            client,
            target,
            paths=paths,
            profile=profile,
            desktop=desktop,
            publisher=publisher,
            movement="included",
            state={"paused"},
            growing=False,
            app=app,
        )
        publisher.movement("idle")
        _require(
            client.get(route, method="PUT", data={"enabled": True})[0] == 200,
            "Master resume failed",
        )
        _policy_state(client, target, {"armed"}, desktop, app=app)
        diagnostic["stage"] = "actual_motion_clock_holds"
        receipt["motion_clock"] = _clock_holds(
            client,
            target,
            desktop=desktop,
            publisher=publisher,
            app=app,
            parent=parent,
            other_path=paths[1],
            profile=profile,
        )
        context = browser.new_context(
            viewport={"width": 1440, "height": 1000}, accept_downloads=True
        )
        page = context.new_page()
        page.set_default_timeout(25_000)
        _login(page, client, username, password)
        diagnostic["stage"] = "retained_motion_utc_playback_export"
        receipt["retained"] = _history_export(
            page,
            client,
            target,
            idle_middle=idle_middle,
            trigger_start=trigger_start,
            trigger_end=trigger_end,
            desktop=desktop,
            browser_helpers=browser_helpers,
            video_helpers=video_helpers,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            parent=parent,
            helpers=helpers,
        )
        _require(desktop._hashes_preserved(retained), "Motion operations rewrote retained files")
        diagnostic["stage"] = "actual_source_geometry_change"
        publisher.movement("idle")
        publisher.geometry(640, 360)
        _policy_state(client, target, {"zones_need_review"}, desktop, app=app, timeout=25)
        observations["stale_geometry"] = _observe_recording(
            client,
            target,
            paths=paths,
            profile=profile,
            desktop=desktop,
            publisher=publisher,
            movement="included",
            state={"zones_need_review"},
            growing=False,
            app=app,
        )
        browser_helpers._open_camera_watch(page, target)
        page.locator("#camera-recording-settings-button").click()
        page.wait_for_function(
            "() => document.getElementById('recording-settings-status')"
            ".textContent.toLowerCase().includes('review')"
        )
        _require(
            "review" in page.locator("#recording-settings-status").inner_text().casefold(),
            "Stale geometry was hidden from the operator",
        )
        receipt.update(
            observations=observations,
            policy_revision_before_restart=before_restart["revision"],
            policy_revision_after_restart=after_restart["revision"],
            policy_source_before_restart=before_restart["source_revision"],
            policy_source_after_restart=after_restart["source_revision"],
            preview_geometry={
                "width": preview["width"],
                "height": preview["height"],
                "rotation": preview["rotation"],
            },
            changed_geometry={"width": 640, "height": 360},
            post_capture_seconds=3,
            pre_capture_seconds=0,
            retained_file_count=len(retained),
        )
        _require(
            all(f.qualified_requests() for f in fixtures),
            "Authenticated ONVIF enrollment was incomplete",
        )
        browser_helpers._assert_no_browser_storage(page)
        context.close()
        context = page = None
        browser.close()
        browser = None
        diagnostic["stage"] = "owned_stop_and_uninstall"
        desktop._stop_app(app, form, api, cohort, diagnostic=diagnostic)
        _require(
            not cohort.console_seen and not cohort.failed, "Motion worker opened a console window"
        )
        files = sorted((profile / "recordings").rglob("*.mp4"))
        result = subprocess.run(
            [str(installed / "unins000.exe"), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=45,
            check=False,
        )
        _require(
            result.returncode == 0
            and files
            and all(x.is_file() for x in files)
            and identity_state.load_identity_state(profile / "identity").site_id
            == identity.site_id,
            "Uninstall lost the motion profile or footage",
        )
        receipt.update(dict.fromkeys(_TRUE_FLAGS, True))
    except BaseException as error:
        failed = True
        diagnostic["failure_kind"] = (
            type(error).__name__
            if type(error).__name__
            in {
                "AssertionError",
                "Failed",
                "TimeoutError",
                "RuntimeError",
                "Error",
                "TimeoutExpired",
            }
            else "other"
        )
        trace = error.__traceback__
        callers = []
        message = getattr(error, "msg", None)
        while trace is not None:
            if Path(trace.tb_frame.f_code.co_filename).name == Path(__file__).name:
                diagnostic["failure_function"] = trace.tb_frame.f_code.co_name
                diagnostic["failure_line"] = trace.tb_lineno
                callers.append({"function": trace.tb_frame.f_code.co_name, "line": trace.tb_lineno})
                if type(message) is str and message in trace.tb_frame.f_code.co_consts:
                    diagnostic["failure_assertion_code"] = _ASSERTION_CODES.get(
                        message, "literal_" + hashlib.sha256(message.encode()).hexdigest()[:12]
                    )
            trace = trace.tb_next
        diagnostic["failure_callers"] = callers[-4:]
        if diagnostic.get("stage") == "graphical_zone_authoring":
            diagnostic["failure_reads"] = {
                "health": _failure_read(client, "/api/v1/health"),
                "policy": _failure_read(
                    client, f"/api/v1/operator/cameras/{target}/recording-policy", policy=True
                ),
            }
            diagnostic["original_backend_stack"] = _original_backend_stack(app, cohort, installed)
    finally:
        for resource in (context, browser, playwright):
            if resource is not None:
                try:
                    resource.stop() if resource is playwright else resource.close()
                except BaseException:
                    cleanup_failed = True
        cohort.finish()
        for process in reversed(cohort.snapshot()):
            try:
                if process.is_running():
                    process.kill()
                    process.wait(timeout=10)
            except psutil.NoSuchProcess:
                pass
            except BaseException:
                cleanup_failed = True
        if failed and diagnostic.get("stage") == "graphical_zone_authoring":
            try:
                helper_path = (
                    Path(__file__).resolve().parents[2] / "scripts/diagnose_live_motion_backend.py"
                )
                spec = importlib.util.spec_from_file_location("k5_live_diagnostic", helper_path)
                assert spec is not None and spec.loader is not None
                helper = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = helper
                try:
                    spec.loader.exec_module(helper)
                    diagnostic["live_backend_probe"] = helper.probe_live_backend(
                        installed,
                        profile,
                        ports[0],
                        Path.cwd() / "live-backend-diagnostic-output",
                        username=username,
                        password=password,
                        camera_id=target,
                    )
                finally:
                    sys.modules.pop(spec.name, None)
            except BaseException:
                diagnostic["live_backend_probe"] = {"probe_helper_failed": True}
        for resource in [publisher, *fixtures]:
            if resource is not None:
                try:
                    resource.close()
                except BaseException:
                    cleanup_failed = True
        for process in reversed(processes):
            try:
                helpers._stop_owned(process)
            except BaseException:
                cleanup_failed = True
        for listener in reservations:
            listener.close()
        if parent.exists():
            try:
                shutil.rmtree(parent)
            except BaseException:
                cleanup_failed = True
        if failed or cleanup_failed:
            safe = {
                k: v
                for k, v in diagnostic.items()
                if k
                in {
                    "stage",
                    "failure_kind",
                    "failure_function",
                    "failure_line",
                    "failure_callers",
                    "failure_assertion_code",
                    "installer_action",
                    "installer_install_posted",
                    "installer_steps_seen",
                    "installer_exit_code",
                    "health_http_status",
                    "foreign_preview_http_status",
                    "foreign_preview_api_code",
                    "graphical_save",
                    "live_backend_probe",
                    "failure_reads",
                    "original_backend_stack",
                }
            }
            safe.update(failed=failed, cleanup_failed=cleanup_failed)
            record_testsuite_property("motion_windows_diagnostic", json.dumps(safe, sort_keys=True))
            print("Motion qualification diagnostic: " + json.dumps(safe, sort_keys=True))
    _require(not failed and not cleanup_failed, "Installed motion qualification failed")
    duration = time.monotonic() - started
    _require(duration < 420, "Installed motion controls exceeded their bounded duration")
    receipt.update(duration_seconds=round(duration, 3), owned_cleanup=True)
    receipt.update(dict.fromkeys(_FALSE_FLAGS, False))
    record_testsuite_property("motion_windows_receipt", json.dumps(receipt, sort_keys=True))


_TRUE_FLAGS = {
    "native_windows_execution",
    "generated_media_only",
    "actual_gui_installer",
    "installed_worker_bundle_verified",
    "actual_gui_account_setup",
    "embedded_runtime_independent",
    "actual_edge_ui",
    "graphical_mode_save",
    "graphical_zone_authoring",
    "trusted_preview",
    "backend_zone_enforced",
    "full_frame_default",
    "zero_precapture_explicit",
    "browser_closed_detection",
    "restart_policy_persisted",
    "global_pause_wins",
    "other_camera_recording_continues",
    "nonadmin_motion_denied",
    "preview_owner_scoped",
    "source_geometry_invalidates",
    "retained_files_preserved",
    "retained_utc_history",
    "recording_gap_honest",
    "retained_playback_decoded",
    "retained_export_decoded",
    "monotonic_post_capture",
    "actual_clock_corrections",
    "utc_restored",
    "time_services_restored",
    "no_console_windows",
    "uninstall_preserves_profile",
}
_FALSE_FLAGS = {
    "physical_camera_acceptance",
    "capacity_acceptance",
    "endurance_acceptance",
    "permanent_privacy_mask",
    "pre_event_buffer",
    "mocked_trigger",
    "mocked_gui",
    "user_terminal_required",
}
