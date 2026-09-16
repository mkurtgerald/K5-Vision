from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from k5vision.analytics.contracts import (
    ANALYTICS_API_VERSION,
    AnalyticEvent,
    AnalyticManifest,
    AnalyticState,
    BoundingBox,
)
from k5vision.analytics.registry import AnalyticRegistrationError, AnalyticRegistry


def _manifest(**overrides: object) -> AnalyticManifest:
    values: dict[str, object] = {
        "analytic_id": "k5.slip-fall",
        "name": "Slip/Fall",
        "version": "0.1.0",
        "event_types": ("person.fall", "person.man_down"),
        "source_repository": "analytics-lab",
        "source_revision": "abc123",
        "license_spdx": "MIT",
    }
    values.update(overrides)
    return AnalyticManifest(**values)


def test_manifest_defaults_to_current_contract() -> None:
    manifest = _manifest()

    assert manifest.api_version == ANALYTICS_API_VERSION
    assert manifest.runtime == "external"
    assert manifest.default_confidence == 0.5


def test_registry_stages_compatible_analytic() -> None:
    registry = AnalyticRegistry()

    registered = registry.register(_manifest())

    assert registered.state is AnalyticState.STAGED
    assert registry.get("k5.slip-fall") is registered
    assert registry.list() == (registered,)


def test_registry_rejects_incompatible_api() -> None:
    registry = AnalyticRegistry()

    with pytest.raises(AnalyticRegistrationError, match="unsupported analytics API"):
        registry.register(_manifest(api_version="k5.analytics/v99"))


def test_registry_requires_explicit_upgrade() -> None:
    registry = AnalyticRegistry()
    registry.register(_manifest())

    with pytest.raises(AnalyticRegistrationError, match="explicit upgrade is required"):
        registry.register(_manifest(version="0.2.0"))


def test_registry_state_and_unregister() -> None:
    registry = AnalyticRegistry()
    registry.register(_manifest())

    updated = registry.set_state("k5.slip-fall", AnalyticState.ENABLED)
    removed = registry.unregister("k5.slip-fall")

    assert updated.state is AnalyticState.ENABLED
    assert removed is updated
    assert registry.get("k5.slip-fall") is None


def test_registry_missing_analytic_raises_key_error() -> None:
    registry = AnalyticRegistry()

    with pytest.raises(KeyError):
        registry.set_state("missing", AnalyticState.DISABLED)
    with pytest.raises(KeyError):
        registry.unregister("missing")


def test_bounding_box_must_remain_inside_frame() -> None:
    with pytest.raises(ValidationError):
        BoundingBox(x=0.8, y=0.1, width=0.3, height=0.2)

    with pytest.raises(ValidationError):
        BoundingBox(x=0.1, y=0.8, width=0.2, height=0.3)


def test_event_accepts_extensible_attributes() -> None:
    event = AnalyticEvent(
        event_id="evt-1",
        analytic_id="k5.slip-fall",
        analytic_version="0.1.0",
        event_type="person.fall",
        occurred_at=datetime.now(UTC),
        source_id="camera-17",
        confidence=0.93,
        bbox=BoundingBox(x=0.1, y=0.2, width=0.3, height=0.4),
        attributes={"posture": "prone"},
        severity="high",
    )

    assert event.attributes["posture"] == "prone"
    assert event.model_extra == {"severity": "high"}
