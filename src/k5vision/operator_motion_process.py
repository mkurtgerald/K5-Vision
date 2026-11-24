"""Owned, killable native live sampling; private relay material stays on stdin."""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

_REQUEST_SECONDS = 4.0
_CLOSE_SECONDS = 2.0
_MAX_JPEG = 262144
_MAX_REQUEST = 131072
_MAX_AGE = 2.0
_MAX_PROGRESS = 12
MOTION_PHASES = frozenset(
    {
        "none",
        "worker_start",
        "request_write",
        "response_read",
        "native_import",
        "detector_import",
        "frame_wait",
        "motion_analysis",
        "jpeg_encode",
        "response_write",
        "decoder_open",
        "decoder_read",
        "decoder_frame",
        "ready",
        "recording_gate",
    }
)
MOTION_REASONS = frozenset(
    {
        "none",
        "pending",
        "worker_start",
        "request_io",
        "response_protocol",
        "child_failure",
        "request_deadline",
        "capture_age",
        "frame_payload",
        "worker_closed",
        "worker_cleanup",
        "runtime_failure",
        "recording_gate",
        "not_started",
        "recorder_unavailable",
        "storage_blocked",
        "paused",
        "continuous",
    }
)


def sampling_diagnostic(value):
    """Admit only bounded, source-free cached sampler facts."""
    if type(value) is not dict or set(value) != {
        "state",
        "phase",
        "decoder_phase",
        "reason",
        "attempts",
        "failures",
    }:
        raise ValueError
    if (
        any(type(value[k]) is not str for k in ("state", "phase", "decoder_phase", "reason"))
        or value["state"] not in {"idle", "sampling", "ready", "failed"}
        or value["phase"] not in MOTION_PHASES
        or value["decoder_phase"] not in {"none", "decoder_open", "decoder_read", "decoder_frame"}
        or value["reason"] not in MOTION_REASONS
        or any(
            type(value[k]) is not int or not 0 <= value[k] <= 1000000
            for k in ("attempts", "failures")
        )
        or value["failures"] > value["attempts"]
    ):
        raise ValueError
    return dict(value)


class MotionProcessError(RuntimeError):
    """Fixed diagnostics without credentials, source addresses or native errors."""

    def __init__(self, message, *, phase="none", decoder_phase="none", reason="runtime_failure"):
        super().__init__(message)
        self.phase = phase if phase in MOTION_PHASES else "none"
        self.decoder_phase = (
            decoder_phase
            if decoder_phase in {"decoder_open", "decoder_read", "decoder_frame"}
            else "none"
        )
        self.reason = reason if reason in MOTION_REASONS else "runtime_failure"


@dataclass(frozen=True, slots=True)
class MotionSample:
    width: int
    height: int
    preview_width: int
    preview_height: int
    sequence: int
    motion: bool
    age_seconds: float
    jpeg: bytes = field(repr=False)
    captured_monotonic: float


def _relay(value: object) -> str:
    try:
        parsed = urlsplit(value)
        valid = (
            type(value) is str
            and len(value) <= 1024
            and parsed.scheme == "rtsp"
            and parsed.hostname == "127.0.0.1"
            and parsed.port is not None
            and 1 <= parsed.port <= 65535
            and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", parsed.username or "") is not None
            and parsed.username != "any"
            and re.fullmatch(r"[A-Za-z0-9_-]{32,256}", parsed.password or "") is not None
            and re.fullmatch(r"/k5_[0-9a-f]{64}", parsed.path) is not None
            and not parsed.query
            and not parsed.fragment
            and value
            == f"rtsp://{parsed.username}:{parsed.password}@127.0.0.1:{parsed.port}{parsed.path}"
        )
    except (TypeError, ValueError, AttributeError):
        valid = False
    if not valid:
        raise MotionProcessError("Motion relay configuration was refused")
    return value


