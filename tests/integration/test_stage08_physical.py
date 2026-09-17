"""Physical Stage-08 qualification for bounded local persistence.

Live camera RTP is written only into a temporary qualification directory. The
recording is finalized, measured, and deleted before source-free evidence is
written. No camera payload is retained as a workflow artifact.
"""

import asyncio
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from k5vision.media.file_sink import AtomicLocalRecordingSink, FileSinkState
from k5vision.media.persistence_evidence import LocalPersistenceEvidence
from k5vision.media.recording import BoundedRtpRecorder, RecordingState
from k5vision.media.rtp_delivery import EphemeralRtpDelivery
from k5vision.stage03_credentials import selected_source_uri

pytestmark = pytest.mark.skipif(
    os.getenv("K5_STAGE08_PHYSICAL") != "1",
    reason="Stage 08 physical qualification is opt-in",
)


def _physical_context() -> tuple[str, str, Path, str]:
    source = os.environ["K5_STAGE03_SOURCE"]
    credentials = os.environ["K5_STAGE03_CAM_CRED"]
    credential_index = int(os.environ["K5_STAGE03_CREDENTIAL_INDEX"])
    output = Path(os.environ["K5_STAGE08_OUTPUT"])
    revision = os.getenv("K5_STAGE08_REVISION", "local").casefold()
    authenticated_source = selected_source_uri(source, credentials, credential_index)
    return source, authenticated_source, output, revision


def _assert_source_free(payload: str, source: str, authenticated_source: str) -> None:
    assert authenticated_source not in payload
    assert source not in payload
    lowered = payload.casefold()
    assert "credential" not in lowered
    assert "runner" not in lowered
    assert "rtsp://" not in lowered


def test_stage08_live_rtp_persists_temporarily_then_is_deleted() -> None:
    source, authenticated_source, output, revision = _physical_context()

    async def qualify() -> LocalPersistenceEvidence:
        with TemporaryDirectory(prefix="k5-stage08-") as temp_dir:
            root = Path(temp_dir)
            sink = AtomicLocalRecordingSink(
                root,
                "physical-qualification",
                max_bytes=8 * 1024 * 1024,
            )
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

            try:
                await recorder.start()

                async def recording_consumer(packet: memoryview) -> None:
                    await recorder.consume(packet)

                result = await delivery.deliver(authenticated_source, recording_consumer)
                final = await recorder.finalize()
                assert final.state == RecordingState.FINALIZED
                assert sink.snapshot.state == FileSinkState.FINALIZED
                persisted_bytes = (root / "physical-qualification.rtp").stat().st_size
                sink_state = sink.snapshot.state
            except BaseException:
                if recorder.snapshot.state != RecordingState.FINALIZED:
                    await recorder.abort()
                raise

        cleanup_confirmed = not root.exists()
        return LocalPersistenceEvidence(
            revision=revision,
            execution_context="camera-lab-windows-x64",
            delivered_packets=result.valid_packets,
            delivered_bytes=result.delivered_bytes,
            persisted_bytes=persisted_bytes,
            sink_state=sink_state,
            cleanup_confirmed=cleanup_confirmed,
            elapsed_ms=result.elapsed_ms,
        )

    evidence = asyncio.run(qualify())
    assert evidence.accepted
    assert evidence.revision == revision
    assert evidence.cleanup_confirmed

    payload = evidence.model_dump_json(indent=2) + "\n"
    _assert_source_free(payload, source, authenticated_source)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(payload, encoding="utf-8")
