"""Selected Stage-04 GStreamer UDP runtime behind the K5 media boundary.

This adapter uses only the Stage-03 reviewed GStreamer surface: rtspsrc, queue,
and fakesink. It never captures stdout/stderr and never persists source material.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import time

import psutil

_GST_LAUNCH = "gst-launch-1.0"
_REVIEWED_GSTREAMER_VERSION = "1.28.7"
_REVIEWED_ELEMENTS = frozenset({"rtspsrc", "queue", "fakesink"})


def build_udp_argv(executable: str, source_uri: str) -> list[str]:
    """Build the reviewed UDP receive path without invoking a command shell."""
    if not executable.strip():
        raise ValueError("GStreamer executable must not be empty")
    if not source_uri.strip():
        raise ValueError("source_uri must not be empty")
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
        "queue",
        "!",
        "fakesink",
        "sync=false",
    ]


def _is_live(process: psutil.Process) -> bool:
    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return False
    except psutil.AccessDenied:
        return True


def stop_process_tree(process: subprocess.Popen[bytes], *, timeout_seconds: float = 1.0) -> None:
    """Boundedly terminate one process and descendants using the proven Stage-03 pattern."""
    descendants: list[psutil.Process] = []
    try:
        root = psutil.Process(process.pid)
        descendants = [child for child in root.children(recursive=True) if _is_live(child)]
    except (psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied):
        pass

    if process.poll() is None:
        try:
            process.terminate()
        except OSError:
            pass

    for child in descendants:
        try:
            child.terminate()
        except (psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied):
            pass

    if descendants:
        _, alive = psutil.wait_procs(descendants, timeout=timeout_seconds)
        for child in alive:
            try:
                child.kill()
            except (psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied):
                pass
        if alive:
            psutil.wait_procs(alive, timeout=timeout_seconds)

    if process.poll() is None:
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=timeout_seconds)


class GStreamerUdpRuntime:
    """Process-backed implementation of the K5 MediaRuntime protocol."""

    reviewed_version = _REVIEWED_GSTREAMER_VERSION
    reviewed_elements = _REVIEWED_ELEMENTS

    def __init__(
        self,
        *,
        executable_name: str = _GST_LAUNCH,
        startup_probe_seconds: float = 0.15,
        stop_timeout_seconds: float = 1.0,
    ) -> None:
        if startup_probe_seconds <= 0:
            raise ValueError("startup_probe_seconds must be positive")
        if stop_timeout_seconds <= 0:
            raise ValueError("stop_timeout_seconds must be positive")
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
            build_udp_argv(executable, source_uri),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
        self._process = process
        time.sleep(self._startup_probe_seconds)
        if process.poll() is not None:
            self._process = None
            raise RuntimeError("GStreamer UDP runtime failed during startup")

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
