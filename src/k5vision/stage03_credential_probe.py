"""Select a working private Stage 03 credential candidate without exposing it."""

from __future__ import annotations

import os
import sys

from k5vision.stage03_credentials import credentialized_uri, parse_cam_cred
from k5vision.stage03_gst_candidate import run_gst_uri

# A completed RTSP/RTP run proves authentication. A downstream caps/negotiation
# failure also proves that RTSP authentication and session setup got far enough
# to reach the media boundary; that failure belongs to runtime qualification,
# not credential selection. Other failures do not prove authentication.
_AUTHENTICATED_STATUSES = frozenset({0, 43})


def probe_credential_candidates(
    source_uri: str,
    cam_cred: str,
) -> tuple[int | None, tuple[int, ...]]:
    """Return a proven credential index plus sanitized candidate status classes."""
    username, passwords = parse_cam_cred(cam_cred)
    statuses: list[int] = []
    for index, password in enumerate(passwords):
        candidate_uri = credentialized_uri(source_uri, username, password)
        status = run_gst_uri("tcp", candidate_uri)
        statuses.append(status)
        if status in _AUTHENTICATED_STATUSES:
            return index, tuple(statuses)
    return None, tuple(statuses)


def resolve_credential_index(source_uri: str, cam_cred: str) -> int | None:
    """Return the first credential index proven to authenticate the RTSP source."""
    return probe_credential_candidates(source_uri, cam_cred)[0]


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    cam_cred = os.getenv("K5_STAGE03_CAM_CRED")
    if not cam_cred:
        return 2
    try:
        index, statuses = probe_credential_candidates(sys.argv[1], cam_cred)
    except (TypeError, ValueError):
        return 2
    if index is None:
        # Only stable numeric failure classes are emitted. Candidate ordering,
        # usernames, passwords, source URIs, and raw GStreamer diagnostics remain
        # private. Sorting/deduplicating also avoids mapping a status to a password.
        safe_statuses = ",".join(str(status) for status in sorted(set(statuses)))
        print(f"credential_probe_statuses={safe_statuses}", file=sys.stderr)
        return 3
    print(index)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
