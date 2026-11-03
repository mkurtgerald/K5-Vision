"""Browser behavior for UTC recording availability, scrubbing and keyboard access."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("K5_OPERATOR_BROWSER_ACCEPTANCE") != "1",
    reason="Timeline interaction requires the explicit browser acceptance environment",
)
_ASSETS = Path(__file__).resolve().parents[2] / "src/k5vision/operator_web"
_START = int(datetime(2026, 11, 1, 5, 30, tzinfo=UTC).timestamp() * 1000)


@pytest.fixture
def timeline_page(request):
    browser_api = pytest.importorskip("playwright.sync_api")
    channel = os.getenv("K5_OPERATOR_BROWSER_CHANNEL", "msedge" if os.name == "nt" else "chromium")
    timezone = getattr(request, "param", "UTC")
    with browser_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=channel, headless=True)
        context = browser.new_context(viewport={"width": 1100, "height": 600}, timezone_id=timezone)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda _error: errors.append("uncaught_script_error"))
        page.set_content('<main><div id="timeline" style="width:1000px"></div></main>')
        # A browser clock assumption must not move supplied UTC recordings.
        page.evaluate("""() => {
          const NativeDate = Date;
          window.Date = class extends NativeDate {
            constructor(...args) {
              super(...(args.length ? args : [NativeDate.now() + 172800000]));
            }
            static now() { return NativeDate.now() + 172800000; }
          };
        }""")
        page.add_style_tag(path=str(_ASSETS / "timeline.css"))
        page.add_script_tag(path=str(_ASSETS / "timeline.js"))
        page.evaluate("""() => {
          window.seeks = [];
          window.timeline = window.K5RecordingTimeline.create({
            container: document.getElementById('timeline'),
            onSeek: value => window.seeks.push(value)
          });
        }""")
        try:
            yield page
            assert not errors
        finally:
            context.close()
            browser.close()


def _set_data(page, *, seconds=60, spans=None):
    page.evaluate(
        "data => window.timeline.setData(data)",
        {
            "start": _START,
            "end": _START + seconds * 1000,
            "spans": spans if spans is not None else [{"start": _START, "end": _START + 10000}],
        },
    )


@pytest.mark.parametrize("timeline_page", ["America/New_York", "Pacific/Auckland"], indirect=True)
def test_timeline_keeps_actual_gaps_and_utc_across_browser_clock_and_dst(timeline_page):
    page = timeline_page
    minute = 60000
    _set_data(
        page,
        seconds=3600,
        spans=[
            {"start": _START + 20 * minute, "end": _START + 30 * minute},
            {"start": _START - 5 * minute, "end": _START + 10 * minute},
            {"start": _START + 25 * minute, "end": _START + 40 * minute},
            {"start": _START + 40 * minute + 250, "end": _START + 50 * minute},
            {"start": _START + 55 * minute, "end": _START + 54 * minute},
            {"start": "private-invalid-value", "end": _START + 60 * minute},
        ],
    )
    track = page.get_by_role("slider", name="Recording time in UTC")
    assert track.get_attribute("aria-valuemin") == str(_START)
    assert track.get_attribute("aria-valuemax") == str(_START + 3600000)
    assert page.locator("#timeline").get_attribute("data-window-start") == str(_START)
    assert page.locator(".k5-timeline-recording").count() == 4
    gaps = page.locator(".k5-timeline-gap").evaluate_all(
        "nodes => nodes.map(node => [Number(node.dataset.start), Number(node.dataset.end)])"
    )
    assert gaps == [
        [_START + 10 * minute, _START + 20 * minute],
        [_START + 40 * minute, _START + 40 * minute + 250],
        [_START + 50 * minute, _START + 60 * minute],
    ]
    assert "05:30:00 UTC" in page.locator(".k5-timeline-tick.first").inner_text()
    assert "06:30:00 UTC" in page.locator(".k5-timeline-tick.last").inner_text()
    assert "private-invalid-value" not in page.locator("#timeline").inner_text()
    page.evaluate("value => window.timeline.setPlayhead(value)", _START + 5 * minute)
    assert page.evaluate("window.seeks") == []
    bounds = track.bounding_box()
    point = {"x": bounds["x"] + bounds["width"] / 4, "y": bounds["y"] + 24}
    page.mouse.move(**point)
    assert "No recording" in page.locator(".k5-timeline-time").inner_text()
    assert page.evaluate("window.seeks") == []
    page.mouse.click(**point)
    seeks = page.evaluate("window.seeks")
    assert len(seeks) == 1
    assert _START + 10 * minute < seeks[0] < _START + 20 * minute
    assert "No recording" in track.get_attribute("aria-valuetext")
    page.locator("#timeline").evaluate("node => { node.style.width = '250px'; }")
    page.wait_for_function("document.querySelectorAll('.k5-timeline-tick').length === 2")
    assert "05:30:00 UTC" in page.locator(".k5-timeline-tick.first").inner_text()
    assert "06:30:00 UTC" in page.locator(".k5-timeline-tick.last").inner_text()


def test_drag_previews_then_commits_once_and_cancel_does_not_seek(timeline_page):
    page = timeline_page
    _set_data(page)
    track = page.get_by_role("slider")
    bounds = track.bounding_box()
    left, middle = bounds["x"], bounds["y"] + 24
    page.mouse.move(left + bounds["width"] / 12, middle)
    page.mouse.down()
    page.mouse.move(left + bounds["width"] / 2, middle, steps=5)
    page.mouse.move(left + bounds["width"] + 50, middle, steps=3)
    assert page.evaluate("window.seeks") == []
    assert track.get_attribute("aria-valuenow") == str(_START + 60000)
    page.mouse.up()
    assert page.evaluate("window.seeks") == [_START + 60000]
    track.evaluate("""node => node.addEventListener('pointerdown',
      event => { window.cancelPointer = event.pointerId; }, {once:true})""")
    page.mouse.move(left + bounds["width"] / 2, middle)
    page.mouse.down()
    track.evaluate("""node => node.dispatchEvent(new PointerEvent('pointercancel', {
      pointerId: window.cancelPointer
    }))""")
    page.mouse.up()
    assert page.evaluate("window.seeks") == [_START + 60000]
    assert track.get_attribute("aria-valuenow") == str(_START + 60000)


def test_keyboard_seeks_are_accessible_and_stay_in_the_selected_window(timeline_page):
    page = timeline_page
    _set_data(page)
    page.evaluate("value => window.timeline.setPlayhead(value)", _START + 30000)
    track = page.get_by_role("slider")
    track.focus()
    for key in ["ArrowRight", "Shift+ArrowLeft", "Home", "ArrowLeft", "End", "ArrowRight"]:
        track.press(key)
    assert page.evaluate("window.seeks") == [
        _START + 31000,
        _START + 21000,
        _START,
        _START,
        _START + 60000,
        _START + 60000,
    ]
    assert "2026-11-01 05:31:00.000 UTC" in track.get_attribute("aria-valuetext")
    assert track.get_attribute("aria-describedby")
    assert track.get_attribute("aria-disabled") == "false"


def test_empty_clear_invalid_data_and_destroy_never_invent_availability(timeline_page):
    page = timeline_page
    _set_data(page, spans=[])
    track = page.get_by_role("slider")
    assert page.locator(".k5-timeline-recording").count() == 0
    assert page.locator(".k5-timeline-gap").count() == 1
    assert page.locator(".k5-timeline-summary").inner_text() == "No recorded video in this window"
    page.evaluate("window.timeline.clear()")
    assert track.get_attribute("aria-disabled") == "true"
    assert track.get_attribute("tabindex") == "-1"
    assert track.get_attribute("aria-valuenow") is None
    assert page.locator("#timeline").get_attribute("data-window-start") is None
    track.dispatch_event("keydown", {"key": "Home"})
    track.click(force=True)
    assert page.evaluate("window.seeks") == []
    _set_data(page)
    page.evaluate("window.timeline.setData({start:Infinity,end:Infinity,spans:[]})")
    assert track.get_attribute("aria-disabled") == "true"
    _set_data(page)
    page.evaluate("""() => {
      window.oldTrack = document.querySelector('[role=slider]');
      window.timeline.destroy();
      window.timeline.destroy();
      window.timeline.setData({start:0,end:1000,spans:[]});
      window.oldTrack.dispatchEvent(new KeyboardEvent('keydown',{key:'Home'}));
    }""")
    assert page.locator("#timeline").inner_html() == ""
    assert page.evaluate("window.seeks") == []
