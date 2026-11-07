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
        tail_offset = (
            max(0, (count / fps - 2) * 1000)
            if math.isfinite(count) and math.isfinite(fps) and count > 0 and fps > 0
            else 0
        )
        if tail_offset > 0:
            capture.set(cv2.CAP_PROP_POS_MSEC, tail_offset)
        selected = None
        deadline = time.monotonic() + 3
        # A growing fragmented MP4 can expose dimensions/count before its last
        # fragment supports seeking. An empty tail seek is not an empty recording.
        # Try a rewind and, if necessary, a fresh decoder on the same immutable
        # snapshot. Keep all attempts inside the same three-second decode budget;
        # never invent timestamps or inspect a different recording.
        for attempt in range(3 if tail_offset > 0 else 1):
            for _index in range(8192):
                if time.monotonic() >= deadline:
                    break
                ready, frame = capture.read()
                if not ready:
                    break
                elapsed = capture.get(cv2.CAP_PROP_POS_MSEC) / 1000
                # Event UTC must use the recorded presentation timestamp only.
                if math.isfinite(elapsed) and 0 <= elapsed <= 3605:
                    selected = frame, elapsed
            if selected is not None or time.monotonic() >= deadline:
                break
            if attempt == 0:
                capture.set(cv2.CAP_PROP_POS_MSEC, 0)
            elif attempt == 1:
                # Some FFmpeg backends cannot rewind a fragmented index in
                # place. Reopening resets the decoder without changing bytes.
                capture.release()
                capture = cv2.VideoCapture(
                    path,
                    cv2.CAP_FFMPEG,
                    [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 1500, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 1500],
                )
                if (
                    not capture.isOpened()
                    or capture.get(cv2.CAP_PROP_FRAME_WIDTH) != width
                    or capture.get(cv2.CAP_PROP_FRAME_HEIGHT) != height
                ):
                    break
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
    phase = "protocol"
    try:
        while True:
            phase = "protocol"
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
                or not 0 <= len(request["path"]) <= 4096
                or (not request["path"] and request["artifact_root"] is None)
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
            if request["path"] == "":
                # A model-only warmup has no camera source, stored pixels or
                # presentation timestamp. It uses the normal bounded worker.
                if detector is None:
                    from pathlib import Path

                    from k5vision.operator_analytics_person import (
                        OperatorPersonConfiguration,
                        _PersonDetector,
                    )

                    artifact_root = request["artifact_root"]
                    phase = "model"
                    detector = _PersonDetector(OperatorPersonConfiguration(Path(artifact_root)))
                # CPU kernels may finish JIT initialization only on their
                # first inference. Exercise that boundary with synthetic zero
                # pixels before the recording sampler enters the 25s event gate.
                # Never emit the synthetic result as a real person event.
                phase = "inference"
                detector.infer(bytes(544 * 320 * 4), 544, 320, 544 * 4, 0)
                phase = "protocol"
                sys.stdout.buffer.write(b'{"status":"primed"}\n')
                sys.stdout.buffer.flush()
                continue
            phase = "decode"
            # A finite, source-free phase notice distinguishes slow decoding from
            # model compilation without publishing any recording identity or pixels.
            sys.stdout.buffer.write(b'{"status":"phase","phase":"decode"}\n')
            sys.stdout.buffer.flush()
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
                    phase = "model"
                    sys.stdout.buffer.write(b'{"status":"phase","phase":"model"}\n')
                    sys.stdout.buffer.flush()
                    detector = _PersonDetector(OperatorPersonConfiguration(Path(artifact_root)))
                phase = "inference"
                sys.stdout.buffer.write(b'{"status":"phase","phase":"inference"}\n')
                sys.stdout.buffer.flush()
                detections = [
                    {"category": item.category, "confidence": item.confidence}
                    for item in detector.infer(pixels, 544, 320, 544 * 4, int(elapsed * 1000))
                ]
            phase = "protocol"
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
        # Send a fixed, source-free phase to the owning parent only. Never
        # serialize exception text, model paths, source URIs or recording bytes.
        try:
            sys.stdout.buffer.write(
                json.dumps({"status": "failed", "phase": phase}).encode("ascii") + b"\n"
            )
            sys.stdout.buffer.flush()
        except (OSError, ValueError):
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
