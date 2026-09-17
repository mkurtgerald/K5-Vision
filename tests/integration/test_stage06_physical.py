"""Physical Stage-06 qualification for bounded ephemeral live-view delivery."""

import asyncio
import os
from pathlib import Path

import pytest

from k5vision.media.gstreamer_udp import GStreamerUdpRuntime
from k5vision.media.live_view import LiveViewBoundary
from k5vision.media.live_view_evidence import LiveViewPhysicalEvidence
from k5vision.media.rtp_delivery import EphemeralRtpDelivery
from k5vision.media.rtp_delivery_evidence import RtpDeliveryEvidence
from k5vision.media.session import MediaSession
from k5vision.stage03_credentials import selected_source_uri

pytestmark = pytest.mark.skipif(
    os.getenv("K5_STAGE06_PHYSICAL") != "1",
    reason="Stage 06 physical qualification is opt-in",
)


def _physical_context() -> tuple[str, str, Path, str]:
    source = os.environ["K5_STAGE03_SOURCE"]
    credentials = os.environ["K5_STAGE03_CAM_CRED"]
    credential_index = int(os.environ["K5_STAGE03_CREDENTIAL_INDEX"])
    output = Path(os.environ["K5_STAGE06_OUTPUT"])
    revision = os.getenv("K5_STAGE06_REVISION", "local").casefold()
    authenticated_source = selected_source_uri(source, credentials, credential_index)
    return source, authenticated_source, output, revision


def _assert_source_free(payload: str, source: str, authenticated_source: str) -> None:
    assert authenticated_source not in payload
    assert source not in payload
    assert "credential" not in payload.casefold()
    assert "runner" not in payload.casefold()


def test_stage06_live_view_lifecycle_on_physical_source() -> None:
    source, authenticated_source, output, revision = _physical_context()

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
    _assert_source_free(payload, source, authenticated_source)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(payload, encoding="utf-8")


def test_stage06_rtp_reaches_non_retaining_k5_consumer() -> None:
    source, authenticated_source, output, revision = _physical_context()

    async def qualify() -> RtpDeliveryEvidence:
        callbacks = 0

        async def non_retaining_consumer(packet: memoryview) -> None:
            nonlocal callbacks
            assert len(packet) >= 12
            callbacks += 1

        delivery = EphemeralRtpDelivery(
            packet_goal=16,
            delivery_timeout_seconds=30.0,
            consumer_timeout_seconds=1.0,
            relay_startup_probe_seconds=0.5,
        )
        result = await delivery.deliver(authenticated_source, non_retaining_consumer)
        return RtpDeliveryEvidence(
            revision=revision,
            execution_context="camera-lab-windows-x64",
            valid_packets=result.valid_packets,
            invalid_packets=result.invalid_packets,
            delivered_bytes=result.delivered_bytes,
            consumer_callbacks=callbacks,
            elapsed_ms=result.elapsed_ms,
        )

    evidence = asyncio.run(qualify())
    assert evidence.accepted
    assert evidence.revision == revision
    assert evidence.consumer_callbacks == evidence.valid_packets

    payload = evidence.model_dump_json(indent=2) + "\n"
    _assert_source_free(payload, source, authenticated_source)

    delivery_output = output.with_name("stage06-rtp-delivery.json")
    delivery_output.parent.mkdir(parents=True, exist_ok=True)
    delivery_output.write_text(payload, encoding="utf-8")
