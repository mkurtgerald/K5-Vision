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
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from k5vision.identity_state import _checked_path, _regular_file, _windows_admission
from k5vision.media.mediamtx_config import MediaMTXSource, generate_mediamtx_configuration
from k5vision.media.mediamtx_provider import (
    MediaMTXBinding,
    MediaMTXCredentials,
    MediaMTXProvider,
    MediaWorkerError,
    MediaWorkerErrorCode,
    _NoRedirect,
)
from k5vision.operator_media_gateway import generate_operator_gateway_configuration
from k5vision.operator_storage_settings import OperatorStorageSettings
from k5vision.operator_time import HostClockObserver

_RECORDING_FLUSH_SECONDS = 3.0
_STORAGE_IDLE_REFRESH_SECONDS = 15.0
_STORAGE_ANALYTICS_REFRESH_SECONDS = 6.0
_STORAGE_ANALYTICS_INTEREST_SECONDS = 12.0


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
        self.storage_settings = OperatorStorageSettings(
            self.state_root, self.site_id, self.recording_root, quota_bytes=max_recording_bytes
        )
        policy = self.storage_settings.snapshot()
        self.recording_root = _private_directory(Path(policy["recording_directory"]))
        self.max_recording_bytes = policy["quota_bytes"]
        self._minimum_free_bytes = policy["minimum_free_bytes"]
        self._retention_seconds = policy["retention_seconds"]
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
        self._storage_operations: set[asyncio.Task] = set()
        self._source_refreshes: set[asyncio.Task] = set()
        self._source_recovery: dict[str, tuple[MediaMTXSource, float]] = {}
        self._storage = None
        self._storage_status = None
        # Only an enabled, active recorded-person sampler asks for this cache.
        # Normal operator polling retains the slower full-retention scan cadence.
        self._analytics_inventory_interest_at: float | None = None
        self._health_failures = {"media": 0, "gateway": 0}
        self._clock = HostClockObserver()
        self._media_generation = 0
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
                if storage.free_bytes < self._minimum_free_bytes:
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
            **self._clock.snapshot(),
        }

    def storage_administration(self) -> dict:
        """Private directories are returned exclusively by administrator routes."""
        snapshot = self.storage_settings.snapshot()
        status = self._storage_status
        snapshot.update(
            recording_bytes=None if status is None else status.total_bytes,
            free_bytes=None if status is None else status.free_bytes,
            recording_allowed=None if status is None else status.recording_allowed,
            inspection_available=status is not None,
        )
        return snapshot

    async def configure_storage(self, *, expected_revision: int, **changes) -> dict:
        """Persist policy and apply limits without resetting observed file ages."""
        async with self._lock:
            if self._closing:
                raise OperatorProductError("Recording storage is stopping")
            policy = self.storage_settings.configure(expected_revision=expected_revision, **changes)
            self.max_recording_bytes = policy["quota_bytes"]
            self._minimum_free_bytes = policy["minimum_free_bytes"]
            self._retention_seconds = policy["retention_seconds"]
            if self._storage is not None:
                await self._storage_operation(
                    self._storage.configure_policy,
                    max_bytes=self.max_recording_bytes,
                    min_free_bytes=self._minimum_free_bytes,
                    retention_seconds=self._retention_seconds,
                )
                result = await self._storage_operation(self._storage.prune)
                await self._apply_storage_status(result)
            return self.storage_administration()

    async def _storage_operation(self, operation, **options):
        """Join a canceled scan/deletion before releasing recorder ownership."""
        if self._closing:
            raise OperatorProductError("Recording storage is stopping")
        task = asyncio.create_task(asyncio.to_thread(operation, **options))
        self._storage_operations.add(task)
        cancelled = False
        try:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancelled = True
                except Exception:
                    break
            result = task.result()
            if cancelled:
                raise asyncio.CancelledError
            return result
        finally:
            self._storage_operations.discard(task)

    async def _apply_storage_status(self, result) -> None:
        if self._closing:
            raise OperatorProductError("Recording storage is stopping")
        self._storage_status = result
        paused = not result.recording_allowed
        if paused != self._disk_full:
            was_ready = self._ready
            self._ready = False
            self._disk_full = paused
            self._needs_reconcile = True
            preferences = {
                source.binding.path_name: source.recording_enabled for source in self._sources
            }
            for path in self._paths:
                await self._storage_operation(
                    self._mutation,
                    method="PATCH",
                    route=f"/v3/config/paths/patch/{path}",
                    payload={"record": preferences.get(path, False) and not paused},
                )
            await self._storage_operation(self._write_media)
            self._needs_reconcile = False
            self._ready = was_ready and not self._closing

    @property
    def media_generation(self) -> int:
        """A new worker gets a fresh bounded connection-readiness window."""
        return self._media_generation

    def recording_progress(self, binding) -> dict:
        """Return cached disk evidence; operator polling never scans recordings."""
        if self._storage is None:
            return {
                "observed_bytes": 0,
                "last_progress_age_seconds": None,
                "inspection_complete": False,
                "observed": False,
            }
        return self._storage.recording_progress(binding.path_name)

    def recording_files(self, binding) -> dict[str, tuple[int, ...]] | None:
        """Private cached inventory for the recorder-owned analytics sampler."""
        if self._storage is None:
            return None
        # Inventory consumers never walk retained recordings. A recent
        # background inference request instead asks existing maintenance to
        # refresh the already-admitted cache sooner after segment rollover.
        self._analytics_inventory_interest_at = time.monotonic()
        return self._storage.recording_files(binding.path_name)

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
        self._media_generation += 1

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
                self.recording_root,
                max_bytes=self.max_recording_bytes,
                min_free_bytes=self._minimum_free_bytes,
                retention_seconds=self._retention_seconds,
            )
            storage = await self._storage_operation(self._storage.prune)
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

    def _mutation(
        self, method: str, route: str, payload: dict | None = None, *, timeout: float = 3.0
    ) -> None:
        if type(timeout) not in (int, float) or not 0 < timeout <= 3:
            raise OperatorProductError("Camera configuration could not be applied")
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
            with build_opener(ProxyHandler({}), _NoRedirect()).open(
                request, timeout=timeout
            ) as response:
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

    def camera_source_refresh_status(self, binding: MediaMTXBinding) -> str:
        """Report target recovery without publishing private source material."""
        if not isinstance(binding, MediaMTXBinding) or binding.site_id != self.site_id:
            raise ValueError("Camera selection belongs to a different site")
        if binding.path_name in self._source_recovery:
            return "recovery_required"
        if self._closing or self.status() == "unavailable" or binding.path_name not in self._paths:
            return "unavailable"
        return "ready"

    @staticmethod
    async def _join_source_task(task: asyncio.Task, *, cancel_operation: bool = False):
        """Keep ownership until a native request or its compensation has exited."""
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
                if cancel_operation:
                    task.cancel()
                    cancel_operation = False
            except Exception:
                break
        try:
            result = task.result()
        except BaseException:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def _source_thread(self, operation, *args, **kwargs):
        return await self._join_source_task(
            asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
        )

    async def _restore_camera_source(self, previous: MediaMTXSource) -> None:
        """Repair only the old target; uncertain writes remain explicitly pending."""
        path = previous.binding.path_name
        failed = False
        try:
            settings = self._configuration(self._sources)["paths"][path]
            await self._source_thread(
                self._mutation, "POST", f"/v3/config/paths/replace/{path}", settings, timeout=3.0
            )
        except Exception:
            failed = True
        try:
            await self._source_thread(self._write_media)
        except Exception:
            failed = True
        if failed:
            self._source_recovery[path] = (previous, time.monotonic() + 10)
        else:
            self._source_recovery.pop(path, None)

    async def _refresh_camera_source(
        self,
        previous: MediaMTXSource,
        replacement: MediaMTXSource,
        sources: tuple[MediaMTXSource, ...],
        settings: dict,
        persist: Callable[[], Awaitable[None]],
    ) -> None:
        before = self._sources
        path = previous.binding.path_name
        try:
            if previous.source_uri == replacement.source_uri:
                # MediaMTX ignores an identical replacement. SourceOnDemand is
                # not hot reloadable in the pinned worker: this temporary pulse
                # recreates only this path. Never persist its temporary value;
                # restore continuous acquisition before saving or committing.
                await self._source_thread(
                    self._mutation,
                    "POST",
                    f"/v3/config/paths/replace/{path}",
                    {**settings, "sourceOnDemand": True},
                    timeout=3.0,
                )
            await self._source_thread(
                self._mutation, "POST", f"/v3/config/paths/replace/{path}", settings, timeout=3.0
            )
            self._sources = sources
            await self._source_thread(self._write_media)
            # The callback is one synchronous atomic SQLite upsert in an async
            # wrapper: no suspension or fallible work may follow its commit.
            await persist()
            self._source_recovery.pop(path, None)
        except BaseException as error:
            self._sources = before
            repair = asyncio.create_task(self._restore_camera_source(previous))
            await self._join_source_task(repair)
            if isinstance(error, asyncio.CancelledError):
                raise
            raise OperatorProductError("Camera source refresh could not be applied") from None

    async def refresh_camera_source(
        self,
        previous: MediaMTXSource,
        replacement: MediaMTXSource,
        *,
        persist: Callable[[], Awaitable[None]],
    ) -> None:
        """Restart one admitted camera without replacing other enrolled paths.

        The caller holds the product mutation lock. Persistence must be exactly
        one atomic synchronous upsert with no await or failure after commit.
        An unchanged URI needs two target replacements to reconnect; every
        request has a three-second HTTP bound. Continuous acquisition is
        restored before the private configuration and camera intent are saved.
        Readiness/video observations remain a separate caller-owned step.
        """
        async with self._lock:
            try:
                if (
                    not isinstance(previous, MediaMTXSource)
                    or not isinstance(replacement, MediaMTXSource)
                    or not callable(persist)
                    or previous.binding != replacement.binding
                    or previous.device != replacement.device
                    or previous.recording_enabled != replacement.recording_enabled
                ):
                    raise ValueError
                previous.__post_init__()
                path = previous.binding.path_name
                current = [source for source in self._sources if source.binding.path_name == path]
                if current != [previous] or path not in self._paths:
                    raise ValueError
                sources = tuple(
                    replacement if source == previous else source for source in self._sources
                )
                settings = self._configuration(sources)["paths"][path]
            except Exception:
                raise OperatorProductError("Camera selection changed during the request") from None
            if (
                self._closing
                or not self._ready
                or self._needs_reconcile
                or self._media is None
                or self._media.poll() is not None
            ):
                raise OperatorProductError("Recording worker is unavailable")
            operation = asyncio.create_task(
                self._refresh_camera_source(previous, replacement, sources, settings, persist)
            )
            self._source_refreshes.add(operation)
            try:
                await self._join_source_task(operation, cancel_operation=True)
            finally:
                self._source_refreshes.discard(operation)

    async def _retry_camera_source(self) -> None:
        """One bounded target repair per maintenance pass, never a worker restart."""
        for path, (previous, retry_at) in tuple(self._source_recovery.items()):
            current = [source for source in self._sources if source.binding.path_name == path]
            if current != [previous] or path not in self._paths:
                self._source_recovery.pop(path, None)
                continue
            if time.monotonic() < retry_at:
                continue
            operation = asyncio.create_task(self._restore_camera_source(previous))
            self._source_refreshes.add(operation)
            try:
                await self._join_source_task(operation)
            finally:
                self._source_refreshes.discard(operation)
            break

    def _change_recording_preferences(self, sources: tuple[MediaMTXSource, ...]) -> None:
        previous = self._sources
        before = {source.binding.path_name: source for source in previous}
        after = {source.binding.path_name: source for source in sources}
        if (
            len(after) != len(sources)
            or set(before) != set(after)
            or any(
                before[path].device != source.device
                or before[path].source_uri != source.source_uri
                or before[path].binding != source.binding
                for path, source in after.items()
            )
        ):
            raise OperatorProductError("Recording selection changed during the request")
        if not self._ready or self._media is None or self._media.poll() is not None:
            raise OperatorProductError("Recording worker is unavailable")
        changed = [
            path
            for path in before
            if before[path].recording_enabled != after[path].recording_enabled
        ]
        applied = []
        try:
            for path in changed:
                # Record-only PATCH keeps the camera source and its live readers.
                applied.append(path)
                self._mutation(
                    "PATCH",
                    f"/v3/config/paths/patch/{path}",
                    {"record": after[path].recording_enabled and not self._disk_full},
                )
            self._sources = tuple(sources)
            self._write_media()
        except BaseException:
            self._sources = previous
            failed = False
            for path in reversed(applied):
                try:
                    self._mutation(
                        "PATCH",
                        f"/v3/config/paths/patch/{path}",
                        {"record": before[path].recording_enabled and not self._disk_full},
                    )
                except Exception:
                    failed = True
            try:
                self._write_media()
            except Exception:
                failed = True
            if failed:
                # Recovery restarts with the previous durable intent; never
                # advertise a successful preference when compensation failed.
                self._needs_reconcile = True
                self._ready = False
            raise OperatorProductError("Recording preference could not be applied") from None

    async def apply_recording_preferences(self, sources: tuple[MediaMTXSource, ...]) -> None:
        """Join the native mutation before cancellation can attempt compensation."""
        async with self._lock:
            previous = self._sources
            try:
                await self._spawn(lambda: self._change_recording_preferences(tuple(sources)))
            except asyncio.CancelledError:
                try:
                    await self._spawn(lambda: self._change_recording_preferences(previous))
                except Exception:
                    self._sources = previous
                    self._needs_reconcile = True
                    self._ready = False
                raise

    async def _maintain(self) -> None:
        last_storage = 0.0
        last_health = 0.0
        while not self._closing:
            await asyncio.sleep(2)
            self._clock.observe()
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
                            except Exception as error:
                                if not (
                                    component == "media"
                                    and isinstance(error, MediaWorkerError)
                                    and error.code is MediaWorkerErrorCode.BUSY
                                ):
                                    self._health_failures[component] += 1
                        last_health = time.monotonic()
                    now = time.monotonic()
                    interest_age = (
                        None
                        if self._analytics_inventory_interest_at is None
                        else now - self._analytics_inventory_interest_at
                    )
                    refresh_seconds = (
                        _STORAGE_ANALYTICS_REFRESH_SECONDS
                        if interest_age is not None
                        and 0 <= interest_age <= _STORAGE_ANALYTICS_INTEREST_SECONDS
                        else _STORAGE_IDLE_REFRESH_SECONDS
                    )
                    if now - last_storage >= refresh_seconds:
                        result = await self._storage_operation(self._storage.prune)
                        await self._apply_storage_status(result)
                        last_storage = time.monotonic()
                    self._ready = not any(self._health_failures.values())
                    if self._ready:
                        await self._retry_camera_source()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._ready = False
                await asyncio.sleep(3)

    def _flush_recordings(self, paths: tuple[str, ...]) -> None:
        """Close active fMP4 writers through only the owned worker's private API.

        One thread spends a shared request budget across all paths; no queued
        work can issue late requests after the worker releases its API port.
        This changes ephemeral worker state only, leaving saved camera settings
        and the next-launch configuration intact. Failed flushes still fall back
        to the existing bounded native process cleanup.
        """
        deadline = time.monotonic() + _RECORDING_FLUSH_SECONDS
        for path in paths:
            if self._media is None or self._media.poll() is not None:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                self._mutation(
                    "PATCH", f"/v3/config/paths/patch/{path}", {"record": False}, timeout=remaining
                )
            except Exception:
                pass

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
        cancelled = False
        for operation in tuple(self._source_refreshes):
            try:
                await self._join_source_task(operation)
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    cancelled = True
            except Exception:
                pass
        for operation in tuple(self._storage_operations):
            while not operation.done():
                try:
                    await asyncio.shield(operation)
                except asyncio.CancelledError:
                    cancelled = True
                except Exception:
                    break
        cleanup_failed = False
        close_provider = asyncio.create_task(self.provider.aclose())
        # A canceled camera-list reader may leave its shared snapshot owner
        # finishing a native read. Close and join that boundary before the
        # worker's authenticated listener can be released or reused.
        while not close_provider.done():
            try:
                await asyncio.shield(close_provider)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                break
        try:
            close_provider.result()
        except Exception:
            cleanup_failed = True
        if self._paths and self._media is not None and self._media.poll() is None:
            flush = asyncio.create_task(
                asyncio.to_thread(self._flush_recordings, tuple(self._paths))
            )
            # Cancellation cannot abandon a credential-bearing native request.
            # Join it before process cleanup and before this API port is reusable.
            while not flush.done():
                try:
                    await asyncio.shield(flush)
                except asyncio.CancelledError:
                    cancelled = True
                except Exception:
                    break
            try:
                flush.result()
            except Exception:
                pass
        # Recorders are flushed and native readers joined before this point.
        # Stop the two independent owned jobs together: serial grace windows
        # unnecessarily delay the installed app's clean exit and restart.
        processes = tuple(
            process for process in (self._gateway, self._media) if process is not None
        )
        if processes:
            stopped = asyncio.gather(
                *(asyncio.to_thread(process.stop) for process in processes),
                return_exceptions=True,
            )
            # A cancelled shutdown still owns both workers until both stop
            # attempts finish. Never abandon a background thread at lease release.
            while not stopped.done():
                try:
                    await asyncio.shield(stopped)
                except asyncio.CancelledError:
                    cancelled = True
            if any(isinstance(result, BaseException) for result in stopped.result()):
                cleanup_failed = True
        if cleanup_failed:
            raise OperatorProductError("Native operator cleanup was incomplete") from None
        self._gateway = self._media = None
        if cancelled:
            raise asyncio.CancelledError
