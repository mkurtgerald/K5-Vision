"""Secret-safe helpers for transient Stage 03 camera credential selection."""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def parse_cam_cred(raw: str) -> tuple[str, tuple[str, ...]]:
    """Parse the private CAM_CRED note without retaining blank-line layout."""
    values = tuple(line.strip() for line in raw.splitlines() if line.strip())
    if len(values) < 3:
        raise ValueError(
            "camera credential bundle must include a username and at least two passwords"
        )
    username, *passwords = values
    if len(passwords) > 8:
        raise ValueError("camera credential bundle contains too many password candidates")
    return username, tuple(passwords)


def source_without_userinfo(source_uri: str) -> str:
    """Return the private RTSP endpoint with any stale embedded userinfo removed."""
    parsed = urlsplit(source_uri)
    if parsed.scheme.casefold() not in {"rtsp", "rtsps"} or not parsed.netloc:
        raise ValueError("Stage 03 source must be an RTSP URI")

    host_port = parsed.netloc.rsplit("@", 1)[-1]
    if not host_port:
        raise ValueError("Stage 03 source is missing a host")
    return urlunsplit((parsed.scheme, host_port, parsed.path, parsed.query, parsed.fragment))


def selected_credentials(cam_cred: str, index: int) -> tuple[str, str]:
    """Return one private username/password pair for transient runtime use."""
    username, passwords = parse_cam_cred(cam_cred)
    if index < 0 or index >= len(passwords):
        raise ValueError("camera credential candidate index is out of range")
    return username, passwords[index]
