"""Physical Stage-06 qualification for bounded ephemeral live-view lifecycle."""

import asyncio
import os
from pathlib import Path

import pytest

from k5vision.media.gstreamer_udp import GStreamerUdpRuntime
from k5vision.media.live_view import LiveViewBoundary
from k5vision.media.live_view_evidence import LiveViewPhysicalEvidence
from k5vision.media.session import MediaSession
from k5vision.stage03_credentials import selected_source_uri

pytestmark = pytest.mark.skipif(
    os.getenv("K5_STAGE06_PHYSICAL") != "1",
    reason="Stage 06 physical qualification is opt-in",
)


def test_stage06_live_view_lifecycle_on_physical_source() -> None:
    source = os.environ["K5_STAGE03_SOURCE"]
    credentials = os.environ["K5_STAGE03_CAM_CRED"]
    credential_index = int(os.environ["K5_STAGE03_CREDENTIAL_INDEX"])
    output = Path(os.environ["K5_STAGE06_OUTPUT"])
    revision = os.getenv("K5_STAGE06_REVISION", "local").casefold()
    authenticated_source = selected_source_uri(source, credentials, credential_index)

    async def qualify() -> LiveViewPhysicalEvidence:
        runtime = GStreamerUdpRuntime(startup_probe_seconds=0.5)
        boundary = LiveViewBoundary(
            MediaSession(runtime),
            max_consumers=2,
            operation_timeout_seconds=30.0,
        )

        first = await boundary.acquire(authenticated_source)
        second = await boundary.acquire(authenticated_source)
        peak = boundary.snapshot

        await boundary.release(first.lease_id)
        partial = boundary.snapshot
        await boundary.release(second.lease_id)
        released = boundary.snapshot

        reentry = await boundary.acquire(authenticated_source)
        await boundary.release(reentry.lease_id)
        final = await boundary.close()

        return LiveViewPhysicalEvidence(
            revision=revision,
            execution_context="camera-lab-windows-x64",
            max_consumers=2,
            first_generation=first.generation,
            shared_generation=second.generation,
            peak_consumers=peak.active_consumers,
            consumers_after_partial_release=partial.active_consumers,
            state_after_final_release=released.state,
            reentry_generation=reentry.generation,
            final_state=final.state,
        )

    evidence = asyncio.run(qualify())
    assert evidence.accepted
    assert evidence.revision == revision
    assert evidence.execution_context == "camera-lab-windows-x64"

    payload = evidence.model_dump_json(indent=2) + "\n"
    assert authenticated_source not in payload
    assert source not in payload
    assert "credential" not in payload.casefold()
    assert "runner" not in payload.casefold()

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(payload, encoding="utf-8")
