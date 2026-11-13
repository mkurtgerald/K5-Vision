"""Actual browser interactions for persistent person-event controls and navigation."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

_ASSETS = Path(__file__).resolve().parents[2] / "src/k5vision/operator_web"
_CAMERA = "e4fd401d-ae8e-4ac2-9f92-e8287f241532"
_OTHER = "df5bb85e-363f-4b8c-bc40-00f278b8bd24"


def test_analytics_assets_keep_session_authority_and_utc_outside_browser_storage():
    script = (_ASSETS / "analytics.js").read_text(encoding="utf-8")
    assert "localStorage" not in script and "sessionStorage" not in script
    assert "innerHTML" not in script
    assert "Date.now" not in script
    assert "window.K5Analytics" in script
    assert (_ASSETS / "analytics.css").is_file()
    html = (_ASSETS / "index.html").read_text(encoding="utf-8")
    assert "/operator/analytics.js" in html and "/operator/analytics.css" in html
    assert 'id="analytics-panel"' in html


@pytest.fixture
def analytics_page():
    if os.getenv("K5_OPERATOR_BROWSER_ACCEPTANCE") != "1":
        pytest.skip("Person-events UI proof requires the explicit browser environment")
    browser_api = pytest.importorskip("playwright.sync_api")
    channel = os.getenv("K5_OPERATOR_BROWSER_CHANNEL", "msedge" if os.name == "nt" else "chromium")
    errors = []
    with browser_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=channel, headless=True)
        page = browser.new_page(viewport={"width": 1200, "height": 900})
        page.set_default_timeout(5000)
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.set_content('<main><section id="analytics"></section></main>')
        page.add_style_tag(path=str(_ASSETS / "analytics.css"))
        page.add_script_tag(path=str(_ASSETS / "analytics.js"))
        page.evaluate(
            """({cameraId, otherId}) => {
              window.session = {token:'generated-in-memory-session',epoch:1,
                account:{role:'administrator'}};
              window.settings = {enabled:false,confidence_threshold:0.6,status:'disabled',
                last_sample_at:null,last_event_at:null,processed_frames:0,event_count:0,
                sample_interval_seconds:2,max_parallel_cameras:2};
              window.eventRows = [{id:12,camera_id:cameraId,occurred_at:'2026-10-27T12:30:00Z',
                playback_at:'2026-10-27T12:29:58Z',confidence:0.87,person_count:2}];
              window.requests = []; window.seeks = []; window.waiters = [];
              window.providerRows = []; window.providerMore = false;
              window.hold = false; window.failEvents = false; window.more = false;
              window.request = async (path,options={}) => {
                window.requests.push({path,method:options.method||'GET',body:options.body});
                if (window.hold) return await new Promise(resolve =>
                  window.waiters.push({path,resolve}));
                if (path.includes('/integration-events')) return {
                  events:structuredClone(window.providerRows),has_more:window.providerMore,
                  next_before:window.providerMore?34:null};
                if (path.endsWith('/analytics')) {
                  if (options.method === 'PATCH') {
                    Object.assign(window.settings,options.body,
                      {status:options.body.enabled ? 'starting':'disabled'});
                  }
                  return structuredClone(window.settings);
                }
                if (window.failEvents) throw Object.assign(
                  new Error('private diagnostics'),{status:503});
                if (new URL(path,'http://localhost').searchParams.has('before')) return {
                  events:[{...window.eventRows[0],id:11,occurred_at:'2026-10-27T12:10:00Z'}],
                  has_more:false,next_before:null};
                return {events:structuredClone(window.eventRows),has_more:window.more,
                  next_before:window.more?12:null};
              };
              window.analytics = window.K5Analytics.create({
                container:document.getElementById('analytics'),request:window.request,
                getSession:()=>window.session,onSeek:value=>window.seeks.push(value)});
              window.analytics.selectCamera({device_id:cameraId,name:'Generated entrance camera'});
              window.analytics.setRange({start:'2026-10-27T00:00:00Z',end:'2026-10-28T00:00:00Z'});
              window.analytics.setVisible(true);
            }""",
            {"cameraId": _CAMERA, "otherId": _OTHER},
        )
        page.locator('#analytics-events li[data-event-id="12"]').wait_for()
        try:
            yield page, errors
        finally:
            page.evaluate("window.analytics.destroy()")
            browser.close()


def test_admin_enables_background_detection_and_events_open_exact_recorded_utc(analytics_page):
    page, errors = analytics_page
    assert page.locator("#analytics-status").inner_text() == "Detection off"
    assert "Keep recording enabled" in page.locator("#analytics-settings-help").inner_text()
    assert page.locator("#analytics-save").is_disabled()
    page.locator("#analytics-enabled").check()
    page.locator("#analytics-confidence").fill("82")
    page.locator("#analytics-save").click()
    page.locator("#analytics-message").filter(has_text="settings saved").wait_for()
    patches = page.evaluate("window.requests.filter(value => value.method==='PATCH')")
    assert len(patches) == 1
    assert patches[0]["body"] == {"enabled": True, "confidence_threshold": 0.82}
    assert page.locator("#analytics-status").inner_text() == "Starting detection"
    page.locator('#analytics-events button[data-event-id="12"]').click()
    page.wait_for_function("window.seeks.length === 1")
    assert page.evaluate("window.seeks") == [
        {"cameraId": _CAMERA, "timestamp": "2026-10-27T12:29:58Z"}
    ]
    assert "2026-10-27 12:30:00 UTC" in page.locator("#analytics-events").inner_text()
    assert "87% confidence" in page.locator("#analytics-events").inner_text()
    paths = page.evaluate("window.requests.map(value => value.path)")
    assert any(
        "start=2026-10-27T00%3A00%3A00.000Z&end=2026-10-28T00%3A00%3A00.000Z" in path
        for path in paths
    )
    assert not errors


def test_unchanged_polled_event_keeps_its_click_target_until_event_data_changes(analytics_page):
    page, errors = analytics_page
    page.evaluate(
        """() => {
          window.originalPersonButton = document.querySelector(
            '#analytics-events button[data-event-id="12"]');
        }"""
    )
    page.evaluate("() => window.analytics.refresh()")
    assert page.evaluate(
        """() => document.querySelector(
          '#analytics-events button[data-event-id="12"]') === window.originalPersonButton"""
    )
    page.locator('#analytics-events button[data-event-id="12"]').click()
    page.wait_for_function("window.seeks.length === 1")
    assert page.evaluate("window.seeks[0].timestamp") == "2026-10-27T12:29:58Z"

    page.evaluate("() => {window.eventRows[0].confidence=0.91; return window.analytics.refresh();}")
    assert "91% confidence" in page.locator("#analytics-events").inner_text()
    assert not page.evaluate(
        """() => document.querySelector(
          '#analytics-events button[data-event-id="12"]') === window.originalPersonButton"""
    )
    assert not errors


def test_viewer_sees_events_without_settings_and_older_events_remain_camera_scoped(analytics_page):
    page, errors = analytics_page
    page.evaluate(
        """() => {window.session.account.role='viewer';window.more=true;
          return window.analytics.refresh();}"""
    )
    assert page.locator("#analytics-settings-form").is_hidden()
    page.locator("#analytics-settings-form").dispatch_event("submit")
    page.locator("#analytics-older").click()
    page.locator('#analytics-events li[data-event-id="11"]').wait_for()
    assert page.locator("#analytics-events li").count() == 2
    assert page.locator("#analytics-older").is_hidden()
    assert page.evaluate("window.requests.filter(value => value.method==='PATCH').length") == 0
    assert not errors


def test_camera_switch_and_logout_discard_delayed_detection_responses(analytics_page):
    page, errors = analytics_page
    page.evaluate("() => {window.hold=true;void window.analytics.refresh();}")
    page.wait_for_function("window.waiters.length===3")
    page.evaluate(
        """other => {
          window.analytics.selectCamera({device_id:other,name:'Different camera'});
          window.analytics.clear();window.session=null;
          for(const waiter of window.waiters) waiter.resolve(waiter.path.endsWith('/analytics')
            ? {...window.settings,enabled:true,status:'active'}
            : {events:window.eventRows,has_more:false,next_before:null});
        }""",
        _OTHER,
    )
    page.wait_for_timeout(100)
    assert page.locator("#analytics").is_hidden()
    assert page.locator("#analytics-events li").count() == 0
    assert page.locator("#analytics-camera-name").inner_text() == ""
    assert page.evaluate("window.seeks.length") == 0
    assert not errors


def test_invalid_or_cross_camera_events_are_cleared_without_exposing_server_diagnostics(
    analytics_page,
):
    page, errors = analytics_page
    page.evaluate(
        """other => {
          window.eventRows[0].camera_id=other;
          return window.analytics.refresh();
        }""",
        _OTHER,
    )
    assert page.locator("#analytics-events li").count() == 0
    assert "could not be loaded" in page.locator("#analytics-events-message").inner_text()
    page.evaluate("() => {window.failEvents=true;return window.analytics.refresh();}")
    assert "private diagnostics" not in page.locator("#analytics").inner_text()
    assert "could not be loaded" in page.locator("#analytics-events-message").inner_text()
    assert not errors


def test_missing_recorder_range_never_invents_a_clock_based_event_query(analytics_page):
    page, errors = analytics_page
    page.evaluate("""() => {window.analytics.setRange(null);window.requests=[];
      return window.analytics.refresh();}""")
    assert page.locator("#analytics-events li").count() == 0
    assert "Choose a recording time range" in page.locator("#analytics-events-message").inner_text()
    assert not any("/events?" in row["path"] for row in page.evaluate("window.requests"))
    assert not errors


def test_status_refresh_preserves_unsaved_settings_and_reports_unavailable_detector(analytics_page):
    page, errors = analytics_page
    page.locator("#analytics-confidence").fill("77")
    page.evaluate(
        """() => {window.settings.status='unavailable';
          return window.analytics.refresh();}"""
    )
    assert page.locator("#analytics-confidence").input_value() == "77"
    assert page.locator("#analytics-save").is_enabled()
    assert page.locator("#analytics-status").inner_text() == "Detector unavailable"
    assert "Recording continues independently" in page.locator("#analytics-message").inner_text()
    assert not errors


def test_event_navigation_discards_an_account_change_before_the_click_callback(analytics_page):
    page, errors = analytics_page
    page.evaluate(
        """() => {
          document.querySelector('#analytics-events button').click();
          window.session.account.role='viewer';
        }"""
    )
    page.wait_for_timeout(50)
    assert page.evaluate("window.seeks") == []
    assert not errors


def test_provider_events_preserve_status_utc_and_assigned_camera_navigation(analytics_page):
    page, errors = analytics_page
    page.evaluate(
        """camera => {
          window.providerRows=[{sequence:34,source_id:camera,event_id:'external-event-34',
            event_type:'analytics.person',status:'candidate',confidence:0.76,
            observed_at:'2026-10-26T23:59:12Z',playback_at:'2026-10-26T23:59:12Z'}];
          return window.analytics.refresh();
        }""",
        _CAMERA,
    )
    row = page.locator('#analytics-provider-events li[data-provider-event-id="external-event-34"]')
    row.wait_for()
    assert "candidate" in row.inner_text()
    assert "76% confidence" in row.inner_text()
    assert "2026-10-26 23:59:12 UTC" in row.inner_text()
    row.get_by_role("button").click()
    assert page.evaluate("window.seeks") == [
        {"cameraId": _CAMERA, "timestamp": "2026-10-26T23:59:12Z"}
    ]
    assert not errors


def test_unchanged_provider_poll_preserves_focused_playback_button(analytics_page):
    page, errors = analytics_page
    page.evaluate(
        """camera => {
          window.providerRows=[{sequence:34,source_id:camera,event_id:'external-event-34',
            event_type:'analytics.person',status:'candidate',confidence:0.76,
            observed_at:'2026-10-26T23:59:12Z',playback_at:'2026-10-26T23:59:12Z'}];
          return window.analytics.refresh();
        }""",
        _CAMERA,
    )
    page.evaluate(
        """() => {
          window.originalProviderButton = document.querySelector(
            '#analytics-provider-events [data-provider-event-id="external-event-34"] button');
          window.originalProviderButton.focus();
        }"""
    )
    page.evaluate("() => window.analytics.refresh()")
    assert page.evaluate(
        """() => {
          const current = document.querySelector(
            '#analytics-provider-events [data-provider-event-id="external-event-34"] button');
          return current === window.originalProviderButton &&
            document.activeElement === window.originalProviderButton;
        }"""
    )
    page.locator(
        '#analytics-provider-events [data-provider-event-id="external-event-34"] button'
    ).click()
    page.wait_for_function("window.seeks.length === 1")
    assert page.evaluate("window.seeks[0]") == {
        "cameraId": _CAMERA,
        "timestamp": "2026-10-26T23:59:12Z",
    }

    page.evaluate(
        "() => {window.providerRows[0].status='confirmed'; return window.analytics.refresh();}"
    )
    assert "confirmed" in page.locator("#analytics-provider-events").inner_text()
    assert not page.evaluate(
        """() => document.querySelector(
          '#analytics-provider-events [data-provider-event-id="external-event-34"] button')
          === window.originalProviderButton"""
    )
    # A range transition changes request authority even when provider data
    # remains identical; the refreshed button must bind the new epoch.
    page.evaluate(
        """() => {
          window.previousProviderButton = document.querySelector(
            '#analytics-provider-events [data-provider-event-id="external-event-34"] button');
          window.analytics.setRange({
            start:'2026-10-27T01:00:00Z',end:'2026-10-28T01:00:00Z'});
        }"""
    )
    page.wait_for_function(
        """() => document.querySelector(
          '#analytics-provider-events [data-provider-event-id="external-event-34"] button')
          !== window.previousProviderButton"""
    )
    page.locator(
        '#analytics-provider-events [data-provider-event-id="external-event-34"] button'
    ).click()
    page.wait_for_function("window.seeks.length === 2")
    assert page.evaluate("window.seeks[1].timestamp") == "2026-10-26T23:59:12Z"
    assert not errors


def test_provider_event_seek_survives_range_change_while_refresh_is_pending(analytics_page):
    page, errors = analytics_page
    page.evaluate(
        """camera => {
          window.providerRows=[{sequence:34,source_id:camera,event_id:'external-event-34',
            event_type:'analytics.person',status:'candidate',confidence:0.76,
            observed_at:'2026-10-26T23:59:12Z',playback_at:'2026-10-26T23:59:12Z'}];
          return window.analytics.refresh();
        }""",
        _CAMERA,
    )
    # A range update invalidates the request epoch before the asynchronous
    # provider refresh completes. A still-visible event must remain actionable.
    page.evaluate(
        """() => {
          window.hold=true;
          window.analytics.setRange({
            start:'2026-10-27T01:00:00Z',end:'2026-10-28T01:00:00Z'});
        }"""
    )
    page.wait_for_function("window.waiters.length === 3")
    page.locator(
        '#analytics-provider-events [data-provider-event-id="external-event-34"] button'
    ).click()
    page.wait_for_function("window.seeks.length === 1")
    assert page.evaluate("window.seeks[0]") == {
        "cameraId": _CAMERA,
        "timestamp": "2026-10-26T23:59:12Z",
    }
    page.evaluate(
        """() => {
          window.hold=false;
          for(const waiter of window.waiters) {
            waiter.resolve(waiter.path.includes('/integration-events')
              ? {events:structuredClone(window.providerRows),has_more:false,next_before:null}
              : waiter.path.endsWith('/analytics') ? structuredClone(window.settings)
              : {events:structuredClone(window.eventRows),has_more:false,next_before:null});
          }
          window.waiters=[];
        }"""
    )
    assert not errors


def test_provider_event_seek_rebinds_after_visibility_change(analytics_page):
    page, errors = analytics_page
    page.evaluate(
        """camera => {
          window.providerRows=[{sequence:34,source_id:camera,event_id:'external-event-34',
            event_type:'analytics.person',status:'candidate',confidence:0.76,
            observed_at:'2026-10-26T23:59:12Z',playback_at:'2026-10-26T23:59:12Z'}];
          return window.analytics.refresh();
        }""",
        _CAMERA,
    )
    page.evaluate(
        """() => {
          window.hold=true;
          window.analytics.setVisible(false);
          window.analytics.setVisible(true);
        }"""
    )
    page.wait_for_function("window.waiters.length === 3")
    page.locator(
        '#analytics-provider-events [data-provider-event-id="external-event-34"] button'
    ).click()
    page.wait_for_function("window.seeks.length === 1")
    assert page.evaluate("window.seeks[0]") == {
        "cameraId": _CAMERA,
        "timestamp": "2026-10-26T23:59:12Z",
    }
    page.evaluate(
        """() => {
          window.session=null;
          document.querySelector('#analytics-provider-events button').click();
        }"""
    )
    page.wait_for_timeout(50)
    assert page.evaluate("window.seeks.length") == 1
    assert not errors


def test_unassigned_provider_event_is_rejected_and_custom_role_cannot_change_detection(
    analytics_page,
):
    page, errors = analytics_page
    page.evaluate(
        """other => {
          window.providerRows=[{sequence:34,source_id:other,event_id:'wrong-camera',
            event_type:'analytics.person',status:'confirmed',confidence:0.9,
            observed_at:'2026-10-27T12:30:00Z',playback_at:'2026-10-27T12:30:00Z'}];
          window.session.account.effective_permissions=['cameras.playback'];
          return window.analytics.refresh();
        }""",
        _OTHER,
    )
    assert page.locator("#analytics-provider-events li").count() == 0
    assert "could not be loaded" in page.locator("#analytics-provider-message").inner_text()
    assert page.locator("#analytics-settings-form").is_hidden()
    page.locator("#analytics-settings-form").dispatch_event("submit")
    assert page.evaluate("window.requests.filter(x=>x.method==='PATCH').length") == 0
    assert not errors
