"""Continuously publish generated pixels with independently controllable movement.

Changing movement does not reconnect RTSP or replace the decoder source. Only
the explicit geometry experiment replaces a publisher. No physical device,
camera credential or captured image enters this fixture.
"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

_MODES = frozenset({"idle", "included", "excluded", "outside"})
_SIZES = frozenset({(320, 240), (640, 360)})


def generated_frame(mode: str, index: int, *, width=320, height=240) -> bytes:
    """Return RGB24 pixels; movement regions never overlap their test boundary."""
    if mode not in _MODES or type(index) is not int or index < 0 or (width, height) not in _SIZES:
        raise ValueError("Generated motion frame request refused")
    frame = bytearray(bytes((220, 8, 8)) * (width * height))
    if mode == "idle":
        return bytes(frame)
    # At 10 FPS this period remains observable by the independent 2-FPS
    # detector. The excluded shape approaches x=.5 without crossing it, so
    # filtering/morphology cannot turn masked-only movement into a trigger.
    phase = (index % 20) / 20
    origin = {"included": 0.08, "excluded": 0.51, "outside": 0.84}[mode]
    extent = {"included": 0.18, "excluded": 0.12, "outside": 0.04}[mode]
    x = int(width * (origin + extent * phase))
    y = int(height * 0.25)
    box_width, box_height = max(8, width // 12), max(16, height // 3)
    row = bytes((232, 232, 232)) * box_width
    for current in range(y, y + box_height):
        offset = (current * width + x) * 3
        frame[offset : offset + len(row)] = row
    return bytes(frame)


class MotionPublisher:
    """Own one encoder and a bounded writer with no movement-switch reconnect."""

    def __init__(self, ffmpeg: Path, uri: str, *, width=320, height=240):
        if (width, height) not in _SIZES:
            raise ValueError("Generated publisher geometry refused")
        self.ffmpeg, self.uri = Path(ffmpeg), uri
        self.width, self.height = width, height
        self._mode = "idle"
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._failure = False
        self.frames_written = 0
        self.restarts = 0
        self.process = None
        self._writer = None
        self._start()

    def _start(self):
        self._stop = threading.Event()
        self._failure = False
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.process = subprocess.Popen(
            [
                str(self.ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "rawvideo",
                "-pixel_format",
                "rgb24",
                "-video_size",
                f"{self.width}x{self.height}",
                "-framerate",
                "10",
                "-i",
                "pipe:0",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-tune",
                "zerolatency",
                "-threads",
                "1",
                "-pix_fmt",
                "yuv420p",
                "-g",
                "10",
                "-keyint_min",
                "10",
                "-bf",
                "0",
                "-f",
                "rtsp",
                "-rtsp_transport",
                "tcp",
                self.uri,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )

        def write_frames():
            index, deadline = 0, time.monotonic()
            try:
                while not self._stop.is_set():
                    with self._lock:
                        mode = self._mode
                    frame = generated_frame(mode, index, width=self.width, height=self.height)
                    self.process.stdin.write(frame)
                    self.process.stdin.flush()
                    self.frames_written += 1
                    index += 1
                    deadline += 0.1
                    self._stop.wait(max(0, deadline - time.monotonic()))
            except (BrokenPipeError, OSError, ValueError):
                if not self._stop.is_set():
                    self._failure = True

        self._writer = threading.Thread(target=write_frames, daemon=True)
        self._writer.start()

    def movement(self, mode):
        if mode not in _MODES:
            raise ValueError("Generated publisher mode refused")
        self.check()
        with self._lock:
            self._mode = mode

    def check(self):
        if self._failure or self.process is None or self.process.poll() is not None:
            raise RuntimeError("Generated motion publisher stopped")

    def geometry(self, width, height):
        if (width, height) not in _SIZES:
            raise ValueError("Generated publisher geometry refused")
        self.close()
        self.width, self.height = width, height
        self.restarts += 1
        self._start()

    def close(self):
        self._stop.set()
        if self.process is not None and self.process.poll() is None:
            # Retire the consumer first so a blocked pipe writer cannot outlive
            # cleanup or block another thread's attempt to close its stream.
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self._writer is not None:
            self._writer.join(timeout=2)
            if self._writer.is_alive():
                raise RuntimeError("Generated motion writer did not retire")
        if self.process is not None and self.process.stdin is not None:
            self.process.stdin.close()
