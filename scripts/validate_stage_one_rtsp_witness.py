"""Fail closed on the dedicated, source-free Stage-One RTSP joined receipt."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

_MAX_RECEIPT_BYTES = 8_192
_MAX_COUNTER = 1_000_000
_COUNTERS = {
    "delivered_frames",
    "presentations",
    "analytics_provider_calls",
    "analytics_tracked_detections",
    "analytics_provider_submissions",
    "analytics_provider_completions",
    "analytics_rendered_boxes",
}
_FLAGS = {"windows_live_launch_completed", "analytics_enabled", "rtsp_tcp_joined"}
_FIELDS = (
    _COUNTERS
    | _FLAGS
    | {
        "schema_version",
        "revision",
        "analytics_revision",
        "execution_context",
        "analytics_failures",
    }
)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("duplicate receipt field")
        document[key] = value
    return document


def _reject_constant(_value: str) -> object:
    raise ValueError("invalid receipt constant")


def validate_receipt(path: Path, *, revision: str, analytics_revision: str) -> None:
    """Validate the existing scalar schema without retaining or printing input data."""
    if not all(re.fullmatch(r"[0-9a-fA-F]{40}", sha) for sha in (revision, analytics_revision)):
        raise ValueError("exact expected revisions are required")
    with path.open("rb") as receipt:
        raw = receipt.read(_MAX_RECEIPT_BYTES + 1)
    if len(raw) > _MAX_RECEIPT_BYTES:
        raise ValueError("receipt exceeds size limit")
    document = json.loads(
        raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant
    )
    if type(document) is not dict or document.keys() != _FIELDS:
        raise ValueError("invalid receipt fields")
    if (
        document["schema_version"] != "1"
        or document["revision"] != revision.lower()
        or document["analytics_revision"] != analytics_revision.lower()
        or document["execution_context"] != "reviewed-video-loopback-rtsp-windows-x64"
    ):
        raise ValueError("receipt identity mismatch")
    if any(document[field] is not True for field in _FLAGS):
        raise ValueError("receipt success flags are required")
    failures = document["analytics_failures"]
    if type(failures) is not int or failures != 0:
        raise ValueError("receipt analytics must succeed")
    if any(
        type(document[field]) is not int or not 1 <= document[field] <= _MAX_COUNTER
        for field in _COUNTERS
    ):
        raise ValueError("receipt counters must be positive bounded integers")


def main() -> int:
    try:
        output = os.environ["K5_STAGE_ONE_RTSP_JOINED_ANALYTICS_OUTPUT"]
        if not output:
            raise ValueError("receipt output is required")
        validate_receipt(
            Path(output),
            revision=os.environ["K5_STAGE_ONE_REVISION"],
            analytics_revision=os.environ["ANALYTICS_LAB_SHA"],
        )
    except Exception:
        # Paths, malformed JSON, and exception text must never enter retained logs.
        print("Stage One RTSP joined receipt validation failed")
        return 1
    print("Stage One RTSP joined receipt validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
