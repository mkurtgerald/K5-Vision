"""Physical Stage-05 readiness qualification for the reviewed transport/session surface."""

import asyncio
import os
from pathlib import Path

import pytest

from k5vision.media.gstreamer_udp import GStreamerUdpRuntime
from k5vision.media.readiness import ReadinessPlan
from k5vision.media.readiness_runner import qualify_readiness
from k5vision.stage03_credentials import selected_source_uri

pytestmark = pytest.mark.skipif(
    os.getenv("K5_STAGE05_PHYSICAL") != "1",
    reason="Stage 05 physical qualification is opt-in",
)


def test_stage05_transport_session_readiness_workload() -> None:
    source = os.environ["K5_STAGE03_SOURCE"]
    credentials = os.environ["K5_STAGE03_CAM_CRED"]
    credential_index = int(os.environ["K5_STAGE03_CREDENTIAL_INDEX"])
    output = Path(os.environ["K5_STAGE05_OUTPUT"])
    revision = os.getenv("K5_STAGE05_REVISION", "local").casefold()
    authenticated_source = selected_source_uri(source, credentials, credential_index)

    plan = ReadinessPlan(
        cycles_per_level=2,
        concurrency_ladder=(1, 2),
        operation_timeout_seconds=30.0,
    )

    evidence = asyncio.run(
        qualify_readiness(
            lambda: GStreamerUdpRuntime(startup_probe_seconds=0.5),
            authenticated_source,
            revision=revision,
            plan=plan,
        )
    )
    assert evidence.accepted

    payload = evidence.model_dump_json(indent=2) + "\n"
    assert authenticated_source not in payload
    assert source not in payload
    assert "credential" not in payload.casefold()
    assert "runner" not in payload.casefold()

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(payload, encoding="utf-8")
