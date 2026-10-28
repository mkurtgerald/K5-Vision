"""Background person events from owned recordings, independent of browser views.

Only native recorder files may supply evidence. Event UTC is the recording's
explicit UTC filename plus decoded presentation time, never browser or inference
wall time. Scheduling, event coalescing and source progress use monotonic time.
Decoded pixels stay transient; durable data contains settings and event metadata.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from k5vision import identity_state
from k5vision.media.presentation_frame import PixelFormat, PresentationVideoFrame
from k5vision.operator_analytics_person import OperatorPersonConfiguration
from k5vision.operator_recording_storage import _SEGMENT_NAME

SAMPLE_INTERVAL_SECONDS = 2.0
EVENT_INTERVAL_SECONDS = 10.0
MAX_EVENT_ROWS = 100_000
MAX_RECORDING_ENTRIES = 100_000
MAX_RECORDING_BYTES = 128 * 1024 * 1024
_NATIVE_REQUEST_SECONDS = 8.0
_DATABASE = "operator-events.sqlite3"


class OperatorEventsError(RuntimeError):
    """Source-free failure for settings, event storage or owned processing."""


def _utc(value: datetime) -> str:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("An aware recording time is required")
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _settings(enabled: bool, confidence: float) -> None:
    if (
        type(enabled) is not bool
        or type(confidence) not in (float, int)
        or not math.isfinite(confidence)
        or not 0.5 <= confidence <= 0.99
    ):
        raise ValueError("Analytics settings were refused")


class OperatorEventStore:
    """Private, site-bound settings and bounded append-only event identities."""

    def __init__(self, root: Path, site_id: str) -> None:
        self._lock = threading.RLock()
        self._connection = None
        try:
            state = identity_state.load_identity_state(root)
            if state.site_id != site_id:
                raise ValueError
            self._root = state.directory
            self._root_identity = self._root.lstat()
            self._path = self._root / _DATABASE
            created = False
            try:
                fd = os.open(
                    self._path,
                    os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
            except FileExistsError:
                pass
            else:
                os.close(fd)
                created = True
            self._identity = self._private_file(self._path)
            self._admit()
            connection = sqlite3.connect(
                self._path.as_uri() + "?mode=rw",
                uri=True,
                isolation_level=None,
                check_same_thread=False,
                timeout=3,
            )
            self._connection = connection
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA synchronous=FULL")
            if created:
                connection.executescript(
                    "BEGIN IMMEDIATE;"
                    "CREATE TABLE event_state (schema_version INTEGER, site_id TEXT);"
                    "CREATE TABLE camera_analytics ("
                    "camera_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL CHECK(enabled IN(0,1)),"
                    "confidence REAL NOT NULL CHECK(confidence>=0.5 AND confidence<=0.99),"
                    "last_frame_key TEXT);"
                    "CREATE TABLE person_events (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "camera_id TEXT NOT NULL, occurred_at TEXT NOT NULL, confidence REAL NOT NULL,"
                    "person_count INTEGER NOT NULL, frame_key TEXT NOT NULL,"
                    "UNIQUE(camera_id,frame_key));"
                    "CREATE INDEX event_camera_time ON person_events(camera_id,occurred_at,id);"
                )
                connection.execute("INSERT INTO event_state VALUES(1,?)", (site_id,))
                connection.execute("COMMIT")
            rows = connection.execute("SELECT * FROM event_state").fetchall()
            if len(rows) != 1 or tuple(rows[0]) != (1, site_id):
                raise ValueError
            for row in connection.execute("SELECT * FROM camera_analytics"):
                UUID(row["camera_id"])
                _settings(bool(row["enabled"]), row["confidence"])
                if row["enabled"] not in (0, 1):
                    raise ValueError
            self._admit()
        except Exception:
            self.close()
            raise OperatorEventsError("Analytics state admission failed") from None

    @staticmethod
    def _private_file(path: Path):
        info = identity_state._regular_file(path)
        if os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise ValueError
        return info

    def _admit(self) -> None:
        identity_state._same_directory(self._root, self._root_identity)
        current = self._private_file(self._path)
        if (current.st_dev, current.st_ino) != (self._identity.st_dev, self._identity.st_ino):
            raise ValueError
        for suffix in ("-journal", "-wal", "-shm"):
            path = Path(str(self._path) + suffix)
            if path.exists() or path.is_symlink():
                self._private_file(path)

    def _execute(self, operation):
        with self._lock:
            try:
                if self._connection is None:
                    raise ValueError
                self._admit()
                result = operation(self._connection)
                self._admit()
                return result
            except Exception:
                if self._connection is not None and self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise OperatorEventsError("Analytics state unavailable") from None

    def settings(self, camera_id: UUID) -> dict:
        def read(connection):
            row = connection.execute(
                "SELECT enabled,confidence FROM camera_analytics WHERE camera_id=?",
                (str(camera_id),),
            ).fetchone()
            return {
                "enabled": False if row is None else bool(row[0]),
                "confidence_threshold": 0.65 if row is None else row[1],
            }

        return self._execute(read)

    def configure(self, camera_id: UUID, *, enabled: bool, confidence_threshold: float) -> dict:
        _settings(enabled, confidence_threshold)

        def write(connection):
            connection.execute(
                "INSERT INTO camera_analytics(camera_id,enabled,confidence) VALUES(?,?,?) "
                "ON CONFLICT(camera_id) DO UPDATE SET enabled=excluded.enabled,"
                "confidence=excluded.confidence",
                (str(camera_id), int(enabled), confidence_threshold),
            )

        self._execute(write)
        return self.settings(camera_id)

    def record(
        self,
        camera_id: UUID,
        *,
        frame_key: str,
        occurred_at: datetime,
        confidence: float,
        person_count: int,
        emit: bool,
    ) -> bool:
        if (
            not isinstance(camera_id, UUID)
            or not isinstance(frame_key, str)
            or len(frame_key) != 64
            or any(char not in "0123456789abcdef" for char in frame_key)
            or type(confidence) not in (float, int)
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
            or type(person_count) is not int
            or not 0 <= person_count <= 32
            or type(emit) is not bool
        ):
            raise ValueError("Analytics frame metadata was refused")
        stamp = _utc(occurred_at)

        def write(connection):
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT enabled,last_frame_key FROM camera_analytics WHERE camera_id=?",
                (str(camera_id),),
            ).fetchone()
            if row is None or not row[0] or row[1] == frame_key:
                connection.execute("COMMIT")
                return False
            connection.execute(
                "UPDATE camera_analytics SET last_frame_key=? WHERE camera_id=?",
                (frame_key, str(camera_id)),
            )
            inserted = False
            if emit and person_count:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO person_events"
                    "(camera_id,occurred_at,confidence,person_count,frame_key) VALUES(?,?,?,?,?)",
                    (str(camera_id), stamp, confidence, person_count, frame_key),
                )
                inserted = cursor.rowcount == 1
                # Prune by durable append identity, never by potentially changed UTC.
                connection.execute(
                    "DELETE FROM person_events WHERE id <= "
                    "COALESCE((SELECT id FROM person_events ORDER BY id DESC LIMIT 1 OFFSET ?),0)",
                    (MAX_EVENT_ROWS,),
                )
            connection.execute("COMMIT")
            return inserted

        return self._execute(write)

    def events(
        self,
        camera_id: UUID,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 50,
        before: int | None = None,
    ) -> dict:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Analytics event limit was refused")
        if before is not None and (type(before) is not int or before < 1):
            raise ValueError("Analytics event cursor was refused")
        if (start is None) != (end is None) or (
            start is not None and not timedelta(0) < end - start <= timedelta(hours=24)
        ):
            raise ValueError("Analytics event range was refused")

        def read(connection):
            terms, arguments = ["camera_id=?"], [str(camera_id)]
            if start is not None:
                terms.extend(("occurred_at>=?", "occurred_at<?"))
                arguments.extend((_utc(start), _utc(end)))
            if before is not None:
                terms.append("id<?")
                arguments.append(before)
            rows = connection.execute(
                "SELECT id,camera_id,occurred_at,confidence,person_count FROM person_events WHERE "
                + " AND ".join(terms)
                + " ORDER BY id DESC LIMIT ?",
                (*arguments, limit + 1),
            ).fetchall()
            items = [{**dict(row), "playback_at": row["occurred_at"]} for row in rows[:limit]]
            more = len(rows) > limit
            return {
                "events": items,
                "has_more": more,
                "next_before": items[-1]["id"] if items and more else None,
            }

        return self._execute(read)

    def summary(self, camera_id: UUID) -> dict:
        def read(connection):
            row = connection.execute(
                "SELECT COUNT(*),MAX(id) FROM person_events WHERE camera_id=?", (str(camera_id),)
            ).fetchone()
            latest = (
                None
                if row[1] is None
                else connection.execute(
                    "SELECT occurred_at FROM person_events WHERE id=?", (row[1],)
                ).fetchone()[0]
            )
            return {"event_count": row[0], "last_event_at": latest}

        return self._execute(read)

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None


@dataclass(frozen=True, slots=True)
class RecordedPersonFrame:
    frame: PresentationVideoFrame
    occurred_at: datetime
    frame_key: str
    detections: tuple[object, ...] | None = None


class _RecordedNativeWorker:
    """One persistent, killable decoder/detector process with bounded requests.

    The child reads no request until Windows job assignment finishes. Paths go
    only through its private stdin pipe. A stalled native call is killed and
    reaped before another process may start; no abandoned inference threads exist.
    """

    def __init__(self, configuration: OperatorPersonConfiguration | None):
        self._configuration = configuration
        self._process = None
        self._job = None
        self._lock = threading.RLock()
        self._closing = threading.Event()

    def _start(self) -> None:
        from k5vision.operator_native_process import _WindowsJob

        if self._closing.is_set():
            raise OperatorEventsError("Analytics worker is closed")
        if self._process is not None and self._process.poll() is None:
            return
        self.close(final=False)
        job = _WindowsJob() if os.name == "nt" else None
        process = None
        try:
            process = subprocess.Popen(
                [sys.executable, "-B", "-m", "k5vision.operator_events_decoder"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                start_new_session=os.name != "nt",
                close_fds=True,
            )
            if job is not None:
                job.assign(process)
            self._process, self._job = process, job
        except BaseException:
            if process is not None:
                process.kill()
                process.wait(timeout=2)
            if job is not None:
                job.close()
            raise OperatorEventsError("Analytics worker ownership unavailable") from None

    def request(self, path: Path):
        with self._lock:
            if self._closing.is_set():
                raise OperatorEventsError("Analytics worker is closed")
            self._start()
            process = self._process
            if self._closing.is_set():
                self.close()
                raise OperatorEventsError("Analytics worker is closed")
            expired = threading.Event()

            def deadline():
                expired.set()
                try:
                    process.kill()
                except OSError:
                    pass

            timer = threading.Timer(_NATIVE_REQUEST_SECONDS, deadline)
            timer.daemon = True
            timer.start()
            failed = False
            try:
                request = {
                    "path": str(path),
                    "artifact_root": None
                    if self._configuration is None
                    else str(self._configuration.artifact_root),
                }
                process.stdin.write(json.dumps(request).encode("utf-8") + b"\n")
                process.stdin.flush()
                raw = process.stdout.readline(4097)
                if not raw.endswith(b"\n") or len(raw) > 4096:
                    raise ValueError
                header = json.loads(raw)
                if header.get("status") == "empty":
                    return None
                if header.get("status") != "ready" or header.get("bytes") != 544 * 320 * 4:
                    raise ValueError
                payload = process.stdout.read(544 * 320 * 4)
                if len(payload) != 544 * 320 * 4:
                    raise ValueError
                elapsed = header["elapsed"]
                if (
                    type(elapsed) not in (int, float)
                    or not math.isfinite(elapsed)
                    or not 0 <= elapsed <= 3605
                ):
                    raise ValueError
                boxes = header.get("detections")
                detections = None
                if boxes is not None:
                    if type(boxes) is not list or len(boxes) > 32:
                        raise ValueError
                    detections = []
                    for box in boxes:
                        confidence = box["confidence"]
                        if (
                            box.get("category") != "person"
                            or type(confidence) not in (float, int)
                            or not math.isfinite(confidence)
                            or not 0.5 <= confidence <= 1
                        ):
                            raise ValueError
                        detections.append(SimpleNamespace(category="person", confidence=confidence))
                    detections = tuple(detections)
                return elapsed, payload, detections
            except Exception:
                failed = True
                raise OperatorEventsError("Analytics native frame unavailable") from None
            finally:
                timer.cancel()
                timer.join()
                if failed or expired.is_set():
                    self.close(final=False)

    def begin_close(self) -> None:
        self._closing.set()
        process = self._process
        if process is not None:
            try:
                process.kill()
            except OSError:
                pass

    def close(self, *, final: bool = True) -> None:
        if final:
            self.begin_close()
        with self._lock:
            process, self._process = self._process, None
            job, self._job = self._job, None
            try:
                if process is not None:
                    if process.stdin is not None:
                        try:
                            process.stdin.close()
                        except (BrokenPipeError, OSError):
                            pass
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
                    if process.stdout is not None:
                        process.stdout.close()
            finally:
                if job is not None:
                    job.close()


class RecordedFrameSampler:
    """Sample growing owned files; clock rollback cannot select an old future file.

    A cold scan establishes file identities only. Subsequent byte progress selects
    the current recorder file by monotonic observation, not filename or mtime age.
    The immutable snapshot is bounded, and only MP4 bytes reach the native decoder.
    """

    def __init__(self, root: Path, *, scratch_root: Path | None = None, recording_files=None):
        self._root = root
        self._root_identity = root.lstat()
        self._scratch = scratch_root or root.parent / "analytics-samples"
        self._scratch.mkdir(mode=0o700, exist_ok=True)
        self._scratch_identity = self._scratch.lstat()
        identity_state._same_directory(self._scratch, self._scratch_identity)
        if os.name == "posix" and self._scratch_identity.st_mode & 0o077:
            raise OperatorEventsError("Analytics sample directory refused")
        identity_state._windows_admission(self._scratch, private=True, directory=True)
        self._observations: dict[UUID, dict[str, tuple[int, int, int]]] = {}
        self._recording_files = recording_files
        self._active_files: dict[UUID, tuple[str, ...]] = {}
        self._closing = threading.Event()
        self._worker_lock = threading.RLock()

    def prune(self, active: set[UUID]) -> None:
        """Forget removed camera hints without touching retained recording files."""
        self._observations = {
            identifier: value
            for identifier, value in self._observations.items()
            if identifier in active
        }
        self._active_files = {
            identifier: value
            for identifier, value in self._active_files.items()
            if identifier in active
        }

    @staticmethod
    def _started(name: str) -> datetime:
        match = _SEGMENT_NAME.fullmatch(name)
        if match is None or match["offset"] is None:
            raise ValueError
        return datetime.strptime(
            match["stamp"] + match["offset"], "%Y-%m-%d_%H-%M-%S-%f_%z"
        ).astimezone(UTC)

    def capture(self, source) -> RecordedPersonFrame | None:
        if self._closing.is_set():
            return None
        identity_state._same_directory(self._root, self._root_identity)
        directory = self._root / source.binding.path_name
        try:
            directory_info = directory.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISDIR(directory_info.st_mode) or directory.is_symlink():
            raise OperatorEventsError("Analytics recording source refused")
        identity_state._windows_admission(directory, private=True, directory=True)
        previous = self._observations.get(source.binding.device_id)
        current = {}
        changed = []
        if self._recording_files is not None:
            inventory = self._recording_files(source.binding)
            if inventory is None:
                return None
            if type(inventory) is not dict or len(inventory) > MAX_RECORDING_ENTRIES:
                raise OperatorEventsError("Analytics recording scan limit reached")
            candidates = []
            directory_identity = directory_info.st_dev, directory_info.st_ino
            for name, value in inventory.items():
                if (
                    type(name) is not str
                    or _SEGMENT_NAME.fullmatch(name) is None
                    or type(value) is not tuple
                    or len(value) != 7
                    or any(type(item) is not int for item in value)
                    or value[:2] != directory_identity
                    or value[4] < 0
                ):
                    raise OperatorEventsError("Analytics recording inventory refused")
                signature = value[2], value[3], value[4]
                old = None if previous is None else previous.get(name)
                # The storage cache refreshes less often than frame sampling.
                # Preserve the actual active-file byte high-water observation.
                current[name] = (
                    old
                    if old is not None and old[:2] == signature[:2] and old[2] > signature[2]
                    else signature
                )
                if previous is not None and current[name] != old:
                    candidates.append(name)
            if previous is None:
                # Timestamp ordering supplies cold-start hints only. No retained
                # bytes become events until new byte growth is actually observed.
                hints = sorted(inventory, key=lambda name: inventory[name][5], reverse=True)[:2]
                self._active_files[source.binding.device_id] = tuple(hints)
                self._observations[source.binding.device_id] = current
                return None
            candidates.sort(key=lambda name: (name in previous, -current[name][2]))
            candidates = list(
                dict.fromkeys(
                    candidates[:2] + list(self._active_files.get(source.binding.device_id, ()))
                )
            )[:4]
            for name in candidates:
                path = directory / name
                try:
                    info = identity_state._regular_file(path)
                except FileNotFoundError:
                    current.pop(name, None)
                    continue
                signature = info.st_dev, info.st_ino, info.st_size
                if signature != previous.get(name):
                    changed.append((path, info))
                current[name] = signature
        else:
            # Source-only integrations may have no recorder cache. Their fallback
            # has the same inventory ceiling as storage and a finite scan budget.
            deadline = time.monotonic() + 3
            with os.scandir(directory) as entries:
                for index, entry in enumerate(entries):
                    if index >= MAX_RECORDING_ENTRIES or time.monotonic() >= deadline:
                        raise OperatorEventsError("Analytics recording scan limit reached")
                    if _SEGMENT_NAME.fullmatch(entry.name) is None:
                        continue
                    path = directory / entry.name
                    info = identity_state._regular_file(path)
                    signature = (info.st_dev, info.st_ino, info.st_size)
                    current[entry.name] = signature
                    if previous is not None and previous.get(entry.name) != signature:
                        changed.append((path, info))
        self._observations[source.binding.device_id] = current
        if not changed:
            return None
        # Normally one open segment grows. At a rollover, prefer the new file;
        # its bytes belong to the same scan, independent of UTC ordering.
        changed.sort(key=lambda item: (item[0].name in previous, -item[1].st_size))
        self._active_files[source.binding.device_id] = tuple(path.name for path, _ in changed[:2])
        for path, info in changed[:2]:
            if not 1 <= info.st_size <= MAX_RECORDING_BYTES:
                continue
            try:
                with path.open("rb") as stream:
                    raw = stream.read(info.st_size)
                    after = os.fstat(stream.fileno())
                current_info = identity_state._regular_file(path)
                identity = info.st_dev, info.st_ino
                if (
                    len(raw) != info.st_size
                    or (after.st_dev, after.st_ino) != identity
                    or (current_info.st_dev, current_info.st_ino) != identity
                    or after.st_size < info.st_size
                ):
                    continue
                result = self.decode(raw, self._started(path.name), path.name)
                if result is not None:
                    return result
            except (OSError, ValueError):
                continue
        return None

    def decode(
        self, raw: bytes, started: datetime, recording_key: str
    ) -> RecordedPersonFrame | None:
        if self._closing.is_set():
            return None
        if not 1 <= len(raw) <= MAX_RECORDING_BYTES:
            raise ValueError("Analytics recording sample was refused")
        identity_state._same_directory(self._scratch, self._scratch_identity)
        descriptor, name = tempfile.mkstemp(prefix="person-", suffix=".mp4", dir=self._scratch)
        path = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
            identity_state._regular_file(path)
            identity_state._windows_admission(path, private=True)
            with self._worker_lock:
                if self._closing.is_set():
                    return None
                if not hasattr(self, "_worker"):
                    self._worker = _RecordedNativeWorker(getattr(self, "_configuration", None))
                worker = self._worker
            result = worker.request(path)
            if result is None:
                return None
            elapsed, payload, detections = result
            key = hashlib.sha256(f"{recording_key}:{elapsed:.6f}".encode("ascii")).hexdigest()
            return RecordedPersonFrame(
                PresentationVideoFrame(
                    memoryview(payload), 544, 320, 544 * 4, PixelFormat.BGRX, int(elapsed * 1000)
                ),
                started + timedelta(seconds=elapsed),
                key,
                detections,
            )
        finally:
            path.unlink(missing_ok=True)

    def begin_close(self) -> None:
        self._closing.set()
        with self._worker_lock:
            worker = getattr(self, "_worker", None)
            if worker is not None:
                worker.begin_close()

    def close(self) -> None:
        self.begin_close()
        with self._worker_lock:
            worker = getattr(self, "_worker", None)
            if worker is not None:
                worker.close()


class OperatorEventService:
    """One fair bounded sampler/inference loop; recording owns no dependency on it."""

    def __init__(
        self,
        *,
        store: OperatorEventStore,
        cameras,
        recording_root: Path,
        configuration: OperatorPersonConfiguration | None,
        unavailable: bool = False,
        sampler=None,
        recording_files=None,
        provider_factory=None,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.store = store
        self._cameras = cameras
        self._sampler = sampler or RecordedFrameSampler(
            recording_root, recording_files=recording_files
        )
        self._factory = provider_factory
        self._available = configuration is not None or provider_factory is not None
        if isinstance(self._sampler, RecordedFrameSampler):
            self._sampler._configuration = configuration
        self._unavailable = unavailable
        self._clock = monotonic
        self._task = None
        self._closing = False
        self._provider = None
        self._provider_failed = False
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="k5-recorded-person")
        self._native_pending = None
        self._states: dict[UUID, dict] = {}
        self._last_events: dict[UUID, float] = {}
        self._configuration_generation: dict[UUID, int] = {}
        self._rotation = 0

    def status(self, camera_id: UUID) -> dict:
        settings = self.store.settings(camera_id)
        state = self._states.get(camera_id, {})
        status = (
            "disabled"
            if not settings["enabled"]
            else "unavailable"
            if not self._available or self._unavailable
            else state.get("status", "starting")
        )
        return {
            **settings,
            "status": status,
            "last_sample_at": state.get("last_sample_at"),
            "processed_frames": state.get("processed_frames", 0),
            **self.store.summary(camera_id),
            "sample_interval_seconds": SAMPLE_INTERVAL_SECONDS,
            "max_parallel_cameras": 1,
            "source": "recorded_camera_frames",
            "model": "person-detection-retail-0013",
        }

    def configure(self, camera_id: UUID, *, enabled: bool, confidence_threshold: float) -> dict:
        if enabled and (not self._available or self._unavailable):
            raise OperatorEventsError("Analytics runtime unavailable")
        self.store.configure(camera_id, enabled=enabled, confidence_threshold=confidence_threshold)
        self._configuration_generation[camera_id] = (
            self._configuration_generation.get(camera_id, 0) + 1
        )
        self._states.pop(camera_id, None)
        return self.status(camera_id)

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="operator-recorded-person-events")

    async def _sample(self, source, settings) -> None:
        identifier = source.binding.device_id
        generation = self._configuration_generation.get(identifier, 0)
        state = self._states.setdefault(identifier, {"processed_frames": 0})
        if not source.recording_enabled:
            state["status"] = "waiting"
            return
        if self._native_pending is not None and not self._native_pending.done():
            state["status"] = "waiting"
            return
        self._native_pending = self._executor.submit(self._sampler.capture, source)
        wrapped = asyncio.wrap_future(self._native_pending)
        # Native decoder exceptions are sanitized before crossing API/UI boundaries.
        wrapped.add_done_callback(lambda future: None if future.cancelled() else future.exception())
        sample = await asyncio.wait_for(asyncio.shield(wrapped), 12)
        if sample is None:
            state["status"] = "waiting"
            return
        if sample.detections is not None:
            detections = sample.detections
        else:
            if self._factory is None:
                raise OperatorEventsError("Analytics inference unavailable")
            if self._provider is None:
                self._provider = await self._factory()
            detections = await asyncio.wait_for(self._provider(sample.frame), 5)
        current = self.store.settings(identifier)
        selected_sources = tuple(self._cameras.list_sources())
        live_source = next(
            (item for item in selected_sources if item.binding == source.binding), None
        )
        if (
            self._closing
            or not current["enabled"]
            or current != settings
            or self._configuration_generation.get(identifier, 0) != generation
            or live_source is None
            or not live_source.recording_enabled
            or getattr(live_source, "source_uri", None) != getattr(source, "source_uri", None)
        ):
            return
        selected = [
            item
            for item in detections
            if item.category == "person" and item.confidence >= current["confidence_threshold"]
        ]
        now = self._clock()
        emitted = self.store.record(
            identifier,
            frame_key=sample.frame_key,
            occurred_at=sample.occurred_at,
            confidence=max((item.confidence for item in selected), default=0.0),
            person_count=min(32, len(selected)),
            emit=now - self._last_events.get(identifier, -math.inf) >= EVENT_INTERVAL_SECONDS,
        )
        if emitted:
            self._last_events[identifier] = now
        state["status"] = "active"
        state["last_sample_at"] = _utc(sample.occurred_at)
        state["processed_frames"] = min(2**63 - 1, state["processed_frames"] + 1)

    async def cycle(self) -> None:
        if not self._available or self._unavailable or self._closing or self._provider_failed:
            return
        sources = tuple(self._cameras.list_sources())
        if len(sources) > 64:
            raise OperatorEventsError("Analytics camera capacity unavailable")
        if not sources:
            if hasattr(self._sampler, "prune"):
                self._sampler.prune(set())
            self._states.clear()
            self._last_events.clear()
            self._configuration_generation.clear()
            return
        self._rotation %= len(sources)
        ordered = sources[self._rotation :] + sources[: self._rotation]
        self._rotation += 1
        active = {source.binding.device_id for source in sources}
        if hasattr(self._sampler, "prune"):
            self._sampler.prune(active)
        self._states = {key: value for key, value in self._states.items() if key in active}
        self._last_events = {
            key: value for key, value in self._last_events.items() if key in active
        }
        self._configuration_generation = {
            key: value for key, value in self._configuration_generation.items() if key in active
        }
        for source in ordered:
            if self._closing:
                return
            identifier = source.binding.device_id
            settings = self.store.settings(identifier)
            if not settings["enabled"]:
                continue
            try:
                await self._sample(source, settings)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._states.setdefault(identifier, {})["status"] = "unavailable"
                if self._provider is not None:
                    try:
                        await self._provider.aclose()
                    except Exception:
                        # Retain ownership and refuse another launch until real
                        # cleanup succeeds. Production uses the killable worker;
                        # alternate providers obey the same ownership boundary.
                        self._provider_failed = True
                    else:
                        self._provider = None

    async def _run(self) -> None:
        while not self._closing:
            try:
                await self.cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Private state/decoder outages never propagate to the recorder.
                pass
            await asyncio.sleep(SAMPLE_INTERVAL_SECONDS)

    async def aclose(self) -> None:
        self._closing = True
        if hasattr(self._sampler, "begin_close"):
            self._sampler.begin_close()
        incomplete = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._provider is not None:
            provider = self._provider
            try:
                await provider.aclose()
            except Exception:
                incomplete = True
            else:
                self._provider = None
        if self._native_pending is not None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(asyncio.wrap_future(self._native_pending)), 10
                )
            except TimeoutError:
                incomplete = True
            except Exception:
                # A completed rejected decode still relinquishes its real worker.
                pass
        finished = self._native_pending is None or self._native_pending.done()
        try:
            if hasattr(self._sampler, "close"):
                self._sampler.close()
        except Exception:
            incomplete = True
        self._executor.shutdown(wait=finished, cancel_futures=True)
        self.store.close()
        if incomplete:
            raise OperatorEventsError("Analytics native cleanup is incomplete")
