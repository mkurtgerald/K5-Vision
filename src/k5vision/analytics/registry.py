"""Registry and compatibility checks for analytics integrations."""

from __future__ import annotations

from dataclasses import dataclass

from k5vision.analytics.contracts import ANALYTICS_API_VERSION, AnalyticManifest, AnalyticState


class AnalyticRegistrationError(ValueError):
    """Raised when an analytic cannot be registered safely."""


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
                raise AnalyticRegistrationError(
                    f"analytic {manifest.analytic_id!r} version {manifest.version!r} is already registered"
                )
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

    def unregister(self, analytic_id: str) -> RegisteredAnalytic:
        try:
            return self._items.pop(analytic_id)
        except KeyError as exc:
            raise KeyError(analytic_id) from exc
