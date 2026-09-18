"""Physical Stage-20 qualification for ephemeral RTP-to-decoded-frame delivery."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from k5vision.media.gstreamer_playback_decoder import GStreamerNativePlaybackDecoder
from k5vision.media.playback_decoder_evidence import PlaybackDecoderPhysicalEvidence
from k5vision.media.rtp_delivery import EphemeralRtpDelivery
from k5vision.stage03_credentials import selected_source_uri

pytestmark = pytest.mark.skipif(
    os.getenv("K5_STAGE20_PHYSICAL") != "1",
    reason="Stage 20 physical qualification is opt-in",
)


def _physical_context() -> tuple[str, str, Path, str]:
    source = os.environ["K5_STAGE03_SOURCE"]
    credentials = os.environ["K5_STAGE03_CAM_CRED"]
    credential_index = int(os.environ["K5_STAGE03_CREDENTIAL_INDEX"])
    output = Path(os.environ["K5_STAGE20_OUTPUT"])
    revision = os.environ["K5_STAGE20_REVISION"].casefold()
    authenticated_source = selected_source_uri(source, credentials, credential_index)
    return source, authenticated_source, output, revision


def _assert_source_free(payload: str, source: str, authenticated_source: str) -> None:
    assert source not in payload
    assert authenticated_source not in payload
    lowered = payload.casefold()
    for token in ("rtsp://", "credential", "password", "runner_name", "payload"):
        assert token not in lowered


def test_stage20_ephemeral_h264_rtp_decodes_without_retention() -> None:
    source, authenticated_source, output, revision = _physical_context()

    async def qualify() -> PlaybackDecoderPhysicalEvidence:
        decoder: GStreamerNativePlaybackDecoder | None = None
        decoded_frames = 0
        decoded_bytes = 0

        async def consume(packet: memoryview) -> None:
            nonlocal decoder, decoded_frames, decoded_bytes
            if decoder is None:
                payload_type = int(packet[1] & 0x7F)
                decoder = GStreamerNativePlaybackDecoder(
                    payload_type,
                    operation_timeout_seconds=2.0,
                    pull_timeout_ms=1,
                )
            frames = await decoder.decode(packet, 0)
            for frame in frames:
                decoded_frames += 1
                decoded_bytes += len(frame.payload)

        delivery = EphemeralRtpDelivery(
            packet_goal=2048,
            delivery_timeout_seconds=30.0,
            consumer_timeout_seconds=3.0,
            relay_startup_probe_seconds=0.5,
        )

        try:
            result = await delivery.deliver(authenticated_source, consume)
            if decoder is None:
                raise AssertionError("decoder was not created")
            tail = await decoder.flush()
            for frame in tail:
                decoded_frames += 1
                decoded_bytes += len(frame.payload)
            await decoder.close()
            final_state = decoder.snapshot.state
        finally:
            if decoder is not None and decoder.snapshot.state != "closed":
                try:
                    await decoder.close()
                except Exception:
                    pass

        assert decoded_frames >= 1
        assert decoded_bytes >= decoded_frames
        return PlaybackDecoderPhysicalEvidence(
            revision=revision,
            execution_context="camera-lab-windows-x64",
            valid_rtp_packets=result.valid_packets,
            delivered_bytes=result.delivered_bytes,
            decoded_frames=decoded_frames,
            decoded_bytes=decoded_bytes,
            final_state=final_state,
        )

    evidence = asyncio.run(qualify())
    payload = evidence.model_dump_json(indent=2) + "\n"
    _assert_source_free(payload, source, authenticated_source)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(payload, encoding="utf-8")
