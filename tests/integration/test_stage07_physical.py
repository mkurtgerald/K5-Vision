"""Physical Stage-07 qualification for bounded recording ingest.

The physical camera payload is never written to disk. The qualification sink
retains only packet/byte counters; the artifact contains source-free evidence.
"""

import asyncio
import os
from pathlib import Path

import pytest

from k5vision.media.recording import BoundedRtpRecorder, RecordingState
from k5vision.media.recording_evidence import RecordingIngestEvidence
from k5vision.media.rtp_delivery import EphemeralRtpDelivery
from k5vision.stage03_credentials import selected_source_uri

pytestmark = pytest.mark.skipif(
    os.getenv("K5_STAGE07_PHYSICAL") != "1",
    reason="Stage 07 physical qualification is opt-in",
)


class NonRetainingQualificationSink:
    """Exercise the recording sink contract without retaining camera payloads."""

    def __init__(self) -> None:
        self.opened = False
        self.finalized = False
        self.aborted = False
        self.packets = 0
        self.bytes = 0

    async def open(self) -> None:
        self.opened = True

    async def write(self, packet: memoryview) -> None:
        assert self.opened
        assert not self.finalized
        assert not self.aborted
        self.packets += 1
        self.bytes += len(packet)

    async def finalize(self) -> None:
        assert self.opened
        assert not self.aborted
        self.finalized = True

    async def abort(self) -> None:
        self.aborted = True


def _physical_context() -> tuple[str, str, Path, str]:
    source = os.environ["K5_STAGE03_SOURCE"]
    credentials = os.environ["K5_STAGE03_CAM_CRED"]
    credential_index = int(os.environ["K5_STAGE03_CREDENTIAL_INDEX"])
    output = Path(os.environ["K5_STAGE07_OUTPUT"])
    revision = os.getenv("K5_STAGE07_REVISION", "local").casefold()
    authenticated_source = selected_source_uri(source, credentials, credential_index)
    return source, authenticated_source, output, revision


def _assert_source_free(payload: str, source: str, authenticated_source: str) -> None:
    assert authenticated_source not in payload
    assert source not in payload
    assert "credential" not in payload.casefold()
    assert "runner" not in payload.casefold()


def test_stage07_live_rtp_reaches_bounded_recording_sink_without_retention() -> None:
    source, authenticated_source, output, revision = _physical_context()

    async def qualify() -> RecordingIngestEvidence:
        sink = NonRetainingQualificationSink()
        recorder = BoundedRtpRecorder(
            sink,
            max_packets=32,
            max_bytes=8 * 1024 * 1024,
            operation_timeout_seconds=2.0,
        )
        delivery = EphemeralRtpDelivery(
            packet_goal=32,
            delivery_timeout_seconds=30.0,
            consumer_timeout_seconds=2.0,
            relay_startup_probe_seconds=0.5,
        )

        await recorder.start()

        async def recording_consumer(packet: memoryview) -> None:
            await recorder.consume(packet)

        result = await delivery.deliver(authenticated_source, recording_consumer)
        final = await recorder.finalize()

        assert final.state == RecordingState.FINALIZED
        assert final.packets_written == sink.packets
        assert final.bytes_written == sink.bytes

        return RecordingIngestEvidence(
            revision=revision,
            execution_context="camera-lab-windows-x64",
            delivered_packets=result.valid_packets,
            delivered_bytes=result.delivered_bytes,
            sink_packets=sink.packets,
            sink_bytes=sink.bytes,
            final_state=final.state,
            elapsed_ms=result.elapsed_ms,
        )

    evidence = asyncio.run(qualify())
    assert evidence.accepted
    assert evidence.revision == revision
    assert evidence.execution_context == "camera-lab-windows-x64"

    payload = evidence.model_dump_json(indent=2) + "\n"
    _assert_source_free(payload, source, authenticated_source)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(payload, encoding="utf-8")