def _document(raw: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def invalid(_value):
        raise ValueError

    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
    if type(value) is not dict:
        raise ValueError
    return value


def _policy(value: object) -> dict:
    if type(value) is not dict or set(value) != {
        "revision",
        "source_revision",
        "sensitivity",
        "minimum_area_percent",
        "include",
        "exclude",
    }:
        raise MotionProcessError("Motion policy was refused")
    if (
        type(value["revision"]) is not int
        or not 0 <= value["revision"] <= 2**53 - 1
        or type(value["source_revision"]) is not str
        or re.fullmatch(r"[0-9a-f]{64}", value["source_revision"]) is None
        or type(value["sensitivity"]) is not int
        or not 1 <= value["sensitivity"] <= 100
        or type(value["minimum_area_percent"]) not in (int, float)
        or not math.isfinite(value["minimum_area_percent"])
        or not 0.1 <= value["minimum_area_percent"] <= 100
    ):
        raise MotionProcessError("Motion policy was refused")
    _regions(value["include"], value["exclude"])
    # A private JSON copy makes a concurrent caller mutation unable to change a
    # request after its validation or while its native child evaluates it.
    return _document(json.dumps(value, allow_nan=False).encode("ascii"))


def _regions(include: object, exclude: object) -> None:
    for polygons in (include, exclude):
        if type(polygons) is not list or len(polygons) > 8:
            raise MotionProcessError("Motion policy was refused")
        for polygon in polygons:
            if type(polygon) is not list or not 3 <= len(polygon) <= 16:
                raise MotionProcessError("Motion policy was refused")
            for point in polygon:
                if (
                    type(point) is not list
                    or len(point) != 2
                    or any(
                        type(item) not in (int, float)
                        or not math.isfinite(item)
                        or not 0 <= item <= 1
                        for item in point
                    )
                ):
                    raise MotionProcessError("Motion policy was refused")
    if len(include) + len(exclude) > 8:
        raise MotionProcessError("Motion policy was refused")


def _mask_wire(value: object) -> bytes:
    if (
        type(value) is not dict
        or set(value) != {"width", "height", "include", "exclude"}
        or not _geometry(value["width"], value["height"])
    ):
        raise MotionProcessError("Motion mask configuration was refused")
    _regions(value["include"], value["exclude"])
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")


def _mask_request(policy: object) -> bytes | None:
    try:
        if type(policy) is not dict or type(policy["zones"]) is not dict:
            raise ValueError
        zones = policy["zones"]
        include = zones["include"]
        exclude = zones["exclude"]
        if type(include) is not list or type(exclude) is not list:
            raise ValueError
        reference = zones["reference"]
        if reference is None:
            if include or exclude:
                raise ValueError
            return None
        if type(reference) is not dict:
            raise ValueError
        return _mask_wire(
            {
                "width": reference["width"],
                "height": reference["height"],
                "include": [polygon["points"] for polygon in include],
                "exclude": [polygon["points"] for polygon in exclude],
            }
        )
    except (KeyError, TypeError, ValueError, MotionProcessError):
        raise MotionProcessError("Motion mask configuration was refused") from None


def _geometry(width: object, height: object) -> bool:
    return (
        type(width) is int
        and type(height) is int
        and 1 <= width <= 8192
        and 1 <= height <= 8192
        and width * height <= 33554432
    )


def _preview_size(width: int, height: int) -> tuple[int, int]:
    scale = min(1.0, 640 / max(width, height))
    return max(1, round(width * scale)), max(1, round(height * scale))


def _jpeg_geometry(raw: bytes) -> tuple[int, int] | None:
    """Read bounded JPEG frame dimensions without native decoding in the parent."""
    if not raw.startswith(b"\xff\xd8") or not raw.endswith(b"\xff\xd9"):
        return None
    offset = 2
    while offset + 4 <= len(raw):
        if raw[offset] != 255:
            return None
        marker = raw[offset + 1]
        if marker in (0, 0xD8, 0xD9, 0xDA):
            return None
        size = int.from_bytes(raw[offset + 2 : offset + 4], "big")
        if size < 2 or offset + 2 + size > len(raw):
            return None
        if marker in (0xC0, 0xC2):
            if size < 8 or raw[offset + 4] != 8 or raw[offset + 9] != 3:
                return None
            return (
                int.from_bytes(raw[offset + 7 : offset + 9], "big"),
                int.from_bytes(raw[offset + 5 : offset + 7], "big"),
            )
        offset += 2 + size
    return None


class _RequestDeadline:
    """Prestarted watchdog: no request waits for creation of a timer thread."""

    def __init__(self):
        self._condition = threading.Condition()
        self._process = None
        self._deadline = 0.0
        self._closed = False
        self._expired = None
        self._thread = threading.Thread(target=self._run, name="k5-motion-deadline", daemon=True)
        self._thread.start()

    def _run(self):
        while True:
            with self._condition:
                while True:
                    if self._closed:
                        return
                    if self._process is None:
                        self._condition.wait()
                        continue
                    remaining = self._deadline - time.monotonic()
                    if remaining > 0:
                        self._condition.wait(remaining)
                        continue
                    process, expired = self._process, self._expired
                    self._process, self._expired = None, None
                    expired.set()
                    break
            try:
                process.kill()
            except OSError:
                pass

    def arm(self, process):
        with self._condition:
            if self._closed or self._process is not None or not self._thread.is_alive():
                raise MotionProcessError("Motion request deadline is unavailable")
            self._expired = threading.Event()
            self._process = process
            self._deadline = time.monotonic() + _REQUEST_SECONDS
            self._condition.notify()
            return self._expired

    def disarm(self):
        with self._condition:
            self._process, self._expired = None, None
            self._condition.notify()

    def close(self, *, timeout=_CLOSE_SECONDS):
        with self._condition:
            self._closed = True
            self._condition.notify()
        self._thread.join(timeout=max(0.0, timeout))
        if self._thread.is_alive():
            raise MotionProcessError("Motion worker cleanup is incomplete")


class LiveMotionWorker:
    """Serialize bounded samples from one continuously decoded private relay."""

    def __init__(self, relay_uri: str):
        self._relay_uri = _relay(relay_uri)
        self._initialize_owner()

    def _initialize_owner(self):
        self._lock = threading.RLock()
        self._closing = threading.Event()
        self._process = None
        self._job = None
        self._last_sequence = 0
        self._sequence_base = 0
        self._public_sequence = 0
        self._decoder_phase = "none"
        self._deadline = _RequestDeadline()

    def _start(self, child_mode="--child"):
        if child_mode not in {"--child", "--mask-child"}:
            raise MotionProcessError("Motion worker mode was refused")
        if self._closing.is_set():
            raise MotionProcessError("Motion worker is closed")
        if self._process is not None and self._process.poll() is None:
            return
        self._dispose()
        from k5vision.operator_native_process import _WindowsJob

        job = _WindowsJob() if os.name == "nt" else None
        process = None
        try:
            environment = None
            if child_mode == "--child":
                # Pin only the live decoder's transport/buffering policy. The
                # parent and image-only mask worker keep their environment.
                environment = os.environ.copy()
                environment["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|fflags;nobuffer"
            process = subprocess.Popen(
                [sys.executable, "-I", "-B", "-m", "k5vision.operator_motion_process", child_mode],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                start_new_session=os.name != "nt",
                close_fds=True,
                env=environment,
            )
            if job is not None:
                job.assign(process)
            process.stdout = io.BufferedReader(process.stdout, buffer_size=65536)
            self._process, self._job = process, job
            self._last_sequence, self._sequence_base = 0, self._public_sequence
            self._decoder_phase = "none"
        except BaseException:
            self._process, self._job = process, job
            self._dispose()
            raise MotionProcessError(
                "Motion worker ownership is unavailable",
                phase="worker_start",
                reason="worker_start",
            ) from None

    @staticmethod
    def _write(stream, raw):
        remaining = memoryview(raw)
        while remaining:
            count = stream.write(remaining)
            if type(count) is not int or not 0 < count <= len(remaining):
                raise MotionProcessError("Motion request is unavailable")
            remaining = remaining[count:]
        stream.flush()

    def sample(self, policy: dict) -> MotionSample:
        admitted = _policy(policy)
        with self._lock:
            self._start()
            process = self._process
            expired = self._deadline.arm(process)
            phase, decoder_phase, reason = "request_write", self._decoder_phase, "request_io"
            try:
                if self._closing.is_set():
                    raise MotionProcessError("Motion worker is closed")
                if not self._last_sequence:
                    self._write(
                        process.stdin, json.dumps({"relay_uri": self._relay_uri}).encode() + b"\n"
                    )
                raw = json.dumps({"policy": admitted}, allow_nan=False).encode("ascii") + b"\n"
                if len(raw) > _MAX_REQUEST:
                    raise MotionProcessError("Motion policy was refused")
                self._write(process.stdin, raw)
                phase, reason = "response_read", "response_protocol"
                # Progress is finite metadata under the original single armed
                # deadline. It never refreshes the watchdog or admits pixels.
                progress = 0
                while True:
                    header = process.stdout.readline(4097)
                    if not header.endswith(b"\n") or len(header) > 4096:
                        raise MotionProcessError("Motion response was refused")
                    metadata = _document(header)
                    if metadata.get("status") not in {"progress", "unavailable"}:
                        break
                    if (
                        set(metadata) != {"status", "phase"}
                        or type(metadata["phase"]) is not str
                        or metadata["phase"]
                        not in MOTION_PHASES
                        - {
                            "none",
                            "worker_start",
                            "request_write",
                            "response_read",
                            "ready",
                            "recording_gate",
                        }
                    ):
                        raise MotionProcessError("Motion response was refused")
                    if metadata["phase"].startswith("decoder_"):
                        decoder_phase = metadata["phase"]
                        self._decoder_phase = decoder_phase
                    else:
                        phase = metadata["phase"]
                    if metadata["status"] == "unavailable":
                        reason = "child_failure"
                        raise MotionProcessError("Motion frame is unavailable")
                    progress += 1
                    if progress > _MAX_PROGRESS:
                        raise MotionProcessError("Motion response was refused")
                if (
                    set(metadata)
                    != {
                        "status",
                        "width",
                        "height",
                        "preview_width",
                        "preview_height",
                        "sequence",
                        "motion",
                        "age_seconds",
                        "captured_monotonic",
                        "bytes",
                    }
                    or metadata["status"] != "ready"
                ):
                    raise MotionProcessError("Motion frame is unavailable")
                width, height = metadata["width"], metadata["height"]
                size, sequence, captured = (
                    metadata["bytes"],
                    metadata["sequence"],
                    metadata["captured_monotonic"],
                )
                if (
                    not _geometry(width, height)
                    or (metadata["preview_width"], metadata["preview_height"])
                    != _preview_size(width, height)
                    or type(metadata["preview_width"]) is not int
                    or type(metadata["preview_height"]) is not int
                    or type(size) is not int
                    or not 4 <= size <= _MAX_JPEG
                    or type(sequence) is not int
                    or not self._last_sequence < sequence <= 2**53 - 1
                    or type(metadata["motion"]) is not bool
                    or type(metadata["age_seconds"]) not in (int, float)
                    or not math.isfinite(metadata["age_seconds"])
                    or not 0 <= metadata["age_seconds"] <= _MAX_AGE
                    or type(captured) not in (int, float)
                    or not math.isfinite(captured)
                ):
                    raise MotionProcessError("Motion response was refused")
                pixels = process.stdout.read(size)
                age = time.monotonic() - captured
                reason = "frame_payload"
                if not 0 <= age <= _MAX_AGE:
                    reason = "capture_age"
                if (
                    len(pixels) != size
                    or _jpeg_geometry(pixels) != _preview_size(width, height)
                    or not 0 <= age <= _MAX_AGE
                    or expired.is_set()
                    or self._closing.is_set()
                ):
                    raise MotionProcessError("Motion frame is unavailable")
                self._last_sequence = sequence
                self._public_sequence = self._sequence_base + sequence
                return MotionSample(
                    width,
                    height,
                    metadata["preview_width"],
                    metadata["preview_height"],
                    self._public_sequence,
                    metadata["motion"],
                    age,
                    pixels,
                    captured,
                )
            except (OSError, ValueError, TypeError, MotionProcessError):
                if expired.is_set():
                    reason = "request_deadline"
                elif self._closing.is_set():
                    reason = "worker_closed"
                try:
                    self._dispose()
                except MotionProcessError:
                    reason = "worker_cleanup"
                raise MotionProcessError(
                    "Motion frame is unavailable",
                    phase=phase,
                    decoder_phase=decoder_phase,
                    reason=reason,
                ) from None
            finally:
                self._deadline.disarm()

    def _dispose(self, *, timeout=_CLOSE_SECONDS):
        process, job = self._process, self._job
        try:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=max(0.0, timeout))
            if job is not None:
                job.close()
            if process is not None:
                for stream in (process.stdin, process.stdout):
                    if stream is not None:
                        stream.close()
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            raise MotionProcessError("Motion worker cleanup is incomplete") from None
        self._process, self._job = None, None

    def begin_close(self):
        self._closing.set()
        process = self._process
        if process is not None:
            try:
                process.kill()
            except OSError:
                pass

    def close(self):
        deadline = time.monotonic() + _CLOSE_SECONDS
        self.begin_close()
        acquired = self._lock.acquire(timeout=max(0.0, deadline - time.monotonic()))
        if not acquired:
            raise MotionProcessError("Motion worker cleanup is incomplete")
        try:
            self._dispose(timeout=max(0.0, deadline - time.monotonic()))
        finally:
            self._lock.release()
            self._deadline.close(timeout=max(0.0, deadline - time.monotonic()))


class MaskValidationWorker(LiveMotionWorker):
    """Rasterize zone geometry in an owned child, never in the API process."""

    def __init__(self):
        self._initialize_owner()

    def validate(self, policy: dict) -> bool:
        request = _mask_request(policy)
        with self._lock:
            if self._closing.is_set():
                raise MotionProcessError("Motion worker is closed")
            if request is None:
                return True
            if len(request) + 1 > _MAX_REQUEST:
                raise MotionProcessError("Motion mask configuration was refused")
            self._start("--mask-child")
            process = self._process
            expired = self._deadline.arm(process)
            try:
                if self._closing.is_set():
                    raise MotionProcessError("Motion worker is closed")
                self._write(process.stdin, request + b"\n")
                raw = process.stdout.readline(4097)
                if not raw.endswith(b"\n") or len(raw) > 4096:
                    raise MotionProcessError("Motion mask response was refused")
                response = _document(raw)
                if (
                    set(response) != {"status", "request_sha256"}
                    or response["status"] not in {"valid", "empty"}
                    or response["request_sha256"] != hashlib.sha256(request).hexdigest()
                    or expired.is_set()
                    or self._closing.is_set()
                ):
                    raise MotionProcessError("Motion mask response was refused")
                return response["status"] == "valid"
            except (OSError, ValueError, TypeError, MotionProcessError):
                self._dispose()
                raise MotionProcessError("Motion mask validation is unavailable") from None
            finally:
                self._deadline.disarm()


def _mask_child_main() -> int:
    """Receive geometry only after Job assignment; native imports stay here."""
    prepared = False
    try:
        while True:
            raw = sys.stdin.buffer.readline(_MAX_REQUEST + 1)
            if not raw:
                return 0
            if len(raw) > _MAX_REQUEST or not raw.endswith(b"\n"):
                return 1
            request = _mask_wire(_document(raw))
            selection = _document(request)
            if not prepared:
                import cv2

                cv2.setNumThreads(1)
                from k5vision.operator_motion import allowed_mask, analysis_dimensions

                prepared = True
            width, height = analysis_dimensions(selection["width"], selection["height"])
            allowed = bool(
                allowed_mask(width, height, selection["include"], selection["exclude"]).any()
            )
            response = {
                "status": "valid" if allowed else "empty",
                "request_sha256": hashlib.sha256(request).hexdigest(),
            }
            sys.stdout.buffer.write(json.dumps(response).encode("ascii") + b"\n")
            sys.stdout.buffer.flush()
    except BaseException:
        try:
            sys.stdout.buffer.write(b'{"status":"unavailable"}\n')
            sys.stdout.buffer.flush()
        except (OSError, ValueError):
            pass
        return 1


def _child_main() -> int:
    """One trusted relay, continuously drained; stdout contains bounded samples."""
    stopped = threading.Event()
    condition = threading.Condition()
    latest = None
    failed = False
    reader = None
    phase, decoder_phase = "native_import", "decoder_open"
    output_lock = threading.Lock()

    def progress(value):
        with output_lock:
            sys.stdout.buffer.write(
                json.dumps({"status": "progress", "phase": value}).encode("ascii") + b"\n"
            )
            sys.stdout.buffer.flush()

    try:
        initial = sys.stdin.buffer.readline(2049)
        if len(initial) > 2048 or not initial.endswith(b"\n"):
            return 1
        startup = _document(initial)
        if set(startup) != {"relay_uri"}:
            return 1
        uri = _relay(startup["relay_uri"])
        progress(phase)
        import cv2

        cv2.setNumThreads(1)
        phase = "detector_import"
        progress(phase)
        from k5vision.operator_motion import SceneMotionDetector

        detector = SceneMotionDetector()

        def capture_frames():
            nonlocal latest, failed, decoder_phase
            capture = None
            try:
                progress("decoder_open")
                capture = cv2.VideoCapture(
                    uri,
                    cv2.CAP_FFMPEG,
                    [
                        cv2.CAP_PROP_OPEN_TIMEOUT_MSEC,
                        1500,
                        cv2.CAP_PROP_READ_TIMEOUT_MSEC,
                        1500,
                        cv2.CAP_PROP_N_THREADS,
                        1,
                    ],
                )
                if not capture.isOpened():
                    raise ValueError
                sequence = 0
                decoder_phase = "decoder_read"
                progress(decoder_phase)
                while not stopped.is_set():
                    decoder_phase = "decoder_read"
                    ready, frame = capture.read()
                    if not ready or frame is None:
                        raise ValueError
                    decoder_phase = "decoder_frame"
                    if frame.ndim != 3 or frame.shape[2] != 3:
                        raise ValueError
                    height, width = frame.shape[:2]
                    if not _geometry(width, height):
                        raise ValueError
                    sequence += 1
                    captured = time.monotonic()
                    with condition:
                        latest = (sequence, captured, frame)
                        condition.notify()
            except BaseException:
                with condition:
                    failed = True
                    condition.notify()
            finally:
                if capture is not None:
                    capture.release()

        reader = threading.Thread(target=capture_frames, name="k5-motion-live-reader", daemon=True)
        reader.start()
        last_sequence = 0
        while True:
            raw = sys.stdin.buffer.readline(_MAX_REQUEST + 1)
            if not raw:
                return 0
            if len(raw) > _MAX_REQUEST or not raw.endswith(b"\n"):
                return 1
            request = _document(raw)
            if set(request) != {"policy"}:
                return 1
            policy = _policy(request["policy"])
            phase = "frame_wait"
            progress(phase)
            deadline = time.monotonic() + 3.0
            with condition:
                while True:
                    now = time.monotonic()
                    if failed:
                        phase = decoder_phase
                        raise ValueError
                    if (
                        latest is not None
                        and latest[0] > last_sequence
                        and now - latest[1] <= _MAX_AGE
                    ):
                        sequence, captured, frame = latest
                        break
                    remaining = deadline - now
                    if remaining <= 0:
                        raise ValueError
                    condition.wait(remaining)
            height, width = frame.shape[:2]
            phase = "motion_analysis"
            progress(phase)
            motion = detector.observe(frame, policy)
            phase = "jpeg_encode"
            progress(phase)
            preview_width, preview_height = _preview_size(width, height)
            preview = cv2.resize(
                frame, (preview_width, preview_height), interpolation=cv2.INTER_AREA
            )
            encoded, pixels = cv2.imencode(".jpg", preview, [cv2.IMWRITE_JPEG_QUALITY, 75])
            if encoded and len(pixels) > _MAX_JPEG:
                encoded, pixels = cv2.imencode(".jpg", preview, [cv2.IMWRITE_JPEG_QUALITY, 60])
            age = time.monotonic() - captured
            if not encoded or len(pixels) > _MAX_JPEG or not 0 <= age <= _MAX_AGE:
                raise ValueError
            metadata = {
                "status": "ready",
                "width": width,
                "height": height,
                "preview_width": preview_width,
                "preview_height": preview_height,
                "sequence": sequence,
                "motion": bool(motion),
                "age_seconds": age,
                "captured_monotonic": captured,
                "bytes": len(pixels),
            }
            phase = "response_write"
            progress(phase)
            with output_lock:
                sys.stdout.buffer.write(
                    json.dumps(metadata, allow_nan=False).encode("ascii") + b"\n"
                )
                sys.stdout.buffer.write(pixels.tobytes())
                sys.stdout.buffer.flush()
            last_sequence = sequence
    except BaseException:
        try:
            with output_lock:
                sys.stdout.buffer.write(
                    json.dumps({"status": "unavailable", "phase": phase}).encode("ascii") + b"\n"
                )
                sys.stdout.buffer.flush()
        except (OSError, ValueError):
            pass
        return 1
    finally:
        stopped.set()
        if reader is not None:
            reader.join(timeout=_CLOSE_SECONDS)


if __name__ == "__main__":
    if sys.argv[1:] == ["--child"]:
        raise SystemExit(_child_main())
    if sys.argv[1:] == ["--mask-child"]:
        raise SystemExit(_mask_child_main())
    raise SystemExit(1)
