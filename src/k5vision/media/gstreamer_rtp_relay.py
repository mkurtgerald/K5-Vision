"""Reviewed GStreamer RTP relay for ephemeral K5 live-view delivery.

The relay runs in-process so private RTSP source material never appears in a
child-process command line. It forwards video RTP to K5-owned loopback UDP only.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Protocol

from k5vision.media.native_rtsp_pipeline import NativeRtspPipeline, quote_pipeline_value

_REVIEWED_GSTREAMER_VERSION = "1.28.7"
_REVIEWED_ELEMENTS = frozenset({"rtspsrc", "capsfilter", "queue", "udpsink"})


class _Pipeline(Protocol):
    def close(self, *, timeout_seconds: float = 1.0) -> None: ...


_PipelineFactory = Callable[[str, float], _Pipeline]


def build_rtp_relay_pipeline(source_uri: str, port: int) -> str:
    """Build a bounded video-RTP relay to K5-owned loopback UDP."""
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    location = quote_pipeline_value(source_uri)
    return (
        f"rtspsrc location={location} protocols=udp latency=100 "
        "tcp-timeout=5000000 teardown-timeout=0 "
        "! application/x-rtp,media=video "
        "! queue max-size-buffers=8 max-size-bytes=0 max-size-time=0 leaky=downstream "
        f"! udpsink host=127.0.0.1 port={port} sync=false async=false"
    )


def _default_pipeline_factory(description: str, startup_probe_seconds: float) -> _Pipeline:
    return NativeRtspPipeline(description, startup_probe_seconds=startup_probe_seconds)


class GStreamerRtpRelayRuntime:
    """In-process RTP relay with bounded startup and cleanup."""

    reviewed_version = _REVIEWED_GSTREAMER_VERSION
    reviewed_elements = _REVIEWED_ELEMENTS

    def __init__(
        self,
        port: int,
        *,
        executable_name: str = "gst-launch-1.0",
        startup_probe_seconds: float = 0.15,
        stop_timeout_seconds: float = 1.0,
        pipeline_factory: _PipelineFactory | None = None,
    ) -> None:
        if not 1 <= port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if not executable_name.strip():
            raise ValueError("executable_name must not be empty")
        if startup_probe_seconds <= 0:
            raise ValueError("startup_probe_seconds must be positive")
        if stop_timeout_seconds <= 0:
            raise ValueError("stop_timeout_seconds must be positive")
        self._port = port
        self._startup_probe_seconds = startup_probe_seconds
        self._stop_timeout_seconds = stop_timeout_seconds
        self._pipeline_factory = pipeline_factory or _default_pipeline_factory
        self._pipeline: _Pipeline | None = None

    def _start_sync(self, source_uri: str) -> None:
        if self._pipeline is not None:
            return
        try:
            self._pipeline = self._pipeline_factory(
                build_rtp_relay_pipeline(source_uri, self._port),
                self._startup_probe_seconds,
            )
        except (RuntimeError, ValueError):
            self._pipeline = None
            raise RuntimeError("GStreamer RTP relay failed during startup") from None

    def _stop_sync(self) -> None:
        pipeline = self._pipeline
        self._pipeline = None
        if pipeline is not None:
            pipeline.close(timeout_seconds=self._stop_timeout_seconds)

    async def start(self, source_uri: str) -> None:
        await asyncio.to_thread(self._start_sync, source_uri)

    async def stop(self) -> None:
        await asyncio.to_thread(self._stop_sync)

    async def close(self) -> None:
        await self.stop()
