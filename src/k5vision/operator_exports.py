"""Owned, verified MP4 exports, separate from the recorded-video player.

The native recorder assembles the selected interval. K5 keeps a completed private
file before offering a download, so a browser never mistakes a donor error page
or interrupted response for a finished MP4. No camera address is accepted here.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import os
import re
import stat
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import islice
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener
from uuid import UUID, uuid4

from k5vision.identity_state import _regular_file
from k5vision.media.mediamtx_provider import MediaMTXBinding, _NoRedirect
from k5vision.operator_product_service import _private_configuration, _private_directory

MAX_EXPORT_SECONDS = 3600
MAX_EXPORT_BYTES = 256 * 1024 * 1024
MAX_RETAINED_BYTES = 1024 * 1024 * 1024
MAX_EXPORT_JOBS = 16
EXPORT_LEASE_SECONDS = 24 * 60 * 60
_MAX_METADATA_BYTES = 4096
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})")
_TEMP_METADATA = re.compile(r"\.operator-[0-9a-f]{32}\.tmp")


class ExportError(RuntimeError):
    """A fixed operator-facing failure, without native URLs or exception text."""


def export_start(value: str) -> datetime:
    if not isinstance(value, str) or len(value) > 40 or _TIMESTAMP.fullmatch(value) is None:
        raise ValueError("Invalid export time")
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Invalid export time")
    return result.astimezone(UTC)


def _duration(value: float) -> float:
    if type(value) not in (float, int) or not math.isfinite(value):
        raise ValueError("Invalid export duration")
    if not 1 <= value <= MAX_EXPORT_SECONDS:
        raise ValueError("Invalid export duration")
    return float(value)


def _filename(start: datetime) -> str:
    return "K5-recording-" + start.strftime("%Y-%m-%dT%H-%M-%SZ") + ".mp4"


def _verify_mp4(path: Path, expected_duration: float | None = None) -> None:
    """Require a complete MP4 container and actual first/last video decoding."""
    size = _regular_file(path).st_size
    if not 32 <= size <= MAX_EXPORT_BYTES:
        raise ValueError
    boxes: set[bytes] = set()
    with path.open("rb") as source:
        position = 0
        count = 0
        while position < size:
            count += 1
            if count > 100000 or size - position < 8:
                raise ValueError
            source.seek(position)
            length, kind = struct.unpack(">I4s", source.read(8))
            header = 8
            if length == 1:
                if size - position < 16:
                    raise ValueError
                length = struct.unpack(">Q", source.read(8))[0]
                header = 16
            elif length == 0:
                length = size - position
            if length < header or length > size - position:
                raise ValueError
            boxes.add(kind)
            position += length
    if not {b"ftyp", b"moov", b"mdat"}.issubset(boxes):
        raise ValueError
    # Decoder calls run in an owned, killable interpreter. A malformed codec
    # cannot leave an unjoinable native thread behind during product shutdown.
    from k5vision.operator_native_process import _WindowsJob

    process = None
    ownership = _WindowsJob() if os.name == "nt" else None
    try:
        process = subprocess.Popen(
            [sys.executable, "-B", "-m", "k5vision.operator_export_validator"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            start_new_session=os.name != "nt",
        )
        if ownership is not None:
            ownership.assign(process)
        # Child waits for this request before importing or invoking the decoder.
        try:
            output, _ = process.communicate(
                json.dumps({"path": str(path)}).encode("utf-8"), timeout=10
            )
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=2)
            raise ValueError("Export decoder deadline exceeded") from None
        if process.returncode != 0 or len(output) > 4096:
            raise ValueError
        result = json.loads(output)
        count, fps, duration = result.get("frame_count"), result.get("fps"), result.get("duration")
        tail = result.get("tail_time")
        leading = result.get("leading_offset")
        if (
            result.get("first") is not True
            or result.get("last") is not True
            or type(count) not in (int, float)
            or type(fps) not in (int, float)
            or type(duration) not in (int, float)
            or type(tail) not in (int, float)
            or type(leading) not in (int, float)
            or not math.isfinite(count)
            or count < 1
            or not math.isfinite(fps)
            or fps <= 0
            or not math.isfinite(duration)
            or not 0 < duration <= 3605
            or not math.isfinite(tail)
            or not math.isfinite(leading)
            or not 0 <= leading < duration
            or not max(0, duration - leading - max(0.1, 1.5 / fps))
            <= tail
            <= duration - leading + 1 / fps
        ):
            raise ValueError
        if expected_duration is not None:
            tolerance = max(0.1, 1 / fps)
            if abs(duration - expected_duration) > tolerance:
                raise ValueError
    finally:
        try:
            if ownership is not None:
                ownership.close()
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate(timeout=2)


def _download(
    service,
    binding: MediaMTXBinding,
    start: datetime,
    duration: float,
    destination: Path,
    progress: Callable[[int], None],
    stopped: threading.Event,
) -> None:
    credentials = service.credentials
    basic = base64.b64encode(
        f"{credentials.username}:{credentials.password}".encode("ascii")
    ).decode("ascii")
    query = urlencode(
        {
            "path": binding.path_name,
            "start": start.isoformat(),
            "duration": duration,
            "format": "mp4",
        }
    )
    request = Request(
        f"http://127.0.0.1:{service.ports['playback_port']}/get?{query}",
        headers={"Authorization": "Basic " + basic, "Accept": "video/mp4"},
    )
    deadline = time.monotonic() + 90
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        try:
            response = opener.open(request, timeout=5)
        except HTTPError as error:
            error.close()
            raise ValueError from None
        with response:
            if response.status != 200:
                raise ValueError
            declared = response.headers.get("Content-Length")
            expected = None if declared is None else int(declared)
            if expected is not None:
                if expected > MAX_EXPORT_BYTES:
                    raise ExportError("export_too_large")
                if expected < 32:
                    raise ValueError
            total = 0
            while True:
                if stopped.is_set() or time.monotonic() > deadline:
                    raise ValueError
                # read1 returns after one bounded socket read, so an upstream
                # trickling bytes cannot keep a large read alive past deadline.
                block = response.read1(65536)
                if not block:
                    break
                total += len(block)
                if total > MAX_EXPORT_BYTES:
                    raise ExportError("export_too_large")
                output.write(block)
                progress(total)
            if expected is not None and total != expected:
                raise ValueError
            output.flush()
            os.fsync(output.fileno())


@dataclass(slots=True)
class ExportJob:
    id: UUID
    owner: UUID
    camera: UUID
    start: datetime
    duration: float
    created: datetime
    expires: float
    status: str = "queued"
    phase: str = "queued"
    bytes: int = 0
    sha256: str | None = None
    error: str | None = None
    identity: tuple[int, int, int, int] | None = field(default=None, repr=False)
    readers: int = field(default=0, repr=False)

    def public(self) -> dict:
        return {
            "id": str(self.id),
            "status": self.status,
            "phase": self.phase,
            "bytes": self.bytes,
            "error": self.error,
            "start": self.start.isoformat(),
            "duration": self.duration,
            "filename": _filename(self.start),
            "download_url": f"/media/exports/{self.camera}/{self.id}.mp4"
            if self.status == "ready"
            else None,
        }


class ExportManager:
    """At most two preparations and a bounded private cache of completed files."""

    def __init__(
        self,
        root: Path,
        *,
        service,
        downloader: Callable = _download,
        validator: Callable = _verify_mp4,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.root = _private_directory(root)
        self.service = service
        self._downloader = downloader
        self._validator = validator
        self._clock = clock
        self._jobs: dict[UUID, ExportJob] = {}
        self._tasks: set[asyncio.Task] = set()
        self._verifications: dict[UUID, asyncio.Task] = {}
        self._orphans: list[Path] = []
        self._cache_unsafe = False
        self._stopped = threading.Event()
        self._closed = False
        self._load()

    def _path(self, job: ExportJob, suffix: str = ".mp4") -> Path:
        return self.root / (str(job.id) + suffix)

    @staticmethod
    def _identity(path: Path) -> tuple[int, int, int, int]:
        info = _regular_file(path)
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns

    def _save(self, job: ExportJob) -> None:
        _private_configuration(
            self._path(job, ".json"),
            json.dumps(
                {
                    "schema_version": 1,
                    "site_id": self.service.site_id,
                    "id": str(job.id),
                    "owner": str(job.owner),
                    "camera": str(job.camera),
                    "start": job.start.isoformat(),
                    "duration": job.duration,
                    "created": job.created.isoformat(),
                    "bytes": job.bytes,
                    "sha256": job.sha256,
                },
                separators=(",", ":"),
            ),
        )

    def _load(self) -> None:
        # Cache retention is an elapsed lease. Reopening grants a fresh bounded
        # lease rather than deleting footage because the PC clock moved forward.
        # Capacity remains strictly bounded across any number of restarts.
        # Read-only until the product lifespan has acquired its profile lock.
        # Hashing and decoding are deferred to an async request, never run on
        # the event loop or against another running instance's owned cache.
        entries = list(islice(self.root.iterdir(), 257))
        if len(entries) > 256:
            self._cache_unsafe = True
            return
        owned = []
        for path in entries:
            try:
                if _TEMP_METADATA.fullmatch(path.name):
                    _regular_file(path)
                    owned.append(path)
                    continue
                if path.suffix not in {".json", ".mp4", ".part"}:
                    raise ValueError
                if str(UUID(path.stem)) != path.stem:
                    raise ValueError
                _regular_file(path)
                owned.append(path)
            except Exception:
                self._cache_unsafe = True
        for path in sorted(item for item in owned if item.suffix == ".json"):
            try:
                if _regular_file(path).st_size > _MAX_METADATA_BYTES:
                    raise ValueError
                document = json.loads(path.read_text(encoding="utf-8"))
                job = ExportJob(
                    id=UUID(document["id"]),
                    owner=UUID(document["owner"]),
                    camera=UUID(document["camera"]),
                    start=export_start(document["start"]),
                    duration=_duration(document["duration"]),
                    created=export_start(document["created"]),
                    expires=self._clock() + EXPORT_LEASE_SECONDS,
                    status="ready",
                    phase="ready",
                    bytes=document["bytes"],
                    sha256=document["sha256"],
                )
                if (
                    document.get("schema_version") != 1
                    or document.get("site_id") != self.service.site_id
                    or str(job.id) + ".json" != path.name
                    or type(job.bytes) is not int
                    or not 32 <= job.bytes <= MAX_EXPORT_BYTES
                    or not isinstance(job.sha256, str)
                    or len(job.sha256) != 64
                ):
                    raise ValueError
                media = self._path(job)
                identity = self._identity(media)
                if identity[2] != job.bytes:
                    raise ValueError
                self._jobs[job.id] = job
            except Exception:
                # Never follow an unknown name from malformed metadata. Only
                # fixed UUID-owned cache names are eligible for later cleanup.
                continue
        retained = {
            self._path(job, suffix) for job in self._jobs.values() for suffix in (".json", ".mp4")
        }
        self._orphans = [path for path in owned if path not in retained]

    def _remove(self, job: ExportJob) -> bool:
        if job.readers:
            return False
        failed = False
        for suffix in (".json", ".mp4", ".part"):
            path = self._path(job, suffix)
            try:
                if path.exists():
                    _regular_file(path)
                    path.unlink()
            except OSError:
                failed = True
        if failed:
            return False
        self._jobs.pop(job.id, None)
        return True

    def _prune(self, *, reserve: bool) -> None:
        if self._cache_unsafe:
            raise ExportError("export_cache_unavailable")
        # Requests arrive after the native product owns the profile lock.
        while self._orphans:
            path = self._orphans[-1]
            try:
                if path.exists():
                    _regular_file(path)
                    path.unlink()
            except Exception:
                raise ExportError("export_cache_unavailable") from None
            self._orphans.pop()
        now = self._clock()
        for job in tuple(self._jobs.values()):
            if job.status not in {"preparing", "queued"} and job.expires <= now:
                self._remove(job)
        requested = MAX_EXPORT_BYTES if reserve else 0
        while self._jobs:
            total = sum(
                MAX_EXPORT_BYTES if job.status in {"preparing", "queued"} else job.bytes
                for job in self._jobs.values()
            )
            count_limit = MAX_EXPORT_JOBS - (1 if reserve else 0)
            if total + requested <= MAX_RETAINED_BYTES and len(self._jobs) <= count_limit:
                break
            candidates = [
                job
                for job in self._jobs.values()
                if job.status in {"ready", "failed"} and not job.readers
            ]
            if not candidates:
                raise ExportError("export_capacity")
            if not self._remove(min(candidates, key=lambda job: job.expires)):
                raise ExportError("export_capacity")

    async def create(
        self,
        *,
        owner: UUID,
        binding: MediaMTXBinding,
        start: datetime,
        duration: float,
        authorize: Callable | None = None,
    ) -> ExportJob:
        duration = _duration(duration)
        if (
            not isinstance(owner, UUID)
            or not isinstance(binding, MediaMTXBinding)
            or binding.site_id != self.service.site_id
            or not isinstance(start, datetime)
            or start.utcoffset() is None
        ):
            raise ValueError("Invalid export selection")
        start = start.astimezone(UTC)
        end = start + timedelta(seconds=duration)
        if self._closed:
            raise ExportError("export_unavailable")
        spans = await self.service.provider.playback_spans(binding, start=start, end=end)
        if authorize is not None:
            await authorize()
        cursor = start
        for span in sorted(spans, key=lambda item: item.started_at):
            span_end = span.started_at + timedelta(seconds=span.duration_seconds)
            if span.started_at > cursor + timedelta(milliseconds=1):
                break
            cursor = max(cursor, span_end)
        if cursor < end - timedelta(milliseconds=1):
            raise ExportError("export_recording_gap")
        if self._closed:
            raise ExportError("export_unavailable")
        if len(self._tasks) >= 2:
            raise ExportError("export_busy")
        self._prune(reserve=True)
        job = ExportJob(
            id=uuid4(),
            owner=owner,
            camera=binding.device_id,
            start=start,
            duration=duration,
            created=datetime.now(UTC),
            expires=self._clock() + EXPORT_LEASE_SECONDS,
        )
        self._jobs[job.id] = job
        task = asyncio.create_task(self._prepare(job, binding))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    def get(self, identifier: UUID, *, owner: UUID, camera: UUID) -> ExportJob | None:
        self._prune(reserve=False)
        job = self._jobs.get(identifier)
        if (
            job is None
            or job.owner != owner
            or job.camera != camera
            or job.expires <= self._clock()
        ):
            return None
        return job

    async def verify_ready(self, job: ExportJob) -> None:
        """Re-admit a persisted completed export before reporting it ready."""
        if job.status != "ready" or job.identity is not None:
            return
        job.phase = "validating"

        def verify():
            path = self._path(job)
            before = self._identity(path)
            if before[2] != job.bytes:
                raise ValueError
            with path.open("rb") as source:
                if hashlib.file_digest(source, "sha256").hexdigest() != job.sha256:
                    raise ValueError
            self._validator(path, job.duration)
            if self._identity(path) != before:
                raise ValueError
            return before

        operation = self._verifications.get(job.id)
        if operation is None:
            if len(self._verifications) >= 2:
                raise ExportError("export_busy")
            operation = asyncio.create_task(asyncio.to_thread(verify))
            self._verifications[job.id] = operation

            def completed(task):
                if self._verifications.get(job.id) is task:
                    del self._verifications[job.id]
                if not task.cancelled():
                    task.exception()

            operation.add_done_callback(completed)
        try:
            job.identity = await asyncio.shield(operation)
            job.phase = "ready"
        except asyncio.CancelledError:
            raise
        except Exception:
            job.status = "failed"
            job.error = "export_file_unavailable"
        finally:
            if operation.done():
                self._verifications.pop(job.id, None)

    def file(self, job: ExportJob) -> Path:
        path = self._path(job)
        if job.status != "ready" or self._identity(path) != job.identity:
            raise ExportError("export_file_unavailable")
        return path

    async def open_file(self, job: ExportJob):
        await self.verify_ready(job)
        path = self.file(job)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(descriptor)
            identity = info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or identity != job.identity
                or self._identity(path) != identity
            ):
                raise ExportError("export_file_unavailable")
            source = os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise
        job.readers += 1
        return source

    def release_file(self, job: ExportJob, source) -> None:
        source.close()
        job.readers = max(0, job.readers - 1)

    async def _prepare(self, job: ExportJob, binding: MediaMTXBinding) -> None:
        temporary = self._path(job, ".part")
        final = self._path(job)
        job.status = "preparing"
        job.phase = "downloading"

        def prepare():
            self._downloader(
                self.service,
                binding,
                job.start,
                job.duration,
                temporary,
                lambda count: setattr(job, "bytes", count),
                self._stopped,
            )
            if self._stopped.is_set():
                raise ValueError
            job.phase = "validating"
            self._validator(temporary, job.duration)
            identity = self._identity(temporary)
            if identity[2] != job.bytes or not 32 <= job.bytes <= MAX_EXPORT_BYTES:
                raise ValueError
            with temporary.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            job.phase = "persisting"
            os.replace(temporary, final)
            job.sha256 = digest
            job.identity = self._identity(final)
            self._save(job)

        worker = asyncio.create_task(asyncio.to_thread(prepare))
        cancelled = False
        failed = False
        failure_code = "export_preparation_failed"
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancelled = True
            self._stopped.set()
            try:
                await asyncio.shield(worker)
            except Exception:
                failed = True
        except ExportError as error:
            failed = True
            if str(error) == "export_too_large":
                failure_code = "export_too_large"
        except Exception:
            failed = True
        if failed or cancelled:
            job.status = "failed"
            job.error = failure_code
            job.bytes = 0
            for path in (temporary, final, self._path(job, ".json")):
                try:
                    if path.exists():
                        _regular_file(path)
                        path.unlink()
                except OSError:
                    pass
        else:
            job.status = "ready"
            job.phase = "ready"
        if cancelled:
            raise asyncio.CancelledError

    async def aclose(self) -> None:
        self._closed = True
        self._stopped.set()
        if self._tasks:
            await asyncio.gather(*(asyncio.shield(task) for task in self._tasks))
        if self._verifications:
            await asyncio.gather(
                *(asyncio.shield(task) for task in self._verifications.values()),
                return_exceptions=True,
            )
