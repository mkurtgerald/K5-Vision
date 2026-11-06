"""Persistent native donor composition behind the local operator dashboard."""

from __future__ import annotations

import asyncio
import os
import socket
import webbrowser
from collections.abc import Callable
from contextlib import ExitStack, asynccontextmanager, suppress
from pathlib import Path

import uvicorn

from k5vision.identity_state import load_identity_state
from k5vision.main import create_app
from k5vision.operator_product_api import install_operator_product_api
from k5vision.operator_product_service import OperatorProductError, OperatorProductService
from k5vision.operator_product_state import OperatorCameraStore
from k5vision.operator_profile_lock import OperatorProfileLock


def _retain_product_authentication_routes(application):
    """Reuse account sessions without inheriting generic administration APIs.

    Product policy, delegation, camera custody and audit live in the product
    facades installed below. Admit only the explicitly shared authentication
    and health routes before installing those facades; new control-plane
    endpoints must not silently become another product administration path.
    """
    shared = {
        "/api/v1/health": {"GET"},
        "/api/v1/auth/bootstrap-password": {"POST"},
        "/api/v1/auth/login": {"POST"},
        "/api/v1/auth/me": {"GET"},
        "/api/v1/auth/logout": {"POST"},
    }
    application.router.routes[:] = [
        route
        for route in application.router.routes
        if getattr(route, "path", None) in shared
        and getattr(route, "methods", set()) == shared[route.path]
    ]
    application.openapi_schema = None


