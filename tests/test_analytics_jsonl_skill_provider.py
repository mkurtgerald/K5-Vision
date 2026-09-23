from __future__ import annotations

import asyncio
import sys

import pytest

from k5vision.analytics.jsonl_skill_provider import JsonlSkillProvider, SkillProviderError
from k5vision.media.presentation_frame import PixelFormat, PresentationVideoFrame

_SKILL_SCRIPT = r"""
import json
import sys
from multiprocessing import shared_memory

for line in sys.stdin:
    message = json.loads(line)
    if message.get("command") == "stop":
        break
    if message.get("event") != "frame":
        continue
    segment = shared_memory.SharedMemory(name=message["shared_memory_name"])
    try:
        payload = bytes(segment.buf[: message["byte_length"]])
    finally:
        segment.close()
    if not payload:
        print(json.dumps({"event": "error", "message": "empty"}), flush=True)
        continue
    print(json.dumps({
        "event": "detections",
        "frame_id": message["frame_id"],
        "objects": [{
            "class": "person",
            "confidence": 0.91,
            "bbox": [0, 0, message["width"], message["height"]]
        }]
    }), flush=True)
"""


def _frame() -> PresentationVideoFrame:
    return PresentationVideoFrame(
        payload=memoryview(bytes(range(16))),
        width=2,
        height=2,
        stride_bytes=8,
        pixel_format=PixelFormat.BGRX,
        source_elapsed_ms=50,
    )


def test_provider_moves_transient_frame_through_shared_memory() -> None:
    async def run() -> None:
        provider = JsonlSkillProvider(
            (sys.executable, "-c", _SKILL_SCRIPT),
            response_timeout_seconds=2.0,
        )
        try:
            detections = await provider(_frame())
        finally:
            await provider.close()

        assert len(detections) == 1
        assert detections[0].category == "person"
        assert detections[0].confidence == pytest.approx(0.91)
        assert detections[0].box.x_min == 0
        assert detections[0].box.y_min == 0
        assert detections[0].box.x_max == 1
        assert detections[0].box.y_max == 1

    asyncio.run(run())


def test_provider_rejects_invalid_command_and_frame() -> None:
    with pytest.raises(ValueError):
        JsonlSkillProvider(())

    async def run() -> None:
        provider = JsonlSkillProvider((sys.executable, "-c", "pass"))
        try:
            with pytest.raises(TypeError):
                await provider(object())  # type: ignore[arg-type]
        finally:
            await provider.close()

    asyncio.run(run())


def test_provider_reports_child_exit_without_leaking_child_output() -> None:
    async def run() -> None:
        provider = JsonlSkillProvider(
            (sys.executable, "-c", "import sys; sys.exit(3)"),
            response_timeout_seconds=1.0,
        )
        try:
            with pytest.raises(SkillProviderError, match="exited unexpectedly"):
                await provider(_frame())
        finally:
            await provider.close()

    asyncio.run(run())
