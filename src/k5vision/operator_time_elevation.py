"""Owned consoleless broker for one native UAC time-administration request."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    try:
        from k5vision.operator_time_admin import TimeAdministrationError, _elevated_helper

        raw = sys.stdin.buffer.readline(8193)
        if len(raw) > 8192 or not raw.endswith(b"\n"):
            raise ValueError
        value = json.loads(raw)
        if type(value) is not dict or set(value) != {"executable", "request", "response"}:
            raise ValueError
        paths = [Path(value[key]) for key in ("executable", "request", "response")]
        if any(not p.is_absolute() or ".." in p.parts for p in paths):
            raise ValueError
        _elevated_helper(*paths)
        print(json.dumps({"code": "completed"}), flush=True)
        return 0
    except TimeAdministrationError as error:
        print(json.dumps({"code": error.code}), flush=True)
        return 1
    except Exception:
        print(json.dumps({"code": "time_operation_failed"}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
