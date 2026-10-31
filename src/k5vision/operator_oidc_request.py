"""Private single-request worker; parent establishes ownership before stdin."""

from __future__ import annotations

import json
import sys


def main() -> int:
    try:
        from k5vision.operator_oidc import (
            _MAX_JSON,
            _document,
            _fetch_json_in_process,
            _reject_constant,
        )

        raw = sys.stdin.buffer.read(65537)
        if not 1 <= len(raw) <= 65536:
            return 1
        request = json.loads(raw, object_pairs_hook=_document, parse_constant=_reject_constant)
        if type(request) is not dict or set(request) != {"url", "form", "headers"}:
            return 1
        if request["form"] is not None and (
            type(request["form"]) is not dict
            or set(request["form"])
            != {"grant_type", "code", "redirect_uri", "code_verifier", "client_id"}
            or request["form"]["grant_type"] != "authorization_code"
            or any(
                type(value) is not str or len(value) > 4096 for value in request["form"].values()
            )
        ):
            return 1
        if request["headers"] is not None and (
            type(request["headers"]) is not dict
            or set(request["headers"]) - {"Authorization"}
            or any(
                type(value) is not str or len(value) > 32768 or "\r" in value or "\n" in value
                for value in request["headers"].values()
            )
        ):
            return 1
        value = _fetch_json_in_process(**request)
        output = json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        if len(output) > _MAX_JSON:
            return 1
        sys.stdout.buffer.write(output)
        sys.stdout.buffer.flush()
        return 0
    except Exception:
        # Redirects, platform/network exceptions and parsing failures must not
        # expose endpoint query strings, Basic credentials or identity tokens.
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
