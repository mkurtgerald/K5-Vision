from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from k5vision.media.live_view import LiveViewBoundary, LiveViewError, LiveViewErrorCode
from k5vision.media.live_view_evidence import LiveViewPhysicalEvidence
from k5vision.media.session import MediaSession


class FakeRuntime:
    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0
        self.closes = 0

    async def start(self, source_uri: str) -> None:
        self.starts += 1

    async def stop(self) -> None:
        self.stops += 1

    async def close(self) -> None:
        self.closes += 1


def test_active_boundary_rejects_different_source_without_leaking_it() -> None:
    async def exercise() -> None:
        runtime = FakeRuntime()
        boundary = LiveViewBoundary(MediaSession(runtime), max_consumers=2)
        source_a = "rtsp://user:secret@192.0.2.10/a"
        source_b = "rtsp://other:hidden@192.0.2.11/b"

        first = await boundary.acquire(source_a)
        with pytest.raises(LiveViewError) as caught:
            await boundary.acquire(source_b)

        rendered = str(caught.value)
        assert caught.value.code == LiveViewErrorCode.SOURCE_MISMATCH
        assert source_a not in rendered
        assert source_b not in rendered
        assert "192.0.2.10" not in rendered
        assert "192.0.2.11" not in rendered
        assert runtime.starts == 1
        assert boundary.snapshot.active_consumers == 1

        snapshot_payload = boundary.snapshot.model_dump_json()
        assert source_a not in snapshot_payload
        assert source_b not in snapshot_payload

        await boundary.release(first.lease_id)
        second = await boundary.acquire(source_b)
        assert second.generation == 2
        assert runtime.starts == 2
        await boundary.release(second.lease_id)

    asyncio.run(exercise())


def test_physical_evidence_rejects_literal_network_identity() -> None:
    with pytest.raises(ValidationError):
        LiveViewPhysicalEvidence(
            revision="a" * 40,
            execution_context="192.0.2.10",
            max_consumers=2,
            first_generation=1,
            shared_generation=1,
            peak_consumers=2,
            consumers_after_partial_release=1,
            state_after_final_release="idle",
            reentry_generation=2,
            final_state="closed",
        )


def test_physical_evidence_requires_shared_generation_and_clean_close() -> None:
    with pytest.raises(ValidationError):
        LiveViewPhysicalEvidence(
            revision="b" * 40,
            execution_context="camera-lab-windows-x64",
            max_consumers=2,
            first_generation=1,
            shared_generation=2,
            peak_consumers=2,
            consumers_after_partial_release=1,
            state_after_final_release="idle",
            reentry_generation=3,
            final_state="closed",
        )
