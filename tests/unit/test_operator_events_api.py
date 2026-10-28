from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest
import test_operator_admin_api as admin_helpers
import test_operator_events as event_helpers
import test_operator_product_api as helpers

from k5vision.domain.users import UserPatch
from k5vision.operator_events import OperatorEventsError

product = helpers.product
_PREFIX = "/api/v1/operator/cameras"


@pytest.fixture
def event_product(monkeypatch, request):
    original = helpers.install_operator_product_api
    events = []

    def compose(app, *, cameras, service, **options):
        store = event_helpers.OperatorEventStore(cameras._root, service.site_id)
        detector, _, _ = event_helpers._service(store, event_helpers._source())
        app.state.operator_events = detector
        events.append(detector)
        return original(app, cameras=cameras, service=service, **options)

    monkeypatch.setattr(helpers, "install_operator_product_api", compose)
    harness = request.getfixturevalue("product")
    yield harness, events[0]
    asyncio.run(events[0].aclose())


def test_admin_configures_persistent_detector_and_reads_source_recorded_events(event_product):
    product, service = event_product
    identifier = product.enroll()
    url = f"{_PREFIX}/{identifier}/analytics"
    before = product.client.get(url, headers=product.headers)
    assert before.status_code == 200 and before.json()["status"] == "disabled"
    assert before.headers["cache-control"] == "no-store"
    changed = product.client.patch(
        url,
        headers=product.headers,
        json={"enabled": True, "confidence_threshold": 0.7},
    )
    assert changed.status_code == 200 and changed.json()["enabled"] is True
    assert changed.json()["source"] == "recorded_camera_frames"
    assert changed.json()["model"] == "person-detection-retail-0013"
    assert changed.json()["max_parallel_cameras"] == 1
    camera = UUID(identifier)
    assert event_helpers._record(service.store, camera, 1)
    response = product.client.get(f"{_PREFIX}/{identifier}/events", headers=product.headers)
    assert response.status_code == 200 and len(response.json()["events"]) == 1
    assert response.headers["cache-control"] == "no-store"
    event = response.json()["events"][0]
    assert event["occurred_at"] == event["playback_at"] == "2026-10-28T12:00:01.000Z"
    assert "private-test-value" not in str(response.json())


@pytest.mark.parametrize("role", ["viewer", "operator"])
def test_read_roles_receive_only_assigned_events_and_cannot_configure(event_product, role):
    product, service = event_product
    first, second = admin_helpers._two_cameras(product)
    for camera in (first, second):
        service.configure(UUID(camera), enabled=True, confidence_threshold=0.5)
        event_helpers._record(service.store, UUID(camera), 1)
    issue = admin_helpers._create(product, role=role, camera_ids=[first])
    _token, headers = admin_helpers._activate(product, issue)
    for suffix in ("analytics", "events"):
        permitted = product.client.get(f"{_PREFIX}/{first}/{suffix}", headers=headers)
        assert permitted.status_code == 200
        forbidden = product.client.get(f"{_PREFIX}/{second}/{suffix}", headers=headers)
        assert forbidden.status_code == 404
    denied = product.client.patch(
        f"{_PREFIX}/{first}/analytics",
        headers=headers,
        json={"enabled": False, "confidence_threshold": 0.5},
    )
    assert denied.status_code == 403


def test_revoked_or_disabled_session_loses_events_immediately(event_product):
    product, _service = event_product
    camera = product.enroll()
    issue = admin_helpers._create(product, camera_ids=[camera])
    _token, headers = admin_helpers._activate(product, issue)
    product.users.update(UUID(issue["account"]["id"]), UserPatch(enabled=False), actor="test")
    assert product.client.get(f"{_PREFIX}/{camera}/events", headers=headers).status_code == 401
    assert product.client.get(f"{_PREFIX}/{camera}/analytics", headers={}).status_code == 401


def test_settings_require_same_origin_and_current_camera(event_product):
    product, _service = event_product
    camera = product.enroll()
    payload = {"enabled": True, "confidence_threshold": 0.5}
    denied = product.client.patch(
        f"{_PREFIX}/{camera}/analytics",
        headers={**product.headers, "Origin": "https://foreign.invalid"},
        json=payload,
    )
    assert denied.status_code == 403
    product.client.delete(f"{_PREFIX}/{camera}", headers=product.headers)
    assert (
        product.client.get(f"{_PREFIX}/{camera}/events", headers=product.headers).status_code == 404
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"enabled": "yes", "confidence_threshold": 0.5},
        {"enabled": True, "confidence_threshold": 0.49},
        {"enabled": True, "confidence_threshold": 1},
        {"enabled": True, "confidence_threshold": 0.5, "source": "private-camera-url"},
    ],
)
def test_invalid_settings_are_generic_and_do_not_echo_source_material(event_product, payload):
    product, _service = event_product
    camera = product.enroll()
    response = product.client.patch(
        f"{_PREFIX}/{camera}/analytics", headers=product.headers, json=payload
    )
    assert response.status_code == 422
    assert "private-camera-url" not in response.text


@pytest.mark.parametrize(
    "query",
    [
        "start=2026-10-28T12:00:00Z",
        "start=2026-10-28T12:00:00&end=2026-10-28T13:00:00",
        "start=2026-10-28T12:00:00Z&end=2026-10-30T12:00:00Z",
        "start=2026-10-28T12:00:00Z&end=2026-10-28T11:00:00Z",
        "limit=101",
        "before=0",
        "before=9223372036854775808",
        "start=0001-01-01T00:00:00%2B01:00&end=0001-01-01T01:00:00%2B01:00",
    ],
)
def test_event_ranges_and_page_sizes_are_bounded(event_product, query):
    product, _service = event_product
    camera = product.enroll()
    response = product.client.get(f"{_PREFIX}/{camera}/events?{query}", headers=product.headers)
    assert response.status_code == 422


def test_recorded_event_range_and_cursor_remain_utc_bound(event_product):
    product, service = event_product
    camera = product.enroll()
    service.configure(UUID(camera), enabled=True, confidence_threshold=0.5)
    for index in range(1, 4):
        event_helpers._record(service.store, UUID(camera), index)
    selected = product.client.get(
        f"{_PREFIX}/{camera}/events",
        headers=product.headers,
        params={
            "start": datetime(2026, 10, 28, 12, tzinfo=UTC).isoformat(),
            "end": datetime(2026, 10, 28, 12, 1, tzinfo=UTC).isoformat(),
            "limit": 1,
        },
    )
    assert selected.status_code == 200
    assert selected.json()["has_more"]
    assert selected.json()["events"][0]["id"] == 3


def test_private_database_failure_returns_fixed_unavailable(event_product, monkeypatch):
    product, service = event_product
    camera = product.enroll()

    def fail(_camera):
        raise OperatorEventsError("private-analytics-path")

    monkeypatch.setattr(service, "status", fail)
    response = product.client.get(f"{_PREFIX}/{camera}/analytics", headers=product.headers)
    assert response.status_code == 503
    assert response.json() == {"detail": "analytics_unavailable"}
