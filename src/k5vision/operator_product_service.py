"""Owned native donor processes for the persistent local operator application."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import stat
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from k5vision.identity_state import _checked_path, _regular_file, _windows_admission
from k5vision.media.mediamtx_config import MediaMTXSource, generate_mediamtx_configuration
from k5vision.media.mediamtx_provider import (
    MediaMTXCredentials,
    MediaMTXProvider,
    MediaWorkerError,
    _NoRedirect,
)
from k5vision.operator_media_gateway import generate_operator_gateway_configuration


class OperatorProductError(RuntimeError):
    """Safe product failure, without private configuration or source values."""


def admit_native_executable(path: Path, component: str) -> Path:
    """Verify the executable bytes derived from the immutable donor archive."""
    try:
        if component not in {"mediamtx", "caddy"}:
            raise ValueError
        path = Path(path)
        info = _regular_file(path)
        if info.st_size > 200 * 1024 * 1024:
            raise ValueError
        manifest_path = Path(__file__).parent / "data" / f"{component}-donor-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        platform = "windows_amd64" if os.name == "nt" else "linux_amd64"
        artifact = next(a for a in manifest["artifacts"] if a["platform"] == platform)
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        current = _regular_file(path)
        if (
            digest.hexdigest() != artifact["executable_sha256"]
            or info.st_size != artifact["executable_bytes"]
            or (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)
            != (current.st_dev, current.st_ino, current.st_mtime_ns, current.st_size)
        ):
            raise ValueError
        return path
    except Exception:
        raise OperatorProductError("Native media executable identity was refused") from None


def _private_directory(path: Path) -> Path:
    path = _checked_path(path)
    if not path.exists():
        path.mkdir(mode=0o700)
    _checked_path(path)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise OperatorProductError("Operator state directory was refused")
    if os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
        raise OperatorProductError("Operator state directory must be private")
    _windows_admission(path, private=True, directory=True)
    return path


def _private_configuration(path: Path, content: str) -> None:
    _checked_path(path)
    if path.exists():
        _regular_file(path)
    temporary = path.parent / (".operator-" + secrets.token_hex(16) + ".tmp")
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        _regular_file(temporary)
        os.replace(temporary, path)
        _regular_file(path)
    finally:
        temporary.unlink(missing_ok=True)


class OperatorProductService:
    """Run native recording independently of browser windows and detector views."""

    def __init__(
        self,
        *,
        site_id: str,
        state_root: Path,
        recording_root: Path,
        media_executable: Path,
        gateway_executable: Path,
        public_port: int,
        control_port: int,
        api_port: int,
        playback_port: int,
        rtsp_port: int,
        hls_port: int,
        max_recording_bytes: int = 20 * 1024**3,
    ) -> None:
        self.site_id = site_id
        self.state_root = _private_directory(state_root)
        self.recording_root = _private_directory(recording_root)
        self.media_executable = admit_native_executable(media_executable, "mediamtx")
        self.gateway_executable = admit_native_executable(gateway_executable, "caddy")
        self.credentials = MediaMTXCredentials("k5worker", secrets.token_urlsafe(32))
        self.cdn_secret = secrets.token_urlsafe(32)
        self.ports = {
            "api_port": api_port,
            "playback_port": playback_port,
            "rtsp_port": rtsp_port,
            "hls_port": hls_port,
        }
        self.public_port = public_port
        self.control_port = control_port
        self.provider = MediaMTXProvider(
            site_id=site_id,
            api_origin=f"http://127.0.0.1:{api_port}",
            playback_origin=f"http://127.0.0.1:{playback_port}",
            credentials=self.credentials,
        )
        self.max_recording_bytes = max_recording_bytes
        if type(max_recording_bytes) is not int or not 1 <= max_recording_bytes <= 4096 * 1024**3:
            raise OperatorProductError("Recording capacity was refused")
        self._media = None
        self._gateway = None
        self._sources: tuple[MediaMTXSource, ...] = ()
        self._paths: set[str] = set()
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._closing = False
        self._disk_full = False
        self._ready = False
        self._needs_reconcile = False
        self._spawns: set[asyncio.Task] = set()
        self._storage = None
        self._storage_status = None
        self._health_failures = {"media": 0, "gateway": 0}
        self._media_file = self.state_root / "operator-mediamtx.json"
        self._gateway_file = self.state_root / "operator-gateway.json"

    def _configuration(self, sources: tuple[MediaMTXSource, ...]) -> dict:
        config = json.loads(
            generate_mediamtx_configuration(
                site_id=self.site_id,
                sources=sources,
                credentials=self.credentials,
                recording_directory=str(self.recording_root),
                allow_empty=True,
                **self.ports,
            )
        )
        # Only K5-owned opaque paths may be read. Browser requests are separately
        # authorized on every gateway request. No wildcard publisher is admitted.
        config["authInternalUsers"][0]["permissions"] = [
            {"action": "api"},
            {"action": "read", "path": "~^k5_[0-9a-f]{64}$"},
            {"action": "playback", "path": "~^k5_[0-9a-f]{64}$"},
        ]
        config["hlsCDNSecret"] = self.cdn_secret
        if self._disk_full:
            for path in config["paths"].values():
                path["record"] = False
        return config

    def status(self) -> str:
        if not self._ready or any(
            process is None or process.poll() is not None
            for process in (self._media, self._gateway)
        ):
            return "unavailable"
        return "disk_full" if self._disk_full else "ready"

    def diagnostics(self) -> dict:
        """Return the last bounded sample, with no paths, sources or credentials."""
        storage = self._storage_status
        snapshot = None
        if storage is not None:
            reason = "ready"
            if not storage.recording_allowed:
                if storage.free_bytes < 1024**3:
                    reason = "free_space"
                elif storage.total_bytes > self.max_recording_bytes:
                    reason = "capacity"
                else:
                    reason = "inspection"
            snapshot = {
                "recording_bytes": storage.total_bytes,
                "free_bytes": storage.free_bytes,
                "recording_allowed": storage.recording_allowed,
                "reason": reason,
            }
        return {
            "worker_status": self.status(),
            "max_recording_bytes": self.max_recording_bytes,
            "storage": snapshot,
        }

    def _check_gateway(self) -> None:
        """Probe the owned loopback HTTP listener without redirects or a proxy."""
        request = Request(f"http://127.0.0.1:{self.public_port}/operator/")
        try:
            with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=2) as response:
                if response.status != 200 or not response.read(1):
                    raise ValueError
        except HTTPError as error:
            error.close()
            raise OperatorProductError("Native operator gateway health check failed") from None
        except Exception:
            raise OperatorProductError("Native operator gateway health check failed") from None

    def _write_media(self) -> None:
        _private_configuration(
            self._media_file, json.dumps(self._configuration(self._sources), indent=2)
        )

    async def _wait_media(self) -> None:
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if self._media is None or self._media.poll() is not None:
                break
            try:
                await self.provider.info()
                return
            except MediaWorkerError:
                await asyncio.sleep(0.1)
        raise OperatorProductError("Native recording worker did not become ready")

    async def _wait_gateway(self) -> None:
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if self._gateway is None or self._gateway.poll() is not None:
                break
            try:
                await asyncio.to_thread(self._check_gateway)
                return
            except OperatorProductError:
                await asyncio.sleep(0.1)
        raise OperatorProductError("Native operator gateway did not become ready")

    def _start_media(self) -> None:
        from k5vision.operator_native_process import OwnedNativeProcess

        self._write_media()
        self._media = OwnedNativeProcess(
            [str(self.media_executable), str(self._media_file)], cwd=self.state_root
        )

    async def _spawn(self, operation) -> None:
        # Join an in-flight constructor before cleanup can snapshot ownership.
        # A canceled asyncio waiter must not leave a late native child behind.
        task = asyncio.create_task(asyncio.to_thread(operation))
        self._spawns.add(task)
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(task)
            except Exception:
                pass
            raise
        finally:
            self._spawns.discard(task)

    def _start_gateway(self) -> None:
        from k5vision.operator_native_process import OwnedNativeProcess

        configuration = generate_operator_gateway_configuration(
            public_port=self.public_port,
            control_port=self.control_port,
            hls_port=self.ports["hls_port"],
            playback_port=self.ports["playback_port"],
            credentials=self.credentials,
            cdn_secret=self.cdn_secret,
            static_root=Path(__file__).parent / "operator_web",
        )
        _private_configuration(self._gateway_file, configuration)
        self._gateway = OwnedNativeProcess(
            [str(self.gateway_executable), "run", "--config", str(self._gateway_file)],
            cwd=self.state_root,
        )

    async def start(self, sources: tuple[MediaMTXSource, ...]) -> None:
        try:
            from k5vision.operator_recording_storage import RecordingStorage

            self._storage = RecordingStorage(
                self.recording_root, max_bytes=self.max_recording_bytes
            )
            storage = await asyncio.to_thread(self._storage.prune)
            self._storage_status = storage
            self._disk_full = not storage.recording_allowed
            self._sources = tuple(sources)
            await self._spawn(self._start_media)
            await self._wait_media()
            self._paths = {source.binding.path_name for source in sources}
            await self._spawn(self._start_gateway)
            await self._wait_gateway()
            self._ready = True
            self._task = asyncio.create_task(self._maintain(), name="operator-native-supervisor")
        except asyncio.CancelledError:
            await self.stop()
            raise
        except Exception:
            await self.stop()
            raise OperatorProductError("Operator startup was refused or incomplete") from None

    def _mutation(self, method: str, route: str, payload: dict | None = None) -> None:
        token = base64.b64encode(
            f"{self.credentials.username}:{self.credentials.password}".encode("ascii")
        ).decode("ascii")
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            f"http://127.0.0.1:{self.ports['api_port']}" + route,
            method=method,
            data=data,
            headers={"Authorization": "Basic " + token, "Content-Type": "application/json"},
        )
        try:
            with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=3) as response:
                if response.status != 200:
                    raise ValueError
        except HTTPError as error:
            error.close()
            raise OperatorProductError("Camera configuration could not be applied") from None
        except Exception:
            raise OperatorProductError("Camera configuration could not be applied") from None

    async def apply_sources(self, sources: tuple[MediaMTXSource, ...]) -> None:
        async with self._lock:
            config = self._configuration(tuple(sources))
            self._sources = tuple(sources)
            self._needs_reconcile = True
            if not self._ready or self._media is None or self._media.poll() is not None:
                raise OperatorProductError("Recording worker is restarting; camera settings saved")
            new_paths = set(config["paths"])
            try:
                for path, settings in config["paths"].items():
                    operation = "replace" if path in self._paths else "add"
                    await asyncio.to_thread(
                        self._mutation, "POST", f"/v3/config/paths/{operation}/{path}", settings
                    )
                for path in self._paths - new_paths:
                    await asyncio.to_thread(
                        self._mutation, "DELETE", f"/v3/config/paths/delete/{path}"
                    )
                self._paths = new_paths
                await asyncio.to_thread(self._write_media)
                self._needs_reconcile = False
            except BaseException:
                self._ready = False
                raise

    async def _maintain(self) -> None:
        last_storage = 0.0
        last_health = 0.0
        while not self._closing:
            await asyncio.sleep(2)
            try:
                async with self._lock:
                    if (
                        self._media is None
                        or self._media.poll() is not None
                        or self._needs_reconcile
                        or self._health_failures["media"] >= 3
                    ):
                        self._ready = False
                        if self._media is not None:
                            await asyncio.to_thread(self._media.stop)
                        await self._spawn(self._start_media)
                        await self._wait_media()
                        self._health_failures["media"] = 0
                        self._paths = {s.binding.path_name for s in self._sources}
                        self._needs_reconcile = False
                    if (
                        self._gateway is None
                        or self._gateway.poll() is not None
                        or self._health_failures["gateway"] >= 3
                    ):
                        self._ready = False
                        if self._gateway is not None:
                            await asyncio.to_thread(self._gateway.stop)
                        await self._spawn(self._start_gateway)
                        await self._wait_gateway()
                        self._health_failures["gateway"] = 0
                    if time.monotonic() - last_health >= 10 or any(self._health_failures.values()):
                        for component, probe in (
                            ("media", self.provider.info),
                            ("gateway", self._check_gateway),
                        ):
                            try:
                                if component == "media":
                                    await probe()
                                else:
                                    await asyncio.to_thread(probe)
                                self._health_failures[component] = 0
                            except Exception:
                                self._health_failures[component] += 1
                        last_health = time.monotonic()
                    if time.monotonic() - last_storage >= 15:
                        result = await asyncio.to_thread(self._storage.prune)
                        self._storage_status = result
                        paused = not result.recording_allowed
                        if paused != self._disk_full:
                            self._disk_full = paused
                            self._needs_reconcile = True
                            for path in self._paths:
                                await asyncio.to_thread(
                                    self._mutation,
                                    "PATCH",
                                    f"/v3/config/paths/patch/{path}",
                                    {"record": not paused},
                                )
                            await asyncio.to_thread(self._write_media)
                            self._needs_reconcile = False
                        last_storage = time.monotonic()
                    self._ready = not any(self._health_failures.values())
            except asyncio.CancelledError:
                raise
            except Exception:
                self._ready = False
                await asyncio.sleep(3)

    async def stop(self) -> None:
        self._closing = True
        self._ready = False
        task = self._task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        for pending in tuple(self._spawns):
            try:
                await asyncio.shield(pending)
            except Exception:
                pass
        cleanup_failed = False
        for process in (self._gateway, self._media):
            if process is not None:
                try:
                    await asyncio.to_thread(process.stop)
                except Exception:
                    cleanup_failed = True
        if cleanup_failed:
            raise OperatorProductError("Native operator cleanup was incomplete") from None
        self._gateway = self._media = None
