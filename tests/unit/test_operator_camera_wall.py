"""Browser ownership checks for the wall; native acceptance proves actual video.

These tests substitute the player transport to exercise stale async completion,
per-camera failures and teardown deterministically, without camera addresses.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("K5_OPERATOR_BROWSER_ACCEPTANCE") != "1",
    reason="Camera-wall behavior requires explicit browser acceptance",
)
_ASSET = Path(__file__).resolve().parents[2] / "src/k5vision/operator_web/wall.js"


@pytest.fixture
def wall_page():
    browser_api = pytest.importorskip("playwright.sync_api")
    channel = os.getenv("K5_OPERATOR_BROWSER_CHANNEL", "msedge" if os.name == "nt" else "chromium")
    with browser_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=channel, headless=True)
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(5000)
        errors = []
        page.on("pageerror", lambda _error: errors.append("uncaught_wall_error"))
        page.set_content('<main id="wall-panel"></main>')
        page.add_script_tag(
            content="""
            window.fixture = {
              session: {token: 'generated-browser-session', epoch: 1},
              calls: [], players: [], pending: [], opened: [], hold: false, failAdmission: {},
            };
            HTMLMediaElement.prototype.play = function () {
              this.dispatchEvent(new Event('playing'));
              return Promise.resolve();
            };
            HTMLMediaElement.prototype.pause = function () {};
            HTMLMediaElement.prototype.load = function () {};
            class TestHls {
              static Events = {
                ERROR: 'error', MEDIA_ATTACHED: 'attached', MANIFEST_PARSED: 'manifest',
              };
              static isSupported() { return true; }
              constructor() {
                this.events = {}; this.destroyed = false; fixture.players.push(this);
              }
              on(event, callback) { this.events[event] = callback; }
              emit(event, data) { this.events[event]?.(event, data); }
              attachMedia(video) { this.video = video; this.emit('attached'); }
              loadSource(url) { this.url = url; this.emit('manifest'); }
              destroy() { this.destroyed = true; }
            }
            window.Hls = TestHls;
            window.fixtureOptions = {
              container: document.getElementById('wall-panel'),
              request: (path, options) => {
                const id = path.split('/').at(-2);
                fixture.calls.push({id, signal: options.signal});
                if (fixture.failAdmission[id]) {
                  const error = new Error('Generated admission failure');
                  error.status = fixture.failAdmission[id];
                  return Promise.reject(error);
                }
                const result = {live_url: '/media/live/' + id + '/index.m3u8'};
                if (fixture.hold) {
                  return new Promise(resolve => fixture.pending.push(() => resolve(result)));
                }
                return Promise.resolve(result);
              },
              ensureHls: () => Promise.resolve(),
              safeMediaUrl: (value, prefix) => {
                if (typeof value !== 'string' || !value.startsWith(prefix)) {
                  throw Error('Invalid media');
                }
                return value;
              },
              getSession: () => fixture.session,
              onOpenCamera: (id, view) => fixture.opened.push({id, view}),
            };
            window.cameras = count => Array.from({length: count}, (_, index) => ({
              device_id: 'camera-' + index, name: 'Camera ' + index, status: 'live',
              recording_enabled: true, recording_state: 'waiting',
            }));
            """
        )
        page.add_script_tag(path=str(_ASSET))
        page.evaluate("window.wall = K5CameraWall.create(fixtureOptions)")
        try:
            yield page
        finally:
            page.evaluate("wall.destroy()")
            context.close()
            browser.close()
            assert not errors


def test_wall_layout_paging_reconciles_players_and_admits_only_confirmed_recording(wall_page):
    page = wall_page
    page.evaluate("wall.show(cameras(14))")
    page.wait_for_function("fixture.players.length === 4")
    assert page.locator(".camera-wall-tile").count() == 4
    assert (
        page.locator(".camera-wall-recording").all_text_contents() == ["Waiting for recording"] * 4
    )
    page.evaluate(
        """const updated = cameras(14);
        updated[0].recording_state = 'recording';
        updated[1].recording_state = 'stalled';
        updated[2].name = '<img src=x onerror=alert(1)>';
        wall.update(updated);"""
    )
    assert page.locator(".camera-wall-recording").all_text_contents()[:2] == [
        "Recording",
        "Recording stalled",
    ]
    assert page.locator(".camera-wall-name").nth(2).inner_text().startswith("<img")
    assert page.locator(".camera-wall-tile img").count() == 0
    assert page.evaluate("fixture.players.length") == 4

    page.locator("#wall-layout").select_option("9")
    page.wait_for_function("fixture.players.length === 9")
    assert page.locator(".camera-wall-tile").count() == 9
    assert page.evaluate("fixture.players.filter(player => !player.destroyed).length") == 9
    page.locator("#wall-page-next").click()
    page.wait_for_function("fixture.players.length === 14")
    assert page.locator(".camera-wall-tile").count() == 5
    assert page.evaluate("fixture.players.filter(player => !player.destroyed).length") == 5
    assert page.evaluate("fixture.calls.slice(0, 9).every(call => call.signal.aborted)")
    page.locator('[data-camera-id="camera-9"] [data-wall-action="watch"]').click()
    page.locator('[data-camera-id="camera-9"] [data-wall-action="recordings"]').click()
    assert page.evaluate("fixture.opened") == [
        {"id": "camera-9", "view": "live"},
        {"id": "camera-9", "view": "history"},
    ]
    page.evaluate("wall.hide()")
    assert page.locator("#wall-panel").is_hidden()
    assert page.locator(".camera-wall-tile").count() == 0
    assert page.evaluate("fixture.players.every(player => player.destroyed)")
    assert page.evaluate("fixture.calls.every(call => call.signal.aborted)")


def test_media_cookie_refresh_is_once_per_tile_generation_and_other_player_survives(wall_page):
    page = wall_page
    page.evaluate("wall.show(cameras(2))")
    page.wait_for_function("fixture.players.length === 2")
    page.evaluate("fixture.players[0].emit('error', {fatal: true, response: {code: 401}})")
    page.wait_for_function("fixture.players.length === 3")
    assert page.evaluate("fixture.calls.map(call => call.id)") == [
        "camera-0",
        "camera-1",
        "camera-0",
    ]
    assert page.evaluate("!fixture.players[1].destroyed")
    page.evaluate("fixture.players[2].emit('error', {fatal: true, response: {code: 403}})")
    assert page.evaluate("fixture.calls.length") == 3
    assert page.locator('[data-camera-id="camera-0"] .camera-wall-retry').is_visible()
    assert page.locator('[data-camera-id="camera-1"] .camera-wall-status').inner_text() == "Live"
    page.locator('[data-camera-id="camera-0"] .camera-wall-retry').click()
    page.wait_for_function("fixture.players.length === 4")
    assert page.evaluate("fixture.calls.length") == 4
    assert page.evaluate("!fixture.players[1].destroyed")
    # A destroyed transport may still deliver a callback; it cannot refresh again.
    page.evaluate("fixture.players[0].emit('error', {fatal: true, response: {code: 401}})")
    assert page.evaluate("fixture.calls.length") == 4


def test_late_admission_after_hide_or_replaced_session_never_starts_old_players(wall_page):
    page = wall_page
    page.evaluate("fixture.hold = true; wall.show(cameras(2))")
    assert page.evaluate("fixture.pending.length") == 2
    page.evaluate("wall.hide(); fixture.pending.splice(0).forEach(resolve => resolve())")
    assert page.evaluate("fixture.players.length") == 0
    assert page.evaluate("fixture.calls.every(call => call.signal.aborted)")
    page.evaluate("wall.show(cameras(1))")
    assert page.evaluate("fixture.pending.length") == 1
    page.evaluate(
        """fixture.session = {token: 'replacement-generated-session', epoch: 2};
        fixture.hold = false;
        wall.update(cameras(1));
        fixture.pending.splice(0).forEach(resolve => resolve());"""
    )
    page.wait_for_function("fixture.players.length === 1")
    assert page.evaluate("fixture.calls.slice(0, 3).every(call => call.signal.aborted)")
    page.evaluate("fixture.session = {token: '', epoch: 3}; wall.update(cameras(1))")
    assert page.locator("#wall-panel").is_hidden()
    assert page.evaluate("fixture.players.every(player => player.destroyed)")


def test_transport_outage_retries_only_its_player_and_reuses_admitted_media(wall_page):
    page = wall_page
    page.evaluate("wall.show(cameras(2))")
    page.wait_for_function("fixture.players.length === 2")
    page.evaluate("fixture.players[0].emit('error', {fatal: true, details: 'networkError'})")
    assert page.evaluate("fixture.players[0].destroyed && !fixture.players[1].destroyed")
    page.wait_for_function("fixture.players.length === 3")
    assert page.evaluate("fixture.calls.length") == 2
    assert page.locator('[data-camera-id="camera-0"] .camera-wall-status').inner_text() == "Live"
    assert page.evaluate("!fixture.players[1].destroyed")


@pytest.mark.parametrize("previous", ["unavailable", "disconnected", "connecting"])
def test_initial_admission_503_recovers_once_when_camera_becomes_live(wall_page, previous):
    page = wall_page
    page.evaluate(
        """previous => {
          fixture.failAdmission['camera-0'] = 503;
          const values = cameras(2); values[0].status = previous;
          wall.show(values);
        }""",
        previous,
    )
    page.wait_for_function("fixture.players.length === 1")
    assert page.locator('[data-camera-id="camera-0"] .camera-wall-retry').is_visible()
    page.evaluate("fixture.failAdmission = {}; wall.update(cameras(2))")
    page.wait_for_function("fixture.players.length === 2")
    assert page.locator('[data-camera-id="camera-0"] .camera-wall-status').inner_text() == "Live"
    assert page.evaluate("fixture.calls.length") == 3
    assert page.evaluate("!fixture.players[0].destroyed")
    page.evaluate("wall.update(cameras(2)); wall.update(cameras(2))")
    assert page.evaluate("fixture.calls.length") == 3


@pytest.mark.parametrize("status,expected_calls", [(503, 2), (401, 1), (403, 1)])
def test_status_flapping_cannot_repeat_admission_or_retry_authoritative_denial(
    wall_page, status, expected_calls
):
    page = wall_page
    page.evaluate(
        """status => {
          fixture.failAdmission['camera-0'] = status;
          const values = cameras(1); values[0].status = 'unavailable';
          wall.show(values);
        }""",
        status,
    )
    page.locator(".camera-wall-retry").wait_for(state="visible")
    page.evaluate("wall.update(cameras(1))")
    page.locator(".camera-wall-retry").wait_for(state="visible")
    page.evaluate(
        """const unavailable = cameras(1); unavailable[0].status = 'unavailable';
        wall.update(unavailable); wall.update(cameras(1));"""
    )
    assert page.evaluate("fixture.calls.length") == expected_calls
    assert page.evaluate("fixture.players.length") == 0


def test_unchanged_camera_poll_keeps_actual_fullscreen_tile_mounted(wall_page):
    page = wall_page
    page.evaluate("wall.show(cameras(4))")
    page.wait_for_function("fixture.players.length === 4")
    tile = page.locator('[data-camera-id="camera-0"]')
    tile.locator('[data-wall-action="fullscreen"]').click()
    page.wait_for_function(
        "document.fullscreenElement === document.querySelector('[data-camera-id=\"camera-0\"]')"
    )
    result = page.evaluate("""async () => {
      const original = document.fullscreenElement;
      let moved = false;
      const observer = new MutationObserver(records => {
        if (records.some(record => [...record.removedNodes].includes(original))) moved = true;
      });
      observer.observe(original.parentNode, {childList: true});
      const updated = cameras(4); updated[0].name = 'Updated camera name';
      wall.update(updated);
      await new Promise(requestAnimationFrame);
      await new Promise(requestAnimationFrame);
      observer.disconnect();
      return {moved, fullscreen: document.fullscreenElement === original,
        connected: original.isConnected};
    }""")
    assert result == {"moved": False, "fullscreen": True, "connected": True}
    assert tile.locator(".camera-wall-name").inner_text() == "Updated camera name"
    assert page.evaluate("fixture.players.length") == 4
