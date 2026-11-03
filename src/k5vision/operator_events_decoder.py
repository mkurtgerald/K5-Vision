"""Killable local-file decoder worker; private request arrives only on stdin."""

from __future__ import annotations

import json
import math
import sys
import time


def decode(path: str):
    import cv2

    capture = cv2.VideoCapture(
        path,
        cv2.CAP_FFMPEG,
        [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 1500, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 1500],
    )
    try:
        if not capture.isOpened():
            return None
        width = capture.get(cv2.CAP_PROP_FRAME_WIDTH)
        height = capture.get(cv2.CAP_PROP_FRAME_HEIGHT)
        if not (1 <= width <= 8192 and 1 <= height <= 8192 and width * height <= 33_554_432):
            return None
        count = capture.get(cv2.CAP_PROP_FRAME_COUNT)
        fps = capture.get(cv2.CAP_PROP_FPS)
        if math.isfinite(count) and math.isfinite(fps) and count > 0 and fps > 0:
            capture.set(cv2.CAP_PROP_POS_MSEC, max(0, (count / fps - 2) * 1000))
        selected = None
        deadline = time.monotonic() + 3
        for _index in range(8192):
            if time.monotonic() >= deadline:
                break
            ready, frame = capture.read()
            if not ready:
                break
            elapsed = capture.get(cv2.CAP_PROP_POS_MSEC) / 1000
            # No arrival-time or guessed-FPS fallback may become event UTC.
            if math.isfinite(elapsed) and 0 <= elapsed <= 3605:
                selected = frame, elapsed
        if selected is None:
            return None
        frame, elapsed = selected
        pixels = cv2.cvtColor(cv2.resize(frame, (544, 320)), cv2.COLOR_BGR2BGRA)
        return elapsed, pixels.tobytes(order="C")
    finally:
        capture.release()


def main() -> int:
    detector = None
    artifact_root = None
    try:
        while True:
            raw = sys.stdin.buffer.readline(8193)
            if not raw:
                return 0
            if len(raw) > 8192 or not raw.endswith(b"\n"):
                return 1
            request = json.loads(raw)
            if (
                type(request) is not dict
                or request.keys() != {"path", "artifact_root"}
                or not isinstance(request["path"], str)
                or not 1 <= len(request["path"]) <= 4096
                or (
                    request["artifact_root"] is not None
                    and (
                        not isinstance(request["artifact_root"], str)
                        or not 1 <= len(request["artifact_root"]) <= 4096
                    )
                )
            ):
                return 1
            if artifact_root is not None and artifact_root != request["artifact_root"]:
                return 1
            result = decode(request["path"])
            if result is None:
                sys.stdout.buffer.write(b'{"status":"empty"}\n')
                sys.stdout.buffer.flush()
                continue
            elapsed, pixels = result
            detections = None
            if request["artifact_root"] is not None:
                if detector is None:
                    from pathlib import Path

                    from k5vision.operator_analytics_person import (
                        OperatorPersonConfiguration,
                        _PersonDetector,
                    )

                    artifact_root = request["artifact_root"]
                    detector = _PersonDetector(OperatorPersonConfiguration(Path(artifact_root)))
                detections = [
                    {"category": item.category, "confidence": item.confidence}
                    for item in detector.infer(pixels, 544, 320, 544 * 4, int(elapsed * 1000))
                ]
            header = json.dumps(
                {
                    "status": "ready",
                    "elapsed": elapsed,
                    "bytes": len(pixels),
                    "detections": detections,
                }
            ).encode("ascii")
            sys.stdout.buffer.write(header + b"\n" + pixels)
            sys.stdout.buffer.flush()
    except BaseException:
        # Native/module/filesystem exception details never cross the worker pipe.
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
