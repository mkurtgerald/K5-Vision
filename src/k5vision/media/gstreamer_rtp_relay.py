"""Reviewed GStreamer RTP relay for ephemeral K5 live-view delivery.

The relay forwards video RTP buffers from the accepted RTSP/UDP source path to a
loopback UDP port owned by K5. It does not decode, record, log, or persist media.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import time

from k5vision.media.gstreamer_udp import stop_process_tree

_GST_LAUNCH = "gst-launch-1.0"
_REVIEWED_GSTREAMER_VERSION = "1.28.7"
_REVIEWED_ELEMENTS = frozenset({"rtspsrc", "capsfilter", "queue", "udpsink"})


def build_rtp_relay_argv(executable: str, source_uri: str, port: int) -> list[str]:
    """Build a bounded video-RTP relay to K5-owned loopback UDP."""
    if not executable.strip():
        raise ValueError("GStreamer executable must not be empty")
    if not source_uri.strip():
        raise ValueError("source_uri must not be empty")
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    return [
        executable,
        "-q",
        "rtspsrc",
        f"location={source_uri}",
        "protocols=udp",
        "latency=100",
        "tcp-timeout=5000000",
        "teardown-timeout=0",
        "!",
        "application/x-rtp,media=video",
        "!",
        "queue",
        "max-size-buffers=8",
        "max-size-bytes=0",
        "max-size-time=0",
        "leaky=downstream",
        "!",
        "udpsink",
        "host=127.0.0.1",
        f"port={port}",
        "sync=false",
        "async=false",
    ]


class GStreamerRtpRelayRuntime:
    """Process-backed RTP relay with bounded startup and cleanup."""

    reviewed_version = _REVIEWED_GSTREAMER_VERSION
    reviewed_elements = _REVIEWED_ELEMENTS

    def __init__(
        self,
        port: int,
        *,
        executable_name: str = _GST_LAUNCH,
        startup_probe_seconds: float = 0.15,
        stop_timeout_seconds: float = 1.0,
    ) -> None:
        if not 1 <= port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if startup_probe_seconds <= 0:
            raise ValueError("startup_probe_seconds must be positive")
        if stop_timeout_seconds <= 0:
            raise ValueError("stop_timeout_seconds must be positive")
        self._port = port
        self._executable_name = executable_name
        self._startup_probe_seconds = startup_probe_seconds
        self._stop_timeout_seconds = stop_timeout_seconds
        self._process: subprocess.Popen[bytes] | None = None

    def _start_sync(self, source_uri: str) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        executable = shutil.which(self._executable_name)
        if executable is None:
            raise RuntimeError("reviewed GStreamer runtime is unavailable")

        process = subprocess.Popen(
            build_rtp_relay_argv(executable, source_uri, self._port),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
        self._process = process
        time.sleep(self._startup_probe_seconds)
        if process.poll() is not None:
            self._process = None
            raise RuntimeError("GStreamer RTP relay failed during startup")

    def _stop_sync(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        stop_process_tree(process, timeout_seconds=self._stop_timeout_seconds)

    async def start(self, source_uri: str) -> None:
        await asyncio.to_thread(self._start_sync, source_uri)

    async def stop(self) -> None:
        await asyncio.to_thread(self._stop_sync)

    async def close(self) -> None:
        await self.stop()
