"""Physical Stage-One witness joining public RTSP to analytics and Windows overlay.

This opt-in witness consumes one credential-free public RTSP source through the same
public source resolver and direct RTSP/TCP delivery used by the alpha path, then joins
the pinned Analytics-lab detector/tracker to the real Windows operator overlay runtime.
Only aggregate counters and exact revisions are retained; no URI, frame, screenshot,
credential, or media payload is written to the receipt.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from k5vision.domain.devices import Device, DeviceProtocol
from k5vision.operator_launch import OperatorLauncher, OperatorLaunchMetrics, OperatorSourceResolver
from k5vision.operator_runtime import (
    PUBLIC_TEST_SOURCE_ENV,
    PUBLIC_TEST_SOURCE_IP_ENV,
    STAGE_ONE_STREAM_TOKEN_ENV,
    build_environment_operator_runtime,
    resolve_public_test_source_ip,
)

_JOINED_SPEC = importlib.util.spec_from_file_location(
    "k5_joined_analytics_witness",
    Path(__file__).with_name("test_stage_one_joined_analytics_physical.py"),
)
assert _JOINED_SPEC is not None and _JOINED_SPEC.loader is not None
_JOINED_MODULE = importlib.util.module_from_spec(_JOINED_SPEC)
_JOINED_SPEC.loader.exec_module(_JOINED_MODULE)
_JoinedAnalyticsProvider = _JOINED_MODULE._JoinedAnalyticsProvider
_source_free_evidence = _JOINED_MODULE._source_free_evidence

pytestmark = pytest.mark.skipif(
    os.getenv("K5_STAGE_ONE_PUBLIC_RTSP_JOINED_ANALYTICS_PHYSICAL") != "1",
    reason="Stage One public RTSP joined analytics/Windows qualification is opt-in",
)


async def _run_public_witness(
    resolver: OperatorSourceResolver,
    launcher: OperatorLauncher,
    device: Device,
) -> OperatorLaunchMetrics:
    source = await resolver.resolve(device, "public-test")
    return await launcher.run(source, width=1280, height=720)


def test_public_rtsp_detector_tracker_overlay_reaches_windows_operator() -> None:
    output_value = os.getenv("K5_STAGE_ONE_PUBLIC_RTSP_JOINED_ANALYTICS_OUTPUT")
    assert output_value
    output = Path(output_value)
    output.unlink(missing_ok=True)

    public_source = os.getenv(PUBLIC_TEST_SOURCE_ENV, "").strip()
    assert public_source
    public_ip = resolve_public_test_source_ip(public_source)

    evidence_root_value = os.getenv("K5_ANALYTICS_EVIDENCE_ROOT")
    assert evidence_root_value
    evidence_root = Path(evidence_root_value).resolve(strict=True)
    provider = _JoinedAnalyticsProvider(evidence_root)

    resolver, launcher = build_environment_operator_runtime(
        {
            PUBLIC_TEST_SOURCE_ENV: public_source,
            PUBLIC_TEST_SOURCE_IP_ENV: public_ip,
            STAGE_ONE_STREAM_TOKEN_ENV: "public-test",
        },
        detection_provider=provider,
    )
    assert resolver is not None
    assert launcher is not None

    parsed = urlsplit(public_source)
    port = parsed.port if parsed.port is not None else 554
    device = Device(
        name="K5 Public RTSP Test",
        host=public_ip,
        management_port=port,
        protocols={DeviceProtocol.RTSP},
        tags={"alpha-public-test", "ephemeral", "non-recording"},
    )
    metrics = asyncio.run(_run_public_witness(resolver, launcher, device))

    evidence = _source_free_evidence(
        metrics,
        provider_calls=provider.provider_calls,
        tracked_detections=provider.tracked_detections,
        revision=os.getenv("K5_STAGE_ONE_REVISION", ""),
        analytics_revision=os.getenv("ANALYTICS_LAB_SHA", ""),
    )
    evidence["execution_context"] = "public-rtsp-windows-x64"
    evidence["rtsp_tcp_joined"] = True
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
