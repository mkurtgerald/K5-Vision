"""Secret-safe Stage 03 GStreamer transport candidate wrapper."""

from __future__ import annotations

import argparse
import shutil
import subprocess

_GST_LAUNCH = "gst-launch-1.0"
_ALLOWED_TRANSPORTS = ("tcp", "udp")

# Exit statuses intentionally expose only a coarse failure class to the outer
# qualification harness. Raw GStreamer stderr may contain a sensitive RTSP URI
# and is therefore never emitted or retained.
_GST_AUTH_FAILURE = 41
_GST_CONNECT_FAILURE = 42
_GST_NEGOTIATION_FAILURE = 43
_GST_SOURCE_FAILURE = 44
_GST_PIPELINE_FAILURE = 45
_GST_OTHER_FAILURE = 46
_GST_WRAPPER_OS_FAILURE = 47
_GST_WRAPPER_INTERNAL_FAILURE = 48


def build_gst_argv(transport: str, source_uri: str) -> list[str]:
    """Build one bounded RTP receive path without invoking a command shell."""
    if transport not in _ALLOWED_TRANSPORTS:
        raise ValueError("unsupported transport candidate")
    if not source_uri.strip():
        raise ValueError("source_uri must not be empty")

    # Stage 03 qualifies the transport/runtime boundary. Keep this path codec
    # agnostic so decoder availability cannot masquerade as a transport result.
    return [
        _GST_LAUNCH,
        "-q",
        "rtspsrc",
        f"location={source_uri}",
        f"protocols={transport}",
        "latency=100",
        "name=src",
        "src.",
        "!",
        "application/x-rtp,media=video",
        "!",
        "queue",
        "!",
        "fakesink",
        "sync=false",
        "num-buffers=60",
    ]


def classify_gst_failure(stderr: str) -> int:
    """Map raw GStreamer diagnostics to a bounded secret-safe exit status."""
    normalized = stderr.casefold()

    if any(
        marker in normalized
        for marker in (
            "401",
            "unauthorized",
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
        for marker in (
            "404",
            "not found",
            "resource not found",
            "no such resource",
        )
    ):
        return _GST_SOURCE_FAILURE
    if any(
        marker in normalized
        for marker in (
            "erroneous pipeline",
            "no property",
            "syntax error",
            "no element",
        )
    ):
        return _GST_PIPELINE_FAILURE
    return _GST_OTHER_FAILURE


def run_candidate(transport: str, source_uri: str) -> int:
    """Run one candidate and expose only a coarse, source-free failure status."""
    if shutil.which(_GST_LAUNCH) is None:
        return 127
    try:
        completed = subprocess.run(
            build_gst_argv(transport, source_uri),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            shell=False,
            check=False,
        )
    except OSError:
        return _GST_WRAPPER_OS_FAILURE
    except Exception:
        # Do not propagate exception text because it may embed argv/source data.
        return _GST_WRAPPER_INTERNAL_FAILURE

    if completed.returncode == 0:
        return 0
    stderr = (completed.stderr or b"").decode("utf-8", errors="replace")
    return classify_gst_failure(stderr)


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--transport", choices=_ALLOWED_TRANSPORTS, required=True)
    parser.add_argument("source")
    args = parser.parse_args()
    return run_candidate(args.transport, args.source)


if __name__ == "__main__":
    raise SystemExit(main())
