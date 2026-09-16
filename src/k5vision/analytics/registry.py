"""Registry and compatibility checks for analytics integrations."""

from __future__ import annotations

from dataclasses import dataclass

from k5vision.analytics.contracts import (
    ANALYTICS_API_VERSION,
    AnalyticEvent,
    AnalyticManifest,
    AnalyticState,
)


class AnalyticRegistrationError(ValueError):
    """Raised when an analytic cannot be registered safely."""


class AnalyticEventValidationError(ValueError):
    """Raised when a worker emits an event outside its registered contract."""


@dataclass(slots=True)
class RegisteredAnalytic:
    manifest: AnalyticManifest
    state: AnalyticState = AnalyticState.STAGED


class AnalyticRegistry:
    """In-memory registry for installed analytics.

    Persistence and package distribution are intentionally separate concerns. The registry
    provides the stable compatibility boundary needed by K5 services, APIs, and operators.
    """

    def __init__(self) -> None:
        self._items: dict[str, RegisteredAnalytic] = {}

    def register(self, manifest: AnalyticManifest) -> RegisteredAnalytic:
        if manifest.api_version != ANALYTICS_API_VERSION:
            raise AnalyticRegistrationError(
                f"unsupported analytics API {manifest.api_version!r}; "
                f"expected {ANALYTICS_API_VERSION!r}"
            )

        existing = self._items.get(manifest.analytic_id)
        if existing is not None:
            if existing.manifest.version == manifest.version:
                message = (
                    f"analytic {manifest.analytic_id!r} version {manifest.version!r} "
                    "is already registered"
                )
                raise AnalyticRegistrationError(message)
            raise AnalyticRegistrationError(
                f"analytic {manifest.analytic_id!r} is already registered at "
                f"version {existing.manifest.version!r}; explicit upgrade is required"
            )

        registered = RegisteredAnalytic(manifest=manifest)
        self._items[manifest.analytic_id] = registered
        return registered

    def get(self, analytic_id: str) -> RegisteredAnalytic | None:
        return self._items.get(analytic_id)

    def list(self) -> tuple[RegisteredAnalytic, ...]:
        return tuple(sorted(self._items.values(), key=lambda item: item.manifest.analytic_id))

    def set_state(self, analytic_id: str, state: AnalyticState) -> RegisteredAnalytic:
        item = self._items.get(analytic_id)
        if item is None:
            raise KeyError(analytic_id)
        item.state = state
        return item

    def validate_event(
        self, event: AnalyticEvent, *, require_enabled: bool = True
    ) -> RegisteredAnalytic:
        item = self._items.get(event.analytic_id)
        if item is None:
            raise AnalyticEventValidationError(
                f"event references unregistered analytic {event.analytic_id!r}"
            )
        if event.analytic_version != item.manifest.version:
            raise AnalyticEventValidationError(
                f"event version {event.analytic_version!r} does not match registered "
                f"version {item.manifest.version!r}"
            )
        if event.event_type not in item.manifest.event_types:
            raise AnalyticEventValidationError(
                f"event type {event.event_type!r} is not declared by {event.analytic_id!r}"
            )
        if require_enabled and item.state is not AnalyticState.ENABLED:
            raise AnalyticEventValidationError(
                f"analytic {event.analytic_id!r} is not enabled (state={item.state.value!r})"
            )
        return item

    def unregister(self, analytic_id: str) -> RegisteredAnalytic:
        try:
            return self._items.pop(analytic_id)
        except KeyError as exc:
            raise KeyError(analytic_id) from exc
