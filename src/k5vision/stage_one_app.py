"""Stage-One application factory with private physical live-operator wiring.

Use this factory for human physical acceptance on the Windows operator host. It reads
the already-established private Stage-03 source/credential environment at process
startup and injects the concrete source resolver and Windows launcher into the normal
K5 control-plane application. Missing or malformed private runtime configuration stays
fail closed; no secret is copied into public API models or retained evidence.
"""

from __future__ import annotations

from os import environ
from pathlib import Path

from fastapi import FastAPI

from k5vision import installed_profile
from k5vision.analytics_config import (
    AnalyticsConfigurationError,
    load_analytics_configuration,
)
from k5vision.analytics_runtime import AnalyticsProviderFactory
from k5vision.media.analytics_overlay_delivery import AnalyticsObservationProvider
from k5vision.operator_launch import OperatorLauncher, OperatorSourceResolver
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


def _create_installed_operator_app(
    *,
    application_root: str | Path,
    identity_directory: str | Path,
    device_database: str | Path,
    recording_root: str | Path,
    source_resolver: OperatorSourceResolver | None = None,
    launcher: OperatorLauncher | None = None,
) -> FastAPI:
    """Internal durable composition, requiring an explicitly supplied private runtime.

    No CLI/Alpha launcher selects this factory. The current Stage03 credential
    trial bundle is not customer configuration, so it must never fill this seam.
    A supported installed source provider and physical restart/recording/playback
    acceptance remain prerequisites before exposing an installed operator mode.
    Admission uses a copied environment and explicit database paths; it never
    temporarily rebinds another application's process-wide identity or storage.
    Existing optional service/admin token authentication is unchanged.
    """
    if not callable(getattr(source_resolver, "resolve", None)) or not callable(
        getattr(launcher, "run", None)
    ):
        raise installed_profile.InstalledProfileError(
            "Installed operator requires an explicit private source resolver and launcher."
        )
    installed_profile.refuse_trial_sources(environ)
    profile = installed_profile.load_installed_profile(
        application_root=application_root,
        identity_directory=identity_directory,
        device_database=device_database,
        recording_root=recording_root,
    )
    # Reuse existing identity conflict checks on a disposable mapping only.
    # Explicit factory arguments remove the need for process-global rebinding.
    with installed_profile.installed_profile_environment(profile, dict(environ)):
        pass
    application = create_app(
        control_plane_site_id=profile.identity.site_id,
        user_db_path=profile.identity.database_path,
        device_db_path=profile.device_database,
        operator_source_resolver=source_resolver,
        operator_launcher=launcher,
        operator_recording_root=profile.recording_root,
    )
    required = (
        "user_registry",
        "device_registry",
        "operator_launch_coordinator",
        "operator_recording_coordinator",
        "operator_playback_coordinator",
        "operator_playback_timeline",
        "operator_export_coordinator",
        "operator_recording_catalog",
    )
    if any(getattr(application.state, name, None) is None for name in required):
        for name in ("user_registry", "device_registry"):
            registry = getattr(application.state, name, None)
            if registry is not None:
                try:
                    registry.close()
                except Exception:
                    # Still attempt the other owned close; never delete state or
                    # expose storage exceptions while reporting failed startup.
                    pass
        raise installed_profile.InstalledProfileError(
            "Installed operator application construction was incomplete."
        )
    return application