def create_operator_product_app(
    *,
    identity_directory: Path,
    service: OperatorProductService,
    open_browser: bool = False,
    ready_callback: Callable[[], None] | None = None,
    profile_lease: OperatorProfileLock | None = None,
):
    identity = load_identity_state(identity_directory)
    if identity.site_id != service.site_id:
        raise OperatorProductError("Operator site configuration was refused")
    application = create_app(
        control_plane_site_id=identity.site_id,
        control_plane_token="",
        control_plane_read_token="",
        device_db_path=identity.directory / "devices.sqlite3",
        user_db_path=identity.database_path,
    )
    _retain_product_authentication_routes(application)
    if application.state.device_registry is None or application.state.user_registry is None:
        raise OperatorProductError("Durable operator account or device state was refused")
    cameras = OperatorCameraStore(
        identity.directory, identity.site_id, registry=application.state.device_registry
    )
    from k5vision.operator_analytics import OperatorAnalyticsBroker
    from k5vision.operator_analytics_person import (
        PERSON_CONFIG_ENV,
        OperatorPersonConfigurationError,
        load_installed_person_configuration,
    )
    from k5vision.operator_events import (
        OperatorEventsError,
        OperatorEventService,
        OperatorEventStore,
    )

    analytics = None
    person_config = None
    analytics_status = "disabled"
    try:
        person_config = load_installed_person_configuration(os.environ)
        if person_config is not None:
            # The sealed installed person model supports both recorder-owned
            # background events and opt-in, transient, same-frame sample boxes.
            # Limit the additional native view worker to one in installed mode;
            # the engineering override retains its four-view test capacity.
            views = 4 if PERSON_CONFIG_ENV in os.environ else 1
            analytics = OperatorAnalyticsBroker(person_config, max_views=views)
            analytics_status = "configured"
    except OperatorPersonConfigurationError:
        # An optional rejected detector cannot prevent camera recording. It
        # remains disabled and the operator sees a fixed, source-free status.
        analytics_status = "unavailable"
    events = None
    try:
        event_store = OperatorEventStore(identity.directory, identity.site_id)
        events = OperatorEventService(
            store=event_store,
            cameras=cameras,
            recording_root=getattr(service, "recording_root", identity.directory / "recordings"),
            configuration=person_config,
            unavailable=analytics_status == "unavailable",
            recording_files=getattr(service, "recording_files", None),
        )
    except (OperatorEventsError, OSError, ValueError):
        # Event-state failures must never prevent camera recording or sign-in.
        if "event_store" in locals():
            event_store.close()
    application.state.operator_events = events
    from k5vision.operator_audit import OperatorAuditStore
    from k5vision.operator_identity_security import AccountSecurityService, AccountSecurityStore
    from k5vision.operator_integrations import OperatorIntegrationStore
    from k5vision.operator_oidc import OIDCService, OIDCStore
    from k5vision.operator_role_policy import OperatorRolePolicy
    from k5vision.operator_time_admin import TimeAdministrationService

    # Account security and access policy are mandatory: damaged state cannot
    # silently restore password-only login or the account's broader base role.
    security_store = security = roles = audit = integrations = oidc = time_admin = None
    try:
        security_store = AccountSecurityStore(identity.directory, identity.site_id)
        security = AccountSecurityService(
            store=security_store, users=application.state.user_registry
        )
        roles = OperatorRolePolicy(identity.directory, identity.site_id)
        audit = OperatorAuditStore(identity.directory, identity.site_id)
        integrations = OperatorIntegrationStore(identity.directory, identity.site_id)
        oidc = OIDCService(store=OIDCStore(security_store), users=application.state.user_registry)
        time_admin = TimeAdministrationService(identity.directory)
    except BaseException:
        with ExitStack() as cleanup:
            cleanup.callback(cameras.close)
            if "event_store" in locals():
                cleanup.callback(event_store.close)
            for component in (security or security_store, roles, audit, integrations, oidc):
                if component is not None:
                    cleanup.callback(component.close)
        raise
    application.state.operator_identity_security = security
    application.state.operator_roles = roles
    application.state.operator_audit = audit
    application.state.operator_integrations = integrations
    application.state.operator_oidc = oidc
    application.state.operator_time_admin = time_admin

    def close_admin_state():
        with ExitStack() as cleanup:
            for component in (cameras, audit, security, roles, integrations, oidc):
                cleanup.callback(component.close)

    install_operator_product_api(
        application,
        cameras=cameras,
        service=service,
        public_port=service.public_port,
        analytics=analytics,
        analytics_status=analytics_status,
        prepare_native_person_on_camera=(
            analytics is not None and PERSON_CONFIG_ENV not in os.environ
        ),
        state_root=identity.directory,
    )
    previous_lifespan = application.router.lifespan_context
    profile_lock = profile_lease or OperatorProfileLock(identity.directory)

    @asynccontextmanager
    async def lifespan(app):
        analytics_task = None
        integration_task = None
        cleaned = False

        async def stop_owned():
            nonlocal cleaned
            if cleaned:
                return
            cleaned = True
            try:
                if integration_task is not None:
                    integration_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await integration_task
                if analytics_task is not None:
                    analytics_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await analytics_task
            finally:
                try:
                    if analytics is not None:
                        await analytics.aclose()
                finally:
                    try:
                        if events is not None:
                            await events.aclose()
                    finally:
                        try:
                            exports = getattr(application.state, "operator_exports", None)
                            if exports is not None:
                                await exports.aclose()
                        finally:
                            try:
                                await time_admin.aclose()
                            finally:
                                try:
                                    await service.stop()
                                finally:
                                    close_admin_state()

        try:
            if profile_lease is None:
                profile_lock.acquire()
            else:
                profile_lock.assert_held(identity.site_id)
        except BaseException:
            # Another running owner retains its workers and files. Close only
            # this unstarted composition's metadata handles.
            if events is not None:
                await events.aclose()
            await time_admin.aclose()
            close_admin_state()
            raise
        try:
            async with previous_lifespan(app):
                try:
                    await service.start(cameras.list_sources())
                    if events is not None:
                        await events.start()

                        async def deliver_observations():
                            while True:
                                task = asyncio.create_task(
                                    asyncio.to_thread(
                                        integrations.capture_person_events,
                                        events.store,
                                        known_camera=lambda identifier: (
                                            application.state.device_registry.get(identifier)
                                            is not None
                                        ),
                                    )
                                )
                                try:
                                    await asyncio.shield(task)
                                except asyncio.CancelledError:
                                    # A file-owning worker must finish before the
                                    # profile lease and stores are released.
                                    with suppress(Exception):
                                        await task
                                    raise
                                except Exception:
                                    # The integration store exposes its finite
                                    # failure state; recording remains independent.
                                    pass
                                await asyncio.sleep(2)

                        integration_task = asyncio.create_task(
                            deliver_observations(), name="operator-neural-observations"
                        )
                    if analytics is not None:

                        async def maintain_analytics():
                            while True:
                                await asyncio.sleep(5)
                                await analytics.expire_idle()

                        analytics_task = asyncio.create_task(
                            maintain_analytics(), name="operator-transient-person-views"
                        )
                    if open_browser:
                        asyncio.get_running_loop().call_later(
                            0.5,
                            webbrowser.open,
                            f"http://127.0.0.1:{service.public_port}/operator/",
                        )
                    if ready_callback is not None:
                        ready_callback()
                    yield
                finally:
                    await stop_owned()
        finally:
            try:
                await stop_owned()
            finally:
                profile_lock.close()

    application.router.lifespan_context = lifespan
    application.state.operator_product_service = service
    application.state.operator_camera_store = cameras
    application.state.operator_person_status = analytics_status
    return application


