"""Stage-One application factory with private physical live-operator wiring.

Use this factory for human physical acceptance on the Windows operator host. It reads
the already-established private Stage-03 source/credential environment at process
startup and injects the concrete source resolver and Windows launcher into the normal
K5 control-plane application. Missing or malformed private runtime configuration stays
fail closed; no secret is copied into public API models or retained evidence.
"""

from __future__ import annotations

from os import environ

from fastapi import FastAPI

from k5vision.analytics_config import (
    AnalyticsConfigurationError,
    load_analytics_configuration,
)
from k5vision.analytics_runtime import AnalyticsProviderFactory
from k5vision.media.analytics_overlay_delivery import AnalyticsObservationProvider
from k5vision.operator_recording import STAGE_ONE_RECORDING_ROOT_ENV
from k5vision.operator_runtime import build_environment_operator_runtime


def create_app(**kwargs: object) -> FastAPI:
    # Keep the factory import behind explicit analytics admission. Importing the
    # factory itself does not construct the separate default ASGI application.
    from k5vision.main import create_app as create_control_plane

    return create_control_plane(**kwargs)


def create_stage_one_app(
    *,
    detection_provider: AnalyticsObservationProvider | None = None,
) -> FastAPI:
    """Build the standard K5 app with bounded physical live operation enabled."""
    config = load_analytics_configuration(environ)
    if config is not None and detection_provider is not None:
        raise AnalyticsConfigurationError("Configured analytics conflicts with injected provider.")
    provider_factory = None if config is None else AnalyticsProviderFactory(config)
    source_resolver, launcher = build_environment_operator_runtime(
        environ,
        detection_provider=detection_provider,
        detection_provider_factory=provider_factory,
    )
    recording_root = environ.get(STAGE_ONE_RECORDING_ROOT_ENV, "").strip() or None
    return create_app(
        operator_source_resolver=source_resolver,
        operator_launcher=launcher,
        operator_recording_root=recording_root,
    )
