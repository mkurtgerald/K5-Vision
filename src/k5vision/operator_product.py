"""Persistent native donor composition behind the local operator dashboard."""

from __future__ import annotations

import asyncio
import socket
import threading
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn

from k5vision.identity_state import load_identity_state
from k5vision.main import create_app
from k5vision.operator_product_analytics import configured_sample_coordinator
from k5vision.operator_product_api import install_operator_product_api
from k5vision.operator_product_service import OperatorProductError, OperatorProductService
from k5vision.operator_product_state import OperatorCameraStore
from k5vision.operator_profile_lock import OperatorProfileLock

# Keep active and unresolved leases reachable independently of application GC.
# Admission reserves a slot before lock acquisition, so failures cannot overflow
# this bound. No finalizer closes an unresolved lease; process exit is recovery.
_PRODUCT_LEASE_LIMIT = 64
_PRODUCT_LEASES: dict[int, OperatorProfileLock] = {}
_PRODUCT_LEASE_GUARD = threading.Lock()


def _own_product_lease(lease: OperatorProfileLock) -> None:
    with _PRODUCT_LEASE_GUARD:
        if id(lease) in _PRODUCT_LEASES or len(_PRODUCT_LEASES) >= _PRODUCT_LEASE_LIMIT:
            raise OperatorProductError("Operator profile ownership capacity unavailable")
        _PRODUCT_LEASES[id(lease)] = lease


def _release_product_lease(lease: OperatorProfileLock) -> None:
    lease.close()
    with _PRODUCT_LEASE_GUARD:
        _PRODUCT_LEASES.pop(id(lease), None)


def create_operator_product_app(
    *, identity_directory: Path, service: OperatorProductService, open_browser: bool = False
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
    analytics = configured_sample_coordinator()
    service.analytics_invalidator = analytics.invalidate
    install_operator_product_api(
        application,
        cameras=cameras,
        service=service,
        public_port=service.public_port,
        analytics=analytics,
    )
    previous_lifespan = application.router.lifespan_context
    profile_lock = OperatorProfileLock(identity.directory)

    @asynccontextmanager
    async def lifespan(app):
        async with previous_lifespan(app):
            retain_profile = False
            profile_acquired = False
            lease_reserved = False
            try:
                _own_product_lease(profile_lock)
                lease_reserved = True
                profile_lock.acquire()
                profile_acquired = True
                await service.start(cameras.list_sources())
                if open_browser:
                    asyncio.get_running_loop().call_later(
                        0.5, webbrowser.open, f"http://127.0.0.1:{service.public_port}/operator/"
                    )
                yield
            finally:
                cleanup_error = None
                try:
                    await analytics.aclose()
                except BaseException as exc:
                    # Coordinator cancellation is propagated only after its drain.
                    if not isinstance(exc, asyncio.CancelledError):
                        retain_profile = profile_acquired
                    cleanup_error = exc
                try:
                    await service.stop()
                except BaseException as exc:
                    # A failed or interrupted native stop cannot release exclusivity.
                    retain_profile = profile_acquired
                    cleanup_error = exc
                try:
                    cameras.close()
                except BaseException as exc:
                    if cleanup_error is None:
                        cleanup_error = exc
                if lease_reserved and not retain_profile:
                    try:
                        _release_product_lease(profile_lock)
                    except BaseException as exc:
                        if cleanup_error is None:
                            cleanup_error = exc
                if cleanup_error is not None:
                    raise cleanup_error from None

    application.router.lifespan_context = lifespan
    application.state.operator_profile_lock = profile_lock
    application.state.operator_product_analytics = analytics
    application.state.operator_product_service = service
    application.state.operator_camera_store = cameras
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
            identity_directory=identity.directory, service=service, open_browser=open_browser
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
        asyncio.run(uvicorn.Server(config).serve(sockets=[control_socket]))
    finally:
        for listener in reservations:
            listener.close()
