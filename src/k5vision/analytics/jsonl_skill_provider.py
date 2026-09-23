"""Bounded in-memory JSONL analytics process provider.

This adapts the useful DeepCamera-style process boundary to K5 without adopting its
frame-on-disk transport. Decoded camera pixels are copied into OS shared memory for
the duration of one request and are unlinked immediately afterward.

The provider does not grant the child process access to source URIs, credentials,
recording paths, operator identity, or evidence state.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from multiprocessing import shared_memory

from k5vision.analytics.skill_protocol import (
    SkillProtocolError,
    SkillTrackedDetection,
    build_shared_memory_frame_message,
    decode_event,
    parse_detection_event,
)
from k5vision.media.presentation_frame import PresentationVideoFrame

_MAX_RESPONSE_BYTES = 256 * 1024
_MAX_EVENTS_PER_REQUEST = 64


class SkillProviderError(RuntimeError):
    """Sanitized analytics process failure."""


class JsonlSkillProvider:
    """Run one optional detector process behind K5's existing analytics seam."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        environment: Mapping[str, str] | None = None,
        response_timeout_seconds: float = 2.0,
        max_detections: int = 128,
        max_response_bytes: int = _MAX_RESPONSE_BYTES,
    ) -> None:
        selected = tuple(command)
        if (
            not selected
            or any(not isinstance(part, str) or not part for part in selected)
            or any("\x00" in part for part in selected)
        ):
            raise ValueError("analytics skill command is invalid")
        if not 0 < response_timeout_seconds <= 10:
            raise ValueError("analytics response timeout must be between zero and 10 seconds")
        if type(max_detections) is not int or not 1 <= max_detections <= 512:
            raise ValueError("max detections must be between 1 and 512")
        if type(max_response_bytes) is not int or not 1024 <= max_response_bytes <= 1024 * 1024:
            raise ValueError("max response bytes must be between 1024 and 1048576")

        if environment is not None:
            for key, value in environment.items():
                if (
                    not isinstance(key, str)
                    or not key
                    or not isinstance(value, str)
                    or "\x00" in key
                    or "\x00" in value
                ):
                    raise ValueError("analytics skill environment is invalid")

        self._command = selected
        self._environment = None if environment is None else dict(environment)
        self._response_timeout_seconds = response_timeout_seconds
        self._max_detections = max_detections
        self._max_response_bytes = max_response_bytes
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._frame_id = 0
        self._closed = False

    async def _ensure_started(self) -> asyncio.subprocess.Process:
        process = self._process
        if process is not None and process.returncode is None:
            return process
        if self._closed:
            raise SkillProviderError("analytics skill provider is closed")

        try:
            process = await asyncio.create_subprocess_exec(
                *self._command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=self._environment,
                limit=self._max_response_bytes + 1,
            )
        except (OSError, ValueError) as exc:
            raise SkillProviderError("analytics skill process could not start") from exc
        if process.stdin is None or process.stdout is None:
            process.kill()
            await process.wait()
            raise SkillProviderError("analytics skill process pipes are unavailable")
        self._process = process
        return process

    async def _read_detection_event(
        self,
        process: asyncio.subprocess.Process,
        *,
        expected_frame_id: int,
        width: int,
        height: int,
    ) -> tuple[SkillTrackedDetection, ...]:
        assert process.stdout is not None

        for _ in range(_MAX_EVENTS_PER_REQUEST):
            try:
                line = await asyncio.wait_for(
                    process.stdout.readline(),
                    timeout=self._response_timeout_seconds,
                )
            except asyncio.TimeoutError as exc:
                raise SkillProviderError("analytics skill response timed out") from exc
            except (ValueError, asyncio.LimitOverrunError) as exc:
                raise SkillProviderError("analytics skill response exceeded configured bound") from exc

            if not line:
                raise SkillProviderError("analytics skill process exited unexpectedly")
            try:
                event = decode_event(line, max_response_bytes=self._max_response_bytes)
            except SkillProtocolError as exc:
                raise SkillProviderError(str(exc)) from exc

            event_name = event.get("event")
            if event_name in {"ready", "progress", "perf_stats"}:
                continue
            if event_name == "error":
                raise SkillProviderError("analytics skill reported an inference failure")
            if event_name != "detections":
                raise SkillProviderError("analytics skill returned an unsupported event")

            try:
                return parse_detection_event(
                    event,
                    expected_frame_id=expected_frame_id,
                    width=width,
                    height=height,
                    max_detections=self._max_detections,
                )
            except SkillProtocolError as exc:
                raise SkillProviderError(str(exc)) from exc

        raise SkillProviderError("analytics skill emitted too many non-result events")

    async def __call__(
        self,
        frame: PresentationVideoFrame,
    ) -> tuple[SkillTrackedDetection, ...]:
        if not isinstance(frame, PresentationVideoFrame):
            raise TypeError("analytics provider requires a presentation frame")

        async with self._lock:
            process = await self._ensure_started()
            if process.stdin is None:
                raise SkillProviderError("analytics skill process input is unavailable")

            frame_id = self._frame_id
            self._frame_id += 1
            segment = shared_memory.SharedMemory(create=True, size=len(frame.payload))
            try:
                segment.buf[: len(frame.payload)] = frame.payload
                message = build_shared_memory_frame_message(
                    frame_id=frame_id,
                    shared_memory_name=segment.name,
                    byte_length=len(frame.payload),
                    width=frame.width,
                    height=frame.height,
                    stride_bytes=frame.stride_bytes,
                    pixel_format=str(frame.pixel_format.value),
                    source_elapsed_ms=frame.source_elapsed_ms,
                )
                payload = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
                try:
                    process.stdin.write(payload)
                    await process.stdin.drain()
                except (BrokenPipeError, ConnectionError, RuntimeError) as exc:
                    raise SkillProviderError("analytics skill process input failed") from exc

                return await self._read_detection_event(
                    process,
                    expected_frame_id=frame_id,
                    width=frame.width,
                    height=frame.height,
                )
            finally:
                segment.close()
                try:
                    segment.unlink()
                except FileNotFoundError:
                    pass

    async def close(self) -> None:
        """Stop the optional child process without retaining frame data."""
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            process = self._process
            self._process = None
            if process is None or process.returncode is not None:
                return

            if process.stdin is not None:
                try:
                    process.stdin.write(b'{"command":"stop"}\n')
                    await process.stdin.drain()
                except (BrokenPipeError, ConnectionError, RuntimeError):
                    pass

            try:
                await asyncio.wait_for(process.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()

    async def __aenter__(self) -> "JsonlSkillProvider":
        if self._closed:
            raise SkillProviderError("analytics skill provider is closed")
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()
