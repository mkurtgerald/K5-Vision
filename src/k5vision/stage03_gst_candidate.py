"""Secret-safe Stage 03 GStreamer transport candidate wrapper."""

from __future__ import annotations

import argparse
import shutil
import subprocess

_GST_LAUNCH = "gst-launch-1.0"
_ALLOWED_TRANSPORTS = ("tcp", "udp")


def build_gst_argv(transport: str, source_uri: str) -> list[str]:
    """Build one bounded GStreamer receive/decode pipeline without invoking a shell."""
    if transport not in _ALLOWED_TRANSPORTS:
        raise ValueError("unsupported transport candidate")
    if not source_uri.strip():
        raise ValueError("source_uri must not be empty")

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
        "decodebin",
        "!",
        "fakesink",
        "sync=false",
        "num-buffers=30",
    ]


def run_candidate(transport: str, source_uri: str) -> int:
    """Run one candidate with stdout/stderr suppressed and return only its exit status."""
    if shutil.which(_GST_LAUNCH) is None:
        return 127
    completed = subprocess.run(
        build_gst_argv(transport, source_uri),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        check=False,
    )
    return int(completed.returncode)


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--transport", choices=_ALLOWED_TRANSPORTS, required=True)
    parser.add_argument("source")
    args = parser.parse_args()
    return run_candidate(args.transport, args.source)


if __name__ == "__main__":
    raise SystemExit(main())
