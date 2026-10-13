"""Persistent native donor composition behind the local operator dashboard."""

from __future__ import annotations

import asyncio
import os
import socket
import webbrowser
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import uvicorn

from k5vision.identity_state import load_identity_state
from k5vision.main import create_app
from k5vision.operator_product_api import install_operator_product_api
from k5vision.operator_product_service import OperatorProductError, OperatorProductService
from k5vision.operator_product_state import OperatorCameraStore
from k5vision.operator_profile_lock import OperatorProfileLock


def create_operator_product_app(
    *,
    identity_directory: Path,
    service: OperatorProductService,
    open_browser: bool = False,
    ready_callback: Callable[[], None] | None = None,
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
    if application.state.device_registry is None or application.state.user_registry is None:
        raise OperatorProductError("Durable operator account or device state was refused")
    cameras = OperatorCameraStore(
        identity.directory, identity.site_id, registry=application.state.device_registry
    )
    from k5vision.operator_analytics import OperatorAnalyticsBroker
    from k5vision.operator_analytics_person import (
        OperatorPersonConfigurationError,
        load_person_configuration,
    )

    analytics = None
    analytics_status = "disabled"
    try:
        person_config = load_person_configuration(os.environ)
        if person_config is not None:
            analytics = OperatorAnalyticsBroker(person_config)
            analytics_status = "configured"
    except OperatorPersonConfigurationError:
        # An optional rejected detector cannot prevent camera recording. It
        # remains disabled and the operator sees a fixed, source-free status.
        analytics_status = "unavailable"
    install_operator_product_api(
        application,
        cameras=cameras,
        service=service,
        public_port=service.public_port,
        analytics=analytics,
        analytics_status=analytics_status,
    )
    previous_lifespan = application.router.lifespan_context
    profile_lock = OperatorProfileLock(identity.directory)

    @asynccontextmanager
    async def lifespan(app):
        async with previous_lifespan(app):
            analytics_task = None
            try:
                profile_lock.acquire()
                await service.start(cameras.list_sources())
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
                        0.5, webbrowser.open, f"http://127.0.0.1:{service.public_port}/operator/"
                    )
                if ready_callback is not None:
                    ready_callback()
                yield
            finally:
                try:
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
                            await service.stop()
                        finally:
                            try:
                                cameras.close()
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
) -> None:
    """Serve only numeric loopback, retaining the control socket until bind."""
    if not 1 <= port <= 65535 or not 1 <= recording_limit_gb <= 4096:
        raise OperatorProductError("Operator listener or recording limit was refused")
    identity = load_identity_state(identity_directory)
    reservations: list[socket.socket] = []
    try:
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
            recording_root=recording_root or identity.directory / "recordings",
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