def run_operator_product(
    *,
    identity_directory: Path,
    recording_root: Path | None,
    media_executable: Path,
    gateway_executable: Path,
    port: int = 8000,
    open_browser: bool = False,
    recording_limit_gb: int = 20,
    shutdown_requested: Callable[[], bool] | None = None,
    ready_callback: Callable[[], None] | None = None,
    storage_progress: Callable[[dict], None] | None = None,
) -> None:
    """Serve only numeric loopback, retaining the control socket until bind."""
    if not 1 <= port <= 65535 or not 1 <= recording_limit_gb <= 4096:
        raise OperatorProductError("Operator listener or recording limit was refused")
    identity = load_identity_state(identity_directory)
    profile_lease = OperatorProfileLock(identity.directory)
    profile_lease.acquire()
    reservations: list[socket.socket] = []
    try:
        from k5vision.operator_storage_settings import prepare_operator_recording_storage

        storage = prepare_operator_recording_storage(
            state_root=identity.directory,
            site_id=identity.site_id,
            default_recording_directory=recording_root or identity.directory / "recordings",
            quota_bytes=recording_limit_gb * 1024**3,
            max_seconds=300,
            progress=storage_progress,
        )
        for index in range(6):
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            reservations.append(listener)
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind(("127.0.0.1", port if index == 0 else 0))
        public_port, control_port, api_port, playback_port, rtsp_port, hls_port = (
            listener.getsockname()[1] for listener in reservations
        )
        control_socket = reservations[1]
        for index, listener in enumerate(reservations):
            if index != 1:
                listener.close()
        service = OperatorProductService(
            site_id=identity.site_id,
            state_root=identity.directory,
            recording_root=Path(storage["recording_directory"]),
            media_executable=media_executable,
            gateway_executable=gateway_executable,
            public_port=public_port,
            control_port=control_port,
            api_port=api_port,
            playback_port=playback_port,
            rtsp_port=rtsp_port,
            hls_port=hls_port,
            max_recording_bytes=recording_limit_gb * 1024**3,
        )
        application = create_operator_product_app(
            identity_directory=identity.directory,
            service=service,
            open_browser=open_browser,
            ready_callback=ready_callback,
            profile_lease=profile_lease,
        )
        config = uvicorn.Config(
            application,
            host="127.0.0.1",
            port=control_port,
            log_level="warning",
            access_log=False,
            workers=1,
        )
        print(f"K5 Vision operator: http://127.0.0.1:{public_port}/operator/")
        print("Recording continues while the browser is closed. Ctrl+C stops the application.")
        asyncio.run(
            _serve_with_shutdown(
                uvicorn.Server(config),
                sockets=[control_socket],
                shutdown_requested=shutdown_requested,
            )
        )
    finally:
        for listener in reservations:
            listener.close()
        profile_lease.close()


async def _serve_with_shutdown(
    server: uvicorn.Server,
    *,
    sockets: list[socket.socket],
    shutdown_requested: Callable[[], bool] | None,
) -> None:
    """Let a GUI owner request the same graceful lifespan cleanup as Ctrl+C."""
    watcher = None
    if shutdown_requested is not None:

        async def watch_owner():
            while not server.should_exit:
                if shutdown_requested():
                    server.should_exit = True
                    return
                await asyncio.sleep(0.1)

        watcher = asyncio.create_task(watch_owner(), name="operator-desktop-owner-lifetime")
    try:
        await server.serve(sockets=sockets)
    finally:
        if watcher is not None:
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher
