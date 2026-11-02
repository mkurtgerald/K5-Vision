"""One bounded, parent-owned SNTP measurement; never adjusts a clock."""

from __future__ import annotations

import json
import math
import secrets
import socket
import struct
import sys
import time
from datetime import UTC, datetime

_NTP_EPOCH = 2_208_988_800


def _timestamp(raw: bytes, near: float) -> float:
    seconds, fraction = struct.unpack("!II", raw)
    era = round((near + _NTP_EPOCH - seconds) / 2**32)
    return seconds + era * 2**32 - _NTP_EPOCH + fraction / 2**32


def measure(server: str, *, port: int = 123, timeout: float = 2.0) -> dict:
    """Validate an SNTP reply bound to our fresh originate nonce and peer.

    This is an unauthenticated NTP reference comparison, not an authenticated
    time service or evidence that Windows Time itself accepted that reference.
    The owning parent bounds DNS resolution and the entire child lifetime.
    """
    addresses = socket.getaddrinfo(server, port, type=socket.SOCK_DGRAM)
    deadline = time.monotonic() + timeout
    for family, kind, protocol, _name, address in addresses[:4]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        with socket.socket(family, kind, protocol) as wire:
            wire.settimeout(remaining)
            wire.connect(address)
            nonce = secrets.token_bytes(8)
            request = b"\x23" + bytes(39) + nonce
            before_wall, before_elapsed = time.time(), time.monotonic()
            try:
                wire.send(request)
                response = wire.recv(513)
            except OSError:
                continue
            after_wall, after_elapsed = time.time(), time.monotonic()
        if len(response) != 48 or response[24:32] != nonce:
            continue
        leap, version, mode = response[0] >> 6, (response[0] >> 3) & 7, response[0] & 7
        if leap == 3 or version not in {3, 4} or mode != 4 or not 1 <= response[1] <= 15:
            continue
        elapsed = after_elapsed - before_elapsed
        # A simultaneous host step invalidates the comparison rather than
        # making that step look like the NTP server's offset.
        if abs((after_wall - before_wall) - elapsed) > 0.1:
            continue
        received = _timestamp(response[32:40], before_wall)
        transmitted = _timestamp(response[40:48], after_wall)
        if response[32:40] == bytes(8) or response[40:48] == bytes(8):
            continue
        processing = transmitted - received
        if not 0 <= processing <= elapsed + 0.1:
            continue
        offset = ((received - before_wall) + (transmitted - after_wall)) / 2
        uncertainty = max(0, elapsed - processing) / 2
        root_delay = struct.unpack("!i", response[4:8])[0] / 65536
        root_dispersion = struct.unpack("!I", response[8:12])[0] / 65536
        if (
            not all(math.isfinite(v) for v in (offset, uncertainty, root_delay, root_dispersion))
            or abs(offset) > 315_576_000
            or root_delay < 0
            or root_delay > 10
            or root_dispersion > 10
        ):
            continue
        uncertainty += root_delay / 2 + root_dispersion
        return {
            "status": "measured",
            "offset_seconds": round(offset, 6),
            "uncertainty_seconds": round(uncertainty, 6),
            "measured_at": datetime.now(UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "stratum": response[1],
            "sync_verified": abs(offset) + uncertainty <= 2.0,
            "authenticated": False,
        }
    raise ValueError("No valid NTP comparison")


def main() -> int:
    try:
        from k5vision.operator_time_admin import normalize_servers

        raw = sys.stdin.buffer.readline(2049)
        if len(raw) > 2048 or not raw.endswith(b"\n"):
            raise ValueError
        request = json.loads(raw)
        if type(request) is not dict or set(request) != {"server"}:
            raise ValueError
        server = normalize_servers([request["server"]])[0]
        result = measure(server)
        print(json.dumps(result, allow_nan=False), flush=True)
        return 0
    except Exception:
        print(json.dumps({"status": "unavailable"}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
