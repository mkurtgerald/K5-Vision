"""The separate clock-origin probe must never fabricate analytics acceptance."""

from scripts.diagnose_windows_clock_origin_58 import (
    resume_clock_diagnostic_after_navigation_timeout,
)


class _Client:
    def __init__(self, events):
        self.events = events
        self.routes = []

    def json(self, route):
        self.routes.append(route)
        return {"events": self.events}


class _Helpers:
    def __init__(self):
        self.stopped = []

    def _stop_owned(self, publisher):
        self.stopped.append(publisher)


class _Browser:
    def __init__(self):
        self.calls = []

    def _open_camera_watch(self, page, camera_id):
        self.calls.append(("open", page, camera_id))

    def _decoded_color(self, page, video, color):
        self.calls.append(("decode", page, video, color))


def _case(*, stage="event_recording_navigation", event=None):
    client = _Client([] if event is None else [event])
    helpers, browser = _Helpers(), _Browser()
    kwargs = {
        "diagnostic": {"analytics_stage": stage},
        "camera_id": "camera-1",
        "client": client,
        "helpers": helpers,
        "publishers": ["positive-publisher"],
        "processes": [],
        "parent": "generated",
        "ffmpeg": "generated-ffmpeg",
        "source_uri": "private-source",
        "publisher_credentials": "private-credentials",
        "browser_helpers": browser,
        "page": "generated-page",
    }
    created = []

    def publisher_factory(parent, ffmpeg, source_uri, _helpers, *, credentials):
        created.append((parent, ffmpeg, source_uri, credentials))
        return "red-publisher"

    return kwargs, created, publisher_factory


def test_clock_probe_continues_only_after_an_actual_backend_person_event():
    event = {
        "id": "event-1",
        "camera_id": "camera-1",
        "person_count": 1,
        "confidence": 0.9,
        "occurred_at": "2026-11-07T00:00:00Z",
        "playback_at": "2026-11-07T00:00:00Z",
    }
    kwargs, created, factory = _case(event=event)
    assert resume_clock_diagnostic_after_navigation_timeout(kwargs, factory) == ({}, event)
    assert kwargs["client"].routes == ["/api/v1/operator/cameras/camera-1/events"]
    assert kwargs["helpers"].stopped == ["positive-publisher"]
    assert kwargs["publishers"] == ["red-publisher"]
    assert kwargs["processes"] == ["red-publisher"]
    assert kwargs["browser_helpers"].calls == [
        ("open", "generated-page", "camera-1"),
        ("decode", "generated-page", "live-video", "red"),
    ]
    assert len(created) == 1


def test_clock_probe_refuses_to_continue_when_event_is_absent():
    kwargs, created, factory = _case()
    assert resume_clock_diagnostic_after_navigation_timeout(kwargs, factory) is None
    assert kwargs["helpers"].stopped == []
    assert created == []


def test_clock_probe_refuses_to_continue_from_an_earlier_analytics_gate():
    kwargs, created, factory = _case(stage="positive_camera_recording")
    assert resume_clock_diagnostic_after_navigation_timeout(kwargs, factory) is None
    assert kwargs["client"].routes == []
    assert kwargs["helpers"].stopped == []
    assert created == []


def test_clock_probe_refuses_to_invent_or_accept_a_malformed_event():
    kwargs, created, factory = _case(
        event={
            "id": "event-1",
            "camera_id": "camera-1",
            "person_count": 1,
            "confidence": float("nan"),
            "occurred_at": "2026-11-07T00:00:00Z",
            "playback_at": "2026-11-07T00:00:00Z",
        }
    )
    assert resume_clock_diagnostic_after_navigation_timeout(kwargs, factory) is None
    assert kwargs["helpers"].stopped == []
    assert created == []
