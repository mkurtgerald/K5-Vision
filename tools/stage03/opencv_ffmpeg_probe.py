"""Bounded source-free-retention probe for the Stage 03 OpenCV/FFmpeg candidate."""

from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("OPENCV_LOG_LEVEL", "SILENT")
os.environ.setdefault("OPENCV_FFMPEG_LOGLEVEL", "-8")

import cv2  # noqa: E402

_OBSERVATION_SECONDS = 3.0
_MINIMUM_FRAMES = 3


def main() -> int:
    """Decode transient frames for a bounded window without writing media to storage."""
    if len(sys.argv) != 2 or not sys.argv[1].strip():
        return 64

    capture = cv2.VideoCapture(sys.argv[1], cv2.CAP_FFMPEG)
    if not capture.isOpened():
        capture.release()
        return 1

    started = time.monotonic()
    frames = 0
    try:
        while time.monotonic() - started < _OBSERVATION_SECONDS:
            ok, frame = capture.read()
            if not ok or frame is None:
                return 2
            frames += 1
            del frame
    finally:
        capture.release()

    return 0 if frames >= _MINIMUM_FRAMES else 3


if __name__ == "__main__":
    raise SystemExit(main())
