"""Bounded source-free-retention probe for the Stage 03 GStreamer candidate."""

from __future__ import annotations

import shutil
import subprocess
import sys
import time

_OBSERVATION_SECONDS = 3.0
_POLL_SECONDS = 0.1


def main() -> int:
    """Decode a live source for a bounded window without retaining media or source data."""
    if len(sys.argv) != 2 or not sys.argv[1].strip():
        return 64

    executable = shutil.which("gst-play-1.0")
    if executable is None:
        return 69

    command = [
        executable,
        "--no-interactive",
        "--videosink=fakesink",
        "--audiosink=fakesink",
        sys.argv[1],
    ]

    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
    except OSError:
        return 70

    started = time.monotonic()
    try:
        while time.monotonic() - started < _OBSERVATION_SECONDS:
            return_code = process.poll()
            if return_code is not None:
                return 1
            time.sleep(_POLL_SECONDS)
        return 0
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)


if __name__ == "__main__":
    raise SystemExit(main())
