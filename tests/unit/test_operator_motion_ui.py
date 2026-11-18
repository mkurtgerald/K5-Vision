"""Browser controls for recording policy and authorized, geometry-bound motion zones."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

_ASSETS = Path(__file__).resolve().parents[2] / "src/k5vision/operator_web"
_CAMERA = "e4fd401d-ae8e-4ac2-9f92-e8287f241532"
_OTHER = "edbc1678-bf89-4936-bbc5-15aff54ffb83"


@pytest.fixture
def motion_page():
    if os.getenv("K5_OPERATOR_BROWSER_ACCEPTANCE") != "1":
        pytest.skip("Motion editor proof requires the explicit browser environment")
    browser_api = pytest.importorskip("playwright.sync_api")
    with browser_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            channel=os.getenv(
                "K5_OPERATOR_BROWSER_CHANNEL", "msedge" if os.name == "nt" else "chromium"
            ),
            headless=True,
        )
        context = browser.new_context(
            viewport={"width": 1200, "height": 1000}, device_scale_factor=2
        )
        page = context.new_page()
        page.set_default_timeout(5000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("http://127.0.0.1/", lambda route: route.fulfill(body="<!doctype html>"))
        page.goto("http://127.0.0.1/")
        page.set_content('<dialog id="recording-settings-dialog"></dialog>')
        page.add_style_tag(path=str(_ASSETS / "app.css"))
        page.add_style_tag(path=str(_ASSETS / "recording-settings.css"))
        page.add_script_tag(path=str(_ASSETS / "recording-settings.js"))
        page.evaluate(
            """async ({id,other}) => {
              window.camera = {device_id:id,name:'Generated motion camera',
                source_revision:'inventory-only-old'};
              window.otherCamera = {device_id:other,name:'Other motion camera',
                source_revision:'inventory-other'};
              window.session = {token:'in-memory-generated-session',epoch:1,
                account:{role:'administrator',effective_permissions:['recording.manage','cameras.view_live']}};
              const defaults = (cameraId) => ({camera_id:cameraId,revision:0,
                source_revision:'a'.repeat(64),
                policy_source_revision:'a'.repeat(64),mode:'continuous',post_capture_seconds:10,
                sensitivity:50,minimum_area_percent:0.5,pre_capture_seconds:0,
                zones:{reference:null,include:[],exclude:[]},status:'continuous'});
              window.policies = {[id]:defaults(id),
                [other]:{...defaults(other),mode:'motion',status:'armed'}};
              window.previewId = 'generated_preview_01234567890123456789';
              window.previewExpires = 120; window.previewWidth=1920; window.previewHeight=1080;
              window.requests=[];window.saved=[];window.holdRead=false;window.holdPreview=false;
              window.pendingReads=[];window.pendingPreviews=[];window.changed=0;window.failSave=null;
              window.createdUrls=[];window.revokedUrls=[];
              const createObjectURL = URL.createObjectURL.bind(URL),
                revokeObjectURL = URL.revokeObjectURL.bind(URL);
              URL.createObjectURL = (blob) => {const value=createObjectURL(blob);
                window.createdUrls.push(value);return value;};
              URL.revokeObjectURL = (value) => {window.revokedUrls.push(value);
                revokeObjectURL(value);};
              const canvas=document.createElement('canvas');canvas.width=640;canvas.height=360;
              const drawing=canvas.getContext('2d');drawing.fillStyle='#559944';
              drawing.fillRect(0,0,640,360);
              window.previewBlob=await new Promise(resolve=>canvas.toBlob(resolve,'image/jpeg'));
              window.request = async (path,options={}) => {
                const method=options.method||'GET',cameraId=path.split('/')[5];
                window.requests.push({path,method,body:options.body?structuredClone(options.body):null});
                if(path.endsWith('/recording-policy')&&method==='GET'){
                  const value=structuredClone(window.policies[cameraId]);
                  if(window.holdRead)
                    return new Promise(resolve=>window.pendingReads.push({resolve,value}));
                  return value;
                }
                if(path.endsWith('/recording-policy')&&method==='PUT'){
                  if(window.failSave)throw window.failSave;
                  const body=structuredClone(options.body);window.saved.push(body);
                  window.policies[cameraId]={...window.policies[cameraId],...body,
                    revision:window.policies[cameraId].revision+1,
                    status:body.mode==='motion'?'armed':'continuous'};
                  delete window.policies[cameraId].expected_revision;
                  delete window.policies[cameraId].preview_id;
                  return structuredClone(window.policies[cameraId]);
                }
                if(path.endsWith('/motion-preview')&&method==='POST'){
                  const value={preview_id:window.previewId,
                    preview_url:window.previewUrl||path+'/'+window.previewId,source_revision:'a'.repeat(64),
                    width:window.previewWidth,height:window.previewHeight,rotation:0,
                    expires_in_seconds:window.previewExpires};
                  if(window.holdPreview)
                    return new Promise(resolve=>window.pendingPreviews.push({resolve,value}));
                  return value;
                }
                if(path.includes('/motion-preview/')&&options.responseType==='blob')
                  return window.previewBlob;
                throw {status:404};
              };
              window.editor=window.K5RecordingSettings.create({
                dialog:document.querySelector('dialog'),request:window.request,getSession:()=>window.session,
                onChanged:()=>window.changed++
              });
              window.editor.open(window.camera);
            }""",
            {"id": _CAMERA, "other": _OTHER},
        )
        page.wait_for_function(
            "document.querySelector('#recording-settings-form').getAttribute('aria-busy')==='false'"
        )
        yield page
        context.close()
        browser.close()
        assert not errors, errors


def _capture(page):
    page.locator("#motion-preview-capture").click()
    page.locator("#motion-preview-frame").wait_for(state="visible")
    page.wait_for_function(
        "document.querySelector('#recording-settings-form').getAttribute('aria-busy')==='false'"
    )


def _draw(page, kind, points):
    page.locator("#motion-zone-" + kind).click()
    box = page.locator("#motion-zone-canvas").bounding_box()
    assert box is not None
    for x, y in points:
        page.mouse.click(box["x"] + x * box["width"], box["y"] + y * box["height"])
    page.locator("#motion-zone-finish").click()


def test_motion_mode_and_graphical_zones_submit_only_bound_writable_policy(motion_page):
    page = motion_page
    page.locator("#recording-mode").select_option("motion")
    page.locator("#motion-sensitivity").fill("73")
    page.locator("#motion-post-capture").fill("15")
    _capture(page)
    _draw(page, "include", [(0.1, 0.1), (0.9, 0.1), (0.9, 0.9), (0.1, 0.9)])
    _draw(page, "exclude", [(0.3, 0.3), (0.6, 0.3), (0.6, 0.6), (0.3, 0.6)])
    page.locator("#recording-settings-save").click()
    page.wait_for_function("window.saved.length===1")
    body = page.evaluate("window.saved[0]")
    assert set(body) == {
        "expected_revision",
        "mode",
        "post_capture_seconds",
        "sensitivity",
        "minimum_area_percent",
        "zones",
        "preview_id",
    }
    assert body["expected_revision"] == 0 and body["mode"] == "motion"
    assert body["post_capture_seconds"] == 15 and body["sensitivity"] == 73
    assert body["zones"]["reference"] == {
        "width": 1920,
        "height": 1080,
        "rotation": 0,
        "source_revision": "a" * 64,
    }
    for kind, expected in [("include", [0.1, 0.1]), ("exclude", [0.3, 0.3])]:
        assert len(body["zones"][kind]) == 1
        assert body["zones"][kind][0]["points"][0] == pytest.approx(expected, abs=0.003)
    assert page.locator("#recording-settings-status").inner_text() == "Armed · Waiting for motion"
    assert page.evaluate("window.changed") == 1
    assert page.evaluate("window.revokedUrls.length") == 1


def test_keyboard_zone_authoring_and_cancel_preserve_no_unsaved_request(motion_page):
    page = motion_page
    _capture(page)
    page.locator("#motion-zone-include").click()
    canvas = page.locator("#motion-zone-canvas")
    for key in ["Enter", "ArrowRight", "Enter", "ArrowDown", "Enter"]:
        canvas.press(key)
    assert page.locator("#motion-zone-finish").is_enabled()
    page.locator("#motion-zone-finish").click()
    page.locator("#recording-settings-cancel").click()
    assert page.locator("#recording-settings-dialog").is_hidden()
    assert page.evaluate("window.saved") == []
    assert page.evaluate("window.revokedUrls.length") == 1
    page.evaluate("window.editor.open(window.camera)")
    page.wait_for_function(
        "document.querySelector('#recording-settings-form').getAttribute('aria-busy')==='false'"
    )
    assert page.locator("#recording-settings-save").is_disabled()


def test_cancel_drawing_does_not_leave_reference_only_policy_change(motion_page):
    page = motion_page
    _capture(page)
    page.locator("#motion-zone-include").click()
    page.locator("#motion-zone-canvas").press("Enter")
    page.locator("#motion-zone-canvas").press("Escape")
    assert page.locator("#recording-settings-dialog").is_visible()
    assert page.locator("#recording-settings-save").is_disabled()
    page.locator("#motion-zone-include").click()
    page.locator("#motion-zone-cancel").click()
    assert page.locator("#recording-settings-save").is_disabled()


def test_self_crossing_outline_requires_correction_before_finish(motion_page):
    page = motion_page
    _capture(page)
    page.locator("#motion-zone-include").click()
    box = page.locator("#motion-zone-canvas").bounding_box()
    for x, y in [(0.1, 0.1), (0.9, 0.9), (0.1, 0.9), (0.9, 0.1)]:
        page.mouse.click(box["x"] + x * box["width"], box["y"] + y * box["height"])
    assert page.locator("#motion-zone-finish").is_disabled()
    assert "crosses" in page.locator("#motion-preview-help").inner_text()
    assert page.locator("#recording-settings-save").is_disabled()


def test_normalized_points_follow_rendered_geometry_after_resize_and_high_dpi(motion_page):
    page = motion_page
    page.set_viewport_size({"width": 740, "height": 900})
    _capture(page)
    page.locator("#recording-settings-dialog").evaluate(
        "node=>{node.style.transform='scale(.83)';node.style.transformOrigin='top left'}"
    )
    _draw(page, "include", [(0.2, 0.2), (0.8, 0.2), (0.8, 0.8)])
    page.locator("#recording-settings-save").click()
    page.wait_for_function("window.saved.length===1")
    points = page.evaluate("window.saved[0].zones.include[0].points")
    for actual, expected in zip(points, [(0.2, 0.2), (0.8, 0.2), (0.8, 0.8)], strict=True):
        assert actual == pytest.approx(expected, abs=0.004)


def test_expired_preview_can_be_recaptured_without_losing_valid_zone_edits(motion_page):
    page = motion_page
    page.evaluate("window.previewExpires=1")
    _capture(page)
    _draw(page, "include", [(0.1, 0.1), (0.9, 0.1), (0.9, 0.9)])
    page.locator("#motion-preview-frame").wait_for(state="hidden")
    assert page.locator("#recording-settings-save").is_disabled()
    page.evaluate(
        "window.previewExpires=120;window.previewId='new_frame_authorization_0123456789012345'"
    )
    _capture(page)
    assert page.locator("#recording-settings-save").is_enabled()
    page.locator("#recording-settings-save").click()
    page.wait_for_function("window.saved.length===1")
    assert page.evaluate("window.saved[0].preview_id") == "new_frame_authorization_0123456789012345"
    assert len(page.evaluate("window.saved[0].zones.include")) == 1


def test_live_view_revocation_clears_pixels_and_preserves_mode_controls(motion_page):
    page = motion_page
    _capture(page)
    page.evaluate(
        "window.session.account.effective_permissions=['recording.manage'];window.editor.authority()"
    )
    assert page.locator("#motion-preview-frame").is_hidden()
    assert page.locator("#motion-preview-capture").is_disabled()
    assert page.evaluate("window.revokedUrls.length") == 1
    page.locator("#recording-mode").select_option("motion")
    assert page.locator("#recording-settings-save").is_enabled()
    page.locator("#recording-settings-save").click()
    page.wait_for_function("window.saved.length===1")
    assert "preview_id" not in page.evaluate("window.saved[0]")


def test_late_preview_after_permission_loss_cannot_fetch_or_show_pixels(motion_page):
    page = motion_page
    page.evaluate("window.holdPreview=true")
    page.locator("#motion-preview-capture").click()
    page.wait_for_function("window.pendingPreviews.length===1")
    page.evaluate(
        """() => {
          window.session.account.effective_permissions=['recording.manage'];window.editor.authority();
          window.pendingPreviews[0].resolve(window.pendingPreviews[0].value);
        }"""
    )
    page.wait_for_timeout(100)
    assert page.locator("#motion-preview-frame").is_hidden()
    assert (
        page.evaluate("window.requests.filter(r=>r.path.includes('/motion-preview/')).length") == 0
    )


def test_recording_permission_or_session_replacement_closes_editor_and_revokes_frame(motion_page):
    page = motion_page
    _capture(page)
    page.evaluate(
        "window.session={...window.session,token:'different-authorized-token'};window.editor.authority()"
    )
    assert page.locator("#recording-settings-dialog").is_hidden()
    assert page.evaluate("window.revokedUrls.length") == 1
    page.evaluate(
        "window.session.account.effective_permissions=['cameras.view_live'];window.editor.open(window.camera)"
    )
    assert page.locator("#recording-settings-dialog").is_hidden()


def test_source_change_clears_pixels_and_refuses_stale_save(motion_page):
    page = motion_page
    _capture(page)
    _draw(page, "include", [(0.1, 0.1), (0.9, 0.1), (0.9, 0.9)])
    page.evaluate(
        "window.editor.inventory([{...window.camera,source_revision:'inventory-only-new'}])"
    )
    assert page.locator("#motion-preview-frame").is_hidden()
    assert page.locator("#recording-settings-save").is_disabled()
    assert "connection changed" in page.locator("#recording-settings-message").inner_text()
    assert page.evaluate("window.saved") == []


def test_preview_url_and_malformed_image_fail_without_trusting_browser_pixels(motion_page):
    page = motion_page
    page.evaluate("window.previewUrl='https://unrelated.test/private.jpg'")
    page.locator("#motion-preview-capture").click()
    page.wait_for_function(
        "document.querySelector('#recording-settings-form').getAttribute('aria-busy')==='false'"
    )
    assert page.locator("#motion-preview-frame").is_hidden()
    assert (
        page.evaluate("window.requests.filter(r=>r.path.includes('/motion-preview/')).length") == 0
    )
    page.evaluate(
        "window.previewUrl=null;window.previewBlob=new Blob(['not a jpeg'],{type:'image/jpeg'})"
    )
    page.locator("#motion-preview-capture").click()
    page.wait_for_function(
        "document.querySelector('#recording-settings-form').getAttribute('aria-busy')==='false'"
    )
    assert page.locator("#motion-preview-frame").is_hidden()
    assert page.evaluate("window.createdUrls.length") == 0


def test_late_camera_read_cannot_replace_a_new_camera_policy(motion_page):
    page = motion_page
    page.evaluate("window.holdRead=true")
    page.locator("#recording-settings-refresh").click()
    page.wait_for_function("window.pendingReads.length===1")
    page.evaluate("window.holdRead=false;window.editor.open(window.otherCamera)")
    page.wait_for_function("document.querySelector('#recording-mode').value==='motion'")
    page.evaluate("window.pendingReads[0].resolve(window.pendingReads[0].value)")
    page.wait_for_timeout(100)
    assert page.locator("#recording-settings-target").inner_text() == "Other motion camera"
    assert page.locator("#recording-mode").input_value() == "motion"


def test_conflicted_or_unknown_save_outcome_requires_refresh(motion_page):
    page = motion_page
    page.locator("#recording-mode").select_option("motion")
    page.evaluate("window.failSave={status:409,code:'motion_policy_conflict'}")
    page.locator("#recording-settings-save").click()
    page.wait_for_function(
        "document.querySelector('#recording-settings-message').textContent"
        ".includes('changed elsewhere')"
    )
    assert page.locator("#recording-settings-save").is_disabled()
    assert page.locator("#recording-settings-refresh").is_enabled()
    assert page.evaluate("window.saved") == []


def test_wrong_aspect_server_preview_is_rejected_before_display(motion_page):
    page = motion_page
    page.evaluate(
        """async () => {
          const canvas=document.createElement('canvas');canvas.width=640;canvas.height=640;
          window.previewBlob=await new Promise(resolve=>canvas.toBlob(resolve,'image/jpeg'));
        }"""
    )
    page.locator("#motion-preview-capture").click()
    page.wait_for_function(
        "document.querySelector('#recording-settings-form').getAttribute('aria-busy')==='false'"
    )
    assert page.locator("#motion-preview-frame").is_hidden()
    assert page.evaluate("window.createdUrls.length") == 0


def test_changed_native_geometry_requires_clearing_old_zones_before_drawing(motion_page):
    page = motion_page
    _capture(page)
    _draw(page, "include", [(0.1, 0.1), (0.9, 0.1), (0.9, 0.9)])
    page.locator("#recording-settings-save").click()
    page.wait_for_function("window.saved.length===1")
    page.evaluate(
        """async () => {
          window.previewWidth=1080;window.previewHeight=1920;
          const canvas=document.createElement('canvas');canvas.width=360;canvas.height=640;
          window.previewBlob=await new Promise(resolve=>canvas.toBlob(resolve,'image/jpeg'));
        }"""
    )
    _capture(page)
    assert page.locator("#motion-zone-include").is_disabled()
    assert "view changed" in page.locator("#motion-preview-help").inner_text()
    page.locator("#motion-zone-clear").click()
    assert page.locator("#motion-zone-include").is_enabled()
    assert page.locator("#recording-settings-save").is_enabled()


def test_stale_whole_frame_policy_can_be_explicitly_reviewed_on_new_capture(motion_page):
    page = motion_page
    page.evaluate(
        """() => {
          const policy=window.policies[window.camera.device_id];
          policy.status='zones_need_review';policy.policy_source_revision='b'.repeat(64);
        }"""
    )
    page.locator("#recording-settings-refresh").click()
    page.wait_for_function(
        "document.querySelector('#recording-settings-status').textContent==='Zones need review'"
    )
    _capture(page)
    assert page.locator("#motion-zone-clear").inner_text() == "Use whole frame"
    page.locator("#motion-zone-clear").click()
    page.locator("#recording-settings-save").click()
    page.wait_for_function("window.saved.length===1")
    saved = page.evaluate("window.saved[0]")
    assert saved["zones"]["reference"]["source_revision"] == "a" * 64
    assert saved["zones"]["include"] == saved["zones"]["exclude"] == []
    assert saved["preview_id"] == "generated_preview_01234567890123456789"


@pytest.fixture
def full_workspace():
    path = Path(__file__).with_name("test_operator_camera_video_ui.py")
    spec = importlib.util.spec_from_file_location("motion_video_workspace", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.workspace.__wrapped__()


def _policy_route(page):
    policy = {
        "camera_id": _CAMERA,
        "revision": 0,
        "source_revision": "a" * 64,
        "policy_source_revision": "a" * 64,
        "mode": "continuous",
        "post_capture_seconds": 10,
        "sensitivity": 50,
        "minimum_area_percent": 0.5,
        "pre_capture_seconds": 0,
        "zones": {"reference": None, "include": [], "exclude": []},
        "status": "continuous",
    }

    def respond(route):
        if route.request.method == "PUT":
            policy.update(route.request.post_data_json)
            policy.update(revision=1, status="armed")
            policy.pop("expected_revision", None)
        route.fulfill(content_type="application/json", body=json.dumps(policy))

    page.context.route("**/recording-policy", respond)


def test_main_workspace_opens_recording_controls_for_recording_only_administrator(full_workspace):
    with full_workspace(permissions=["recording.manage"]) as (page, state):
        _policy_route(page)
        page.locator(".camera-item").first.click()
        page.locator("#recording-settings-dialog").wait_for(state="visible")
        page.wait_for_function(
            "document.querySelector('#recording-settings-form').getAttribute('aria-busy')==='false'"
        )
        assert page.locator("#motion-preview-capture").is_disabled()
        page.locator("#recording-mode").select_option("motion")
        page.locator("#recording-settings-save").click()
        page.locator("#recording-settings-status").get_by_text("Armed").wait_for()
        assert not any("/video-settings" in path for _, path, _ in state["calls"])


def test_main_workspace_armed_badge_and_action_use_actual_motion_state(full_workspace):
    with full_workspace() as (page, state):
        _policy_route(page)
        state["cameras"][0]["motion_policy_state"] = "armed"
        page.locator(".camera-item").first.click()
        page.locator("#recording-badge").get_by_text("Armed").wait_for()
        page.locator("#camera-recording-settings-button").click()
        page.locator("#recording-settings-dialog").wait_for(state="visible")
        assert "Motion-triggered" in page.locator("#recording-mode").inner_text()
