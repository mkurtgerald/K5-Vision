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


def resolve_credential_index(source_uri: str, cam_cred: str) -> int | None:
    """Return the first credential index proven to authenticate the RTSP source."""
    username, passwords = parse_cam_cred(cam_cred)
    for index, password in enumerate(passwords):
        candidate_uri = credentialized_uri(source_uri, username, password)
        if run_gst_uri("tcp", candidate_uri) in _AUTHENTICATED_STATUSES:
            return index
    return None


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    cam_cred = os.getenv("K5_STAGE03_CAM_CRED")
    if not cam_cred:
        return 2
    try:
        index = resolve_credential_index(sys.argv[1], cam_cred)
    except (TypeError, ValueError):
        return 2
    if index is None:
        return 3
    print(index)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
