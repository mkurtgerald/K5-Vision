"""Isolated, locally owned native person-view compilation and inference.

No camera identity, source address, credentials, recording or log leaves the
authenticated parent. Raw view pixels travel only over anonymous child pipes.
Windows runs the child below normal CPU priority and in a kill-on-close Job.
The signed model admission and 2-second live inference deadline are unchanged.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

from k5vision.analytics_runtime import _AnalyticsSession, _protected_future

_MAX_FRAME = 640 * 640 * 4
_COLD_SECONDS = 22.0


def _line(stream) -> dict:
    raw = stream.readline(4097)
    if not raw.endswith(b"\n") or len(raw) > 4096:
        raise RuntimeError("Native person protocol unavailable")
    try:
        data = json.loads(raw)
    except (UnicodeError, ValueError):
        raise RuntimeError("Native person protocol unavailable") from None
    if type(data) is not dict:
        raise RuntimeError("Native person protocol unavailable")
    return data


def _write(stream, payload: bytes) -> None:
    # Anonymous unbuffered pipes may perform short writes on Windows.
    pending = memoryview(payload)
    while pending:
        count = stream.write(pending)
        if type(count) is not int or not 0 < count <= len(pending):
            raise RuntimeError("Native person request unavailable")
        pending = pending[count:]


class _ViewPersonWorker:
    """One media-free model owner; cancellation kills and reaps its CPU work."""

    def __init__(self, config):
        self._root = str(config.artifact_root)
        self._process = None
        self._job = None
        self._closing = threading.Event()

    def start(self) -> bool:
        from k5vision.operator_native_process import _WindowsJob

        if self._closing.is_set():
            return False
        process = None
        try:
            job = _WindowsJob() if os.name == "nt" else None
            self._job = job
            # Child cannot open the model until job assignment and stdin grant.
            process = subprocess.Popen(
                [sys.executable, "-I", "-B", "-m", "k5vision.operator_person_view_process"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
                close_fds=True,
                creationflags=(
                    getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
                ),
                start_new_session=os.name != "nt",
            )
            self._process = process
            if job is not None:
                job.assign(process)
            if self._closing.is_set():
                return False
            _write(process.stdin, json.dumps({"artifact_root": self._root}).encode("utf-8") + b"\n")
            return _line(process.stdout) == {"status": "ready"}
        except BaseException:
            return False

    def infer(
        self, payload: bytes, width: int, height: int, stride: int, elapsed: int
    ) -> tuple[object, ...]:
        process = self._process
        if self._closing.is_set() or process is None or process.poll() is not None:
            raise RuntimeError("Native person worker unavailable")
        if (
            type(width) is not int
            or type(height) is not int
            or type(stride) is not int
            or type(elapsed) is not int
            or width < 1
            or height < 1
            or stride != width * 4
            or not 0 <= elapsed <= 2_147_483_647
            or len(payload) != stride * height
            or len(payload) > _MAX_FRAME
        ):
            raise RuntimeError("Native person frame refused")
        header = {"bytes": len(payload), "width": width, "height": height, "stride": stride, "elapsed": elapsed}
        _write(process.stdin, json.dumps(header).encode("utf-8") + b"\n")
        _write(process.stdin, payload)
        report = _line(process.stdout)
        if set(report) != {"status", "boxes"} or report["status"] != "ready":
            raise RuntimeError("Native person response unavailable")
        boxes = report["boxes"]
        if type(boxes) is not list or len(boxes) > 32:
            raise RuntimeError("Native person response unavailable")
        detections = []
        for index, box in enumerate(boxes):
            if type(box) is not dict or set(box) != {
                "confidence", "x_min", "y_min", "x_max", "y_max"
            }:
                raise RuntimeError("Native person response unavailable")
            values = tuple(box[key] for key in ("confidence", "x_min", "y_min", "x_max", "y_max"))
            if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
                raise RuntimeError("Native person response unavailable")
            confidence, left, top, right, bottom = values
            if not (
                0.5 <= confidence <= 1
                and 0 <= left < right <= 1
                and 0 <= top < bottom <= 1
            ):
                raise RuntimeError("Native person response unavailable")
            detections.append(
                SimpleNamespace(
                    track_id=f"view-person-{index}",
                    category="person",
                    confidence=float(confidence),
                    box=SimpleNamespace(
                        x_min=float(left), y_min=float(top), x_max=float(right), y_max=float(bottom)
                    ),
                )
            )
        return tuple(detections)

    def begin_close(self) -> None:
        self._closing.set()
        process = self._process
        if process is not None and process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass

    def finish_close(self) -> None:
        self.begin_close()
        process = self._process
        if process is not None:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
        if self._job is not None:
            self._job.close()
            self._job = None


class IsolatedPersonSession(_AnalyticsSession):
    """Keep same frame/capacity/cancel semantics; move native CPU out of ASGI."""

    def _initialize(self) -> bool:
        worker = _ViewPersonWorker(self._config)
        self._backend = worker
        return worker.start()

    async def start(self) -> None:
        ready = await asyncio.wait_for(_protected_future(self._pending), _COLD_SECONDS)
        if ready is not True:
            raise RuntimeError("Person session initialization failed.")

    async def aclose(self) -> None:
        worker = self._backend
        if worker is not None:
            worker.begin_close()
        try:
            await super().aclose()
        finally:
            if worker is not None:
                await asyncio.to_thread(worker.finish_close)


def _read_exact(stream, size: int) -> bytearray:
    payload = bytearray(size)
    view = memoryview(payload)
    index = 0
    while index < size:
        count = stream.readinto(view[index:])
        if count is None or count <= 0:
            raise RuntimeError("Native person input incomplete")
        index += count
    return payload


def _child() -> int:
    from k5vision.operator_analytics_person import OperatorPersonConfiguration, _PersonDetector

    raw = _line(sys.stdin.buffer)
    if set(raw) != {"artifact_root"} or type(raw["artifact_root"]) is not str:
        return 1
    # Exact signed model bytes and native version are revalidated in child.
    if not 0 < len(raw["artifact_root"]) <= 4096:
        return 1
    try:
        detector = _PersonDetector(
            OperatorPersonConfiguration(Path(raw["artifact_root"])),
            inference_threads=1,
            fixed_view_input=True,
        )
    except BaseException:
        sys.stdout.buffer.write(b'{"status":"failed"}\n')
        sys.stdout.buffer.flush()
        return 1
    sys.stdout.buffer.write(b'{"status":"ready"}\n')
    sys.stdout.buffer.flush()
    while True:
        raw = sys.stdin.buffer.readline(4097)
        if not raw:
            return 0
        if not raw.endswith(b"\n") or len(raw) > 4096:
            return 1
        try:
            request = json.loads(raw)
        except (ValueError, UnicodeError):
            return 1
        if type(request) is not dict or set(request) != {
            "bytes", "width", "height", "stride", "elapsed"
        }:
            return 1
        count, width, height, stride, elapsed = (
            request[key] for key in ("bytes", "width", "height", "stride", "elapsed")
        )
        if (
            any(type(value) is not int for value in (count, width, height, stride, elapsed))
            or width < 1 or height < 1 or stride != width * 4
            or not 0 <= elapsed <= 2_147_483_647
            or count != stride * height or not 0 < count <= _MAX_FRAME
        ):
            return 1
        pixels = _read_exact(sys.stdin.buffer, count)
        try:
            detected = detector.infer(bytes(pixels), width, height, stride, elapsed)
        except BaseException:
            return 1
        finally:
            pixels[:] = b"\0" * len(pixels)
        # Only the bounded geometry from real admitted inference crosses back.
        boxes = [
            {
                "confidence": float(item.confidence),
                "x_min": float(item.box.x_min),
                "y_min": float(item.box.y_min),
                "x_max": float(item.box.x_max),
                "y_max": float(item.box.y_max),
            }
            for item in detected
        ]
        if len(boxes) > 32:
            return 1
        sys.stdout.buffer.write(
            json.dumps({"status": "ready", "boxes": boxes}, allow_nan=False).encode("utf-8") + b"\n"
        )
        sys.stdout.buffer.flush()


if __name__ == "__main__":
    try:
        raise SystemExit(_child())
    except BaseException:
        raise SystemExit(1) from None
