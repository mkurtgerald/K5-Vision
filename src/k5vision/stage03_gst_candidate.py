"""Secret-safe Stage 03 GStreamer transport candidate wrapper."""

from __future__ import annotations

import os
import sys

from k5vision.media.native_rtsp_pipeline import (
    NativeRtspPipeline,
    NativeRtspPipelineError,
    NativeRtspPipelineUnavailable,
    quote_pipeline_value,
)
from k5vision.stage03_credentials import selected_source_uri

_ALLOWED_TRANSPORTS = ("tcp", "udp")
_SOURCE_ARG_HANDLE = "env:K5_STAGE03_SOURCE"
_SOURCE_ENV = "K5_STAGE03_SOURCE"
_GST_AUTH_FAILURE = 41
_GST_CONNECT_FAILURE = 42
_GST_NEGOTIATION_FAILURE = 43
_GST_SOURCE_FAILURE = 44
_GST_PIPELINE_FAILURE = 45
_GST_OTHER_FAILURE = 46
_GST_WRAPPER_OS_FAILURE = 47
_GST_WRAPPER_INTERNAL_FAILURE = 48


def build_gst_pipeline(transport: str, source_uri: str) -> str:
    """Build one finite raw-RTP receive path for in-process GStreamer parsing."""
    if transport not in _ALLOWED_TRANSPORTS:
        raise ValueError("unsupported transport candidate")
    location = quote_pipeline_value(source_uri)
    return (
        f"rtspsrc location={location} protocols={transport} latency=100 "
        "tcp-timeout=5000000 teardown-timeout=0 "
        "! queue ! identity eos-after=60 ! fakesink sync=false"
    )


def classify_gst_failure(stderr: str) -> int:
    """Map legacy raw diagnostics to a bounded secret-safe status."""
    normalized = stderr.casefold()
    if any(
        marker in normalized
        for marker in (
            "401",
            "403",
            "unauthorized",
            "forbidden",
            "not authorized",
            "authentication",
            "authentication required",
        )
    ):
        return _GST_AUTH_FAILURE
    if any(
        marker in normalized
        for marker in (
            "could not connect",
            "connection refused",
            "connection timed out",
            "timed out",
            "network is unreachable",
            "no route to host",
            "failed to connect",
        )
    ):
        return _GST_CONNECT_FAILURE
    if any(
        marker in normalized
        for marker in (
            "not-negotiated",
            "not negotiated",
            "not-linked",
            "not linked",
            "failed delayed linking",
            "internal data stream error",
            "streaming stopped",
            "missing plugin",
            "no suitable plugins",
            "no decoder",
            "could not link",
            "caps",
        )
    ):
        return _GST_NEGOTIATION_FAILURE
    if any(
        marker in normalized
        for marker in ("404", "not found", "resource not found", "no such resource")
    ):
        return _GST_SOURCE_FAILURE
    if any(
        marker in normalized
        for marker in ("erroneous pipeline", "no property", "syntax error", "no element")
    ):
        return _GST_PIPELINE_FAILURE
    return _GST_OTHER_FAILURE


def _qualification_timeout_seconds() -> float:
    raw = os.getenv("K5_STAGE03_TIMEOUT", "10").strip()
    try:
        timeout = float(raw)
    except ValueError:
        return 10.0
    return min(max(timeout, 0.1), 120.0)


def run_gst_uri(transport: str, source_uri: str) -> int:
    """Run one finite native GStreamer proof and return only a sanitized status."""
    pipeline: NativeRtspPipeline | None = None
    try:
        pipeline = NativeRtspPipeline(
            build_gst_pipeline(transport, source_uri),
            startup_probe_seconds=min(_qualification_timeout_seconds(), 0.5),
        )
        terminal = pipeline.wait_for_terminal(timeout_seconds=_qualification_timeout_seconds())
    except NativeRtspPipelineUnavailable:
        return 127
    except (NativeRtspPipelineError, OSError):
        return _GST_OTHER_FAILURE
    except Exception:
        return _GST_WRAPPER_INTERNAL_FAILURE
    finally:
        if pipeline is not None:
            pipeline.close(timeout_seconds=1.0)

    if terminal == "eos":
        return 0
    if terminal == "timeout":
        return _GST_CONNECT_FAILURE
    return _GST_OTHER_FAILURE


def run_candidate(transport: str, source_uri: str) -> int:
    """Run one candidate using the preselected private credential when configured."""
    cam_cred = os.getenv("K5_STAGE03_CAM_CRED")
    credential_index = os.getenv("K5_STAGE03_CREDENTIAL_INDEX")
    if cam_cred and credential_index is not None:
        try:
            source_uri = selected_source_uri(source_uri, cam_cred, int(credential_index))
        except (TypeError, ValueError):
            return _GST_AUTH_FAILURE
    return run_gst_uri(transport, source_uri)


def main() -> int:
    args = sys.argv[1:]
    if len(args) != 3 or args[0] != "--transport":
        return 2
    transport, source_handle = args[1], args[2]
    if transport not in _ALLOWED_TRANSPORTS or source_handle != _SOURCE_ARG_HANDLE:
        return 2
    source_uri = os.getenv(_SOURCE_ENV)
    if not source_uri or not source_uri.strip():
        return 2
    return run_candidate(transport, source_uri)


if __name__ == "__main__":
    raise SystemExit(main())
