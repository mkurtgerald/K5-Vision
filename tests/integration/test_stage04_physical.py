from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from k5vision.media.gstreamer_udp import GStreamerUdpRuntime
from k5vision.media.session import MediaSession
from k5vision.media.stage04_evidence import Stage04PhysicalEvidence
from k5vision.stage03_credentials import selected_source_uri
from k5vision.stage03_gst_candidate import run_gst_uri


pytestmark = pytest.mark.skipif(
    os.getenv("K5_STAGE04_PHYSICAL") != "1",
    reason="Stage 04 physical qualification is opt-in",
)


def test_stage04_selected_udp_runtime_lifecycle() -> None:
    source = os.environ["K5_STAGE03_SOURCE"]
    credentials = os.environ["K5_STAGE03_CAM_CRED"]
    credential_index = int(os.environ["K5_STAGE03_CREDENTIAL_INDEX"])
    output = Path(os.environ["K5_STAGE04_OUTPUT"])
    revision = os.getenv("GITHUB_SHA", "local").casefold()
    authenticated_source = selected_source_uri(source, credentials, credential_index)

    # Reuse the Stage-03 finite, secret-safe receive proof before exercising
    # the persistent Stage-04 lifecycle adapter. No media or raw diagnostics
    # are retained by either path.
    receive_probe_passed = run_gst_uri("udp", authenticated_source) == 0
    assert receive_probe_passed

    async def scenario() -> Stage04PhysicalEvidence:
        session = MediaSession(GStreamerUdpRuntime(startup_probe_seconds=0.5))
        states = []
        states.append((await session.start(authenticated_source)).state)
        states.append((await session.stop()).state)
        states.append((await session.start(authenticated_source)).state)
        final = await session.close()
        states.append(final.state)
        return Stage04PhysicalEvidence(
            revision=revision,
            receive_probe_passed=receive_probe_passed,
            lifecycle_states=tuple(states),
            generation_count=final.generation,
            final_state=final.state,
        )

    evidence = asyncio.run(scenario())
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(evidence.model_dump_json(indent=2) + "\n", encoding="utf-8")
